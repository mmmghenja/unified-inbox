"""Удаление у себя в хабе: сообщение и беседа целиком. Спецификация: docs/architecture.md–B3 и поправки П1–П6, П10 (они
главнее текста выше них).

Удаление ЛОГИЧЕСКОЕ (П10): строка сообщения остаётся (порядок, треды, сверка коннекторов), содержимое
очищается той же функцией, что у ретенции (`retention.clean_message_content`), плюс реакции; `purged = 1`,
`hidden = 1`.
Физически текст может оставаться в свободных страницах SQLite, WAL и бэкапах до их ротации — принято.
У источника (Telegram/Slack) ничего не удаляется: хаб никогда не пишет туда сам.

Вызывается ТОЛЬКО внутри `Db.write()` (как `outbox.py`/`channels.py`) — всё одной транзакцией, включая
удаление файлов медиа (правило ссылок `media.release_file`, как у медиа-куска). Видимость канала/сообщения
принципалу проверяет маршрут (`queries.channel_visible`) в той же транзакции.
"""

from typing import NamedTuple

from . import mail_reactions as mail_reactions_mod
from . import media as media_mod
from . import msg_ops as msg_ops_mod
from . import outbox as outbox_mod
from . import retention
from . import threads as threads_mod
from .db import db_of, next_seq

# П1: заготовку, которую ещё не отправляли (черновик, невыданная очередь), удаление отменяет сразу.
_CANCELLABLE_STATUSES = frozenset({"pending_approval", "queued"})
# П1: `outbox.text` очищается только в конечных статусах — строка очереди остаётся журналом отправки.
FINAL_OUTBOX_STATUSES = frozenset({"sent", "cancelled", "failed"})

_MESSAGE_COLUMNS = (
    "id",
    "channel_id",
    "ext_id",
    "thread_ext_id",
    "outbox_id",
    "hidden",
    "text",
    "entities_json",
    "attachments_json",
    "forward_json",
    "mail_to_json",
    "refs_json",
)


class HidingError(Exception):
    """Базовая ошибка удаления у себя."""


class NotFound(HidingError):
    """Сообщения или беседы нет — 404."""


class SendInProgress(HidingError):
    """Своё исходящее ещё отправляется или его итог неизвестен (`sending`/`unknown`, `queued`, которую
    коннектор уже брал) — 409, ничего не изменено (П1)."""


class OpPending(HidingError):
    """Ф14-8 П1: у сообщения (или в беседе) есть строка `msg_op` — правка/«удалить у всех» ждёт или не
    удалась; скрытое у себя её итог потерял бы (лента и `/sync` скрытое не отдают) — 409, ничего не изменено.
    Сначала «Отменить»/«Скрыть» операцию."""


class HideResult(NamedTuple):
    channel_id: int
    changed: bool  # False — уже было скрыто (повторный `DELETE` — 204, П12)
    count: int  # сколько сообщений скрыто этим вызовом


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в ingest.py/outbox.py — см. комментарий в ingest.py."""
    return template.format(schema=schema, **parts)


def _select_messages(conn, schema: str, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(
        _sql(
            schema,
            "SELECT {cols} FROM {schema}.message WHERE {where} ORDER BY id",
            cols=", ".join(_MESSAGE_COLUMNS),
            where=where,
        ),
        params,
    ).fetchall()
    return [dict(zip(_MESSAGE_COLUMNS, row, strict=True)) for row in rows]


def _outbox_status(conn, schema: str, outbox_id: int) -> str | None:
    row = conn.execute(
        _sql(schema, "SELECT status FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    return row[0] if row is not None else None


# П1 + ревью Ф8-4: «отправка может идти» — `sending`/`unknown` и `queued`, которую коннектор уже брал
# (`send_started_ts`): её `cancel` дал бы `unknown` с живым текстом (виден в `GET /outbox?status=unknown`
# любому bearer `read`, а `resolve_unknown(retry)` отправил бы «удалённое»). Один предикат на сообщение и на
# беседу.
_IN_FLIGHT_SQL = "(status IN ('sending', 'unknown') OR (status = 'queued' AND send_started_ts IS NOT NULL))"


def _check_sending(conn, schema: str, targets: list[dict]) -> None:
    """П1: ДО любых изменений — своё исходящее в полёте среди скрываемых -> `SendInProgress`."""
    for message in targets:
        if message["outbox_id"] is None:
            continue
        in_flight = conn.execute(
            _sql(
                schema, "SELECT 1 FROM {schema}.outbox WHERE id = ? AND {in_flight}", in_flight=_IN_FLIGHT_SQL
            ),
            (message["outbox_id"],),
        ).fetchone()
        if in_flight is not None:
            raise SendInProgress(f"outbox {message['outbox_id']}: отправка ещё идёт")


def _settle_outbox(conn, schema: str, outbox_id: int, now_ms: int) -> None:
    """П1: черновик/невыданная очередь — отмена тем же `outbox.cancel` (выданную коннектору `queued` сюда не
    пускает `_check_sending`); в конечном статусе текст строки очереди стирается, сама строка остаётся
    журналом отправки. Перед стиранием — хеш текста (`text_sha256`), если его ещё нет (строка старше
    миграции 0012): по нему эхо из источника склеится со скрытой заготовкой, а не ляжет новой видимой
    строкой."""
    status = _outbox_status(conn, schema, outbox_id)
    if status in _CANCELLABLE_STATUSES:
        status = outbox_mod.cancel(conn, outbox_id, now_ms=now_ms)
    if status in FINAL_OUTBOX_STATUSES:
        outbox_mod.backfill_text_sha256(conn, schema, outbox_id)
        # Ф19-1 C: вместе с текстом — исходный текст правленого черновика.
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET text = '', draft_text = NULL WHERE id = ?"), (outbox_id,)
        )
        # Ф10-4 П2: вместе с текстом — имя файла (в `file_json` остаётся `{kind, size}`).
        outbox_mod.scrub_file_json(conn, schema, outbox_id)


def _scrub(conn, schema: str, message: dict) -> None:
    """Одно сообщение: содержимое — функцией очистки ретенции, реакции — явно (П5), `purged = 1`,
    `hidden = 1`, новый `seq`; версии правок (C) и строки `media` с файлами (правило ссылок) — прочь."""
    content = {
        key: message[key]
        for key in ("text", "entities_json", "attachments_json", "forward_json", "mail_to_json")
    }
    content["refs_json"] = message["refs_json"]
    retention.clean_message_content(content)
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.message SET text = ?, entities_json = ?, attachments_json = ?, "
            "forward_json = ?, mail_to_json = ?, refs_json = ?, reactions_json = NULL, "
            # Ф44-1a П5: кнопки бота — чужой текст и сырой callback, стираются вместе с содержимым.
            "buttons_json = NULL, buttons_ts = NULL, purged = 1, "
            "hidden = 1, seq = ? WHERE id = ?",
        ),
        (
            content["text"],
            content["entities_json"],
            content["attachments_json"],
            content["forward_json"],
            content["mail_to_json"],
            content["refs_json"],
            next_seq(conn),
            message["id"],
        ),
    )
    conn.execute(_sql(schema, "DELETE FROM {schema}.message_edit WHERE message_id = ?"), (message["id"],))
    # Ф15-1 A1: входящие реакции письма (адреса отправителей) и пометки реакций сессий — вместе с сообщением.
    mail_reactions_mod.drop_for_message(conn, schema, message["id"])
    media_rows = conn.execute(
        _sql(schema, "SELECT id, account_id, sha256 FROM {schema}.media WHERE message_id = ?"),
        (message["id"],),
    ).fetchall()
    for media_id, account_id, sha256 in media_rows:
        conn.execute(_sql(schema, "DELETE FROM {schema}.media WHERE id = ?"), (media_id,))
        media_mod.release_file(conn, schema, account_id, sha256)


def _hide(conn, schema: str, targets: list[dict], now_ms: int) -> None:
    """Общий шаг сообщения и беседы: заготовки (П1) -> очистка -> карточки агента (П4)."""
    for message in targets:
        if message["outbox_id"] is not None:
            _settle_outbox(conn, schema, message["outbox_id"], now_ms)
    for message in targets:
        _scrub(conn, schema, message)
    ids = [m["id"] for m in targets]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.agent_item SET status = 'dismissed' "
                "WHERE last_message_id IN ({ph}) AND status IN ('new', 'seen')",
                ph=placeholders,
            ),
            chunk,
        )


def _bump_channel(conn, schema: str, channel_id: int) -> None:
    """Непрочитанное и видимость беседы изменились — новый `seq` каналу (UI узнаёт через `/sync`)."""
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET seq = ? WHERE id = ?"), (next_seq(conn), channel_id)
    )


def hide_message(conn, message_id: int, *, now_ms: int) -> HideResult:
    """B2: `DELETE /messages/{id}`. Родитель треда уносит с собой все текущие ответы (П6: новые ответы,
    пришедшие потом, видимы — родитель в панели треда заглушкой); ответ — пересчёт сводки треда
    (`touch_and_bump`, как удаление отправителем). Уже скрытое — `changed=False`, ничего не меняется."""
    schema = db_of(message_id)
    found = _select_messages(conn, schema, "id = ?", (message_id,))
    if not found:
        raise NotFound(f"message {message_id}")
    message = found[0]
    channel_id = message["channel_id"]
    if message["hidden"]:
        return HideResult(channel_id, changed=False, count=0)

    targets = [message]
    ext_id, thread_ext_id = message["ext_id"], message["thread_ext_id"]
    is_reply = threads_mod.is_reply(thread_ext_id, ext_id)
    is_parent = ext_id is not None and not is_reply
    if is_parent:
        targets += _select_messages(
            conn,
            schema,
            "channel_id = ? AND thread_ext_id = ? AND id <> ? AND hidden = 0 "
            "AND (ext_id IS NULL OR ext_id <> thread_ext_id)",
            (channel_id, ext_id, message_id),
        )

    _check_sending(conn, schema, targets)
    if msg_ops_mod.has_rows(conn, schema, message_ids=[m["id"] for m in targets]):  # Ф14-8 П1
        raise OpPending(f"message {message_id}: есть операция правки/удаления")
    _hide(conn, schema, targets, now_ms)

    if is_reply:
        threads_mod.touch_and_bump(conn, schema, channel_id, thread_ext_id, now_ms)
    elif is_parent and threads_mod.thread_exists(conn, schema, channel_id, ext_id):
        threads_mod.touch_and_bump(conn, schema, channel_id, ext_id, now_ms)
    _bump_channel(conn, schema, channel_id)
    return HideResult(channel_id, changed=True, count=len(targets))


def hide_channel(conn, channel_id: int, *, now_ms: int) -> HideResult:
    """B3 + П2: `DELETE /channels/{id}` — ОДНОЙ транзакцией. Граница — позиция `(ts, ext_sort)` последнего
    сообщения ИСТОЧНИКА в беседе (заготовки outbox не в счёт; сообщений источника нет — «сейчас»; повторное
    удаление границу только двигает вперёд);
    все сообщения беседы, включая ответы тредов, — как B2; открытые карточки беседы закрываются (П4). Правила
    раскладки, ручная привязка к проекту и режим внимания не трогаются."""
    schema = db_of(channel_id)
    channel = conn.execute(
        _sql(schema, "SELECT hidden_before_ts, hidden_before_sort FROM {schema}.channel WHERE id = ?"),
        (channel_id,),
    ).fetchone()
    if channel is None:
        raise NotFound(f"channel {channel_id}")

    # П1: исходящее в полёте — даже то, чья заготовка уже скрыта (отправка всё равно идёт в эту беседу).
    in_flight = conn.execute(
        _sql(
            schema,
            "SELECT id FROM {schema}.outbox WHERE channel_id = ? AND {in_flight} LIMIT 1",
            in_flight=_IN_FLIGHT_SQL,
        ),
        (channel_id,),
    ).fetchone()
    if in_flight is not None:
        raise SendInProgress(f"outbox {in_flight[0]}: отправка ещё идёт")
    if msg_ops_mod.has_rows(conn, schema, channel_id=channel_id):  # Ф14-8 П1
        raise OpPending(f"channel {channel_id}: есть операция правки/удаления")

    # Ревью Ф8-4 (блокер): граница — только по сообщениям ИСТОЧНИКА (`ext_id IS NOT NULL`). У заготовки outbox
    # без `ext_id` (черновик агента, отменённое, failed) `ts` — часы хаба: такая граница уехала бы вперёд и
    # проглотила бы, очистив навсегда, настоящее сообщение, отправленное до удаления, но доехавшее позже.
    last = conn.execute(
        _sql(
            schema,
            "SELECT ts, ext_sort FROM {schema}.message WHERE channel_id = ? AND ext_id IS NOT NULL "
            "ORDER BY ts DESC, ext_sort DESC LIMIT 1",
        ),
        (channel_id,),
    ).fetchone()
    boundary = (last[0], last[1]) if last is not None else (now_ms, 0)
    if channel[0] is not None:
        boundary = max(boundary, (channel[0], channel[1] or 0))

    targets = _select_messages(conn, schema, "channel_id = ? AND hidden = 0", (channel_id,))
    _check_sending(conn, schema, targets)
    _hide(conn, schema, targets, now_ms)
    mail_reactions_mod.drop_for_channel(conn, schema, channel_id)  # Ф15-1 A1
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.agent_item SET status = 'dismissed' "
            "WHERE channel_id = ? AND status IN ('new', 'seen')",
        ),
        (channel_id,),
    )
    thread_ext_ids = [
        row[0]
        for row in conn.execute(
            _sql(schema, "SELECT ext_id FROM {schema}.thread WHERE channel_id = ?"), (channel_id,)
        ).fetchall()
    ]
    for thread_ext_id in thread_ext_ids:
        threads_mod.touch_and_bump(conn, schema, channel_id, thread_ext_id, now_ms)
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.channel SET hidden_before_ts = ?, hidden_before_sort = ?, seq = ? WHERE id = ?",
        ),
        (boundary[0], boundary[1], next_seq(conn), channel_id),
    )
    return HideResult(channel_id, changed=True, count=len(targets))


def covered_by_boundary(conn, schema: str, channel_id: int, ts: int, ext_sort: int) -> bool:
    """П2: входящее с позицией `(ts, ext_sort)` не новее границы удалённой беседы — ложится сразу скрытым и
    очищенным (старое, пришедшее повторно сверкой или окном, не возвращает беседу)."""
    row = conn.execute(
        _sql(schema, "SELECT hidden_before_ts, hidden_before_sort FROM {schema}.channel WHERE id = ?"),
        (channel_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return False
    return (ts, ext_sort) <= (row[0], row[1] or 0)
