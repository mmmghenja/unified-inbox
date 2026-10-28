"""Ф53-1 (`docs/architecture.md`): `hub feed push` — лента от внешней программы.

Тонкая обёртка над `POST /events` внутреннего сокета: сборка события, проверка «аккаунт — лента», коды выхода.
Ядро и схема не меняются. Секрет и текст сообщения в stderr не попадают.
"""

import asyncio
import re
import sys
import time

import httpx

from . import paths
from .connectors.base import CoreClient

_SOCKET_NAME = "internal.sock"
FEED_SOURCE = "feed"
MAX_TEXT = 8000
MAX_EXT = 200
_FEED_KEY = re.compile(r"[a-z0-9_-]{1,40}")

EXIT_OK = 0
EXIT_RETRY = 1  # ядро недоступно, таймаут, 5xx — скрипт повторит позже
EXIT_ARGS = 2  # плохие аргументы, событие отвергнуто, аккаунт не лента
EXIT_AUTH = 78  # нет файла секрета или 401


def clip_text(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def build_event(
    *,
    feed: str,
    feed_title: str,
    ext: str,
    ts: int,
    sender: str,
    text: str,
    note: bool,
    alert: bool = False,
) -> dict:
    channel_ext = f"feed:{feed}"
    return {
        "type": "upsert",
        "channel": {
            "ext_id": channel_ext,
            "kind": "task",
            "title": feed_title,
            "attrs": {"no_reply": True, "feed": feed},
        },
        "ext_id": ext,
        "ts": ts,
        "sender": {"ext_id": channel_ext, "name": sender},
        "is_me": False,
        "kind": "event" if note else "text",
        "text": text,
        # `--alert`: «лично владельцу» — у беседы-задачи такая запись поднимается до «реагирую» (`attention.py`).
        "mentions_me": alert,
    }


async def push(
    client: CoreClient,
    *,
    feed: str,
    feed_title: str | None,
    ext: str,
    ts: int,
    sender: str | None,
    text: str,
    note: bool,
    alert: bool = False,
) -> tuple[int, str]:
    """Один push. Возвращает `(код выхода, сообщение)`: при коде 0 — в stdout, иначе в stderr."""
    try:
        account = await client.get_account()
        if account.get("source") != FEED_SOURCE:
            return EXIT_ARGS, f"аккаунт не является лентой (source={account.get('source')!r}), push отклонён"
        title = feed_title or account.get("title") or f"Лента {feed}"
        event = build_event(
            feed=feed,
            feed_title=title,
            ext=ext,
            ts=ts,
            sender=sender or title,
            text=clip_text(text),
            note=note,
            alert=alert,
        )
        result = await client.push_events([event])
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 401:
            return EXIT_AUTH, "ядро отвергло секрет аккаунта (401)"
        if 400 <= code < 500:
            return EXIT_ARGS, f"ядро ответило {code}"
        return EXIT_RETRY, f"ядро ответило {code}"
    except httpx.HTTPError as exc:
        return EXIT_RETRY, f"ядро недоступно ({exc.__class__.__name__})"
    if result.get("dead", 0) >= 1:
        return EXIT_ARGS, "ядро отвергло событие"
    if result.get("applied", 0) >= 1:
        return EXIT_OK, "ok"
    if result.get("noop", 0) >= 1:
        return EXIT_OK, "noop"
    return EXIT_ARGS, "ядро не приняло событие"


def check_args(args) -> str | None:
    """Причина ошибки аргументов или `None`. Текст сообщения здесь не проверяется."""
    ext = args.ext
    if not isinstance(ext, str) or not ext.strip() or len(ext) > MAX_EXT:
        return "--ext: непустая строка до 200 символов"
    if not _FEED_KEY.fullmatch(args.feed):
        return "--feed: [a-z0-9_-], 1–40 символов"
    if args.note and getattr(args, "alert", False):
        return "--note и --alert вместе нельзя"
    return None


def run_push(args, *, transport: httpx.AsyncBaseTransport | None = None, stdin=None) -> int:
    """Тело `hub feed push`. `transport` — для тестов (ASGI вместо сокета)."""
    bad = check_args(args)
    if bad is not None:
        print(f"hub feed push: {bad}", file=sys.stderr)
        return EXIT_ARGS
    text = args.text
    if text is None:
        text = (stdin if stdin is not None else sys.stdin).read()
    if not text.strip():
        print("hub feed push: пустой текст (--text или stdin)", file=sys.stderr)
        return EXIT_ARGS

    key_path = paths.config_dir() / "connectors" / f"{args.account}.key"
    if not key_path.exists():
        print(f"hub feed push: {key_path} не найден — заведите аккаунт (`hub account add`)", file=sys.stderr)
        return EXIT_AUTH
    secret = key_path.read_text(encoding="utf-8").strip()
    ts = args.ts if args.ts is not None else int(time.time() * 1000)

    async def _run() -> tuple[int, str]:
        if transport is not None:
            client = CoreClient(secret=secret, transport=transport)
        else:
            client = CoreClient(socket_path=str(paths.data_dir() / _SOCKET_NAME), secret=secret)
        try:
            return await push(
                client,
                feed=args.feed,
                feed_title=args.feed_title,
                ext=args.ext,
                ts=ts,
                sender=args.sender,
                text=text,
                note=args.note,
                alert=getattr(args, "alert", False),
            )
        finally:
            await client.aclose()

    code, message = asyncio.run(_run())
    print(message, file=sys.stdout if code == EXIT_OK else sys.stderr)
    return code
