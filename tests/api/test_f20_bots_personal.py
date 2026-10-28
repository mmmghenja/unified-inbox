"""Ф20-1 (docs/architecture.md и поправки П1–П12): боты, `@channel` и режим
подписки на проект «лично мне», ядро.

- F1: миграции 0033/0034 на прошлых версиях обоих файлов.
- F3: внимание — бот не «реагирую», `@channel` не «для меня» (B1/П7).
- F5/П2/П6: фильтр `personal` и `all` (боты), строка `skipped` с причиной, сигнал после строки треда.
- F6/П10: категория сигнала и `thread_ext`.
- П1/П3: `msg_filter` в `POST /subs`, смена на месте, клиент протокола 2.

Стиль — как test_sub_messages_mode.py: публичное приложение через ASGITransport, события через
`ingest.apply_batch`, `/proc` подменён словарём."""

import json

import pytest

from hub.core import attention, chan_subs, ingest, queries, security, subs
from hub.core import db as db_module
from hub.core.db import Db

from .conftest import insert_account, insert_channel, insert_message, insert_project

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
UI = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str, protocol: str | None = "3") -> dict:
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    if protocol is not None:
        headers["X-Hub-Sub-Wait"] = protocol
    return headers


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Globex", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    sl = insert_account(db, source="slack")
    tg = insert_account(db, source="tg")
    ml = insert_account(db, source="mail", db_name="work")
    ch = {}
    for ext, kind in (("D1", "dm"), ("MP", "group"), ("C1", "group"), ("C2", "group"), ("C3", "group")):
        ch[ext] = insert_channel(db, sl, project_id=pid, ext_id=ext, kind=kind)
    for ext in ("C4", "C5", "C6"):
        ch[ext] = insert_channel(db, sl, project_id=pid, ext_id=ext, kind="group")
    ch["G1"] = insert_channel(db, tg, project_id=pid, ext_id="G1", kind="group")
    ch["M1"] = insert_channel(db, ml, project_id=pid, ext_id="M1", kind="mail_thread", db_name="work")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET meta_json = ? WHERE id = ?",
            (json.dumps({"attrs": {"slack_type": "mpim"}}), ch["MP"]),
        )
    )
    return {
        "pid": pid,
        "workdir": str(workdir),
        "acc": {"slack": sl, "tg": tg, "mail": ml},
        "ch": ch,
        "tok": _token(db),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


def _session(w, *, pid=PID, start=START, sid=SID, full=True):
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


async def _subscribe(client, w, *, mode=None, msg_filter=None, expect=200):
    body: dict = {"session": _session(w)}
    if mode is not None:
        body["mode"] = mode
    if msg_filter is not None:
        body["msg_filter"] = msg_filter
    r = await client.post("/api/v1/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, *, protocol="3"):
    return await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"], protocol),
    )


def _ev(
    chat: str,
    ext: str,
    ts: int,
    *,
    kind: str = "group",
    sender: str = "U2",
    is_me: bool = False,
    bot: bool = False,
    mention: bool = False,
    broadcast: bool = False,
    thread: str | None = None,
    reply_to: str | None = None,
    text: str = "текст",
    edited_ts: int | None = None,
) -> dict:
    snd: dict = {"ext_id": "U1", "is_me": True} if is_me else {"ext_id": sender, "name": "Пётр"}
    if bot:
        snd["is_bot"] = True
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind},
        "ext_id": ext,
        "ts": ts,
        "sender": snd,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": mention or broadcast,
    }
    if broadcast:
        event["mentions_broadcast_only"] = True
    if thread is not None:
        event["thread_ext_id"] = thread
    if reply_to is not None:
        event["reply_to_ext_id"] = reply_to
    if edited_ts is not None:
        event["edited_ts"] = edited_ts
    return event


def _ingest(db, account_id, events, now_ms):
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0, result


def _now(clock) -> int:
    return int(clock() * 1000)


def _signals(db) -> dict[int, list[tuple]]:
    """Беседа -> строки сигналов `(status, reason, msg_count, cat, thread_ext, last_message_id)`."""
    out: dict[int, list[tuple]] = {}
    with db.reader() as conn:
        for row in conn.execute(
            "SELECT channel_id, status, reason, msg_count, cat, thread_ext, last_message_id "
            "FROM main.session_chan_signal ORDER BY id"
        ):
            out.setdefault(row[0], []).append(tuple(row[1:]))
    return out


def _msg_id(db, channel_id, ext):
    schema = db_module.db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext),
        ).fetchone()[0]


# --- F1: миграции 0033/0034 ---------------------------------------------------------------------------------


def test_f1_migrations_0033_0034_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 32}
    )
    old = Db(tmp_path)
    old.start()
    try:
        acc_main = insert_account(old, source="slack")
        acc_work = insert_account(old, source="slack", db_name="work")
        cm = insert_channel(old, acc_main, ext_id="c1")
        cw = insert_channel(old, acc_work, ext_id="c1", db_name="work")
        insert_message(old, cm, ext_id="1", ts=1)
        insert_message(old, cw, ext_id="1", ts=1)

        def _old_rows(conn):
            for project_id, mode in ((1, "cards"), (2, "messages")):
                conn.execute(
                    "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, "
                    "session_proc_start, session_id, session_cwd, created_ts, mode) "
                    "VALUES (?, 1, 1, 1, 's', '/', 0, ?)",
                    (project_id, mode),
                )
            conn.execute(
                "INSERT INTO main.session_chan_signal (project_sub_id, channel_id, first_message_id, "
                "last_message_id, msg_count, status, created_ts, updated_ts) "
                "VALUES (2, 1, 1, 1, 1, 'pending', 0, 0)"
            )

        old.write_sync(_old_rows)
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 32
            ids_before = conn.execute("SELECT id, mode FROM main.session_sub ORDER BY id").fetchall()
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 34
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] >= 33
            for schema in ("main", "work"):
                assert conn.execute(f"SELECT is_bot FROM {schema}.message").fetchall() == [(0,)]  # noqa: S608
            assert conn.execute("SELECT id, mode FROM main.session_sub ORDER BY id").fetchall() == ids_before
            assert conn.execute("SELECT msg_filter FROM main.session_sub").fetchall() == [("all",), ("all",)]
            assert conn.execute("SELECT cat, thread_ext FROM main.session_chan_signal").fetchall() == [
                (None, None)
            ]
        new.write_sync(
            lambda conn: conn.execute("UPDATE main.session_sub SET msg_filter = 'personal' WHERE id = 2")
        )
        with pytest.raises(Exception, match="CHECK"):
            new.write_sync(lambda conn: conn.execute("UPDATE main.session_sub SET msg_filter = 'junk'"))
    finally:
        new.close()


# --- F3: внимание -------------------------------------------------------------------------------------------


def _messages(db, channel_id) -> dict[str, dict]:
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, channel_id, before=None, limit=200)
    return {m["ext_id"]: m for m in page.messages}


def _unread_react(db, channel_id) -> int:
    with db.reader() as conn:
        return queries.get_channel(conn, UI, channel_id)["unread_react"]


def test_f3_message_attention_python():
    assert attention.message_attention("react", 0, "dm", 1) == "info"
    assert attention.message_attention("info", 1, "group", 1) == "info"
    assert attention.message_attention("digest", 1, "group", 1) == "digest"
    assert attention.message_attention("muted", 1, "dm", 1) == "muted"
    # без признака (прямые вызовы старых тестов) — как раньше
    assert attention.message_attention("react", 0, "dm") == "react"
    assert attention.message_attention("info", 1, "group") == "react"


def test_f3_bot_in_dm_and_mention_in_info_not_react(db, world, clock):
    now = _now(clock)
    d1, c1 = world["ch"]["D1"], world["ch"]["C1"]
    _ingest(
        db,
        world["acc"]["slack"],
        [
            _ev("D1", "1.0", now, kind="dm", bot=True),
            _ev("C1", "2.0", now, bot=True, mention=True),
            _ev("C1", "3.0", now + 1, broadcast=True),
            _ev("C1", "4.0", now + 2, mention=True),
        ],
        now,
    )
    dm = _messages(db, d1)["1.0"]
    assert (dm["is_bot"], dm["attention"]) == (True, "info")
    assert _unread_react(db, d1) == 0
    top = _messages(db, c1)
    assert (top["2.0"]["is_bot"], top["2.0"]["for_me"], top["2.0"]["attention"]) == (True, True, "info")
    assert (top["3.0"]["mentions_broadcast"], top["3.0"]["for_me"], top["3.0"]["attention"]) == (
        True,
        False,
        "info",
    )
    assert (top["4.0"]["mentions_broadcast"], top["4.0"]["is_bot"], top["4.0"]["for_me"]) == (
        False,
        False,
        True,
    )
    assert top["4.0"]["attention"] == "react"
    assert _unread_react(db, c1) == 1
    # «Нужна реакция» на главной — тот же предикат: только личное упоминание
    with db.reader() as conn:
        last = attention.last_unread_react(conn, "main", [d1, c1])
    assert set(last) == {c1} and last[c1][0] == top["4.0"]["id"]


def test_f3_bot_flag_written_on_insert_only(db, world, clock):
    """П8: правка/сверка признак не меняют (ветка обновления пишет только общий словарь)."""
    now = _now(clock)
    d1 = world["ch"]["D1"]
    _ingest(db, world["acc"]["slack"], [_ev("D1", "1.0", now, kind="dm", bot=True)], now)
    _ingest(
        db,
        world["acc"]["slack"],
        [_ev("D1", "1.0", now, kind="dm", text="правка", edited_ts=now + 5)],
        now + 5,
    )
    msg = _messages(db, d1)["1.0"]
    assert (msg["text"], msg["is_bot"]) == ("правка", True)


# --- F5 + П2 + П6: фильтр «лично мне» -----------------------------------------------------------------------


def _personal_events(now):
    return {
        "slack": [
            _ev("D1", "1.0", now, kind="dm"),
            _ev("MP", "1.0", now),
            _ev("C1", "2.0", now),  # обычное сообщение канала
            _ev("C1", "4.0", now + 1, broadcast=True),  # @channel
            _ev("C1", "5.0", now + 2, bot=True, mention=True),  # бот, даже с упоминанием
            _ev("C2", "3.0", now, mention=True),  # личное упоминание
            _ev("C3", "6.0", now, is_me=True),  # мой родитель (строки треда ещё нет)
            _ev("C3", "6.1", now + 1, thread="6.0"),  # первый чужой ответ на него — П2
            _ev("C4", "7.0", now, is_me=True),  # своё — ничего
        ],
        "tg": [
            _ev("G1", "100", now, is_me=True),
            _ev("G1", "101", now + 1, reply_to="100"),  # ответ на сообщение владельца
        ],
        "mail": [_ev("M1", "<m1@x>", now, kind="mail_thread", mention=True)],  # письмо мне (П9)
    }


async def test_f5_personal_signals_and_filtered_rows(client, db, world, procs, clock):
    body = await _subscribe(client, world, mode="messages", msg_filter="personal")
    sub = body["sub"]
    assert (sub["mode"], sub["msg_filter"]) == ("messages", "personal")
    now = _now(clock)
    for source, events in _personal_events(now).items():
        _ingest(db, world["acc"][source], events, now)
    ch = world["ch"]
    got = _signals(db)
    pend = {cid: rows[0][3:5] for cid, rows in got.items() if rows[0][0] == "pending"}
    assert pend == {
        ch["D1"]: ("dm", None),
        ch["MP"]: ("dm", None),
        ch["C2"]: ("mention", None),
        ch["C3"]: ("thread", "6.0"),
        ch["G1"]: ("thread", None),
        ch["M1"]: ("mention", None),
    }
    # П6: отфильтрованное — одна строка `skipped` на причину, последнее сообщение и число дописываются
    assert got[ch["C1"]] == [
        ("skipped", "not_personal", 2, None, None, _msg_id(db, ch["C1"], "4.0")),
        ("skipped", "bot", 1, None, None, _msg_id(db, ch["C1"], "5.0")),
    ]
    assert ch["C4"] not in got
    # правка и удаление не сигналят
    _ingest(
        db,
        world["acc"]["slack"],
        [
            _ev("D1", "1.0", now, kind="dm", text="правка", edited_ts=now + 9),
            {"type": "delete", "ext_ids": ["3.0"], "channel": {"ext_id": "C2", "kind": "group"}},
        ],
        now + 10,
    )
    after = _signals(db)
    assert after[ch["D1"]][0][2] == 1 and after[ch["C2"]][0][2] == 1
    # выдача: категория и родитель треда
    r = await _wait(client, world, sub["id"])
    assert r.status_code == 200, r.text
    by_channel = {s["channel_id"]: (s["kind"], s["cat"], s["thread_ext"]) for s in r.json()["signals"]}
    assert by_channel[ch["C3"]] == ("messages", "thread", "6.0")
    assert by_channel[ch["D1"]] == ("messages", "dm", None)
    assert ch["C1"] not in by_channel
    with db.reader() as conn:
        details = [
            json.loads(d)
            for (d,) in conn.execute("SELECT detail_json FROM main.audit WHERE action = 'sub_signal'")
        ]
    assert {d["msg_filter"] for d in details} == {"personal"}


async def test_f5_messages_all_skips_bots_chan_sub_keeps_them(client, db, world, procs, clock):
    await _subscribe(client, world, mode="messages")
    # Ф26-1: подписка на беседу глушит подписку на проект только той же сессии
    r = await client.post(
        "/api/v1/chan-subs",
        json={
            "channel_id": world["ch"]["C2"],
            "session": _session(world),
        },
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    now = _now(clock)
    _ingest(
        db,
        world["acc"]["slack"],
        [
            _ev("C1", "1.0", now, bot=True),
            _ev("C1", "2.0", now + 1),
            _ev("C1", "3.0", now + 2, broadcast=True),
            _ev("C2", "1.0", now, bot=True),  # подписка на беседу — с ботами (A6)
        ],
        now,
    )
    got = _signals(db)
    c1, c2 = world["ch"]["C1"], world["ch"]["C2"]
    assert sorted(got[c1]) == sorted(
        [
            ("skipped", "bot", 1, None, None, _msg_id(db, c1, "1.0")),
            ("pending", None, 2, "other", None, _msg_id(db, c1, "3.0")),
        ]
    )
    assert got[c2] == [("pending", None, 1, "other", None, _msg_id(db, c2, "1.0"))]


# --- F6 + П10: категория — сильнейшая, `thread_ext` — у давшего её, только формат `ts` ----------------------


async def test_f6_category_strongest_and_thread_ext(client, db, world, procs, clock):
    await _subscribe(client, world, mode="messages")
    now = _now(clock)
    sl = world["acc"]["slack"]
    c3, c5, c6 = world["ch"]["C3"], world["ch"]["C5"], world["ch"]["C6"]

    def row(cid):
        return _signals(db)[cid][-1][3:5]

    _ingest(db, sl, [_ev("C3", "10.0", now, is_me=True), _ev("C3", "11.0", now + 1)], now)
    assert row(c3) == ("other", None)
    _ingest(db, sl, [_ev("C3", "10.1", now + 2, thread="10.0")], now)
    assert row(c3) == ("thread", "10.0")
    _ingest(db, sl, [_ev("C3", "12.0", now + 3)], now)  # слабее — категория и тред остаются
    assert row(c3) == ("thread", "10.0")
    _ingest(db, sl, [_ev("C3", "13.0", now + 4, mention=True)], now)
    assert row(c3) == ("mention", None)
    _ingest(db, sl, [_ev("C3", "10.2", now + 5, thread="10.0")], now)
    assert row(c3) == ("mention", None)

    # равенство — у последнего: два моих треда
    _ingest(
        db,
        sl,
        [
            _ev("C5", "20.0", now, is_me=True),
            _ev("C5", "21.0", now, is_me=True),
            _ev("C5", "20.1", now + 1, thread="20.0"),
            _ev("C5", "21.1", now + 2, thread="21.0"),
        ],
        now,
    )
    assert row(c5) == ("thread", "21.0")

    # родитель не формата `ts` — категория тред, но `thread_ext` нет
    _ingest(db, sl, [_ev("C6", "555", now, is_me=True), _ev("C6", "556", now + 1, thread="555")], now)
    assert row(c6) == ("thread", None)


def test_p10_old_null_cat_is_message(db, world, clock):
    """`cat IS NULL` (строки до 0034) — «сообщение»; новое более сильное перекрывает."""
    now = _now(clock)
    sub_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts, mode) VALUES (?, 1, ?, ?, ?, ?, 0, 'messages')",
                (world["pid"], PID, START, SID, world["workdir"]),
            ).lastrowid
        )
    )
    c1 = world["ch"]["C1"]
    _ingest(db, world["acc"]["slack"], [_ev("C1", "1.0", now)], now)
    db.write_sync(lambda conn: conn.execute("UPDATE main.session_chan_signal SET cat = NULL"))
    _ingest(db, world["acc"]["slack"], [_ev("C1", "2.0", now + 1)], now)
    assert _signals(db)[c1] == [("pending", None, 2, "other", None, _msg_id(db, c1, "2.0"))]
    with db.reader() as conn:
        assert subs.get_sub(conn, sub_id).msg_filter == "all"


# --- П1: `msg_filter` в `POST /subs`, смена на месте --------------------------------------------------------


async def test_p1_msg_filter_validation_and_switch_in_place(client, db, world, procs, clock):
    await _subscribe(client, world, mode="cards", msg_filter="personal", expect=422)
    await _subscribe(client, world, msg_filter="personal", expect=422)
    first = await _subscribe(client, world, mode="messages")
    sub_id = first["sub"]["id"]
    assert first["sub"]["msg_filter"] == "all"
    now = _now(clock)
    _ingest(db, world["acc"]["slack"], [_ev("C1", "1.0", now)], now)
    switched = await _subscribe(client, world, mode="messages", msg_filter="personal")
    assert switched["created"] is False and switched["sub"]["id"] == sub_id
    assert switched["sub"]["msg_filter"] == "personal"
    # ожидающий сигнал на момент смены фильтра выдаётся как есть
    assert _signals(db)[world["ch"]["C1"]][0][0] == "pending"
    # без `mode` — ничего не меняется
    assert (await _subscribe(client, world))["sub"]["msg_filter"] == "personal"
    back = await _subscribe(client, world, mode="messages")  # `mode` без фильтра — `all`
    assert back["sub"]["msg_filter"] == "all"
    cards = await _subscribe(client, world, mode="cards")
    assert (cards["sub"]["mode"], cards["sub"]["msg_filter"]) == ("cards", "all")
    with db.reader() as conn:
        modes = [
            json.loads(d)
            for (d,) in conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'sub_mode' ORDER BY id"
            )
        ]
    assert modes == [
        {"sub_id": sub_id, "mode": "messages", "msg_filter": "personal"},
        {"sub_id": sub_id, "mode": "messages", "msg_filter": "all"},
        {"sub_id": sub_id, "mode": "cards"},
    ]
    listed = (await client.get("/api/v1/subs", headers=_h(world["tok"]))).json()["items"]
    assert listed[0]["msg_filter"] == "all"


async def test_p3_protocol_2_client_still_gets_personal_signals(client, db, world, procs, clock):
    sub_id = (await _subscribe(client, world, mode="messages", msg_filter="personal"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world["acc"]["slack"], [_ev("D1", "1.0", now, kind="dm")], now)
    r = await _wait(client, world, sub_id, protocol="2")
    assert r.status_code == 200, r.text
    (signal,) = r.json()["signals"]
    assert signal["kind"] == "messages" and signal["channel_id"] == world["ch"]["D1"]
    # клиент без заголовка (старше Ф14-5) — как раньше 409
    r = await _wait(client, world, sub_id, protocol=None)
    assert r.status_code == 409


# --- П6: карточка по отфильтрованному — дежурная не будится -------------------------------------------------


async def _card(client, w, channel_id, message_ids):
    r = await client.post(
        "/api/v1/agent/items",
        json={
            "channel_id": channel_id,
            "message_ids": message_ids,
            "label": "reply",
            "summary": "сводка",
            "action": None,
            "provider": "test",
            "model": "m",
        },
        headers=_h(w["triage"], None),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _items(client, w) -> dict[int, dict]:
    r = await client.get("/api/v1/agent/items", params={"status": "any"}, headers=_h(w["tok"], None))
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body if isinstance(body, list) else body["items"]
    return {item["id"]: item for item in rows}


async def test_p6_card_by_filtered_message_does_not_wake(client, db, world, procs, clock):
    await _subscribe(client, world, mode="messages", msg_filter="personal")
    now = _now(clock)
    ch = world["ch"]
    _ingest(db, world["acc"]["slack"], [_ev("C1", "1.0", now, bot=True), _ev("C2", "1.0", now)], now)
    bot_card = await _card(client, world, ch["C1"], [_msg_id(db, ch["C1"], "1.0")])
    plain_card = await _card(client, world, ch["C2"], [_msg_id(db, ch["C2"], "1.0")])
    items = await _items(client, world)
    for item_id, reason in ((bot_card, "bot"), (plain_card, "not_personal")):
        signal = items[item_id]["msg_signal"]
        assert (signal["status"], signal["reason"]) == ("skipped", reason)

    # позже в той же беседе — личное упоминание: обычный сигнал покрывает карточку, запасной путь — по нему
    _ingest(db, world["acc"]["slack"], [_ev("C2", "2.0", now + 10, mention=True)], now + 10)
    mid = _msg_id(db, ch["C2"], "2.0")
    plain_card = await _card(client, world, ch["C2"], [_msg_id(db, ch["C2"], "1.0"), mid])
    item = (await _items(client, world))[plain_card]
    assert (item["msg_signal"]["status"], item["msg_signal"]["cat"]) == ("pending", "mention")
    # и наоборот: после личного — бот; пока личное не выдано, строка фильтра его не перекрывает (после ревью):
    # дежурная ждёт, а глухой сессии — запасной путь
    _ingest(db, world["acc"]["slack"], [_ev("C2", "3.0", now + 30, bot=True)], now + 30)
    plain_card = await _card(client, world, ch["C2"], [_msg_id(db, ch["C2"], "3.0")])
    item = (await _items(client, world))[plain_card]
    assert item["msg_signal"]["status"] == "pending"


async def _filtered_after_pending(client, db, world, clock, *, msg_filter, later_kwargs):
    # После ревью (.runs/critic-f201/): личное `pending` (сессия глухая), следом отфильтрованное — карточка по
    # личному не должна получить «сессия получила» от строки фильтра.
    await _subscribe(client, world, mode="messages", msg_filter=msg_filter)
    now = _now(clock)
    ch = world["ch"]
    _ingest(db, world["acc"]["slack"], [_ev("C2", "1.0", now, mention=True)], now)
    card = await _card(client, world, ch["C2"], [_msg_id(db, ch["C2"], "1.0")])
    _ingest(db, world["acc"]["slack"], [_ev("C2", "2.0", now + 10, **later_kwargs)], now + 10)
    item = (await _items(client, world))[card]
    assert item["msg_signal"]["status"] == "pending"


async def test_review_bot_after_pending_mention_keeps_fallback(client, db, world, procs, clock):
    await _filtered_after_pending(client, db, world, clock, msg_filter="all", later_kwargs={"bot": True})


async def test_review_not_personal_after_pending_mention_keeps_fallback(client, db, world, procs, clock):
    await _filtered_after_pending(client, db, world, clock, msg_filter="personal", later_kwargs={})


def test_message_cat_direct(db, world, clock):
    now = _now(clock)
    _ingest(db, world["acc"]["slack"], [_ev("C1", "9.0", now, bot=True, mention=True)], now)
    with db.reader() as conn:
        info = chan_subs.message_cat(conn, _msg_id(db, world["ch"]["C1"], "9.0"))
    assert info == chan_subs.MessageCat(True, "mention", None)
