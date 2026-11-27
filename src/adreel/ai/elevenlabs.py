"""ElevenLabs: Scribe speech-to-text (word timestamps) and premium TTS (char timestamps)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from adreel.ai.align import chars_to_words
from adreel.ai.base import ProviderError, SpeechResult, Transcript, TranscriptWord
from adreel.ai.http import request_with_retries


class _ElevenLabsClient:
    def __init__(
        self, api_key: str, base_url: str, timeout_s: float, client: httpx.Client | None
    ) -> None:
        if not api_key:
            raise ProviderError("elevenlabs", "ADREEL_ELEVENLABS_API_KEY is not set")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=timeout_s)

    def _post(self, path: str, provider: str, **kwargs: Any) -> httpx.Response:
        headers = {"xi-api-key": self._key, **kwargs.pop("headers", {})}
        return request_with_retries(
            self._http, "POST", f"{self._base}{path}", provider=provider, headers=headers, **kwargs
        )


class ElevenLabsTranscriber(_ElevenLabsClient):
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        model: str = "scribe_v1",
        base_url: str = "https://api.elevenlabs.io",
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(api_key, base_url, timeout_s, client)
        self.model = model

    def transcribe(self, audio: Path, *, language: str, duration_s: float) -> Transcript:
        data = audio.read_bytes()  # small mono MP3; bytes so retries can resend
        resp = self._post(
            "/v1/speech-to-text",
            "elevenlabs-stt",
            data={
                "model_id": self.model,
                "language_code": language,
                "timestamps_granularity": "word",
                "tag_audio_events": "true",
                "diarize": "false",
            },
            files={"file": (audio.name, data, "audio/mpeg")},
        )
        return parse_scribe_response(resp.json(), duration_s, self.model)


def parse_scribe_response(body: dict[str, Any], duration_s: float, model: str) -> Transcript:
    words: list[TranscriptWord] = []
    for w in body.get("words") or []:
        kind = w.get("type", "word")
        text = (w.get("text") or "").strip()
        if kind == "spacing" or not text or w.get("start") is None or w.get("end") is None:
            continue
        words.append(
            TranscriptWord(
                text=text,
                start=float(w["start"]),
                end=max(float(w["end"]), float(w["start"]) + 0.01),
                kind="event" if kind == "audio_event" else "word",
                speaker=w.get("speaker_id"),
            )
        )
    return Transcript(
        language=str(body.get("language_code") or ""),
        text=str(body.get("text") or ""),
        words=tuple(words),
        audio_seconds=duration_s,
        provider="elevenlabs",
        model=model,
        language_probability=body.get("language_probability"),
    )


class ElevenLabsTTS(_ElevenLabsClient):
    name = "elevenlabs"
    max_speed = 1.2  # API limit for voice_settings.speed

    def __init__(
        self,
        api_key: str,
        voice_id: str | None,
        model: str = "eleven_v3",
        language_code: str | None = "am",
        base_url: str = "https://api.elevenlabs.io",
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(api_key, base_url, timeout_s, client)
        if not voice_id:
            raise ProviderError("elevenlabs", "ADREEL_ELEVENLABS_VOICE_ID is not set")
        self.voice_id = voice_id
        self.model = model
        self.language_code = language_code

    def synthesize(
        self, text: str, out: Path, *, language: str, speed: float = 1.0
    ) -> SpeechResult:
        body: dict[str, Any] = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"speed": round(min(max(speed, 0.7), self.max_speed), 2)},
        }
        if self.language_code:
            body["language_code"] = self.language_code
        resp = self._post(
            f"/v1/text-to-speech/{self.voice_id}/with-timestamps",
            "elevenlabs-tts",
            params={"output_format": "mp3_44100_128"},
            json=body,
        )
        data = resp.json()
        audio_b64 = data.get("audio_base64")
        if not audio_b64:
            raise ProviderError("elevenlabs-tts", "response has no audio")
        out = out.with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(audio_b64))
        al = data.get("alignment") or data.get("normalized_alignment") or {}
        words = chars_to_words(
            al.get("characters", []),
            al.get("character_start_times_seconds", []),
            al.get("character_end_times_seconds", []),
        )
        duration = max((w.end for w in words), default=0.0)
        return SpeechResult(
            out,
            duration,
            tuple(words),
            len(text),
            "elevenlabs",
            self.voice_id,
            timings_estimated=False,
        )
