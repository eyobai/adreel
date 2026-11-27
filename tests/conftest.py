from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan
from adreel.schemas.template import TemplateSpec
from adreel.templates_registry import TemplateRegistry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


@pytest.fixture(scope="session")
def registry() -> TemplateRegistry:
    return TemplateRegistry()


@pytest.fixture(scope="session")
def promo_tpl(registry: TemplateRegistry) -> TemplateSpec:
    return registry.get("product_promo")


@pytest.fixture(scope="session")
def talk_tpl(registry: TemplateRegistry) -> TemplateSpec:
    return registry.get("talking_head")


@pytest.fixture
def brand() -> BrandKit:
    return BrandKit(
        business_name="አበበ ቡና ቤት",
        primary_color="#7B3F00",
        phone="0911234567",
        telegram_handle="abebe_coffee",
    )


def load_example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return data


@pytest.fixture
def promo_plan_data() -> dict[str, Any]:
    return load_example("plan_product_promo.json")


@pytest.fixture
def promo_plan(promo_plan_data: dict[str, Any]) -> EditPlan:
    return EditPlan.model_validate(promo_plan_data)


def make_plan(
    segments: list[tuple[str, str, float, float]],
    captions: list[dict[str, Any]] | None = None,
    template_id: str = "talking_head",
    **extra: Any,
) -> EditPlan:
    """segments: (id, clip_id, start, end) all role=speaker."""
    return EditPlan.model_validate(
        {
            "template_id": template_id,
            "segments": [
                {"id": i, "clip_id": c, "start": s, "end": e, "role": "speaker"}
                for i, c, s, e in segments
            ],
            "captions": captions or [],
            **extra,
        }
    )
