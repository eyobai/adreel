"""Credits in the bot: /balance, /buy, payment confirmation, admin /stats and /grant.

Charging itself happens in the preview flow (handlers.on_preview_action) when the user
approves; this module only shows and sells credits.
"""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adreel.billing import ledger, payments, stats
from adreel.billing.chapa import ChapaError
from adreel.billing.service import PaymentService
from adreel.config import Settings
from adreel.db.models import LedgerKind, PaymentStatus, User
from adreel.i18n import t
from adreel.log import get_logger

log = get_logger(__name__)
router = Router(name="adreel-billing")


def buy_button(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("btn_buy", lang), callback_data="buy")]]
    )


def packs_kb(settings: Settings, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("pack_label", lang, credits=p.credits, price=f"{p.price_etb:,}"),
                    callback_data=f"buy:{p.id}",
                )
            ]
            for p in settings.credit_packs
        ]
    )


def checkout_kb(url: str, tx_ref: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_pay", lang), url=url)],
            [InlineKeyboardButton(text=t("btn_paid_check", lang), callback_data=f"paid:{tx_ref}")],
        ]
    )


def is_admin(settings: Settings, user: User) -> bool:
    return user.telegram_id in settings.admin_telegram_ids


async def balance_text(db: AsyncSession, settings: Settings, user: User, lang: str) -> str:
    bal = await ledger.balance(db, user.id)
    free_tier = not await ledger.has_paid(db, user.id)
    return t(
        "balance",
        lang,
        balance=bal,
        secs=settings.credit_seconds,
        premium=settings.premium_voice_credits,
        redos=settings.max_redos,
        wm=t("balance_free_tier", lang) if free_tier else "",
    )


# --------------------------------------------------------------------------- user


@router.message(Command("balance"))
async def cmd_balance(
    message: Message, db: AsyncSession, user: User, lang: str, settings: Settings
) -> None:
    await message.answer(
        await balance_text(db, settings, user, lang), reply_markup=buy_button(lang)
    )


@router.message(Command("buy"))
async def cmd_buy(
    message: Message, lang: str, settings: Settings, payment_service: PaymentService
) -> None:
    if not payment_service.enabled:
        await message.answer(t("payments_off", lang))
        return
    await message.answer(t("buy_choose", lang), reply_markup=packs_kb(settings, lang))


@router.callback_query(F.data == "buy")
async def on_buy_button(
    cb: CallbackQuery, lang: str, settings: Settings, payment_service: PaymentService
) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        if not payment_service.enabled:
            await cb.message.answer(t("payments_off", lang))
            return
        await cb.message.answer(t("buy_choose", lang), reply_markup=packs_kb(settings, lang))


@router.callback_query(F.data.startswith("buy:"))
async def on_pack(
    cb: CallbackQuery,
    bot: Bot,
    db: AsyncSession,
    user: User,
    lang: str,
    settings: Settings,
    payment_service: PaymentService,
) -> None:
    pack = payments.find_pack(settings, (cb.data or "").split(":", 1)[1])
    if pack is None or payment_service.client is None or not isinstance(cb.message, Message):
        await cb.answer(t("payments_off", lang), show_alert=True)
        return
    await cb.answer()
    me = await bot.me()
    try:
        payment = await payments.create_checkout(
            db, payment_service.client, settings, user=user, pack=pack, bot_username=me.username
        )
    except ChapaError as exc:
        log.warning("checkout_failed", user_id=user.id, error=str(exc))
        await db.rollback()
        await cb.message.answer(t("payment_init_failed", lang))
        return
    assert payment.checkout_url
    await cb.message.answer(
        t("buy_link", lang, credits=pack.credits, price=f"{pack.price_etb:,}"),
        reply_markup=checkout_kb(payment.checkout_url, payment.tx_ref, lang),
    )


async def _report(
    target: Message, db: AsyncSession, user: User, lang: str, service: PaymentService, tx_ref: str
) -> None:
    """Check one payment now and tell the user where it stands."""
    try:
        res = await service.settle_and_notify(tx_ref)
    except ChapaError:
        await target.answer(t("payment_pending", lang))
        return
    if res is None or res.payment.user_id != user.id:
        await target.answer(t("payment_unknown", lang))
        return
    if res.credited:
        return  # settle_and_notify already sent the "payment received" message
    status = res.payment.status
    if status == PaymentStatus.PAID:
        await target.answer(t("payment_already", lang, balance=await ledger.balance(db, user.id)))
    elif status == PaymentStatus.PENDING:
        await target.answer(t("payment_pending", lang))
    elif status == PaymentStatus.MISMATCH:
        await target.answer(t("payment_mismatch", lang, tx_ref=tx_ref))
    else:
        await target.answer(t("payment_failed", lang))


@router.callback_query(F.data.startswith("paid:"))
async def on_paid_check(
    cb: CallbackQuery, db: AsyncSession, user: User, lang: str, payment_service: PaymentService
) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        tx_ref = (cb.data or "").split(":", 1)[1]
        await _report(cb.message, db, user, lang, payment_service, tx_ref)


@router.message(CommandStart(deep_link=True, magic=F.args.startswith("paid_")))
async def on_paid_return(
    message: Message,
    command: CommandObject,
    db: AsyncSession,
    user: User,
    lang: str,
    payment_service: PaymentService,
) -> None:
    """Chapa's return_url brings the user back with /start paid_<tx_ref>."""
    tx_ref = (command.args or "").removeprefix("paid_")
    await _report(message, db, user, lang, payment_service, tx_ref)


# --------------------------------------------------------------------------- admin


@router.message(Command("stats"))
async def cmd_stats(
    message: Message, db: AsyncSession, user: User, lang: str, settings: Settings
) -> None:
    if not is_admin(settings, user):
        await message.answer(t("unknown", lang))
        return
    await message.answer(stats.render(await stats.collect(db), settings.usd_etb_rate))


@router.message(Command("grant"))
async def cmd_grant(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: AsyncSession,
    user: User,
    lang: str,
    settings: Settings,
) -> None:
    """/grant <telegram_id> <credits> [note] — support credits (negative to take back)."""
    if not is_admin(settings, user):
        await message.answer(t("unknown", lang))
        return
    parts = (command.args or "").split(maxsplit=2)
    try:
        tg_id, delta = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        await message.answer(t("grant_usage", lang))
        return
    target = await db.scalar(select(User).where(User.telegram_id == tg_id))
    if target is None or delta == 0:
        await message.answer(
            t("user_not_found", lang) if target is None else t("grant_usage", lang)
        )
        return
    note = parts[2] if len(parts) > 2 else f"by admin {user.telegram_id}"
    await ledger.add_entry(
        db,
        user_id=target.id,
        delta=delta,
        kind=LedgerKind.ADMIN,
        key=f"admin:{uuid.uuid4()}",
        note=note,
    )
    bal = await ledger.balance(db, target.id)
    await db.commit()
    log.info("credits_granted", admin=user.telegram_id, target=tg_id, delta=delta)
    await message.answer(t("grant_done", lang, user=tg_id, delta=delta, balance=bal))
    if delta > 0:
        try:
            await bot.send_message(
                tg_id, t("grant_received", target.language, credits=delta, balance=bal)
            )
        except Exception as exc:
            log.warning("grant_notify_failed", target=tg_id, error=str(exc))


def build_billing_router() -> Router:
    fresh = Router(name="adreel-billing")
    for name, observer in router.observers.items():
        fresh.observers[name].handlers.extend(observer.handlers)
    return fresh
