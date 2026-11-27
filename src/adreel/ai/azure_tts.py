"""Azure Neural TTS over REST (am-ET-MekdesNeural / am-ET-AmehaNeural).

REST keeps the image light (the Speech SDK is a large native library). It returns audio
only, so word timings are estimated from the audio's pauses (see ``ai.align``).
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from adreel.ai.align import Interval, estimate_word_timings
from adreel.ai.base import ProviderError, SpeechResult
from adreel.ai.http import request_with_retries

SpeechSpanFn = Callable[[Path, float], list[Interval]]


def build_ssml(text: str, voice: str, speed: float = 1.0) -> str:
    locale = "-".join(voice.split("-")[:2])  # am-ET-MekdesNeural -> am-ET
    rate = f"{round((speed - 1) * 100):+d}%"
    return (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{locale}'>"
        f"<voice name='{escape(voice)}'><prosody rate='{rate}'>{escape(text)}</prosody>"
        "</voice></speak>"
    )


class AzureTTS:
    name = "azure"
    max_speed = 1.3  # beyond this Amharic voices get hard to follow

    def __init__(
        self,
        api_key: str,
        region: str,
        voice: str,
        speech_spans: SpeechSpanFn,
        timeout_s: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError("azure-tts", "ADREEL_AZURE_SPEECH_KEY is not set")
        self._key = api_key
        self._url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self.voice = voice
        self._spans = speech_spans
        self._http = client or httpx.Client(timeout=timeout_s)

    def synthesize(
        self, text: str, out: Path, *, language: str, speed: float = 1.0
    ) -> SpeechResult:
        resp = request_with_retries(
            self._http,
            "POST",
            self._url,
            provider="azure-tts",
            headers={
                "Ocp-Apim-Subscription-Key": self._key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "riff-48khz-16bit-mono-pcm",
                "User-Agent": "adreel",
            },
            content=build_ssml(text, self.voice, speed).encode("utf-8"),
        )
        out = out.with_suffix(".wav")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.content)
        try:
            with wave.open(str(out), "rb") as w:
                duration = w.getnframes() / w.getframerate()
        except (wave.Error, EOFError) as exc:
            raise ProviderError("azure-tts", f"invalid audio returned: {exc}") from exc
        words = estimate_word_timings(text, self._spans(out, duration), duration)
        return SpeechResult(
            out, duration, tuple(words), len(text), "azure", self.voice, timings_estimated=True
        )
