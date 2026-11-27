"""Vendor-neutral interfaces for every AI provider.

The rest of the system depends only on these types, so swapping Scribe for another ASR,
Gemini for another LLM, or Azure for another TTS is a new class plus a config value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


class ProviderError(RuntimeError):
    """A provider call failed after retries (network, auth, quota, bad response)."""

    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"{provider}: {message}")


# --------------------------------------------------------------------------- transcription


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start: float
    end: float
    kind: Literal["word", "event"] = "word"  # events: (laughter), (music)...
    speaker: str | None = None


@dataclass(frozen=True)
class Transcript:
    language: str
    text: str
    words: tuple[TranscriptWord, ...]
    audio_seconds: float  # billed duration
    provider: str
    model: str
    language_probability: float | None = None

    @property
    def spoken(self) -> tuple[TranscriptWord, ...]:
        return tuple(w for w in self.words if w.kind == "word")


class Transcriber(Protocol):
    name: str
    model: str

    def transcribe(self, audio: Path, *, language: str, duration_s: float) -> Transcript: ...


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    input_audio_tokens: int = 0  # subset of input_tokens, billed at a different rate
    output_tokens: int = 0  # includes thinking tokens
    cached_tokens: int = 0


@dataclass(frozen=True)
class MediaInput:
    clip_id: str
    path: Path  # low-res proxy
    mime_type: str
    description: str  # e.g. "c1: 12.3 s, portrait 1080x1920, speech"


@dataclass
class PlannerTurn:
    """One LLM exchange. Retries append feedback turns to the same conversation."""

    prompt: str
    media: Sequence[MediaInput]
    system: str
    response_schema: dict[str, object]
    feedback: list[tuple[str, str]] = field(default_factory=list)  # (previous answer, problems)


@dataclass(frozen=True)
class PlannerReply:
    text: str
    usage: LLMUsage
    model: str


class Planner(Protocol):
    name: str
    model: str

    def complete(self, turn: PlannerTurn) -> PlannerReply: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- speech synthesis


@dataclass(frozen=True)
class SpeechResult:
    audio_path: Path
    duration: float
    words: tuple[TranscriptWord, ...]  # timings in the audio; may be estimated
    characters: int  # billed characters
    provider: str
    voice: str
    timings_estimated: bool


class SpeechSynthesizer(Protocol):
    name: str
    max_speed: float  # fastest speaking-rate factor the provider allows

    def synthesize(
        self, text: str, out: Path, *, language: str, speed: float = 1.0
    ) -> SpeechResult: ...
