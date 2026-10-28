"""Обвязка тестов Ф21-1 (люди): контакты и сообщения со всеми нужными колонками напрямую в БД, проход
связывания и пересчёт статистики с явным временем."""

import json

from hub.core import people, people_stats
from hub.core.db import db_of, next_seq

DAY_MS = 24 * 60 * 60 * 1000
MIN_MS = 60 * 1000
NOW_MS = 1_700_000_000_000


def account(db, source, *, schema="main", title=None, config=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, title, config_json) VALUES (?, ?, ?, ?)",
                (source, schema, title, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )


def channel(
    db, account_id, ext_id, *, kind="dm", schema="main", project_id=None, title=None, meta=None
) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {schema}.channel (account_id, ext_id, kind, project_id, title, first_seen_ts, "
                "meta_json) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (account_id, ext_id, kind, project_id, title, json.dumps(meta) if meta is not None else None),
            ).lastrowid
        )
    )


def contact(db, account_id, ext_id, name=None, handle=None, *, is_me=0, schema="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {schema}.contact (account_id, ext_id, name, handle, is_me) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, ext_id, name, handle, is_me),
            ).lastrowid
        )
    )


_counter = {"n": 0}


def msg(
    db,
    channel_id,
    *,
    ts,
    sender=None,
    me=False,
    text="сообщение",
    kind="text",
    ext_id=None,
    thread=None,
    reply_to=None,
    mentions_me=0,
    mentions_broadcast=0,
    mail_to=None,
    reactions=None,
    is_bot=0,
    hidden=0,
    deleted=0,
    purged=0,
    outbox_id=None,
    stub=False,
) -> int:
    """Сообщение. `stub=True` — заготовка outbox (`ext_id = NULL`)."""
    schema = db_of(channel_id)
    if ext_id is None and not stub:
        _counter["n"] += 1
        ext_id = f"x{_counter['n']}"

    def _fn(conn):
        seq = next_seq(conn)
        return conn.execute(
            f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, ts, "
            "sender_contact_id, is_me, kind, text, thread_ext_id, reply_to_ext_id, mentions_me, "
            "mentions_broadcast, mail_to_json, "
            "reactions_json, is_bot, hidden, deleted, purged, outbox_id) VALUES (?, ?, "
            f"(SELECT account_id FROM {schema}.channel WHERE id = ?), "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (
                seq,
                seq,
                channel_id,
                channel_id,
                ext_id,
                ts,
                sender,
                1 if me else 0,
                kind,
                text,
                thread,
                reply_to,
                mentions_me,
                mentions_broadcast,
                json.dumps(mail_to) if mail_to is not None else None,
                json.dumps(reactions, ensure_ascii=False) if reactions is not None else None,
                is_bot,
                hidden,
                deleted,
                purged,
                outbox_id,
            ),
        ).lastrowid

    return db.write_sync(_fn)


def outbox_row(db, channel_id, *, origin="api", text="t", draft_text=None, status="sent") -> int:
    schema = db_of(channel_id)
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {schema}.outbox (channel_id, account_id, text, origin, api_token_id, status, "
                "created_ts, draft_text) VALUES (?, "
                f"(SELECT account_id FROM {schema}.channel WHERE id = ?), ?, ?, 0, ?, 0, ?)",
                (channel_id, channel_id, text, origin, status, draft_text),
            ).lastrowid
        )
    )


def slack_people(db, account_id, items, *, schema="work") -> None:
    def _fn(conn):
        conn.execute(
            f"INSERT OR REPLACE INTO {schema}.account_meta (account_id, key, value_json, updated_ts) "
            "VALUES (?, 'slack_people:0', ?, 0)",
            (account_id, json.dumps(items)),
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {schema}.account_meta (account_id, key, value_json, updated_ts) "
            "VALUES (?, 'slack_people', ?, 0)",
            (account_id, json.dumps({"parts": 1, "count": len(items)})),
        )

    db.write_sync(_fn)


def link(db, now_ms=NOW_MS) -> dict:
    return db.write_sync(lambda conn: people.link_all(conn, now_ms))


def recompute(db, now_ms=NOW_MS) -> int | None:
    return people_stats.recompute(db, now_ms)


def contact_row(db, contact_id) -> dict:
    with db.reader() as conn:
        cur = conn.execute(
            f"SELECT * FROM {db_of(contact_id)}.contact WHERE id = ?",  # noqa: S608
            (contact_id,),
        )
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def person_of(db, contact_id):
    return contact_row(db, contact_id)["person_id"]


def stats_of(db, contact_id) -> dict | None:
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT known, last_ts, seen_ts, in_total, out_total, in_30d, out_30d, stats_json "  # noqa: S608
            f"FROM {db_of(contact_id)}.contact_stats WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
    if row is None:
        return None
    stats = json.loads(row[7])
    stats["row"] = dict(
        zip(
            ("known", "last_ts", "seen_ts", "in_total", "out_total", "in_30d", "out_30d"),
            row[:7],
            strict=True,
        )
    )
    return stats


def persons(db) -> set[int]:
    with db.reader() as conn:
        return {r[0] for r in conn.execute("SELECT id FROM main.person")}


def not_same(db) -> set[tuple[int, int]]:
    with db.reader() as conn:
        return {(a, b) for a, b in conn.execute("SELECT a, b FROM main.person_not_same")}
