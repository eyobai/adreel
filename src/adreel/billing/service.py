"""Payment confirmation service: settle + notify, the Chapa webhook server, the sweep.

Runs inside the bot process (it needs the Bot to message users). The web server is
optional (``ADREEL_PUBLIC_BASE_URL`` tells Chapa where it is); without it, payments are
still confirmed by the return link, the "I've paid" button and the periodic sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import weakref
from typing import Any

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adreel.billing import payments
from adreel.billing.chapa import ChapaClient, ChapaError, valid_webhook_signature
from adreel.billing.payments import Settlement
from adreel.config import Settings
from adreel.db.models import User
from adreel.i18n import t
from adreel.log import get_logger

log = get_logger(__name__)


class PaymentService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot | None,
        client: ChapaClient | None = None,
    ) -> None:
        self.s = settings
        self.session_factory = session_factory
        self.bot = bot
        if client is None:
            with contextlib.suppress(payments.PaymentsDisabled):  # no key: /buy says so
                client = payments.make_client(settings)
        self.client = client
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def settle_and_notify(self, tx_ref: str) -> Settlement | None:
        """Verify and apply ``tx_ref`` in its own transaction; message the user if credited.

        A per-tx_ref lock keeps the webhook, callback and button from verifying at the same
        moment (the ledger key would stop a double credit anyway; this avoids double
        messages). Transient Chapa errors propagate as ChapaError.
        """
        if self.client is None:
            return None
        lock = self._locks.get(tx_ref)
        if lock is None:
            lock = self._locks[tx_ref] = asyncio.Lock()
        async with lock, self.session_factory() as db:
            result = await payments.settle(db, self.client, tx_ref)
            if result is None:
                return None
            user = await db.get(User, result.payment.user_id)
            await db.commit()
        if result.credited and user is not None:
            await self._notify(
                user,
                t(
                    "payment_ok",
                    user.language,
                    credits=result.payment.credits,
                    balance=result.balance,
                ),
            )
        return result

    async def _notify(self, user: User, text: str) -> None:
        if self.bot is None:
            return
        try:
            await self.bot.send_message(user.telegram_id, text)
        except Exception as exc:  # never lose a credit over a failed message
            log.warning("payment_notify_failed", user_id=user.id, error=str(exc))

    async def sweep_once(self) -> int:
        async with self.session_factory() as db:
            due = await payments.due_for_sweep(db, self.s.payment_sweep_interval_s)
        settled = 0
        for tx_ref in due:
            try:
                res = await self.settle_and_notify(tx_ref)
            except ChapaError as exc:
                log.warning("payment_sweep_error", tx_ref=tx_ref, error=str(exc))
                continue
            settled += bool(res and res.credited)
        return settled

    async def sweep_forever(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:  # keep sweeping whatever happens
                log.exception("payment_sweep_crashed")
            await asyncio.sleep(self.s.payment_sweep_interval_s)


# --------------------------------------------------------------------------- web server

SERVICE = web.AppKey("service", PaymentService)


def build_web_app(service: PaymentService) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app[SERVICE] = service
    app.router.add_post("/chapa/webhook", _webhook)
    app.router.add_get("/chapa/callback", _callback)
    app.router.add_get("/health", _health)
    return app


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _webhook(request: web.Request) -> web.Response:
    service = request.app[SERVICE]
    secret = service.s.chapa_webhook_secret
    body = await request.read()
    if secret is None or not valid_webhook_signature(
        body, request.headers, secret.get_secret_value()
    ):
        log.warning("chapa_webhook_bad_signature")
        return web.json_response({"ok": False}, status=401)
    try:
        event: Any = json.loads(body or b"{}")
    except ValueError:
        return web.json_response({"ok": False}, status=400)
    tx_ref = (event.get("tx_ref") or event.get("trx_ref")) if isinstance(event, dict) else None
    if not tx_ref:
        return web.json_response({"ok": True, "ignored": True})
    # The event is only a hint: settle() asks Chapa's verify endpoint for the truth.
    return await _settle_response(service, str(tx_ref))


async def _callback(request: web.Request) -> web.Response:
    """Chapa's callback_url GET (``?trx_ref=...&status=...``): only the reference is used."""
    service = request.app[SERVICE]
    tx_ref = request.query.get("trx_ref") or request.query.get("tx_ref")
    if not tx_ref:
        return web.json_response({"ok": False}, status=400)
    return await _settle_response(service, tx_ref)


async def _settle_response(service: PaymentService, tx_ref: str) -> web.Response:
    try:
        res = await service.settle_and_notify(tx_ref)
    except ChapaError as exc:
        log.warning("chapa_settle_error", tx_ref=tx_ref, error=str(exc))
        return web.json_response({"ok": False}, status=503)  # Chapa retries the webhook
    if res is None:
        return web.json_response({"ok": True, "unknown": True})
    return web.json_response({"ok": True, "status": res.payment.status})


async def start_web(service: PaymentService) -> web.AppRunner:
    runner = web.AppRunner(build_web_app(service), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, service.s.http_host, service.s.http_port).start()
    log.info("web.start", host=service.s.http_host, port=service.s.http_port)
    return runner


@contextlib.asynccontextmanager
async def running(service: PaymentService):  # type: ignore[no-untyped-def]
    """Web server + sweep for the lifetime of the bot (no-ops when payments are off)."""
    runner = await start_web(service) if service.enabled else None
    sweep = asyncio.create_task(service.sweep_forever()) if service.enabled else None
    try:
        yield service
    finally:
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
        if runner is not None:
            await runner.cleanup()
        await service.aclose()
