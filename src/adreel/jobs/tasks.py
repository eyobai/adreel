"""Worker side: run one project job end to end.

    load snapshot from DB -> fetch clips (Telegram once, then storage) -> AI edit or render
    -> upload outputs -> deliver in Telegram -> persist plan, costs, status

Transient failures (provider 5xx/429, network, Telegram hiccups) are re-raised while RQ has
retries left; everything else ends the job cleanly with a friendly message, and a failed
re-render keeps the earlier preview usable.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from adreel.ai.base import ProviderError
from adreel.autoedit.orchestrator import AutoEditor, AutoEditRequest, RevisionError
from adreel.autoedit.schema import BusinessContext
from adreel.config import Settings, get_settings
from adreel.db.models import (
    ApiCost,
    Clip,
    Job,
    JobKind,
    JobStatus,
    Project,
    ProjectStatus,
    utcnow,
)
from adreel.db.session import session_scope
from adreel.i18n import t
from adreel.jobs.telegram_client import TelegramClient, TelegramError
from adreel.log import configure_logging, get_logger
from adreel.render.ffmpeg import FFmpeg, FFmpegError
from adreel.render.pipeline import Quality, Renderer, RenderRequest
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan, VoiceTier
from adreel.storage import Storage, StorageError, build_storage, clip_key, output_key
from adreel.telegram_ui import preview_keyboard, status_text

log = get_logger(__name__)

STORAGE_PREFIX = "storage:"


class UserFacingError(RuntimeError):
    """A failure the user can fix (too long, bad file): message key + format args."""

    def __init__(self, key: str, **fmt: object) -> None:
        self.key, self.fmt = key, fmt
        super().__init__(key)


@dataclass
class Snapshot:
    """Everything the job needs, copied out of the DB so no session spans the render."""

    job_id: uuid.UUID
    kind: str
    project_id: uuid.UUID
    user_id: int
    chat_id: int
    status_message_id: int | None
    lang: str
    template_id: str
    context: dict[str, Any]
    voice: str | None
    watermark: bool
    plan: dict[str, Any] | None
    clips: list[tuple[int, str, str | None]]  # (idx, telegram file_id, storage_key)
    params: dict[str, Any]
    brand: dict[str, Any]
    logo_key: str | None


@dataclass
class Outcome:
    plan: dict[str, Any] | None = None
    preview_key: str | None = None
    final_key: str | None = None
    duration: float | None = None
    planner: str | None = None
    cost_usd: float = 0.0
    costs: list[dict[str, Any]] = field(default_factory=list)
    clip_keys: dict[int, str] = field(default_factory=dict)


class StatusUpdater:
    """Edits the user's status message, at most every ``interval`` seconds."""

    def __init__(
        self, tg: TelegramClient, chat_id: int, message_id: int | None, lang: str, interval: float
    ) -> None:
        self._tg, self._chat, self._msg, self._lang, self._interval = (
            tg,
            chat_id,
            message_id,
            lang,
            interval,
        )
        self._last = 0.0
        self._last_text = ""

    def __call__(self, stage: str, frac: float) -> None:
        if self._msg is None:
            return
        text = status_text(stage, frac, self._lang)
        now = time.monotonic()
        if text == self._last_text or now - self._last < self._interval:
            return
        self._last, self._last_text = now, text
        try:
            self._tg.edit_message_text(self._chat, self._msg, text)
        except TelegramError as exc:  # cosmetic: never fail a job over it
            log.warning("status.edit_failed", error=str(exc))

    def replace(self, text: str) -> None:
        if self._msg is None:
            self._tg.send_message(self._chat, text)
            return
        try:
            self._tg.edit_message_text(self._chat, self._msg, text)
        except TelegramError:
            self._tg.send_message(self._chat, text)

    def remove(self) -> None:
        if self._msg is not None:
            self._tg.delete_message(self._chat, self._msg)


def build_telegram(s: Settings) -> TelegramClient:
    token = s.telegram_bot_token.get_secret_value() if s.telegram_bot_token else ""
    return TelegramClient(
        token,
        api_base=s.telegram_api_base,
        local_mode=s.telegram_local_mode,
        local_files_dir=s.telegram_local_files_dir,
        server_files_dir=s.telegram_server_files_dir,
    )


# --------------------------------------------------------------------------- entry point


def run_project_job(job_id: str, settings: Settings | None = None) -> None:
    """RQ entry point (``adreel.jobs.tasks.run_project_job``)."""
    s = settings or get_settings()
    if settings is None:
        configure_logging(s.log_level, s.log_json)
    JobRunner(s).run(job_id)


class JobRunner:
    def __init__(
        self,
        s: Settings,
        *,
        telegram: TelegramClient | None = None,
        storage: Storage | None = None,
        editor_factory: Callable[[Renderer], AutoEditor] | None = None,
        retries_left: Callable[[], int] | None = None,
    ) -> None:
        self.s = s
        self.tg = telegram or build_telegram(s)
        self.storage = storage or build_storage(s)
        self.renderer = Renderer(settings=s)
        self.ff = self.renderer.ff
        self._editor_factory = editor_factory or (lambda r: AutoEditor(s, r))
        self._retries_left = retries_left or _rq_retries_left

    # ------------------------------------------------------------------ lifecycle

    def run(self, job_id: str) -> None:
        snap = self._start(uuid.UUID(job_id))
        if snap is None:
            return
        status = StatusUpdater(
            self.tg,
            snap.chat_id,
            snap.status_message_id,
            snap.lang,
            self.s.status_update_interval_s,
        )
        log.info("job.start", job=str(snap.job_id), kind=snap.kind, project=str(snap.project_id))
        try:
            outcome = self._execute(snap, status)
        except Exception as exc:
            self._fail(snap, exc, status)
            return
        finally:
            self.renderer.close()
        self._succeed(snap, outcome)
        log.info(
            "job.done", job=str(snap.job_id), kind=snap.kind, cost_usd=round(outcome.cost_usd, 4)
        )

    def _start(self, job_id: uuid.UUID) -> Snapshot | None:
        with session_scope(self.s.database_url) as db:
            job = db.get(Job, job_id)
            if job is None or job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                log.warning("job.skip", job=str(job_id), reason="missing or finished")
                return None
            project = job.project
            user = project.user
            brand = user.brand_kit
            if brand is None or project.chat_id is None:
                job.status, job.error = JobStatus.FAILED, "no brand kit or chat"
                return None
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.started_at = utcnow()
            project.status = (
                ProjectStatus.RENDERING_FINAL
                if job.kind == JobKind.FINAL
                else ProjectStatus.PROCESSING
            )
            return Snapshot(
                job_id=job.id,
                kind=job.kind,
                project_id=project.id,
                user_id=user.id,
                chat_id=project.chat_id,
                status_message_id=project.status_message_id,
                lang=project.language,
                template_id=project.template_id,
                context=dict(project.context or {}),
                voice=project.voice,
                watermark=project.watermark,
                plan=project.plan,
                clips=[(c.idx, c.tg_file_id, c.storage_key) for c in project.clips],
                brand={
                    "business_name": brand.business_name,
                    "business_type": brand.business_type,
                    "primary_color": brand.primary_color,
                    "secondary_color": brand.secondary_color,
                    "phone": brand.phone,
                    "telegram_handle": brand.telegram_handle,
                    "language": project.language,
                },
                logo_key=brand.logo_key,
                params=dict(job.params or {}),
            )

    def _succeed(self, snap: Snapshot, out: Outcome) -> None:
        with session_scope(self.s.database_url) as db:
            job = db.get(Job, snap.job_id)
            project = db.get(Project, snap.project_id)
            assert job is not None and project is not None
            job.status = JobStatus.SUCCEEDED
            job.finished_at = utcnow()
            job.error = None
            job.planner = out.planner or job.planner
            job.cost_usd = round(out.cost_usd, 6)
            job.video_seconds = out.duration
            for c in out.costs:
                db.add(ApiCost(job_id=job.id, **c))
            if out.plan is not None:
                project.plan = out.plan
            if out.preview_key:
                project.preview_key = out.preview_key
            if out.final_key:
                project.final_key = out.final_key
            if out.duration:
                project.duration_s = out.duration
            project.status = (
                ProjectStatus.DELIVERED
                if snap.kind == JobKind.FINAL
                else ProjectStatus.PREVIEW_READY
            )
            project.error = None
            project.status_message_id = None
            for clip in db.query(Clip).filter(Clip.project_id == project.id):
                if clip.idx in out.clip_keys:
                    clip.storage_key = out.clip_keys[clip.idx]

    def _fail(self, snap: Snapshot, exc: Exception, status: StatusUpdater) -> None:
        transient = _is_transient(exc)
        left = self._retries_left()
        log.error(
            "job.failed",
            job=str(snap.job_id),
            error=f"{type(exc).__name__}: {exc}",
            transient=transient,
            retries_left=left,
        )
        if transient and left > 0:
            with session_scope(self.s.database_url) as db:
                job = db.get(Job, snap.job_id)
                if job is not None:
                    job.status, job.error = JobStatus.QUEUED, f"retrying: {exc}"[:2000]
            status.replace(t("job_retrying", snap.lang))
            raise exc  # RQ schedules the retry
        has_preview = snap.plan is not None and snap.kind != JobKind.AUTO_EDIT
        if isinstance(exc, UserFacingError):
            message = t(exc.key, snap.lang, **exc.fmt)
        else:
            message = t("job_failed_retry_preview" if has_preview else "job_failed", snap.lang)
        with session_scope(self.s.database_url) as db:
            job = db.get(Job, snap.job_id)
            project = db.get(Project, snap.project_id)
            if job is not None:
                job.status, job.finished_at = JobStatus.FAILED, utcnow()
                job.error = f"{type(exc).__name__}: {exc}"[:4000]
            if project is not None:
                project.status = (
                    ProjectStatus.PREVIEW_READY if has_preview else ProjectStatus.FAILED
                )
                project.error = str(exc)[:2000]
                project.status_message_id = None
        try:
            status.replace(message)
        except TelegramError:
            log.warning("job.failure_notice_failed", job=str(snap.job_id))

    # ------------------------------------------------------------------ work

    def _workdir(self, snap: Snapshot) -> Path:
        d = self.s.work_dir.resolve() / "projects" / str(snap.project_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _fetch_clips(
        self, snap: Snapshot, work: Path, status: StatusUpdater, out: Outcome
    ) -> dict[str, Path]:
        clips: dict[str, Path] = {}
        total = 0.0
        for n, (idx, file_id, key) in enumerate(snap.clips, 1):
            local = work / "clips" / f"{idx}.mp4"
            if not local.is_file():
                status("download", 0.05 * n / max(1, len(snap.clips)))
                if key and self.storage.exists(key):
                    self.storage.get_file(key, local)
                else:
                    try:
                        self.tg.download_file(file_id, local)
                    except TelegramError as exc:
                        if exc.transient:
                            raise
                        raise UserFacingError("clip_download_failed", n=idx) from exc
                    new_key = clip_key(snap.user_id, snap.project_id, idx)
                    self.storage.put_file(local, new_key)
                    out.clip_keys[idx] = new_key
            info = self.ff.probe(local)
            if info.duration > self.s.max_clip_seconds:
                raise UserFacingError(
                    "clip_too_long", secs=round(info.duration), limit=self.s.max_clip_seconds
                )
            total += info.duration
            clips[f"c{idx}"] = local
        if total > self.s.max_total_seconds:
            raise UserFacingError(
                "clips_too_long_total", secs=round(total), limit=self.s.max_total_seconds
            )
        return clips

    def _brand(self, snap: Snapshot, work: Path) -> BrandKit:
        logo: Path | None = None
        if snap.logo_key:
            logo = work / ("logo" + Path(snap.logo_key).suffix)
            if not logo.is_file():
                try:
                    self.storage.get_file(snap.logo_key, logo)
                except StorageError:
                    log.warning("logo.missing", key=snap.logo_key)
                    logo = None
        return BrandKit.model_validate({**snap.brand, "logo_path": logo})

    def _resolve_voiceover(self, plan: EditPlan, work: Path) -> EditPlan:
        vo = plan.voiceover
        if vo is None or not vo.audio_path or not vo.audio_path.startswith(STORAGE_PREFIX):
            return plan
        key = vo.audio_path.removeprefix(STORAGE_PREFIX)
        local = work / ("voiceover" + Path(key).suffix)
        if not local.is_file():
            self.storage.get_file(key, local)
        return plan.model_copy(
            update={"voiceover": vo.model_copy(update={"audio_path": str(local)})}
        )

    def _execute(self, snap: Snapshot, status: StatusUpdater) -> Outcome:
        out = Outcome()
        work = self._workdir(snap)
        clips = self._fetch_clips(snap, work, status, out)
        brand = self._brand(snap, work)

        note: str | None = None
        if snap.kind in (JobKind.AUTO_EDIT, JobKind.REVISE):
            editor = self._editor_factory(self.renderer)
            request = AutoEditRequest(
                template_id=snap.template_id,
                brand=brand,
                clips=clips,
                context=BusinessContext.model_validate(snap.context),
                output=work / "preview.mp4",
                voice=VoiceTier(snap.voice) if snap.voice else None,
                quality=Quality.PREVIEW,
                watermark=snap.watermark,
                language=snap.lang,
            )

            def progress(stage: str, frac: float) -> None:
                status(stage, 0.05 + 0.9 * frac)

            if snap.kind == JobKind.REVISE:
                if snap.plan is None:
                    raise RuntimeError("project has no edit plan to revise")
                current = self._resolve_voiceover(EditPlan.model_validate(snap.plan), work)
                try:
                    res = editor.revise(
                        request, current, str(snap.params.get("instruction", "")), progress
                    )
                except RevisionError as exc:
                    raise UserFacingError("revise_failed") from exc
                note = res.message
            else:
                res = editor.run(request, progress)
            plan = res.plan
            if plan.voiceover and plan.voiceover.audio_path:
                src = Path(plan.voiceover.audio_path)
                key = output_key(snap.user_id, snap.project_id, "voiceover" + src.suffix)
                self.storage.put_file(src, key)
                plan = plan.model_copy(
                    update={
                        "voiceover": plan.voiceover.model_copy(
                            update={"audio_path": STORAGE_PREFIX + key}
                        )
                    }
                )
            out.plan = plan.model_dump(mode="json")
            out.planner = res.planner_used
            out.cost_usd = res.costs.total_usd
            out.costs = [
                {
                    "step": e.step,
                    "provider": e.provider,
                    "model": e.model,
                    "units": dict(e.units),
                    "usd": round(e.usd, 6),
                }
                for e in res.costs.entries
            ]
            video, duration, size = (
                res.render.output,
                res.render.duration,
                (res.render.width, res.render.height),
            )
            out.preview_key = output_key(snap.user_id, snap.project_id, "preview.mp4")
        else:
            if snap.plan is None:
                raise RuntimeError("project has no edit plan to render")
            plan = self._resolve_voiceover(EditPlan.model_validate(snap.plan), work)
            final = snap.kind == JobKind.FINAL
            result = self.renderer.render(
                RenderRequest(
                    plan=plan,
                    brand=brand,
                    clips=clips,
                    output=work / ("final.mp4" if final else "preview.mp4"),
                    quality=Quality.FINAL if final else Quality.PREVIEW,
                    watermark=snap.watermark,
                ),
                lambda stage, frac: status(f"render:{stage}", 0.05 + 0.9 * frac),
            )
            video, duration, size = result.output, result.duration, (result.width, result.height)
            name = "final.mp4" if final else "preview.mp4"
            key = output_key(snap.user_id, snap.project_id, name)
            if final:
                out.final_key = key
            else:
                out.preview_key = key

        out.duration = duration
        status("upload", 0.97)
        if snap.kind == JobKind.FINAL:
            if video.stat().st_size > self.tg.upload_limit * 0.97:
                video = fit_to_size(
                    self.ff,
                    video,
                    work / "final_small.mp4",
                    int(self.tg.upload_limit * 0.9),
                    duration,
                )
            self.storage.put_file(video, out.final_key or "")
            self.tg.send_document(
                snap.chat_id,
                video,
                caption=t("final_caption", snap.lang),
                filename=f"adreel_{str(snap.project_id)[:8]}.mp4",
            )
            status.remove()
        else:
            self.storage.put_file(video, out.preview_key or "")
            status.remove()
            self.tg.send_video(
                snap.chat_id,
                video,
                caption=t("preview_caption", snap.lang, secs=round(duration))
                + (f"\n\n💬 {note}" if note else ""),
                reply_markup=preview_keyboard(snap.project_id, snap.lang),
                width=size[0],
                height=size[1],
                duration=duration,
            )
        return out


def fit_to_size(ff: FFmpeg, src: Path, dst: Path, max_bytes: int, duration: float) -> Path:
    """Re-encode to fit Telegram's upload limit (only for long finals without a local Bot API)."""
    audio_kbps = 128
    total_kbps = max_bytes * 8 / 1000 / max(duration, 1.0) * 0.92
    video_kbps = max(300, int(total_kbps - audio_kbps))
    ff.run(
        [
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )
    return dst if dst.stat().st_size < src.stat().st_size else src


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    if isinstance(exc, TelegramError):
        return exc.transient
    if isinstance(exc, (httpx.TransportError, StorageError, ConnectionError)):
        return True
    if isinstance(exc, FFmpegError):
        return False
    return False


def _rq_retries_left() -> int:
    try:
        from rq import get_current_job

        job = get_current_job()
    except Exception:
        return 0
    return int(getattr(job, "retries_left", 0) or 0) if job is not None else 0
