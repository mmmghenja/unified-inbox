"""Очередь «правка / удалить у всех» своих сообщений (Ф14-8, docs/architecture.md — они главнее текста выше них).
Устройство — по образцу `reactions.py` (Ф10-3).

Одна строка на сообщение (`msg_op`, схема сообщения): целевое состояние — «текст = X» (`edit`) или «удалить
у всех» (`delete`). Новая операция заменяет прежнюю (`gen += 1`, новые попытки); правка поверх ЖДУЩЕГО
удаления — `DeletePending` (передумал — сначала «Отменить»). Итог виден в хабе (решение 2): неудача —
`state='failed'` с кодом причины, пока владелец не скроет или не повторит; каждая смена состояния строки двигает
`seq` сообщения (поле `op` в выдаче — `attach_ops`).

Правду о тексте и удалении пишет только источник (решение 3): здесь `message` не меняется НИКОГДА, кроме
`seq`. Ставит в очередь ТОЛЬКО ручка UI (`ui_only`, решение 4).

Вызывать только внутри `Db.write()` (кроме читателей `wait_state`, `attach_ops`, `op_of`); время — параметром
`now_ms`, как везде в core.
"""

import json
import sqlite3
from typing import Literal, NamedTuple

from . import draft_channels
from .db import db_of, next_seq

# fake — для тестов и стенда (`scripts/dev-demo.py`).
SOURCES = frozenset({"telegram", "slack", "fake", "mattermost", "whatsapp"})
EDIT_KINDS = frozenset({"text", "file"})
# П6: Telegram правит подпись только у этих вложений (кружок — `video` с `round` — нет).
TELEGRAM_EDITABLE_FILES = frozenset({"photo", "video", "document", "audio", "voice", "animation"})
TELEGRAM_TEXT_MAX_UTF16 = 4096
TELEGRAM_CAPTION_MAX_UTF16 = 1024
SLACK_TEXT_MAX = 4000
MATTERMOST_TEXT_MAX = 16383  # Ф40-1 D: `MaxPostSize` сервера, в символах (не UTF-16)
WHATSAPP_TEXT_MAX = 65536  # Ф62-1: предел моста (как у отправки), в символах
MAX_REVOKE_IDS = 10
# П5: лиз 120 с при таймауте `push` 60 с — `cancel` не проходит, пока запрос ещё мог дойти до источника.
LEASE_MS = 120_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_MS = 30_000
MAX_LEASE_LIMIT = 10
MAX_DETAIL_LEN = 200
FAIL_CODES = frozenset({"expired", "forbidden", "not_found", "too_long", "other"})
REPORT_OUTCOMES = frozenset({"done", "retry", "fail"})
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})


class MsgOpError(Exception):
    """Базовая ошибка очереди правок."""


class NotFound(MsgOpError):
    """Сообщения нет, оно скрыто у себя (П1), или строки очереди нет / она чужого аккаунта — 404."""


class NotEditable(MsgOpError):
    """Не своё, без `ext_id`, служебное, удалённое, в беседе-заготовке; для правки ещё пересланное, не
    текст/файл, у Telegram — вложение, подпись которого не правится (П6) — 409 `not_editable`."""


class Unsupported(MsgOpError):
    """Источник без правки (почта и прочие) — 409 `unsupported`."""


class DeletePending(MsgOpError):
    """Правка поверх ждущего удаления — 409 `delete_pending`."""


class InFlight(MsgOpError):
    """`cancel` при живом лизе — запрос мог уже уйти к источнику, 409 `in_flight`."""


class InvalidText(MsgOpError):
    """Пустой (по `strip()`) или длиннее лимита источника текст — 422. `code`: `empty` / `too_long`,
    `limit` — лимит источника (для `too_long`)."""

    def __init__(self, code: Literal["empty", "too_long"], limit: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.limit = limit


class InvalidBatch(MsgOpError):
    """`revoke`: пустой список, больше 10 id или сообщения разных бесед — 422, ничего не записано."""


class StaleLease(MsgOpError):
    """`lease_gen` не совпал (или строка уже не ждёт) — задание перехвачено новой выдачей, 409."""


class EnqueueResult(NamedTuple):
    status: Literal["queued", "unchanged"]
    channel_id: int


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `ingest._sql`: схема (`main`/`work`) и доверенные фрагменты, не ввод пользователя."""
    return template.format(schema=schema, **parts)


def _account(conn: sqlite3.Connection, account_id: int) -> tuple[str, str] | None:
    """`(схема, статус)` аккаунта или `None`."""
    row = conn.execute("SELECT db, status FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1]) if row is not None else None


def _attachments(attachments_json: str | None) -> list:
    try:
        value = json.loads(attachments_json) if attachments_json else []
    except ValueError:
        return []
    if isinstance(value, dict):
        value = [value]
    return value if isinstance(value, list) else []


def telegram_file_editable(attachments_json: str | None) -> bool:
    """П6: подпись правится, только если ВСЕ вложения — фото, видео (не кружок), документ, аудио, голосовое,
    анимация. Стикер, опрос, геоточка, контакт, `other`, кружок, пустой список — нет."""
    items = _attachments(attachments_json)
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in TELEGRAM_EDITABLE_FILES:
            return False
        if item.get("round") is True:
            return False
    return True


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def text_limit(source: str, message_kind: str) -> tuple[int, bool]:
    """Решение 6: `(лимит, в UTF-16 ли)`. Telegram — 4096 UTF-16 у текста, 1024 у подписи (`file`); Slack (и
    fake, как Slack) — 4000 символов до экранирования; Mattermost (Ф40-1 D) — 16383 символа."""
    if source == "telegram":
        return (TELEGRAM_CAPTION_MAX_UTF16 if message_kind == "file" else TELEGRAM_TEXT_MAX_UTF16), True
    if source == "mattermost":
        return MATTERMOST_TEXT_MAX, False
    if source == "whatsapp":
        return WHATSAPP_TEXT_MAX, False
    return SLACK_TEXT_MAX, False


def _check_text(source: str, message_kind: str, text: str) -> None:
    if not text.strip():
        raise InvalidText("empty")
    limit, utf16 = text_limit(source, message_kind)
    if (_utf16_len(text) if utf16 else len(text)) > limit:
        raise InvalidText("too_long", limit)


_MESSAGE_SQL = (
    "SELECT m.channel_id, m.ext_id, m.kind, m.deleted, m.hidden, m.is_me, m.forward_json, "
    "m.attachments_json, m.text, c.account_id, a.source, c.ext_id FROM {schema}.message m "
    "JOIN {schema}.channel c ON c.id = m.channel_id "
    "JOIN main.account a ON a.id = c.account_id WHERE m.id = ?"
)


class _Msg(NamedTuple):
    channel_id: int
    ext_id: str | None
    kind: str
    deleted: int
    hidden: int
    is_me: int
    forward_json: str | None
    attachments_json: str | None
    text: str | None
    account_id: int
    source: str
    channel_ext_id: str | None


def _load(conn: sqlite3.Connection, message_id: int) -> _Msg:
    row = conn.execute(_sql(db_of(message_id), _MESSAGE_SQL), (message_id,)).fetchone()
    if row is None:
        raise NotFound(f"message {message_id}")
    msg = _Msg(*row)
    if msg.hidden:  # П1: скрытое у себя — ни правки, ни «удалить у всех»
        raise NotFound(f"message {message_id}")
    if msg.source not in SOURCES:
        raise Unsupported(msg.source)
    # Ф12-1 A7: беседа-заготовка коннектору не видна.
    if (
        not msg.is_me
        or msg.ext_id is None
        or msg.kind == "event"
        or msg.deleted
        or draft_channels.is_temp_ext_id(msg.channel_ext_id)
    ):
        raise NotEditable(f"message {message_id}")
    return msg


def _check_editable(message_id: int, msg: _Msg) -> None:
    """Решение 5 + П6: правка — только текст/файл, не пересланное; Telegram — подпись не у всех вложений."""
    if msg.kind not in EDIT_KINDS or msg.forward_json:
        raise NotEditable(f"message {message_id}")
    if msg.source == "telegram" and msg.kind == "file" and not telegram_file_editable(msg.attachments_json):
        raise NotEditable(f"message {message_id}")
    if msg.source == "whatsapp" and (msg.kind == "file" or _attachments(msg.attachments_json)):
        # Ф62-1: мост правит только текст; подпись к файлу WhatsApp — не правится из хаба.
        raise NotEditable(f"message {message_id}")


def _bump(conn: sqlite3.Connection, schema: str, message_id: int) -> None:
    conn.execute(
        _sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (next_seq(conn), message_id)
    )


def _row(conn: sqlite3.Connection, schema: str, message_id: int) -> dict | None:
    cur = conn.execute(_sql(schema, "SELECT * FROM {schema}.msg_op WHERE message_id = ?"), (message_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row, strict=True))


def _upsert(
    conn: sqlite3.Connection, schema: str, message_id: int, msg: _Msg, op: str, text: str | None, now_ms: int
) -> None:
    """Вставка или перезапись строки (новая операция): `gen += 1`, попытки заново, ошибка снята; пауза
    источника не обходится (`not_before = max(now, pause_until)`); лиз не трогается — идущее задание
    доработает старую операцию, отчёт увидит смену `gen`."""
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.msg_op (message_id, channel_id, account_id, op, text, state, gen, "
            "not_before_ms, created_ms, updated_ms) VALUES (?, ?, ?, ?, ?, 'queued', 1, ?, ?, ?) "
            "ON CONFLICT (message_id) DO UPDATE SET channel_id = excluded.channel_id, "
            "account_id = excluded.account_id, op = excluded.op, text = excluded.text, state = 'queued', "
            "error_code = NULL, error_detail = NULL, gen = gen + 1, attempts = 0, "
            "not_before_ms = MAX(excluded.not_before_ms, pause_until_ms), updated_ms = excluded.updated_ms",
        ),
        (message_id, msg.channel_id, msg.account_id, op, text, now_ms, now_ms, now_ms),
    )
    _bump(conn, schema, message_id)


# --- A2: постановка ------------------------------------------------------------------------------------


def enqueue_edit(conn: sqlite3.Connection, message_id: int, text: str, now_ms: int) -> EnqueueResult:
    """A2. Видимость принципалу проверяет ручка (`queries.message_visible`) в той же транзакции. Текст
    хранится как прислан (без `strip`); пустота — по `strip()`. Строки нет и текст совпал с текущим —
    `unchanged`, ничего не пишется."""
    msg = _load(conn, message_id)
    _check_editable(message_id, msg)
    schema = db_of(message_id)
    existing = _row(conn, schema, message_id)
    if existing is not None and existing["op"] == "delete" and existing["state"] == "queued":
        raise DeletePending(f"message {message_id}")
    _check_text(msg.source, msg.kind, text)
    if existing is None and text == (msg.text or ""):
        return EnqueueResult("unchanged", msg.channel_id)
    _upsert(conn, schema, message_id, msg, "edit", text, now_ms)
    return EnqueueResult("queued", msg.channel_id)


def enqueue_delete(conn: sqlite3.Connection, message_ids: list[int], now_ms: int) -> int:
    """A2: 1..10 id одной беседы (альбом), всё-или-ничего — первая ошибка поднимается, ничего не записано.
    Удаление поверх правки заменяет её. Возвращает `channel_id` беседы."""
    ids = list(dict.fromkeys(message_ids))
    if not ids or len(ids) > MAX_REVOKE_IDS:
        raise InvalidBatch("1..10 id")
    loaded = [(mid, _load(conn, mid)) for mid in ids]
    channels = {msg.channel_id for _mid, msg in loaded}
    if len(channels) != 1:
        raise InvalidBatch("разные беседы")
    for mid, msg in loaded:
        _upsert(conn, db_of(mid), mid, msg, "delete", None, now_ms)
    return loaded[0][1].channel_id


def cancel(conn: sqlite3.Connection, message_id: int, now_ms: int) -> int:
    """«Отменить» (`queued`) / «Скрыть» (`failed`): строка снимается, `seq` сообщения новый. Живой лиз ЭТОЙ
    операции (`leased_gen == gen`) — `InFlight` (запрос мог уже уйти). Ревью Ф14-8: живой лиз СТАРОЙ операции
    (удаление нажато, пока правка у коннектора) новую не держит — строка снимается, новая операция не уйдёт,
    отчёт старой получит 404 (`NotFound`), её итог у источника придёт событием. Возвращает `channel_id`."""
    schema = db_of(message_id)
    row = _row(conn, schema, message_id)
    if row is None:
        raise NotFound(f"msg_op {message_id}")
    leased = row["lease_until_ms"] is not None and row["lease_until_ms"] > now_ms
    if leased and row["leased_gen"] == row["gen"]:
        raise InFlight(f"msg_op {message_id}")
    conn.execute(_sql(schema, "DELETE FROM {schema}.msg_op WHERE message_id = ?"), (message_id,))
    _bump(conn, schema, message_id)
    return row["channel_id"]


# --- выдача -------------------------------------------------------------------------------------------


def _resolve(conn: sqlite3.Connection, schema: str, account_id: int, message_id: int) -> dict | None:
    """Сообщение задания или `None` — строку надо удалить: сообщения/канала нет, чужой аккаунт, удалено у
    источника (любой op), скрыто у себя (П1 — любой op), без `ext_id`, беседа-заготовка."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.ext_id, m.kind, m.deleted, m.hidden, m.thread_ext_id, m.attachments_json, "
            "c.account_id, c.ext_id, c.kind, p.ext_id FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id "
            "LEFT JOIN {schema}.channel p ON p.id = c.parent_channel_id WHERE m.id = ?",
        ),
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    m_ext, m_kind, deleted, hidden, thread_ext, attachments_json, c_account, c_ext, c_kind, parent_ext = row
    if c_account != account_id or deleted or hidden or m_ext is None:
        return None
    if draft_channels.is_temp_ext_id(c_ext):
        return None
    return {
        "channel": {"ext_id": c_ext, "kind": c_kind, "parent_ext_id": parent_ext},
        "message_ext_id": m_ext,
        "thread_ext_id": thread_ext,
        "message_kind": m_kind,
        "has_files": bool(_attachments(attachments_json)),
    }


def _drop_row(conn: sqlite3.Connection, schema: str, message_id: int) -> None:
    conn.execute(_sql(schema, "DELETE FROM {schema}.msg_op WHERE message_id = ?"), (message_id,))
    exists = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.message WHERE id = ?"), (message_id,)
    ).fetchone()
    if exists is not None:
        _bump(conn, schema, message_id)


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """A2 + П5: задания своего аккаунта (по умолчанию одно), лиз 120 с; `failed` не выдаётся.
    `limited`/`auth_required` — пусто, строки ждут. Пропавшее/удалённое/скрытое после постановки — строка
    удаляется по пути (+ `seq` сообщения), не выдаётся."""
    info = _account(conn, account_id)
    if info is None:
        return []
    schema, status = info
    if status in _BLOCKED_STATUSES:
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    rows = conn.execute(
        _sql(
            schema,
            "SELECT message_id, op, text, gen FROM {schema}.msg_op WHERE account_id = ? AND state = 'queued' "
            "AND not_before_ms <= ? AND pause_until_ms <= ? "
            "AND (lease_until_ms IS NULL OR lease_until_ms <= ?) "
            "ORDER BY not_before_ms, message_id",
        ),
        (account_id, now_ms, now_ms, now_ms),
    ).fetchall()
    items: list[dict] = []
    for message_id, op, text, gen in rows:
        if len(items) >= limit:
            break
        resolved = _resolve(conn, schema, account_id, message_id)
        if resolved is None:
            _drop_row(conn, schema, message_id)
            continue
        lease_gen = conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.msg_op SET lease_until_ms = ?, lease_gen = lease_gen + 1, leased_gen = gen "
                "WHERE message_id = ? RETURNING lease_gen",
            ),
            (now_ms + LEASE_MS, message_id),
        ).fetchone()[0]
        item = {"message_id": message_id, "gen": gen, "lease_gen": lease_gen, "op": op, **resolved}
        if op == "edit":
            item["text"] = text
        items.append(item)
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait` (как `reactions.wait_state`): `(готово, ближайший срок в мс)`, только строки
    `queued`; `limited`/`auth_required` — `(False, None)` (полный таймаут, без горячего цикла)."""
    info = _account(conn, account_id)
    if info is None:
        return False, None
    schema, status = info
    if status in _BLOCKED_STATUSES:
        return False, None
    row = conn.execute(
        _sql(
            schema,
            "SELECT MIN(MAX(not_before_ms, pause_until_ms, COALESCE(lease_until_ms, 0))) "
            "FROM {schema}.msg_op WHERE account_id = ? AND state = 'queued'",
        ),
        (account_id,),
    ).fetchone()
    due = row[0] if row is not None else None
    if due is None:
        return False, None
    if due <= now_ms:
        return True, None
    return False, due


# --- отчёт --------------------------------------------------------------------------------------------


def backoff_ms(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки (с 1): 30 с, 60 с, 120 с, …"""
    return BACKOFF_BASE_MS * 2 ** (max(attempt, 1) - 1)


def _clip_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    return str(detail)[:MAX_DETAIL_LEN]


def report(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    message_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    code: str | None = None,
    detail: str | None = None,
    retry_after_ms: int | None = None,
) -> str:
    """A2: итог для журнала — `deleted` (сделано, строка снята), `released` (лиз снят, ждёт новая операция
    UI), `failed` (неудача, видна в хабе), `paused` (повтор позже), `gave_up` (пятая неудача -> `failed`/
    `other`). Нет строки или чужой аккаунт — `NotFound`; устаревший `lease_gen`, снятый лиз или строка уже
    не ждёт — `StaleLease`."""
    if outcome not in REPORT_OUTCOMES:
        raise ValueError(outcome)
    if outcome == "fail" and code not in FAIL_CODES:
        raise ValueError(code)
    schema = db_of(message_id)
    row = _row(conn, schema, message_id)
    if row is None or row["account_id"] != account_id:
        raise NotFound(f"msg_op {message_id}")
    # Отчёт — только по выданному и ещё не отчитанному заданию: повтор отчёта после `retry`/`fail` (лиз уже
    # снят) не засчитывает попытку дважды.
    if row["lease_gen"] != lease_gen or row["state"] != "queued" or row["lease_until_ms"] is None:
        raise StaleLease(f"msg_op {message_id}: lease_gen {lease_gen} != {row['lease_gen']}")
    same_target = row["leased_gen"] == row["gen"]

    def _release() -> str:
        conn.execute(
            _sql(schema, "UPDATE {schema}.msg_op SET lease_until_ms = NULL WHERE message_id = ?"),
            (message_id,),
        )
        return "released"

    def _fail(fail_code: str, fail_detail: str | None) -> None:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.msg_op SET state = 'failed', error_code = ?, error_detail = ?, "
                "lease_until_ms = NULL, updated_ms = ? WHERE message_id = ?",
            ),
            (fail_code, _clip_detail(fail_detail), now_ms, message_id),
        )
        _bump(conn, schema, message_id)

    if outcome == "done":
        if not same_target:
            return _release()
        conn.execute(_sql(schema, "DELETE FROM {schema}.msg_op WHERE message_id = ?"), (message_id,))
        _bump(conn, schema, message_id)
        return "deleted"

    if outcome == "fail":
        if not same_target:
            return _release()
        _fail(code, detail)
        return "failed"

    attempt = row["attempts"] + 1
    pause_until_ms = now_ms + max(retry_after_ms or 0, backoff_ms(attempt))
    if same_target:
        if attempt >= MAX_ATTEMPTS:
            conn.execute(
                _sql(schema, "UPDATE {schema}.msg_op SET attempts = ? WHERE message_id = ?"),
                (attempt, message_id),
            )
            _fail("other", f"{MAX_ATTEMPTS} попыток: {detail or 'повтор'}")
            return "gave_up"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.msg_op SET attempts = ?, lease_until_ms = NULL, pause_until_ms = ?, "
                "not_before_ms = ? WHERE message_id = ?",
            ),
            (attempt, pause_until_ms, pause_until_ms, message_id),
        )
        return "paused"
    # Попытка была по старой операции — новой её не засчитываем, но паузу источника соблюдаем.
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.msg_op SET lease_until_ms = NULL, pause_until_ms = ?, not_before_ms = ? "
            "WHERE message_id = ?",
        ),
        (pause_until_ms, max(row["not_before_ms"], pause_until_ms), message_id),
    )
    return "paused"


# --- поле `op` в выдаче ----------------------------------------------------------------------------------


def _op_dict(op: str, state: str, text: str | None, error_code: str | None, error_detail: str | None) -> dict:
    out: dict = {"kind": op, "state": state}
    if op == "edit":
        out["text"] = text
    out["error"] = {"code": error_code, "detail": error_detail} if state == "failed" else None
    return out


def attach_ops(conn: sqlite3.Connection, schema: str, messages: list[dict]) -> None:
    """A2: ОДНИМ запросом на страницу (одной схемы) кладёт в каждое сообщение `op` — `None` или
    `{"kind": "edit"|"delete", "state": "queued"|"failed", "text" (edit), "error": {code, detail} | None}`."""
    if not messages:
        return
    ids = [m["id"] for m in messages]
    found: dict[int, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            _sql(
                schema,
                "SELECT message_id, op, state, text, error_code, error_detail FROM {schema}.msg_op "
                "WHERE message_id IN ({ph})",
                ph=placeholders,
            ),
            chunk,
        ).fetchall()
        for message_id, op, state, text, error_code, error_detail in rows:
            found[message_id] = _op_dict(op, state, text, error_code, error_detail)
    for m in messages:
        m["op"] = found.get(m["id"])


def op_of(conn: sqlite3.Connection, message_id: int) -> dict | None:
    """`op` одного сообщения — для ответа ручки (UI кладёт его в стор сразу, без ожидания `/sync`)."""
    row = _row(conn, db_of(message_id), message_id)
    if row is None:
        return None
    return _op_dict(row["op"], row["state"], row["text"], row["error_code"], row["error_detail"])


def has_rows(
    conn: sqlite3.Connection,
    schema: str,
    *,
    message_ids: list[int] | None = None,
    channel_id: int | None = None,
) -> bool:
    """П1: есть ли строка `msg_op` (любое состояние) у этих сообщений / в этой беседе — скрытие у себя
    (`hiding`) тогда отвечает 409 `op_pending`."""
    if channel_id is not None:
        row = conn.execute(
            _sql(schema, "SELECT 1 FROM {schema}.msg_op WHERE channel_id = ? LIMIT 1"), (channel_id,)
        ).fetchone()
        return row is not None
    ids = message_ids or []
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            _sql(schema, "SELECT 1 FROM {schema}.msg_op WHERE message_id IN ({ph}) LIMIT 1", ph=placeholders),
            chunk,
        ).fetchone()
        if row is not None:
            return True
    return False
