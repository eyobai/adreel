"""Gemini (google-genai SDK) as the planner LLM.

* Video goes in as low-res proxies (``media_resolution=low``: far fewer tokens per frame;
  enough to judge framing, motion and what's on screen). Small batches are sent inline;
  larger ones go through the Files API and are deleted afterwards.
* Output is constrained with ``response_json_schema`` and still validated by Pydantic
  on our side (schema-constrained decoding is not a guarantee).
* Retries with feedback reuse the same conversation and the same uploaded files.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from adreel.ai.base import LLMUsage, MediaInput, PlannerReply, PlannerTurn, ProviderError
from adreel.log import get_logger

log = get_logger(__name__)

RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


class GeminiPlanner:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        media_resolution: str = "low",
        thinking_budget: int | None = None,
        temperature: float = 0.4,
        inline_max_mb: float = 15.0,
        timeout_s: float = 300.0,
        client: Any = None,
    ) -> None:
        from google import genai
        from google.genai import types

        if not api_key and client is None:
            raise ProviderError("gemini", "ADREEL_GEMINI_API_KEY is not set")
        self._types = types
        self._client = client or genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000))
        )
        self.model = model
        self._media_resolution = {
            "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        }[media_resolution]
        self._thinking_budget = thinking_budget
        self._temperature = temperature
        self._inline_max = inline_max_mb * 1024 * 1024
        self._uploaded: dict[Path, Any] = {}

    # ------------------------------------------------------------------ media

    def _media_parts(self, media: list[MediaInput]) -> list[Any]:
        t = self._types
        inline = sum(m.path.stat().st_size for m in media) <= self._inline_max
        parts: list[Any] = []
        for m in media:
            parts.append(t.Part.from_text(text=m.description))
            if inline:
                parts.append(t.Part.from_bytes(data=m.path.read_bytes(), mime_type=m.mime_type))
            else:
                f = self._upload(m)
                parts.append(t.Part.from_uri(file_uri=f.uri, mime_type=f.mime_type))
        return parts

    def _upload(self, m: MediaInput) -> Any:
        if m.path in self._uploaded:
            return self._uploaded[m.path]
        f = self._client.files.upload(file=str(m.path), config={"mime_type": m.mime_type})
        deadline = time.monotonic() + 300
        while getattr(f.state, "name", str(f.state)) == "PROCESSING":
            if time.monotonic() > deadline:
                raise ProviderError("gemini", f"file processing timed out: {m.path.name}")
            time.sleep(2)
            f = self._client.files.get(name=str(f.name))
        if getattr(f.state, "name", str(f.state)) == "FAILED":
            raise ProviderError("gemini", f"file processing failed: {m.path.name}")
        self._uploaded[m.path] = f
        return f

    # ------------------------------------------------------------------ completion

    def complete(self, turn: PlannerTurn) -> PlannerReply:
        t = self._types
        contents = [
            t.Content(
                role="user",
                parts=[*self._media_parts(list(turn.media)), t.Part.from_text(text=turn.prompt)],
            )
        ]
        for previous, problems in turn.feedback:
            contents.append(t.Content(role="model", parts=[t.Part.from_text(text=previous)]))
            contents.append(t.Content(role="user", parts=[t.Part.from_text(text=problems)]))
        config = t.GenerateContentConfig(
            system_instruction=turn.system,
            response_mime_type="application/json",
            response_json_schema=turn.response_schema,
            temperature=self._temperature,
            media_resolution=self._media_resolution,
            thinking_config=(
                t.ThinkingConfig(thinking_budget=self._thinking_budget)
                if self._thinking_budget is not None
                else None
            ),
        )
        resp = self._generate(contents, config)
        text = resp.text
        if not text:
            reason = getattr(resp, "prompt_feedback", None)
            raise ProviderError("gemini", f"empty response (feedback: {reason})")
        return PlannerReply(
            text=text, usage=usage_from_response(resp), model=resp.model_version or self.model
        )

    def _generate(self, contents: list[Any], config: Any, attempts: int = 4) -> Any:
        from google.genai import errors

        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                return self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except errors.APIError as exc:
                if exc.code not in RETRYABLE_CODES or attempt == attempts:
                    raise ProviderError(
                        "gemini",
                        f"{exc.code}: {exc.message}",
                        retryable=exc.code in RETRYABLE_CODES,
                    ) from exc
                log.warning("gemini.retry", attempt=attempt, code=exc.code)
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        for f in self._uploaded.values():
            try:
                self._client.files.delete(name=f.name)
            except Exception as exc:  # cleanup is best effort; files expire after 48 h anyway
                log.warning("gemini.file_delete_failed", file=f.name, error=str(exc))
        self._uploaded.clear()


def usage_from_response(resp: Any) -> LLMUsage:
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return LLMUsage()
    audio = 0
    for d in getattr(um, "prompt_tokens_details", None) or []:
        if str(getattr(d.modality, "name", d.modality)).upper() == "AUDIO":
            audio += d.token_count or 0
    return LLMUsage(
        input_tokens=um.prompt_token_count or 0,
        input_audio_tokens=audio,
        output_tokens=(um.candidates_token_count or 0) + (um.thoughts_token_count or 0),
        cached_tokens=um.cached_content_token_count or 0,
    )
