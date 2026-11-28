"""Phase 3 unit tests: parsing, i18n catalogue, Telegram client, storage, queue."""

from __future__ import annotations

import json
import string
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from adreel.bot.parsing import (
    clean_text,
    parse_caption_fixes,
    parse_handle,
    parse_hex_color,
    parse_phone,
    parse_price,
)
from adreel.config import Settings
from adreel.db.session import async_url, sync_url
from adreel.i18n import TEXTS, is_skip, t
from adreel.jobs.telegram_client import TelegramClient, TelegramError
from adreel.storage import LocalStorage, StorageError, clip_key
from adreel.telegram_ui import parse_preview_callback, preview_keyboard, progress_bar, status_text

# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("1200", 1200.0),
        ("1,200 ብር", 1200.0),
        ("ETB 99.50", 99.5),
        ("450birr", 450.0),
        (" 3 200 ", 3200.0),
        ("abc", None),
        ("0", None),
        ("-5", None),
        ("1.234", None),
        ("", None),
    ],
)
def test_parse_price(text: str, want: float | None) -> None:
    assert parse_price(text) == want


def test_parse_misc() -> None:
    assert parse_hex_color("1f7a4d") == "#1F7A4D" and parse_hex_color("#12345") is None
    assert parse_phone("0911 234 567") == "+251911234567" and parse_phone("12") is None
    assert parse_handle("t.me/my_shop") == "@my_shop" and parse_handle("@ab") is None
    assert (
        clean_text("  a   b ", 5) == "a b"
        and clean_text("", 5) is None
        and clean_text("x" * 6, 5) is None
    )
    assert is_skip("ዝለል") and is_skip(" Skip ") and not is_skip("hello")


def test_parse_caption_fixes() -> None:
    assert parse_caption_fixes("3: ሰላም\n\n12 - hello there\n፪) ቡና") == [
        (3, "ሰላም"),
        (12, "hello there"),
        (2, "ቡና"),
    ]
    for bad in ("ሰላም", "3 ሰላም", "", "a: b"):
        with pytest.raises(ValueError):
            parse_caption_fixes(bad)


# ---------------------------------------------------------------- i18n


def _fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def test_every_text_in_both_languages_with_same_placeholders() -> None:
    for key, entry in TEXTS.items():
        assert set(entry) == {"am", "en"}, key
        assert entry["am"].strip() and entry["en"].strip(), key
        assert _fields(entry["am"]) == _fields(entry["en"]), key


def test_t_formats_and_falls_back() -> None:
    assert t("clip_received", "en", n=2, max=5) == "Clip 2/5 received ✅"
    assert t("clip_received", "xx", n=1, max=5).startswith("ቪዲዮ 1/5")


def test_preview_callbacks_roundtrip_and_fit_telegram_limit() -> None:
    pid = uuid.uuid4()
    kb = preview_keyboard(pid, "am")
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert all(len(d.encode()) <= 64 for d in datas)
    assert [parse_preview_callback(d).action for d in datas] == ["approve", "fix", "redo"]  # type: ignore[union-attr]
    assert (
        parse_preview_callback("pv:x:" + str(pid)) is None
        and parse_preview_callback("pv:a:nope") is None
    )


def test_status_text() -> None:
    assert progress_bar(0.5) == "▓▓▓▓▓░░░░░"
    assert status_text("render:compose", 0.42, "en") == "⏳ Rendering the video… 42%\n▓▓▓▓░░░░░░"
    assert "ንግግሩን" in status_text("transcribe", 0.2, "am")


# ---------------------------------------------------------------- telegram client


def _client(handler: Any, **kw: Any) -> TelegramClient:
    return TelegramClient(
        "123:abc", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
    )


def test_send_message_encodes_markup() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    msg = _client(handler).send_message(5, "ሰላም", reply_markup=preview_keyboard(uuid.uuid4(), "am"))
    assert msg == {"message_id": 7} and seen["url"].endswith("/bot123:abc/sendMessage")
    assert (
        seen["body"]["text"] == "ሰላም"
        and json.loads(seen["body"]["reply_markup"])["inline_keyboard"]
    )


def test_rate_limit_retry_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 1},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    assert (
        _client(handler).call("deleteMessage", {"chat_id": 1, "message_id": 2}) is True
        and len(calls) == 2
    )

    def bad(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "chat not found"}
        )

    with pytest.raises(TelegramError) as ei:
        _client(bad).send_message(1, "x")
    assert not ei.value.transient

    def same(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message is not modified",
            },
        )

    _client(same).edit_message_text(1, 2, "x")  # swallowed


def test_download_standard_and_local(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "videos/a.mp4"}})
        assert req.url.path == "/file/bot123:abc/videos/a.mp4"
        return httpx.Response(200, content=b"VIDEO")

    out = _client(handler).download_file("fid", tmp_path / "a.mp4")
    assert out.read_bytes() == b"VIDEO"

    server_dir = tmp_path / "shared"
    (server_dir / "bot1" / "videos").mkdir(parents=True)
    (server_dir / "bot1" / "videos" / "b.mp4").write_bytes(b"LOCAL")

    def local(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"file_path": "/var/lib/telegram-bot-api/bot1/videos/b.mp4"},
            },
        )

    c = _client(local, local_mode=True, local_files_dir=server_dir)
    assert c.download_file("fid", tmp_path / "b.mp4").read_bytes() == b"LOCAL"
    assert c.upload_limit > 50 * 1024 * 1024


def test_client_requires_token() -> None:
    with pytest.raises(TelegramError):
        TelegramClient("")


# ---------------------------------------------------------------- storage, db urls, queue


def test_local_storage(tmp_path: Path) -> None:
    s = LocalStorage(tmp_path / "store")
    src = tmp_path / "x.mp4"
    src.write_bytes(b"data")
    key = clip_key(3, "p", 1)
    s.put_file(src, key)
    assert s.exists(key) and s.get_file(key, tmp_path / "y.mp4").read_bytes() == b"data"
    s.delete(key)
    assert not s.exists(key)
    for bad in ("../etc/passwd", "/abs", "a\\b", ""):
        with pytest.raises(StorageError):
            s.exists(bad)
    with pytest.raises(StorageError):
        s.get_file("missing.mp4", tmp_path / "z")


def test_db_urls() -> None:
    assert sync_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert async_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert async_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert sync_url("sqlite+aiosqlite:///x.db") == "sqlite:///x.db"


def test_inline_queue_runs_job_off_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from adreel.jobs import queue as q

    ran: list[tuple[str, str]] = []
    done = threading.Event()

    def fake_run(job_id: str, settings: Settings | None = None) -> None:
        ran.append((job_id, threading.current_thread().name))
        done.set()

    monkeypatch.setattr("adreel.jobs.tasks.run_project_job", fake_run)
    queue = q.build_queue(Settings(queue_backend="inline"))
    assert queue.enqueue("job-1") is None
    assert done.wait(5) and ran[0][0] == "job-1" and ran[0][1].startswith("adreel-job")


def test_rq_enqueue_sets_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis
    import redis

    from adreel.jobs.queue import TASK_PATH, RQJobQueue

    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: fake))
    q = RQJobQueue(Settings(job_timeout_s=600, job_retries=2))
    rq_id = q.enqueue("abc")
    from rq.job import Job

    job = Job.fetch(rq_id, connection=fake)  # type: ignore[arg-type]
    assert rq_id == "adreel-abc" and job.func_name == TASK_PATH and job.args == ("abc",)
    assert job.timeout == 600 and job.retries_left == 2 and job.retry_intervals == [30, 120]
