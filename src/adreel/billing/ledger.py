"""Credit ledger: append-only, idempotent, balance = SUM(delta).

Rules from the product spec:
* new users get ``signup_bonus_credits`` free credits (their videos carry the watermark
  until they have bought credits once)
* 1 credit = a finished video up to ``credit_seconds`` (30 s); 60 s = 2; premium voice +1
* previews, caption fixes and the free re-edits cost nothing; the charge happens when the
  user approves the final video, and is refunded if that final render fails for good

Every movement has an idempotency key; inserting the same key twice is a no-op, so retried
webhooks, retried jobs and double taps can never double-credit or double-charge.
"""

from __future__ import annotations

import math
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from adreel.db.models import CreditLedger, LedgerKind, Payment, PaymentStatus, User


class InsufficientCredits(Exception):
    def __init__(self, needed: int, balance: int) -> None:
        self.needed, self.balance = needed, balance
        super().__init__(f"needs {needed} credits, balance {balance}")


def credits_for(duration_s: float | None, voice: str | None, *, credit_seconds: int = 30,
                premium_extra: int = 1) -> int:  # fmt: skip
    """30 s -> 1, 31-60 s -> 2, ...; premium voice adds ``premium_extra``."""
    seconds = max(0.0, duration_s or 0.0)
    base = max(1, math.ceil(seconds / credit_seconds - 1e-6))
    return base + (premium_extra if voice == "premium" else 0)


def _balance_stmt(user_id: int):  # type: ignore[no-untyped-def]
    return select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
        CreditLedger.user_id == user_id
    )


def _entry(user_id: int, delta: int, kind: LedgerKind, key: str, project_id: uuid.UUID | None,
           payment_id: int | None, note: str | None) -> CreditLedger:  # fmt: skip
    return CreditLedger(
        user_id=user_id,
        delta=delta,
        kind=kind.value,
        idempotency_key=key[:100],
        project_id=project_id,
        payment_id=payment_id,
        note=note[:200] if note else None,
    )


# --------------------------------------------------------------------------- async (bot)


async def balance(db: AsyncSession, user_id: int) -> int:
    return int(await db.scalar(_balance_stmt(user_id)) or 0)


async def add_entry(
    db: AsyncSession,
    *,
    user_id: int,
    delta: int,
    kind: LedgerKind,
    key: str,
    project_id: uuid.UUID | None = None,
    payment_id: int | None = None,
    note: str | None = None,
) -> bool:
    """Append a movement. False if this key was already applied (nothing changes)."""
    if await db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key[:100])):
        return False
    try:
        async with db.begin_nested():  # a duplicate from a concurrent request only undoes this
            db.add(_entry(user_id, delta, kind, key, project_id, payment_id, note))
            await db.flush()
    except IntegrityError:
        return False
    return True


async def charge(
    db: AsyncSession,
    *,
    user_id: int,
    amount: int,
    key: str,
    project_id: uuid.UUID | None,
    note: str,
) -> int:
    """Debit ``amount`` credits; raises InsufficientCredits. Returns the new balance."""
    # Serialise charges per user (row lock on Postgres; SQLite is single-writer anyway).
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    current = await balance(db, user_id)
    if current < amount:
        raise InsufficientCredits(amount, current)
    await add_entry(db, user_id=user_id, delta=-amount, kind=LedgerKind.CHARGE, key=key,
                    project_id=project_id, note=note)  # fmt: skip
    return current - amount


async def has_paid(db: AsyncSession, user_id: int) -> bool:
    """Free-tier users (never bought credits) get watermarked videos."""
    n = await db.scalar(
        select(func.count()).select_from(Payment)
        .where(Payment.user_id == user_id, Payment.status == PaymentStatus.PAID)
    )  # fmt: skip
    return bool(n)


async def grant_signup_bonus(db: AsyncSession, user_id: int, credits: int) -> bool:
    if credits <= 0:
        return False
    return await add_entry(db, user_id=user_id, delta=credits, kind=LedgerKind.SIGNUP_BONUS,
                           key=f"signup:{user_id}", note="welcome bonus")  # fmt: skip


# --------------------------------------------------------------------------- sync (worker)


def balance_sync(db: Session, user_id: int) -> int:
    return int(db.scalar(_balance_stmt(user_id)) or 0)


def add_entry_sync(
    db: Session,
    *,
    user_id: int,
    delta: int,
    kind: LedgerKind,
    key: str,
    project_id: uuid.UUID | None = None,
    payment_id: int | None = None,
    note: str | None = None,
) -> bool:
    if db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key[:100])):
        return False
    try:
        with db.begin_nested():
            db.add(_entry(user_id, delta, kind, key, project_id, payment_id, note))
            db.flush()
    except IntegrityError:
        return False
    return True


def refund_job_sync(db: Session, job_id: uuid.UUID) -> int:
    """Give back what was charged for ``job_id`` (once). Returns the credits refunded."""
    charged = db.scalar(
        select(CreditLedger).where(CreditLedger.idempotency_key == f"charge:{job_id}")
    )
    if charged is None or charged.delta >= 0:
        return 0
    applied = add_entry_sync(
        db,
        user_id=charged.user_id,
        delta=-charged.delta,
        kind=LedgerKind.REFUND,
        key=f"refund:{job_id}",
        project_id=charged.project_id,
        note="final render failed",
    )
    return -charged.delta if applied else 0
