"""The edit plan: the contract between the AI layer (Gemini, Phase 2) and the renderer.

Design rules
------------
* **Source-anchored captions.** Captions come from the transcript, so their times are
  seconds *inside the source clip* (``timebase="source"`` + ``clip_id``). The renderer maps
  them through the cuts. The LLM never has to do timeline arithmetic. Captions generated
  in output time (e.g. voiceover captions from TTS word boundaries) use ``timebase="output"``.
* **Segment-anchored overlays.** On-screen text and the price card point at a segment id
  plus an offset, which an LLM can reason about reliably.
* **Gemini-friendly JSON Schema.** No free-form dicts, enums are plain strings, every
  object is closed (``extra="forbid"``) so hallucinated keys fail loudly.
* Shape validation lives here; checks against real clip durations and the template live in
  :mod:`adreel.plan_checks` (which can also repair).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"
MIN_SEGMENT_S = 0.3
MAX_SOURCE_S = 600.0  # 10 min per clip is far beyond what we accept upstream

Seconds = Annotated[float, Field(ge=0, le=MAX_SOURCE_S)]
Ident = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# --------------------------------------------------------------------------- enums


class SegmentRole(StrEnum):
    HOOK = "hook"
    PRODUCT = "product"
    SPEAKER = "speaker"
    BROLL = "broll"
    CTA = "cta"


class ReframeMode(StrEnum):
    AUTO = "auto"  # face crop if a face is found, else fit (landscape) / centre crop
    FACE = "face"  # track the largest face, fall back to focus point
    CENTER = "center"  # static crop around focus_x/focus_y
    FIT = "fit"  # whole frame, letterboxed on a blurred copy of itself


class Timebase(StrEnum):
    SOURCE = "source"
    OUTPUT = "output"


class TextStyle(StrEnum):
    HOOK = "hook"  # big, bold, top of frame, pop-in
    LABEL = "label"  # small pill
    EMPHASIS = "emphasis"  # medium, brand colour


class TextPosition(StrEnum):
    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class VoiceTier(StrEnum):
    STANDARD = "standard"
    PREMIUM = "premium"


# --------------------------------------------------------------------------- parts


class Reframe(_Strict):
    mode: ReframeMode = ReframeMode.AUTO
    focus_x: float = Field(0.5, ge=0, le=1, description="Horizontal point of interest, 0=left")
    focus_y: float = Field(0.5, ge=0, le=1, description="Vertical point of interest, 0=top")
    zoom: float = Field(1.0, ge=1.0, le=2.0, description="Punch-in factor on top of the crop")


class Segment(_Strict):
    id: Ident
    clip_id: Ident
    start: Seconds = Field(description="In-point in the source clip, seconds")
    end: Seconds = Field(description="Out-point in the source clip, seconds")
    role: SegmentRole
    reframe: Reframe = Reframe()
    keep_audio: bool = Field(True, description="False mutes the original sound of this segment")
    note: str | None = Field(None, max_length=200, description="Why this cut (ignored by render)")

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.end - self.start < MIN_SEGMENT_S:
            raise ValueError(
                f"segment {self.id}: end ({self.end}) must be at least {MIN_SEGMENT_S}s "
                f"after start ({self.start})"
            )
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class Word(_Strict):
    text: str = Field(min_length=1, max_length=40)
    start: Seconds
    end: Seconds

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.end <= self.start:
            raise ValueError(f"word {self.text!r}: end must be after start")
        return self


class CaptionLine(_Strict):
    id: int = Field(ge=1, le=999, description="Stable 1-based number shown to the user for fixes")
    text: str = Field(min_length=1, max_length=160)
    timebase: Timebase = Timebase.SOURCE
    clip_id: Ident | None = Field(None, description="Required when timebase=source")
    start: Seconds
    end: Seconds
    words: list[Word] = Field(
        default_factory=list,
        max_length=60,
        description="Optional word timings (same timebase). Enables cut-aware text and "
        "active-word highlighting.",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end <= self.start:
            raise ValueError(f"caption {self.id}: end must be after start")
        if self.timebase is Timebase.SOURCE and self.clip_id is None:
            raise ValueError(f"caption {self.id}: clip_id is required when timebase='source'")
        if self.timebase is Timebase.OUTPUT and self.clip_id is not None:
            raise ValueError(f"caption {self.id}: clip_id must be null when timebase='output'")
        tol = 0.05
        for w in self.words:
            if w.start < self.start - tol or w.end > self.end + tol:
                raise ValueError(f"caption {self.id}: word {w.text!r} lies outside the caption")
        return self


class OnScreenText(_Strict):
    text: str = Field(min_length=1, max_length=60)
    segment_id: Ident
    start_offset: float = Field(0.0, ge=0, le=60, description="Seconds after segment start")
    duration: float | None = Field(
        None, gt=0, le=30, description="Seconds; null = until the end of the segment"
    )
    style: TextStyle = TextStyle.EMPHASIS
    position: TextPosition = TextPosition.TOP


class TemplateSlots(_Strict):
    hook_text: str | None = Field(None, max_length=60)
    product_name: str | None = Field(None, max_length=40)
    price: float | None = Field(None, ge=0, le=10_000_000, description="In ETB")
    old_price: float | None = Field(None, ge=0, le=10_000_000, description="Shown struck through")
    price_note: str | None = Field(None, max_length=40, description="e.g. 'ለ3 ቀን ብቻ'")
    cta_text: str | None = Field(None, max_length=60)

    @model_validator(mode="after")
    def _check_prices(self) -> Self:
        if self.old_price is not None and self.price is None:
            raise ValueError("old_price given without price")
        return self


class PriceCard(_Strict):
    enabled: bool = True
    segment_id: Ident | None = Field(None, description="null = last 'product' segment")
    start_offset: float = Field(0.3, ge=0, le=30)
    duration: float = Field(3.0, ge=1.0, le=8.0)


class Voiceover(_Strict):
    script: str = Field(min_length=1, max_length=1500)
    voice: VoiceTier = VoiceTier.STANDARD
    start: float = Field(0.0, ge=0, le=60, description="Output time at which the VO starts")
    audio_path: str | None = Field(
        None, description="Filled in by the pipeline after TTS (or by hand for testing)"
    )


# --------------------------------------------------------------------------- root


class EditPlan(_Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    template_id: Ident
    language: Literal["am", "en"] = "am"
    title: str | None = Field(None, max_length=80)
    segments: list[Segment] = Field(min_length=1, max_length=40)
    captions: list[CaptionLine] = Field(default_factory=list, max_length=200)
    on_screen_text: list[OnScreenText] = Field(default_factory=list, max_length=20)
    slots: TemplateSlots = TemplateSlots()
    price_card: PriceCard = PriceCard(enabled=False)
    voiceover: Voiceover | None = None

    @model_validator(mode="after")
    def _check_refs(self) -> Self:
        seg_ids = [s.id for s in self.segments]
        dupes = {i for i in seg_ids if seg_ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate segment ids: {sorted(dupes)}")
        cap_ids = [c.id for c in self.captions]
        dupes_c = {i for i in cap_ids if cap_ids.count(i) > 1}
        if dupes_c:
            raise ValueError(f"duplicate caption ids: {sorted(dupes_c)}")
        known = set(seg_ids)
        for t in self.on_screen_text:
            if t.segment_id not in known:
                raise ValueError(f"on_screen_text {t.text!r} references unknown segment")
        if self.price_card.segment_id is not None and self.price_card.segment_id not in known:
            raise ValueError(f"price_card references unknown segment {self.price_card.segment_id}")
        return self

    # ---- helpers ---------------------------------------------------------

    @property
    def clip_ids(self) -> set[str]:
        ids = {s.clip_id for s in self.segments}
        ids.update(c.clip_id for c in self.captions if c.clip_id)
        return ids

    def segment(self, segment_id: str) -> Segment:
        for s in self.segments:
            if s.id == segment_id:
                return s
        raise KeyError(segment_id)

    def with_caption_text(self, caption_id: int, text: str) -> EditPlan:
        """Return a copy with one caption's text replaced (the "fix captions" flow).

        Word timings are dropped for that line because they no longer match the text; the
        line keeps its overall timing.
        """
        found = False
        new_caps: list[CaptionLine] = []
        for c in self.captions:
            if c.id == caption_id:
                found = True
                c = c.model_copy(update={"text": text.strip(), "words": []})
                CaptionLine.model_validate(c.model_dump())  # re-run field validation
            new_caps.append(c)
        if not found:
            raise KeyError(f"no caption with id {caption_id}")
        return self.model_copy(update={"captions": new_caps})


def edit_plan_json_schema() -> dict[str, object]:
    return EditPlan.model_json_schema(mode="validation")
