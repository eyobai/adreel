"""Smart cuts: make the planner's segments precise using word timings.

* Speaker segments are tightened: pauses longer than ``max_gap`` and filler words are cut
  out (each becomes a split), and edges snap to the first/last word plus a little padding.
* Other segments that keep their audio are snapped so they never start or end mid-word.

The LLM chooses *which* moments to keep; this module makes the cuts frame-tight without
trusting the LLM's timestamp accuracy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from adreel.ai.base import TranscriptWord
from adreel.autoedit.schema import PlannedSegment
from adreel.schemas.edit_plan import MIN_SEGMENT_S, SegmentRole

# Amharic hesitation sounds as ASR tends to write them (after stripping punctuation).
FILLERS = frozenset(
    {"እ", "እእ", "እህ", "እም", "ኧ", "ኧኧ", "ኤ", "ኣ", "ም", "ህም", "eh", "uh", "um", "erm"}
)
_STRIP = re.compile(r"[\s.…,፣።!?፧-]+")


def is_filler(text: str) -> bool:
    return _STRIP.sub("", text).lower() in FILLERS


def _words_in(seg: PlannedSegment, words: Sequence[TranscriptWord]) -> list[TranscriptWord]:
    return [w for w in words if seg.start <= (w.start + w.end) / 2 <= seg.end]


def refine_segments(
    segments: Sequence[PlannedSegment],
    words_by_clip: Mapping[str, Sequence[TranscriptWord]],
    clip_durations: Mapping[str, float],
    *,
    max_gap: float = 0.35,
    pad: float = 0.08,
    min_len: float = MIN_SEGMENT_S,
) -> list[PlannedSegment]:
    out: list[PlannedSegment] = []
    for seg in segments:
        dur = clip_durations.get(seg.clip_id, seg.end)
        words = _words_in(seg, words_by_clip.get(seg.clip_id, ()))
        if not words:
            out.append(seg)
            continue
        if seg.role is SegmentRole.SPEAKER:
            out.extend(_tighten(seg, words, dur, max_gap=max_gap, pad=pad, min_len=min_len))
        else:
            out.append(_snap(seg, words_by_clip.get(seg.clip_id, ()), dur))
    return out


def _tighten(
    seg: PlannedSegment,
    words: list[TranscriptWord],
    dur: float,
    *,
    max_gap: float,
    pad: float,
    min_len: float,
) -> list[PlannedSegment]:
    groups: list[list[TranscriptWord]] = []
    prev_kept_idx = -2
    for i, w in enumerate(words):
        if is_filler(w.text):
            continue
        new_group = (
            not groups
            or i != prev_kept_idx + 1  # a filler was removed in between
            or w.start - groups[-1][-1].end > max_gap
        )
        if new_group:
            groups.append([w])
        else:
            groups[-1].append(w)
        prev_kept_idx = i
    pieces: list[tuple[float, float]] = []
    for g in groups:
        s = max(0.0, g[0].start - pad)  # padding may reach slightly past the LLM's in-point
        e = min(dur, g[-1].end + pad)
        if pieces and s < pieces[-1][1]:  # padding overlap across a short filler: meet halfway
            mid = (pieces[-1][1] + s) / 2
            pieces[-1] = (pieces[-1][0], mid)
            s = mid
        pieces.append((s, e))
    out = []
    for k, (s, e) in enumerate(p for p in pieces if p[1] - p[0] >= min_len):
        seg_id = seg.id if k == 0 else f"{seg.id}_{k + 1}"[:32]
        out.append(seg.model_copy(update={"id": seg_id, "start": round(s, 3), "end": round(e, 3)}))
    return out or [seg]


def _snap(seg: PlannedSegment, words: Sequence[TranscriptWord], dur: float) -> PlannedSegment:
    """Move a boundary that falls inside a word to that word's nearest edge.

    Nearest (not "always include the word") so that two consecutive segments from the
    same clip don't both claim the same word and show it twice.
    """
    start, end = seg.start, seg.end
    for w in words:
        mid = (w.start + w.end) / 2
        before, after = max(0.0, w.start - 0.03), min(dur, w.end + 0.03)  # same both sides:
        if w.start < start < w.end:  # adjacent cuts at one word edge meet exactly
            start = before if start < mid else after
        if w.start < end < w.end:
            end = after if end >= mid else before
    if end - start < MIN_SEGMENT_S or (start, end) == (seg.start, seg.end):
        return seg
    return seg.model_copy(update={"start": round(start, 3), "end": round(end, 3)})
