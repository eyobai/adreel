"""Bot conversations through the real aiogram Dispatcher, with a mocked Telegram session."""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetFile, GetMe, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, File, Message, Update, Video
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adreel.bot.app import build_dispatcher
from adreel.config import Settings
from adreel.db.migrate import upgrade
from adreel.db.models import (
    BrandKitRow,
    Clip,
    Job,
    JobKind,
    JobStatus,
    Project,
    ProjectStatus,
    User,
)
from adreel.db.session import async_session_factory, make_async_engine
from adreel.i18n import t
from adreel.storage import LocalStorage
from tests.conftest import make_plan

CHAT = 4242
UID = 4242


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(1000)

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            ).as_(bot)  # real responses are bound to the bot (status.edit_text works)
        if isinstance(method, GetMe):
            return TgUser(id=1, is_bot=True, first_name="AdReel", username="adreel_bot")
        if isinstance(method, GetFile):
            return File(
                file_id=method.file_id,
                file_unique_id="u",
                file_path="photos/logo.jpg",
                file_size=10,
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b"\x89PNG fake logo"

    async def close(self) -> None:
        pass

    # helpers
    def texts(self) -> list[str]:
        return [str(m.text) for m in self.requests if isinstance(m, SendMessage)]

    def last_text(self) -> str:
        return self.texts()[-1]

    def alerts(self) -> list[str]:
        return [str(m.text) for m in self.requests if isinstance(m, AnswerCallbackQuery) and m.text]


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[str] = []

    def enqueue(self, job_id: str) -> str:
        self.jobs.append(job_id)
        return f"rq-{len(self.jobs)}"


class Harness:
    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        session: MockSession,
        factory: async_sessionmaker[AsyncSession],
        queue: FakeQueue,
    ) -> None:
        self.dp, self.bot, self.tg, self.factory, self.queue = dp, bot, session, factory, queue
        self._uid = itertools.count(1)
        self.user = TgUser(id=UID, is_bot=False, first_name="Abebe", language_code="am")
        self.chat = Chat(id=CHAT, type="private")

    def _msg(self, **kw: Any) -> Message:
        return Message(
            message_id=next(self._uid),
            date=datetime.now(UTC),
            chat=self.chat,
            from_user=self.user,
            **kw,
        )

    async def send(self, text: str) -> None:
        await self.dp.feed_update(
            self.bot, Update(update_id=next(self._uid), message=self._msg(text=text))
        )

    async def send_video(
        self, *, size: int = 2_000_000, duration: int = 12, uid: str | None = None
    ) -> None:
        n = next(self._uid)
        video = Video(
            file_id=f"vid-{n}",
            file_unique_id=uid or f"vu-{n}",
            width=1080,
            height=1920,
            duration=duration,
            file_size=size,
        )
        await self.dp.feed_update(self.bot, Update(update_id=n, message=self._msg(video=video)))

    async def tap(self, data: str) -> None:
        n = next(self._uid)
        origin = Message(
            message_id=n,
            date=datetime.now(UTC),
            chat=self.chat,
            text="…",
            from_user=TgUser(id=1, is_bot=True, first_name="bot"),
        )
        cb = CallbackQuery(
            id=str(n), from_user=self.user, chat_instance="ci", data=data, message=origin
        )
        await self.dp.feed_update(self.bot, Update(update_id=n, callback_query=cb))

    async def db(self) -> AsyncSession:
        return self.factory()


@pytest_asyncio.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    s = Settings(
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        storage_dir=tmp_path / "store",
        rate_limit_messages_per_min=200,
        rate_limit_videos_per_hour=3,
    )
    upgrade(s.database_url)
    engine = make_async_engine(s.database_url)
    factory = async_session_factory(engine)
    session = MockSession()
    bot = Bot("123456:TEST-token", session=session)
    queue = FakeQueue()
    dp = build_dispatcher(
        s, session_factory=factory, queue=queue, blobs=LocalStorage(s.storage_dir)
    )
    yield Harness(dp, bot, session, factory, queue)
    await engine.dispose()


async def _onboard(h: Harness) -> None:
    await h.send("/start")
    await h.tap("lang:am")
    await h.send("አበበ ካፌ")
    await h.tap("bt:cafe")
    await h.tap("skip")  # logo
    await h.tap("col:#1F7A4D")
    await h.send("0911 234 567")
    await h.send("@abebe_cafe")


# ---------------------------------------------------------------- onboarding


@pytest.mark.asyncio
async def test_onboarding_creates_brand(h: Harness) -> None:
    await h.send("/start")
    assert h.tg.last_text() == t("choose_language")
    await h.tap("lang:en")
    assert h.tg.last_text() == t("ask_business_name", "en")
    await h.send("Abebe Coffee")
    assert h.tg.last_text() == t("ask_business_type", "en")
    await h.tap("bt:cafe")
    await h.tap("skip")
    assert h.tg.last_text() == t("ask_color", "en")
    await h.send("not a colour")
    assert h.tg.last_text() == t("bad_color", "en")
    await h.send("1f7a4d")
    await h.send("12345")
    assert h.tg.last_text() == t("bad_phone", "en")
    await h.send("+251 911 234 567")
    await h.send("https://t.me/abebe_coffee")
    assert h.tg.last_text().startswith("Your brand is saved!")
    async with await h.db() as db:
        brand = await db.scalar(select(BrandKitRow))
        user = await db.scalar(select(User))
        assert brand and user and user.language == "en"
        assert (brand.business_name, brand.business_type, brand.primary_color) == (
            "Abebe Coffee",
            "cafe",
            "#1F7A4D",
        )
        assert (brand.phone, brand.telegram_handle, brand.logo_key) == (
            "+251911234567",
            "@abebe_coffee",
            None,
        )


@pytest.mark.asyncio
async def test_logo_upload_and_brand_edit(h: Harness) -> None:
    await _onboard(h)
    await h.send("/brand")
    assert "🏷 አበበ ካፌ" in h.tg.last_text()
    await h.tap("be:logo")
    photo = [
        {"file_id": "ph-1", "file_unique_id": "pu1", "width": 512, "height": 512, "file_size": 2048}
    ]
    await h.dp.feed_update(h.bot, Update(update_id=999, message=h._msg(photo=photo)))
    assert any(isinstance(r, GetFile) for r in h.tg.requests)
    async with await h.db() as db:
        brand = await db.scalar(select(BrandKitRow))
        assert brand and brand.logo_key == "users/1/brand/logo.jpg"
    await h.tap("be:color")
    await h.tap("col:#D62828")
    async with await h.db() as db:
        assert (await db.scalar(select(BrandKitRow))).primary_color == "#D62828"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_new_requires_brand(h: Harness) -> None:
    await h.send("/new")
    assert h.tg.last_text() == t("need_brand", "am")


# ---------------------------------------------------------------- /new


@pytest.mark.asyncio
async def test_product_promo_flow_enqueues_job(h: Harness) -> None:
    await _onboard(h)
    await h.send("/new")
    assert h.tg.last_text() == t("choose_template", "am")
    await h.tap("tpl:product_promo")
    await h.tap("clips:done")
    assert t("no_clips_yet", "am") in h.tg.alerts()
    await h.send("hello")
    assert h.tg.last_text() == t("not_a_video", "am")
    await h.send_video(size=30_000_000)
    assert h.tg.last_text().startswith("ይህ ቪዲዮ በጣም ትልቅ ነው (30 MB)")
    await h.send_video(duration=500)
    assert "500" in h.tg.last_text()
    await h.send_video(uid="same")
    await h.send_video(uid="same")  # duplicate is ignored
    await h.send_video()
    assert h.tg.last_text() == t("clip_received", "am", n=2, max=5)
    await h.tap("clips:done")
    assert h.tg.last_text() == t("ask_brief", "am")
    await h.send("ይርጋጨፌ ቡና ማስታወቂያ፣ ፈጣን አድርጉት")
    assert h.tg.last_text() == t("brief_need_price", "am")  # promo needs a price
    await h.send("ይርጋጨፌ ቡና ማስታወቂያ፣ ዋጋው 1,200 ብር፣ ፈጣን አድርጉት")
    assert h.tg.last_text() == t("ask_voice", "am")
    await h.tap("voice:standard")
    assert h.tg.last_text() == t("queued", "am")
    assert len(h.queue.jobs) == 1
    async with await h.db() as db:
        project = await db.scalar(select(Project))
        job = await db.scalar(select(Job))
        clips = (await db.scalars(select(Clip).order_by(Clip.idx))).all()
        assert project and job
        assert project.status == ProjectStatus.QUEUED and project.voice == "standard"
        assert project.context == {"brief": "ይርጋጨፌ ቡና ማስታወቂያ፣ ዋጋው 1,200 ብር፣ ፈጣን አድርጉት"}
        assert project.status_message_id is not None and project.chat_id == CHAT
        assert (
            job.kind == JobKind.AUTO_EDIT
            and str(job.id) == h.queue.jobs[0]
            and job.rq_job_id == "rq-1"
        )
        assert [c.idx for c in clips] == [1, 2]
    await h.send("/new")
    assert h.tg.last_text() == t("busy", "am")


@pytest.mark.asyncio
async def test_max_clips_moves_on_and_talking_head_skips_price(h: Harness) -> None:
    await _onboard(h)
    await h.send("/new")
    await h.tap("tpl:talking_head")
    for _ in range(5):
        await h.send_video()
    assert h.tg.texts()[-2] == t("max_clips", "am", max=5)
    assert h.tg.last_text() == t("ask_brief", "am")


@pytest.mark.asyncio
async def test_let_ai_choose_template_and_skip_brief(h: Harness) -> None:
    await _onboard(h)
    await h.send("/new")
    kb = next(m for m in reversed(h.tg.requests) if isinstance(m, SendMessage)).reply_markup
    assert kb.inline_keyboard[0][0].callback_data == "tpl:auto"  # type: ignore[union-attr]
    await h.tap("tpl:auto")
    await h.send_video()
    await h.tap("clips:done")
    await h.tap("skip")  # no brief: the AI decides from the footage alone
    assert h.tg.last_text() == t("ask_voice", "am")
    await h.tap("voice:none")
    async with await h.db() as db:
        project = await db.scalar(select(Project))
        assert project and project.template_id == "auto" and project.context == {}
        assert project.status == ProjectStatus.QUEUED


@pytest.mark.asyncio
async def test_resume_draft(h: Harness) -> None:
    await _onboard(h)
    await h.send("/new")
    await h.tap("tpl:talking_head")
    await h.send_video()
    await h.send("/cancel")
    await h.send("/new")
    assert h.tg.last_text() == t("resume_draft", "am", n=1)
    await h.tap("new:resume")
    await h.send_video()
    assert t("clip_received", "am", n=2, max=5) in h.tg.last_text()
    await h.send("/new")
    await h.tap("new:restart")
    async with await h.db() as db:
        statuses = sorted(p.status for p in (await db.scalars(select(Project))).all())
        assert statuses == [ProjectStatus.CANCELLED]


@pytest.mark.asyncio
async def test_hourly_video_limit(h: Harness) -> None:
    await _onboard(h)
    for i in range(4):
        await h.send("/new")
        await h.tap("tpl:talking_head")
        await h.send_video()
        await h.tap("clips:done")
        await h.tap("skip")
        await h.tap("voice:none")
        async with await h.db() as db:  # pretend the job finished
            for job in (await db.scalars(select(Job))).all():
                job.status = JobStatus.SUCCEEDED
            await db.commit()
        if i < 3:
            assert h.tg.last_text() == t("queued", "am")
    assert h.tg.last_text() == t("rate_limited_videos", "am")
    assert len(h.queue.jobs) == 3


# ---------------------------------------------------------------- preview review


async def _ready_project(h: Harness, **updates: Any) -> str:
    await _onboard(h)
    plan = make_plan(
        [("s1", "c1", 0.0, 5.0)],
        [
            {"id": 1, "text": "ሰላም", "clip_id": "c1", "start": 0.5, "end": 1.5},
            {"id": 2, "text": "እንኳን ደህና መጡ", "clip_id": "c1", "start": 2.0, "end": 3.0},
        ],
    )
    async with await h.db() as db:
        user = await db.scalar(select(User))
        assert user
        project = Project(
            user_id=user.id,
            template_id="talking_head",
            status=ProjectStatus.PREVIEW_READY,
            chat_id=CHAT,
            plan=plan.model_dump(mode="json"),
            **updates,
        )
        db.add(project)
        await db.commit()
        return str(project.id)


@pytest.mark.asyncio
async def test_fix_captions_flow(h: Harness) -> None:
    pid = await _ready_project(h)
    await h.tap(f"pv:f:{pid}")
    assert "1: ሰላም\n2: እንኳን ደህና መጡ" in h.tg.last_text()
    await h.send("ሰላም ጤና ይስጥልኝ")
    assert h.tg.last_text() == t("fix_bad_format", "am")
    await h.send("9: ምንም")
    assert h.tg.last_text() == t("fix_unknown_line", "am", n=9)
    await h.send("1: ሰላም ወዳጆች\n፪. እንኳን በደህና መጡ")
    assert h.tg.last_text() == t("fix_applied", "am", count=2)
    async with await h.db() as db:
        project = await db.scalar(select(Project))
        job = await db.scalar(select(Job))
        assert project and job and job.kind == JobKind.RERENDER and project.fix_count == 1
        assert [c["text"] for c in project.plan["captions"]] == ["ሰላም ወዳጆች", "እንኳን በደህና መጡ"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_approve_and_redo(h: Harness) -> None:
    pid = await _ready_project(h, redo_count=2)
    await h.tap(f"pv:r:{pid}")
    assert t("redo_limit", "am") in h.tg.alerts()
    await h.tap(f"pv:a:{pid}")
    assert h.tg.last_text() == t("final_rendering", "am")
    async with await h.db() as db:
        job = await db.scalar(select(Job))
        project = await db.scalar(select(Project))
        assert (
            job
            and project
            and job.kind == JobKind.FINAL
            and project.status == ProjectStatus.RENDERING_FINAL
        )
    await h.tap(f"pv:a:{pid}")  # double tap: project no longer ready
    assert t("not_ready", "am") in h.tg.alerts()
    assert len(h.queue.jobs) == 1


@pytest.mark.asyncio
async def test_change_something_with_a_prompt(h: Harness) -> None:
    pid = await _ready_project(h, redo_count=1)
    await h.tap(f"pv:c:{pid}")
    assert h.tg.last_text() == t("ask_change", "am")
    await h.send("x" * 600)
    assert h.tg.last_text() == t("bad_change", "am")
    await h.send("ዋጋውን መጀመሪያ ላይ አሳይ፣ ቀለሙን ሞቅ አድርገው")
    assert h.tg.last_text() == t("change_started", "am", left=0)
    async with await h.db() as db:
        job = await db.scalar(select(Job))
        project = await db.scalar(select(Project))
        assert job and project and job.kind == JobKind.REVISE and project.redo_count == 2
        assert job.params == {"instruction": "ዋጋውን መጀመሪያ ላይ አሳይ፣ ቀለሙን ሞቅ አድርገው"}
        project.status = ProjectStatus.PREVIEW_READY
        job.status = JobStatus.SUCCEEDED
        await db.commit()
    await h.tap(f"pv:c:{pid}")  # free changes used up (shared with redo)
    assert t("redo_limit", "am") in h.tg.alerts()
    assert len(h.queue.jobs) == 1


@pytest.mark.asyncio
async def test_preview_buttons_of_other_users_are_rejected(h: Harness) -> None:
    pid = await _ready_project(h)
    async with await h.db() as db:
        other = User(telegram_id=1, language="am")
        db.add(other)
        await db.flush()
        project = await db.get(Project, __import__("uuid").UUID(pid))
        assert project
        project.user_id = other.id
        await db.commit()
    await h.tap(f"pv:a:{pid}")
    assert t("not_ready", "am") in h.tg.alerts() and h.queue.jobs == []


@pytest.mark.asyncio
async def test_flood_control(h: Harness) -> None:
    h.dp.message.outer_middleware._middlewares[0].per_minute = 3  # type: ignore[attr-defined]
    for _ in range(6):
        await h.send("/help")
    assert h.tg.texts().count(t("help", "am")) == 3
    assert t("slow_down", "am") in h.tg.texts()
