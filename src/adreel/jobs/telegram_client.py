"""Minimal synchronous Telegram Bot API client for the worker.

The worker only needs a handful of methods (send/edit messages, send video/document,
download files), so it talks to the Bot API directly instead of running an asyncio bot.
It works against api.telegram.org or a self-hosted Bot API server; in local mode
``getFile`` returns an absolute path on that server's disk, which we read through a
shared volume (``telegram_local_files_dir``).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from adreel.log import get_logger

log = get_logger(__name__)

STANDARD_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile limit on api.telegram.org
STANDARD_UPLOAD_LIMIT = 50 * 1024 * 1024
LOCAL_UPLOAD_LIMIT = 2000 * 1024 * 1024


class TelegramError(RuntimeError):
    def __init__(
        self, method: str, code: int, description: str, retry_after: int | None = None
    ) -> None:
        self.method, self.code, self.description, self.retry_after = (
            method,
            code,
            description,
            retry_after,
        )
        super().__init__(f"{method}: {code} {description}")

    @property
    def transient(self) -> bool:
        return self.code == 429 or self.code >= 500


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        local_mode: bool = False,
        local_files_dir: Path | None = None,
        server_files_dir: str = "/var/lib/telegram-bot-api",
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise TelegramError("init", 0, "ADREEL_TELEGRAM_BOT_TOKEN is not set")
        self._token = token
        self._base = api_base.rstrip("/")
        self.local_mode = local_mode
        self._local_dir = local_files_dir
        self._server_dir = server_files_dir.rstrip("/")
        self._http = client or httpx.Client(timeout=timeout_s)

    @property
    def upload_limit(self) -> int:
        return LOCAL_UPLOAD_LIMIT if self.local_mode else STANDARD_UPLOAD_LIMIT

    # ------------------------------------------------------------------ core

    def call(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        files: dict[str, tuple[str, Any, str]] | None = None,
        attempts: int = 3,
    ) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        payload = {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v)
            for k, v in (data or {}).items()
            if v is not None
        }
        last: TelegramError | None = None
        for attempt in range(1, attempts + 1):
            try:
                if files:
                    for f in files.values():  # rewind file objects on retries
                        if hasattr(f[1], "seek"):
                            f[1].seek(0)
                    resp = self._http.post(url, data=payload, files=files)
                else:
                    resp = self._http.post(url, data=payload)
                body = resp.json()
            except (httpx.TransportError, ValueError) as exc:
                last = TelegramError(method, 503, f"{type(exc).__name__}: {exc}")
            else:
                if body.get("ok"):
                    return body.get("result")
                params = body.get("parameters") or {}
                last = TelegramError(
                    method,
                    int(body.get("error_code") or resp.status_code),
                    str(body.get("description")),
                    params.get("retry_after"),
                )
                if not last.transient:
                    raise last
            if attempt < attempts:
                time.sleep(min(float(last.retry_after or 2**attempt), 30.0))
        assert last is not None
        raise last

    # ------------------------------------------------------------------ messages

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = self.call(
            "sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return result

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        try:
            self.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": reply_markup,
                },
                attempts=1,
            )
        except TelegramError as exc:
            if "message is not modified" in exc.description:
                return
            raise

    def delete_message(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramError):  # best effort (too old, already deleted)
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, attempts=1)

    def send_video(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str,
        reply_markup: dict[str, Any] | None,
        width: int,
        height: int,
        duration: float,
    ) -> dict[str, Any]:
        with path.open("rb") as f:
            result: dict[str, Any] = self.call(
                "sendVideo",
                {
                    "chat_id": chat_id,
                    "caption": caption,
                    "reply_markup": reply_markup,
                    "width": width,
                    "height": height,
                    "duration": round(duration),
                    "supports_streaming": "true",
                },
                files={"video": (path.name, f, "video/mp4")},
            )
        return result

    def send_document(
        self, chat_id: int, path: Path, *, caption: str, filename: str
    ) -> dict[str, Any]:
        with path.open("rb") as f:
            result: dict[str, Any] = self.call(
                "sendDocument",
                {"chat_id": chat_id, "caption": caption, "disable_content_type_detection": "true"},
                files={"document": (filename, f, "video/mp4")},
            )
        return result

    # ------------------------------------------------------------------ files

    def download_file(self, file_id: str, dst: Path) -> Path:
        info = self.call("getFile", {"file_id": file_id})
        file_path = str(info.get("file_path") or "")
        if not file_path:
            raise TelegramError(
                "getFile", 400, "no file_path (file too big for this Bot API server?)"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        if self.local_mode and file_path.startswith("/"):
            src = Path(file_path)
            if not src.is_file() and self._local_dir is not None:
                rel = file_path.removeprefix(self._server_dir).lstrip("/")
                src = self._local_dir / rel
            if not src.is_file():
                raise TelegramError(
                    "getFile", 404, f"local file not visible to the worker: {file_path}"
                )
            shutil.copyfile(src, tmp)
        else:
            url = f"{self._base}/file/bot{self._token}/{file_path}"
            with self._http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise TelegramError("download", resp.status_code, f"HTTP {resp.status_code}")
                with tmp.open("wb") as out:
                    for chunk in resp.iter_bytes(1 << 16):
                        out.write(chunk)
        tmp.replace(dst)
        return dst
