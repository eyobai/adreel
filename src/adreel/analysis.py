"""Cheap, local media analysis that feeds the planner (and saves API money).

* silences: ffmpeg ``silencedetect``. Clips with no speech skip transcription entirely.
* scenes: PySceneDetect content detector on the low-res proxy (fast).
* proxy: small 360p/10 fps H.264 + mono AAC for Gemini (it samples ~1 fps anyway).
* audio: mono 16 kHz MP3 for speech-to-text (small uploads on slow links).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from adreel.log import get_logger
from adreel.render.ffmpeg import FFmpeg

log = get_logger(__name__)

Interval = tuple[float, float]

_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silencedetect(stderr: str, duration: float) -> list[Interval]:
    """Parse ``silencedetect`` output into [start, end) intervals clamped to the clip."""
    out: list[Interval] = []
    start: float | None = None
    for line in stderr.splitlines():
        if m := _SIL_START.search(line):
            start = max(0.0, float(m.group(1)))
        elif (m := _SIL_END.search(line)) and start is not None:
            out.append((start, min(float(m.group(1)), duration)))
            start = None
    if start is not None and start < duration:
        out.append((start, duration))  # trailing silence runs to the end
    return [(s, e) for s, e in out if e > s]


def complement(intervals: list[Interval], duration: float, min_len: float = 0.05) -> list[Interval]:
    """Gaps between intervals inside [0, duration] (e.g. speech spans from silences)."""
    out, t = [], 0.0
    for s, e in sorted(intervals):
        if s - t >= min_len:
            out.append((t, s))
        t = max(t, e)
    if duration - t >= min_len:
        out.append((t, duration))
    return out


def detect_silences(
    ff: FFmpeg, path: Path, duration: float, *, noise_db: float = -35.0, min_silence: float = 0.3
) -> list[Interval]:
    stderr = ff.run(
        [
            "-i",
            str(path),
            "-vn",
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        capture_stderr=True,
    )
    return parse_silencedetect(stderr, duration)


def detect_scenes(path: Path, threshold: float = 27.0) -> list[float]:
    """Cut times (seconds) between shots. Empty if PySceneDetect isn't installed."""
    try:
        from scenedetect import ContentDetector, detect
    except ImportError:  # pragma: no cover
        log.warning("scenes.unavailable", reason="scenedetect not installed")
        return []
    scenes = detect(str(path), ContentDetector(threshold=threshold, min_scene_len=10))
    return [round(start.get_seconds(), 3) for start, _ in scenes[1:]]


def extract_speech_audio(ff: FFmpeg, src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    ff.run(
        [
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "48k",
            str(dst),
        ]
    )
    return dst


def make_proxy(ff: FFmpeg, src: Path, dst: Path, short_side: int = 360, fps: int = 10) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    scale = f"scale='if(lt(iw,ih),{short_side},-2)':'if(lt(iw,ih),-2,{short_side})'"
    ff.run(
        [
            "-i",
            str(src),
            "-vf",
            f"{scale},fps={fps}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "32",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "1",
            "-b:a",
            "32k",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )
    return dst


@dataclass(frozen=True)
class SpeechStats:
    speech_seconds: float
    speech_ratio: float

    @property
    def has_speech(self) -> bool:
        # >= 1.5 s of non-silent audio and not just a constant hum (silence detected somewhere
        # or plenty of signal). Conservative: when unsure, transcribe.
        return self.speech_seconds >= 1.5


def speech_stats(silences: list[Interval], duration: float) -> SpeechStats:
    silent = sum(e - s for s, e in silences)
    speech = max(0.0, duration - silent)
    return SpeechStats(speech, speech / duration if duration else 0.0)
