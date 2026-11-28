"""Database schema (SQLAlchemy 2, typed declarative).

Portable on purpose: Postgres in prod, SQLite in tests. Statuses are plain strings checked
by Python enums (adding a status never needs an enum migration); JSON becomes JSONB on
Postgres. Credits and payments (Phase 4) get their own append-only ledger table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONType, list[Any]: JSONType}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    language: Mapped[str] = mapped_column(String(2), default="am")
    first_name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    brand_kit: Mapped[BrandKitRow | None] = relationship(back_populates="user", uselist=False)
    projects: Mapped[list[Project]] = relationship(back_populates="user")


class BrandKitRow(TimestampMixin, Base):
    __tablename__ = "brand_kits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    business_name: Mapped[str] = mapped_column(String(60))
    business_type: Mapped[str] = mapped_column(String(20), default="other")
    logo_key: Mapped[str | None] = mapped_column(String(255))
    primary_color: Mapped[str] = mapped_column(String(7), default="#E4572E")
    secondary_color: Mapped[str | None] = mapped_column(String(7))
    phone: Mapped[str | None] = mapped_column(String(16))
    telegram_handle: Mapped[str | None] = mapped_column(String(33))

    user: Mapped[User] = relationship(back_populates="brand_kit")


class ProjectStatus(StrEnum):
    DRAFT = "draft"  # collecting clips / answers
    QUEUED = "queued"
    PROCESSING = "processing"
    PREVIEW_READY = "preview_ready"
    RENDERING_FINAL = "rendering_final"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Project(TimestampMixin, Base):
    """One video the user is making (a /new session)."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.DRAFT, index=True)
    language: Mapped[str] = mapped_column(String(2), default="am")
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)  # BusinessContext answers
    voice: Mapped[str | None] = mapped_column(String(10))  # None | standard | premium
    watermark: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # latest EditPlan
    redo_count: Mapped[int] = mapped_column(Integer, default=0)
    fix_count: Mapped[int] = mapped_column(Integer, default=0)
    preview_key: Mapped[str | None] = mapped_column(String(255))
    final_key: Mapped[str | None] = mapped_column(String(255))
    duration_s: Mapped[float | None] = mapped_column(Float)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status_message_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="projects")
    clips: Mapped[list[Clip]] = relationship(
        back_populates="project", order_by="Clip.idx", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Clip(TimestampMixin, Base):
    __tablename__ = "clips"
    __table_args__ = (UniqueConstraint("project_id", "idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)  # 1-based; clip id in plans is f"c{idx}"
    tg_file_id: Mapped[str] = mapped_column(String(255))
    tg_file_unique_id: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    duration_s: Mapped[float | None] = mapped_column(Float)
    storage_key: Mapped[str | None] = mapped_column(String(255))  # set once downloaded

    project: Mapped[Project] = relationship(back_populates="clips")

    @property
    def clip_id(self) -> str:
        return f"c{self.idx}"


class JobKind(StrEnum):
    AUTO_EDIT = "auto_edit"  # full AI pipeline -> preview
    RERENDER = "rerender"  # stored plan (caption fixes) -> preview
    FINAL = "final"  # stored plan -> full quality, sent as a document


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED, index=True)
    rq_job_id: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    planner: Mapped[str | None] = mapped_column(String(20))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    video_seconds: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="jobs")
    costs: Mapped[list[ApiCost]] = relationship(back_populates="job", cascade="all, delete-orphan")


class ApiCost(Base):
    """One billable provider call (the per-video cost log, queryable for /stats)."""

    __tablename__ = "api_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    step: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(80))
    units: Mapped[dict[str, Any]] = mapped_column(default=dict)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="costs")
