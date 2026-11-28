"""Async data access for the bot (thin, explicit queries; the worker has its own)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from adreel.db.models import (
    BrandKitRow,
    Clip,
    Job,
    JobKind,
    JobStatus,
    Project,
    ProjectStatus,
    User,
    utcnow,
)
from adreel.i18n import normalize_lang, t

ACTIVE_JOB = (JobStatus.QUEUED, JobStatus.RUNNING)


async def get_or_create_user(
    db: AsyncSession,
    telegram_id: int,
    *,
    first_name: str | None,
    username: str | None,
    language_code: str | None,
) -> tuple[User, bool]:
    user = await db.scalar(
        select(User).where(User.telegram_id == telegram_id).options(selectinload(User.brand_kit))
    )
    if user is not None:
        return user, False
    user = User(
        telegram_id=telegram_id,
        first_name=first_name,
        username=username,
        language=normalize_lang(language_code if language_code == "en" else "am"),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user, ["brand_kit"])
    return user, True


async def save_brand(db: AsyncSession, user: User, **fields: object) -> BrandKitRow:
    brand = await db.scalar(select(BrandKitRow).where(BrandKitRow.user_id == user.id))
    if brand is None:
        brand = BrandKitRow(user_id=user.id, business_name=str(fields.get("business_name") or "—"))
        db.add(brand)
    for key, value in fields.items():
        setattr(brand, key, value)
    await db.flush()
    return brand


def brand_summary(brand: BrandKitRow, lang: str) -> str:
    missing = t("not_set", lang)
    return "\n".join(
        [
            f"🏷 {brand.business_name}",
            f"🗂 {t('bt_' + brand.business_type, lang)}",
            f"🖼 {t('yes', lang) if brand.logo_key else missing}",
            f"🎨 {brand.primary_color}",
            f"📞 {brand.phone or missing}",
            f"✈️ {brand.telegram_handle or missing}",
        ]
    )


async def latest_draft(db: AsyncSession, user_id: int) -> Project | None:
    return await db.scalar(
        select(Project)
        .where(Project.user_id == user_id, Project.status == ProjectStatus.DRAFT)
        .order_by(Project.created_at.desc())
        .limit(1)
        .options(selectinload(Project.clips))
    )


async def create_project(db: AsyncSession, user: User, template_id: str, chat_id: int) -> Project:
    drafts = await db.scalars(
        select(Project).where(Project.user_id == user.id, Project.status == ProjectStatus.DRAFT)
    )
    for d in drafts:
        d.status = ProjectStatus.CANCELLED
    project = Project(
        user_id=user.id, template_id=template_id, language=user.language, chat_id=chat_id
    )
    db.add(project)
    await db.flush()
    return project


async def get_project(
    db: AsyncSession, project_id: uuid.UUID | str, user_id: int
) -> Project | None:
    pid = project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id))
    return await db.scalar(
        select(Project)
        .where(Project.id == pid, Project.user_id == user_id)
        .options(selectinload(Project.clips))
    )


async def clip_count(db: AsyncSession, project_id: uuid.UUID) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(Clip).where(Clip.project_id == project_id))
        or 0
    )


async def add_clip(
    db: AsyncSession,
    project: Project,
    *,
    file_id: str,
    unique_id: str,
    size: int | None,
    duration: float | None,
) -> tuple[Clip | None, int]:
    """Returns (clip, total). clip is None for a duplicate (same file sent twice)."""
    existing = await db.scalar(
        select(Clip).where(Clip.project_id == project.id, Clip.tg_file_unique_id == unique_id)
    )
    count = await clip_count(db, project.id)
    if existing is not None:
        return None, count
    clip = Clip(
        project_id=project.id,
        idx=count + 1,
        tg_file_id=file_id,
        tg_file_unique_id=unique_id,
        file_size=size,
        duration_s=duration,
    )
    db.add(clip)
    await db.flush()
    return clip, count + 1


async def has_active_job(db: AsyncSession, user_id: int) -> bool:
    n = await db.scalar(
        select(func.count())
        .select_from(Job)
        .join(Project)
        .where(Project.user_id == user_id, Job.status.in_(ACTIVE_JOB))
    )
    return bool(n)


async def videos_last_hour(db: AsyncSession, user_id: int) -> int:
    since = utcnow() - timedelta(hours=1)
    n = await db.scalar(
        select(func.count())
        .select_from(Job)
        .join(Project)
        .where(Project.user_id == user_id, Job.kind == JobKind.AUTO_EDIT, Job.created_at >= since)
    )
    return int(n or 0)


async def create_job(
    db: AsyncSession, project: Project, kind: JobKind, status_message_id: int | None
) -> Job:
    job = Job(project_id=project.id, kind=kind)
    db.add(job)
    project.status = (
        ProjectStatus.QUEUED if kind != JobKind.FINAL else ProjectStatus.RENDERING_FINAL
    )
    project.status_message_id = status_message_id
    await db.flush()
    return job
