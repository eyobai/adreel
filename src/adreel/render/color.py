"""Automatic colour correction for phone footage, plus a few brand "looks".

Each normalised clip is measured once (ffmpeg ``signalstats`` on a few frames per second,
cached next to the clip): average brightness, tonal spread and saturation. From that we
compute a gentle correction, then multiply in the look the planner picked:

* dark clips get lifted (gamma), bright ones calmed; small errors are left alone
* flat, low-contrast footage gets more contrast
* dull colour gets more saturation, garish colour a little less
* looks: natural (correction only), vivid (food / products), warm (cafés), cool, none

Everything is clamped so a bad measurement can never wreck a clip. Pure maths below is
unit-tested; ``measure`` is the only part that runs ffmpeg.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from adreel.log import get_logger
from adreel.render.ffmpeg import FFmpeg

log = get_logger(__name__)

LOOKS = ("natural", "vivid", "warm", "cool", "none")
# (saturation x, contrast x, warmth): warmth shifts midtones red(+)/blue(-)
_LOOK_PARAMS: dict[str, tuple[float, float, float]] = {
    "natural": (1.0, 1.0, 0.0),
    "vivid": (1.22, 1.07, 0.0),
    "warm": (1.08, 1.03, 0.05),
    "cool": (1.0, 1.03, -0.05),
}
TARGET_LUMA = 0.46  # mid-grey-ish average for social video
LUMA_DEADBAND = 0.06
TARGET_SAT = 38.0  # signalstats SATAVG of nicely saturated phone footage


@dataclass(frozen=True)
class ColorStats:
    yavg: float  # 8-bit luma average (16..235 nominal)
    ylow: float  # 10th percentile
    yhigh: float  # 90th percentile
    satavg: float
    frames: int


@dataclass(frozen=True)
class Grade:
    gamma: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    warmth: float = 0.0

    @property
    def is_identity(self) -> bool:
        return (
            abs(self.gamma - 1) < 0.01
            and abs(self.contrast - 1) < 0.01
            and abs(self.saturation - 1) < 0.01
            and abs(self.warmth) < 0.005
        )

    def filter(self) -> str:
        """ffmpeg filter chain ('' when nothing to do)."""
        if self.is_identity:
            return ""
        # Two passes on purpose: ffmpeg's eq applies contrast (around mid-grey) *before*
        # gamma, which would crush a dark clip's shadows before lifting them. Brighten
        # first, then add contrast around the corrected mid-tones.
        parts = []
        if abs(self.gamma - 1) >= 0.01:
            parts.append(f"eq=gamma={self.gamma:.3f}")
        if abs(self.contrast - 1) >= 0.01 or abs(self.saturation - 1) >= 0.01:
            parts.append(f"eq=contrast={self.contrast:.3f}:saturation={self.saturation:.3f}")
        if abs(self.warmth) >= 0.005:
            w = self.warmth
            parts.append(f"colorbalance=rm={w:.3f}:bm={-w:.3f}:rh={w / 2:.3f}:bh={-w / 2:.3f}")
        return ",".join(parts)


_STAT = re.compile(r"lavfi\.signalstats\.(YAVG|YLOW|YHIGH|SATAVG)=([\d.]+)")


def parse_signalstats(text: str) -> ColorStats | None:
    values: dict[str, list[float]] = {"YAVG": [], "YLOW": [], "YHIGH": [], "SATAVG": []}
    for key, val in _STAT.findall(text):
        values[key].append(float(val))
    n = len(values["YAVG"])
    if n == 0:
        return None

    def mean(k: str) -> float:
        return sum(values[k]) / max(1, len(values[k]))

    return ColorStats(mean("YAVG"), mean("YLOW"), mean("YHIGH"), mean("SATAVG"), n)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def correction(stats: ColorStats | None, look: str = "natural") -> Grade:
    if look == "none":
        return Grade()
    sat_x, con_x, warmth = _LOOK_PARAMS.get(look, _LOOK_PARAMS["natural"])
    gamma = contrast = saturation = 1.0
    if stats is not None:
        m = _clamp((stats.yavg - 16) / 219, 0.03, 0.97)
        if abs(m - TARGET_LUMA) > LUMA_DEADBAND:
            gamma = _clamp(math.log(m) / math.log(TARGET_LUMA), 0.8, 1.45)
        # tonal spread *after* the gamma lift (dark footage gains spread from it)
        lo = _clamp((stats.ylow - 16) / 219, 0.0, 1.0) ** (1 / gamma)
        hi = _clamp((stats.yhigh - 16) / 219, 0.0, 1.0) ** (1 / gamma)
        spread = hi - lo
        mid = m ** (1 / gamma)  # eq's contrast pivots at mid-grey: only safe near it
        if spread < 0.65 and 0.32 <= mid <= 0.68:
            contrast = _clamp(0.72 / max(spread, 0.2), 1.0, 1.3)
        if stats.satavg < 25:
            saturation = _clamp(TARGET_SAT / max(stats.satavg, 8.0), 1.0, 1.35)
        elif stats.satavg > 70:
            saturation = _clamp(60.0 / stats.satavg, 0.85, 1.0)
    return Grade(
        gamma=round(gamma, 3),
        contrast=round(_clamp(contrast * con_x, 0.9, 1.35), 3),
        saturation=round(_clamp(saturation * sat_x, 0.8, 1.6), 3),
        warmth=warmth,
    )


def measure(ff: FFmpeg, clip: Path, sample_fps: float = 2.0) -> ColorStats | None:
    """Colour statistics of a (normalised) clip, cached as ``<clip>.color.json``."""
    cache = clip.with_suffix(".color.json")
    if cache.is_file():
        try:
            return ColorStats(**json.loads(cache.read_text()))
        except (ValueError, TypeError):
            pass
    with tempfile.TemporaryDirectory() as tmp:
        # relative stats file + cwd: no Windows drive letters inside a filter argument
        ff.run(
            [
                "-i",
                str(clip.resolve()),
                "-an",
                "-vf",
                f"fps={sample_fps},scale=160:-2,signalstats,metadata=print:file=stats.txt",
                "-f",
                "null",
                "-",
            ],
            cwd=Path(tmp),
        )
        stats_file = Path(tmp) / "stats.txt"
        stats = parse_signalstats(stats_file.read_text()) if stats_file.is_file() else None
    if stats is not None:
        cache.write_text(json.dumps(asdict(stats)))
    log.info("color.measured", clip=clip.name, stats=asdict(stats) if stats else None)
    return stats
