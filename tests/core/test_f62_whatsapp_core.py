"""Ф62-1 D: ядро для WhatsApp — «прочитано собеседником» по времени (id не число), одна реакция на человека,
правка только текста, новый адресат только по номеру со своим пределом `wa_phone_daily`.
Номера — выдуманные."""

from __future__ import annotations

import json

import pytest

from hub.core import msg_ops, new_recipient, reactions
from hub.core.ingest import apply_batch


def _account(db, source: str = "whatsapp", config: dict | None = None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )


def _upsert(ext_id: str, ts: int, *, is_me: bool = True, attachments=None) -> dict:
    return {
        "type": "upsert",
        "channel": {"ext_id": "555@lid", "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "привет",
        "mentions_me": False,
        "attachments": attachments,
    }


def _apply(db, account_id: int, events: list[dict]):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=1000)
    )


def _marker(db) -> int | None:
    with db.reader() as conn:
        return conn.execute("SELECT peer_read_sort FROM main.channel WHERE ext_id = '555@lid'").fetchone()[0]


def _message_id(db, ext_id: str) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def test_peer_read_by_message_time(db):
    account = _account(db)
    _apply(db, account, [_upsert("3EB0AAAA", 1_791_000_000_100), _upsert("3EB0BBBB", 1_791_000_000_200)])
    assert _apply(db, account, [{"type": "peer_read", "channel": {"ext_id": "555@lid", "kind": "dm"},
                                 "up_to_ext_id": "3EB0AAAA"}]).applied == 1  # fmt: skip
    assert _marker(db) == 1_791_000_000_100
    # Неизвестное ядру сообщение — no-op (позиции нет); назад — no-op.
    assert _apply(db, account, [{"type": "peer_read", "channel": {"ext_id": "555@lid", "kind": "dm"},
                                 "up_to_ext_id": "NOPE"}]).noop == 1  # fmt: skip
    _apply(db, account, [{"type": "peer_read", "channel": {"ext_id": "555@lid", "kind": "dm"},
                          "up_to_ext_id": "3EB0BBBB"}])  # fmt: skip
    assert _marker(db) == 1_791_000_000_200
    _apply(db, account, [{"type": "peer_read", "channel": {"ext_id": "555@lid", "kind": "dm"},
                          "up_to_ext_id": "3EB0AAAA"}])  # fmt: skip
    assert _marker(db) == 1_791_000_000_200


def test_reaction_ops_accumulate_in_order_like_telegram(db):
    """Одна реакция (П5): операции копятся по порядку — итог «👍, 🔥, снять 🔥» считает коннектор
    от состояния;
    у источников с несколькими реакциями `🔥 off` заменил бы `🔥 on`."""
    account = _account(db)
    _apply(db, account, [_upsert("M1", 1_791_000_000_100, is_me=False)])
    message_id = _message_id(db, "M1")
    for emoji, on in (("👍", True), ("🔥", True), ("🔥", False)):
        db.write_sync(lambda conn, e=emoji, o=on: reactions.enqueue(conn, message_id, e, o, 2000))
    with db.reader() as conn:
        (ops_json,) = conn.execute(
            "SELECT ops_json FROM main.reaction_ack WHERE message_id = ?", (message_id,)
        ).fetchone()
    assert json.loads(ops_json) == [["👍", True], ["🔥", True], ["🔥", False]]


def test_whatsapp_text_limit_and_file_not_editable(db):
    assert msg_ops.text_limit("whatsapp", "text") == (msg_ops.WHATSAPP_TEXT_MAX, False)
    account = _account(db)
    photo = [{"type": "photo", "src_id": "F1", "mime": "image/jpeg"}]
    _apply(
        db, account, [_upsert("T1", 1_791_000_000_100), _upsert("F1", 1_791_000_000_200, attachments=photo)]
    )
    with db.reader() as conn:
        text_msg = msg_ops._load(conn, _message_id(db, "T1"))
        file_msg = msg_ops._load(conn, _message_id(db, "F1"))
    msg_ops._check_editable(1, text_msg)
    with pytest.raises(msg_ops.NotEditable):
        msg_ops._check_editable(2, file_msg)


def test_new_recipient_whatsapp_phone_only():
    rec = new_recipient._recipient(
        "whatsapp", "main", None, to="8 (900) 000-00-01", subject=None, from_address=None
    )
    assert rec.new_to == {"kind": "tg_phone", "phone": "+79000000001"}
    assert rec.title == "+7 ••• ••• 00 01" and rec.kind == "dm"
    with pytest.raises(new_recipient.Invalid):
        new_recipient._recipient("whatsapp", "main", None, to="@petr", subject=None, from_address=None)
    with pytest.raises(new_recipient.Invalid, match="wa_phone_daily=0"):
        new_recipient._recipient(
            "whatsapp", "main", json.dumps({"wa_phone_daily": 0}), to="+79000000001", subject=None,
            from_address=None,
        )  # fmt: skip


def test_phone_daily_per_source():
    config = json.dumps({"tg_phone_daily": 9, "wa_phone_daily": 2})
    assert new_recipient.phone_daily("whatsapp", config) == 2
    assert new_recipient.phone_daily("telegram", config) == 9
    assert new_recipient.phone_daily("whatsapp", None) == new_recipient.TG_PHONE_DAILY_DEFAULT
    assert (
        new_recipient.phone_daily("whatsapp", json.dumps({"wa_phone_daily": 99}))
        == new_recipient.TG_PHONE_DAILY_MAX
    )


def test_phone_limit_counts_wa_phone_daily(db):
    account = _account(db, config={"wa_phone_daily": 1, "tg_phone_daily": 20})
    meta = json.dumps({"new_draft": {"to": {"kind": "tg_phone"}, "origin": "mcp"}})
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, first_seen_ts, meta_json) "
            "VALUES (?, 'new:x', 'dm', 5000, ?)",
            (account, meta),
        )
    )
    with pytest.raises(new_recipient.TooMany):
        db.write_sync(lambda conn: new_recipient._check_phone_limit(conn, "main", account, 6000))


def test_api_accounts_include_whatsapp(db):
    account = _account(db)
    source, schema, _config = db.write_sync(lambda conn: new_recipient._account(conn, account))
    assert (source, schema) == ("whatsapp", "main")


# --- ревью Ф62, Н2: беседа переехала с номера на LID — удаление и реакция на старое сообщение -------------

_PN = "79000010002@s.whatsapp.net"
_LID = "556@lid"


def _old_msg(chat: str, ext_id: str, ts: int) -> dict:
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "dm", "attrs": {"wa_type": "dm"}},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": "старое",
        "mentions_me": False,
        "sender": {"ext_id": chat},
    }


def _migrate_to_lid(db, account: int) -> None:
    _apply(db, account, [_old_msg(_PN, "OLD1", 1_791_000_000_000)])
    _apply(db, account, [
        {"type": "channel_migrate", "old_ext_id": _PN,
         "channel": {"ext_id": _LID, "kind": "dm", "attrs": {"wa_type": "dm"}}},
        _old_msg(_LID, "NEW1", 1_791_000_100_000),
    ])  # fmt: skip


def _old_row(db) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT deleted, reactions_json FROM main.message WHERE ext_id = 'OLD1'"
        ).fetchone()


def test_delete_and_reaction_after_alias_reach_pre_alias_message(db):
    account = _account(db)
    _migrate_to_lid(db, account)
    reaction = {"type": "reaction", "channel": {"ext_id": _LID, "kind": "dm"}, "ext_id": "OLD1",
                "reactions": [{"emoji": "👍", "count": 1, "me": False}]}  # fmt: skip
    res = _apply(db, account, [reaction])
    assert res.applied == 1
    assert json.loads(_old_row(db)[1])[0]["emoji"] == "👍"
    res = _apply(
        db, account, [{"type": "delete", "channel": {"ext_id": _LID, "kind": "dm"}, "ext_ids": ["OLD1"]}]
    )
    assert res.applied == 1 and _old_row(db)[0] == 1


def test_alias_lookup_only_for_whatsapp(db):
    # У Telegram номера сообщений старой и новой беседы — разные ряды: по алиасу не ищем.
    account = _account(db, source="telegram")
    _migrate_to_lid(db, account)
    res = _apply(
        db, account, [{"type": "delete", "channel": {"ext_id": _LID, "kind": "dm"}, "ext_ids": ["OLD1"]}]
    )
    assert res.noop == 1 and _old_row(db)[0] == 0
