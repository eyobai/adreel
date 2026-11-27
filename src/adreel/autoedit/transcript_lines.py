"""Turn word-level transcripts into caption lines (deterministic; exact timings).

Lines break on sentence punctuation, pauses, length and duration. They get stable ids
that the planner refers to for spelling fixes; after the edit, visible lines are renumbered
in output order so the user sees "1:, 2:, 3:" in the fix-captions flow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from adreel.ai.base import Transcript, TranscriptWord
from adreel.render.captions import map_to_output, text_units, tokenize
from adreel.render.timeline import build_timeline
from adreel.schemas.edit_plan import CaptionLine, EditPlan, Timebase, Word

SENTENCE_END = ("።", "?", "!", "፧", ".", "፨")
MAX_WORD_CHARS = 40
MAX_LINE_CHARS = 160


@dataclass(frozen=True)
class TranscriptLine:
    id: int
    clip_id: str | None  # None = output timebase (voiceover)
    words: tuple[TranscriptWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def split_lines(
    words: Sequence[TranscriptWord],
    *,
    max_units: float = 28,
    max_gap: float = 0.6,
    max_duration: float = 4.5,
) -> list[list[TranscriptWord]]:
    lines: list[list[TranscriptWord]] = []
    cur: list[TranscriptWord] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            too_long = text_units(" ".join(x.text for x in [*cur, w])) > max_units
            too_slow = w.end - cur[0].start > max_duration
            if gap > max_gap or too_long or too_slow:
                lines.append(cur)
                cur = []
        cur.append(w)
        if w.text.endswith(SENTENCE_END) and not w.text.endswith(
            ("..", "…")
        ):  # ellipsis = hesitation
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def build_transcript_lines(
    transcripts: Mapping[str, Transcript], *, max_units: float = 28, start_id: int = 1
) -> list[TranscriptLine]:
    out: list[TranscriptLine] = []
    i = start_id
    for clip_id in sorted(transcripts):
        for ws in split_lines(transcripts[clip_id].spoken, max_units=max_units):
            out.append(TranscriptLine(i, clip_id, tuple(ws)))
            i += 1
    return out


def lines_from_words(
    words: Sequence[TranscriptWord], *, offset: float, max_units: float
) -> list[TranscriptLine]:
    """Output-timebase lines (voiceover): word times shifted by ``offset``."""
    shifted = [TranscriptWord(w.text, w.start + offset, w.end + offset) for w in words]
    return [
        TranscriptLine(i, None, tuple(ws))
        for i, ws in enumerate(split_lines(shifted, max_units=max_units), 1)
    ]


def to_caption_lines(
    lines: Sequence[TranscriptLine], fixes: Mapping[int, str] | None = None
) -> list[CaptionLine]:
    """CaptionLines, applying spelling fixes. A fix that keeps the word count keeps the word
    timings (so active-word highlight still works); otherwise only line timing is kept."""
    fixes = fixes or {}
    out: list[CaptionLine] = []
    for ln in lines:
        text = fixes.get(ln.id, ln.text).strip()[:MAX_LINE_CHARS]
        new_tokens = tokenize(text)
        words: list[Word] = []
        if len(new_tokens) == len(ln.words) and all(len(t) <= MAX_WORD_CHARS for t in new_tokens):
            words = [
                Word(text=t, start=w.start, end=w.end)
                for t, w in zip(new_tokens, ln.words, strict=True)
            ]
        out.append(
            CaptionLine(
                id=ln.id,
                text=text,
                timebase=Timebase.SOURCE if ln.clip_id else Timebase.OUTPUT,
                clip_id=ln.clip_id,
                start=ln.start,
                end=max(ln.end, ln.start + 0.05),
                words=words,
            )
        )
    return out


def finalize_captions(plan: EditPlan, fps: int) -> EditPlan:
    """Drop captions that the cuts made invisible; renumber the rest 1..n in output order."""
    tl = build_timeline(plan, fps)
    visible: list[tuple[float, CaptionLine]] = []
    for c in plan.captions:
        ev = map_to_output([c], tl)
        if ev:
            visible.append((min(e.start for e in ev), c))
    visible.sort(key=lambda x: x[0])
    caps = [c.model_copy(update={"id": i}) for i, (_, c) in enumerate(visible, 1)]
    return plan.model_copy(update={"captions": caps})
