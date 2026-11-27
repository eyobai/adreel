from __future__ import annotations

import pytest

from adreel.render.ass import (
    AssDocument,
    AssEvent,
    AssStyle,
    ass_time,
    escape,
    font_runs,
    rounded_rect,
    style_color,
    tag_color,
)
from adreel.render.overlays import build_overlays, format_number, readable_highlight
from adreel.render.timeline import build_timeline
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan
from adreel.schemas.template import TemplateSpec


def test_ass_time() -> None:
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.234) == "0:01:01.23"
    assert ass_time(3600 + 0.999) == "1:00:01.00"
    assert ass_time(-1) == "0:00:00.00"


def test_colors() -> None:
    assert style_color("#112233") == "&H00332211"
    assert style_color("#112233", opacity=0) == "&HFF332211"
    assert tag_color("#7B3F00") == "&H003F7B&"


def test_escape_neutralises_override_blocks_and_emoji() -> None:
    assert escape("a{\\b1}c\nd") == "a(/b1)c d"
    assert escape("ቡና ☕️👍") == "ቡና "


def test_font_runs_switch_on_script_change() -> None:
    out = font_runs("ዋጋ 1,200 ብር!")
    assert out == (
        "{\\fnNoto Sans Ethiopic}ዋጋ {\\fnNoto Sans}1,200 {\\fnNoto Sans Ethiopic}ብር{\\fnNoto Sans}!"
    )
    assert font_runs("Hello") == "{\\fnNoto Sans}Hello"


def test_rounded_rect_is_closed_path() -> None:
    p = rounded_rect(100, 50, 10)
    assert p.startswith("m 10 0") and p.endswith("10 0")
    assert rounded_rect(10, 10, 100).count("b ") == 4  # radius clamped


def test_document_rejects_unknown_style_and_skips_empty() -> None:
    doc = AssDocument(1080, 1920)
    doc.add_style(AssStyle("A", 40))
    with pytest.raises(KeyError):
        doc.add(AssEvent(0, 1, "B", "x"))
    doc.add(AssEvent(1, 1, "A", "empty"))
    doc.add(AssEvent(0, 1, "A", "ok"))
    text = doc.render()
    assert "PlayResX: 1080" in text and "WrapStyle: 2" in text
    assert text.count("Dialogue:") == 1


def test_readable_highlight() -> None:
    assert readable_highlight("#7B3F00") == "#FFD400"  # dark brown on black outline
    assert readable_highlight("#FF8800") == "#FF8800"


def test_format_number() -> None:
    assert format_number(1200) == "1,200"
    assert format_number(99.5) == "99.50"


def test_build_overlays_full(
    promo_plan: EditPlan, brand: BrandKit, promo_tpl: TemplateSpec
) -> None:
    tl = build_timeline(promo_plan, promo_tpl.canvas.fps, promo_tpl.end_card.duration_s)
    res = build_overlays(promo_plan, brand, promo_tpl, tl, has_logo=True, watermark_text="WM")
    text = res.doc.render()
    assert res.warnings == []
    assert "1,200 " in text and "ብር" in text and "\\s1" in text  # price + struck old price
    assert "0911 234 567" in text and "@abebe_coffee" in text  # end card
    assert "WM" in text
    assert "\\c&H00D4FF&" in text  # active-word highlight (yellow fallback)
    assert [lp.anchor for lp in res.logos] == ["top_right", "center"]
    main = tl.main_duration
    assert res.logos[0].end == pytest.approx(main) and res.logos[1].start == pytest.approx(main)
    assert all(c.end <= main + 1e-6 for c in res.captions)


def test_price_card_skipped_without_price(
    promo_plan: EditPlan, brand: BrandKit, promo_tpl: TemplateSpec
) -> None:
    plan = promo_plan.model_copy(
        update={"slots": promo_plan.slots.model_copy(update={"price": None, "old_price": None})}
    )
    tl = build_timeline(plan, 30, 3)
    res = build_overlays(plan, brand, promo_tpl, tl, has_logo=False)
    assert any("price" in w for w in res.warnings)
    assert res.logos == []


def test_logo_overlay_expressions_scale() -> None:
    from adreel.render.overlays import LogoPlacement

    lp = LogoPlacement(200, 200, "top_right", 48, 0, 0, 5, 1.0)
    assert lp.overlay_xy(0.5) == ("W-w-24", "24")
    lp = LogoPlacement(400, 400, "center", 0, 576, 5, 8, 1.0)
    assert lp.overlay_xy(0.5) == ("(W-w)/2", "288-h/2")
