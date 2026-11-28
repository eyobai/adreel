"""Telegram UI pieces shared by the bot (aiogram) and the worker (plain Bot API calls).

Keyboards are plain Bot API dicts so the worker can send them without aiogram; the bot
converts them with ``InlineKeyboardMarkup.model_validate``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from adreel.i18n import t

PreviewAction = Literal["approve", "fix", "redo", "change"]
_ACTION_CODES: dict[str, PreviewAction] = {"a": "approve", "f": "fix", "r": "redo", "c": "change"}


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def preview_keyboard(project_id: uuid.UUID | str, lang: str) -> dict[str, Any]:
    pid = str(project_id)
    return inline_keyboard(
        [
            [(t("btn_approve", lang), f"pv:a:{pid}")],
            [(t("btn_change", lang), f"pv:c:{pid}")],
            [(t("btn_fix", lang), f"pv:f:{pid}"), (t("btn_redo", lang), f"pv:r:{pid}")],
        ]
    )


@dataclass(frozen=True)
class PreviewCallback:
    action: PreviewAction
    project_id: uuid.UUID


def parse_preview_callback(data: str | None) -> PreviewCallback | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "pv" or parts[1] not in _ACTION_CODES:
        return None
    try:
        return PreviewCallback(_ACTION_CODES[parts[1]], uuid.UUID(parts[2]))
    except ValueError:
        return None


# ---------------------------------------------------------------------------- progress

_STAGE_LABEL = {
    "download": "stage_download",
    "analyze": "stage_analyze",
    "normalize": "stage_analyze",
    "transcribe": "stage_transcribe",
    "plan": "stage_plan",
    "voiceover": "stage_voiceover",
    "segments": "stage_render",
    "compose": "stage_render",
    "render": "stage_render",
    "done": "stage_upload",
    "upload": "stage_upload",
}


def stage_label(stage: str, lang: str) -> str:
    base = stage.split(":")[-1]  # "render:compose" -> "compose"
    key = _STAGE_LABEL.get(base) or _STAGE_LABEL.get(stage.split(":")[0], "stage_render")
    return t(key, lang)


def progress_bar(frac: float, width: int = 10) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "▓" * filled + "░" * (width - filled)


def status_text(stage: str, frac: float, lang: str) -> str:
    frac = max(0.0, min(1.0, frac))
    return t(
        "status_line",
        lang,
        label=stage_label(stage, lang),
        pct=int(frac * 100),
        bar=progress_bar(frac),
    )
