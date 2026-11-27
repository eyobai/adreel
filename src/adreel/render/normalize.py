"""Normalise raw phone clips into a uniform intermediate format.

Phone footage is messy: variable frame rate, rotation metadata, 10-bit HLG (iPhone),
60 fps, 4K, sometimes no audio track. Everything downstream assumes:

* constant frame rate = template fps, SAR 1:1, yuv420p, long side <= ``max_long_side``
* HDR tone-mapped to SDR BT.709 (otherwise iPhone clips look washed out)
* stereo 48 kHz PCM, loudness-normalised with *two-pass linear* loudnorm (no pumping);
  a silent track is synthesised when the clip has no audio, so concat never breaks
* short GOP so per-segment trims are fast and frame-accurate

Outputs are cached by (source identity, params) and written atomically.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from adreel.log import get_logger
from adreel.render.ffmpeg import FFmpeg, MediaInfo

log = get_logger(__name__)

_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


@dataclass(frozen=True)
class NormalizeParams:
    fps: int = 30
    max_long_side: int = 1920
    target_lufs: float = -14.0
    true_peak_db: float = -1.5
    crf: int = 17

    def key(self) -> str:
        return f"{self.fps}|{self.max_long_side}|{self.target_lufs}|{self.true_peak_db}|{self.crf}"


@dataclass(frozen=True)
class NormalizedClip:
    clip_id: str
    source: Path
    path: Path
    info: MediaInfo  # of the normalised file
    source_info: MediaInfo


def target_size(width: int, height: int, max_long_side: int) -> tuple[int, int]:
    """Scale (w, h) so the long side is <= max_long_side; both even; aspect preserved."""
    scale = min(1.0, max_long_side / max(width, height))
    w = max(2, round(width * scale / 2) * 2)
    h = max(2, round(height * scale / 2) * 2)
    return w, h


def parse_loudnorm_json(stderr: str) -> dict[str, str] | None:
    """Extract the JSON block loudnorm prints at the end of stderr."""
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, flags=re.S)
    if not matches:
        return None
    data: dict[str, str] = json.loads(matches[-1])
    return data


def loudnorm_filter(measured: dict[str, str] | None, target_lufs: float, tp: float) -> str:
    base = f"loudnorm=I={target_lufs}:TP={tp}:LRA=11"
    if measured is None:
        return base  # single pass (dynamic) fallback
    try:
        if float(measured["input_i"]) < -70:  # effectively silence: don't amplify noise
            return "anull"
    except ValueError:  # "-inf"
        return "anull"
    return (
        f"{base}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true"
    )


def normalize_clip(
    ff: FFmpeg, clip_id: str, src: Path, out_dir: Path, params: NormalizeParams
) -> NormalizedClip:
    src = src.resolve()
    st = src.stat()
    key_src = f"{src}|{st.st_size}|{st.st_mtime_ns}|{params.key()}"
    key = hashlib.sha256(key_src.encode()).hexdigest()[:20]
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"norm_{key}.mkv"
    src_info = ff.probe(src)
    if dst.exists():
        log.info("normalize.cached", clip=clip_id)
        return NormalizedClip(clip_id, src, dst, ff.probe(dst), src_info)

    tw, th = target_size(src_info.width, src_info.height, params.max_long_side)
    vf = []
    if src_info.is_hdr:
        vf.append(_TONEMAP)
    vf += [f"scale={tw}:{th}:flags=lanczos", "setsar=1", f"fps={params.fps}", "format=yuv420p"]

    args: list[str] = ["-i", str(src)]
    if src_info.has_audio:
        stderr = ff.run(
            [
                "-i",
                str(src),
                "-vn",
                "-af",
                f"loudnorm=I={params.target_lufs}:TP={params.true_peak_db}:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
            capture_stderr=True,
        )
        af = loudnorm_filter(parse_loudnorm_json(stderr), params.target_lufs, params.true_peak_db)
        args += ["-map", "0:v:0", "-map", "0:a:0"]
        args += ["-af", f"{af},aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo"]
    else:
        args += [
            "-f",
            "lavfi",
            "-t",
            f"{src_info.duration:.3f}",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    gop = str(params.fps)
    args += [
        "-vf",
        ",".join(vf),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(params.crf),
        "-g",
        gop,
        "-keyint_min",
        gop,
        "-sc_threshold",
        "0",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-map_metadata",
        "-1",
        "-shortest",
    ]
    tmp = dst.with_suffix(".part.mkv")
    ff.run([*args, str(tmp)])
    tmp.replace(dst)
    info = ff.probe(dst)
    log.info(
        "normalize.done",
        clip=clip_id,
        src=f"{src_info.width}x{src_info.height}@{src_info.fps:.2f}",
        out=f"{info.width}x{info.height}@{info.fps:.0f}",
        hdr=src_info.is_hdr,
        audio=src_info.has_audio,
        duration=round(info.duration, 2),
    )
    return NormalizedClip(clip_id, src, dst, info, src_info)
