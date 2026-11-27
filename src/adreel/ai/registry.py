"""Build providers from settings. The only place that knows concrete vendor classes."""

from __future__ import annotations

from pathlib import Path

from adreel.ai.align import Interval
from adreel.ai.base import Planner, ProviderError, SpeechSynthesizer, Transcriber
from adreel.analysis import complement, detect_silences
from adreel.config import Settings
from adreel.render.ffmpeg import FFmpeg
from adreel.schemas.edit_plan import VoiceTier


def _secret(v: object) -> str:
    return v.get_secret_value() if v is not None else ""  # type: ignore[attr-defined]


def speech_span_fn(ff: FFmpeg):  # type: ignore[no-untyped-def]
    def spans(path: Path, duration: float) -> list[Interval]:
        return complement(
            detect_silences(ff, path, duration, noise_db=-40, min_silence=0.15), duration
        )

    return spans


def build_transcriber(s: Settings, ff: FFmpeg) -> Transcriber:
    if s.transcriber == "fake":
        from adreel.ai.fakes import FakeTranscriber

        return FakeTranscriber(speech_span_fn(ff))
    from adreel.ai.elevenlabs import ElevenLabsTranscriber

    return ElevenLabsTranscriber(
        _secret(s.elevenlabs_api_key),
        s.elevenlabs_stt_model,
        s.elevenlabs_base_url,
        s.http_timeout_s,
    )


def build_planner(s: Settings) -> Planner | None:
    """None means: use the deterministic heuristic planner."""
    if s.planner == "heuristic":
        return None
    from adreel.ai.gemini import GeminiPlanner

    return GeminiPlanner(
        _secret(s.gemini_api_key),
        s.gemini_model,
        media_resolution=s.gemini_media_resolution,
        thinking_budget=s.gemini_thinking_budget,
        temperature=s.gemini_temperature,
        inline_max_mb=s.gemini_inline_max_mb,
        timeout_s=max(s.http_timeout_s, 300),
    )


def build_tts(s: Settings, tier: VoiceTier, ff: FFmpeg) -> SpeechSynthesizer:
    kind = s.tts_premium if tier is VoiceTier.PREMIUM else s.tts_standard
    if kind == "fake":
        from adreel.ai.fakes import FakeTTS

        return FakeTTS(ff)
    if kind == "azure":
        from adreel.ai.azure_tts import AzureTTS

        return AzureTTS(
            _secret(s.azure_speech_key),
            s.azure_speech_region,
            s.azure_voice,
            speech_span_fn(ff),
            s.http_timeout_s,
        )
    if kind == "elevenlabs":
        from adreel.ai.elevenlabs import ElevenLabsTTS

        return ElevenLabsTTS(
            _secret(s.elevenlabs_api_key),
            s.elevenlabs_voice_id,
            s.elevenlabs_tts_model,
            s.elevenlabs_tts_language,
            s.elevenlabs_base_url,
            s.http_timeout_s,
        )
    raise ProviderError("tts", f"unknown TTS provider {kind!r}")  # pragma: no cover
