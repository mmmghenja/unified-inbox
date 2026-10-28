"""Ф23-3 (docs/architecture.md): просмотры постов каналов
Telegram в ядре — миграция 0037 (`message.views_json`), счётчик при приёме (`upsert`/`edit` с
`views`/`forwards`) и событие `views`; выдача `views` в ленте, `GET /messages/{id}` и `/sync`. Событие
`views` не трогает непрочитанное, активность беседы и сигналы подписок; скрытое у себя не оживает;
неизвестное — no-op, не `dead_letter`."""

import json

import pytest

from hub.core import chan_subs, security, subs
from hub.core.ingest import apply_batch

from .conftest import insert_account, insert_channel, insert_message, insert_project, login_via_code

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
CHAN = {"ext_id": "-1001", "kind": "channel", "title": "Канал"}


def _account(db) -> int:
    return insert_account(db, source="telegram")


def _post(
    ext_id, ts, *, etype="upsert", views=None, forwards=None, channel=CHAN, text="пост", edited_ts=None
):
    event = {
        "type": etype,
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    if views is not None:
        event["views"] = views
        event["forwards"] = forwards
    if edited_ts is not None:
        event["edited_ts"] = edited_ts
    return event


def _views(items, ts, *, channel=CHAN):
    return {
        "type": "views",
        "channel": {"ext_id": channel["ext_id"], "kind": channel["kind"]},
        "ts": ts,
        "items": items,
    }


def _ingest(db, acc, events, now):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=now)
    )


def _row(db, ext_id, channel_ext="-1001"):
    with db.reader() as conn:
        row = conn.execute(
            "SELECT m.id, m.seq, m.first_seq, m.views_json, c.id, c.seq, c.last_msg_ts FROM main.message m "
            "JOIN main.channel c ON c.id = m.channel_id WHERE c.ext_id = ? AND m.ext_id = ?",
            (channel_ext, ext_id),
        ).fetchone()
    mid, seq, first_seq, views_json, cid, cseq, last_ts = row
    return {
        "id": mid,
        "seq": seq,
        "first_seq": first_seq,
        "views": json.loads(views_json) if views_json else None,
        "cid": cid,
        "cseq": cseq,
        "last_msg_ts": last_ts,
    }


def _unread(db, cid) -> tuple[int, int]:
    from hub.core.ingest import unread_breakdown

    with db.reader() as conn:
        return unread_breakdown(conn, cid)


def _dead(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.dead_letter").fetchone()[0]


# --- приём: счётчик в upsert/edit -------------------------------------------------------------------------


def test_upsert_with_views_writes_json_edit_without_keeps(db, clock):
    acc = _account(db)
    now = int(clock() * 1000)
    res = _ingest(db, acc, [_post("10", now - 1000, views=1234, forwards=5)], now)
    assert res.dead == 0
    assert _row(db, "10")["views"] == {"views": 1234, "forwards": 5, "ts": now}

    # правка без счётчика (живой EditedMessage без views) — `views_json` не стирается
    _ingest(db, acc, [_post("10", now - 1000, etype="edit", text="правка", edited_ts=now)], now + 10)
    assert _row(db, "10")["views"] == {"views": 1234, "forwards": 5, "ts": now}

    # тот же пост без полей и не-канал со счётчиком — не трогается / не пишется
    group = {"ext_id": "-1002", "kind": "group", "title": "Группа"}
    _ingest(db, acc, [_post("11", now, views=50, channel=group)], now + 20)
    assert _row(db, "11", "-1002")["views"] is None


def test_upsert_same_content_new_views_bumps_seq_same_views_only_ts(db, clock):
    acc = _account(db)
    now = int(clock() * 1000)
    _ingest(db, acc, [_post("10", now - 1000, views=100, forwards=1)], now)
    before = _row(db, "10")
    # повтор сверкой — содержимое то же, просмотров больше: новый seq сообщения
    _ingest(db, acc, [_post("10", now - 1000, views=150, forwards=1)], now + 60_000)
    after = _row(db, "10")
    assert after["views"] == {"views": 150, "forwards": 1, "ts": now + 60_000}
    assert after["seq"] > before["seq"] and after["first_seq"] == before["first_seq"]
    # те же цифры — только время снятия, без seq
    _ingest(db, acc, [_post("10", now - 1000, views=150, forwards=1)], now + 120_000)
    again = _row(db, "10")
    assert again["views"]["ts"] == now + 120_000 and again["seq"] == after["seq"]


# --- событие `views` --------------------------------------------------------------------------------------


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _bearer(db) -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    ).token


async def test_views_event_updates_only_counters_no_unread_no_signal(client, db, clock, procs, tmp_path):
    acc = _account(db)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    now = int(clock() * 1000)
    _ingest(db, acc, [_post("10", now - 5000), _post("11", now - 4000, views=7)], now - 3000)
    cid = _row(db, "10")["cid"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?", (pid, cid)
        )
    )
    # подписка сессии на беседу «каждое сообщение»
    token = _bearer(db)
    session = {
        "pid": PID,
        "proc_start": START,
        "session_id": SID,
        "cwd": str(workdir),
        "kind": "interactive",
        "name": None,
        "name_source": None,
    }
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": cid, "session": session},
        headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "2"},
    )
    assert r.status_code == 200, r.text

    def signals():
        with db.reader() as conn:
            return conn.execute("SELECT COUNT(*) FROM main.session_chan_signal").fetchone()[0]

    before10, before11 = _row(db, "10"), _row(db, "11")
    unread_before = _unread(db, cid)
    res = _ingest(
        db,
        acc,
        [_views([{"ext_id": "10", "views": 1234, "forwards": 5}, {"ext_id": "11", "views": 7}], now)],
        now + 1000,
    )
    assert (res.applied, res.dead) == (1, 0)
    after10, after11 = _row(db, "10"), _row(db, "11")
    assert after10["views"] == {"views": 1234, "forwards": 5, "ts": now}
    assert after10["seq"] > before10["seq"]
    # 11: те же 7 просмотров (пересылок не было) — только время снятия, без seq
    assert after11["views"] == {"views": 7, "forwards": None, "ts": now}
    assert after11["seq"] == before11["seq"]
    # беседа: ни seq, ни активности, ни непрочитанного; сигналов подписке нет
    assert (after10["cseq"], after10["last_msg_ts"]) == (before10["cseq"], before10["last_msg_ts"])
    assert _unread(db, cid) == unread_before
    assert after10["first_seq"] == before10["first_seq"]
    assert signals() == 0
    with db.reader() as conn:
        assert chan_subs.get(conn, r.json()["sub"]["id"]) is not None

    # зубы проверки сигналов: новый пост канала — сигнал есть
    _ingest(db, acc, [_post("12", now + 2000)], now + 3000)
    assert signals() == 1


def test_views_event_skips_unknown_hidden_older_and_non_channel(db, clock):
    acc = _account(db)
    now = int(clock() * 1000)
    group = {"ext_id": "-1002", "kind": "group", "title": "Группа"}
    _ingest(db, acc, [_post("10", now - 5000), _post("11", now - 4000), _post("1", now, channel=group)], now)
    mid11 = _row(db, "11")["id"]
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (mid11,)))

    res = _ingest(
        db,
        acc,
        [
            _views([{"ext_id": "999", "views": 5}], now),  # неизвестное сообщение
            _views([{"ext_id": "1", "views": 5}], now, channel={"ext_id": "-1009", "kind": "channel"}),
            _views([{"ext_id": "11", "views": 5}], now),  # скрыто у себя
            _views([{"ext_id": "1", "views": 5}], now, channel=group),  # не канал
        ],
        now + 1000,
    )
    assert (res.applied, res.noop, res.dead) == (0, 4, 0)
    assert _dead(db) == 0
    assert _row(db, "11")["views"] is None
    assert _row(db, "1", "-1002")["views"] is None
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = '-1009'").fetchone()[0] == 0

    # снимок старше записанного — цифры не откатываются
    _ingest(db, acc, [_views([{"ext_id": "10", "views": 500}], now)], now + 2000)
    _ingest(db, acc, [_views([{"ext_id": "10", "views": 400}], now - 60_000)], now + 3000)
    assert _row(db, "10")["views"] == {"views": 500, "forwards": None, "ts": now}


def test_views_event_validation_goes_to_dead_letter(db, clock):
    acc = _account(db)
    now = int(clock() * 1000)
    too_many = [{"ext_id": str(i), "views": 1} for i in range(101)]
    res = _ingest(db, acc, [_views(too_many, now), _views([{"ext_id": "1", "views": -1}], now)], now)
    assert res.dead == 2


# --- выдача -----------------------------------------------------------------------------------------------


async def test_views_in_public_api(client, db, clock):
    acc = _account(db)
    pid = insert_project(db, name="P")
    now = int(clock() * 1000)
    _ingest(db, acc, [_post("10", now - 2000, views=1234, forwards=5), _post("11", now - 1000)], now)
    row = _row(db, "10")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?", (pid, row["cid"])
        )
    )
    await login_via_code(client, db, clock)
    expected = {"views": 1234, "forwards": 5, "ts": now}

    page = (await client.get(f"/api/v1/channels/{row['cid']}/messages")).json()
    by_ext = {m["ext_id"]: m for m in page["messages"]}
    assert by_ext["10"]["views"] == expected
    assert by_ext["11"]["views"] is None

    assert (await client.get(f"/api/v1/messages/{row['id']}")).json()["views"] == expected

    sync = (await client.get("/api/v1/sync", params={"since": 0})).json()
    assert {m["ext_id"]: m["views"] for m in sync["messages"]}["10"] == expected


def test_migration_0037_on_previous_version(tmp_path, monkeypatch):
    from hub.core import db as db_module
    from hub.core.db import Db

    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 36}
    )
    old = Db(tmp_path)
    old.start()
    try:
        acc = insert_account(old, source="telegram")
        # Строка — прямой вставкой: приём текущим кодом на схеме 36 упал бы на колонках новее (0042).
        cid = insert_channel(old, acc, ext_id=CHAN["ext_id"], kind="channel", title=CHAN["title"])
        insert_message(old, cid, ext_id="10", ts=1000, text="пост")
        with old.reader() as conn:
            assert "views_json" not in {r[1] for r in conn.execute("PRAGMA main.table_info(message)")}
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 37
                assert "views_json" in {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(message)")}
        assert _row(new, "10")["views"] is None
    finally:
        new.close()
