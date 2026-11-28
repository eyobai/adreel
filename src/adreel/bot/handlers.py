"""Bot conversation: onboarding / brand kit, /new, preview review, help.

Handlers get these from the dispatcher's workflow data and middlewares:
``db`` (AsyncSession), ``user`` (User), ``lang``, ``settings``, ``queue`` (JobQueue),
``blobs`` (Storage), ``templates`` (list of template ids).
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Document, Message, Video, VideoNote
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from adreel.bot import keyboards as kb
from adreel.bot.parsing import (
    clean_text,
    parse_caption_fixes,
    parse_handle,
    parse_hex_color,
    parse_phone,
    parse_price,
)
from adreel.bot.services import (
    add_clip,
    brand_summary,
    create_job,
    create_project,
    get_project,
    has_active_job,
    latest_draft,
    save_brand,
    videos_last_hour,
)
from adreel.bot.states import BrandForm, FixCaptions, NewVideo
from adreel.config import Settings
from adreel.db.models import JobKind, Project, ProjectStatus, User
from adreel.i18n import is_skip, normalize_lang, t
from adreel.jobs.queue import JobQueue
from adreel.jobs.telegram_client import STANDARD_DOWNLOAD_LIMIT
from adreel.log import get_logger
from adreel.render.captions import caption_listing
from adreel.schemas.brand_kit import BusinessType
from adreel.schemas.edit_plan import EditPlan
from adreel.storage import Storage, logo_key
from adreel.telegram_ui import parse_preview_callback

log = get_logger(__name__)
router = Router(name="adreel")

BRAND_ORDER = ["name", "btype", "logo", "color", "phone", "handle"]
BRAND_STATE: dict[str, State] = {
    "name": BrandForm.name,
    "btype": BrandForm.btype,
    "logo": BrandForm.logo,
    "color": BrandForm.color,
    "phone": BrandForm.phone,
    "handle": BrandForm.handle,
}
BRAND_COLUMN = {
    "name": "business_name",
    "btype": "business_type",
    "logo": "logo_key",
    "color": "primary_color",
    "phone": "phone",
    "handle": "telegram_handle",
}
MAX_LOGO_BYTES = 5 * 1024 * 1024
LOCAL_DOWNLOAD_LIMIT = 2000 * 1024 * 1024

_clip_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# =========================================================================== common


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: User, lang: str) -> None:
    await state.clear()
    if user.brand_kit is not None:
        await message.answer(t("welcome_back", lang))
        return
    await state.set_state(BrandForm.language)
    await message.answer(t("choose_language", lang), reply_markup=kb.language_kb())


@router.message(Command("help"))
async def cmd_help(message: Message, lang: str) -> None:
    await message.answer(t("help", lang))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, lang: str) -> None:
    await state.clear()
    await message.answer(t("cancelled", lang))


@router.message(Command("lang"))
async def cmd_lang(message: Message) -> None:
    await message.answer(t("choose_language"), reply_markup=kb.language_kb())


@router.callback_query(F.data.startswith("lang:"))
async def on_language(cb: CallbackQuery, state: FSMContext, user: User) -> None:
    lang = normalize_lang((cb.data or "").split(":", 1)[1])
    user.language = lang
    await cb.answer()
    if user.brand_kit is None:  # first run: continue onboarding
        await state.set_state(BrandForm.name)
        await state.update_data(mode="onboard")
        await _reply(cb).answer(t("welcome", lang))
        await _reply(cb).answer(t("ask_business_name", lang))
    else:
        await _reply(cb).answer(t("lang_set", lang))


# =========================================================================== brand kit


async def _ask_brand_step(target: Message, step: str, lang: str) -> None:
    if step == "name":
        await target.answer(t("ask_business_name", lang))
    elif step == "btype":
        await target.answer(t("ask_business_type", lang), reply_markup=kb.business_type_kb(lang))
    elif step == "logo":
        await target.answer(t("ask_logo", lang), reply_markup=kb.skip_kb(lang))
    elif step == "color":
        await target.answer(t("ask_color", lang), reply_markup=kb.color_kb(lang))
    elif step == "phone":
        await target.answer(t("ask_phone", lang), reply_markup=kb.skip_kb(lang))
    elif step == "handle":
        await target.answer(t("ask_handle", lang), reply_markup=kb.skip_kb(lang))


async def _brand_value(
    target: Message,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    step: str,
    value: str | None,
) -> None:
    """Store one answer; in edit mode save and finish, in onboarding go to the next step."""
    data = await state.get_data()
    if data.get("mode") == "edit":
        brand = await save_brand(db, user, **{BRAND_COLUMN[step]: value})
        await state.clear()
        await target.answer(t("brand_updated", lang, summary=brand_summary(brand, lang)))
        return
    await state.update_data({step: value})
    i = BRAND_ORDER.index(step)
    if i + 1 < len(BRAND_ORDER):
        nxt = BRAND_ORDER[i + 1]
        await state.set_state(BRAND_STATE[nxt])
        await _ask_brand_step(target, nxt, lang)
        return
    data = await state.get_data()
    fields: dict[str, Any] = {BRAND_COLUMN[k]: data.get(k) for k in BRAND_ORDER}
    fields["business_type"] = fields.get("business_type") or "other"
    fields["primary_color"] = fields.get("primary_color") or kb.BRAND_COLORS[3][1]
    brand = await save_brand(db, user, **fields)
    await state.clear()
    await target.answer(t("brand_saved", lang, summary=brand_summary(brand, lang)))


@router.message(Command("brand"))
async def cmd_brand(message: Message, state: FSMContext, user: User, lang: str) -> None:
    await state.clear()
    if user.brand_kit is None:
        await message.answer(t("need_brand", lang))
        return
    await message.answer(
        t("brand_menu", lang, summary=brand_summary(user.brand_kit, lang)),
        reply_markup=kb.brand_menu_kb(lang),
    )


@router.callback_query(F.data.startswith("be:"))
async def on_brand_edit(cb: CallbackQuery, state: FSMContext, lang: str) -> None:
    step = (cb.data or "").split(":", 1)[1]
    await cb.answer()
    if step not in BRAND_STATE:
        return
    await state.set_state(BRAND_STATE[step])
    await state.update_data(mode="edit")
    await _ask_brand_step(_reply(cb), step, lang)


@router.message(BrandForm.name, F.text)
async def brand_name(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    name = clean_text(message.text, 60)
    if name is None:
        await message.answer(t("bad_business_name", lang))
        return
    await _brand_value(message, state, db, user, lang, "name", name)


@router.callback_query(BrandForm.btype, F.data.startswith("bt:"))
async def brand_type(
    cb: CallbackQuery, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    value = (cb.data or "").split(":", 1)[1]
    await cb.answer()
    if value not in {b.value for b in BusinessType}:
        return
    await _brand_value(_reply(cb), state, db, user, lang, "btype", value)


@router.message(BrandForm.logo, F.photo | F.document)
async def brand_logo(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    bot: Bot,
    blobs: Storage,
) -> None:
    if message.photo:
        file_id, suffix, size = message.photo[-1].file_id, ".jpg", message.photo[-1].file_size
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        doc = message.document
        file_id, size = doc.file_id, doc.file_size
        suffix = ".png" if (doc.mime_type or "").endswith("png") else ".jpg"
    else:
        await message.answer(t("bad_logo", lang))
        return
    if size and size > MAX_LOGO_BYTES:
        await message.answer(t("bad_logo", lang))
        return
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"logo{suffix}"
        await bot.download(file_id, destination=local)
        key = logo_key(user.id, suffix)
        await asyncio.to_thread(blobs.put_file, local, key)
    await message.answer(t("logo_saved", lang))
    await _brand_value(message, state, db, user, lang, "logo", key)


@router.callback_query(BrandForm.color, F.data.startswith("col:"))
async def brand_color_pick(
    cb: CallbackQuery, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    color = parse_hex_color((cb.data or "").split(":", 1)[1])
    await cb.answer()
    if color:
        await _brand_value(_reply(cb), state, db, user, lang, "color", color)


@router.message(BrandForm.color, F.text)
async def brand_color_text(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    color = parse_hex_color(message.text or "")
    if color is None:
        await message.answer(t("bad_color", lang), reply_markup=kb.color_kb(lang))
        return
    await _brand_value(message, state, db, user, lang, "color", color)


@router.message(BrandForm.phone, F.text)
async def brand_phone(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    if is_skip(message.text):
        await _brand_value(message, state, db, user, lang, "phone", None)
        return
    phone = parse_phone(message.text or "")
    if phone is None:
        await message.answer(t("bad_phone", lang), reply_markup=kb.skip_kb(lang))
        return
    await _brand_value(message, state, db, user, lang, "phone", phone)


@router.message(BrandForm.handle, F.text)
async def brand_handle(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    if is_skip(message.text):
        await _brand_value(message, state, db, user, lang, "handle", None)
        return
    handle = parse_handle(message.text or "")
    if handle is None:
        await message.answer(t("bad_handle", lang), reply_markup=kb.skip_kb(lang))
        return
    await _brand_value(message, state, db, user, lang, "handle", handle)


@router.message(BrandForm.logo, F.text)
async def brand_logo_text(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    if is_skip(message.text):
        await _skip_logo(message, state, db, user, lang)
    else:
        await message.answer(t("bad_logo", lang), reply_markup=kb.skip_kb(lang))


async def _skip_logo(
    target: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    data = await state.get_data()
    if data.get("mode") == "edit":  # skipping in edit mode keeps the current logo
        await state.clear()
        await target.answer(t("brand_updated", lang, summary=brand_summary(user.brand_kit, lang)))  # type: ignore[arg-type]
        return
    await _brand_value(target, state, db, user, lang, "logo", None)


# =========================================================================== /new


@router.message(Command("new"))
async def cmd_new(
    message: Message,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    templates: list[str],
) -> None:
    await state.clear()
    if user.brand_kit is None:
        await message.answer(t("need_brand", lang))
        return
    if await has_active_job(db, user.id):
        await message.answer(t("busy", lang))
        return
    draft = await latest_draft(db, user.id)
    if draft is not None and draft.clips:
        await message.answer(
            t("resume_draft", lang, n=len(draft.clips)), reply_markup=kb.resume_kb(lang)
        )
        return
    await state.set_state(NewVideo.template)
    await message.answer(t("choose_template", lang), reply_markup=kb.template_kb(lang, templates))


@router.callback_query(F.data.in_({"new:resume", "new:restart"}))
async def on_resume(
    cb: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    settings: Settings,
    templates: list[str],
) -> None:
    await cb.answer()
    draft = await latest_draft(db, user.id)
    if cb.data == "new:resume" and draft is not None:
        await state.set_state(NewVideo.clips)
        await state.update_data(project_id=str(draft.id))
        n = len(draft.clips)
        await _reply(cb).answer(
            t("clip_received", lang, n=n, max=settings.max_clips)
            + "\n\n"
            + t("send_clips", lang, max=settings.max_clips),
            reply_markup=kb.done_kb(lang),
        )
        return
    if draft is not None:
        draft.status = ProjectStatus.CANCELLED
    await state.set_state(NewVideo.template)
    await _reply(cb).answer(
        t("choose_template", lang), reply_markup=kb.template_kb(lang, templates)
    )


@router.callback_query(F.data.startswith("tpl:"))
async def on_template(
    cb: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    settings: Settings,
    templates: list[str],
) -> None:
    template_id = (cb.data or "").split(":", 1)[1]
    await cb.answer()
    if template_id not in templates or user.brand_kit is None:
        return
    project = await create_project(
        db, user, template_id, chat_id=cb.message.chat.id if cb.message else user.telegram_id
    )
    await state.set_state(NewVideo.clips)
    await state.update_data(project_id=str(project.id))
    await _reply(cb).answer(
        t("send_clips", lang, max=settings.max_clips), reply_markup=kb.done_kb(lang)
    )


@router.message(NewVideo.clips, F.video | F.document | F.video_note)
async def on_clip(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str, settings: Settings
) -> None:
    media: Video | VideoNote | Document | None = message.video or message.video_note
    if (
        media is None
        and message.document
        and (message.document.mime_type or "").startswith("video/")
    ):
        media = message.document
    if media is None:
        await message.answer(t("not_a_video", lang))
        return
    limit = LOCAL_DOWNLOAD_LIMIT if settings.telegram_local_mode else STANDARD_DOWNLOAD_LIMIT
    if media.file_size and media.file_size > limit:
        await message.answer(
            t("clip_too_big", lang, mb=round(media.file_size / 1e6), limit=round(limit / 1e6))
        )
        return
    duration = getattr(media, "duration", None)
    if duration and duration > settings.max_clip_seconds:
        await message.answer(
            t("clip_too_long", lang, secs=duration, limit=settings.max_clip_seconds)
        )
        return
    data = await state.get_data()
    async with _clip_locks[user.id]:  # albums arrive as parallel updates
        project = (
            await get_project(db, data.get("project_id", ""), user.id)
            if data.get("project_id")
            else None
        )
        if project is None or project.status != ProjectStatus.DRAFT:
            await state.clear()
            await message.answer(t("unknown", lang))
            return
        _, total = await add_clip(
            db,
            project,
            file_id=media.file_id,
            unique_id=media.file_unique_id,
            size=media.file_size,
            duration=float(duration) if duration else None,
        )
        await db.commit()
    if total >= settings.max_clips:
        await message.answer(t("max_clips", lang, max=settings.max_clips))
        await _ask_questions(message, state, project, lang)
        return
    await message.answer(
        t("clip_received", lang, n=total, max=settings.max_clips), reply_markup=kb.done_kb(lang)
    )


@router.message(NewVideo.clips)
async def on_clip_other(message: Message, lang: str) -> None:
    await message.answer(t("not_a_video", lang), reply_markup=kb.done_kb(lang))


@router.callback_query(F.data == "clips:done")
async def on_clips_done(
    cb: CallbackQuery, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    data = await state.get_data()
    project = await get_project(db, data["project_id"], user.id) if data.get("project_id") else None
    if project is None or project.status != ProjectStatus.DRAFT:
        await cb.answer()
        return
    if not project.clips:
        await cb.answer(t("no_clips_yet", lang), show_alert=True)
        return
    await cb.answer()
    await _ask_questions(_reply(cb), state, project, lang)


async def _ask_questions(target: Message, state: FSMContext, project: Project, lang: str) -> None:
    if project.template_id == "product_promo":
        await state.set_state(NewVideo.product)
        await target.answer(t("ask_product", lang))
    else:
        await state.set_state(NewVideo.highlight)
        await target.answer(t("ask_highlight", lang), reply_markup=kb.skip_kb(lang))


async def _set_context(
    db: AsyncSession, user: User, state: FSMContext, **values: Any
) -> Project | None:
    data = await state.get_data()
    project = (
        await get_project(db, data.get("project_id", ""), user.id)
        if data.get("project_id")
        else None
    )
    if project is not None:
        project.context = {**(project.context or {}), **values}  # new dict: JSON change tracking
    return project


@router.message(NewVideo.product, F.text)
async def on_product(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    name = clean_text(message.text, 40)
    if name is None:
        await message.answer(t("bad_product", lang))
        return
    await _set_context(db, user, state, product_name=name)
    await state.set_state(NewVideo.price)
    await message.answer(t("ask_price", lang))


@router.message(NewVideo.price, F.text)
async def on_price(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    price = parse_price(message.text or "")
    if price is None:
        await message.answer(t("bad_price", lang))
        return
    await _set_context(db, user, state, price=price)
    await state.set_state(NewVideo.highlight)
    await message.answer(t("ask_highlight", lang), reply_markup=kb.skip_kb(lang))


@router.message(NewVideo.highlight, F.text)
async def on_highlight(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    value = None if is_skip(message.text) else clean_text(message.text, 200)
    await _set_context(db, user, state, highlight=value)
    await state.set_state(NewVideo.voice)
    await message.answer(t("ask_voice", lang), reply_markup=kb.voice_kb(lang))


@router.callback_query(F.data == "skip")
async def on_skip(
    cb: CallbackQuery, state: FSMContext, db: AsyncSession, user: User, lang: str
) -> None:
    await cb.answer()
    current = await state.get_state()
    target = _reply(cb)
    if current == BrandForm.logo.state:
        await _skip_logo(target, state, db, user, lang)
    elif current == BrandForm.phone.state:
        await _brand_value(target, state, db, user, lang, "phone", None)
    elif current == BrandForm.handle.state:
        await _brand_value(target, state, db, user, lang, "handle", None)
    elif current == NewVideo.highlight.state:
        await _set_context(db, user, state, highlight=None)
        await state.set_state(NewVideo.voice)
        await target.answer(t("ask_voice", lang), reply_markup=kb.voice_kb(lang))


@router.callback_query(NewVideo.voice, F.data.startswith("voice:"))
async def on_voice(
    cb: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    settings: Settings,
    queue: JobQueue,
) -> None:
    choice = (cb.data or "").split(":", 1)[1]
    await cb.answer()
    project = await _set_context(db, user, state)
    if project is None or project.status != ProjectStatus.DRAFT:
        await state.clear()
        return
    project.voice = choice if choice in ("standard", "premium") else None
    target = _reply(cb)
    if await has_active_job(db, user.id):
        await target.answer(t("busy", lang))
        return
    if await videos_last_hour(db, user.id) >= settings.rate_limit_videos_per_hour:
        await target.answer(t("rate_limited_videos", lang))
        return
    await state.clear()
    await _start_job(db, target, project, JobKind.AUTO_EDIT, t("queued", lang), queue)


async def _start_job(
    db: AsyncSession, target: Message, project: Project, kind: JobKind, text: str, queue: JobQueue
) -> None:
    status = await target.answer(text)
    project.chat_id = status.chat.id
    job = await create_job(db, project, kind, status.message_id)
    await db.commit()  # the worker must see the job before it runs
    job.rq_job_id = await asyncio.to_thread(queue.enqueue, str(job.id))
    log.info("job.enqueued", job=str(job.id), kind=kind.value, project=str(project.id))


# =========================================================================== preview review


@router.callback_query(F.data.startswith("pv:"))
async def on_preview_action(
    cb: CallbackQuery,
    state: FSMContext,
    db: AsyncSession,
    user: User,
    lang: str,
    settings: Settings,
    queue: JobQueue,
) -> None:
    parsed = parse_preview_callback(cb.data)
    project = await get_project(db, parsed.project_id, user.id) if parsed else None
    if (
        parsed is None
        or project is None
        or project.status != ProjectStatus.PREVIEW_READY
        or not project.plan
    ):
        await cb.answer(t("not_ready", lang), show_alert=True)
        return
    if await has_active_job(db, user.id):
        await cb.answer(t("busy", lang), show_alert=True)
        return
    target = _reply(cb)
    if parsed.action == "fix":
        plan = EditPlan.model_validate(project.plan)
        if not plan.captions:
            await cb.answer(t("no_captions", lang), show_alert=True)
            return
        if project.fix_count >= settings.max_caption_fixes:
            await cb.answer(t("fix_limit", lang), show_alert=True)
            return
        await cb.answer()
        await state.set_state(FixCaptions.waiting)
        await state.update_data(project_id=str(project.id))
        for chunk in _chunks(t("fix_intro", lang, listing=caption_listing(plan.captions))):
            await target.answer(chunk)
        return
    if parsed.action == "redo":
        if project.redo_count >= settings.max_redos:
            await cb.answer(t("redo_limit", lang), show_alert=True)
            return
        await cb.answer()
        await _drop_keyboard(cb)
        project.redo_count += 1
        left = settings.max_redos - project.redo_count
        await _start_job(
            db, target, project, JobKind.AUTO_EDIT, t("redo_started", lang, left=left), queue
        )
        return
    await cb.answer()  # approve
    await _drop_keyboard(cb)
    await _start_job(db, target, project, JobKind.FINAL, t("final_rendering", lang), queue)


@router.message(FixCaptions.waiting, F.text)
async def on_caption_fix(
    message: Message, state: FSMContext, db: AsyncSession, user: User, lang: str, queue: JobQueue
) -> None:
    try:
        fixes = parse_caption_fixes(message.text or "")
    except ValueError:
        await message.answer(t("fix_bad_format", lang))
        return
    data = await state.get_data()
    project = (
        await get_project(db, data.get("project_id", ""), user.id)
        if data.get("project_id")
        else None
    )
    if project is None or project.status != ProjectStatus.PREVIEW_READY or not project.plan:
        await state.clear()
        await message.answer(t("not_ready", lang))
        return
    plan = EditPlan.model_validate(project.plan)
    known = {c.id for c in plan.captions}
    for n, _ in fixes:
        if n not in known:
            await message.answer(t("fix_unknown_line", lang, n=n))
            return
    try:
        for n, text in fixes:
            plan = plan.with_caption_text(n, text)
    except ValidationError:
        await message.answer(t("fix_bad_format", lang))
        return
    if await has_active_job(db, user.id):
        await message.answer(t("busy", lang))
        return
    project.plan = plan.model_dump(mode="json")
    project.fix_count += 1
    await state.clear()
    await _start_job(
        db, message, project, JobKind.RERENDER, t("fix_applied", lang, count=len(fixes)), queue
    )


# =========================================================================== fallbacks


@router.message(StateFilter(None))
async def fallback(message: Message, lang: str) -> None:
    await message.answer(t("unknown", lang))


@router.message()
async def fallback_in_state(message: Message, lang: str) -> None:
    await message.answer(t("unknown", lang))


@router.callback_query()
async def fallback_callback(cb: CallbackQuery) -> None:
    await cb.answer()


# =========================================================================== helpers


def build_router() -> Router:
    """A fresh router with all handlers above (a Router can join only one Dispatcher)."""
    fresh = Router(name="adreel")
    for name, observer in router.observers.items():
        fresh.observers[name].handlers.extend(observer.handlers)
    return fresh


def _reply(cb: CallbackQuery) -> Message:
    """The message a callback came from (where we answer)."""
    assert isinstance(cb.message, Message)
    return cb.message


async def _drop_keyboard(cb: CallbackQuery) -> None:
    """Remove the preview buttons so a second tap can't start a second job."""
    with contextlib.suppress(TelegramBadRequest):
        await _reply(cb).edit_reply_markup(reply_markup=None)


def _chunks(text: str, size: int = 3800) -> list[str]:
    lines, out, cur = text.split("\n"), [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out
