"""Word timings for synthesized speech.

* ElevenLabs returns per-character timings: ``chars_to_words`` folds them into words.
* Azure's REST API returns audio only. ``estimate_word_timings`` spreads the words over
  the speech spans found by silence detection, weighted by visual width. TTS pauses fall
  at commas and sentence ends, so line-level caption timing is good; individual word
  highlights are approximate (they are flagged ``timings_estimated``).
"""

from __future__ import annotations

from collections.abc import Sequence

from adreel.ai.base import TranscriptWord
from adreel.render.captions import text_units, tokenize

Interval = tuple[float, float]


def chars_to_words(
    chars: Sequence[str], starts: Sequence[float], ends: Sequence[float]
) -> list[TranscriptWord]:
    words: list[TranscriptWord] = []
    buf, w_start, w_end = "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends, strict=True):
        if ch.isspace():
            if buf:
                words.append(TranscriptWord(buf, w_start, w_end))
                buf = ""
            continue
        if not buf:
            w_start = s
        buf += ch
        w_end = e
    if buf:
        words.append(TranscriptWord(buf, w_start, w_end))
    return words


def estimate_word_timings(
    text: str, spans: Sequence[Interval], duration: float
) -> list[TranscriptWord]:
    tokens = tokenize(text)
    if not tokens:
        return []
    spans = [s for s in spans if s[1] > s[0]] or [(0.0, max(duration, 0.1))]
    weights = [text_units(t) + 0.4 for t in tokens]  # +0.4: per-word articulation overhead
    total_w = sum(weights)
    lengths = [e - s for s, e in spans]
    total_s = sum(lengths)

    def at(frac: float) -> tuple[float, int]:
        """Map a fraction of speech time to (absolute time, span index)."""
        target = min(max(frac, 0.0), 1.0) * total_s
        acc = 0.0
        for i, ((s, _), ln) in enumerate(zip(spans, lengths, strict=True)):
            if target <= acc + ln or i == len(spans) - 1:
                return s + min(target - acc, ln), i
            acc += ln
        return spans[-1][1], len(spans) - 1  # pragma: no cover

    out: list[TranscriptWord] = []
    cum = 0.0
    for tok, w in zip(tokens, weights, strict=True):
        start, si = at(cum / total_w)
        _, mi = at((cum + w / 2) / total_w)
        end, ei = at((cum + w) / total_w)
        # never let a word straddle a pause: keep it inside the span holding its midpoint
        s_lo, s_hi = spans[mi]
        start = max(start, s_lo) if si != mi else start
        end = min(end, s_hi) if ei != mi else end
        if end - start < 0.05:
            end = min(s_hi, start + 0.05)
        out.append(TranscriptWord(tok, round(start, 3), round(end, 3)))
        cum += w
    return out
