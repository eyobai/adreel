"""Changing a finished edit with a prompt: engine, prompt, worker job."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from adreel.ai.base import LLMUsage, PlannerReply, PlannerTurn, ProviderError
from adreel.ai.fakes import FakeTranscriber, FakeTTS
from adreel.ai.registry import speech_span_fn
from adreel.autoedit.orchestrator import (
    AutoEditor,
    AutoEditRequest,
    RevisionError,
    carry_fixes,
)
from adreel.autoedit.prompt import ClipAnalysis, build_revision_prompt
from adreel.autoedit.schema import BusinessContext
from adreel.autoedit.transcript_lines import build_transcript_lines
from adreel.config import Settings
from adreel.render.ffmpeg import FFmpeg
from adreel.render.pipeline import Renderer
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan, VoiceTier
from adreel.schemas.template import TemplateSpec
from tests.conftest import make_plan, requires_ffmpeg

# ---------------------------------------------------------------- helpers


class ScriptedPlanner:
    name = "scripted"
    model = "scripted-1"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def complete(self, turn: PlannerTurn) -> PlannerReply:
        self.prompts.append(turn.prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return PlannerReply(r, LLMUsage(input_tokens=4000, output_tokens=300), self.model)

    def close(self) -> None:
        pass


def as_reply(plan: EditPlan, **changes: Any) -> str:
    """A planner reply that reproduces ``plan`` (as PlannerOutput) with some changes."""
    out: dict[str, Any] = {
        "segments": [
            {"id": s.id, "clip_id": s.clip_id, "start": s.start, "end": s.end, "role": s.role.value}
            for s in plan.segments
        ],
        "slots": plan.slots.model_dump(mode="json", exclude_none=True),
        "on_screen_text": [t.model_dump(mode="json") for t in plan.on_screen_text],
        "look": plan.look if plan.look != "none" else "natural",
        "voiceover_script": plan.voiceover.script if plan.voiceover else None,
    }
    out["slots"].pop("price", None)
    out["slots"].pop("old_price", None)
    out.update(changes)
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------- pure parts


def test_revision_prompt(talk_tpl: TemplateSpec, brand: BrandKit) -> None:
    plan = make_plan([("s1", "c1", 0.5, 3.0), ("s2", "c1", 4.0, 6.0)])
    plan = plan.model_copy(update={"slots": plan.slots.model_copy(update={"hook_text": "ሰላም!"})})
    clip = ClipAnalysis("c1", 8.0, 1080, 1920, Path("x"))
    p = build_revision_prompt(
        template=talk_tpl,
        brand=brand,
        context=BusinessContext(brief="የቡና ማስታወቂያ"),
        clips=[clip],
        lines=[],
        current=plan,
        instruction="አጠር አድርገው",
        has_voiceover=False,
        language="am",
    )
    assert "## Current edit" in p and "- s1: c1 0.50-3.00 speaker" in p
    assert "- slots.hook_text: ሰላም!" in p and "- look: natural" in p
    assert p.rstrip().endswith("never change them yourself.")
    assert "## Requested change (the business's own words)\nአጠር አድርገው" in p
    assert "Return only the JSON object." not in p


def test_carry_fixes_keeps_edited_caption_text() -> None:
    from adreel.ai.base import Transcript, TranscriptWord

    words = (TranscriptWord("ሰላም።", 0.5, 1.0), TranscriptWord("ቡና", 2.0, 2.5))
    lines = build_transcript_lines({"c1": Transcript("am", "", words, 5, "fake", "fake")})
    plan = make_plan(
        [("s1", "c1", 0, 5)],
        [
            {"id": 1, "text": "ሰላም ወዳጆች።", "clip_id": "c1", "start": 0.5, "end": 1.0},  # edited
            {"id": 2, "text": "ቡና", "clip_id": "c1", "start": 2.0, "end": 2.5},  # untouched
        ],
    )
    assert carry_fixes(lines, plan.captions) == {1: "ሰላም ወዳጆች።"}


# ---------------------------------------------------------------- engine (real ffmpeg)

pytestmark_ffmpeg = [pytest.mark.ffmpeg, requires_ffmpeg]


@pytest.fixture(scope="module")
def talk(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("revise_media") / "talk.mp4"
    FFmpeg().run(
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
            str(p),
        ]
    )
    return p


def _editor(tmp_path: Path, planner: object) -> AutoEditor:
    s = Settings(work_dir=tmp_path / "work", face_tracking=False, plan_attempts=2)
    ff = FFmpeg()
    return AutoEditor(
        s,
        Renderer(settings=s, face_tracker=None),
        transcriber=FakeTranscriber(speech_span_fn(ff)),
        planner=planner,  # type: ignore[arg-type]
        tts=lambda tier: FakeTTS(ff),
    )


def _req(talk: Path, out: Path, **kw: Any) -> AutoEditRequest:
    return AutoEditRequest(
        "talking_head",
        BrandKit(business_name="ሙከራ"),
        {"c1": talk},
        BusinessContext(brief="የካፌ መግቢያ"),
        out,
        **kw,
    )


@pytest.mark.ffmpeg
@requires_ffmpeg
def test_revise_changes_only_what_was_asked_and_keeps_caption_fixes(
    talk: Path, tmp_path: Path
) -> None:
    with _editor(tmp_path, None) as ed:  # first edit: heuristic planner
        first = ed.run(_req(talk, tmp_path / "v1.mp4"))
    current = first.plan.with_caption_text(1, "በእጅ የተስተካከለ")  # the business fixed a caption
    reply = as_reply(
        current, slots={"hook_text": "አዲስ መንጠቆ"}, look="vivid", message="ቪዲዮው ከዚህ በላይ ማጠር አይችልም"
    )
    planner = ScriptedPlanner(["{not json", reply])
    with _editor(tmp_path, planner) as ed:
        res = ed.revise(_req(talk, tmp_path / "v2.mp4"), current, "  ደማቅ አድርገው፣ መንጠቆውን ቀይር  ")
    plan = res.plan
    assert plan.slots.hook_text == "አዲስ መንጠቆ" and plan.look == "vivid"
    assert [s.id for s in plan.segments] == [s.id for s in current.segments]
    assert "በእጅ የተስተካከለ" in [c.text for c in plan.captions]  # the fix survived
    assert res.message == "ቪዲዮው ከዚህ በላይ ማጠር አይችልም" and res.attempts == 2
    assert [e.step for e in res.costs.entries] == ["plan", "plan"]  # transcript came from cache
    assert "ደማቅ አድርገው፣ መንጠቆውን ቀይር" in planner.prompts[0]
    assert res.render.output.is_file()


@pytest.mark.ffmpeg
@requires_ffmpeg
def test_revise_keeps_voiceover_and_takes_new_price_from_words(talk: Path, tmp_path: Path) -> None:
    with _editor(tmp_path, None) as ed:
        first = ed.run(_req(talk, tmp_path / "v1.mp4", voice=VoiceTier.STANDARD))
    current = first.plan
    assert current.voiceover is not None
    with _editor(tmp_path, ScriptedPlanner([as_reply(current)])) as ed:
        res = ed.revise(
            _req(talk, tmp_path / "v2.mp4", voice=VoiceTier.STANDARD), current, "ዋጋውን ወደ 400 ብር ቀይር"
        )
    assert res.plan.slots.price == 400.0  # from the business's words, not the model
    assert res.plan.voiceover == current.voiceover  # same script: recording reused
    assert "tts" not in [e.step for e in res.costs.entries]


@pytest.mark.ffmpeg
@requires_ffmpeg
def test_revise_failures(talk: Path, tmp_path: Path) -> None:
    with _editor(tmp_path, None) as ed:
        current = ed.run(_req(talk, tmp_path / "v1.mp4")).plan
        with pytest.raises(RevisionError) as ei:
            ed.revise(_req(talk, tmp_path / "v2.mp4"), current, "shorter")
        assert ei.value.reason == "needs_ai"
    with (
        _editor(tmp_path, ScriptedPlanner(["nope", "still nope"])) as ed,
        pytest.raises(RevisionError) as ei,
    ):
        ed.revise(_req(talk, tmp_path / "v3.mp4"), current, "shorter")
    assert ei.value.reason == "not_applied"
    with (
        _editor(tmp_path, ScriptedPlanner([ProviderError("gemini", "down")])) as ed,
        pytest.raises(RevisionError),
    ):
        ed.revise(_req(talk, tmp_path / "v4.mp4"), current, "shorter")


# ---------------------------------------------------------------- worker job


@pytest.mark.ffmpeg
@requires_ffmpeg
def test_worker_revise_job(talk: Path, tmp_path: Path) -> None:
    from adreel.db.migrate import upgrade
    from adreel.db.models import (
        BrandKitRow,
        Clip,
        Job,
        JobKind,
        JobStatus,
        Project,
        ProjectStatus,
        User,
    )
    from adreel.db.session import session_scope
    from adreel.i18n import t
    from adreel.jobs.tasks import JobRunner
    from tests.test_jobs import FakeTelegram

    s = Settings(
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
        storage_dir=tmp_path / "store",
        work_dir=tmp_path / "work",
        transcriber="fake",
        planner="heuristic",
        face_tracking=False,
        status_update_interval_s=0.5,
    )
    upgrade(s.database_url)
    with session_scope(s.database_url) as db:
        user = User(telegram_id=7, language="am")
        db.add(user)
        db.flush()
        db.add(BrandKitRow(user_id=user.id, business_name="ሙከራ"))
        project = Project(
            user_id=user.id,
            template_id="auto",
            status=ProjectStatus.QUEUED,
            language="am",
            chat_id=9,
            status_message_id=3,
            context={"brief": "የካፌ መግቢያ"},
        )
        db.add(project)
        db.flush()
        db.add(Clip(project_id=project.id, idx=1, tg_file_id="f1", tg_file_unique_id="u1"))
        job = Job(project_id=project.id, kind=JobKind.AUTO_EDIT)
        db.add(job)
        db.flush()
        pid, first_job = project.id, job.id
    tg = FakeTelegram({"f1": talk})
    JobRunner(s, telegram=tg, retries_left=lambda: 0).run(str(first_job))  # type: ignore[arg-type]
    with session_scope(s.database_url) as db:
        current = EditPlan.model_validate(db.get(Project, pid).plan)  # type: ignore[union-attr]

    def new_revise_job() -> str:
        with session_scope(s.database_url) as db:
            j = Job(project_id=pid, kind=JobKind.REVISE, params={"instruction": "መንጠቆውን ቀይር"})
            db.add(j)
            db.flush()
            return str(j.id)

    planner = ScriptedPlanner([as_reply(current, slots={"hook_text": "ተቀይሯል"}, message="ተሰርቷል")])

    def factory(renderer: Renderer) -> AutoEditor:
        return AutoEditor(
            s, renderer, transcriber=FakeTranscriber(speech_span_fn(renderer.ff)), planner=planner
        )  # type: ignore[arg-type]

    shutil.rmtree(s.work_dir)  # a different worker: clips come back from storage
    tg.calls.clear()
    job_id = new_revise_job()
    JobRunner(s, telegram=tg, editor_factory=factory, retries_left=lambda: 0).run(job_id)  # type: ignore[arg-type]
    with session_scope(s.database_url) as db:
        assert db.get(Job, __import__("uuid").UUID(job_id)).status == JobStatus.SUCCEEDED  # type: ignore[union-attr]
        project_row = db.get(Project, pid)
        assert project_row and project_row.status == ProjectStatus.PREVIEW_READY
        assert project_row.plan["slots"]["hook_text"] == "ተቀይሯል"  # type: ignore[index]
    video = next(kw for name, kw in tg.calls if name == "video")
    assert video["caption"].endswith("💬 ተሰርቷል")
    assert [
        b["callback_data"][:4] for row in video["reply_markup"]["inline_keyboard"] for b in row
    ] == ["pv:a", "pv:c", "pv:f", "pv:r"]

    # A change the planner can't make: clear message, the earlier preview stays usable
    planner.replies = ["bad"] * s.plan_attempts
    tg.calls.clear()
    job_id = new_revise_job()
    JobRunner(s, telegram=tg, editor_factory=factory, retries_left=lambda: 0).run(job_id)  # type: ignore[arg-type]
    with session_scope(s.database_url) as db:
        assert db.get(Job, __import__("uuid").UUID(job_id)).status == JobStatus.FAILED  # type: ignore[union-attr]
        assert db.get(Project, pid).status == ProjectStatus.PREVIEW_READY  # type: ignore[union-attr]
    assert tg.calls[-1][1]["text"] == t("revise_failed", "am")
