"""Phase 4: credit ledger, Chapa rules/client/webhook, purchases and charging in the bot."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from sqlalchemy import select

from adreel.billing import ledger, payments, stats
from adreel.billing.chapa import ChapaClient, ChapaError, Verdict, judge_verification
from adreel.billing.chapa import valid_webhook_signature as valid_sig
from adreel.billing.service import PaymentService, build_web_app
from adreel.bot.app import build_dispatcher
from adreel.config import Settings
from adreel.db.migrate import upgrade
from adreel.db.models import (
    CreditLedger,
    Job,
    JobKind,
    JobStatus,
    LedgerKind,
    Payment,
    PaymentStatus,
    Project,
    ProjectStatus,
    User,
    utcnow,
)
from adreel.db.session import async_session_factory, make_async_engine, session_scope
from adreel.i18n import t
from adreel.storage import LocalStorage
from tests.test_bot import CHAT, UID, FakeQueue, Harness, MockSession, _onboard, _ready_project

WEBHOOK_SECRET = "whsec-test"


# =========================================================================== pricing


@pytest.mark.parametrize(
    ("secs", "voice", "credits"),
    [
        (None, None, 1),
        (0, None, 1),
        (12, None, 1),
        (30, None, 1),
        (30.4, None, 2),
        (45, "standard", 2),
        (60, None, 2),
        (61, None, 3),
        (20, "premium", 2),
        (60, "premium", 3),
    ],
)
def test_credits_for(secs: float | None, voice: str | None, credits: int) -> None:
    assert ledger.credits_for(secs, voice) == credits


# =========================================================================== Chapa rules


def _verify(**data: Any) -> dict[str, Any]:
    base = {"status": "success", "tx_ref": "adr-1", "amount": "600.00", "currency": "ETB"}
    base |= {"mode": "test", "reference": "APabc"}
    return {"status": "success", "data": base | data}


def _judge(payload: dict[str, Any], test_mode: bool = True) -> Verdict:
    return judge_verification(
        payload, tx_ref="adr-1", amount_cents=60000, currency="ETB", test_mode=test_mode
    ).verdict


def test_verification_rules() -> None:
    assert _judge(_verify()) is Verdict.PAID
    assert _judge(_verify(amount=600)) is Verdict.PAID
    assert _judge(_verify(amount="599.99")) is Verdict.MISMATCH
    assert _judge(_verify(currency="USD")) is Verdict.MISMATCH
    assert _judge(_verify(tx_ref="adr-2")) is Verdict.MISMATCH
    assert _judge(_verify(mode="live")) is Verdict.MISMATCH  # test key, live payment
    assert _judge(_verify(mode="live"), test_mode=False) is Verdict.PAID
    assert _judge(_verify(mode=None)) is Verdict.MISMATCH
    assert _judge(_verify(status="pending")) is Verdict.PENDING
    assert _judge(_verify(status="failed")) is Verdict.FAILED
    assert _judge({"status": "failed", "message": "not found", "data": None}) is Verdict.PENDING
    mismatch = {"message": "Transaction mode mismatch", "data": None, "mode_mismatch": True}
    assert _judge(mismatch) is Verdict.MISMATCH
    paid = judge_verification(
        _verify(), tx_ref="adr-1", amount_cents=60000, currency="ETB", test_mode=True
    )
    assert paid.provider_ref == "APabc"


def test_webhook_signature() -> None:
    body = b'{"tx_ref":"adr-1"}'
    body_sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    key = WEBHOOK_SECRET.encode()
    secret_sig = hmac.new(key, key, hashlib.sha256).hexdigest()
    assert valid_sig(body, {"x-chapa-signature": body_sig}, WEBHOOK_SECRET)
    assert valid_sig(body, {"chapa-signature": secret_sig}, WEBHOOK_SECRET)
    assert valid_sig(body, {"x-chapa-signature": body_sig.upper()}, WEBHOOK_SECRET)
    assert not valid_sig(body + b" ", {"x-chapa-signature": body_sig}, WEBHOOK_SECRET)
    assert not valid_sig(body, {"x-chapa-signature": body_sig}, "other")
    assert not valid_sig(body, {}, WEBHOOK_SECRET)
    assert not valid_sig(body, {"x-chapa-signature": body_sig}, "")


# =========================================================================== Chapa client


@pytest.mark.asyncio
async def test_chapa_client_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path == "/v1/transaction/initialize":
            return httpx.Response(200, json={"data": {"checkout_url": "https://checkout/x"}})
        ref = path.rsplit("/", 1)[1]
        if ref == "ok":
            return httpx.Response(200, json=_verify(tx_ref="ok"))
        if ref == "missing":
            return httpx.Response(404, json={"message": "Invalid transaction or not found"})
        if ref == "othermode":
            return httpx.Response(401, json={"message": "Transaction mode mismatch"})
        if ref == "badkey":
            return httpx.Response(401, json={"message": "Invalid API Key"})
        return httpx.Response(502, text="bad gateway")

    client = ChapaClient("CHASECK_TEST-abc", transport=httpx.MockTransport(handler))
    assert client.test_mode
    url = await client.initialize(
        tx_ref="adr-1",
        amount_cents=60000,
        currency="ETB",
        title="AdReel credits for you",
        description="5 video credits",
        return_url="https://t.me/bot?start=paid_adr-1",
        callback_url="https://x/chapa/callback",
    )
    assert url == "https://checkout/x"
    body = json.loads(seen[0].content)
    assert body["amount"] == "600.00" and body["tx_ref"] == "adr-1"
    assert body["customization"]["title"] == "AdReel credits f"  # Chapa's 16-char limit
    assert seen[0].headers["authorization"] == "Bearer CHASECK_TEST-abc"
    assert (await client.verify("ok"))["data"]["status"] == "success"
    assert (await client.verify("missing"))["data"] is None
    assert (await client.verify("othermode"))["mode_mismatch"] is True
    with pytest.raises(ChapaError):
        await client.verify("badkey")
    with pytest.raises(ChapaError):
        await client.verify("down")
    await client.aclose()


# =========================================================================== fakes / fixtures


class FakeChapa:
    """Stands in for ChapaClient: remembers checkouts, answers verify from ``state``."""

    test_mode = True

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}  # tx_ref -> verify payload
        self.inits: list[dict[str, Any]] = []
        self.verifies = 0
        self.fail_init = False

    async def initialize(self, **kw: Any) -> str:
        if self.fail_init:
            raise ChapaError("initialize 400: bad")
        self.inits.append(kw)
        return f"https://checkout.chapa.co/{kw['tx_ref']}"

    async def verify(self, tx_ref: str) -> dict[str, Any]:
        self.verifies += 1
        return self.state.get(tx_ref, {"status": "failed", "message": "not found", "data": None})

    def pay(self, tx_ref: str, amount: str = "600.00", **data: Any) -> None:
        self.state[tx_ref] = _verify(tx_ref=tx_ref, amount=amount, **data)

    async def aclose(self) -> None:
        pass


class BillingHarness(Harness):
    chapa: FakeChapa
    service: PaymentService
    settings: Settings


@pytest_asyncio.fixture
async def hb(tmp_path: Path) -> AsyncIterator[BillingHarness]:
    s = Settings(
        database_url=f"sqlite:///{tmp_path / 'bill.db'}",
        storage_dir=tmp_path / "store",
        rate_limit_messages_per_min=500,
        admin_telegram_ids=[777],
        chapa_webhook_secret=SecretStr(WEBHOOK_SECRET),
    )
    upgrade(s.database_url)
    engine = make_async_engine(s.database_url)
    factory = async_session_factory(engine)
    session = MockSession()
    bot = Bot("123456:TEST-token", session=session)
    chapa = FakeChapa()
    service = PaymentService(s, factory, bot, client=chapa)  # type: ignore[arg-type]
    queue = FakeQueue()
    dp = build_dispatcher(
        s,
        session_factory=factory,
        queue=queue,
        blobs=LocalStorage(s.storage_dir),
        payment_service=service,
    )
    h = BillingHarness(dp, bot, session, factory, queue)
    h.chapa, h.service, h.settings = chapa, service, s
    yield h
    await engine.dispose()


async def _user_id(h: Harness) -> int:
    async with await h.db() as db:
        user = await db.scalar(select(User).where(User.telegram_id == UID))
        assert user
        return user.id


async def _balance(h: Harness) -> int:
    async with await h.db() as db:
        return await ledger.balance(db, await _user_id(h))


def _edits(h: Harness) -> list[str]:
    return [str(m.text) for m in h.tg.requests if isinstance(m, EditMessageText)]


def _sent_to(h: Harness, chat_id: int) -> list[str]:
    return [
        str(m.text)
        for m in h.tg.requests
        if isinstance(m, SendMessage) and int(m.chat_id) == chat_id
    ]


async def _buy(h: BillingHarness, pack: str = "p5") -> str:
    await h.tap(f"buy:{pack}")
    async with await h.db() as db:
        payment = await db.scalar(select(Payment).order_by(Payment.id.desc()))
        assert payment
        return payment.tx_ref


# =========================================================================== ledger


@pytest.mark.asyncio
async def test_signup_bonus_once_and_idempotent_entries(hb: BillingHarness) -> None:
    await hb.send("/start")
    await hb.send("/start")
    assert await _balance(hb) == 2
    uid = await _user_id(hb)
    async with await hb.db() as db:
        assert await ledger.grant_signup_bonus(db, uid, 2) is False
        assert await ledger.add_entry(db, user_id=uid, delta=5, kind=LedgerKind.ADMIN, key="k1")
        assert not await ledger.add_entry(db, user_id=uid, delta=5, kind=LedgerKind.ADMIN, key="k1")
        assert (
            await ledger.charge(db, user_id=uid, amount=3, key="c1", project_id=None, note="x") == 4
        )
        with pytest.raises(ledger.InsufficientCredits) as err:
            await ledger.charge(db, user_id=uid, amount=5, key="c2", project_id=None, note="x")
        assert (err.value.needed, err.value.balance) == (5, 4)
        await db.commit()
    assert await _balance(hb) == 4


@pytest.mark.asyncio
async def test_signup_bonus_can_be_disabled(hb: BillingHarness) -> None:
    hb.settings.signup_bonus_credits = 0
    await hb.send("/start")
    assert await _balance(hb) == 0


# =========================================================================== /balance /buy


@pytest.mark.asyncio
async def test_balance_and_buy_flow(hb: BillingHarness) -> None:
    await _onboard(hb)
    await hb.send("/balance")
    text = hb.tg.last_text()
    assert "2" in text and t("balance_free_tier", "am").strip() in text

    await hb.send("/buy")
    assert hb.tg.last_text() == t("buy_choose", "am")
    buttons = hb.tg.requests[-1].reply_markup.inline_keyboard  # type: ignore[attr-defined]
    assert [b[0].callback_data for b in buttons] == ["buy:p5", "buy:p15", "buy:p40"]
    assert buttons[1][0].text == t("pack_label", "am", credits=15, price="1,500")

    tx_ref = await _buy(hb, "p15")
    assert tx_ref.startswith("adr-") and len(tx_ref) <= 50
    init = hb.chapa.inits[-1]
    assert init["amount_cents"] == 150000 and init["currency"] == "ETB"
    assert init["return_url"] == f"https://t.me/adreel_bot?start=paid_{tx_ref}"
    pay_kb = hb.tg.requests[-1].reply_markup.inline_keyboard  # type: ignore[attr-defined]
    assert pay_kb[0][0].url == f"https://checkout.chapa.co/{tx_ref}"
    assert pay_kb[1][0].callback_data == f"paid:{tx_ref}"

    await hb.tap(f"paid:{tx_ref}")  # not paid yet
    assert hb.tg.last_text() == t("payment_pending", "am")

    hb.chapa.pay(tx_ref, amount="1500.00")
    await hb.tap(f"paid:{tx_ref}")
    assert hb.tg.last_text() == t("payment_ok", "am", credits=15, balance=17)
    await hb.send(f"/start paid_{tx_ref}")  # the return link, after the fact
    assert hb.tg.last_text() == t("payment_already", "am", balance=17)
    assert await _balance(hb) == 17

    await hb.send("/balance")
    assert t("balance_free_tier", "am").strip() not in hb.tg.last_text()
    async with await hb.db() as db:
        p = await db.scalar(select(Payment).where(Payment.tx_ref == tx_ref))
        assert p and p.status == PaymentStatus.PAID and p.paid_at and p.provider_ref == "APabc"
        n = await db.scalar(
            select(CreditLedger).where(CreditLedger.idempotency_key == f"payment:{tx_ref}")
        )
        assert n and n.delta == 15 and n.payment_id == p.id


@pytest.mark.asyncio
async def test_buy_when_payments_are_off_or_failing(hb: BillingHarness) -> None:
    await _onboard(hb)
    hb.chapa.fail_init = True
    await hb.tap("buy:p5")
    assert hb.tg.last_text() == t("payment_init_failed", "am")
    async with await hb.db() as db:
        assert await db.scalar(select(Payment)) is None  # nothing half-created
    hb.service.client = None
    await hb.send("/buy")
    assert hb.tg.last_text() == t("payments_off", "am")


@pytest.mark.asyncio
async def test_mismatched_payment_is_never_credited(hb: BillingHarness) -> None:
    await _onboard(hb)
    tx_ref = await _buy(hb)
    hb.chapa.pay(tx_ref, amount="6.00")  # paid 6 ETB for a 600 ETB pack
    await hb.send(f"/start paid_{tx_ref}")
    assert hb.tg.last_text() == t("payment_mismatch", "am", tx_ref=tx_ref)
    hb.chapa.pay(tx_ref)  # even if Chapa later says otherwise, the verdict stands
    await hb.tap(f"paid:{tx_ref}")
    assert await _balance(hb) == 2


@pytest.mark.asyncio
async def test_someone_elses_payment_is_not_shown(hb: BillingHarness) -> None:
    await _onboard(hb)
    async with await hb.db() as db:
        other = User(telegram_id=55, language="am")
        db.add(other)
        await db.flush()
        db.add(
            Payment(user_id=other.id, tx_ref="adr-x", pack_id="p5", credits=5, amount_cents=60000)
        )
        await db.commit()
    await hb.tap("paid:adr-x")
    assert hb.tg.last_text() == t("payment_unknown", "am")


# =========================================================================== settle / sweep


@pytest.mark.asyncio
async def test_settle_is_idempotent_and_sweep_expires(hb: BillingHarness) -> None:
    await _onboard(hb)
    tx_ref = await _buy(hb)
    hb.chapa.pay(tx_ref)
    first = await hb.service.settle_and_notify(tx_ref)
    second = await hb.service.settle_and_notify(tx_ref)
    assert first and first.credited and first.balance == 7
    assert second and not second.credited
    assert len([x for x in _sent_to(hb, UID) if x.startswith("✅")]) == 1
    assert await hb.service.settle_and_notify("adr-unknown") is None

    old = await _buy(hb)
    async with await hb.db() as db:
        p = await db.scalar(select(Payment).where(Payment.tx_ref == old))
        assert p
        p.created_at = utcnow() - timedelta(hours=25)
        await db.commit()
    assert await hb.service.sweep_once() == 0
    async with await hb.db() as db:
        p = await db.scalar(select(Payment).where(Payment.tx_ref == old))
        assert p and p.status == PaymentStatus.FAILED and p.failure_reason == "expired"
        assert await payments.due_for_sweep(db, 60) == []


@pytest.mark.asyncio
async def test_sweep_credits_paid_pending_payments(hb: BillingHarness) -> None:
    await _onboard(hb)
    tx_ref = await _buy(hb)
    hb.chapa.pay(tx_ref)
    assert await hb.service.sweep_once() == 1
    assert await _balance(hb) == 7
    calls = hb.chapa.verifies
    assert await hb.service.sweep_once() == 0 and hb.chapa.verifies == calls  # nothing due


# =========================================================================== webhook server


@pytest.mark.asyncio
async def test_webhook_and_callback(hb: BillingHarness) -> None:
    await _onboard(hb)
    tx_ref = await _buy(hb)
    hb.chapa.pay(tx_ref)
    async with TestClient(TestServer(build_web_app(hb.service))) as client:
        assert (await client.get("/health")).status == 200
        body = json.dumps({"tx_ref": tx_ref, "status": "success"}).encode()
        resp = await client.post("/chapa/webhook", data=body, headers={"x-chapa-signature": "00"})
        assert resp.status == 401
        assert await _balance(hb) == 2  # a forged webhook does nothing

        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        for _ in range(2):  # Chapa may deliver twice
            resp = await client.post(
                "/chapa/webhook", data=body, headers={"x-chapa-signature": sig}
            )
            assert resp.status == 200 and (await resp.json())["status"] == "paid"
        assert await _balance(hb) == 7

        resp = await client.get("/chapa/callback", params={"trx_ref": tx_ref, "status": "success"})
        assert resp.status == 200
        assert await _balance(hb) == 7
        resp = await client.get("/chapa/callback")
        assert resp.status == 400

        empty = b"{}"
        sig = hmac.new(WEBHOOK_SECRET.encode(), empty, hashlib.sha256).hexdigest()
        resp = await client.post("/chapa/webhook", data=empty, headers={"x-chapa-signature": sig})
        assert (await resp.json()).get("ignored") is True


# =========================================================================== charging


@pytest.mark.asyncio
async def test_approve_charges_by_length_and_voice(hb: BillingHarness) -> None:
    pid = await _ready_project(hb, duration_s=45.0, voice="premium")  # 2 + 1 = 3 credits
    await hb.tap(f"pv:a:{pid}")
    assert hb.tg.last_text() == t("need_credits", "am", needed=3, balance=2)
    assert hb.queue.jobs == []

    async with await hb.db() as db:
        uid = await _user_id(hb)
        await ledger.add_entry(db, user_id=uid, delta=5, kind=LedgerKind.ADMIN, key="top-up")
        await db.commit()
    await hb.tap(f"pv:a:{pid}")  # the preview buttons still work after buying
    assert len(hb.queue.jobs) == 1
    assert _edits(hb)[-1].endswith(t("charged", "am", credits=3, balance=4))
    async with await hb.db() as db:
        job = await db.scalar(select(Job))
        project = await db.get(Project, uuid.UUID(pid))
        assert job and job.kind == JobKind.FINAL and project
        assert project.watermark is True  # still free tier: credits were granted, not bought
        charge = await db.scalar(
            select(CreditLedger).where(CreditLedger.idempotency_key == f"charge:{job.id}")
        )
        assert charge and charge.delta == -3 and charge.project_id == project.id
    await hb.tap(f"pv:a:{pid}")  # double tap
    assert await _balance(hb) == 4 and len(hb.queue.jobs) == 1


@pytest.mark.asyncio
async def test_paying_customer_gets_no_watermark(hb: BillingHarness) -> None:
    pid = await _ready_project(hb, duration_s=20.0, watermark=True)
    tx_ref = await _buy(hb)
    hb.chapa.pay(tx_ref)
    await hb.service.settle_and_notify(tx_ref)
    await hb.tap(f"pv:a:{pid}")
    async with await hb.db() as db:
        project = await db.get(Project, uuid.UUID(pid))
        assert (
            project
            and project.watermark is False
            and project.status == ProjectStatus.RENDERING_FINAL
        )
    assert await _balance(hb) == 6


@pytest.mark.asyncio
async def test_new_video_needs_credits(hb: BillingHarness) -> None:
    pid = await _ready_project(hb, duration_s=20.0, redo_count=0)
    async with await hb.db() as db:
        uid = await _user_id(hb)
        await ledger.add_entry(db, user_id=uid, delta=-2, kind=LedgerKind.ADMIN, key="drain")
        await db.commit()
    await hb.tap(f"pv:r:{pid}")
    assert hb.tg.last_text() == t("need_credits", "am", needed=1, balance=0)
    await hb.tap(f"pv:c:{pid}")
    assert hb.tg.last_text() == t("need_credits", "am", needed=1, balance=0)
    assert hb.queue.jobs == []


# =========================================================================== admin


@pytest.mark.asyncio
async def test_stats_and_grant_are_admin_only(hb: BillingHarness) -> None:
    await _onboard(hb)
    await hb.send("/stats")
    assert hb.tg.last_text() == t("unknown", "am")
    await hb.send(f"/grant {UID} 5")
    assert hb.tg.last_text() == t("unknown", "am")

    tx_ref = await _buy(hb)
    hb.chapa.pay(tx_ref)
    await hb.service.settle_and_notify(tx_ref)
    async with await hb.db() as db:
        project = await db.scalar(select(Project))
        if project is None:
            project = Project(user_id=await _user_id(hb), template_id="x", chat_id=CHAT)
            db.add(project)
            await db.flush()
        project.status = ProjectStatus.DELIVERED
        db.add(
            Job(
                project_id=project.id,
                kind=JobKind.AUTO_EDIT,
                status=JobStatus.SUCCEEDED,
                cost_usd=0.04,
            )
        )
        db.add(
            Job(
                project_id=project.id, kind=JobKind.FINAL, status=JobStatus.SUCCEEDED, cost_usd=0.02
            )
        )
        db.add(Job(project_id=project.id, kind=JobKind.RERENDER, status=JobStatus.FAILED))
        await db.commit()

    hb.user = hb.user.model_copy(update={"id": 777})  # now the admin speaks
    await hb.send("/stats")
    report = hb.tg.last_text()
    assert "Revenue: 600 ETB" in report and "Videos delivered: 1" in report
    assert "Failure rate: 33.3%" in report and "$0.060" in report
    assert "Credits sold 5" in report

    await hb.send("/grant")
    assert hb.tg.last_text() == t("grant_usage", "am")
    await hb.send("/grant 999 5")
    assert hb.tg.last_text() == t("user_not_found", "am")
    await hb.send(f"/grant {UID} 3 sorry for the outage")
    assert hb.tg.texts()[-2:] == [
        t("grant_done", "am", user=UID, delta=3, balance=10),  # to the admin
        t("grant_received", "am", credits=3, balance=10),  # to the user
    ]
    async with await hb.db() as db:
        st = await stats.collect(db)
        assert st.users == 2 and st.paying_users == 1 and st.credits_sold == 5


def test_sync_ledger_matches_async(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'l.db'}"
    upgrade(url)
    with session_scope(url) as db:
        user = User(telegram_id=1)
        db.add(user)
        db.flush()
        job_id = uuid.uuid4()
        assert ledger.refund_job_sync(db, job_id) == 0  # nothing charged
        ledger.add_entry_sync(db, user_id=user.id, delta=4, kind=LedgerKind.PURCHASE, key="p")
        ledger.add_entry_sync(
            db, user_id=user.id, delta=-3, kind=LedgerKind.CHARGE, key=f"charge:{job_id}"
        )
        assert not ledger.add_entry_sync(
            db, user_id=user.id, delta=-3, kind=LedgerKind.CHARGE, key=f"charge:{job_id}"
        )
        assert ledger.balance_sync(db, user.id) == 1
        assert ledger.refund_job_sync(db, job_id) == 3
        assert ledger.refund_job_sync(db, job_id) == 0
        assert ledger.balance_sync(db, user.id) == 4


def test_empty_chapa_key_means_payments_off(tmp_path: Path) -> None:
    s = Settings(chapa_secret_key=SecretStr("  "))
    assert not PaymentService(s, None, None).enabled  # type: ignore[arg-type]
    s = Settings(chapa_secret_key=SecretStr("CHASECK_TEST-x"))
    service = PaymentService(s, None, None)  # type: ignore[arg-type]
    assert service.enabled and service.client and service.client.test_mode
