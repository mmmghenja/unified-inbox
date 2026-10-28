"""24.10 (просьба менеджера «Hooli»): вступление по заявке сессии подписывает сессию-заявителя на беседу
группы/канала — как `hub_subscribe(channel_id=…)` без ключей; сессии нет — живую сессию той же линии роли;
итог виден в `GET /joins` (`chan_sub`). Стенд и помощники — `test_joins`."""

# ruff: noqa: F811 — фикстуры `env`, `ic`, `procs` взяты из соседнего модуля и принимаются аргументами тестов

from hub.core import chan_subs

from .test_joins import (  # noqa: F401 — фикстуры стенда
    API,
    PID,
    SESSION,
    SID,
    START,
    _bearer,
    _channel,
    _create,
    _decide,
    _events,
    _lease,
    _now_ms,
    _peek,
    _report,
    _req,
    _signals,
    _ui,
    _up,
    env,
    ic,
    internal_app,
    procs,
)

ROLE, ROLE_KEY = "менеджер", "менеджер"


def _role_sub(db, *, project_id, token_id, pid=PID, start=START, sid=SID, ended=False) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts, mode, role, role_key, role_no, ended_ts, end_reason) "
                "VALUES (?, ?, ?, ?, ?, '/tmp', 0, 'cards', ?, ?, 1, ?, ?)",
                (
                    project_id,
                    token_id,
                    pid,
                    start,
                    sid,
                    ROLE,
                    ROLE_KEY,
                    1 if ended else None,
                    "session_gone" if ended else None,
                ),
            ).lastrowid
        )
    )


def _live_chan_subs(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT channel_id, session_pid, session_id, keywords_json FROM main.session_chan_sub "
            "WHERE ended_ts IS NULL ORDER BY id"
        ).fetchall()


async def _join_with_history(client, ic, db, clock, env, ext_id, *, session=SESSION):
    """Заявка от сессии -> предпросмотр -> «Вступить» -> вступили -> пришла история беседы."""
    ui = await _ui(client, db, clock)
    extra = {"session": session} if session is not None else {}
    item = await _create(client, env["token"], env["project"], f"@chan{ext_id[1:]}", **extra)
    [peek] = await _lease(ic, env["h"])
    await _report(ic, env["h"], peek, peek=_peek(ext_id))
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, result="joined", ext_id=ext_id)
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up(ext_id, "1", _now_ms(clock), kind="group")])
    return item, _channel(db, ext_id)


async def _joins(client, env) -> dict:
    r = await client.get(f"{API}/joins", headers=_bearer(env["token"]))
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()}


async def test_requester_subscribed_and_signalled(client, ic, db, clock, env, procs):
    _role_sub(db, project_id=env["project"], token_id=env["token_id"])
    item, ch = await _join_with_history(client, ic, db, clock, env, "-3001")
    assert _req(db, item["id"]).channel_id == ch["id"]
    assert _live_chan_subs(db) == [(ch["id"], PID, SID, None)]
    state = (await _joins(client, env))[item["id"]]["chan_sub"]
    assert state["state"] == "live" and isinstance(state["id"], int)
    # Беседа заглушена и подписке на проект не сигналит, а подписке на беседу — сигналит.
    await _events(ic, env["h"], [_up("-3001", "2", _now_ms(clock) + 1, kind="group")])
    assert [(s[0], s[2], s[3]) for s in _signals(db)] == [(state["id"], ch["id"], "pending")]
    # Повторная привязка (отчёт после истории) подписку не дублирует.
    await _events(ic, env["h"], [_up("-3001", "3", _now_ms(clock) + 2, kind="group")])
    assert len(_live_chan_subs(db)) == 1


async def test_existing_own_chan_sub_kept(client, ic, db, clock, env, procs):
    """Своя подписка беседы уже есть (сессия подписалась сама, пока шла история) — вторая не заводится."""
    _role_sub(db, project_id=env["project"], token_id=env["token_id"])
    ui = await _ui(client, db, clock)
    item = await _create(client, env["token"], env["project"], "@chan3002", session=SESSION)
    [peek] = await _lease(ic, env["h"])
    await _report(ic, env["h"], peek, peek=_peek("-3002"))
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, env["h"])
    # история пришла раньше отчёта: беседа привязана при создании
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-3002", "1", _now_ms(clock), kind="group")])
    await _report(ic, env["h"], job, result="joined", ext_id="-3002")
    ch = _channel(db, "-3002")
    assert [r[0] for r in _live_chan_subs(db)] == [ch["id"]]
    assert (await _joins(client, env))[item["id"]]["chan_sub"]["state"] == "live"


async def test_requester_gone_role_successor_subscribed(client, ic, db, clock, env, procs):
    _role_sub(db, project_id=env["project"], token_id=env["token_id"])
    item = await _create(client, env["token"], env["project"], "@chan3003", session=SESSION)
    # Сессия ушла, роль переподписалась новой сессией.
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.session_sub SET ended_ts = 1, end_reason = 'session_gone'")
    )
    procs.pop(PID)
    procs[5151] = 888_000
    _role_sub(
        db, project_id=env["project"], token_id=env["token_id"], pid=5151, start=888_000, sid="sess-new"
    )
    ui = await _ui(client, db, clock)
    [peek] = await _lease(ic, env["h"])
    await _report(ic, env["h"], peek, peek=_peek("-3003"))
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, result="joined", ext_id="-3003")
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-3003", "1", _now_ms(clock), kind="group")])
    ch = _channel(db, "-3003")
    assert _live_chan_subs(db) == [(ch["id"], 5151, "sess-new", None)]


async def test_no_session_no_subscription_reason_shown(client, ic, db, clock, env, procs):
    item, ch = await _join_with_history(client, ic, db, clock, env, "-3004", session=None)
    assert _req(db, item["id"]).channel_id == ch["id"]
    assert _live_chan_subs(db) == []
    assert (await _joins(client, env))[item["id"]]["chan_sub"] == {
        "state": "none",
        "id": None,
        "reason": "no_sub",
    }


async def test_session_gone_without_successor(client, ic, db, clock, env, procs):
    _role_sub(db, project_id=env["project"], token_id=env["token_id"])
    ui = await _ui(client, db, clock)
    item = await _create(client, env["token"], env["project"], "@chan3005", session=SESSION)
    procs.pop(PID)  # процесса нет, подписка ещё не снята
    [peek] = await _lease(ic, env["h"])
    await _report(ic, env["h"], peek, peek=_peek("-3005"))
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, result="joined", ext_id="-3005")
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-3005", "1", _now_ms(clock), kind="group")])
    assert _live_chan_subs(db) == []
    state = (await _joins(client, env))[item["id"]]["chan_sub"]
    assert state["state"] == "none" and state["reason"] == "no_session"


async def test_limit_reached_no_subscription(client, ic, db, clock, env, procs, monkeypatch):
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 0)
    _role_sub(db, project_id=env["project"], token_id=env["token_id"])
    item, _ch = await _join_with_history(client, ic, db, clock, env, "-3006")
    assert _live_chan_subs(db) == []
    assert (await _joins(client, env))[item["id"]]["chan_sub"]["reason"] == "limit"
