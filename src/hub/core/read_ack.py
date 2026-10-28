"""Очередь «прочитано в хабе -> прочитано в источнике» (Ф8-7, docs/architecture.md — они главнее текста выше них).

Одна строка на канал (`read_ack`, схема канала): новая отметка UI перезаписывает цель (`gen += 1`), задержка
5 с от последней отметки, но не дольше 20 с от первой невыполненной (`first_ms`, сбрасывается при выдаче).
Ставит в очередь ТОЛЬКО `mark_channel_read` (UI сдвинул маркер) — события `read` источника, чтение агентами и
`/sync` ничего не ставят.

Выдача подбирает ДЕЙСТВУЮЩУЮ цель (П1): последнее по позиции `(ts, ext_sort, ext_id)` сообщение канала не
дальше цели UI, у которого есть `ext_id` источника, не скрытое, не ответ в треде без `thread_broadcast`, а у
почты — ещё и не своё (`is_me = 0`: `ts` своего письма может быть по часам хаба). Цель устарела — маркер
канала уже СТРОГО дальше цели UI (источник сообщил, что прочитано дальше) — строка удаляется, не выдаётся.

Вызывать только внутри `Db.write()` (кроме читателя `wait_state`); время — параметром `now_ms`, как везде в
core.
"""

import json
import sqlite3
from typing import Literal

from . import draft_channels
from . import threads as threads_mod
from .db import db_of

# fake — для тестов и стенда (`scripts/dev-demo.py`).
# Ф40-1 D: Mattermost — как Slack (цель — не ответ треда без показа в ленте, свои сообщения годятся).
SOURCES = frozenset({"telegram", "slack", "mail", "fake", "mattermost", "whatsapp"})
DELAY_MS = 5_000
MAX_DELAY_MS = 20_000
LEASE_MS = 60_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_MS = 30_000
MAX_LEASE_LIMIT = 10
REPORT_OUTCOMES = frozenset({"done", "retry", "drop"})
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})

EnqueueResult = Literal["queued", "off", "unsupported"]


class ReadAckError(Exception):
    """Базовая ошибка отчёта."""


class NotFound(ReadAckError):
    """Строки нет или она чужого аккаунта — 404."""


class StaleLease(ReadAckError):
    """`lease_gen` не совпал — задание перехвачено новой выдачей, 409."""


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `ingest._sql`: схема (`main`/`work`) и доверенные фрагменты, не ввод пользователя."""
    return template.format(schema=schema, **parts)


def read_ack_off(config_json: str | None) -> bool:
    """Выключатель аккаунта (решение 4): `config_json.read_ack` — JSON `false`. Всё прочее (нет ключа, `true`,
    битый JSON) — включено."""
    if not config_json:
        return False
    try:
        config = json.loads(config_json)
    except ValueError:
        return False
    return isinstance(config, dict) and config.get("read_ack") is False


def _account(conn: sqlite3.Connection, account_id: int) -> tuple[str, str, str, str | None] | None:
    """`(схема, статус, источник, config_json)` аккаунта или `None`."""
    row = conn.execute(
        "SELECT db, status, source, config_json FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row is not None else None


# --- A2: постановка ------------------------------------------------------------------------------------


def enqueue(conn: sqlite3.Connection, channel_id: int, message_id: int, now_ms: int) -> EnqueueResult:
    """A2 + П4: вызывается из `mark_channel_read` в той же транзакции и только при `changed=True`. Возвращает
    значение поля `read_ack` аудита `channel_read`: `queued|off|unsupported`. Лиз не трогается — идущее
    задание доработает старую цель, отчёт увидит смену `gen`."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT c.account_id, a.source, a.config_json, c.ext_id FROM {schema}.channel c "
            "JOIN main.account a ON a.id = c.account_id WHERE c.id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if row is None:
        return "unsupported"
    account_id, source, config_json, channel_ext_id = row
    if source not in SOURCES or draft_channels.is_temp_ext_id(channel_ext_id):
        return "unsupported"  # Ф12-1 A7: беседа-заготовка коннектору не видна
    if read_ack_off(config_json):
        return "off"

    existing = conn.execute(
        _sql(schema, "SELECT first_ms, pause_until_ms FROM {schema}.read_ack WHERE channel_id = ?"),
        (channel_id,),
    ).fetchone()
    first_ms = existing[0] if existing is not None and existing[0] is not None else now_ms
    pause_until_ms = existing[1] if existing is not None else 0
    not_before_ms = max(pause_until_ms, min(now_ms + DELAY_MS, first_ms + MAX_DELAY_MS))
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.read_ack (channel_id, account_id, message_id, gen, first_ms, "
            "not_before_ms) VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET "
            "account_id = excluded.account_id, message_id = excluded.message_id, gen = gen + 1, "
            "attempts = 0, first_ms = excluded.first_ms, not_before_ms = excluded.not_before_ms",
        ),
        (channel_id, account_id, message_id, first_ms, not_before_ms),
    )
    return "queued"


# --- A3: выдача -----------------------------------------------------------------------------------------


def _position(ts: int, ext_sort: int | None, ext_id: str | None) -> tuple[int, int, str]:
    """Позиция как у `channels.advance_read_marker`: заготовка без `ext_id` — пустая строка."""
    return (ts, ext_sort or 0, ext_id or "")


def _resolve(
    conn: sqlite3.Connection, schema: str, account_id: int, source: str, channel_id: int, message_id: int
) -> tuple[dict, dict] | None:
    """`(channel, message)` задания или `None` — строку надо удалить (канала/цели нет, цель устарела, П1:
    действующей цели не нашлось)."""
    channel = conn.execute(
        _sql(
            schema,
            "SELECT c.account_id, c.ext_id, c.kind, p.ext_id, c.last_read_ts, c.last_read_sort, "
            "c.last_read_ext FROM {schema}.channel c "
            "LEFT JOIN {schema}.channel p ON p.id = c.parent_channel_id WHERE c.id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if channel is None or channel[0] != account_id or draft_channels.is_temp_ext_id(channel[1]):
        return None
    _acc, ext_id, kind, parent_ext_id, lr_ts, lr_sort, lr_ext = channel

    target = conn.execute(
        _sql(schema, "SELECT channel_id, ts, ext_sort, ext_id FROM {schema}.message WHERE id = ?"),
        (message_id,),
    ).fetchone()
    if target is None or target[0] != channel_id:
        return None
    target_pos = _position(target[1], target[2], target[3])
    # Решение 3: источник уже ушёл дальше цели UI (прочитано на телефоне) — толкать нечего.
    if lr_ts is not None and _position(lr_ts, lr_sort, lr_ext) > target_pos:
        return None

    mine_filter = "AND is_me = 0 " if source == "mail" else ""
    effective = conn.execute(
        _sql(
            schema,
            "SELECT ext_id, ts, ext_sort, thread_ext_id, is_me FROM {schema}.message "
            "WHERE channel_id = ? AND ext_id IS NOT NULL AND hidden = 0 AND {top_level} {mine}"
            "AND (ts, ext_sort, ext_id) <= (?, ?, ?) "
            "ORDER BY ts DESC, ext_sort DESC, ext_id DESC LIMIT 1",
            top_level=threads_mod.top_level_sql("thread_ext_id", "ext_id", "thread_broadcast"),
            mine=mine_filter,
        ),
        (channel_id, *target_pos),
    ).fetchone()
    if effective is None:
        return None
    m_ext_id, m_ts, m_sort, m_thread, m_is_me = effective
    return (
        {"ext_id": ext_id, "kind": kind, "parent_ext_id": parent_ext_id},
        {
            "ext_id": m_ext_id,
            "ts": m_ts,
            "ext_sort": m_sort,
            "thread_ext_id": m_thread,
            "is_me": bool(m_is_me),
        },
    )


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """A3 + П1/П2/П7: задания своего аккаунта (по умолчанию одно), лиз 60 с. `read_ack=false` — строки
    аккаунта удаляются; `limited`/`auth_required` — пусто, строки ждут. Устаревшие строки удаляются по
    пути."""
    info = _account(conn, account_id)
    if info is None:
        return []
    schema, status, source, config_json = info
    if read_ack_off(config_json):
        conn.execute(_sql(schema, "DELETE FROM {schema}.read_ack WHERE account_id = ?"), (account_id,))
        return []
    if status in _BLOCKED_STATUSES:
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    rows = conn.execute(
        _sql(
            schema,
            "SELECT channel_id, message_id, gen FROM {schema}.read_ack WHERE account_id = ? "
            "AND not_before_ms <= ? AND (lease_until_ms IS NULL OR lease_until_ms <= ?) "
            "ORDER BY not_before_ms, channel_id",
        ),
        (account_id, now_ms, now_ms),
    ).fetchall()
    items: list[dict] = []
    for channel_id, message_id, gen in rows:
        if len(items) >= limit:
            break
        resolved = _resolve(conn, schema, account_id, source, channel_id, message_id)
        if resolved is None:
            conn.execute(_sql(schema, "DELETE FROM {schema}.read_ack WHERE channel_id = ?"), (channel_id,))
            continue
        channel, message = resolved
        lease_gen = conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.read_ack SET lease_until_ms = ?, lease_gen = lease_gen + 1, "
                "leased_gen = gen, first_ms = NULL WHERE channel_id = ? RETURNING lease_gen",
            ),
            (now_ms + LEASE_MS, channel_id),
        ).fetchone()[0]
        items.append(
            {
                "channel_id": channel_id,
                "gen": gen,
                "lease_gen": lease_gen,
                "channel": channel,
                "message": message,
            }
        )
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait` (A5 + П2): `(готово, ближайший срок в мс)`. «Готово» — есть строка, которую `lease`
    взял бы в работу прямо сейчас (устаревшие он удалит сам — ложное «готово» стоит одного пустого `lease`).
    Срок — `max(not_before_ms, lease_until_ms)` ближайшей строки, только если он в будущем и только когда
    аккаунт вообще может получить задания (`read_ack=false`, `limited`, `auth_required` — `(False, None)`:
    `wait` держит полный таймаут, горячего цикла нет)."""
    info = _account(conn, account_id)
    if info is None:
        return False, None
    schema, status, _source, config_json = info
    if status in _BLOCKED_STATUSES or read_ack_off(config_json):
        return False, None
    row = conn.execute(
        _sql(
            schema,
            "SELECT MIN(MAX(not_before_ms, COALESCE(lease_until_ms, 0))) FROM {schema}.read_ack "
            "WHERE account_id = ?",
        ),
        (account_id,),
    ).fetchone()
    due = row[0] if row is not None else None
    if due is None:
        return False, None
    if due <= now_ms:
        return True, None
    return False, due


# --- A4: отчёт ------------------------------------------------------------------------------------------


def backoff_ms(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки (с 1): 30 с, 60 с, 120 с, …"""
    return BACKOFF_BASE_MS * 2 ** (max(attempt, 1) - 1)


def report(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    channel_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    retry_after_ms: int | None = None,
) -> str:
    """A4 + П4. Итог для журнала: `deleted` (строка снята), `released` (лиз снят, ждёт новая цель UI),
    `paused` (повтор позже), `gave_up` (пятая неудача — строка снята)."""
    if outcome not in REPORT_OUTCOMES:
        raise ValueError(outcome)
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT account_id, gen, leased_gen, lease_gen, attempts, not_before_ms FROM {schema}.read_ack "
            "WHERE channel_id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if row is None or row[0] != account_id:
        raise NotFound(f"read_ack {channel_id}")
    _acc, gen, leased_gen, row_lease_gen, attempts, not_before_ms = row
    if row_lease_gen != lease_gen:
        raise StaleLease(f"read_ack {channel_id}: lease_gen {lease_gen} != {row_lease_gen}")
    same_target = leased_gen == gen

    if outcome in ("done", "drop"):
        if same_target:
            conn.execute(_sql(schema, "DELETE FROM {schema}.read_ack WHERE channel_id = ?"), (channel_id,))
            return "deleted"
        conn.execute(
            _sql(schema, "UPDATE {schema}.read_ack SET lease_until_ms = NULL WHERE channel_id = ?"),
            (channel_id,),
        )
        return "released"

    attempt = attempts + 1
    pause_until_ms = now_ms + max(retry_after_ms or 0, backoff_ms(attempt))
    if same_target:
        if attempt >= MAX_ATTEMPTS:
            conn.execute(_sql(schema, "DELETE FROM {schema}.read_ack WHERE channel_id = ?"), (channel_id,))
            return "gave_up"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.read_ack SET attempts = ?, lease_until_ms = NULL, pause_until_ms = ?, "
                "not_before_ms = ? WHERE channel_id = ?",
            ),
            (attempt, pause_until_ms, pause_until_ms, channel_id),
        )
        return "paused"
    # П4: попытка была по старой цели — новой цели попытку не засчитываем, но паузу источника соблюдаем.
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.read_ack SET lease_until_ms = NULL, pause_until_ms = ?, not_before_ms = ? "
            "WHERE channel_id = ?",
        ),
        (pause_until_ms, max(not_before_ms, pause_until_ms), channel_id),
    )
    return "paused"
