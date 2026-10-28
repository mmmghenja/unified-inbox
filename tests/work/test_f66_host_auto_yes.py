"""Ф66-1 (docs/architecture.md.1, П3): «сразу да» на разрешение сессии хаба. Ядро
записывает запрос решённым и доставленным и отвечает хосту `decided: "allow"` — хост сам отдаёт движку
«разрешить» в той же гонке, без карточки и без уведомления владельцу. Ядро — в процессе, `claude` — подставной."""

from .test_held_autopilot import _bash, _requests
from .test_held_host import _events, _running, _session, _turn_done

MARKED = "гард хаба: связь с сервером — решает владелец\n[hub-rule:server]"


async def test_auto_route_permission_is_answered_by_the_host_itself(env):
    env.scenario(turns=[[_bash("ssh prod uptime", reason_type="hook", reason=MARKED), {"text": "готово"}]])
    host = await env.started()
    sid, pid, _ = await _session(env, title="Главная", mode="auto")
    r = await env.ui("PATCH", f"/projects/{pid}", {"card_route": {"permission": "auto"}})
    assert r.status_code == 200, r.text
    await _running(env, host, sid)
    await env.until(host, lambda: env.responses(), what="ответ движку")
    [answer] = env.responses()
    assert answer["response"]["behavior"] == "allow"
    assert answer["response"]["updatedInput"] == {"command": "ssh prod uptime"}
    await _turn_done(env, host, sid)
    assert await _requests(env, sid) == []  # открытых карточек нет
    with env.db.reader() as conn:
        rows = conn.execute(
            "SELECT status, delivered_ts IS NOT NULL FROM main.held_request WHERE session_id = ?", (sid,)
        ).fetchall()
    assert rows == [("allowed", 1)]
    assert not [n for n in env.notes if "просит" in n[1]]  # уведомления владельцу нет
    runner = host.held.runners[sid]
    assert runner.pending == {} and runner.by_core == {}
    summaries = [(e["kind"], e["summary"]) for e in await _events(env, sid)]
    assert ("decision", "владелец разрешил: Bash") in summaries


async def test_me_route_still_waits_for_owner(env):
    env.scenario(turns=[[_bash("ssh prod uptime", reason_type="hook", reason=MARKED), {"text": "готово"}]])
    host = await env.started()
    sid, _pid, _ = await _session(env, title="Главная", mode="auto")
    await _running(env, host, sid)

    async def pending():
        return [r for r in await _requests(env, sid) if r["status"] == "pending"]

    [request] = await env.until(host, pending, what="карточка")
    await env.until(host, lambda: [n for n in env.notes if "просит" in n[1]], what="уведомление")
    assert env.responses() == []
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: env.responses(), what="решение у процесса")
    await _turn_done(env, host, sid)
