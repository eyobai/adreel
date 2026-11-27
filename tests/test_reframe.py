from __future__ import annotations

import pytest

from adreel.render.reframe import (
    centered_mean,
    crop_offset,
    crop_size,
    decide_reframe,
    fill_gaps,
    median_filter,
    piecewise_linear_expr,
    plan_camera_path,
    simplify,
    square_tiles,
)
from adreel.schemas.edit_plan import Reframe, ReframeMode


def test_crop_size_landscape_and_portrait() -> None:
    assert crop_size(1920, 1080) == (606, 1080)
    w, h = crop_size(1080, 1440)  # 3:4 portrait: width-limited
    assert w == 810 and h == 1440
    w, h = crop_size(1920, 1080, zoom=1.2)
    assert (w, h) == (506, 900)
    assert w % 2 == 0 and h % 2 == 0


def test_crop_offset_clamps() -> None:
    assert crop_offset(0, 1920, 606) == 0
    assert crop_offset(1920, 1920, 606) == 1314
    assert crop_offset(960, 1920, 606) == 657


def test_fill_gaps() -> None:
    assert fill_gaps([(0, None), (1, 5.0), (2, None), (3, 7.0)], 0) == [
        (0, 5.0),
        (1, 5.0),
        (2, 5.0),
        (3, 7.0),
    ]
    assert fill_gaps([(0, None)], 42.0) == [(0, 42.0)]


def test_filters_have_no_edge_bias_on_linear_motion() -> None:
    ramp = [float(i * 10) for i in range(12)]
    assert median_filter(ramp, 5) == ramp
    assert centered_mean(ramp, 3) == pytest.approx(ramp)


def test_median_kills_glitch() -> None:
    assert median_filter([1, 1, 50, 1, 1], 5)[2] == 1


def test_simplify_line_to_two_points() -> None:
    pts = [(t / 10, t * 3.0) for t in range(50)]
    assert simplify(pts, 1.0) == [pts[0], pts[-1]]
    corner = [(0, 0.0), (1, 10.0), (2, 20.0), (3, 20.0), (4, 20.0)]
    assert (2, 20.0) in simplify(corner, 0.5)


def _eval_ffmpeg_expr(expr: str, t: float) -> float:
    """Evaluate the if/lt subset of ffmpeg's expression language in Python."""
    env = {"if_": lambda c, a, b: a if c else b, "lt": lambda a, b: a < b, "t": t}
    return float(eval(expr.replace("if(", "if_("), {"__builtins__": {}}, env))


def test_piecewise_expr_values() -> None:
    kf = [(0.0, 100.0), (2.0, 300.0), (4.0, 300.0)]
    expr = piecewise_linear_expr(kf)
    for t, want in [(-1, 100), (0, 100), (1.0, 200), (2.0, 300), (3.0, 300), (9, 300)]:
        assert _eval_ffmpeg_expr(expr, t) == pytest.approx(want, abs=0.5)
    assert piecewise_linear_expr([(0, 42.0)]) == "42"


def test_camera_path_static_when_subject_still() -> None:
    centers = [(i / 3, 960.0 + (i % 3) * 10) for i in range(30)]
    path = plan_camera_path(centers, 1920, 606)
    assert path.static and path.x_expr == str(crop_offset(970, 1920, 606))


def test_camera_path_follows_pan_with_few_keyframes() -> None:
    centers = [(i / 3, 300.0 + i * 70.0) for i in range(18)]
    path = plan_camera_path(centers, 1920, 606)
    assert not path.static and path.keyframes <= 4


def test_square_tiles_cover_frame() -> None:
    tiles = square_tiles(960, 540, 324)
    assert (0, 0) in tiles and (960 - 324, 540 - 324) in tiles
    assert all(x + 324 <= 960 and y + 324 <= 540 for x, y in tiles)


@pytest.mark.parametrize(
    ("mode", "w", "h", "kind"),
    [
        (ReframeMode.AUTO, 1080, 1920, "scale"),
        (ReframeMode.AUTO, 1920, 1080, "fit"),
        (ReframeMode.AUTO, 1080, 1440, "crop"),
        (ReframeMode.FIT, 1080, 1920, "fit"),
        (ReframeMode.CENTER, 1920, 1080, "crop"),
        (ReframeMode.FACE, 1920, 1080, "crop"),  # no samples -> focus crop
    ],
)
def test_decide_without_faces(mode: ReframeMode, w: int, h: int, kind: str) -> None:
    assert decide_reframe(Reframe(mode=mode), w, h).kind == kind


def test_decide_with_faces_tracks() -> None:
    samples = [(i / 3, 0.25, 0.4) for i in range(9)] + [(3.0, None, None)]
    d = decide_reframe(Reframe(), 1920, 1080, samples=samples)
    assert d.kind == "crop" and d.x_expr == str(crop_offset(480, 1920, 606))
    assert d.reason.startswith("face")


def test_decide_ignores_rare_face_hits() -> None:
    samples = [(i / 3, None, None) for i in range(9)] + [(3.0, 0.5, 0.5)]
    assert decide_reframe(Reframe(), 1920, 1080, samples=samples).kind == "fit"


def test_alternate_zoom_forces_crop_on_916() -> None:
    d = decide_reframe(Reframe(), 1080, 1920, zoom=1.12)
    assert d.kind == "crop" and d.crop_w < 1080


def test_filter_strings() -> None:
    d = decide_reframe(Reframe(mode=ReframeMode.CENTER), 1920, 1080)
    f = d.video_filter(540, 960, "0:v", "v")
    assert f.startswith("[0:v]setpts=PTS-STARTPTS,crop=w=606:h=1080:x='657'") and f.endswith("[v]")
    fit = decide_reframe(Reframe(mode=ReframeMode.FIT), 1920, 1080).video_filter(
        540, 960, "0:v", "v"
    )
    assert "gblur" in fit and fit.endswith("[v]")
