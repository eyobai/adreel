"""Semantic checks of an edit plan against real clip durations and the template.

Pydantic guarantees *shape*. This module checks *reality* (does clip c2 have a 14 s mark?
does the video fit the template's length?) and can repair the common LLM slips
deterministically. In Phase 2 the errors that remain after ``repair_plan`` are fed back to
Gemini for a retry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from adreel.schemas.edit_plan import MIN_SEGMENT_S, EditPlan, SegmentRole
from adreel.schemas.template import TemplateSpec

TOLERANCE_S = 0.05
TRIMMABLE = (SegmentRole.PRODUCT, SegmentRole.BROLL, SegmentRole.SPEAKER, SegmentRole.CTA)


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


class PlanError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues))


def planned_duration(plan: EditPlan, tpl: TemplateSpec) -> float:
    end_card = tpl.end_card.duration_s if tpl.end_card.enabled else 0.0
    return sum(s.duration for s in plan.segments) + end_card


def check_plan(
    plan: EditPlan, tpl: TemplateSpec, clip_durations: Mapping[str, float]
) -> list[Issue]:
    issues: list[Issue] = []

    def err(code: str, msg: str) -> None:
        issues.append(Issue("error", code, msg))

    def warn(code: str, msg: str) -> None:
        issues.append(Issue("warning", code, msg))

    if plan.template_id != tpl.id:
        err("template_mismatch", f"plan is for {plan.template_id!r}, template is {tpl.id!r}")

    for s in plan.segments:
        dur = clip_durations.get(s.clip_id)
        if dur is None:
            err("unknown_clip", f"segment {s.id} uses unknown clip {s.clip_id!r}")
        elif s.start > dur - MIN_SEGMENT_S:
            err(
                "segment_out_of_range",
                f"segment {s.id} starts at {s.start}s but {s.clip_id} is {dur:.2f}s",
            )
        elif s.end > dur + TOLERANCE_S:
            warn(
                "segment_clamped",
                f"segment {s.id} ends at {s.end}s, beyond {s.clip_id} ({dur:.2f}s)",
            )

    for c in plan.captions:
        if c.clip_id is None:
            continue
        dur = clip_durations.get(c.clip_id)
        if dur is None:
            warn("caption_unknown_clip", f"caption {c.id} uses unknown clip {c.clip_id!r}")
        elif c.start >= dur:
            warn("caption_out_of_range", f"caption {c.id} starts after the end of {c.clip_id}")

    total = planned_duration(plan, tpl)
    if total > tpl.duration.max_s + 0.5:
        warn("too_long", f"video is {total:.1f}s, template max is {tpl.duration.max_s:.0f}s")
    if total < tpl.duration.min_s - 0.5:
        warn("too_short", f"video is {total:.1f}s, template min is {tpl.duration.min_s:.0f}s")

    roles = {s.role for s in plan.segments}
    for step in tpl.structure:
        if step.required and step.role not in roles:
            warn("missing_role", f"template expects a {step.role.value!r} segment")

    for slot in tpl.required_slots:
        if getattr(plan.slots, slot) in (None, ""):
            err("missing_slot", f"template requires slots.{slot}")
    return issues


def repair_plan(
    plan: EditPlan, tpl: TemplateSpec, clip_durations: Mapping[str, float]
) -> tuple[EditPlan, list[str]]:
    """Fix what can be fixed without judgement. Returns (plan, human-readable fixes)."""
    fixes: list[str] = []
    data: dict[str, Any] = plan.model_dump(mode="json")

    segs = []
    for s in data["segments"]:
        dur = clip_durations.get(s["clip_id"])
        if dur is None or s["start"] > dur - MIN_SEGMENT_S:
            fixes.append(f"dropped segment {s['id']} (outside clip)")
            continue
        if s["end"] > dur:
            fixes.append(f"clamped segment {s['id']} end {s['end']} -> {dur:.3f}")
            s["end"] = round(dur, 3)
        segs.append(s)
    if not segs:
        raise PlanError([Issue("error", "no_segments", "no usable segments after repair")])

    # Trim to the template's max length: shorten the longest trimmable segment first.
    end_card = tpl.end_card.duration_s if tpl.end_card.enabled else 0.0
    min_keep = 1.0
    excess = sum(s["end"] - s["start"] for s in segs) + end_card - tpl.duration.max_s
    while excess > 0.01:
        cands = [
            s for s in segs if s["role"] in TRIMMABLE and s["end"] - s["start"] > min_keep + 0.01
        ]
        if not cands:
            break
        s = max(cands, key=lambda x: x["end"] - x["start"])
        cut = min(excess, s["end"] - s["start"] - min_keep)
        s["end"] = round(s["end"] - cut, 3)
        excess -= cut
        fixes.append(f"shortened segment {s['id']} by {cut:.2f}s to fit {tpl.duration.max_s:.0f}s")
    while excess > 0.01 and len(segs) > 1:
        idx = max(
            i
            for i, s in enumerate(segs)
            if s["role"] != SegmentRole.HOOK.value or i == len(segs) - 1
        )
        dropped = segs.pop(idx)
        excess -= dropped["end"] - dropped["start"]
        fixes.append(f"dropped segment {dropped['id']} to fit template length")
    data["segments"] = segs

    kept = {s["id"] for s in segs}
    caps = []
    for c in data["captions"]:
        if c["clip_id"] is not None:
            dur = clip_durations.get(c["clip_id"])
            if dur is None or c["start"] >= dur:
                fixes.append(f"dropped caption {c['id']} (outside clip)")
                continue
            if c["end"] > dur:
                c["end"] = round(dur, 3)
                c["words"] = [w for w in c["words"] if w["start"] < dur]
                for w in c["words"]:
                    w["end"] = min(w["end"], c["end"])
                c["words"] = [w for w in c["words"] if w["end"] > w["start"]]
        caps.append(c)
    data["captions"] = caps

    before = len(data["on_screen_text"])
    data["on_screen_text"] = [t for t in data["on_screen_text"] if t["segment_id"] in kept]
    if len(data["on_screen_text"]) != before:
        fixes.append("dropped on-screen text attached to removed segments")
    if data["price_card"]["segment_id"] not in (None, *kept):
        data["price_card"]["segment_id"] = None
        fixes.append("price card moved to the default segment")

    return EditPlan.model_validate(data), fixes
