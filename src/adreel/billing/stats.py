"""Numbers for the admin ``/stats`` command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adreel.db.models import (
    CreditLedger,
    Job,
    JobStatus,
    LedgerKind,
    Payment,
    PaymentStatus,
    Project,
    ProjectStatus,
    User,
    utcnow,
)


@dataclass(frozen=True)
class Stats:
    users: int
    users_7d: int
    paying_users: int
    jobs_by_status: dict[str, int]
    jobs_24h: int
    failed_24h: int
    delivered: int
    revenue_etb: float
    revenue_7d_etb: float
    payments_pending: int
    payments_mismatch: int
    credits_sold: int
    credits_used: int
    api_cost_usd: float
    avg_cost_per_video_usd: float | None
    queue_now: int

    @property
    def failure_rate(self) -> float:
        done = self.jobs_by_status.get(JobStatus.SUCCEEDED, 0) + self.jobs_by_status.get(
            JobStatus.FAILED, 0
        )
        return self.jobs_by_status.get(JobStatus.FAILED, 0) / done if done else 0.0


async def collect(db: AsyncSession) -> Stats:
    now = utcnow()
    week, day = now - timedelta(days=7), now - timedelta(days=1)

    async def count(stmt) -> int:  # type: ignore[no-untyped-def]
        return int(await db.scalar(stmt) or 0)

    by_status = dict(
        (await db.execute(select(Job.status, func.count()).group_by(Job.status))).all()
    )
    paid = select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
        Payment.status == PaymentStatus.PAID
    )
    ledger_sum = select(func.coalesce(func.sum(CreditLedger.delta), 0))
    delivered = await count(
        select(func.count()).select_from(Project).where(Project.status == ProjectStatus.DELIVERED)
    )
    # Every job of a delivered video counts toward its cost: previews, redos, changes, final.
    delivered_cost = float(
        await db.scalar(
            select(func.coalesce(func.sum(Job.cost_usd), 0.0))
            .join(Project, Project.id == Job.project_id)
            .where(Project.status == ProjectStatus.DELIVERED)
        )
        or 0.0
    )
    charges = await count(ledger_sum.where(CreditLedger.kind == LedgerKind.CHARGE))
    refunds = await count(ledger_sum.where(CreditLedger.kind == LedgerKind.REFUND))
    return Stats(
        users=await count(select(func.count()).select_from(User)),
        users_7d=await count(select(func.count()).select_from(User).where(User.created_at >= week)),
        paying_users=await count(
            select(func.count(func.distinct(Payment.user_id))).where(
                Payment.status == PaymentStatus.PAID
            )
        ),
        jobs_by_status={str(k): int(v) for k, v in by_status.items()},
        jobs_24h=await count(select(func.count()).select_from(Job).where(Job.created_at >= day)),
        failed_24h=await count(
            select(func.count())
            .select_from(Job)
            .where(Job.created_at >= day, Job.status == JobStatus.FAILED)
        ),
        delivered=delivered,
        revenue_etb=await count(paid) / 100,
        revenue_7d_etb=await count(paid.where(Payment.paid_at >= week)) / 100,
        payments_pending=await count(
            select(func.count()).select_from(Payment).where(Payment.status == PaymentStatus.PENDING)
        ),
        payments_mismatch=await count(
            select(func.count())
            .select_from(Payment)
            .where(Payment.status == PaymentStatus.MISMATCH)
        ),
        credits_sold=await count(ledger_sum.where(CreditLedger.kind == LedgerKind.PURCHASE)),
        credits_used=-(charges + refunds),
        api_cost_usd=float(
            await db.scalar(select(func.coalesce(func.sum(Job.cost_usd), 0.0))) or 0
        ),
        avg_cost_per_video_usd=delivered_cost / delivered if delivered else None,
        queue_now=await count(
            select(func.count())
            .select_from(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        ),
    )


def render(st: Stats, usd_etb: float) -> str:
    avg = (
        f"${st.avg_cost_per_video_usd:.3f} (≈{st.avg_cost_per_video_usd * usd_etb:.1f} ETB)"
        if st.avg_cost_per_video_usd is not None
        else "—"
    )
    jobs = ", ".join(f"{k} {v}" for k, v in sorted(st.jobs_by_status.items())) or "none"
    return "\n".join(
        [
            "📊 AdReel stats",
            f"Users: {st.users} (+{st.users_7d} in 7 d), paying {st.paying_users}",
            f"Jobs: {jobs}",
            f"Failure rate: {st.failure_rate:.1%}"
            f" · last 24 h: {st.failed_24h}/{st.jobs_24h} failed",
            f"Queue now: {st.queue_now}",
            f"Videos delivered: {st.delivered}",
            f"Revenue: {st.revenue_etb:,.0f} ETB (7 d: {st.revenue_7d_etb:,.0f})",
            f"Payments pending {st.payments_pending}, mismatched {st.payments_mismatch}",
            f"Credits sold {st.credits_sold}, used {st.credits_used}",
            f"API cost: ${st.api_cost_usd:.2f} · avg per delivered video {avg}",
        ]
    )
