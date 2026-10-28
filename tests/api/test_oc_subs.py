"""Ф16-4 (docs/architecture.md остаток после Р6, П17): личность сессии OpenCode —
`(pid, proc_start, session_id)`. Две сессии `ses_…` на одном живом pid (сервер 4096 держит много сессий) — на
КАЖДОЙ ручке подписок: снятие, ожидание, подписки на беседы, «это ты» (`mine`), живость 10 мин.

Обвязка (живые процессы, два проекта, токены, готовность OpenCode) — как в `test_engine.py` (кусок 16-1).
"""

import asyncio

import pytest

from hub.core import chan_subs, runner, subs
from hub.core import engine as engine_mod

from .conftest import insert_account, insert_channel, insert_project, make_bearer_token
from .test_engine import (
    API,
    OC_A,
    OC_B,
    PID,
    START,
    _bearer,
    _chan_rows,
    _chan_subscribe,
    _sub_rows,
    _subscribe,
    _switch,
)

MIN_MS = 60_000


@pytest.fixture
def procs(monkeypatch):
    """Живые процессы: pid -> starttime (процесс OpenCode с двумя сессиями)."""
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, tmp_path):
    """Два проекта с рабочими папками и включёнными подписками, по беседе в каждом, токен сессии `mcp:all`."""
    dirs = []
    for name in ("umbrella", "initech"):
        folder = tmp_path / name
        folder.mkdir()
        dirs.append(folder)
    first = insert_project(db, name="Umbrella", workdir=str(dirs[0]))
    second = insert_project(db, name="Initech", workdir=str(dirs[1]))
    for project_id in (first, second):
        db.write_sync(lambda conn, pid=project_id: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="mail")
    return {
        "first": first,
        "second": second,
        "dirs": [str(d) for d in dirs],
        "chan": insert_channel(db, acc, project_id=first, ext_id="t1", kind="mail_thread", title="Клиент"),
        "other_chan": insert_channel(db, acc, project_id=second, ext_id="t2", kind="mail_thread"),
        "sess": make_bearer_token(db, kind="interactive", scopes=("read", "draft"), name="mcp:all"),
    }


@pytest.fixture
def oc_ready(monkeypatch, tmp_path, app):
    """OpenCode готов к переключению (П13) — флаг и `engine.json` во временном каталоге конфига."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / engine_mod.SETUP_FILE).write_text('{"opencode": {"path": "/bin/opencode"}}', "utf-8")
    app.state.config_dir = config_dir
    monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(engine_mod, "OPENCODE_READY", True)
    return config_dir


def _token_id(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.api_token WHERE name = 'mcp:all'").fetchone()[0]


def _sess(sid: str, *, pid: int = PID, start: int = START) -> dict:
    return {"pid": pid, "proc_start": start, "session_id": sid}


async def _oc(client, db, clock) -> None:
    r = await _switch(client, db, clock, "opencode")
    assert r.status_code == 200, r.text


async def _two_project_subs(client, world) -> tuple[int, int]:
    a = await _subscribe(client, world, sid=OC_A, kind="opencode", project_id=world["first"])
    assert a.status_code == 200, a.text
    b = await _subscribe(
        client, world, sid=OC_B, kind="opencode", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert b.status_code == 200, b.text
    return a.json()["sub"]["id"], b.json()["sub"]["id"]


async def _two_chan_subs(client, world) -> tuple[int, int]:
    a = await _chan_subscribe(client, world, world["chan"], sid=OC_A, kind="opencode")
    assert a.status_code == 200, a.text
    b = await _chan_subscribe(
        client, world, world["other_chan"], sid=OC_B, kind="opencode", cwd=world["dirs"][1]
    )
    assert b.status_code == 200, b.text
    return a.json()["sub"]["id"], b.json()["sub"]["id"]


# --- /subs/{id}/end и ожидание подписки на проект ---------------------------------------------------------


async def test_end_by_other_oc_session_of_same_pid_refused(client, db, clock, world, procs, oc_ready):
    await _oc(client, db, clock)
    a_id, b_id = await _two_project_subs(client, world)
    r = await client.post(
        f"{API}/subs/{a_id}/end",
        json={"session": _sess(OC_B), "reason": "unsubscribed"},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 403, r.text
    assert all(row[3] is None for row in _sub_rows(db))
    # Своя сессия снимает свою; чужая подписка того же процесса жива.
    r = await client.post(
        f"{API}/subs/{a_id}/end",
        json={"session": _sess(OC_A), "reason": "unsubscribed"},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 200 and r.json()["sub"]["end_reason"] == "unsubscribed"
    rows = {row[0]: row for row in _sub_rows(db)}
    assert rows[a_id][3] is not None and rows[b_id][3] is None


async def test_wait_by_other_oc_session_is_403_not_conversation_changed(
    client, db, clock, world, procs, oc_ready
):
    await _oc(client, db, clock)
    a_id, _b_id = await _two_project_subs(client, world)
    r = await client.post(
        f"{API}/subs/{a_id}/wait",
        json={"session": _sess(OC_B), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 403, r.text
    assert all(row[3] is None for row in _sub_rows(db))  # подписку A не сняли
    r = await client.post(
        f"{API}/subs/{a_id}/wait",
        json={"session": _sess(OC_A), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 200 and r.json()["signals"] == []


# --- подписки на беседы ---------------------------------------------------------------------------------


async def test_chan_waiters_of_two_oc_sessions_do_not_supersede(
    client, db, clock, world, procs, oc_ready, app
):
    await _oc(client, db, clock)
    await _two_chan_subs(client, world)

    def wait(sid, timeout_s, waiter):
        return client.post(
            f"{API}/chan-subs/wait",
            json={"session": _sess(sid), "waiter": waiter, "timeout_s": timeout_s},
            headers=_bearer(world["sess"]),
        )

    token_id = _token_id(db)
    waiters = app.state.chan_sub_waiters
    first = asyncio.create_task(wait(OC_A, 2, "0123456789abcdef"))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(wait(OC_B, 1, "fedcba9876543210"))
    await asyncio.sleep(0.2)
    # Оба ожидания идут одновременно, каждое — под своим ключом с `session_id`.
    assert waiters.waiting(chan_subs.session_key(token_id, PID, START, OC_A), OC_A)
    assert waiters.waiting(chan_subs.session_key(token_id, PID, START, OC_B), OC_B)
    r2 = await asyncio.wait_for(second, 5)
    r1 = await asyncio.wait_for(first, 5)
    assert r1.status_code == 200 and r1.json() == {"signals": []}, r1.text
    assert r2.status_code == 200 and r2.json() == {"signals": []}, r2.text
    assert all(row[3] is None for row in _chan_rows(db))


async def test_chan_wait_of_oc_session_without_own_subs_is_404_not_conversation_changed(
    client, db, clock, world, procs, oc_ready
):
    await _oc(client, db, clock)
    a = await _chan_subscribe(client, world, world["chan"], sid=OC_A, kind="opencode")
    assert a.status_code == 200, a.text
    r = await client.post(
        f"{API}/chan-subs/wait",
        json={"session": _sess(OC_B), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 404, r.text  # у B подписок нет; подписка A — не «сменившийся разговор»
    assert all(row[3] is None for row in _chan_rows(db))


async def test_chan_end_of_oc_session_leaves_other_session(client, db, clock, world, procs, oc_ready):
    await _oc(client, db, clock)
    a_id, b_id = await _two_chan_subs(client, world)
    # Ф39 A3: беседу слушает только другая сессия — своей подписки нет: 404 (было 403), подписка A жива.
    r = await client.post(
        f"{API}/chan-subs/end",
        json={"session": _sess(OC_B), "channel_id": world["chan"]},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 404, r.text
    assert {row[0]: row for row in _chan_rows(db)}[a_id][3] is None
    # «Все свои» у B — только его беседа; A не тронута.
    r = await client.post(
        f"{API}/chan-subs/end", json={"session": _sess(OC_B)}, headers=_bearer(world["sess"])
    )
    assert r.status_code == 200, r.text
    assert [row["id"] for row in r.json()["items"]] == [b_id]
    rows = {row[0]: row for row in _chan_rows(db)}
    assert rows[a_id][3] is None and rows[b_id][4] == "unsubscribed"
    # Повтор у B — своих нет, у процесса есть подписка A: у OpenCode это не 410 `conversation_changed`.
    r = await client.post(
        f"{API}/chan-subs/end", json={"session": _sess(OC_B)}, headers=_bearer(world["sess"])
    )
    assert r.status_code == 200 and r.json()["items"] == []
    assert _chan_rows(db)[0][3] is None


# --- П17: поле `mine` -------------------------------------------------------------------------------------


async def test_get_subs_mine_by_full_triple(client, db, clock, world, procs, oc_ready):
    await _oc(client, db, clock)
    a_id, b_id = await _two_project_subs(client, world)
    ca, cb = await _two_chan_subs(client, world)
    r = await client.get(
        f"{API}/subs",
        params={"live": 1, "pid": PID, "proc_start": START, "session_id": OC_A},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert {row["id"]: row["mine"] for row in body["items"]} == {a_id: True, b_id: False}
    assert {row["id"]: row["mine"] for row in body["channels"]} == {ca: True, cb: False}
    # Без своей сессии — поля нет (как раньше); неполная тройка — 422.
    plain = (await client.get(f"{API}/subs", headers=_bearer(world["sess"]))).json()
    assert all("mine" not in row for row in plain["items"] + plain["channels"])
    r = await client.get(f"{API}/subs", params={"pid": PID}, headers=_bearer(world["sess"]))
    assert r.status_code == 422


# --- П1: живость подписки OpenCode — 10 мин без ожидания ------------------------------------------------


async def test_oc_sub_without_wait_for_10_min_is_session_gone(client, db, clock, world, procs, oc_ready, app):
    await _oc(client, db, clock)
    a_id, b_id = await _two_project_subs(client, world)
    ca, cb = await _two_chan_subs(client, world)
    sub_waiters = app.state.sub_waiters
    chan_waiters = app.state.chan_sub_waiters
    token_id = _token_id(db)
    t0 = int(clock() * 1000)

    def cleanup(now_ms):
        return db.write_sync(lambda conn: runner._cleanup_all(conn, now_ms, sub_waiters, chan_waiters))

    # Моложе 10 мин — живы.
    assert cleanup(t0 + 9 * MIN_MS) == []
    # B недавно ждал (подписка на проект и ожидание бесед) — живы; A не ждал — сняты.
    db.write_sync(lambda conn: subs.touch_wait(conn, b_id, now_ms=t0 + 5 * MIN_MS))
    db.write_sync(lambda conn: chan_subs.touch_wait(conn, [cb], now_ms=t0 + 5 * MIN_MS))
    ended = cleanup(t0 + 11 * MIN_MS)
    assert sorted(ended) == sorted([(str(a_id), "session_gone"), (f"c{ca}", "session_gone")])
    # Идёт ожидание — жива, хоть последнее и старое.
    mark, _ = sub_waiters.enter(b_id)
    chan_mark, _ = chan_waiters.enter(chan_subs.session_key(token_id, PID, START, OC_B), OC_B)
    try:
        assert cleanup(t0 + 30 * MIN_MS) == []
    finally:
        sub_waiters.leave(b_id, mark)
        chan_waiters.leave(chan_subs.session_key(token_id, PID, START, OC_B), chan_mark)
    assert sorted(cleanup(t0 + 30 * MIN_MS)) == sorted(
        [(str(b_id), "session_gone"), (f"c{cb}", "session_gone")]
    )


async def test_claude_sub_liveness_unchanged(client, db, clock, world, procs, oc_ready, app):
    """Подписка Claude Code живёт, пока жив процесс, — без правила 10 минут."""
    r = await _subscribe(client, world)
    assert r.status_code == 200, r.text
    t0 = int(clock() * 1000)
    ended = db.write_sync(
        lambda conn: runner._cleanup_all(
            conn, t0 + 24 * 60 * MIN_MS, app.state.sub_waiters, app.state.chan_sub_waiters
        )
    )
    assert ended == []
    assert _sub_rows(db)[0][3] is None


async def test_stale_oc_sub_not_live_in_get(client, db, clock, world, procs, oc_ready):
    await _oc(client, db, clock)
    a_id, _b = await _two_project_subs(client, world)
    clock.advance_ms(11 * MIN_MS)
    rows = (await client.get(f"{API}/subs", headers=_bearer(world["sess"]))).json()["items"]
    assert {row["id"]: row["live"] for row in rows}[a_id] is False


def test_oc_stale_rule():
    rec = subs.SubRec(1, 1, 1, PID, START, OC_A, "/w", None, 0, None, None, None, None)
    assert not subs.oc_stale(rec, 10 * MIN_MS, waiting=False)
    assert subs.oc_stale(rec, 10 * MIN_MS + 1, waiting=False)
    assert not subs.oc_stale(rec, 60 * MIN_MS, waiting=True)
    assert not subs.oc_stale(rec._replace(last_wait_ts=55 * MIN_MS), 60 * MIN_MS, waiting=False)
    assert not subs.oc_stale(rec._replace(session_id="0f6f6d6a-9c3d"), 60 * MIN_MS, waiting=False)
    assert engine_mod.engine_of_session(None, OC_A) == "opencode"
