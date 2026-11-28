"""Inline keyboards (localized)."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardMarkup

from adreel.i18n import t
from adreel.schemas.brand_kit import BusinessType
from adreel.telegram_ui import inline_keyboard

BRAND_COLORS: list[tuple[str, str]] = [
    ("🔴", "#D62828"),
    ("🟠", "#F77F00"),
    ("🟡", "#E9B308"),
    ("🟢", "#1F7A4D"),
    ("🔵", "#1D4ED8"),
    ("🟣", "#7B2CBF"),
    ("🟤", "#7B3F00"),
    ("⚫", "#111111"),
]
BRAND_FIELDS = ("name", "btype", "logo", "color", "phone", "handle")
_FIELD_LABEL = {
    "name": "f_name",
    "btype": "f_type",
    "logo": "f_logo",
    "color": "f_color",
    "phone": "f_phone",
    "handle": "f_handle",
}


def markup(kb: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup.model_validate(kb)


def language_kb() -> InlineKeyboardMarkup:
    return markup(inline_keyboard([[("አማርኛ", "lang:am"), ("English", "lang:en")]]))


def skip_kb(lang: str) -> InlineKeyboardMarkup:
    return markup(inline_keyboard([[(t("btn_skip", lang), "skip")]]))


def business_type_kb(lang: str) -> InlineKeyboardMarkup:
    items = [(t(f"bt_{b.value}", lang), f"bt:{b.value}") for b in BusinessType]
    return markup(inline_keyboard([items[i : i + 2] for i in range(0, len(items), 2)]))


def color_kb(lang: str) -> InlineKeyboardMarkup:
    items = [(f"{dot} {hexv}", f"col:{hexv}") for dot, hexv in BRAND_COLORS]
    return markup(inline_keyboard([items[i : i + 2] for i in range(0, len(items), 2)]))


def brand_menu_kb(lang: str) -> InlineKeyboardMarkup:
    items = [(t(_FIELD_LABEL[f], lang), f"be:{f}") for f in BRAND_FIELDS]
    return markup(inline_keyboard([items[i : i + 3] for i in range(0, len(items), 3)]))


def template_kb(lang: str, template_ids: list[str]) -> InlineKeyboardMarkup:
    return markup(
        inline_keyboard([[(t(f"tpl_{tid}", lang), f"tpl:{tid}")] for tid in template_ids])
    )


def done_kb(lang: str) -> InlineKeyboardMarkup:
    return markup(inline_keyboard([[(t("btn_done", lang), "clips:done")]]))


def resume_kb(lang: str) -> InlineKeyboardMarkup:
    return markup(
        inline_keyboard(
            [[(t("btn_continue", lang), "new:resume"), (t("btn_start_over", lang), "new:restart")]]
        )
    )


def voice_kb(lang: str) -> InlineKeyboardMarkup:
    return markup(
        inline_keyboard(
            [
                [(t("voice_none", lang), "voice:none")],
                [(t("voice_standard", lang), "voice:standard")],
                [(t("voice_premium", lang), "voice:premium")],
            ]
        )
    )
