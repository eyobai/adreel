"""Key-free providers: deterministic stand-ins for tests, demos and CI.

``FakeTranscriber`` puts Amharic placeholder words on the clip's real speech spans (from
silence detection), so smart cuts and caption timing behave as with real speech.
``FakeTTS`` produces a tone track whose bursts line up with known word timings.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from adreel.ai.align import Interval, estimate_word_timings
from adreel.ai.base import SpeechResult, Transcript, TranscriptWord
from adreel.render.ffmpeg import FFmpeg

_WORDS = (
    "ሰላም",
    "እንኳን",
    "ደህና",
    "መጡ",
    "ዛሬ",
    "አዲስ",
    "ምርት",
    "አለን",
    "በጣም",
    "ጥሩ",
    "ዋጋ",
    "ነው",
    "ይምጡ",
    "ይጎብኙን",
    "እናመሰግናለን",
)
WORDS_PER_SECOND = 2.4

SpeechSpanFn = Callable[[Path, float], list[Interval]]


class FakeTranscriber:
    name = "fake"
    model = "fake-stt"

    def __init__(self, speech_spans: SpeechSpanFn) -> None:
        self._spans = speech_spans

    def transcribe(self, audio: Path, *, language: str, duration_s: float) -> Transcript:
        words: list[TranscriptWord] = []
        i = 0
        for s, e in self._spans(audio, duration_s):
            n = max(1, round((e - s) * WORDS_PER_SECOND))
            step = (e - s) / n
            for k in range(n):
                text = _WORDS[i % len(_WORDS)]
                if k == n - 1:
                    text += "።"  # sentence end at every pause
                words.append(
                    TranscriptWord(text, round(s + k * step, 3), round(s + (k + 0.85) * step, 3))
                )
                i += 1
        return Transcript(
            language, " ".join(w.text for w in words), tuple(words), duration_s, "fake", self.model
        )


class FakeTTS:
    name = "fake"
    max_speed = 1.3

    def __init__(self, ff: FFmpeg, label: str = "fake") -> None:
        self._ff = ff
        self.name = label

    def synthesize(
        self, text: str, out: Path, *, language: str, speed: float = 1.0
    ) -> SpeechResult:
        tokens = text.split()
        duration = max(0.5, len(tokens) / (WORDS_PER_SECOND * speed))
        words = estimate_word_timings(text, [(0.0, duration)], duration)
        gate = "+".join(f"between(t,{w.start:.3f},{w.end:.3f})" for w in words) or "0"
        out = out.with_suffix(".wav")
        out.parent.mkdir(parents=True, exist_ok=True)
        self._ff.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"aevalsrc='0.3*sin(2*PI*520*t)*({gate})':s=48000:d={duration:.3f}",
                "-ac",
                "1",
                str(out),
            ]
        )
        return SpeechResult(
            out, duration, tuple(words), len(text), "fake", "fake-voice", timings_estimated=True
        )
