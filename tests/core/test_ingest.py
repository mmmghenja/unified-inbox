import json
import sqlite3

import pytest

from hub.core import ingest
from hub.core.db import WORK_ID_BASE
from hub.core.events import CursorIn
from hub.core.ingest import apply_batch, unread_count


def _insert_account(db, *, source: str = "tg", db_name: str = "main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _upsert(ext_id, ts, *, channel_ext="c1", channel_kind="dm", text="hi", is_me=False, kind="text", **extra):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": channel_kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": kind,
        "text": text,
        "mentions_me": False,
    }
    event.update(extra)
    return event


def _apply(db, account_id, events, cursor=None, now_ms=1000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=cursor, now_ms=now_ms)
    )


# 1. upsert нового: строка, канал, контакт созданы; seq == first_seq; last_msg_ts, my_msg_count.
@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_upsert_new_message_creates_row_channel_contact(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    event = _upsert("m1", 1000, sender={"ext_id": "s1", "name": "Bob"}, is_me=True)

    result = _apply(db, account_id, [event])
    assert result == (1, 0, 0, result.max_seq)

    with db.reader() as conn:
        channel_id, last_msg_ts, my_msg_count = conn.execute(
            f"SELECT id, last_msg_ts, my_msg_count FROM {account_kind}.channel WHERE ext_id='c1'"
        ).fetchone()
        seq, first_seq, text = conn.execute(
            f"SELECT seq, first_seq, text FROM {account_kind}.message WHERE ext_id='m1'"
        ).fetchone()
        contact = conn.execute(f"SELECT id FROM {account_kind}.contact WHERE ext_id='s1'").fetchone()

    if account_kind == "work":
        assert channel_id >= WORK_ID_BASE
    assert last_msg_ts == 1000
    assert my_msg_count == 1
    assert seq == first_seq
    assert text == "hi"
    assert contact is not None


# 2. Повтор той же пачки — всё no-op, seq_counter не вырос.
@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_repeat_batch_is_noop(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    event = _upsert("m1", 1000, sender={"ext_id": "s1"})

    _apply(db, account_id, [event])
    with db.reader() as conn:
        counter_before = conn.execute("SELECT value FROM main.seq_counter").fetchone()[0]

    result = _apply(db, account_id, [event])
    assert (result.applied, result.noop, result.dead) == (0, 1, 0)

    with db.reader() as conn:
        counter_after = conn.execute("SELECT value FROM main.seq_counter").fetchone()[0]
    assert counter_after == counter_before


# 3. upsert с изменённым текстом: seq вырос, first_seq прежний; FTS находит новое, не находит старое.
@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_upsert_changed_text_bumps_seq_keeps_first_seq(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    _apply(db, account_id, [_upsert("m1", 1000, text="старый")])

    with db.reader() as conn:
        seq_before, first_seq_before = conn.execute(
            f"SELECT seq, first_seq FROM {account_kind}.message WHERE ext_id='m1'"
        ).fetchone()

    result = _apply(db, account_id, [_upsert("m1", 1000, text="новый")])
    assert (result.applied, result.noop) == (1, 0)

    with db.reader() as conn:
        seq_after, first_seq_after = conn.execute(
            f"SELECT seq, first_seq FROM {account_kind}.message WHERE ext_id='m1'"
        ).fetchone()

        def _search(term):
            return conn.execute(
                f"SELECT text FROM {account_kind}.message_fts WHERE message_fts MATCH ?", (term,)
            ).fetchall()

        assert _search("нов*")
        assert not _search("стар*")

    assert seq_after > seq_before
    assert first_seq_after == first_seq_before


def _deleted_flag(db, channel_ext, ext_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT deleted FROM main.message WHERE ext_id = ? "
            "AND channel_id = (SELECT id FROM main.channel WHERE ext_id = ?)",
            (ext_id, channel_ext),
        ).fetchone()[0]


# 4. delete с каналом; delete без канала находит по (account_id, ext_id) и НЕ трогает канал с own_id_space.
def test_delete_with_and_without_channel(db):
    account_id = _insert_account(db)
    channel_b = {"ext_id": "b", "kind": "channel", "own_id_space": True}
    _apply(db, account_id, [_upsert("x1", 1, channel_ext="a", channel_kind="dm")])
    _apply(db, account_id, [_upsert("x1", 1, channel_ext="b", channel_kind="channel", channel=channel_b)])

    # delete без канала: должен затронуть только канал 'a' (own_id_space=False), не 'b' (own_id_space=True)
    result = _apply(db, account_id, [{"type": "delete", "ext_ids": ["x1"]}])
    assert (result.applied, result.noop) == (1, 0)
    assert _deleted_flag(db, "a", "x1") == 1
    assert _deleted_flag(db, "b", "x1") == 0

    # delete с явным каналом 'b' — удаляет, несмотря на own_id_space
    delete_b = {"type": "delete", "ext_ids": ["x1"], "channel": {"ext_id": "b", "kind": "channel"}}
    result2 = _apply(db, account_id, [delete_b])
    assert (result2.applied, result2.noop) == (1, 0)
    assert _deleted_flag(db, "b", "x1") == 1

    # уже удалённое — no-op
    result3 = _apply(db, account_id, [delete_b])
    assert (result3.applied, result3.noop) == (0, 1)

    # неизвестный ext_id — no-op, не ошибка
    result4 = _apply(db, account_id, [{"type": "delete", "ext_ids": ["nope"]}])
    assert (result4.applied, result4.noop, result4.dead) == (0, 1, 0)


# 5. reaction не меняет unread_count; edit не меняет unread_count.
@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_reaction_and_edit_do_not_change_unread(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    _apply(db, account_id, [_upsert("m1", 1000)])

    with db.reader() as conn:
        channel_id = conn.execute(f"SELECT id FROM {account_kind}.channel WHERE ext_id='c1'").fetchone()[0]

    with db.reader() as conn:
        before = unread_count(conn, channel_id)
    assert before == 1

    reaction = {
        "type": "reaction",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": "m1",
        "reactions": {"x": 1},
    }
    result = _apply(db, account_id, [reaction])
    assert result.applied == 1
    with db.reader() as conn:
        assert unread_count(conn, channel_id) == 1

    edit_event = _upsert("m1", 1000, text="изменено")
    edit_event["type"] = "edit"
    result2 = _apply(db, account_id, [edit_event])
    assert result2.applied == 1
    with db.reader() as conn:
        assert unread_count(conn, channel_id) == 1


# 6. read: unread_count падает; read назад не двигает; новое сообщение после read снова даёт 1;
#    своё (is_me) и kind='event' не считаются.
@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_read_advances_unread_count(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    _apply(db, account_id, [_upsert("e1", 100)])
    _apply(db, account_id, [_upsert("e2", 200, is_me=True)])
    _apply(db, account_id, [_upsert("e3", 300, kind="event")])

    with db.reader() as conn:
        channel_id = conn.execute(f"SELECT id FROM {account_kind}.channel WHERE ext_id='c1'").fetchone()[0]
        assert unread_count(conn, channel_id) == 1  # только e1: e2 свой, e3 kind=event

    def _read(up_to_ext_id):
        event = {"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": up_to_ext_id}
        return _apply(db, account_id, [event])

    result = _read("e3")
    assert result.applied == 1
    with db.reader() as conn:
        assert unread_count(conn, channel_id) == 0

    # назад — no-op, не двигает
    result_back = _read("e1")
    assert (result_back.applied, result_back.noop) == (0, 1)
    with db.reader() as conn:
        assert unread_count(conn, channel_id) == 0

    # новое сообщение после read — снова 1
    _apply(db, account_id, [_upsert("e4", 400)])
    with db.reader() as conn:
        assert unread_count(conn, channel_id) == 1

    # неизвестное up_to_ext_id — no-op
    result_unknown = _read("nope")
    assert (result_unknown.applied, result_unknown.noop) == (0, 1)


# 7. Ядовитое событие в середине пачки: невалидная схема И (сымитированное monkeypatch'ем) исключение БД —
#    соседи применены, dead_letter +N с исходным payload, курсор записан.
def test_poison_events_mid_batch(db, monkeypatch):
    account_id = _insert_account(db)

    original = ingest._apply_reaction

    def _boom(conn, schema, account_id_, ev, now_ms):
        if ev.ext_id == "boom":
            raise sqlite3.IntegrityError("имитация ошибки БД")
        return original(conn, schema, account_id_, ev, now_ms)

    monkeypatch.setattr(ingest, "_apply_reaction", _boom)

    events = [
        _upsert("a", 1),
        {"type": "no-such-type"},  # невалидная схема
        {"type": "reaction", "channel": {"ext_id": "c1", "kind": "dm"}, "ext_id": "boom", "reactions": {}},
        _upsert("b", 2),
    ]

    result = _apply(db, account_id, events, cursor=CursorIn(value="cur1", ts=100), now_ms=100)

    assert result.applied == 2
    assert result.dead == 2

    with db.reader() as conn:
        dead_payloads = [row[0] for row in conn.execute("SELECT payload FROM main.dead_letter ORDER BY id")]
        assert len(dead_payloads) == 2
        assert json.loads(dead_payloads[0]) == {"type": "no-such-type"}
        assert json.loads(dead_payloads[1])["ext_id"] == "boom"

        ext_ids = [row[0] for row in conn.execute("SELECT ext_id FROM main.message ORDER BY ext_id")]
        assert ext_ids == ["a", "b"]
        assert _cursor_value(conn, "main", account_id) == "cur1"


# 8. Пачка 501 -> ValueError, БД не изменилась. Неизвестный account -> исключение.
def test_batch_cap_and_unknown_account(db):
    account_id = _insert_account(db)
    events = [_upsert(str(i), i, channel_ext=f"c{i}") for i in range(501)]

    with pytest.raises(ValueError):
        _apply(db, account_id, events)

    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.message").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM main.channel").fetchone()[0] == 0

    with pytest.raises(ValueError):
        _apply(db, 999999, [_upsert("m1", 1)])


# 9. work-аккаунт: все строки в work с id >= 2**40; в main.message/channel/contact/dead_letter/cursor — пусто,
#    включая случай ядовитого события.
def test_work_account_fully_isolated(db):
    account_id = _insert_account(db, db_name="work")
    events = [_upsert("a", 1, sender={"ext_id": "s1"}), {"type": "bad-type"}]

    result = _apply(db, account_id, events, cursor=CursorIn(value="v1", ts=10), now_ms=10)
    assert result.applied == 1
    assert result.dead == 1

    with db.reader() as conn:
        channel_id, message_id, contact_id, dead_id = (
            conn.execute("SELECT id FROM work.channel WHERE ext_id='c1'").fetchone()[0],
            conn.execute("SELECT id FROM work.message WHERE ext_id='a'").fetchone()[0],
            conn.execute("SELECT id FROM work.contact WHERE ext_id='s1'").fetchone()[0],
            conn.execute("SELECT id FROM work.dead_letter").fetchone()[0],
        )
        for value in (channel_id, message_id, contact_id, dead_id):
            assert value >= WORK_ID_BASE
        assert _cursor_value(conn, "work", account_id) == "v1"

        for table in ("channel", "message", "contact", "dead_letter", "cursor"):
            assert conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0] == 0


def _cursor_value(conn, schema, account_id):
    query = f"SELECT value FROM {schema}.cursor WHERE account_id = ?"
    return conn.execute(query, (account_id,)).fetchone()[0]


def _anomaly_count(conn):
    return conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'cursor_anomaly'").fetchone()[0]


# 10. Курсор назад и скачок > 24ч -> строка cursor_anomaly в audit; нормальное движение — без audit.
def test_cursor_anomaly_detection(db):
    account_id = _insert_account(db)
    day_ms = 24 * 60 * 60 * 1000

    _apply(db, account_id, [], cursor=CursorIn(value="c1", ts=1000), now_ms=1000)
    with db.reader() as conn:
        assert _anomaly_count(conn) == 0

    # назад
    _apply(db, account_id, [], cursor=CursorIn(value="c0", ts=500), now_ms=1500)
    with db.reader() as conn:
        assert _anomaly_count(conn) == 1
        assert _cursor_value(conn, "main", account_id) == "c0"

    # скачок вперёд от сохранённого больше суток
    jump_ts = 500 + day_ms + 1000
    _apply(db, account_id, [], cursor=CursorIn(value="c2", ts=jump_ts), now_ms=jump_ts)
    with db.reader() as conn:
        assert _anomaly_count(conn) == 2

    # нормальное движение вперёд — без новой записи
    _apply(db, account_id, [], cursor=CursorIn(value="c3", ts=jump_ts + 10), now_ms=jump_ts + 10)
    with db.reader() as conn:
        assert _anomaly_count(conn) == 2


# 11. Переименование канала: title_history_json дополнен, channel.seq вырос.
def test_channel_rename_appends_history_and_bumps_seq(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("m1", 1, channel={"ext_id": "c1", "kind": "group", "title": "Старое"})])

    with db.reader() as conn:
        seq_before = conn.execute("SELECT seq FROM main.channel WHERE ext_id='c1'").fetchone()[0]

    _apply(db, account_id, [_upsert("m2", 2, channel={"ext_id": "c1", "kind": "group", "title": "Новое"})])

    with db.reader() as conn:
        title, history_json, seq_after = conn.execute(
            "SELECT title, title_history_json, seq FROM main.channel WHERE ext_id='c1'"
        ).fetchone()

    assert title == "Новое"
    # ts в истории — now_ms момента правки, а не ts события
    assert json.loads(history_json) == [{"title": "Старое", "ts": 1000}]
    assert seq_after > seq_before


# Первое появление названия у канала — не переименование (по истории API потом решает, доверять ли каналу).
def test_first_title_is_not_a_rename(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("m1", 1)])
    _apply(db, account_id, [_upsert("m2", 2, channel={"ext_id": "c1", "kind": "dm", "title": "Вася"})])

    with db.reader() as conn:
        title, history_json = conn.execute(
            "SELECT title, title_history_json FROM main.channel WHERE ext_id='c1'"
        ).fetchone()

    assert title == "Вася"
    assert json.loads(history_json or "[]") == []


# Числовые ext_id в одну секунду: "99" текстом больше "100", порядок обязан быть числовым.
def test_read_orders_numeric_ext_ids_numerically(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("99", 5), _upsert("100", 5), _upsert("101", 5)])
    with db.reader() as conn:
        channel_id = conn.execute("SELECT id FROM main.channel WHERE ext_id='c1'").fetchone()[0]
        assert unread_count(conn, channel_id) == 3

    _apply(
        db, account_id, [{"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": "100"}]
    )

    with db.reader() as conn:
        assert unread_count(conn, channel_id) == 1
        order = [
            r[0]
            for r in conn.execute(
                "SELECT ext_id FROM main.message WHERE channel_id = ? ORDER BY ts, ext_sort, ext_id",
                (channel_id,),
            )
        ]
    assert order == ["99", "100", "101"]
