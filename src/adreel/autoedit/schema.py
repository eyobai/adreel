"""What the planner LLM returns, and the business context it gets.

The LLM does the *judgement* (which moments, what order, what to say on screen, voiceover
script, caption spelling fixes). Code does the *precision* work it is bad at: caption
timings come straight from the word-level transcript, and speaker cuts are tightened to
word boundaries (``autoedit.cuts``). ``autoedit.assemble`` turns this into an ``EditPlan``.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adreel.schemas.edit_plan import (
    Ident,
    OnScreenText,
    ReframeMode,
    Seconds,
    SegmentRole,
    TemplateSlots,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PlannedSegment(_Strict):
    id: Ident
    clip_id: Ident
    start: Seconds = Field(description="Seconds in the source clip")
    end: Seconds = Field(description="Seconds in the source clip")
    role: SegmentRole
    reframe_mode: ReframeMode = ReframeMode.AUTO
    focus_x: float = Field(0.5, ge=0, le=1, description="Horizontal subject position, 0=left")
    note: str | None = Field(None, max_length=200, description="Why this moment")

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.end <= self.start:
            raise ValueError(f"segment {self.id}: end must be after start")
        return self


class CaptionFix(_Strict):
    id: int = Field(ge=1, description="Transcript line id, e.g. 3 for [L3]")
    text: str = Field(min_length=1, max_length=160)


class PlannerOutput(_Strict):
    title: str | None = Field(None, max_length=80)
    segments: list[PlannedSegment] = Field(min_length=1, max_length=40)
    caption_fixes: list[CaptionFix] = Field(default_factory=list, max_length=200)
    on_screen_text: list[OnScreenText] = Field(default_factory=list, max_length=6)
    slots: TemplateSlots = TemplateSlots()
    price_card_segment_id: Ident | None = None
    voiceover_script: str | None = Field(None, max_length=1500)


class BusinessContext(BaseModel):
    """Answers from the bot's quick questions (Phase 3) or CLI flags."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_name: str | None = Field(None, max_length=40)
    price: float | None = Field(None, ge=0)
    old_price: float | None = Field(None, ge=0)
    highlight: str | None = Field(None, max_length=200, description="What to highlight")
    target_customer: str | None = Field(None, max_length=200)
    cta: str | None = Field(None, max_length=60)
    notes: str | None = Field(None, max_length=500)
