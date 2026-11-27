"""Caption timing and layout. Pure functions, no I/O, fully unit-tested.

Pipeline: ``map_to_output`` (source time -> output time through the cuts)
-> ``layout`` (split long lines, wrap, clamp, de-overlap, enforce minimum duration).

Width is measured in *units* where one Ethiopic syllable = 1.0. Latin letters/digits are
narrower (~0.8), spaces ~0.32. Ethiopic fidel are wide, so a char count based on Latin
heuristics would overflow lines.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from adreel.render.timeline import Timeline
from adreel.schemas.edit_plan import CaptionLine, Timebase

_ETHIOPIC_RANGES = ((0x1200, 0x139F), (0x2D80, 0x2DDF), (0xAB00, 0xAB2F), (0x1E7E0, 0x1E7FF))
_TOKEN_SPLIT = re.compile(r"\s+|(?<=፡)")


def is_ethiopic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _ETHIOPIC_RANGES)


# One Ethiopic syllable in Noto Sans Ethiopic Bold renders ≈0.50 × ASS font size wide in
# libass (measured; libass scales fonts by ascent+descent). Kept slightly pessimistic.
ETHIOPIC_ADVANCE_PER_PX = 0.52


def text_units(text: str) -> float:
    """Visual width in Ethiopic-syllable units (calibrated against libass renders)."""
    total = 0.0
    for ch in text:
        if ch.isspace():
            total += 0.32
        elif is_ethiopic(ch):
            total += 1.0
        elif ch.isupper() or ch in "MW@%&":
            total += 0.92
        else:
            total += 0.8
    return total


def text_width_px(text: str, font_size: float) -> float:
    return text_units(text) * ETHIOPIC_ADVANCE_PER_PX * font_size


def units_that_fit(width_px: float, font_size: float) -> float:
    return width_px / (ETHIOPIC_ADVANCE_PER_PX * font_size)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(text.strip()) if t]


def _join(tokens: Sequence[str]) -> str:
    out = ""
    for t in tokens:
        out += t if (not out or out.endswith("፡")) else " " + t
    return out


def wrap_lines(text: str, max_units: float, max_lines: int) -> list[str]:
    """Balanced wrap into the fewest lines (<= max_lines when possible).

    Among partitions with the minimal line count, picks the one with the smallest widest
    line, so 2-line captions look even instead of "long line + one word".
    """
    tokens = tokenize(text)
    if not tokens:
        return []
    if text_units(_join(tokens)) <= max_units or len(tokens) == 1:
        return [_join(tokens)]
    n = len(tokens)
    best: tuple[float, list[str]] | None = None
    for k in range(2, max(2, max_lines) + 1):
        for cuts in _partitions(n, k):
            lines = [_join(tokens[a:b]) for a, b in zip((0, *cuts), (*cuts, n), strict=True)]
            widest = max(text_units(ln) for ln in lines)
            if widest <= max_units and (best is None or widest < best[0]):
                best = (widest, lines)
        if best is not None:
            return best[1]
    # Nothing fits (very long words): fall back to greedy fill, overflow allowed.
    return _greedy(tokens, max_units)


def _partitions(n: int, k: int) -> list[tuple[int, ...]]:
    """All ways to cut n tokens into k non-empty runs (k <= 3, n small: cheap)."""
    if k == 1:
        return [()]
    out: list[tuple[int, ...]] = []
    for first in range(1, n - k + 2):
        out.extend((first, *(first + c for c in rest)) for rest in _partitions(n - first, k - 1))
    return out


def _greedy(tokens: Sequence[str], max_units: float) -> list[str]:
    lines: list[list[str]] = [[]]
    for t in tokens:
        if lines[-1] and text_units(_join([*lines[-1], t])) > max_units:
            lines.append([])
        lines[-1].append(t)
    return [_join(ln) for ln in lines]


def chunk_tokens(tokens: Sequence[str], max_units: float, max_lines: int) -> list[list[str]]:
    """Split tokens into consecutive chunks that each fit in ``max_lines`` wrapped lines."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        trial = [*cur, t]
        if cur and len(wrap_lines(_join(trial), max_units, max_lines)) > max_lines:
            chunks.append(cur)
            cur = [t]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    # Avoid a dangling single short word at the end: rebalance the last two chunks.
    if len(chunks) >= 2 and len(chunks[-1]) == 1 and len(chunks[-2]) > 2:
        moved = chunks[-2].pop()
        chunks[-1].insert(0, moved)
    return chunks


# --------------------------------------------------------------------------- events


@dataclass(frozen=True)
class TimedWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class CaptionEvent:
    caption_id: int
    start: float
    end: float
    text: str
    words: tuple[TimedWord, ...] = ()
    lines: tuple[str, ...] = field(default=())

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class CaptionLayout:
    max_units_per_line: float = 16
    max_lines: int = 2
    min_duration_s: float = 0.7
    min_visible_s: float = 0.25
    gap_s: float = 0.04
    merge_gap_s: float = 0.12


def merge_intervals(
    intervals: Sequence[tuple[float, float]], max_gap: float
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(intervals):
        if out and s - out[-1][1] <= max_gap:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def map_to_output(
    captions: Sequence[CaptionLine], timeline: Timeline, merge_gap_s: float = 0.12
) -> list[CaptionEvent]:
    """Project captions onto the output timeline.

    * output-timebase captions pass through unchanged
    * with word timings: each word survives only if >= 50% of it is kept by the cuts, so
      a removed filler word ("እ...") disappears from the caption text too
    * without word timings: pieces split by a removed silence are re-joined (they are
      adjacent in the output); if the line was split across re-ordered segments, the
      longest visible piece carries the text
    """
    events: list[CaptionEvent] = []
    for c in captions:
        if c.timebase is Timebase.OUTPUT:
            words = tuple(TimedWord(w.text, w.start, w.end) for w in c.words)
            events.append(CaptionEvent(c.id, c.start, c.end, c.text, words))
            continue
        assert c.clip_id is not None
        if c.words:
            mapped: list[tuple[TimedWord, float, float]] = []  # (output word, src start, src end)
            for w in c.words:
                pieces = timeline.source_to_output(c.clip_id, w.start, w.end)
                covered = sum(e - s for s, e in pieces)
                if not pieces or covered < 0.5 * (w.end - w.start):
                    continue
                s, e = max(merge_intervals(pieces, 0.0), key=lambda p: p[1] - p[0])
                mapped.append((TimedWord(w.text, s, e), w.start, w.end))
            for run in _runs(mapped, merge_gap_s):
                text = _join([w.text for w in run])
                events.append(CaptionEvent(c.id, run[0].start, run[-1].end, text, tuple(run)))
        else:
            pieces = merge_intervals(
                timeline.source_to_output(c.clip_id, c.start, c.end), merge_gap_s
            )
            if not pieces:
                continue
            s, e = max(pieces, key=lambda p: p[1] - p[0])
            events.append(CaptionEvent(c.id, s, e, c.text))
    return events


def _runs(words: Sequence[tuple[TimedWord, float, float]], max_gap: float) -> list[list[TimedWord]]:
    """Group consecutive surviving words that stay together on the output timeline.

    A new run starts only on a real discontinuity: the next word lands *before* the
    previous one (re-ordered segments) or further away than it was in the source (another
    segment was inserted between them). Natural pauses and removed silences keep the run.
    """
    runs: list[list[TimedWord]] = []
    prev: tuple[TimedWord, float, float] | None = None
    for item in words:
        w, s_start, _ = item
        if prev is not None and runs:
            out_gap = w.start - prev[0].end
            src_gap = s_start - prev[2]
            # small negative gaps are ASR jitter
            if -0.05 <= out_gap <= max(src_gap, 0.0) + max_gap:
                runs[-1].append(w)
                prev = item
                continue
        runs.append([w])
        prev = item
    return runs


def _split_event(ev: CaptionEvent, cfg: CaptionLayout) -> list[CaptionEvent]:
    tokens = tokenize(ev.text)
    chunks = chunk_tokens(tokens, cfg.max_units_per_line, cfg.max_lines)
    if len(chunks) <= 1:
        return [ev]
    if ev.words and len(ev.words) == len(tokens):
        out, i = [], 0
        for ch in chunks:
            ws = ev.words[i : i + len(ch)]
            i += len(ch)
            out.append(replace(ev, start=ws[0].start, end=ws[-1].end, text=_join(ch), words=ws))
        # keep the line on screen across the inter-chunk pauses
        return [
            replace(e, end=out[j + 1].start) if j + 1 < len(out) else replace(e, end=ev.end)
            for j, e in enumerate(out)
        ]
    # No word timings: distribute the time proportionally to visual width.
    widths = [text_units(_join(ch)) for ch in chunks]
    total = sum(widths)
    out, t = [], ev.start
    for ch, wdt in zip(chunks, widths, strict=True):
        d = ev.duration * wdt / total
        out.append(replace(ev, start=t, end=t + d, text=_join(ch), words=()))
        t += d
    return out


def layout(
    events: Sequence[CaptionEvent], cfg: CaptionLayout, main_duration: float
) -> list[CaptionEvent]:
    """Turn mapped events into display-ready, non-overlapping, wrapped caption events."""
    split: list[CaptionEvent] = []
    for ev in sorted(events, key=lambda e: (e.start, e.caption_id)):
        split.extend(_split_event(ev, cfg))

    # clamp to the main part of the video (never over the end card)
    clamped = [
        replace(e, start=max(0.0, e.start), end=min(e.end, main_duration))
        for e in split
        if min(e.end, main_duration) - max(0.0, e.start) > 0.02
    ]
    clamped.sort(key=lambda e: (e.start, e.caption_id))

    # resolve overlaps: an earlier caption gives way to the next one
    resolved: list[CaptionEvent] = []
    for i, e in enumerate(clamped):
        if i + 1 < len(clamped) and clamped[i + 1].start < e.end + cfg.gap_s:
            e = replace(e, end=clamped[i + 1].start - cfg.gap_s)
            if e.duration < cfg.min_visible_s:
                continue  # squeezed to a flash by the next caption: drop it
        resolved.append(e)

    # enforce minimum on-screen time: extend forward into the gap, then backward
    out: list[CaptionEvent] = []
    for i, e in enumerate(resolved):
        if e.duration < cfg.min_duration_s:
            nxt = resolved[i + 1].start - cfg.gap_s if i + 1 < len(resolved) else main_duration
            end = min(e.start + cfg.min_duration_s, nxt, main_duration)
            prev_end = out[-1].end + cfg.gap_s if out else 0.0
            start = max(prev_end, min(e.start, end - cfg.min_duration_s), 0.0)
            e = replace(e, start=start, end=max(end, e.end))
        lines = tuple(wrap_lines(e.text, cfg.max_units_per_line, cfg.max_lines))
        words = tuple(
            TimedWord(w.text, max(w.start, e.start), min(w.end, e.end))
            for w in e.words
            if w.end > e.start and w.start < e.end
        )
        out.append(replace(e, lines=lines, words=words))
    return out


def caption_listing(captions: Sequence[CaptionLine]) -> str:
    """Numbered caption list as shown to the user in the "fix captions" flow."""
    return "\n".join(f"{c.id}: {c.text}" for c in sorted(captions, key=lambda c: c.id))
