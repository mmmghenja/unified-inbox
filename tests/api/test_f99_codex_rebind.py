"""Ф99-0 (docs/architecture.md; долг 23.10 «Codex теряет подписку между ходами»):
MCP-сервер окна Codex — потомок общего демона `codex app-server`, а не окна. Личность подписки внешней
сессии — разговор `codex:<id>`: смена процесса (демон перезапустился) при том же разговоре переносит живые
подписки на новый процесс (`subs.ext_rebind`, `POST /subs/rebind`, повторная подписка) — роль и номер те
же, «роль 2» не заводится; живость строки — срок без ожидания профиля (`idle_grace_ms`), а не `/proc`.

`/proc` подменён словарём `subs.read_proc_start`, часы — `FakeClock`."""

import pytest

from hub.core import runner, security, subs

from .conftest import insert_account, insert_channel, insert_project

ORIGIN = "http://127.0.0.1:8787"
API = "/api/v1"
MIN = 60_000
DAEMON = (4242, 777_000)  # общий демон `codex app-server`
DAEMON_2 = (4343, 778_000)  # тот же демон после перезапуска
CLAUDE = (5151, 888_000, "0f6f6d6a-9c3d-4d3f-9c9c-2f1a0e5d4c3b")
CX_A = "codex:019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a51"
CX_B = "codex:019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a52"
DEV = "разработчик агента"
LEAD = "головная"


@pytest.fixture
def procs(monkeypatch):
    table = {DAEMON[0]: DAEMON[1], CLAUDE[0]: CLAUDE[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _token(db, name="mcp:all") -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind="interactive", scopes=["read", "draft"], project_ids=None, expires_ts=None
        )
    ).token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "4"}


@pytest.fixture
def world(db, tmp_path, procs):
    (tmp_path / "w1").mkdir()
    p1 = insert_project(db, name="Umbrella", workdir=str(tmp_path / "w1"))
    db.write_sync(lambda conn: subs.set_project_subs(conn, p1, True, now_ms=0))
    acc = insert_account(db, source="mail")
    return {
        "p1": p1,
        "dir": str(tmp_path / "w1"),
        "chan": insert_channel(db, acc, project_id=p1, ext_id="t1", kind="mail_thread"),
        "tok": _token(db),
        "other": _token(db, name="mcp:other"),
    }


def _sess(sid: str, proc=DAEMON) -> dict:
    return {"pid": proc[0], "proc_start": proc[1], "session_id": sid}


def _full(sid: str, cwd: str, proc=DAEMON) -> dict:
    kind = "codex" if sid.startswith("codex:") else "interactive"
    return {**_sess(sid, proc), "cwd": cwd, "kind": kind, "name": None, "name_source": None}


async def _sub(client, w, sid, proc=DAEMON, *, role=DEV, token=None):
    body = {"session": _full(sid, w["dir"], proc), "role": role}
    r = await client.post(f"{API}/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def _chan_sub(client, w, sid, proc=DAEMON):
    body = {"channel_id": w["chan"], "session": _full(sid, w["dir"], proc)}
    r = await client.post(f"{API}/chan-subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


def _rows(db, table="session_sub") -> list[tuple]:
    no = "role_no" if table == "session_sub" else "NULL"
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, session_pid, session_proc_start, session_id, {no}, ended_ts "  # noqa: S608
            f"FROM main.{table} ORDER BY id"
        ).fetchall()


def _cleanup(db, app, now_ms):
    return db.write_sync(
        lambda conn: runner._cleanup_all(conn, now_ms, app.state.sub_waiters, app.state.chan_sub_waiters)
    )


def _restart_daemon(procs):
    del procs[DAEMON[0]]
    procs[DAEMON_2[0]] = DAEMON_2[1]


async def test_two_windows_through_one_daemon_are_two_subscriptions(client, db, world):
    a = await _sub(client, world, CX_A)
    b = await _sub(client, world, CX_B)
    assert a["sub"]["id"] != b["sub"]["id"]
    assert [(r[3], r[4], r[5]) for r in _rows(db)] == [(CX_A, 1, None), (CX_B, 2, None)]
    # Переезд одного разговора не трогает соседа по демону.
    r = await client.post(f"{API}/subs/rebind", json={"session": _sess(CX_A)}, headers=_h(world["tok"]))
    assert r.status_code == 200 and r.json() == {"rebound": 0}, r.text


async def test_daemon_restart_keeps_line_and_agent_letters_arrive(client, db, clock, app, world, procs):
    first = await _sub(client, world, CX_A)
    sub_id = first["sub"]["id"]
    chan = await _chan_sub(client, world, CX_A)
    claude = {"session": _full(CLAUDE[2], world["dir"], CLAUDE[:2]), "role": LEAD}
    assert (await client.post(f"{API}/subs", json=claude, headers=_h(world["tok"]))).status_code == 200

    _restart_daemon(procs)
    # Демон мёртв, разговор жив: чистка строку внешней сессии по `/proc` не снимает.
    assert _cleanup(db, app, int(clock() * 1000) + MIN) == []
    assert _rows(db)[0][5] is None

    r = await client.post(
        f"{API}/subs/rebind", json={"session": _sess(CX_A, DAEMON_2)}, headers=_h(world["tok"])
    )
    assert r.status_code == 200 and r.json() == {"rebound": 2}, r.text
    assert _rows(db)[0][:5] == (sub_id, *DAEMON_2, CX_A, 1)
    assert _rows(db, "session_chan_sub")[0][:4] == (chan["sub"]["id"], *DAEMON_2, CX_A)

    # Письмо головной роли «разработчик агента» доходит до нового процесса того же разговора.
    sent = await client.post(
        f"{API}/peer-msgs",
        json={
            "text": "проверь сборку",
            "to": {"project_id": world["p1"], "role": DEV},
            "session": _sess(CLAUDE[2], CLAUDE[:2]),
        },
        headers=_h(world["tok"]),
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["recipient"]["live"] is True
    pid, start = DAEMON_2
    inbox = await client.get(
        f"{API}/peer-msgs",
        params={"pid": pid, "proc_start": start, "session_id": CX_A},
        headers=_h(world["tok"]),
    )
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["sub"] == {**inbox.json()["sub"], "id": sub_id, "role": DEV, "role_no": 1}
    assert [m["text"] for m in inbox.json()["items"]] == ["проверь сборку"]

    # Повторная подписка — та же строка и номер, а не «разработчик агента 2».
    again = await _sub(client, world, CX_A, DAEMON_2)
    assert again["sub"]["id"] == sub_id and again["created"] is False
    assert [(r[0], r[4], r[5]) for r in _rows(db)][0] == (sub_id, 1, None)
    with db.reader() as conn:
        audit = conn.execute("SELECT detail_json FROM main.audit WHERE action = 'sub_rebind'").fetchall()
    assert len(audit) == 1


async def test_resubscribe_after_restart_moves_rows_itself(client, db, world, procs):
    """Без `POST /subs/rebind` (ядро старше клиента не важно — проверяется путь подписки): повторная
    подписка и подписка на беседу с новым процессом переносят свои строки сами."""
    sub_id = (await _sub(client, world, CX_A))["sub"]["id"]
    chan_id = (await _chan_sub(client, world, CX_A))["sub"]["id"]
    _restart_daemon(procs)
    again = await _sub(client, world, CX_A, DAEMON_2)
    assert again["sub"]["id"] == sub_id and again["sub"]["role_no"] == 1
    chan = await _chan_sub(client, world, CX_A, DAEMON_2)
    assert chan["sub"]["id"] == chan_id
    assert [r[1:3] for r in _rows(db)] == [DAEMON_2]
    assert [r[5] for r in _rows(db, "session_chan_sub")] == [None]


async def test_silent_conversation_past_grace_is_not_alive(client, db, clock, app, world, procs):
    """Демон жив, разговор молчит дольше `idle_grace_ms` Codex (10 мин) — подписка не вечная; демон мёртв —
    до срока жива, после — снята."""
    a = (await _sub(client, world, CX_A))["sub"]["id"]
    t0 = int(clock() * 1000)
    with db.reader() as conn:
        rec = subs.get_sub(conn, a)
    assert not subs._sub_dead(rec, t0 + 9 * MIN, None)
    assert subs._sub_dead(rec, t0 + 11 * MIN, None)
    _restart_daemon(procs)
    assert not subs._sub_dead(rec, t0 + 9 * MIN, None)  # процесс не судят
    assert _cleanup(db, app, t0 + 9 * MIN) == []
    assert _cleanup(db, app, t0 + 11 * MIN) == [(str(a), "session_gone")]


async def test_claude_still_judged_by_process(client, db, clock, app, world, procs):
    """Claude Code — без изменений: процесс умер — подписка снята первой же чисткой."""
    body = {"session": _full(CLAUDE[2], world["dir"], CLAUDE[:2]), "role": LEAD}
    r = await client.post(f"{API}/subs", json=body, headers=_h(world["tok"]))
    assert r.status_code == 200, r.text
    del procs[CLAUDE[0]]
    assert _cleanup(db, app, int(clock() * 1000) + MIN) == [(str(r.json()["sub"]["id"]), "session_gone")]
    assert not subs.is_alive(*CLAUDE[:2], session_id=CLAUDE[2])


async def test_rebind_route_limits(client, db, world, procs):
    sub_id = (await _sub(client, world, CX_A))["sub"]["id"]
    _restart_daemon(procs)
    # Не внешняя сессия — 422; мёртвый процесс тела — 409; другой токен чужие строки не двигает.
    r = await client.post(
        f"{API}/subs/rebind", json={"session": _sess(CLAUDE[2], CLAUDE[:2])}, headers=_h(world["tok"])
    )
    assert r.status_code == 422, r.text
    r = await client.post(f"{API}/subs/rebind", json={"session": _sess(CX_A)}, headers=_h(world["tok"]))
    assert r.status_code == 409, r.text
    r = await client.post(
        f"{API}/subs/rebind", json={"session": _sess(CX_A, DAEMON_2)}, headers=_h(world["other"])
    )
    assert r.status_code == 200 and r.json() == {"rebound": 0}, r.text
    assert _rows(db)[0][:3] == (sub_id, *DAEMON)


async def test_rebind_skips_place_already_held_by_new_process(client, db, world, procs):
    """На месте (проект) у нового процесса уже своя живая подписка (её завела старая версия) — старая строка
    не трогается (уникальный индекс), её снимет срок без ожидания."""
    old = (await _sub(client, world, CX_A))["sub"]["id"]
    _restart_daemon(procs)

    def plant(conn):
        conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, mode, msg_filter, direct, session_engine) "
            "SELECT project_id, api_token_id, ?, ?, session_id, session_cwd, created_ts, mode, msg_filter, "
            "direct, session_engine FROM main.session_sub WHERE id = ?",
            (*DAEMON_2, old),
        )

    db.write_sync(plant)
    r = await client.post(
        f"{API}/subs/rebind", json={"session": _sess(CX_A, DAEMON_2)}, headers=_h(world["tok"])
    )
    assert r.status_code == 200 and r.json() == {"rebound": 0}, r.text
    assert _rows(db)[0][:3] == (old, *DAEMON)
