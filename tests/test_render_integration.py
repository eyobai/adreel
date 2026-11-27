"""End-to-end renders through real ffmpeg/libass (small and fast)."""

from __future__ import annotations

from pathlib import Path

import pytest

from adreel.config import Settings, get_settings
from adreel.render.ffmpeg import FFmpeg
from adreel.render.pipeline import Quality, Renderer, RenderRequest
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan
from tests.conftest import requires_ffmpeg

pytestmark = [pytest.mark.ffmpeg, requires_ffmpeg]


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    ff = FFmpeg()
    clips = {"c1": d / "portrait.mp4", "c2": d / "landscape_silent.mp4"}
    ff.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=360x640:r=60:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=f=330:d=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(clips["c1"]),
        ]
    )
    ff.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=640x360:r=25:d=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(clips["c2"]),
        ]
    )
    ff.run(
        ["-f", "lavfi", "-i", "color=c=red:s=200x100:d=1", "-frames:v", "1", str(d / "logo.png")]
    )
    return {**clips, "logo": d / "logo.png"}


@pytest.fixture
def renderer(tmp_path: Path) -> Renderer:
    s = Settings(work_dir=tmp_path / "work", face_tracking=False)
    return Renderer(settings=s, face_tracker=None)


def _plan() -> EditPlan:
    return EditPlan.model_validate(
        {
            "template_id": "talking_head",
            "segments": [
                {"id": "a", "clip_id": "c1", "start": 0.0, "end": 1.5, "role": "speaker"},
                {"id": "b", "clip_id": "c1", "start": 2.0, "end": 3.5, "role": "speaker"},
                {"id": "r", "clip_id": "c2", "start": 0.5, "end": 2.5, "role": "broll"},
                {"id": "c", "clip_id": "c1", "start": 3.5, "end": 4.0, "role": "speaker"},
            ],
            "captions": [
                {"id": 1, "text": "ሰላም! ዋጋ 1,200 ብር", "clip_id": "c1", "start": 0.2, "end": 3.0}
            ],
            "slots": {"hook_text": "አዲስ!", "price": 1200},
            "price_card": {"enabled": True, "segment_id": "r", "duration": 1.5},
        }
    )


def test_render_preview_end_to_end(
    media: dict[str, Path], renderer: Renderer, tmp_path: Path
) -> None:
    brand = BrandKit(
        business_name="ሙከራ ሱቅ",
        logo_path=media["logo"],
        phone="0911234567",
        telegram_handle="@test_shop",
    )
    req = RenderRequest(
        plan=_plan(),
        brand=brand,
        clips={"c1": media["c1"], "c2": media["c2"]},
        output=tmp_path / "out.mp4",
        quality=Quality.PREVIEW,
        watermark=True,
    )
    stages: list[str] = []
    res = renderer.render(req, lambda stage, frac: stages.append(stage))
    assert (res.width, res.height) == (540, 960)
    # 45 + 45 + 60 + 15 frames at 30 fps + 2.5 s end card
    assert res.duration == pytest.approx(165 / 30 + 2.5, abs=0.05)
    info = FFmpeg().probe(res.output)
    assert info.has_audio and info.fps == pytest.approx(30)
    assert stages[-1] == "done" and "compose" in stages
    assert res.captions and res.warnings == []

    # Fix-captions flow: only the compose step re-runs.
    fixed = res.plan.with_caption_text(1, "ሰላም! ዋጋ 1,000 ብር")
    res2 = renderer.render(
        RenderRequest(
            plan=fixed,
            brand=brand,
            clips=req.clips,
            output=tmp_path / "out2.mp4",
            quality=Quality.PREVIEW,
        )
    )
    assert res2.timings["segments"] < 0.5
    assert res2.captions[0].text.endswith("1,000 ብር")


def test_ethiopic_glyphs_render_with_bundled_fonts(tmp_path: Path) -> None:
    """libass must find every glyph in the bundled fonts (no tofu / fallback warnings)."""
    from adreel.render.ass import AssDocument, AssEvent, AssStyle, font_runs

    doc = AssDocument(540, 200)
    doc.add_style(AssStyle("A", 48))
    doc.add(AssEvent(0, 1, "A", "{\\an5\\pos(270,100)}" + font_runs("ዋጋ 1,200 ብር! ይዘዙ፣ @shop")))
    (tmp_path / "t.ass").write_text(doc.render(), encoding="utf-8")
    (tmp_path / "fonts").symlink_to(get_settings().fonts_dir.resolve())
    stderr = FFmpeg().run(
        [
            "-loglevel",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=540x200:d=1",
            "-vf",
            "ass=filename=t.ass:fontsdir=fonts",
            "-frames:v",
            "1",
            "frame.png",
        ],
        capture_stderr=True,
        cwd=tmp_path,
    )
    assert "failed to find any fallback" not in stderr
    assert "Glyph 0x" not in stderr
    assert (tmp_path / "frame.png").stat().st_size > 2000  # something was drawn
