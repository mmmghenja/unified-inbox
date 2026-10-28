"""Входящие реакции писем (Ф15-1, docs/architecture.md и
поправка П6).

Письмо-реакция Gmail (часть `text/vnd.google.email-reaction+json`) или RFC 9078 — не сообщение ленты, а
событие `mail_reaction`: строка `mail_reaction` (беседа, Message-ID цели, Message-ID письма-реакции, эмодзи,
отправитель). Значок на письме-цели — `message.reactions_json`, пересчитанный из ВСЕХ строк цели: по эмодзи
(без U+FE0F) `count` — число разных отправителей, `me` — есть ли моя. Реакция, пришедшая раньше письма-цели,
применяется, когда цель придёт (`refresh_target` из `ingest._apply_message`). Снять реакцию у источника
нельзя — строки только копятся.

Вызывать только внутри `Db.write()`; SQL — с подставленной схемой (`main`/`work`), как в `ingest.py`.
"""

import json
import sqlite3

import emoji as emoji_lib

from . import draft_channels
from .db import next_seq

MAX_EMOJI_LEN = 64
_VS16 = chr(0xFE0F)


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `ingest._sql`: схема и доверенные фрагменты, не ввод пользователя."""
    return template.format(schema=schema, **parts)


def _key(value: str) -> str:
    return value.replace(_VS16, "")


def valid_emoji(value: object) -> bool:
    """Ровно один эмодзи (как у Gmail: одна графема TS51) — длина ≤ 64, `emoji.is_emoji` целиком."""
    return isinstance(value, str) and 0 < len(value) <= MAX_EMOJI_LEN and emoji_lib.is_emoji(value)


def _channel_id(conn: sqlite3.Connection, schema: str, account_id: int, ext_id: str) -> int | None:
    """Только поиск (как `ingest._find_channel_id`): событие реакции беседу не создаёт."""
    if draft_channels.is_temp_ext_id(ext_id):
        return None
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, ext_id),
    ).fetchone()
    return row[0] if row is not None else None


BY_MAX = 100  # Ф23-2: сколько имён поставивших хранится в элементе реакции


def aggregate(rows: list[tuple[str, str, int]], names: dict[str, str] | None = None) -> list[dict]:
    """`[(emoji, sender_key, is_me)]` по порядку прихода -> `[{"emoji", "count", "me", "by"?}]`: по эмодзи без
    U+FE0F, `count` — разные отправители, `emoji` — как в первой строке. Ф23-2: `by` — чужие отправители по
    порядку прихода: имя контакта из `names` (адрес -> имя), нет имени — сам адрес; своя реакция в `by` не
    пишется; лишние сверх `BY_MAX` — в `by_more`."""
    names = names or {}
    order: list[str] = []
    groups: dict[str, dict] = {}
    for value, sender_key, is_me in rows:
        if not value:  # ревью 3: строка-позиция реакции на скрытое у себя письмо — без эмодзи и отправителя
            continue
        key = _key(value)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"emoji": value, "senders": set(), "me": False, "by": []}
            order.append(key)
        if sender_key not in group["senders"] and not is_me:
            group["by"].append(names.get(sender_key) or sender_key)
        group["senders"].add(sender_key)
        group["me"] = group["me"] or bool(is_me)
    out = []
    for k in order:
        group = groups[k]
        entry: dict = {"emoji": group["emoji"], "count": len(group["senders"]), "me": group["me"]}
        if group["by"]:
            entry["by"] = group["by"][:BY_MAX]
            if len(group["by"]) > BY_MAX:
                entry["by_more"] = len(group["by"]) - BY_MAX
        out.append(entry)
    return out


def _contact_names(
    conn: sqlite3.Connection, schema: str, channel_id: int, addresses: set[str]
) -> dict[str, str]:
    """Ф23-2: адрес (нижний регистр) -> имя контакта аккаунта беседы; нет имени — адреса нет в словаре."""
    if not addresses:
        return {}
    ph = ",".join("?" * len(addresses))
    rows = conn.execute(
        _sql(
            schema,
            "SELECT lower(ct.ext_id), ct.name FROM {schema}.contact ct "
            "JOIN {schema}.channel ch ON ch.account_id = ct.account_id WHERE ch.id = ? "
            "AND lower(ct.ext_id) IN ({ph}) AND ct.name IS NOT NULL AND ct.name != ''",
            ph=ph,
        ),
        (channel_id, *sorted(addresses)),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def refresh_target(conn: sqlite3.Connection, schema: str, channel_id: int, target_ext_id: str) -> int:
    """A4: пересчитать `reactions_json` письма-цели из строк `mail_reaction`. Нет цели, скрыта у себя, нет
    строк, значок не изменился — 0; иначе новый `seq` сообщения."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT id, reactions_json, hidden FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",
        ),
        (channel_id, target_ext_id),
    ).fetchone()
    if row is None or row[2]:
        return 0
    message_id, old = row[0], row[1]
    rows = conn.execute(
        _sql(
            schema,
            "SELECT emoji, sender_key, is_me FROM {schema}.mail_reaction WHERE channel_id = ? "
            "AND target_ext_id = ? ORDER BY ts, src_ext_id",
        ),
        (channel_id, target_ext_id),
    ).fetchall()
    if not rows:
        return 0
    names = _contact_names(conn, schema, channel_id, {r[1] for r in rows if r[1] and not r[2]})
    new = json.dumps(aggregate([(r[0], r[1], r[2]) for r in rows], names), sort_keys=True, ensure_ascii=False)
    if new == old:
        return 0
    seq = next_seq(conn)
    conn.execute(
        _sql(schema, "UPDATE {schema}.message SET reactions_json = ?, seq = ? WHERE id = ?"),
        (new, seq, message_id),
    )
    return seq


def apply_event(conn: sqlite3.Connection, schema: str, account_id: int, ev) -> tuple[str, int]:
    """A4: событие `mail_reaction`. Беседы нет — `noop` (не создаётся); цель есть и скрыта у себя — `noop`,
    пишется только позиция для маркера прочтения, без эмодзи и отправителя (как реакции Ф8-4 П5; ревью 3);
    повтор `src_ext_id` — `noop`; иначе строка и пересчёт цели.
    Кривой эмодзи — исключение (событие ядовитое, `dead_letter`)."""
    if not valid_emoji(ev.emoji):
        raise ValueError("mail_reaction: не одиночный эмодзи")
    channel_id = _channel_id(conn, schema, account_id, ev.channel.ext_id)
    if channel_id is None:
        return "noop", 0
    target = conn.execute(
        _sql(schema, "SELECT hidden FROM {schema}.message WHERE channel_id = ? AND ext_id = ?"),
        (channel_id, ev.target_ext_id),
    ).fetchone()
    if target is not None and target[0]:
        # Ф15-1 ревью 3 (П6): цель удалена у себя — реакция не пишется (Ф8-4 П5), но позиция письма-реакции
        # нужна маркеру прочтения: строка без эмодзи и отправителя (ПДн не хранятся), значок не меняется.
        conn.execute(
            _sql(
                schema,
                "INSERT OR IGNORE INTO {schema}.mail_reaction (channel_id, target_ext_id, src_ext_id, emoji, "
                "sender_key, is_me, ts) VALUES (?, ?, ?, '', '', 0, ?)",
            ),
            (channel_id, ev.target_ext_id, ev.src_ext_id, ev.ts),
        )
        return "noop", 0
    cur = conn.execute(
        _sql(
            schema,
            "INSERT OR IGNORE INTO {schema}.mail_reaction (channel_id, target_ext_id, src_ext_id, emoji, "
            "sender_key, is_me, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ),
        (
            channel_id,
            ev.target_ext_id,
            ev.src_ext_id,
            ev.emoji,
            ev.sender.ext_id.strip().lower(),
            int(ev.sender.is_me),
            ev.ts,
        ),
    )
    if cur.rowcount == 0:
        return "noop", 0
    return "applied", refresh_target(conn, schema, channel_id, ev.target_ext_id)


def position(conn: sqlite3.Connection, schema: str, channel_id: int, src_ext_id: str) -> int | None:
    """П6: `ts` письма-реакции беседы (маркер прочтения почты может указывать на него) или `None`."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT ts FROM {schema}.mail_reaction WHERE channel_id = ? AND src_ext_id = ?",
        ),
        (channel_id, src_ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def drop_for_message(conn: sqlite3.Connection, schema: str, message_id: int) -> None:
    """A1: сообщение скрыто у себя / очищено — его входящие реакции (адреса отправителей) и пометки сессий
    уходят."""
    row = conn.execute(
        _sql(schema, "SELECT channel_id, ext_id FROM {schema}.message WHERE id = ?"), (message_id,)
    ).fetchone()
    if row is not None and row[1] is not None:
        conn.execute(
            _sql(schema, "DELETE FROM {schema}.mail_reaction WHERE channel_id = ? AND target_ext_id = ?"),
            (row[0], row[1]),
        )
    conn.execute(_sql(schema, "DELETE FROM {schema}.reaction_by_session WHERE message_id = ?"), (message_id,))


def drop_for_channel(conn: sqlite3.Connection, schema: str, channel_id: int) -> None:
    """A1: беседа удалена у себя — все её входящие реакции (и к письмам, которых ещё нет)."""
    conn.execute(_sql(schema, "DELETE FROM {schema}.mail_reaction WHERE channel_id = ?"), (channel_id,))
