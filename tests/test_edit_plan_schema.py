from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from adreel.schemas.edit_plan import EditPlan, Timebase, edit_plan_json_schema
from tests.conftest import EXAMPLES, load_example


@pytest.mark.parametrize("name", ["plan_product_promo.json", "plan_talking_head.json"])
def test_examples_validate(name: str) -> None:
    plan = EditPlan.model_validate(load_example(name))
    assert plan.schema_version == "1.0"
    assert plan.segments


def test_defaults_are_filled(promo_plan: EditPlan) -> None:
    s1 = promo_plan.segment("s1")
    assert s1.reframe.mode.value == "auto" and s1.reframe.zoom == 1.0
    assert s1.keep_audio is True
    assert promo_plan.captions[0].timebase is Timebase.SOURCE


def _bad(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        EditPlan.model_validate(data)


def test_rejects_unknown_fields(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["segments"][0]["speed"] = 2
    _bad(d, "Extra inputs are not permitted")
    d = copy.deepcopy(promo_plan_data)
    d["music"] = "x"
    _bad(d, "Extra inputs are not permitted")


def test_segment_must_be_long_enough(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["segments"][0]["end"] = d["segments"][0]["start"] + 0.1
    _bad(d, "at least 0.3s")


def test_duplicate_ids(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["segments"][1]["id"] = "s1"
    _bad(d, "duplicate segment ids")
    d = copy.deepcopy(promo_plan_data)
    d["captions"][1]["id"] = 1
    _bad(d, "duplicate caption ids")


def test_dangling_references(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["price_card"]["segment_id"] = "nope"
    _bad(d, "price_card references unknown segment")
    d = copy.deepcopy(promo_plan_data)
    d["on_screen_text"][0]["segment_id"] = "nope"
    _bad(d, "references unknown segment")


def test_caption_timebase_rules(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["captions"][1]["clip_id"] = None
    _bad(d, "clip_id is required")
    d = copy.deepcopy(promo_plan_data)
    d["captions"][1]["timebase"] = "output"
    _bad(d, "clip_id must be null")


def test_caption_words_inside_line(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["captions"][0]["words"][0]["start"] = 0.1  # caption starts at 0.6
    _bad(d, "outside the caption")


def test_caption_end_after_start(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["captions"][1]["end"] = d["captions"][1]["start"]
    _bad(d, "end must be after start")


def test_old_price_requires_price(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["slots"]["price"] = None
    _bad(d, "old_price given without price")


def test_negative_and_absurd_values(promo_plan_data: dict[str, Any]) -> None:
    d = copy.deepcopy(promo_plan_data)
    d["slots"]["price"] = -5
    _bad(d, "greater than or equal to 0")
    d = copy.deepcopy(promo_plan_data)
    d["segments"][0]["reframe"] = {"zoom": 5}
    _bad(d, "less than or equal to 2")


def test_with_caption_text_replaces_and_drops_words(promo_plan: EditPlan) -> None:
    fixed = promo_plan.with_caption_text(1, "  ዛሬ አዲስ ቡና ደርሷል  ")
    c1 = next(c for c in fixed.captions if c.id == 1)
    assert c1.text == "ዛሬ አዲስ ቡና ደርሷል"
    assert c1.words == []
    assert (c1.start, c1.end) == (0.6, 3.3)
    # original untouched (frozen models)
    assert next(c for c in promo_plan.captions if c.id == 1).words


def test_with_caption_text_unknown_id(promo_plan: EditPlan) -> None:
    with pytest.raises(KeyError):
        promo_plan.with_caption_text(99, "x")


def test_with_caption_text_validates(promo_plan: EditPlan) -> None:
    with pytest.raises(ValidationError):
        promo_plan.with_caption_text(1, "x" * 500)


def test_roundtrip_json(promo_plan: EditPlan) -> None:
    again = EditPlan.model_validate_json(promo_plan.model_dump_json())
    assert again == promo_plan


def test_json_schema_is_llm_friendly() -> None:
    schema = edit_plan_json_schema()
    text = json.dumps(schema)
    # every object closed, no free-form dicts
    assert '"additionalProperties": true' not in text
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    for name, d in defs.items():
        if d.get("type") == "object":
            assert d.get("additionalProperties") is False, name
    assert set(schema["required"]) == {"template_id", "segments"}  # type: ignore[arg-type]


def test_schema_file_in_sync() -> None:
    """docs copy of the schema must match the code (regenerate with `adreel schema -o`)."""
    path = EXAMPLES.parent / "docs" / "edit_plan.schema.json"
    assert json.loads(path.read_text(encoding="utf-8")) == edit_plan_json_schema()
