from __future__ import annotations

import pytest
from pydantic import ValidationError

from adreel.plan_checks import PlanError, check_plan, planned_duration, repair_plan
from adreel.schemas.brand_kit import BrandKit, display_et_phone, normalize_et_phone
from adreel.schemas.edit_plan import EditPlan
from adreel.schemas.template import TemplateSpec
from tests.conftest import make_plan

DUR = {"c1": 10.0, "c2": 8.0, "c3": 12.0}


def codes(issues):  # type: ignore[no-untyped-def]
    return {i.code for i in issues}


def test_example_plan_is_clean(promo_plan: EditPlan, promo_tpl: TemplateSpec) -> None:
    assert check_plan(promo_plan, promo_tpl, DUR) == []
    assert planned_duration(promo_plan, promo_tpl) == pytest.approx(22.5)


def test_detects_reality_problems(promo_plan: EditPlan, promo_tpl: TemplateSpec) -> None:
    issues = check_plan(promo_plan, promo_tpl, {"c1": 10.0, "c2": 3.0})
    assert {"unknown_clip", "segment_clamped", "caption_unknown_clip"} <= codes(issues)
    issues = check_plan(promo_plan, promo_tpl, {"c1": 10.0, "c2": 8.0, "c3": 0.9})
    assert "segment_out_of_range" in codes(issues)


def test_missing_slot_is_error(promo_plan: EditPlan, promo_tpl: TemplateSpec) -> None:
    plan = promo_plan.model_copy(
        update={"slots": promo_plan.slots.model_copy(update={"product_name": None})}
    )
    errs = [i for i in check_plan(plan, promo_tpl, DUR) if i.severity == "error"]
    assert codes(errs) == {"missing_slot"}


def test_template_mismatch(promo_plan: EditPlan, talk_tpl: TemplateSpec) -> None:
    assert "template_mismatch" in codes(check_plan(promo_plan, talk_tpl, DUR))


def test_repair_clamps_and_drops(promo_plan: EditPlan, promo_tpl: TemplateSpec) -> None:
    dur = {"c1": 10.0, "c2": 3.0, "c3": 6.0}  # s5 (c3 7.0–10.5) is now outside its clip
    fixed, fixes = repair_plan(promo_plan, promo_tpl, dur)
    assert [s.id for s in fixed.segments] == ["s1", "s2", "s3", "s4"]
    assert fixed.segment("s2").end == 3.0
    assert any("dropped segment s5" in f for f in fixes)
    assert 5 not in {c.id for c in fixed.captions}  # caption 5 started at c3 8.0 > 6.0
    assert not [i for i in check_plan(fixed, promo_tpl, dur) if i.severity == "error"]


def test_repair_trims_to_template_max(talk_tpl: TemplateSpec) -> None:
    plan = make_plan([("a", "c1", 0, 40), ("b", "c1", 40, 70)])  # 70 s + end card > 60 s
    fixed, fixes = repair_plan(plan, talk_tpl, {"c1": 100.0})
    assert planned_duration(fixed, talk_tpl) == pytest.approx(talk_tpl.duration.max_s)
    assert fixes and "shortened" in fixes[0]


def test_repair_retargets_dangling_price_card(
    promo_plan: EditPlan, promo_tpl: TemplateSpec
) -> None:
    fixed, _ = repair_plan(promo_plan, promo_tpl, {"c1": 3.6, "c2": 8.0, "c3": 12.0})
    assert "s4" not in {s.id for s in fixed.segments}
    assert fixed.price_card.segment_id is None


def test_repair_with_nothing_usable(promo_plan: EditPlan, promo_tpl: TemplateSpec) -> None:
    with pytest.raises(PlanError):
        repair_plan(promo_plan, promo_tpl, {})


# ------------------------------------------------------------------ brand kit


@pytest.mark.parametrize("raw", ["0911234567", "+251911234567", "251 911 234 567", "911-234-567"])
def test_phone_normalisation(raw: str) -> None:
    assert normalize_et_phone(raw) == "+251911234567"


def test_safaricom_and_display() -> None:
    assert display_et_phone(normalize_et_phone("0712345678")) == "0712 345 678"


@pytest.mark.parametrize("bad", ["12345", "0811234567", "+1 555 1234567"])
def test_phone_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_et_phone(bad)


def test_brand_kit_normalises() -> None:
    b = BrandKit(
        business_name="X",
        telegram_handle="https://t.me/my_shop",
        primary_color="#ffcc00",
        phone="0911 234 567",
    )
    assert b.telegram_handle == "@my_shop"
    assert b.primary_color == "#FFCC00"
    assert b.on_primary == "#111111"  # light brand -> dark text
    assert b.phone_display == "0911 234 567"
    assert BrandKit(business_name="X", primary_color="#7B3F00").on_primary == "#FFFFFF"


def test_brand_kit_rejects_bad_handle_and_color() -> None:
    with pytest.raises(ValidationError):
        BrandKit(business_name="X", telegram_handle="@ab")
    with pytest.raises(ValidationError):
        BrandKit(business_name="X", primary_color="red")
