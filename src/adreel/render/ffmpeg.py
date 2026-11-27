"""Thin, safe wrappers around the ffmpeg / ffprobe binaries.

* Arguments are always passed as a list (no shell), so user-provided paths/text are inert.
* Every call has a hard timeout (watchdog kills the process).
* ``-progress pipe:1`` lets callers report percentage to the bot's status message.
* On failure the tail of stderr is attached to the exception for logs.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from adreel.log import get_logger

log = get_logger(__name__)

ProgressCb = Callable[[float], None]

# Every intermediate we write carries identical colour metadata. ffmpeg 7+ rebuilds the
# whole filter graph when a concatenated input changes stream parameters (colour tags
# included); the rebuilt graph then waits forever on looped/side inputs that already
# ended. Uniform tags (plus -reinit_filter 0 on the compose input) prevent that.
BT709_FILTER = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
BT709_TAGS = [
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
    "-color_range", "tv", "-chroma_sample_location", "left",
]  # fmt: skip
SD_MATRICES = {"bt470bg", "smpte170m", "bt601"}  # seconds of output written so far


class FFmpegError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int | None, stderr_tail: str) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        reason = "timed out" if returncode is None else f"exited with {returncode}"
        super().__init__(f"{Path(cmd[0]).name} {reason}:\n{stderr_tail}")


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int  # display width (rotation applied)
    height: int  # display height (rotation applied)
    fps: float
    has_audio: bool
    rotation: int
    is_hdr: bool
    pix_fmt: str | None
    color_space: str | None = None  # matrix, e.g. "bt709", "bt470bg", "smpte170m"

    @property
    def aspect(self) -> float:
        return self.width / self.height


class FFmpeg:
    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        timeout_s: int = 900,
        threads: int = 0,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.timeout_s = timeout_s
        self.threads = threads

    # ------------------------------------------------------------------ ffmpeg

    def run(
        self,
        args: Sequence[str],
        *,
        progress: ProgressCb | None = None,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        capture_stderr: bool = False,
        cwd: Path | None = None,
    ) -> str:
        """Run ffmpeg with ``args`` (without the binary). Returns stderr text if requested."""
        cmd = [self.ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-nostats"]
        cmd += ["-progress", "pipe:1"]
        if self.threads:
            cmd += ["-threads", str(self.threads)]
        cmd += list(args)
        timeout = timeout_s or self.timeout_s
        t0 = time.monotonic()
        log.debug("ffmpeg.start", cmd=_short(cmd))
        with tempfile.TemporaryFile(mode="w+b") as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, env=env, cwd=cwd)
            timed_out = threading.Event()

            def _kill() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(timeout, _kill)
            timer.start()
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    if progress is None:
                        continue
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        # Both keys are microseconds (ffmpeg's historic naming bug).
                        val = line.split("=", 1)[1]
                        if val.lstrip("-").isdigit():
                            progress(max(0.0, int(val) / 1_000_000))
                proc.wait()
            finally:
                timer.cancel()
            errf.seek(0)
            stderr = errf.read().decode("utf-8", "replace")
        if timed_out.is_set() or proc.returncode != 0:
            raise FFmpegError(cmd, None if timed_out.is_set() else proc.returncode, _tail(stderr))
        log.debug("ffmpeg.done", seconds=round(time.monotonic() - t0, 2))
        return stderr if capture_stderr else ""

    # ------------------------------------------------------------------ ffprobe

    def probe(self, path: Path) -> MediaInfo:
        cmd = [
            self.ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
        except subprocess.CalledProcessError as exc:
            stderr = _tail(exc.stderr.decode("utf-8", "replace"))
            raise FFmpegError(cmd, exc.returncode, stderr) from exc
        except subprocess.TimeoutExpired:
            raise FFmpegError(cmd, None, "ffprobe timed out") from None
        data = json.loads(out)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise FFmpegError(cmd, 0, f"{path}: no video stream")
        rotation = _rotation(video)
        w, h = int(video["width"]), int(video["height"])
        if rotation % 180 != 0:
            w, h = h, w
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
        return MediaInfo(
            path=path,
            duration=duration,
            width=w,
            height=h,
            fps=_fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate")) or 30.0,
            has_audio=any(s.get("codec_type") == "audio" for s in streams),
            rotation=rotation,
            is_hdr=video.get("color_transfer") in {"arib-std-b67", "smpte2084"},
            pix_fmt=video.get("pix_fmt"),
            color_space=video.get("color_space"),
        )


def _rotation(stream: dict[str, object]) -> int:
    for sd in stream.get("side_data_list", []) or []:  # type: ignore[attr-defined]
        if isinstance(sd, dict) and "rotation" in sd:
            return int(float(sd["rotation"])) % 360
    tags = stream.get("tags") or {}
    if isinstance(tags, dict) and "rotate" in tags:
        return int(tags["rotate"]) % 360
    return 0


def _fps(rate: object) -> float | None:
    if not isinstance(rate, str) or rate in {"0/0", ""}:
        return None
    try:
        f = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return None
    return f if 1 <= f <= 240 else None


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _short(cmd: Sequence[str], limit: int = 600) -> str:
    s = " ".join(cmd)
    return s if len(s) <= limit else s[:limit] + "…"
