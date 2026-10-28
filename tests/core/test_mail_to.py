"""Ф11-8 (docs/architecture.md): адресаты своего письма `mail_to` ->
колонка `message.mail_to_json` (миграция 0023, обе схемы) по пути `forward_json`; обнуление как ПДн —
скрытие сообщения и беседы, приём скрытого, физическая очистка `work.message`; эхо своей отправки; выдача
`queries`."""

import json

import pytest

from hub.core import db as db_module
from hub.core import hiding, queries, retention
from hub.core.db import Db
from hub.core.ingest import apply_batch
from hub.core.outbox import enqueue, lease
from hub.core.security import Principal

UI = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)
TO = ["alisa@client.example", "boss@client.example"]


def _insert_account(db, *, source="mail", db_name="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _upsert(ext_id, ts, *, mail_to=None, is_me=True, text="письмо", chat="t1", **extra) -> dict:
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "mail_thread", "title": "Тема"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    if mail_to is not None:
        event["mail_to"] = mail_to
    event.update(extra)
    return event


def _apply(db, account_id, events, now_ms=1000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _row(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, mail_to_json, seq, hidden, purged FROM {schema}.message WHERE ext_id = ?",  # noqa: S608
            (ext_id,),
        ).fetchone()


# --- приём -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["main", "work"])
def test_mail_to_stored_and_in_queries(db, schema):
    acc = _insert_account(db, db_name=schema)
    result = _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO), _upsert("<m2@x>", 200, is_me=False)])
    assert result.dead == 0
    mid, mail_to_json, *_ = _row(db, "<m1@x>", schema)
    assert json.loads(mail_to_json) == TO
    assert _row(db, "<m2@x>", schema)[1] is None

    with db.reader() as conn:
        channel_id = conn.execute(
            f"SELECT channel_id FROM {schema}.message WHERE id = ?",
            (mid,),  # noqa: S608
        ).fetchone()[0]
        page = queries.list_messages(conn, UI, channel_id, before=None, limit=10)
        single = queries.get_message(conn, UI, mid)
        changes = queries.changes_since(conn, UI, since=0, limit=100)
    by_ext = {m["ext_id"]: m for m in page.messages}
    assert json.loads(by_ext["<m1@x>"]["mail_to_json"]) == TO
    assert by_ext["<m2@x>"]["mail_to_json"] is None
    assert json.loads(single["mail_to_json"]) == TO
    synced = {m["ext_id"]: m for m in changes.messages}
    assert json.loads(synced["<m1@x>"]["mail_to_json"]) == TO


def test_empty_list_stored_as_null(db):
    acc = _insert_account(db)
    assert _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=[])]).dead == 0
    assert _row(db, "<m1@x>")[1] is None


@pytest.mark.parametrize(
    "bad",
    [
        [f"a{i}@x.ru" for i in range(21)],  # больше 20
        ["a@"],  # короче 3
        ["a" * 318 + "@x.ru"],  # длиннее 320
        "a@x.ru",  # не список
    ],
)
def test_core_rejects_malformed_mail_to(db, bad):
    acc = _insert_account(db)
    result = _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=bad)])
    assert result.dead == 1
    assert _row(db, "<m1@x>") is None


def test_twenty_addresses_accepted(db):
    acc = _insert_account(db)
    addrs = [f"a{i}@x.ru" for i in range(20)]
    assert _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=addrs)]).dead == 0
    assert json.loads(_row(db, "<m1@x>")[1]) == addrs


def test_repeat_upsert_with_mail_to_fills_old_row(db):
    """Поправка 3: колонка в `_MESSAGE_CONTENT_COLUMNS` — повтор с адресатами не no-op, строка дополняется."""
    acc = _insert_account(db)
    _apply(db, acc, [_upsert("<m1@x>", 100)])
    _mid, mail_to_json, seq_before, *_ = _row(db, "<m1@x>")
    assert mail_to_json is None

    result = _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO)])
    assert result.applied == 1
    _mid, mail_to_json, seq_after, *_ = _row(db, "<m1@x>")
    assert json.loads(mail_to_json) == TO
    assert seq_after > seq_before

    # Идентичный повтор — no-op без нового seq.
    assert _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO)]).applied == 0
    assert _row(db, "<m1@x>")[2] == seq_after


def test_echo_of_own_send_carries_mail_to(db):
    """Поправка 3: письмо из хаба -> эхо с адресатами -> `mail_to_json` у строки заготовки."""
    acc = _insert_account(db)
    channel_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, my_msg_count, first_seen_ts) "
                "VALUES (?, 't1', 'mail_thread', 1, 0)",
                (acc,),
            ).lastrowid
        )
    )
    token_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')").lastrowid
        )
    )
    enq = db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="письмо",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="k1",
            now_ms=100,
        )
    )
    leased = db.write_sync(
        lambda conn: lease(conn, account_id=acc, owner="w1", now_ms=100, lease_ms=60_000, limit=10)
    )[0]

    echo = _upsert("<hub-1@x>", 150, mail_to=TO, send_token=leased.send_token)
    assert _apply(db, acc, [echo], now_ms=150).applied == 1

    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, ext_id, mail_to_json FROM main.message WHERE channel_id = ?", (channel_id,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == enq.message_id
    assert rows[0][1] == "<hub-1@x>"
    assert json.loads(rows[0][2]) == TO


# --- обнуление (ПДн), поправка 4 ----------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ["main", "work"])
def test_hide_message_nulls_mail_to_and_reingest_keeps_null(db, schema):
    acc = _insert_account(db, db_name=schema)
    _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO)])
    mid = _row(db, "<m1@x>", schema)[0]

    db.write_sync(lambda conn: hiding.hide_message(conn, mid, now_ms=2000))
    _mid, mail_to_json, _seq, hidden, purged = _row(db, "<m1@x>", schema)
    assert (mail_to_json, hidden, purged) == (None, 1, 1)

    # Повторный приём скрытого письма с адресатами — остаётся NULL.
    assert _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO)], now_ms=3000).dead == 0
    assert _row(db, "<m1@x>", schema)[1] is None


def test_hide_channel_nulls_mail_to_and_old_letter_lands_null(db):
    acc = _insert_account(db)
    _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO), _upsert("<m2@x>", 200, mail_to=["x@y.ru"])])
    mid = _row(db, "<m1@x>")[0]
    with db.reader() as conn:
        channel_id = conn.execute("SELECT channel_id FROM main.message WHERE id = ?", (mid,)).fetchone()[0]

    db.write_sync(lambda conn: hiding.hide_channel(conn, channel_id, now_ms=2000))
    assert _row(db, "<m1@x>")[1] is None
    assert _row(db, "<m2@x>")[1] is None

    # Письмо за границей удалённой беседы (догрузка истории) ложится сразу очищенным.
    assert _apply(db, acc, [_upsert("<m0@x>", 50, mail_to=TO)], now_ms=3000).dead == 0
    _mid, mail_to_json, _seq, hidden, _purged = _row(db, "<m0@x>")
    assert (mail_to_json, hidden) == (None, 1)


def test_clean_message_content_nulls_mail_to():
    content = {"text": "x", "mail_to_json": json.dumps(TO), "attachments_json": None, "refs_json": None}
    retention.clean_message_content(content)
    assert content["mail_to_json"] is None


def test_physical_purge_of_work_nulls_mail_to(db):
    acc = _insert_account(db, db_name="work")
    _apply(db, acc, [_upsert("<m1@x>", 100, mail_to=TO)])
    cleaned, _last, _more = db.write_sync(
        lambda conn: retention._purge_message_portion(conn, after_id=0, cutoff=10_000)
    )
    assert cleaned == 1
    _mid, mail_to_json, _seq, _hidden, purged = _row(db, "<m1@x>", "work")
    assert (mail_to_json, purged) == (None, 1)


# --- миграция 0023 -----------------------------------------------------------------------------------------


def _mail_to_column(conn, schema):
    cols = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(message)")}
    return cols.get("mail_to_json")


def test_migration_on_empty_base_both_files(tmp_path):
    fresh = Db(tmp_path)
    fresh.start()
    try:
        with fresh.reader() as conn:
            for schema in ("main", "work"):
                col = _mail_to_column(conn, schema)
                assert col is not None
                assert col[2] == "TEXT"
                assert col[3] == 0  # NULL допустим
    finally:
        fresh.close()


def test_migration_on_filled_base_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _before(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v < 23}

    monkeypatch.setattr(db_module, "_migrations", _before)
    old = Db(tmp_path)
    old.start()
    try:
        # Прямыми вставками: ingest уже знает о новой колонке и на старой схеме не работает.
        for schema in ("main", "work"):
            acc = _insert_account(old, db_name=schema)

            def _fill(conn, schema=schema, acc=acc):
                channel_id = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 't1', 'mail_thread', 0)",
                    (acc,),
                ).lastrowid
                seq = db_module.next_seq(conn)
                conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, kind, "  # noqa: S608
                    "text, ts) VALUES (?, ?, ?, ?, ?, 'text', ?, 100)",
                    (seq, seq, acc, channel_id, f"<{schema}@x>", f"старое {schema}"),
                )

            old.write_sync(_fill)
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert _mail_to_column(conn, schema) is None
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert _mail_to_column(conn, schema) is not None
                rows = conn.execute(
                    f"SELECT ext_id, text, mail_to_json FROM {schema}.message"  # noqa: S608
                ).fetchall()
                assert rows == [(f"<{schema}@x>", f"старое {schema}", None)]
    finally:
        new.close()
