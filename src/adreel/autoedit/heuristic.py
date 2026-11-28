"""Deterministic fallback planner (no LLM).

Used when ``ADREEL_PLANNER=heuristic``, when the LLM fails all attempts, and in tests. It
produces a reasonable, never-broken edit so the user always gets a video.
"""

from __future__ import annotations

from collections.abc import Sequence

from adreel.autoedit.brief import first_phrase
from adreel.autoedit.prompt import ClipAnalysis
from adreel.autoedit.schema import BusinessContext, PlannedSegment, PlannerOutput
from adreel.render.overlays import format_number
from adreel.schemas.edit_plan import SegmentRole, TemplateSlots
from adreel.schemas.template import TemplateSpec


def _speech_bounds(c: ClipAnalysis) -> tuple[float, float] | None:
    if not c.transcript or not c.transcript.spoken:
        return None
    w = c.transcript.spoken
    return max(0.0, w[0].start - 0.1), min(c.duration, w[-1].end + 0.1)


def heuristic_plan(
    template: TemplateSpec,
    context: BusinessContext,
    clips: Sequence[ClipAnalysis],
    *,
    want_voiceover: bool,
    language: str,
    look: str = "natural",
) -> PlannerOutput:
    roles = {st.role for st in template.structure}
    if SegmentRole.SPEAKER in roles:
        segs = _talking(clips)
    else:
        end_card = template.end_card.duration_s if template.end_card.enabled else 0.0
        target = (template.duration.min_s + template.duration.max_s) / 2 - end_card
        segs = _promo(clips, target)
    products = [s for s in segs if s.role is SegmentRole.PRODUCT]
    slots = TemplateSlots(
        hook_text=(context.highlight or first_phrase(context.brief) or context.product_name or "")[
            :60
        ]
        or None,
        product_name=context.product_name,
        price=context.price,
        old_price=context.old_price if context.price is not None else None,
        cta_text=context.cta,
    )
    return PlannerOutput(
        title=context.product_name,
        segments=segs,
        slots=slots,
        price_card_segment_id=(products[-1].id if products else None),
        voiceover_script=default_voiceover(context, language) if want_voiceover else None,
        look=look if look in ("natural", "vivid", "warm", "cool") else "natural",
    )


def _talking(clips: Sequence[ClipAnalysis]) -> list[PlannedSegment]:
    segs: list[PlannedSegment] = []
    broll: list[PlannedSegment] = []
    for c in clips:
        b = _speech_bounds(c)
        if b and b[1] - b[0] >= 0.5:
            segs.append(
                PlannedSegment(
                    id=f"s{len(segs) + 1}",
                    clip_id=c.clip_id,
                    start=round(b[0], 3),
                    end=round(b[1], 3),
                    role=SegmentRole.SPEAKER,
                )
            )
        elif c.duration >= 1.0:
            mid = c.duration / 2
            half = min(1.5, c.duration / 2 - 0.1)
            broll.append(
                PlannedSegment(
                    id=f"b{len(broll) + 1}",
                    clip_id=c.clip_id,
                    start=round(mid - half, 3),
                    end=round(mid + half, 3),
                    role=SegmentRole.BROLL,
                )
            )
    if not segs:  # nobody talks: treat everything as b-roll
        return broll or [_whole(clips[0], SegmentRole.BROLL)]
    # one b-roll cutaway after the first speaker segment, the rest at the end
    return segs[:1] + broll[:1] + segs[1:] + broll[1:]


def _promo(clips: Sequence[ClipAnalysis], target: float) -> list[PlannedSegment]:
    first = clips[0]
    hook_len = min(3.0, max(0.5, first.duration - 0.4))
    segs = [
        PlannedSegment(
            id="s1",
            clip_id=first.clip_id,
            start=0.2,
            end=round(0.2 + hook_len, 3),
            role=SegmentRole.HOOK,
        )
    ]
    cursors = {c.clip_id: (0.2 + hook_len if c is first else 0.2) for c in clips}
    total = hook_len
    chunk = 3.5
    progressed = True
    while total < target - 0.5 and progressed:
        progressed = False
        for c in clips:
            start = cursors[c.clip_id]
            length = min(chunk, c.duration - 0.2 - start, target - total)
            if length < 1.2:
                continue
            segs.append(
                PlannedSegment(
                    id=f"s{len(segs) + 1}",
                    clip_id=c.clip_id,
                    start=round(start, 3),
                    end=round(start + length, 3),
                    role=SegmentRole.PRODUCT,
                )
            )
            cursors[c.clip_id] = start + length + 0.3
            total += length
            progressed = True
            if total >= target - 0.5:
                break
    return segs


def _whole(c: ClipAnalysis, role: SegmentRole) -> PlannedSegment:
    return PlannedSegment(
        id="s1", clip_id=c.clip_id, start=0.0, end=round(c.duration, 3), role=role
    )


def default_voiceover(context: BusinessContext, language: str) -> str:
    parts: list[str] = []
    if language == "am":
        if context.product_name:
            parts.append(f"{context.product_name}!")
        if context.highlight:
            parts.append(f"{context.highlight}።")
        if context.price is not None:
            parts.append(f"ዋጋው {format_number(context.price)} ብር ብቻ።")
        parts.append(context.cta or "አሁኑኑ ይዘዙ!")
    else:
        if context.product_name:
            parts.append(f"{context.product_name}!")
        if context.highlight:
            parts.append(f"{context.highlight}.")
        if context.price is not None:
            parts.append(f"Only {format_number(context.price)} birr.")
        parts.append(context.cta or "Order now!")
    return " ".join(parts)
