"""9:16 reframing: crop geometry, face tracking and camera-path smoothing.

Strategy (per segment):
* source already ~9:16 -> plain scale
* face found in >= 30% of samples -> crop that follows the face. Faces in small-business
  clips are mostly still, so if the face barely moves we use a *static* crop (rock-steady,
  looks professional); otherwise a smoothed, simplified path becomes a piecewise-linear
  ffmpeg expression for crop ``x`` (evaluated per frame, no extra decode pass).
* no face: landscape -> "fit" (whole frame over a blurred fill: product shots keep the
  product in frame); portrait-ish -> static crop at the plan's focus point.

The math is pure and unit-tested. MediaPipe is an optional extra (``adreel[face]``).
"""

from __future__ import annotations

import contextlib
import itertools
import os
import statistics
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from adreel.log import get_logger
from adreel.schemas.edit_plan import Reframe, ReframeMode

log = get_logger(__name__)

TARGET_ASPECT = 9 / 16
Sample = tuple[float, float | None, float | None]  # (t, cx_norm, cy_norm)


# --------------------------------------------------------------------------- geometry


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def crop_size(
    src_w: int, src_h: int, zoom: float = 1.0, aspect: float = TARGET_ASPECT
) -> tuple[int, int]:
    """Largest ``aspect`` crop inside the source, shrunk by ``zoom``; even dimensions."""
    cw: float
    ch: float
    if src_w / src_h > aspect:
        ch = src_h
        cw = ch * aspect
    else:
        cw = src_w
        ch = cw / aspect
    return _even(cw / zoom), _even(ch / zoom)


def crop_offset(center: float, src: int, crop: int) -> int:
    """Top/left offset placing ``center`` (pixels) in the middle of the crop, clamped."""
    return int(min(max(center - crop / 2, 0), src - crop))


# --------------------------------------------------------------------------- smoothing


def fill_gaps(
    samples: Sequence[tuple[float, float | None]], default: float
) -> list[tuple[float, float]]:
    """Forward-fill missed detections, back-fill leading gaps, ``default`` if nothing found."""
    first = next((v for _, v in samples if v is not None), default)
    out: list[tuple[float, float]] = []
    last = first
    for t, v in samples:
        last = v if v is not None else last
        out.append((t, last))
    return out


def median_filter(values: Sequence[float], window: int = 5) -> list[float]:
    """Median filter (kills single-frame detection glitches); symmetric shrink at edges."""
    n, half = len(values), window // 2
    out = []
    for i in range(n):
        h = min(half, i, n - 1 - i)
        out.append(statistics.median(values[i - h : i + h + 1]))
    return out


def centered_mean(values: Sequence[float], half_window: int = 3) -> list[float]:
    """Symmetric moving average; the window shrinks symmetrically at the edges.

    Zero lag, and exact on linear pans (a one-way EMA lags; forward/backward averaging
    biases the endpoints).
    """
    n = len(values)
    out = []
    for i in range(n):
        h = min(half_window, i, n - 1 - i)
        win = values[i - h : i + h + 1]
        out.append(sum(win) / len(win))
    return out


def simplify(points: Sequence[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker on (t, x): fewest keyframes within ``epsilon`` px of the path."""
    if len(points) <= 2:
        return list(points)
    (t0, x0), (t1, x1) = points[0], points[-1]
    worst, idx = -1.0, 0
    for i in range(1, len(points) - 1):
        t, x = points[i]
        interp = x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else x0
        d = abs(x - interp)
        if d > worst:
            worst, idx = d, i
    if worst <= epsilon:
        return [points[0], points[-1]]
    left = simplify(points[: idx + 1], epsilon)
    return left[:-1] + simplify(points[idx:], epsilon)


def piecewise_linear_expr(keyframes: Sequence[tuple[float, float]]) -> str:
    """ffmpeg expression of ``t`` that linearly interpolates between keyframes."""
    if not keyframes:
        raise ValueError("no keyframes")
    if len(keyframes) == 1:
        return f"{keyframes[0][1]:.0f}"
    expr = f"{keyframes[-1][1]:.0f}"
    for (ta, xa), (tb, xb) in reversed(list(itertools.pairwise(keyframes))):
        seg = f"{xa:.1f}+({xb - xa:.1f})*(t-{ta:.3f})/{max(tb - ta, 1e-3):.3f}"
        expr = f"if(lt(t,{tb:.3f}),{seg},{expr})"
    return f"if(lt(t,{keyframes[0][0]:.3f}),{keyframes[0][1]:.0f},{expr})"


@dataclass(frozen=True)
class CameraPath:
    x_expr: str  # constant or expression of t
    static: bool
    keyframes: int


def plan_camera_path(
    centers: Sequence[tuple[float, float]],
    src_w: int,
    crop_w: int,
    *,
    static_tolerance: float = 0.06,
    epsilon_px: float = 6.0,
    max_keyframes: int = 40,
) -> CameraPath:
    """From smoothed face centres (pixels) to a crop-x path."""
    xs = [c for _, c in centers]
    lo, hi = _percentile(xs, 0.1), _percentile(xs, 0.9)
    if hi - lo <= static_tolerance * src_w or len(xs) < 3:
        x = crop_offset(statistics.median(xs), src_w, crop_w)
        return CameraPath(str(x), True, 1)
    smooth = centered_mean(median_filter(xs, 5), 3)
    pts = [
        (t, float(crop_offset(c, src_w, crop_w))) for (t, _), c in zip(centers, smooth, strict=True)
    ]
    eps = epsilon_px
    kf = simplify(pts, eps)
    while len(kf) > max_keyframes:
        eps *= 1.5
        kf = simplify(pts, eps)
    return CameraPath(piecewise_linear_expr(kf), False, len(kf))


def _percentile(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


# --------------------------------------------------------------------------- face tracking


@contextlib.contextmanager
def quiet_native_stderr() -> Iterator[None]:
    """Silence C++ logging (MediaPipe / LiteRT INFO and W0000 lines) written straight to fd 2.

    POSIX only: on Windows, redirecting the console's fd is not worth the risk for
    cosmetic output.
    """
    if os.name == "nt":
        yield
        return
    try:
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        yield
        return
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


class FaceTracker(Protocol):
    def sample(
        self, path: Path, start: float, end: float, src_w: int, src_h: int
    ) -> list[Sample]: ...


class FaceTrackingUnavailable(RuntimeError):
    pass


def square_tiles(width: int, height: int, side: int, overlap: float = 0.5) -> list[tuple[int, int]]:
    """Top-left corners of overlapping ``side``-px square tiles covering the frame."""
    side = min(side, width, height)
    step = max(1, int(side * (1 - overlap)))

    def axis(n: int) -> list[int]:
        pos = list(range(0, n - side + 1, step))
        if pos[-1] != n - side:
            pos.append(n - side)
        return pos

    return [(x, y) for y in axis(height) for x in axis(width)]


class MediaPipeFaceTracker:
    """Samples frames via an ffmpeg pipe and runs BlazeFace (full-range) on tiles.

    BlazeFace squeezes its input to ~192 px, so a person filmed from 1–2 m in landscape
    (face ≈ 3–8% of frame width) is missed on the whole frame. Running it on overlapping
    square tiles of 0.6 × the short side detected every face in our scale tests at
    ~50 ms per sampled frame on one CPU core.
    """

    def __init__(
        self,
        model_path: Path,
        ffmpeg_bin: str = "ffmpeg",
        sample_fps: float = 3.0,
        analysis_short_side: int = 540,
        tile_frac: float = 0.6,
        min_confidence: float = 0.5,
    ) -> None:
        # Silence MediaPipe/TFLite native INFO/WARNING spam (must be set before import).
        os.environ.setdefault("GLOG_minloglevel", "2")
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        try:
            import mediapipe as mp  # noqa: F401
            import numpy  # noqa: F401
            from mediapipe.tasks.python import vision
            from mediapipe.tasks.python.core.base_options import BaseOptions
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise FaceTrackingUnavailable("install adreel[face] for face tracking") from exc
        if not model_path.is_file():
            raise FaceTrackingUnavailable(f"face model not found: {model_path}")
        opts = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.IMAGE,
            min_detection_confidence=min_confidence,
        )
        try:
            with quiet_native_stderr():
                self._detector = vision.FaceDetector.create_from_options(opts)
        except (OSError, RuntimeError) as exc:  # e.g. missing libEGL/libGLES system libs
            raise FaceTrackingUnavailable(f"mediapipe failed to start: {exc}") from exc
        self.ffmpeg_bin = ffmpeg_bin
        self.sample_fps = sample_fps
        self.short_side = analysis_short_side
        self.tile_frac = tile_frac

    def sample(self, path: Path, start: float, end: float, src_w: int, src_h: int) -> list[Sample]:

        k = min(1.0, self.short_side / min(src_w, src_h))
        w, h = max(2, round(src_w * k / 2) * 2), max(2, round(src_h * k / 2) * 2)
        cmd = [
            self.ffmpeg_bin,
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-i",
            str(path),
            "-vf",
            f"fps={self.sample_fps},scale={w}:{h}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
        raw = subprocess.run(cmd, capture_output=True, check=True, timeout=300).stdout
        frame_bytes = w * h * 3
        side = int(min(w, h) * self.tile_frac)
        tiles = square_tiles(w, h, side)
        out: list[Sample] = []
        with quiet_native_stderr():
            self._detect_frames(raw, w, h, frame_bytes, side, tiles, out)
        return out

    def _detect_frames(self, raw: bytes, w: int, h: int, frame_bytes: int, side: int,
                       tiles: list[tuple[int, int]], out: list[Sample]) -> None:  # fmt: skip
        import mediapipe as mp
        import numpy as np

        for i in range(len(raw) // frame_bytes):
            frame = np.frombuffer(raw, np.uint8, frame_bytes, i * frame_bytes).reshape(h, w, 3)
            best: tuple[float, float, float] | None = None  # (score, cx_px, cy_px)
            for tx, ty in tiles:
                tile = np.ascontiguousarray(frame[ty : ty + side, tx : tx + side])
                for d in self._detector.detect(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=tile)
                ).detections:
                    bb = d.bounding_box
                    conf = d.categories[0].score if d.categories else 1.0
                    score = bb.width * bb.height * conf  # prefer the biggest, surest face
                    if best is None or score > best[0]:
                        # Aim slightly above the face centre: headroom looks right in 9:16.
                        best = (
                            score,
                            tx + bb.origin_x + bb.width / 2,
                            ty + bb.origin_y + bb.height * 0.4,
                        )
            t = i / self.sample_fps
            out.append((t, best[1] / w, best[2] / h) if best else (t, None, None))

    def close(self) -> None:
        with quiet_native_stderr():
            self._detector.close()


# --------------------------------------------------------------------------- decision


@dataclass(frozen=True)
class ReframeDecision:
    kind: Literal["scale", "crop", "fit"]
    crop_w: int = 0
    crop_h: int = 0
    x_expr: str = "0"
    y: int = 0
    reason: str = ""

    def video_filter(self, out_w: int, out_h: int, src_label: str, out_label: str) -> str:
        """filter_complex snippet from ``[src_label]`` to ``[out_label]``."""
        pre = f"[{src_label}]setpts=PTS-STARTPTS"
        if self.kind == "scale":
            return f"{pre},scale={out_w}:{out_h}:flags=lanczos,setsar=1[{out_label}]"
        if self.kind == "crop":
            return (
                f"{pre},crop=w={self.crop_w}:h={self.crop_h}:x='{self.x_expr}':y={self.y},"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[{out_label}]"
            )
        bw, bh = _even(out_w / 8), _even(out_h / 8)
        return (
            f"{pre},split=2[{out_label}_b][{out_label}_f];"
            f"[{out_label}_b]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"gblur=sigma=4,scale={out_w}:{out_h},eq=brightness=-0.07:saturation=0.85[{out_label}_bb];"
            f"[{out_label}_f]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[{out_label}_ff];"
            f"[{out_label}_bb][{out_label}_ff]overlay=(W-w)/2:(H-h)/2,setsar=1[{out_label}]"
        )


def decide_reframe(
    reframe: Reframe,
    src_w: int,
    src_h: int,
    *,
    zoom: float = 1.0,
    samples: Sequence[Sample] | None = None,
    min_face_ratio: float = 0.3,
) -> ReframeDecision:
    """Pick scale / crop / fit and compute the crop path for one segment."""
    zoom = max(zoom, reframe.zoom)
    aspect = src_w / src_h
    if abs(aspect - TARGET_ASPECT) < 0.015 and zoom == 1.0 and reframe.mode is not ReframeMode.FIT:
        return ReframeDecision("scale", reason="already 9:16")
    if reframe.mode is ReframeMode.FIT:
        return ReframeDecision("fit", reason="requested")

    cw, ch = crop_size(src_w, src_h, zoom)

    def static_focus(reason: str) -> ReframeDecision:
        x = crop_offset(reframe.focus_x * src_w, src_w, cw)
        y = crop_offset(reframe.focus_y * src_h, src_h, ch)
        return ReframeDecision("crop", cw, ch, str(x), y, reason)

    if reframe.mode is ReframeMode.CENTER:
        return static_focus("focus point")

    hits = [(t, cx, cy) for t, cx, cy in (samples or []) if cx is not None]
    if samples and len(hits) >= max(1, min_face_ratio * len(samples)):
        centers = fill_gaps(
            [(t, cx * src_w if cx is not None else None) for t, cx, _ in samples], src_w / 2
        )
        path = plan_camera_path(centers, src_w, cw)
        cy = statistics.median(c for _, _, c in hits if c is not None)
        y = crop_offset(cy * src_h, src_h, ch)
        return ReframeDecision(
            "crop",
            cw,
            ch,
            path.x_expr,
            y,
            f"face ({'static' if path.static else f'{path.keyframes} keyframes'})",
        )

    if reframe.mode is ReframeMode.AUTO and aspect > 1.1:
        return ReframeDecision("fit", reason="landscape, no face")
    return static_focus("no face")
