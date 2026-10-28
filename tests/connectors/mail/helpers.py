"""Общие помощники тестов почты: письма в `FakeGmail`, выборки из БД, шпион пачек ядра."""

from __future__ import annotations

import json

from .fakegmail import FakeGmail, make_email, new_msgid

ME = "me@gmail.com"


def incoming(
    gm: FakeGmail,
    *,
    thrid: int | None = None,
    subject: str = "Проект",
    body: str = "Привет!",
    labels=("\\Inbox",),
    flags=(),
    msgid: str | None = "auto",
    from_: str = "Пётр Петров <petr@example.com>",
    to: str = ME,
    in_reply_to: str | None = None,
    references: str | None = None,
    reply_to: str | None = None,
    categories=(),
    date_ts: int | None = None,
):
    mid = new_msgid("in") if msgid == "auto" else msgid
    raw = make_email(
        msgid=mid,
        from_=from_,
        to=to,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        references=references,
        reply_to=reply_to,
        date_ts=date_ts or 1_790_000_000,
    )
    return gm.add(raw, thrid=thrid, labels=labels, flags=flags, categories=categories, date_ts=date_ts)


def mine(gm: FakeGmail, *, thrid: int | None = None, subject: str = "Re: Проект", body: str = "Ответ", **kw):
    kw.setdefault("labels", ("\\Sent",))
    kw.setdefault("flags", ("\\Seen",))
    return incoming(
        gm,
        thrid=thrid,
        subject=subject,
        body=body,
        from_=f"владелец <{ME}>",
        to=kw.pop("to", "petr@example.com"),
        **kw,
    )


def messages(db, schema: str = "main") -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            f"SELECT c.ext_id, m.ext_id, m.ts, m.is_me, m.text, m.seq, m.reply_to_ext_id, m.kind, "
            f"m.mentions_me, m.attachments_json, m.outbox_id FROM {schema}.message m "
            f"JOIN {schema}.channel c ON c.id = m.channel_id WHERE m.deleted = 0 ORDER BY m.ts, m.id"
        ).fetchall()
    keys = ("thread", "ext_id", "ts", "is_me", "text", "seq", "reply_to", "kind", "mentions_me", "att", "oid")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def channels(db, schema: str = "main") -> dict[str, dict]:
    with db.reader() as conn:
        rows = conn.execute(
            f"SELECT ext_id, kind, title, meta_json, last_read_ext, project_id, map_reason, seq "
            f"FROM {schema}.channel"
        ).fetchall()
    out = {}
    for ext_id, kind, title, meta_json, last_read, project_id, reason, seq in rows:
        meta = json.loads(meta_json) if meta_json else {}
        out[ext_id] = {
            "kind": kind,
            "title": title,
            "attrs": meta.get("attrs"),
            "last_read": last_read,
            "project_id": project_id,
            "map_reason": reason,
            "seq": seq,
        }
    return out


def max_seq(db, schema: str = "main") -> int:
    with db.reader() as conn:
        a = conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {schema}.message").fetchone()[0]
        b = conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {schema}.channel").fetchone()[0]
    return max(a, b)


async def core_cursor(client) -> dict | None:
    raw = await client.get_cursor()
    return json.loads(raw["value"]) if raw else None


def spy_push(client) -> list:
    calls: list = []
    orig = client.push_events

    async def _push(events, cursor=None):
        r = await orig(events, cursor=cursor)
        calls.append((list(events), cursor, r))
        return r

    client.push_events = _push
    return calls


async def init_cursor(connector) -> None:
    """Первый запуск на пустом ящике: курсор создан, стартовая загрузка пуста и закончена."""
    await connector.sync("start")
