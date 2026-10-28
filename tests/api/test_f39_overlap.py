"""Ф39 A (docs/architecture.md): подписка на беседу — своя у
каждой сессии. Сигнал получает каждая (по своему окну и своим ключам), подписки друг друга не снимают и не
глушат; возврат по роли — по строкам источника, чужие подписки ему не мешают, уже возвращённое второй раз не
возвращается (П1); миграция 0055.

Обвязка — `test_f32_chan_subs_restore.py`: публичное приложение через ASGITransport, события через
`ingest.apply_batch`, `/proc` подменён словарём, часы — `FakeClock`."""

import sqlite3

import pytest

from hub.core import chan_subs, ingest, security
from hub.core import db as db_module
from hub.core.db import Db

from . import test_f32_chan_subs_restore as base
from .test_f32_chan_subs_restore import (
    A2,
    MIN_MS,
    ROLE,
    A,
    B,
    C,
    _a_with_two,
    _chan_end,
    _chan_rows,
    _chan_sub,
    _chan_wait,
    _cleanup,
    _ev,
    _h,
    _ingest,
    _live,
    _now,
    _restored,
    _session,
    _skipped,
    _sub,
)

procs = base.procs  # фикстуры
world = base.world


def _sig(db) -> list[tuple]:
    """`(chan_sub_id, project_sub_id, channel_id, status, reason, cat, msg_count)` всех сигналов сообщений."""
    with db.reader() as conn:
        return conn.execute(
            "SELECT chan_sub_id, project_sub_id, channel_id, status, reason, cat, msg_count "
            "FROM main.session_chan_signal ORDER BY id"
        ).fetchall()


def _row(db, sub_id) -> chan_subs.ChanSubRec:
    with db.reader() as conn:
        return chan_subs.get(conn, sub_id)


async def _chan_post(client, w, who, ext, *, token=None, **extra):
    return await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch"][ext], "session": _session(w, who), **extra},
        headers=_h(token or w["tok"]),
    )


# --- сигнал каждой -----------------------------------------------------------------------------------------


async def test_two_sessions_each_own_pending_and_others(client, db, world, procs, clock):
    g1 = world["ch"]["G1"]
    await _sub(client, world, A, role=ROLE)
    r1 = await _chan_post(client, world, A, "G1")
    assert r1.status_code == 200 and r1.json()["created"] is True and r1.json()["others"] == []
    a = r1.json()["sub"]["id"]
    assert (r1.json()["sub"]["role"], r1.json()["sub"]["role_no"]) == (ROLE, 1)
    r2 = await _chan_post(client, world, B, "G1")
    assert r2.status_code == 200 and r2.json()["created"] is True
    b = r2.json()["sub"]["id"]
    assert b != a and _row(db, a).ended_ts is None and _row(db, b).ended_ts is None
    # в ответе подписки — сосед с ролью (только ЧИСЛА ключей); у самой B роли нет
    assert r2.json()["others"] == [
        {"id": a, "role": ROLE, "role_no": 1, "session_name": None, "keys": 0, "exclude": 0}
    ]
    assert (r2.json()["sub"]["role"], r2.json()["sub"]["role_no"]) == (None, None)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="привет")], now)
    assert [(s[0], s[3], s[6]) for s in _sig(db)] == [(a, "pending", 1), (b, "pending", 1)]
    # выдача каждой отдаёт свой сигнал; `others` — сколько ещё сессий слушает беседу
    for who, sub_id in ((A, a), (B, b)):
        [signal] = await _chan_wait(client, world, who)
        assert (signal["sub_id"], signal["channel_id"], signal["msg_count"]) == (sub_id, g1, 1)
        assert signal["others"] == 1
    assert [s[3] for s in _sig(db)] == ["delivered", "delivered"]


async def test_others_counts_only_live_processes(client, db, world, procs, clock):
    await _chan_sub(client, world, A, "G1")
    await _chan_sub(client, world, B, "G1")
    del procs[A[0]]  # cleanup ещё не прогонялся: строка A жива, процесс мёртв — «слушающей» не считается
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now)], now)
    [signal] = await _chan_wait(client, world, B)
    assert signal["others"] == 0
    assert (await _chan_post(client, world, B, "G1")).json()["others"] == []


async def test_own_keys_per_subscription(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1", keywords=["штраф"])
    b = await _chan_sub(client, world, B, "G1", keywords=["выплат"])
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="пришёл штраф 500")], now)
    assert [(s[0], s[3], s[4], s[5]) for s in _sig(db)] == [
        (a, "pending", None, "keyword"),
        (b, "skipped", "no_keyword", None),
    ]


async def test_own_window_per_subscription(client, db, world, procs, clock):
    """Окно — своё у каждой подписки: сообщение старше подписки B (с запасом) сигналит только A."""
    a = await _chan_sub(client, world, A, "G1")
    old_ts = _now(clock)
    clock.advance_ms(chan_subs.SIGNAL_TS_SLACK_MS + MIN_MS)
    await _chan_sub(client, world, B, "G1")
    _ingest(db, world, [_ev("G1", "1", old_ts)], _now(clock))
    assert [(s[0], s[3]) for s in _sig(db)] == [(a, "pending")]


# --- подписки друг друга не снимают ------------------------------------------------------------------------


async def test_unsubscribe_and_cleanup_leave_other_session(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1")
    b = await _chan_sub(client, world, B, "G1")
    await _chan_end(client, world, A, "G1")
    assert _row(db, a).end_reason == "unsubscribed" and _row(db, b).ended_ts is None
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now)], now)
    assert [(s[0], s[3]) for s in _sig(db)] == [(b, "pending")]
    # мёртвая сессия C: её подписку снимает cleanup, подписка B жива и сигналит
    c = await _chan_sub(client, world, C, "G1")
    del procs[C[0]]
    _cleanup(db, clock)
    assert _row(db, c).end_reason == "session_gone" and _row(db, b).ended_ts is None
    _ingest(db, world, [_ev("G1", "2", now + 1)], now + 1)
    assert [(s[0], s[3], s[6]) for s in _sig(db)] == [(b, "pending", 2)]
    assert len(await _chan_wait(client, world, B)) == 1


async def test_repeat_subscribe_changes_keys_in_place(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1", keywords=["штраф"])
    b = await _chan_sub(client, world, B, "G1", keywords=["выплат"])
    r = await _chan_post(client, world, A, "G1", keywords=["возврат"])
    assert r.status_code == 200 and r.json()["created"] is False and r.json()["sub"]["id"] == a
    assert r.json()["sub"]["keywords"]["keys"] == ["возврат"]
    assert [o["id"] for o in r.json()["others"]] == [b]
    rows = {row[0]: row for row in _chan_rows(db)}
    assert len(rows) == 2 and rows[b][3] is None and "выплат" in rows[b][4] and "возврат" not in rows[b][4]


async def test_clear_ends_only_own_previous_conversation(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1")
    b = await _chan_sub(client, world, B, "G1")
    a2 = await _chan_sub(client, world, A2, "G2")  # /clear: тот же процесс, новый `session_id`
    assert _row(db, a).end_reason == "conversation_changed"
    assert _row(db, b).ended_ts is None and _row(db, a2).ended_ts is None


async def test_limit_counts_only_own(client, db, world, procs, monkeypatch):
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 2)
    for ext in ("G1", "G2"):
        await _chan_sub(client, world, A, ext)
        await _chan_sub(client, world, B, ext)  # беседы A в предел B не входят
    for who in (A, B):
        r = await _chan_post(client, world, who, "G3")
        assert r.status_code == 409 and "2 бесед" in r.json()["detail"]
    assert (await _chan_post(client, world, A, "G1")).json()["created"] is False  # повтор своей — не третья


async def test_same_session_other_token_replaces(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1")
    b = await _chan_sub(client, world, B, "G1")
    r = await _chan_post(client, world, A, "G1", token=world["narrow"])
    assert r.status_code == 200 and r.json()["created"] is True
    assert _row(db, a).end_reason == "replaced" and _row(db, b).ended_ts is None
    assert set(_live(db, A)) == {world["ch"]["G1"]}


async def test_project_sub_of_same_session_silent_third_session_gets(client, db, world, procs, clock):
    """Ф26-1, не сломать: подписка на проект (`messages`) той же сессии, что слушает беседу, её не дублирует;
    подписка на проект третьей сессии — получает."""
    pa = (await _sub(client, world, A, role=ROLE, mode="messages"))["sub"]["id"]
    a = await _chan_sub(client, world, A, "G1")
    pb = (await _sub(client, world, B, role="программист", mode="messages"))["sub"]["id"]
    b = await _chan_sub(client, world, B, "G1")
    pc = (await _sub(client, world, C, role="аналитик", mode="messages"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now)], now)
    rows = [(s[0], s[1], s[3]) for s in _sig(db)]
    assert rows == [(a, None, "pending"), (b, None, "pending"), (None, pc, "pending")]
    assert pa not in {s[1] for s in _sig(db)} and pb not in {s[1] for s in _sig(db)}


# --- POST /chan-subs/end -----------------------------------------------------------------------------------


async def test_end_channel_listened_only_by_others_is_404(client, db, world, procs, clock):
    b = await _chan_sub(client, world, B, "G1")
    r = await client.post(
        "/api/v1/chan-subs/end",
        json={"session": _session(world, A, full=False), "channel_id": world["ch"]["G1"]},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 404  # было 403 «подписка другой сессии»
    assert _row(db, b).ended_ts is None


async def test_end_own_keeps_other_and_old_conversation_is_410(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1")
    b = await _chan_sub(client, world, B, "G1")
    # устаревший запрос прежнего разговора того же процесса — 410, подписка нового разговора цела
    body = {"session": _session(world, A2, full=False), "channel_id": world["ch"]["G1"]}
    r = await client.post("/api/v1/chan-subs/end", json=body, headers=_h(world["tok"]))
    assert r.status_code == 410 and r.json()["detail"]["end_reason"] == "conversation_changed"
    assert _row(db, a).ended_ts is None
    await _chan_end(client, world, A, "G1")
    assert _row(db, a).end_reason == "unsubscribed" and _row(db, b).ended_ts is None
    await _chan_end(client, world, A, "G1")  # повтор — 200 со своей завершённой строкой


# --- возврат по роли (A2 п.4, П1, П2) ----------------------------------------------------------------------


async def test_restore_ignores_foreign_subs_of_channel(client, db, world, procs, clock):
    await _a_with_two(client, world)
    await _sub(client, world, C, role="программист")
    c1 = await _chan_sub(client, world, C, "G1")  # беседу источника слушает и сессия другой роли
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    ch = world["ch"]
    assert set(_restored(body)) == {ch["G1"], ch["G2"]} and _skipped(body) == {}
    assert _restored(body)[ch["G1"]]["keys"] == 2  # ключи — источника, не соседа
    assert _row(db, c1).ended_ts is None and ch["G1"] in _live(db, B)


async def test_restore_skips_channel_new_session_already_listens(client, db, world, procs, clock):
    """П2: у новой сессии уже своя живая подписка беседы — молча: второй не появляется, ключи — её."""
    await _a_with_two(client, world)
    mine = await _chan_sub(client, world, B, "G1", keywords=["свой"])
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    ch = world["ch"]
    assert set(_restored(body)) == {ch["G2"]} and _skipped(body) == {}
    assert _live(db, B)[ch["G1"]] == mine
    assert _row(db, mine).keywords_json is not None and "свой" in _row(db, mine).keywords_json
    assert [r[0] for r in _chan_rows(db) if r[1] == ch["G1"] and r[2] == B[2]] == [mine]


@pytest.mark.parametrize("how", ["unsubscribed", "channel_gone"])
async def test_p1_restored_then_ended_is_not_restored_again(client, db, world, procs, clock, how):
    """П1: вернули -> сессия сняла беседу сама (или беседа ушла) -> повторный `hub_subscribe()` её не
    возвращает и окно не добирает."""
    await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    ch = world["ch"]
    assert set(_restored(body)) == {ch["G1"], ch["G2"]}
    g2 = _live(db, B)[ch["G2"]]
    if how == "unsubscribed":
        await _chan_end(client, world, B, "G2")
    else:
        now = _now(clock)
        db.write_sync(lambda conn: chan_subs.end(conn, g2, "channel_gone", now_ms=now, actor="core"))
    clock.advance_ms(MIN_MS)
    now = _now(clock)
    _ingest(db, world, [_ev("G2", "1", now, text="после снятия")], now)
    before = _sig(db)
    for extra in ({}, {"role": ROLE}):
        body = await _sub(client, world, B, **extra)
        assert ch["G2"] not in _restored(body) and ch["G2"] not in _skipped(body)
        assert set(_live(db, B)) == {ch["G1"]}
    assert _sig(db) == before  # ничего не просигналено повторно
    assert _row(db, g2).end_reason == how


# --- перенос беседы (A2 п.5) -------------------------------------------------------------------------------


def _migrate(db, w, clock, old_ext, new_ext):
    now = _now(clock)
    channel = {"ext_id": new_ext, "kind": "group"}
    event = {"type": "channel_migrate", "old_ext_id": old_ext, "channel": channel}
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=now)
    )
    assert result.dead == 0, result


async def test_migrate_moves_every_live_sub(client, db, world, procs, clock):
    a = await _chan_sub(client, world, A, "G1")
    b = await _chan_sub(client, world, B, "G1")
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now)], now)
    _migrate(db, world, clock, "G1", "SUPER")
    with db.reader() as conn:
        new_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = 'SUPER'").fetchone()[0]
    for sub_id in (a, b):
        rec = _row(db, sub_id)
        assert rec.ended_ts is None and rec.channel_id == new_id
    assert [(s[0], s[2], s[3]) for s in _sig(db)] == [(a, new_id, "pending"), (b, new_id, "pending")]


async def test_migrate_same_session_already_on_new_channel(client, db, world, procs, clock):
    a_old = await _chan_sub(client, world, A, "G1")
    b_old = await _chan_sub(client, world, B, "G1")
    b_new = await _chan_sub(client, world, B, "G2")
    _migrate(db, world, clock, "G1", "G2")
    g2 = world["ch"]["G2"]
    assert _row(db, a_old).ended_ts is None and _row(db, a_old).channel_id == g2  # у A на новой не было
    assert _row(db, b_old).end_reason == "channel_gone"  # у B на новой уже своя живая
    assert _row(db, b_new).ended_ts is None and _row(db, b_new).channel_id == g2


# --- `msg_signals` карточки и пометка ключей в чтении (A2 п.7–8) -------------------------------------------


async def test_card_carries_signal_of_every_chan_sub(client, db, world, procs, clock):
    triage = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="agent:triage",
            kind="background",
            scopes=["read", "triage"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    a = await _chan_sub(client, world, A, "G1")
    b = await _chan_sub(client, world, B, "G1", keywords=["штраф"])
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="без ключа")], now)
    g1 = world["ch"]["G1"]
    with db.reader() as conn:
        message_id = conn.execute("SELECT id FROM main.message WHERE channel_id = ?", (g1,)).fetchone()[0]
    card = {"channel_id": g1, "message_ids": [message_id], "label": "reply", "summary": "сводка"}
    card.update({"action": None, "provider": "test", "model": "m"})
    r = await client.post("/api/v1/agent/items", json=card, headers=_h(triage))
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/agent/items", params={"status": "new"}, headers=_h(triage))
    [item] = [row for row in r.json() if row["channel_id"] == g1]
    assert [(s["sub_ref"], s["status"], s["reason"]) for s in item["msg_signals"]] == [
        (f"c{a}", "pending", None),
        (f"c{b}", "skipped", "no_keyword"),
    ]
    assert item["msg_signal"]["sub_ref"] == f"c{a}"  # одиночное поле — подписка с меньшим `id`


async def test_read_marks_keys_of_all_chan_subs(client, db, world, procs, clock):
    await _chan_sub(client, world, A, "G1", keywords=["штраф"])
    await _chan_sub(client, world, B, "G1", keywords=["выплат"])
    messages = [{"is_me": False, "text": "штраф и выплата"}, {"is_me": False, "text": "ничего"}]
    with db.reader() as conn:
        chan_subs.annotate_keys(conn, world["ch"]["G1"], messages)
    assert messages[0]["keys"] == ["штраф", "выплат"] and "keys" not in messages[1]


# --- миграция 0055 (A1) ------------------------------------------------------------------------------------


def _insert_live(db: Db, channel_id: int, session_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
            "session_proc_start, session_id, session_cwd, created_ts) "
            "VALUES (?, NULL, 1, 10, 20, ?, '/x', 0)",
            (channel_id, session_id),
        )
    )


def test_migration_0055_live_sub_per_session(tmp_path, monkeypatch):
    original = db_module._migrations

    def _upto(version):
        return lambda *, main: {v: f for v, f in original(main=main).items() if v <= version}

    monkeypatch.setattr(db_module, "_migrations", _upto(54))
    old = Db(tmp_path)
    old.start()
    try:
        _insert_live(old, 7, "sess-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_live(old, 7, "sess-2")  # прежнее правило: одна живая на беседу на весь хаб
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", _upto(55))
    for attempt in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 55
                rows = conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'index'")
                names = {r[0] for r in rows}
                assert "session_chan_sub_live" not in names
                assert {"session_chan_sub_live_session", "session_chan_sub_live_channel"} <= names
            if attempt == 0:
                _insert_live(new, 7, "sess-2")  # другая сессия той же беседы — можно
            with pytest.raises(sqlite3.IntegrityError):
                _insert_live(new, 7, "sess-2")  # та же сессия — одна живая
            with new.reader() as conn:
                live = conn.execute(
                    "SELECT COUNT(*) FROM main.session_chan_sub WHERE channel_id = 7 AND ended_ts IS NULL"
                ).fetchone()[0]
            assert live == 2
        finally:
            new.close()
