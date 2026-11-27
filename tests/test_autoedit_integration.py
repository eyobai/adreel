"""AutoEditor end to end with real ffmpeg and scripted/fake providers (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adreel.ai.base import LLMUsage, PlannerReply, PlannerTurn, ProviderError
from adreel.ai.fakes import FakeTranscriber, FakeTTS
from adreel.ai.registry import speech_span_fn
from adreel.autoedit.orchestrator import AutoEditError, AutoEditor, AutoEditRequest
from adreel.autoedit.schema import BusinessContext
from adreel.config import Settings
from adreel.render.ffmpeg import FFmpeg
from adreel.render.pipeline import Renderer
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import VoiceTier
from tests.conftest import requires_ffmpeg

pytestmark = [pytest.mark.ffmpeg, requires_ffmpeg]


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("auto_media")
    ff = FFmpeg()
    talk = d / "talk.mp4"  # "speech": 1.2 s tone, 0.8 s silence, repeating
    ff.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=480x270:r=25:d=8",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='0.4*sin(2*PI*220*t)*lt(mod(t\\,2)\\,1.2)':s=48000:d=8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(talk),
        ]
    )
    product = d / "product.mp4"
    ff.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=270x480:r=30:d=12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(product),
        ]
    )
    return {"c1": talk, "c2": product}


class ScriptedPlanner:
    name = "scripted"
    model = "scripted-1"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = replies
        self.turns: list[int] = []
        self.closed = False

    def complete(self, turn: PlannerTurn) -> PlannerReply:
        self.turns.append(len(turn.feedback))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return PlannerReply(
            r, LLMUsage(input_tokens=5000, input_audio_tokens=500, output_tokens=400), self.model
        )

    def close(self) -> None:
        self.closed = True


def _editor(tmp_path: Path, planner: object) -> AutoEditor:
    s = Settings(work_dir=tmp_path / "work", face_tracking=False)
    ff = FFmpeg()
    return AutoEditor(
        s,
        Renderer(settings=s, face_tracker=None),
        transcriber=FakeTranscriber(speech_span_fn(ff)),
        planner=planner,
        tts=lambda tier: FakeTTS(ff),
    )  # type: ignore[arg-type]


GOOD = {
    "title": "ቡና",
    "segments": [
        {"id": "s1", "clip_id": "c2", "start": 0.5, "end": 3.0, "role": "hook"},
        {"id": "s2", "clip_id": "c1", "start": 0.0, "end": 5.5, "role": "product"},
        {"id": "s3", "clip_id": "c2", "start": 3.0, "end": 11.5, "role": "product"},
        {"id": "s4", "clip_id": "c1", "start": 5.5, "end": 8.0, "role": "cta"},
    ],
    "caption_fixes": [{"id": 1, "text": "ሰላም ሰላም"}],
    "on_screen_text": [{"text": "አዲስ!", "segment_id": "s3", "style": "label", "position": "top"}],
    "slots": {"hook_text": "ትኩስ ቡና", "cta_text": "ይዘዙ!"},
    "price_card_segment_id": "s3",
    "voiceover_script": "ይርጋጨፌ ቡና። ዋጋው 1,200 ብር ብቻ። አሁኑኑ ይዘዙ!",
}


def test_retry_with_feedback_then_render(media: dict[str, Path], tmp_path: Path) -> None:
    bad_ref = json.loads(json.dumps(GOOD))
    bad_ref["segments"] = [
        {"id": "s1", "clip_id": "c1", "start": 0.0, "end": 2.0, "role": "hook"}
    ]  # too short
    planner = ScriptedPlanner(["not json at all", json.dumps(bad_ref), json.dumps(GOOD)])
    brand = BrandKit(business_name="ሙከራ", phone="0911234567")
    out = tmp_path / "out" / "promo.mp4"
    req = AutoEditRequest(
        "product_promo",
        brand,
        media,
        BusinessContext(product_name="ቡና", price=1200),
        out,
        voice=VoiceTier.STANDARD,
    )
    with _editor(tmp_path, planner) as ed:
        res = ed.run(req)
    assert planner.turns == [0, 1, 2] and planner.closed
    assert res.planner_used == "scripted" and res.attempts == 3
    assert [e.step for e in res.costs.entries].count("plan") == 3
    assert res.costs.entries[0].step == "transcribe"  # c1 has speech; c2 (silent) skipped
    assert sum(1 for e in res.costs.entries if e.step == "transcribe") == 1
    plan = res.plan
    assert plan.voiceover is not None and Path(plan.voiceover.audio_path or "").is_file()
    assert all(c.timebase.value == "output" for c in plan.captions)  # VO captions replace speech
    assert [c.id for c in plan.captions] == list(range(1, len(plan.captions) + 1))
    assert plan.slots.price == 1200 and plan.price_card.segment_id == "s3"
    assert out.is_file() and res.plan_path.is_file() and res.cost_path.is_file()
    cost = json.loads(res.cost_path.read_text())
    assert cost["planner"] == "scripted" and cost["attempts"] == 3 and cost["total_usd"] > 0
    assert 20 <= res.render.duration <= 30


def test_provider_error_falls_back_to_heuristic(media: dict[str, Path], tmp_path: Path) -> None:
    planner = ScriptedPlanner([ProviderError("gemini", "quota")])
    req = AutoEditRequest(
        "talking_head",
        BrandKit(business_name="ሙከራ"),
        {"c1": media["c1"]},
        BusinessContext(),
        tmp_path / "talk.mp4",
    )
    with _editor(tmp_path, planner) as ed:
        res = ed.run(req)
    assert res.planner_used == "heuristic" and any("planner failed" in w for w in res.warnings)
    # silences removed: speech is 1.2 s on / 0.8 s off over 8 s -> 4 tight pieces
    assert len(res.plan.segments) == 4
    assert all(c.timebase.value == "source" for c in res.plan.captions) and res.plan.captions
    assert res.render.duration < 8 + 2.5


def test_transcripts_are_cached(media: dict[str, Path], tmp_path: Path) -> None:
    req = AutoEditRequest(
        "talking_head",
        BrandKit(business_name="ሙከራ"),
        {"c1": media["c1"]},
        BusinessContext(),
        tmp_path / "a.mp4",
    )
    with _editor(tmp_path, None) as ed:
        first = ed.run(req)
        second = ed.run(AutoEditRequest(**{**req.__dict__, "output": tmp_path / "b.mp4"}))
    assert [e.step for e in first.costs.entries] == ["transcribe"]
    assert second.costs.entries == []  # nothing paid twice


def test_missing_facts_rejected_before_spending(media: dict[str, Path], tmp_path: Path) -> None:
    req = AutoEditRequest(
        "product_promo",
        BrandKit(business_name="x"),
        media,
        BusinessContext(product_name="ቡና"),
        tmp_path / "x.mp4",
    )
    with _editor(tmp_path, None) as ed, pytest.raises(AutoEditError, match="price"):
        ed.run(req)
