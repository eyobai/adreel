"""PlannerOutput + transcripts + context -> EditPlan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from adreel.ai.base import TranscriptWord
from adreel.autoedit.cuts import refine_segments
from adreel.autoedit.schema import BusinessContext, PlannerOutput
from adreel.autoedit.transcript_lines import TranscriptLine, to_caption_lines
from adreel.schemas.edit_plan import (
    MIN_SEGMENT_S,
    CaptionLine,
    EditPlan,
    PriceCard,
    Reframe,
    Voiceover,
)
from adreel.schemas.template import TemplateSpec


def merge_slots(out: PlannerOutput, context: BusinessContext) -> dict[str, object]:
    """Facts come from the business, never from the LLM: price, old price, product name."""
    slots = out.slots.model_dump()
    if context.product_name:
        slots["product_name"] = context.product_name
    slots["price"] = context.price
    slots["old_price"] = context.old_price if context.price is not None else None
    if context.cta and not slots.get("cta_text"):
        slots["cta_text"] = context.cta
    if not slots.get("hook_text") and context.highlight:
        slots["hook_text"] = context.highlight[:60]
    return slots


def assemble_plan(
    out: PlannerOutput,
    *,
    template: TemplateSpec,
    language: str,
    context: BusinessContext,
    lines: Sequence[TranscriptLine],
    words_by_clip: Mapping[str, Sequence[TranscriptWord]],
    clip_durations: Mapping[str, float],
    voiceover: Voiceover | None = None,
    voiceover_captions: Sequence[CaptionLine] | None = None,
) -> EditPlan:
    planned = [s for s in out.segments if s.clip_id in clip_durations]
    refined = refine_segments(planned, words_by_clip, clip_durations)
    segments, seen = [], set()
    for s in refined:
        start, end = s.start, min(s.end, clip_durations[s.clip_id])
        if end - start < MIN_SEGMENT_S:
            continue
        seg_id = s.id
        n = 2
        while seg_id in seen:
            seg_id = f"{s.id}_{n}"[:32]
            n += 1
        seen.add(seg_id)
        segments.append(
            {
                "id": seg_id,
                "clip_id": s.clip_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "role": s.role.value,
                "note": s.note,
                "reframe": Reframe(mode=s.reframe_mode, focus_x=s.focus_x).model_dump(mode="json"),
            }
        )

    fixes = {f.id: f.text for f in out.caption_fixes}
    if voiceover_captions is not None:
        captions = list(voiceover_captions)  # the VO is what viewers hear: caption it
    else:
        captions = [c for c in to_caption_lines(lines, fixes) if c.clip_id in clip_durations]

    slots = merge_slots(out, context)
    price_ok = template.price_card.enabled and slots.get("price") is not None
    pc_seg = out.price_card_segment_id if out.price_card_segment_id in seen else None
    return EditPlan.model_validate(
        {
            "template_id": template.id,
            "language": language,
            "title": out.title,
            "segments": segments,
            "captions": [c.model_dump(mode="json") for c in captions],
            "on_screen_text": [
                t.model_dump(mode="json") for t in out.on_screen_text if t.segment_id in seen
            ],
            "slots": slots,
            "price_card": PriceCard(enabled=price_ok, segment_id=pc_seg).model_dump(mode="json"),
            "voiceover": voiceover.model_dump(mode="json") if voiceover else None,
            "look": out.look,
        }
    )
