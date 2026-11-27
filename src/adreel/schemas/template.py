"""Schema for declarative video templates (``templates/*.yaml``).

A template owns *layout and pacing*; the edit plan owns *content*. Colours can reference the
brand kit by token so one template works for every business:
``primary`` | ``on_primary`` | ``accent`` | ``white`` | ``black`` | ``#RRGGBB``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adreel.schemas.edit_plan import SegmentRole, TextStyle

ColorRef = Annotated[
    str, Field(pattern=r"^(primary|on_primary|accent|white|black|#[0-9A-Fa-f]{6})$")
]
Fraction = Annotated[float, Field(ge=0, le=1)]
SlotName = Literal["hook_text", "product_name", "price", "old_price", "price_note", "cta_text"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class L10n(_Spec):
    am: str
    en: str

    def get(self, lang: str) -> str:
        return self.am if lang == "am" else self.en


class CanvasSpec(_Spec):
    width: int = Field(1080, ge=360, le=2160, multiple_of=2)
    height: int = Field(1920, ge=640, le=3840, multiple_of=2)
    fps: int = Field(30, ge=24, le=60)


class DurationSpec(_Spec):
    min_s: float = Field(ge=3, le=180)
    max_s: float = Field(ge=3, le=180)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.max_s < self.min_s:
            raise ValueError("duration.max_s must be >= min_s")
        return self


class StructureStep(_Spec):
    role: SegmentRole
    required: bool = True
    min_s: float = Field(0.5, ge=0)
    max_s: float = Field(30.0, ge=0)
    repeat: bool = False


class CaptionMode(StrEnum):
    OUTLINE = "outline"
    BOX = "box"


class CaptionSpec(_Spec):
    enabled: bool = True
    font_size: int = Field(88, ge=20, le=200)
    bold: bool = True
    mode: CaptionMode = CaptionMode.OUTLINE
    text_color: ColorRef = "white"
    outline_color: ColorRef = "black"
    box_color: ColorRef = "black"
    box_opacity: Fraction = 0.6
    outline_px: float = Field(5, ge=0, le=20)
    shadow_px: float = Field(2, ge=0, le=20)
    y: Fraction = Field(0.78, description="Bottom edge of the caption block (fraction of height)")
    side_margin_px: int = Field(80, ge=0)
    # Line width is measured in "Ethiopic character units" (Latin ≈ 0.6 units).
    max_units_per_line: float = Field(16, ge=4, le=60)
    max_lines: int = Field(2, ge=1, le=3)
    highlight_active_word: bool = True
    highlight_color: ColorRef = "primary"
    min_duration_s: float = Field(0.7, ge=0.1, le=5)
    min_visible_s: float = Field(0.25, ge=0.05, le=2)
    gap_s: float = Field(0.04, ge=0, le=0.5)
    merge_gap_s: float = Field(0.12, ge=0, le=1, description="Join caption pieces split by a cut")


class TextStyleSpec(_Spec):
    font_size: int = Field(ge=16, le=240)
    bold: bool = True
    color: ColorRef = "white"
    outline_color: ColorRef = "black"
    outline_px: float = Field(4, ge=0, le=20)
    shadow_px: float = Field(0, ge=0, le=20)
    box: bool = False
    box_color: ColorRef = "primary"
    pop_in: bool = False


class TextLayoutSpec(_Spec):
    top_y: Fraction = 0.16
    center_y: Fraction = 0.45
    bottom_y: Fraction = 0.62
    max_units_per_line: float = Field(12, ge=4, le=60)
    styles: dict[TextStyle, TextStyleSpec]

    @model_validator(mode="after")
    def _all_styles(self) -> Self:
        missing = set(TextStyle) - set(self.styles)
        if missing:
            raise ValueError(f"text.styles missing: {sorted(m.value for m in missing)}")
        return self


class Corner(StrEnum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class LogoSpec(_Spec):
    enabled: bool = True
    corner: Corner = Corner.TOP_RIGHT
    width_frac: float = Field(0.2, gt=0, le=0.6)
    margin_px: int = Field(48, ge=0)
    opacity: Fraction = 0.95


class PriceCardSpec(_Spec):
    enabled: bool = True
    y: Fraction = Field(0.6, description="Vertical centre of the card")
    width_frac: float = Field(0.8, gt=0.3, le=1)
    radius_px: int = Field(36, ge=0, le=200)
    background: ColorRef = "primary"
    text_color: ColorRef = "on_primary"
    name_size: int = 66
    price_size: int = 132
    old_price_size: int = 60
    note_size: int = 54
    price_label: L10n = L10n(am="ዋጋ", en="Price")
    currency: L10n = L10n(am="ብር", en="ETB")
    slide_in: bool = True


class EndCardSpec(_Spec):
    enabled: bool = True
    duration_s: float = Field(3.0, ge=1, le=8)
    background: ColorRef = "primary"
    text_color: ColorRef = "on_primary"
    logo_width_frac: float = Field(0.42, gt=0, le=0.8)
    logo_center_y: Fraction = 0.3
    title_size: int = 116
    cta_size: int = 86
    info_size: int = 66
    default_cta: L10n = L10n(am="አሁኑኑ ይዘዙ!", en="Order now!")
    phone_label: L10n = L10n(am="ስልክ", en="Call")
    telegram_label: L10n = L10n(am="ቴሌግራም", en="Telegram")


class WatermarkSpec(_Spec):
    font_size: int = 46
    opacity: Fraction = 0.6
    y: Fraction = 0.93


class AudioSpec(_Spec):
    target_lufs: float = Field(-14.0, ge=-30, le=-8)
    true_peak_db: float = Field(-1.5, ge=-9, le=0)
    duck_threshold: float = Field(0.03, gt=0, le=1)
    duck_ratio: float = Field(8.0, ge=1, le=20)
    voiceover_gain_db: float = Field(0.0, ge=-12, le=12)


class ReframeSpec(_Spec):
    # Alternating punch-in on consecutive cuts from the same clip hides jump cuts.
    alternate_zoom: float = Field(1.0, ge=1.0, le=1.5)


class TemplateSpec(_Spec):
    id: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    version: int = Field(1, ge=1)
    name: L10n
    description: str = ""
    canvas: CanvasSpec = CanvasSpec()
    duration: DurationSpec
    structure: list[StructureStep] = Field(min_length=1)
    required_slots: list[SlotName] = Field(default_factory=list)
    captions: CaptionSpec = CaptionSpec()
    text: TextLayoutSpec
    logo: LogoSpec = LogoSpec()
    price_card: PriceCardSpec = PriceCardSpec()
    end_card: EndCardSpec = EndCardSpec()
    watermark: WatermarkSpec = WatermarkSpec()
    audio: AudioSpec = AudioSpec()
    reframe: ReframeSpec = ReframeSpec()
    ai_guidance: str = Field("", description="Extra instructions for the planner LLM (Phase 2)")
