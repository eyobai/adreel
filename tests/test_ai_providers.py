"""Provider adapters against mocked HTTP / a fake google-genai client (no network)."""

from __future__ import annotations

import base64
import io
import itertools
import json
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from adreel.ai.align import chars_to_words, estimate_word_timings
from adreel.ai.azure_tts import AzureTTS, build_ssml
from adreel.ai.base import LLMUsage, MediaInput, PlannerTurn, ProviderError
from adreel.ai.costs import CostLog, Prices
from adreel.ai.elevenlabs import ElevenLabsTranscriber, ElevenLabsTTS, parse_scribe_response
from adreel.ai.gemini import GeminiPlanner, usage_from_response
from adreel.ai.http import request_with_retries

# ---------------------------------------------------------------- costs


def test_cost_log_math_and_summary(tmp_path: Path) -> None:
    log = CostLog(Prices())
    log.transcription("elevenlabs", "scribe_v1", 1800)  # half an hour
    log.llm(
        "gemini", "m", input_tokens=1_000_000, input_audio_tokens=200_000, output_tokens=100_000
    )
    log.tts("azure", "am-ET-MekdesNeural", 1000)
    log.tts("elevenlabs", "v", 1000)
    log.tts("fake", "v", 1000)
    log.transcription("fake", "fake", 3600)
    assert [round(e.usd, 6) for e in log.entries] == [
        0.2,
        round(0.8 * 0.30 + 0.2 * 1.0 + 0.1 * 2.5, 6),
        0.016,
        0.18,
        0.0,
        0.0,
    ]
    s = log.summary()
    assert s["total_usd"] == pytest.approx(0.2 + 0.69 + 0.016 + 0.18, abs=1e-5)
    assert s["units"]["characters"] == 3000  # type: ignore[index]
    log.write(tmp_path / "c.json")
    assert json.loads((tmp_path / "c.json").read_text())["by_step_usd"]["tts"] == pytest.approx(
        0.196
    )


# ---------------------------------------------------------------- http retries


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_then_succeeds() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    sleeps: list[float] = []
    r = request_with_retries(
        _client(handler), "GET", "https://x/y", provider="p", sleep=sleeps.append
    )
    assert r.json() == {"ok": True} and len(calls) == 3 and len(sleeps) == 2


def test_auth_error_fails_fast() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="bad key")

    with pytest.raises(ProviderError, match="401"):
        request_with_retries(
            _client(handler), "GET", "https://x", provider="p", sleep=lambda s: None
        )
    assert len(calls) == 1


def test_retry_after_is_honoured_and_gives_up() -> None:
    sleeps: list[float] = []
    handler = lambda req: httpx.Response(429, headers={"retry-after": "7"})  # noqa: E731
    with pytest.raises(ProviderError) as ei:
        request_with_retries(
            _client(handler), "GET", "https://x", provider="p", attempts=3, sleep=sleeps.append
        )
    assert sleeps == [7.0, 7.0] and ei.value.retryable


# ---------------------------------------------------------------- ElevenLabs

SCRIBE = {
    "language_code": "amh",
    "language_probability": 0.97,
    "text": "ሰላም እንዴት ናችሁ",
    "words": [
        {"text": "ሰላም", "start": 0.1, "end": 0.5, "type": "word", "speaker_id": "speaker_0"},
        {"text": " ", "start": 0.5, "end": 0.6, "type": "spacing"},
        {"text": "(ሳቅ)", "start": 0.6, "end": 0.9, "type": "audio_event"},
        {"text": "እንዴት", "start": 1.0, "end": 1.4, "type": "word"},
        {"text": "ናችሁ", "start": 1.45, "end": 1.45, "type": "word"},
    ],
}


def test_parse_scribe() -> None:
    tr = parse_scribe_response(SCRIBE, 2.0, "scribe_v1")
    assert [w.text for w in tr.spoken] == ["ሰላም", "እንዴት", "ናችሁ"]
    assert [w.kind for w in tr.words] == ["word", "event", "word", "word"]
    assert tr.spoken[-1].end > tr.spoken[-1].start  # zero-length word fixed
    assert tr.language == "amh" and tr.audio_seconds == 2.0


def test_scribe_request(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["key"] = req.headers["xi-api-key"]
        seen["body"] = req.content
        return httpx.Response(200, json=SCRIBE)

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3fake")
    t = ElevenLabsTranscriber("k123", client=_client(handler))
    tr = t.transcribe(audio, language="am", duration_s=2.0)
    assert seen["url"] == "https://api.elevenlabs.io/v1/speech-to-text" and seen["key"] == "k123"
    for part in (
        b'name="model_id"',
        b"scribe_v1",
        b'name="timestamps_granularity"',
        b"word",
        b'name="language_code"',
        b'filename="a.mp3"',
        b"ID3fake",
    ):
        assert part in seen["body"]
    assert len(tr.spoken) == 3


def test_elevenlabs_tts(tmp_path: Path) -> None:
    text = "ሰላም ዓለም"
    chars = list(text)
    starts = [i * 0.1 for i in range(len(chars))]
    body = {
        "audio_base64": base64.b64encode(b"MP3DATA").decode(),
        "alignment": {
            "characters": chars,
            "character_start_times_seconds": starts,
            "character_end_times_seconds": [s + 0.1 for s in starts],
        },
    }
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json=body)

    tts = ElevenLabsTTS("k", "voice1", client=_client(handler))
    r = tts.synthesize(text, tmp_path / "vo", language="am", speed=1.5)
    assert (
        "/v1/text-to-speech/voice1/with-timestamps" in seen["url"]
        and "output_format=mp3_44100_128" in seen["url"]
    )
    assert seen["json"]["voice_settings"]["speed"] == 1.2  # clamped to API max
    assert seen["json"]["language_code"] == "am"
    assert r.audio_path.read_bytes() == b"MP3DATA" and r.audio_path.suffix == ".mp3"
    assert [w.text for w in r.words] == ["ሰላም", "ዓለም"]
    assert (
        r.duration == pytest.approx(0.7) and not r.timings_estimated and r.characters == len(text)
    )


def test_elevenlabs_requires_key_and_voice() -> None:
    with pytest.raises(ProviderError, match="API_KEY"):
        ElevenLabsTranscriber("")
    with pytest.raises(ProviderError, match="VOICE_ID"):
        ElevenLabsTTS("k", None)


# ---------------------------------------------------------------- Azure


def _wav_bytes(seconds: float, rate: int = 48000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_ssml_escapes_and_sets_rate() -> None:
    ssml = build_ssml("ዋጋ <1,200> & ብር", "am-ET-AmehaNeural", 1.15)
    assert "xml:lang='am-ET'" in ssml and "name='am-ET-AmehaNeural'" in ssml
    assert "&lt;1,200&gt; &amp;" in ssml and "rate='+15%'" in ssml
    assert "rate='+0%'" in build_ssml("x", "am-ET-MekdesNeural")


def test_azure_tts(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, content=_wav_bytes(2.0))

    spans = lambda path, dur: [(0.1, 0.9), (1.2, 1.9)]  # noqa: E731
    tts = AzureTTS("key", "westeurope", "am-ET-MekdesNeural", spans, client=_client(handler))
    r = tts.synthesize("ሰላም ወዳጆች። እንኳን ደህና መጡ", tmp_path / "vo", language="am")
    assert seen["url"] == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    assert seen["headers"]["ocp-apim-subscription-key"] == "key"
    assert seen["headers"]["x-microsoft-outputformat"] == "riff-48khz-16bit-mono-pcm"
    assert r.duration == pytest.approx(2.0) and r.timings_estimated and len(r.words) == 5


def test_azure_rejects_garbage(tmp_path: Path) -> None:
    tts = AzureTTS(
        "k",
        "r",
        "am-ET-MekdesNeural",
        lambda p, d: [],
        client=_client(lambda req: httpx.Response(200, content=b"nope")),
    )
    with pytest.raises(ProviderError, match="invalid audio"):
        tts.synthesize("x", tmp_path / "vo", language="am")


# ---------------------------------------------------------------- alignment


def test_chars_to_words() -> None:
    words = chars_to_words(list(" ab  c "), [0, 1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6, 7])
    assert [(w.text, w.start, w.end) for w in words] == [("ab", 1, 3), ("c", 5, 6)]


def test_estimated_timings_respect_pauses() -> None:
    spans = [(0.0, 1.0), (1.5, 3.0)]
    words = estimate_word_timings("ሀለ መሠ ረሰ። ሸቀ በተ ቸኀ", spans, 3.0)
    assert len(words) == 6
    for w in words:
        assert any(s - 1e-6 <= w.start and w.end <= e + 1e-6 for s, e in spans), w
    assert all(a.start <= b.start for a, b in itertools.pairwise(words))
    assert words[0].start == 0.0 and words[-1].end == pytest.approx(3.0)


# ---------------------------------------------------------------- Gemini


class _FakeModels:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _FakeFiles:
    def __init__(self) -> None:
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.polls = 0

    def upload(self, file: str, config: Any) -> Any:
        self.uploaded.append(file)
        return SimpleNamespace(
            name=f"files/{len(self.uploaded)}",
            uri=f"https://g/{file}",
            mime_type="video/mp4",
            state=SimpleNamespace(name="PROCESSING"),
        )

    def get(self, name: str) -> Any:
        self.polls += 1
        return SimpleNamespace(
            name=name,
            uri=f"https://g/{name}",
            mime_type="video/mp4",
            state=SimpleNamespace(name="ACTIVE"),
        )

    def delete(self, name: str) -> None:
        self.deleted.append(name)


def _response(text: str = '{"a": 1}') -> Any:
    from google.genai import types

    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part.from_text(text=text)])
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1000,
            candidates_token_count=200,
            thoughts_token_count=50,
            prompt_tokens_details=[
                types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=300),
                types.ModalityTokenCount(modality=types.MediaModality.VIDEO, token_count=600),
            ],
        ),
        model_version="gemini-test-001",
    )


def _turn(tmp_path: Path, size: int = 10) -> PlannerTurn:
    p = tmp_path / "c1.mp4"
    p.write_bytes(b"\0" * size)
    return PlannerTurn(
        prompt="plan it",
        media=[MediaInput("c1", p, "video/mp4", "Clip c1: 3 s")],
        system="sys",
        response_schema={"type": "object"},
    )


def test_gemini_inline_request_and_usage(tmp_path: Path) -> None:
    from google.genai import types

    models = _FakeModels([_response()])
    client = SimpleNamespace(models=models, files=_FakeFiles())
    g = GeminiPlanner("", "gemini-x", client=client, thinking_budget=0)
    turn = _turn(tmp_path)
    turn.feedback.append(('{"bad": 1}', "fix it"))
    reply = g.complete(turn)
    call = models.calls[0]
    cfg: types.GenerateContentConfig = call["config"]
    assert call["model"] == "gemini-x"
    assert cfg.response_mime_type == "application/json" and cfg.response_json_schema == {
        "type": "object"
    }
    assert cfg.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert cfg.system_instruction == "sys" and cfg.thinking_config.thinking_budget == 0
    roles = [c.role for c in call["contents"]]
    assert roles == ["user", "model", "user"]
    first = call["contents"][0].parts
    assert first[0].text == "Clip c1: 3 s" and first[1].inline_data.mime_type == "video/mp4"
    assert first[-1].text == "plan it"
    assert reply.text == '{"a": 1}' and reply.model == "gemini-test-001"
    assert reply.usage == LLMUsage(input_tokens=1000, input_audio_tokens=300, output_tokens=250)


def test_gemini_uses_files_api_for_big_media_and_cleans_up(tmp_path: Path) -> None:
    files = _FakeFiles()
    client = SimpleNamespace(models=_FakeModels([_response(), _response()]), files=files)
    g = GeminiPlanner("", "m", client=client, inline_max_mb=0.000001)
    turn = _turn(tmp_path, size=100)
    g.complete(turn)
    g.complete(turn)  # retry reuses the upload
    assert len(files.uploaded) == 1 and files.polls == 1
    part = client.models.calls[0]["contents"][0].parts[1]
    assert part.file_data.file_uri.startswith("https://g/")
    g.close()
    assert files.deleted == ["files/1"]


def test_gemini_retries_server_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.genai import errors

    monkeypatch.setattr("adreel.ai.gemini.time.sleep", lambda s: None)
    err = errors.ServerError(
        503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}
    )
    models = _FakeModels([err, _response()])
    g = GeminiPlanner("", "m", client=SimpleNamespace(models=models, files=_FakeFiles()))
    assert g.complete(_turn(tmp_path)).text == '{"a": 1}'
    bad = errors.ClientError(
        400, {"error": {"code": 400, "message": "bad schema", "status": "INVALID_ARGUMENT"}}
    )
    g2 = GeminiPlanner(
        "", "m", client=SimpleNamespace(models=_FakeModels([bad]), files=_FakeFiles())
    )
    with pytest.raises(ProviderError, match="400"):
        g2.complete(_turn(tmp_path))


def test_gemini_requires_key() -> None:
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        GeminiPlanner("", "m")


def test_usage_without_metadata() -> None:
    assert usage_from_response(SimpleNamespace(usage_metadata=None)) == LLMUsage()


def test_planner_output_schema_is_accepted_by_genai_types() -> None:
    from google.genai import types

    from adreel.autoedit.schema import PlannerOutput

    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=PlannerOutput.model_json_schema(),
    )
    assert cfg.response_json_schema["title"] == "PlannerOutput"  # type: ignore[index]
