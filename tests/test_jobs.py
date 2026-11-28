"""Worker jobs end to end: SQLite + local storage + fake Telegram + key-free AI providers."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from adreel.config import Settings
from adreel.db.migrate import upgrade
from adreel.db.models import (
    ApiCost,
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
from adreel.jobs.telegram_client import STANDARD_UPLOAD_LIMIT, TelegramError
from adreel.render.ffmpeg import FFmpeg
from adreel.schemas.edit_plan import EditPlan
from adreel.storage import LocalStorage
from adreel.telegram_ui import parse_preview_callback
from tests.conftest import requires_ffmpeg

pytestmark = [pytest.mark.ffmpeg, requires_ffmpeg]


class FakeTelegram:
    upload_limit = STANDARD_UPLOAD_LIMIT

    def __init__(self, files: dict[str, Path], fail_download: TelegramError | None = None) -> None:
        self.files = files
        self.fail_download = fail_download
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def download_file(self, file_id: str, dst: Path) -> Path:
        self.calls.append(("download", {"file_id": file_id}))
        if self.fail_download:
            raise self.fail_download
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.files[file_id], dst)
        return dst

    def send_message(self, chat_id: int, text: str, reply_markup: Any = None) -> dict[str, Any]:
        self.calls.append(("send_message", {"text": text}))
        return {"message_id": 99}

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> None:
        self.calls.append(("edit", {"text": text}))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("delete", {"message_id": message_id}))

    def send_video(self, chat_id: int, path: Path, **kw: Any) -> dict[str, Any]:
        assert path.is_file()
        self.calls.append(("video", kw))
        return {"message_id": 100}

    def send_document(self, chat_id: int, path: Path, **kw: Any) -> dict[str, Any]:
        assert path.is_file()
        self.calls.append(("document", kw))
        return {"message_id": 101}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture(scope="module")
def talk_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("jobmedia") / "talk.mp4"
    FFmpeg().run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=480x270:r=25:d=6",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='0.4*sin(2*PI*220*t)*lt(mod(t\\,2)\\,1.2)':s=48000:d=6",
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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
        storage_dir=tmp_path / "store",
        work_dir=tmp_path / "work",
        transcriber="fake",
        planner="heuristic",
        tts_standard="fake",
        face_tracking=False,
        status_update_interval_s=0.5,
    )
    upgrade(s.database_url)
    return s


def _seed(
    s: Settings, kind: JobKind = JobKind.AUTO_EDIT, voice: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    with session_scope(s.database_url) as db:
        user = User(telegram_id=1001, language="am")
        db.add(user)
        db.flush()
        db.add(
            BrandKitRow(
                user_id=user.id,
                business_name="ሙከራ ካፌ",
                primary_color="#1F7A4D",
                phone="+251911234567",
            )
        )
        project = Project(
            user_id=user.id,
            template_id="talking_head",
            status=ProjectStatus.QUEUED,
            language="am",
            chat_id=555,
            status_message_id=7,
            voice=voice,
            context={"highlight": "ትኩስ ቡና"},
        )
        db.add(project)
        db.flush()
        db.add(Clip(project_id=project.id, idx=1, tg_file_id="file-1", tg_file_unique_id="u1"))
        job = Job(project_id=project.id, kind=kind)
        db.add(job)
        db.flush()
        return project.id, job.id


def _new_job(s: Settings, project_id: uuid.UUID, kind: JobKind) -> uuid.UUID:
    with session_scope(s.database_url) as db:
        job = Job(project_id=project_id, kind=kind)
        db.add(job)
        db.flush()
        return job.id


def test_full_flow_preview_fix_final(settings: Settings, talk_clip: Path) -> None:
    tg = FakeTelegram({"file-1": talk_clip})
    project_id, job_id = _seed(settings, voice="standard")
    runner = JobRunner(settings, telegram=tg, retries_left=lambda: 0)  # type: ignore[arg-type]
    runner.run(str(job_id))

    with session_scope(settings.database_url) as db:
        job = db.get(Job, job_id)
        project = db.get(Project, project_id)
        assert job and project
        assert (
            job.status == JobStatus.SUCCEEDED and job.planner == "heuristic" and job.attempts == 1
        )
        assert (
            project.status == ProjectStatus.PREVIEW_READY and project.plan and project.preview_key
        )
        assert project.clips[0].storage_key  # downloaded once, kept in storage
        assert project.plan["voiceover"]["audio_path"].startswith("storage:")
        steps = {c.step for c in db.query(ApiCost).filter(ApiCost.job_id == job_id)}
        assert steps == {"transcribe", "tts"}
        plan = EditPlan.model_validate(project.plan)
    store = LocalStorage(settings.storage_dir)
    assert store.exists(f"users/1/projects/{project_id}/preview.mp4")
    assert tg.names().count("download") == 1 and "video" in tg.names() and "delete" in tg.names()
    video_kw = next(kw for n, kw in tg.calls if n == "video")
    cb = video_kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_preview_callback(cb)
    assert parsed and parsed.action == "approve" and parsed.project_id == project_id

    # Caption fix: re-render from the stored plan, no AI calls, no Telegram download
    assert plan.captions
    fixed = plan.with_caption_text(plan.captions[0].id, "የተስተካከለ ጽሑፍ")
    with session_scope(settings.database_url) as db:
        p = db.get(Project, project_id)
        assert p
        p.plan = fixed.model_dump(mode="json")
    shutil.rmtree(settings.work_dir)  # fresh worker: everything must come from storage
    tg.calls.clear()
    rerender = _new_job(settings, project_id, JobKind.RERENDER)
    JobRunner(settings, telegram=tg, retries_left=lambda: 0).run(str(rerender))  # type: ignore[arg-type]
    assert tg.names().count("download") == 0 and "video" in tg.names()
    with session_scope(settings.database_url) as db:
        assert db.get(Job, rerender).status == JobStatus.SUCCEEDED  # type: ignore[union-attr]
        assert db.query(ApiCost).filter(ApiCost.job_id == rerender).count() == 0

    # Approve: full quality, delivered as a document
    tg.calls.clear()
    final = _new_job(settings, project_id, JobKind.FINAL)
    JobRunner(settings, telegram=tg, retries_left=lambda: 0).run(str(final))  # type: ignore[arg-type]
    assert "document" in tg.names()
    with session_scope(settings.database_url) as db:
        project = db.get(Project, project_id)
        assert project and project.status == ProjectStatus.DELIVERED and project.final_key
    assert store.exists(f"users/1/projects/{project_id}/final.mp4")


def test_user_facing_failure(settings: Settings, talk_clip: Path) -> None:
    s = settings.model_copy(update={"max_clip_seconds": 2})
    tg = FakeTelegram({"file-1": talk_clip})
    project_id, job_id = _seed(s)
    JobRunner(s, telegram=tg, retries_left=lambda: 0).run(str(job_id))  # type: ignore[arg-type]
    with session_scope(s.database_url) as db:
        assert db.get(Job, job_id).status == JobStatus.FAILED  # type: ignore[union-attr]
        assert db.get(Project, project_id).status == ProjectStatus.FAILED  # type: ignore[union-attr]
    last = tg.calls[-1]
    assert last[0] == "edit" and "6" in last[1]["text"] and "2" in last[1]["text"]  # clip_too_long


def test_transient_failure_is_retried(settings: Settings, talk_clip: Path) -> None:
    tg = FakeTelegram(
        {"file-1": talk_clip}, fail_download=TelegramError("getFile", 502, "Bad Gateway")
    )
    _, job_id = _seed(settings)
    with pytest.raises(TelegramError):
        JobRunner(settings, telegram=tg, retries_left=lambda: 1).run(str(job_id))  # type: ignore[arg-type]
    with session_scope(settings.database_url) as db:
        job = db.get(Job, job_id)
        assert job and job.status == JobStatus.QUEUED and job.attempts == 1
    # last attempt: no retries left -> clean failure, no exception for RQ
    JobRunner(settings, telegram=tg, retries_left=lambda: 0).run(str(job_id))  # type: ignore[arg-type]
    with session_scope(settings.database_url) as db:
        job = db.get(Job, job_id)
        assert job and job.status == JobStatus.FAILED and job.attempts == 2


def test_finished_jobs_are_not_rerun(settings: Settings, talk_clip: Path) -> None:
    tg = FakeTelegram({"file-1": talk_clip})
    _, job_id = _seed(settings)
    with session_scope(settings.database_url) as db:
        db.get(Job, job_id).status = JobStatus.SUCCEEDED  # type: ignore[union-attr]
    JobRunner(settings, telegram=tg, retries_left=lambda: 0).run(str(job_id))  # type: ignore[arg-type]
    assert tg.calls == []


def test_failed_final_render_is_refunded_once(settings: Settings, talk_clip: Path) -> None:
    from adreel.billing import ledger
    from adreel.db.models import CreditLedger, LedgerKind

    tg = FakeTelegram({"file-1": talk_clip})
    project_id, job_id = _seed(settings, kind=JobKind.FINAL)  # no plan: the final render fails
    with session_scope(settings.database_url) as db:
        user_id = db.get(Project, project_id).user_id  # type: ignore[union-attr]
        ledger.add_entry_sync(db, user_id=user_id, delta=2, kind=LedgerKind.SIGNUP_BONUS, key="s")
        ledger.add_entry_sync(
            db, user_id=user_id, delta=-2, kind=LedgerKind.CHARGE, key=f"charge:{job_id}"
        )
    JobRunner(settings, telegram=tg, retries_left=lambda: 0).run(str(job_id))  # type: ignore[arg-type]
    with session_scope(settings.database_url) as db:
        assert db.get(Job, job_id).status == JobStatus.FAILED  # type: ignore[union-attr]
        assert ledger.balance_sync(db, user_id) == 2
        assert ledger.refund_job_sync(db, job_id) == 0  # a second refund is a no-op
        kinds = [r.kind for r in db.query(CreditLedger).order_by(CreditLedger.id)]
        assert kinds == ["signup_bonus", "charge", "refund"]
    assert t("refunded", "am", credits=2) in tg.calls[-1][1]["text"]
