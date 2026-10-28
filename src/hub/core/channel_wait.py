"""Ожидание новых сообщений одной беседы (живой разговор через сессию). Спецификация:
docs/architecture.md

Курсор — глобальный `seq` (решение 1): он ловит и поздно вставленное старое сообщение, и правки, удаления,
реакции. Будят только НОВЫЕ сообщения других людей (решение 2); правки, свои сообщения, заготовки и служебные
события не будят, но идут в ближайшую выдачу со своим `wait_class`. Ожидание — long-poll на том же
`Notifier`, что у SSE (решение 3): снять поколение -> прочитать БД -> есть будящее — ответ, нет — ждать
пробуждения до дедлайна.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from . import queries
from .feed import Notifier
from .security import Principal

DEFAULT_LIMIT = 100
TIMEOUT_MIN_S = 1
TIMEOUT_MAX_S = 120

WAIT_NEW = "new"
WAIT_MINE = "mine"
WAIT_EVENT = "event"
WAIT_CHANGED = "changed"


class WaitPage(NamedTuple):
    messages: list[dict]
    cursor: int
    woke: bool
    more: bool
    from_now: bool
    # Точка отсчёта выдачи: переданный `after` или (при `from_now`) максимум seq беседы на момент вызова.
    after: int


class SessionRevoked(Exception):
    """Сессия или токен отозваны (или удалённый доступ выключен) во время ожидания — роут отдаёт 401."""


class ClientGone(Exception):
    """Клиент отключился, пока ждал, — отвечать некому."""


def wait_class(message: dict, *, after: int, skip_mine: bool) -> str:
    """Решение 2: `new` — новое (`first_seq > after`) сообщение другого человека (или своё при
    `skip_mine=False`); `event` — новое служебное событие; `mine` — своё (включая заготовки) новое или
    изменённое; `changed` — изменение уже показанного (правка, удаление, реакция, догрузка медиа)."""
    is_new = message["first_seq"] > after
    if message["kind"] == "event":
        return WAIT_EVENT if is_new else WAIT_CHANGED
    if is_new and (not message["is_me"] or not skip_mine):
        return WAIT_NEW
    if message["is_me"]:
        return WAIT_MINE
    return WAIT_CHANGED


def transcript_hold_ms(page: WaitPage) -> int:
    """Ф14-6b: будящие (`new`) сообщения выдачи ждут расшифровку голосового — сколько ещё держать ответ
    (самое долгое `transcript_wait_ms`; поле кладёт `media.attach_media`, 0 — служба не берёт задания или
    ждать нечего). Страница не последняя (`more`) — не держать: клиент дочитает."""
    if page.more:
        return 0
    return max(
        (m.get("transcript_wait_ms") or 0 for m in page.messages if m.get("wait_class") == WAIT_NEW),
        default=0,
    )


def poll(
    conn,
    principal: Principal,
    channel_id: int,
    *,
    after: int | None,
    thread: str | None,
    skip_mine: bool,
    limit: int = DEFAULT_LIMIT,
) -> WaitPage:
    """Одна читающая выборка в одной транзакции (как `queries.changes_since`). Невидимая беседа —
    `queries.NotFound`. `after is None` — «с этого момента»: курсор = текущий максимум seq беседы."""
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN")
    try:
        from_now = after is None
        if after is None:
            after = queries.channel_max_seq(conn, principal, channel_id, thread=thread)
        page = queries.channel_messages_after(
            conn, principal, channel_id, after=after, thread=thread, limit=limit
        )
    finally:
        if own_tx:
            conn.execute("ROLLBACK")
    for message in page.messages:
        message["wait_class"] = wait_class(message, after=after, skip_mine=skip_mine)
    woke = page.more or any(m["wait_class"] == WAIT_NEW for m in page.messages)
    cursor = page.last_seq if page.last_seq is not None else after
    return WaitPage(page.messages, cursor, woke, page.more, from_now, after)


async def wait(
    db,
    notifier: Notifier,
    principal: Principal,
    channel_id: int,
    *,
    after: int | None,
    thread: str | None,
    skip_mine: bool,
    timeout_s: float,
    clock: Callable[[], float] | None = None,
    still_valid: Callable[[], bool] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> WaitPage:
    """Цикл решения 3. Каждый `poll` идёт от ОДНОЙ точки отсчёта (первый `poll` без `after` её фиксирует),
    поэтому в ответе по пробуждению есть и накопившиеся за ожидание правки и свои. По дедлайну отдаётся
    последний `poll` как есть (`woke = False`). `clock` — монотонные секунды для ДЛИТЕЛЬНОСТИ ожидания (по
    умолчанию часы event loop, как у heartbeat `feed.stream`); не срок — часы хаба тут не нужны.

    На каждом пробуждении: `still_valid()` ложно -> `SessionRevoked`; `is_disconnected()` истинно ->
    `ClientGone`."""
    if clock is None:
        clock = asyncio.get_running_loop().time
    deadline = clock() + timeout_s
    from_now = after is None
    while True:
        gen = notifier.gen  # ДО чтения БД — notify() между чтением и wait() не потеряется
        with db.reader() as conn:
            page = poll(
                conn, principal, channel_id, after=after, thread=thread, skip_mine=skip_mine, limit=limit
            )
        after = page.after
        if from_now:
            page = page._replace(from_now=True)
        remaining = deadline - clock()
        held_ms = transcript_hold_ms(page)
        if page.woke and not held_ms:
            return page
        if remaining <= 0:
            # Ф14-6b «Мелкое»: вышел таймаут самого ожидания — отдать как есть (в выдаче «расшифровка идёт»).
            return page
        await notifier.wait(gen, min(remaining, held_ms / 1000) if held_ms else remaining)
        if notifier.closed:
            return page  # Ф31-1 П5: ядро останавливается — последний `poll` как есть (как по таймауту)
        if still_valid is not None and not still_valid():
            raise SessionRevoked
        if is_disconnected is not None and await is_disconnected():
            raise ClientGone


def to_json(page: WaitPage, *, channel_id: int, waited_ms: int) -> dict:
    """Ответ ручек ожидания (A2/A3)."""
    return {
        "channel_id": channel_id,
        "after": page.after,
        "cursor": page.cursor,
        "woke": page.woke,
        "more": page.more,
        "from_now": page.from_now,
        "waited_ms": waited_ms,
        "messages": page.messages,
    }
