"""Credit-pack purchases: create a Chapa checkout, then settle it exactly once.

Every confirmation path ends in :func:`settle`: the Chapa webhook, the callback, the
return link back into the bot (``/start paid_<tx_ref>``), the "I've paid" button and the
background sweep. ``settle`` asks Chapa's verify endpoint itself and credits under the
ledger key ``payment:<tx_ref>``, so however many of those fire, the user is credited once.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from adreel.billing import ledger
from adreel.billing.chapa import ChapaClient, Verdict, judge_verification
from adreel.config import CreditPack, Settings
from adreel.db.models import LedgerKind, Payment, PaymentStatus, User, utcnow
from adreel.log import get_logger

log = get_logger(__name__)

PENDING_TTL = timedelta(hours=24)  # Chapa checkouts are abandoned long before this


class PaymentsDisabled(Exception):
    pass


def find_pack(settings: Settings, pack_id: str) -> CreditPack | None:
    return next((p for p in settings.credit_packs if p.id == pack_id), None)


def new_tx_ref() -> str:
    return f"adr-{secrets.token_hex(12)}"  # Chapa: letters, digits, - and _ only


def make_client(settings: Settings) -> ChapaClient:
    key = settings.chapa_secret_key.get_secret_value().strip() if settings.chapa_secret_key else ""
    if not key:
        raise PaymentsDisabled("ADREEL_CHAPA_SECRET_KEY is not set")
    return ChapaClient(key, base_url=settings.chapa_base_url)


def public_url(settings: Settings, path: str) -> str | None:
    if not settings.public_base_url:
        return None
    return settings.public_base_url.rstrip("/") + path


async def create_checkout(
    db: AsyncSession,
    client: ChapaClient,
    settings: Settings,
    *,
    user: User,
    pack: CreditPack,
    bot_username: str | None,
) -> Payment:
    payment = Payment(
        user_id=user.id,
        tx_ref=new_tx_ref(),
        pack_id=pack.id,
        credits=pack.credits,
        amount_cents=pack.price_etb * 100,
        currency="ETB",
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    await db.flush()
    return_url = (
        f"https://t.me/{bot_username}?start=paid_{payment.tx_ref}" if bot_username else None
    )
    payment.checkout_url = await client.initialize(
        tx_ref=payment.tx_ref,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        title="AdReel credits",
        description=f"{pack.credits} video credits",
        return_url=return_url,
        callback_url=public_url(settings, "/chapa/callback"),
        first_name=user.first_name,
    )
    await db.flush()
    log.info("payment_created", tx_ref=payment.tx_ref, user_id=user.id, pack=pack.id)
    return payment


@dataclass(frozen=True)
class Settlement:
    payment: Payment
    credited: bool  # True only for the one call that actually added the credits
    balance: int | None = None


async def settle(db: AsyncSession, client: ChapaClient, tx_ref: str) -> Settlement | None:
    """Verify ``tx_ref`` with Chapa and apply the outcome. None if we don't know it."""
    payment = await db.scalar(select(Payment).where(Payment.tx_ref == tx_ref).with_for_update())
    if payment is None:
        return None
    if payment.status != PaymentStatus.PENDING:
        return Settlement(payment, credited=False)
    judgement = judge_verification(
        await client.verify(tx_ref),
        tx_ref=payment.tx_ref,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        test_mode=client.test_mode,
    )
    now = utcnow()
    payment.last_checked_at = now
    payment.provider_ref = judgement.provider_ref or payment.provider_ref
    credited = False
    if judgement.verdict is Verdict.PAID:
        credited = await ledger.add_entry(
            db,
            user_id=payment.user_id,
            delta=payment.credits,
            kind=LedgerKind.PURCHASE,
            key=f"payment:{payment.tx_ref}",
            payment_id=payment.id,
            note=f"pack {payment.pack_id}",
        )
        payment.status, payment.paid_at = PaymentStatus.PAID, now
    elif judgement.verdict is Verdict.MISMATCH:
        payment.status, payment.failure_reason = PaymentStatus.MISMATCH, judgement.reason[:300]
        log.warning("payment_mismatch", tx_ref=tx_ref, reason=judgement.reason)
    elif judgement.verdict is Verdict.FAILED:
        payment.status, payment.failure_reason = PaymentStatus.FAILED, judgement.reason[:300]
    elif _aware(payment.created_at) < now - PENDING_TTL:
        payment.status, payment.failure_reason = PaymentStatus.FAILED, "expired"
    await db.flush()
    log.info("payment_checked", tx_ref=tx_ref, verdict=judgement.verdict, credited=credited)
    new_balance = await ledger.balance(db, payment.user_id) if credited else None
    return Settlement(payment, credited=credited, balance=new_balance)


async def due_for_sweep(db: AsyncSession, interval_s: int, limit: int = 50) -> list[str]:
    """Pending payments not checked in the last ``interval_s`` seconds."""
    cutoff = utcnow() - timedelta(seconds=interval_s)
    rows = await db.scalars(
        select(Payment.tx_ref)
        .where(
            Payment.status == PaymentStatus.PENDING,
            or_(Payment.last_checked_at.is_(None), Payment.last_checked_at < cutoff),
        )
        .order_by(Payment.id)
        .limit(limit)
    )
    return list(rows)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive UTC
