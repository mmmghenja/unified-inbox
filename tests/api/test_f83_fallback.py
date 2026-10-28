"""Ф83-1 (docs/architecture.md; план phase83-00, 3а, П1–П13): запасные движки — ядро.

- агент владельца и факты: запасной (`PUT /engine/fallback/{who}`), отказ основного (`…/failed`, права П11),
  «Вернуть на основной», блоки `GET /engine` (прежние поля — основной, П10), смена движка основного снимает
  «на запасном», `effective` в `/owner_channel/agent-settings`;
- сессии хаба: настройка у сессии и проекта, переход хоста (`/work/host/sessions/{id}/fallback`) — один раз,
  без цепочек, только авто и только `limit`/`balance`; возврат на паузе — 409; передача дел переносит запасной
  и пометку; карточка паузы ручной сессии — ответ «Перейти на …» (кукой), и при «Сразу да» — у владельца.

Мир и помощники — test_hub_sessions.py; исполняемые файлы движков — подставные (`home` из test_f78)."""

import json

from hub.core import card_route, engine_fallback, held
from hub.core import engine as engine_mod

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import DIGEST, PROC
from .test_f56_queue import _agent, _queue, _refs
from .test_f66_card_route import _route
from .test_f78_engine_choice import _engine, home  # noqa: F401 — фикстура
from .test_hub_sessions import _audits, _h, _host, _hsub, _mk, _now, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000


def _facts(db) -> str:
    return _token(db, name="facts", kind="background", scopes=("read", "facts"))[0]


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


def _ring(app) -> list[dict]:
    return app.state.notifier.ring.after(0)


def _pause(db, clock, engine="claude", kind="limit", *, until_min=60, since=None) -> dict:
    now = _now(clock)
    entry = {
        "until": now + until_min * MIN,
        "kind": kind,
        "since": since if since is not None else now - 1000,
    }
    db.write_sync(lambda conn: engine_fallback.set_host_pauses(conn, {engine: entry}))
    return entry


async def _patch(ui, path, body, *, expect=200) -> dict:
    r = await ui.c.patch(f"{API}{path}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _auto(client, ui, w, *, mode="auto", **body) -> int:
    sid = (await _mk(ui, w, role="маркетолог", mode=mode, **body))["id"]
    await _hsub(client, w, sid, cwd=w["dir3"])
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "launch_key_hash": DIGEST})
    return sid


async def _switch(client, w, sid, body=None, *, expect=200) -> dict:
    return await _host(client, w, f"sessions/{sid}/fallback", body or {"kind": "limit"}, expect=expect)


# --- агент владельца и факты -------------------------------------------------------------------------------------


async def test_engine_blocks_and_put_fallback(client, ui, db, home, world):  # noqa: F811
    body = await _engine(ui)
    assert body["agent_fallback"] is None and body["facts_fallback"] is None
    assert body["on_fallback"] is None and body["fallback"] is None
    assert body["effective"] == {"engine": body["engine"], "model": body["model"], "effort": body["effort"]}
    assert body["facts"]["on_fallback"] is None and body["facts"]["effective"]["engine"] == "claude"
    # Запасной — другой движок (П2): тот же, что основной, — 422; недоступный — 409; bearer — 403.
    r = await ui.put(f"{API}/engine/fallback/agent", {"engine": "claude"})
    assert r.status_code == 422
    (home / ".local" / "bin" / "cursor-agent").unlink()
    r = await ui.put(f"{API}/engine/fallback/agent", {"engine": "cursor"})
    assert r.status_code == 409
    r = await client.put(f"{API}/engine/fallback/agent", json={"engine": "codex"}, headers=_h(world["tok"]))
    assert r.status_code == 403
    r = await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex", "effort": "low"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_fallback"]["engine"] == "codex" and body["agent_fallback"]["effort"] == "low"
    assert body["fallback"]["engine"] == "codex" and body["fallback"]["model"]  # умолчание движка подставлено
    # Факты «как у агента» без своей записи — запасной агента.
    assert body["facts_fallback"] is None and body["facts"]["fallback"]["engine"] == "codex"
    assert _audits(db, engine_fallback.AUDIT_SET)[-1]["new"] == "codex"
    # Повтор — без записи; снять — `engine: null`.
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex", "effort": "low"})
    assert len(_audits(db, engine_fallback.AUDIT_SET)) == 1
    r = await ui.put(f"{API}/engine/fallback/agent", {"engine": None})
    assert r.json()["agent_fallback"] is None and r.json()["fallback"] is None


async def test_failed_rights_switch_once_and_notify_once(client, ui, db, app, home, world):  # noqa: F811
    agent, _ = _agent(db)
    facts = _facts(db)
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex"})
    await ui.put(f"{API}/engine/fallback/facts", {"engine": "codex"})
    url = f"{API}/engine/fallback/agent/failed"
    # П11: агент — только `agent`, факты — только `facts`; кука и прочие bearer — 403.
    for token in (facts, world["tok"], world["host"]):
        r = await client.post(url, json={"kind": "limit"}, headers=_h(token))
        assert r.status_code == 403, token
    assert (await ui.post(url, {"kind": "limit"})).status_code == 403
    r = await client.post(f"{API}/engine/fallback/facts/failed", json={"kind": "limit"}, headers=_h(agent))
    assert r.status_code == 403
    # Не повод (retry, other) — без перехода.
    r = await client.post(url, json={"kind": "retry"}, headers=_h(agent))
    assert r.status_code == 200 and r.json()["switched"] is False
    assert _ring(app) == []
    r = await client.post(url, json={"kind": "limit", "reason": "лимит до 18:00"}, headers=_h(agent))
    body = r.json()
    assert body["switched"] is True and body["effective"]["engine"] == "codex"
    assert body["on_fallback"]["kind"] == "limit" and body["on_fallback"]["main"]["engine"] == "claude"
    assert "notify" not in body
    # Второй отказ — второго перехода и второго уведомления нет.
    r = await client.post(url, json={"kind": "balance"}, headers=_h(agent))
    assert r.json()["switched"] is False
    ring = _ring(app)
    assert len(ring) == 1 and "запасном" in ring[0]["title"]
    # П10: прежние поля — основной; `effective` — запасной.
    status = await _engine(ui)
    assert status["engine"] == "claude" and status["effective"]["engine"] == "codex"
    assert status["on_fallback"]["reason"] == "лимит до 18:00"
    # Факты «как у агента» уже на запасном вместе с ним; свой выбор фактов — свой переход, своим токеном.
    r = await client.post(f"{API}/engine/fallback/facts/failed", json={"kind": "auth"}, headers=_h(facts))
    assert r.json()["switched"] is False and r.json()["effective"]["engine"] == "codex"
    await ui.put(f"{API}/engine/facts", {"engine": "claude"})
    r = await client.post(f"{API}/engine/fallback/facts/failed", json={"kind": "auth"}, headers=_h(facts))
    assert r.status_code == 200 and r.json()["switched"] is True
    assert (await _engine(ui))["facts"]["on_fallback"]["kind"] == "auth"
    # «Вернуть на основной» — кука; bearer — 403.
    r = await client.post(f"{API}/engine/fallback/agent/return", json={}, headers=_h(agent))
    assert r.status_code == 403
    r = await ui.post(f"{API}/engine/fallback/agent/return")
    assert r.status_code == 200 and r.json()["returned"] is True
    assert r.json()["on_fallback"] is None and r.json()["effective"]["engine"] == "claude"
    r = await ui.post(f"{API}/engine/fallback/agent/return")
    assert r.json()["returned"] is False


async def test_failed_without_fallback_or_chain(client, ui, db, home, world):  # noqa: F811
    agent, _ = _agent(db)
    url = f"{API}/engine/fallback/agent/failed"
    r = await client.post(url, json={"kind": "limit"}, headers=_h(agent))
    assert r.json()["switched"] is False and "не задан" in r.json()["why"]
    # Запасной, ставший основным (владелец переключил основной на тот же движок), — не переход (П1/П2).
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex"})
    await ui.put(f"{API}/engine", {"engine": "codex"})
    r = await client.post(url, json={"kind": "limit"}, headers=_h(agent))
    assert r.json()["switched"] is False


async def test_main_switch_clears_on_fallback(client, ui, db, home, world):  # noqa: F811
    agent, _ = _agent(db)
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex"})
    await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": "limit"}, headers=_h(agent))
    # PUT /engine старого интерфейса тем же движком (другая модель) — «на запасном» остаётся.
    r = await ui.put(f"{API}/engine", {"engine": "claude", "model": "opus"})
    assert r.status_code == 200 and r.json()["on_fallback"] is not None
    assert r.json()["engine"] == "claude" and r.json()["effective"]["engine"] == "codex"
    # Смена движка основного — снимает.
    r = await ui.put(f"{API}/engine", {"engine": "cursor"})
    assert r.status_code == 200 and r.json()["on_fallback"] is None
    assert r.json()["effective"]["engine"] == "cursor"


async def test_facts_main_switch_clears_facts_on(client, ui, db, home, world):  # noqa: F811
    facts = _facts(db)
    await ui.put(f"{API}/engine/facts", {"engine": "claude"})
    await ui.put(f"{API}/engine/fallback/facts", {"engine": "codex"})
    r = await client.post(f"{API}/engine/fallback/facts/failed", json={"kind": "limit"}, headers=_h(facts))
    assert r.json()["switched"] is True
    await ui.put(f"{API}/engine/facts", {"engine": "claude", "model": "opus"})
    assert (await _engine(ui))["facts"]["on_fallback"] is not None
    await ui.put(f"{API}/engine/facts", {"engine": "cursor"})
    assert (await _engine(ui))["facts"]["on_fallback"] is None


async def test_agent_settings_effective(client, ui, db, home, world):  # noqa: F811
    agent, _ = _agent(db)
    got = (await client.get(f"{API}/owner_channel/agent-settings", headers=_h(agent))).json()
    assert got["effective"]["engine"] == "claude" and got["on_fallback"] is None and got["fallback"] is None
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex"})
    await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": "model"}, headers=_h(agent))
    got = (await client.get(f"{API}/owner_channel/agent-settings", headers=_h(agent))).json()
    assert got["engine"] == "claude" and got["effective"]["engine"] == "codex"
    assert got["on_fallback"]["kind"] == "model" and got["fallback"]["engine"] == "codex"


# --- сессии хаба: настройка ---------------------------------------------------------------------------------


async def test_session_and_project_setting(client, ui, db, home, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w, model="opus"))["id"]
    row = await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "codex", "model": "gpt-6.1-sol"}})
    s = row["session"]
    assert s["fallback"] == {"engine": "codex", "model": "gpt-6.1-sol", "effort": None}
    assert s["fallback_target"]["source"] == "session" and s["on_fallback"] is None
    # Свой движок — 422 (П2); bearer — 403.
    await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "claude"}}, expect=422)
    r = await client.patch(
        f"{API}/sessions/{sid}", json={"fallback": {"engine": "codex"}}, headers=_h(w["tok"])
    )
    assert r.status_code == 403
    # П13: смена модели не стирает запасной.
    await _patch(ui, f"/sessions/{sid}", {"model": "sonnet"})
    assert json.loads(_rec(db, sid).fallback_json)["engine"] == "codex"
    # «Нет запасного» у сессии сильнее умолчания проекта; `null` — снова как у проекта.
    await _patch(ui, f"/projects/{w['p3']}", {"session_fallback": {"engine": "cursor"}})
    projects = (await ui.get(f"{API}/projects")).json()
    p3 = next(p for p in projects if p["id"] == w["p3"])
    assert p3["session_fallback"]["engine"] == "cursor"
    s = (await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": None}}))["session"]
    assert s["fallback"] == {"engine": None} and s["fallback_target"] is None
    s = (await _patch(ui, f"/sessions/{sid}", {"fallback": None}))["session"]
    assert s["fallback"] is None and s["fallback_target"]["engine"] == "cursor"
    assert s["fallback_target"]["source"] == "project"
    assert _audits(db, engine_fallback.AUDIT_PROJECT_SET)[-1]["new"]["engine"] == "cursor"
    r = await client.patch(f"{API}/projects/{w['p3']}", json={"session_fallback": None}, headers=_h(w["tok"]))
    assert r.status_code == 403


# --- сессии хаба: переход хоста и возврат -------------------------------------------------------------------


async def test_host_switch_once_and_no_chain(client, ui, db, app, clock, home, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w, model="opus")
    await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "codex", "effort": "high"}})
    entry = _pause(db, clock)
    got = await _switch(client, w, sid, {"kind": "limit", "until": entry["until"], "since": entry["since"]})
    assert got["switched"] is True, got
    new = got["session"]
    assert got["closed"]["id"] == sid and got["closed"]["closed_ts"] is not None
    assert (new["engine"], new["effort"]) == ("codex", "high") and new["mode"] == "auto"
    assert new["on_fallback"]["main"] == {"engine": "claude", "model": "opus", "effort": None}
    assert new["on_fallback"]["by"] == "auto" and new["on_fallback"]["since"] == entry["since"]
    assert new["on_fallback_text"].startswith("на запасном: ")
    assert new["fallback"]["engine"] == "codex" and new["fallback_target"] is None  # П1: без цепочек
    assert _rec(db, new["id"]).role == _rec(db, sid).role
    # Повтор по старой — 410; второй сессии нет.
    await _switch(client, w, sid, expect=410)
    with db.reader() as conn:
        assert len(held.list_sessions(conn, project_id=w["p3"])) == 1
    # Запасной в лимите — второго перехода нет (пометка стоит).
    _pause(db, clock, "codex")
    got = await _switch(client, w, new["id"])
    assert got == {"switched": False, "why": got["why"], "code": "on_fallback"}
    ring = _ring(app)
    assert len(ring) == 1 and "запасном" in ring[0]["title"]
    assert _audits(db, engine_fallback.AUDIT_SESSION_SWITCH)[-1]["by"] == "auto"


async def test_host_switch_refusals(client, ui, db, clock, home, world):  # noqa: F811
    w = world
    manual = await _auto(client, ui, w, mode="manual")
    await _patch(ui, f"/sessions/{manual}", {"fallback": {"engine": "codex"}})
    assert (await _switch(client, w, manual))["code"] == "manual"
    auto = (await _mk(ui, w, role="аналитик", mode="auto"))["id"]
    # Нет запасного — отказ; retry/overage-подобные и прочие поводы — не повод (П4).
    assert (await _switch(client, w, auto))["code"] == "no_fallback"
    await _patch(ui, f"/sessions/{auto}", {"fallback": {"engine": "codex"}})
    for kind in ("retry", "auth", "model", "other"):
        assert (await _switch(client, w, auto, {"kind": kind}))["code"] == "not_reason"
    # Недоступный запасной — отказ, сессия жива.
    (home / ".local" / "bin" / "codex").unlink()
    assert (await _switch(client, w, auto, {"kind": "balance"}))["code"] == "unavailable"
    assert _rec(db, auto).closed_ts is None
    # Не хост — 403.
    r = await client.post(
        f"{API}/work/host/sessions/{auto}/fallback", json={"kind": "limit"}, headers=_h(w["tok"])
    )
    assert r.status_code == 403


async def test_return_on_pause_409_then_back(client, ui, db, clock, home, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w, model="opus")
    await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "codex"}})
    _pause(db, clock)
    new = (await _switch(client, w, sid))["session"]["id"]
    r = await ui.post(f"{API}/sessions/{new}/fallback/return")
    assert r.status_code == 409 and "на паузе до" in r.json()["detail"]
    # Не на запасном — 409.
    other = (await _mk(ui, w, role="аналитик"))["id"]
    assert (await ui.post(f"{API}/sessions/{other}/fallback/return")).status_code == 409
    # Пауза кончилась (хост прислал пустые паузы) — возврат: движок и модель основного, пометки нет.
    await _host(client, w, "wait?timeout_s=0&pauses=%7B%7D")
    r = await ui.post(f"{API}/sessions/{new}/fallback/return")
    assert r.status_code == 200, r.text
    back = r.json()["session"]
    assert (back["engine"], back["model"]) == ("claude", "opus") and back["on_fallback"] is None
    assert back["fallback"]["engine"] == "codex" and back["fallback_target"]["engine"] == "codex"
    r = await client.post(f"{API}/sessions/{back['id']}/fallback/return", json={}, headers=_h(w["tok"]))
    assert r.status_code == 403


async def test_host_pauses_param_and_pause_cleared(client, ui, db, clock, home, world):  # noqa: F811
    w = world
    now = _now(clock)
    pauses = json.dumps({"claude": {"until": now + MIN, "kind": "balance", "since": now - 5}, "x": {}})
    await _host(client, w, "wait?timeout_s=0&pauses=" + pauses.replace(" ", ""))
    with db.reader() as conn:
        assert engine_fallback.host_pauses(conn, now)["claude"]["kind"] == "balance"
        assert "x" not in engine_fallback.host_pauses(conn, now)
    # «Снять паузу лимита» гасит записи не новее отметки.
    assert (await ui.post(f"{API}/work/pause/clear")).status_code == 200
    with db.reader() as conn:
        assert engine_fallback.host_pauses(conn, now) == {}


async def test_manual_handoff_carries_and_main_engine_drops_marker(client, ui, db, clock, home, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)
    await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "codex"}})
    _pause(db, clock)
    on = (await _switch(client, w, sid))["session"]["id"]
    # Передача дел на тот же запасной движок — пометка и запасной переезжают.
    r = await ui.post(f"{API}/sessions/{on}/handoff", {"model": "gpt-6.1-sol"})
    assert r.status_code == 200, r.text
    moved = r.json()["session"]
    assert moved["on_fallback"] is not None and moved["fallback"]["engine"] == "codex"
    # Ручная передача на движок основного снимает пометку.
    r = await ui.post(f"{API}/sessions/{moved['id']}/handoff", {"engine": "claude"})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["on_fallback"] is None


async def test_f92_b1_engine_change_keeps_chan_subs(client, ui, db, clock, home, world):  # noqa: F811
    """Ф92-1 (B1): переход на запасной, возврат на основной и передача дел на другой движок закрывают сессию
    причиной `handoff` — подписки на беседы переходят новой сессии линии каждый раз; «Закрыть сессию» —
    насовсем."""
    w = world
    sid = await _auto(client, ui, w)
    r = await client.post(
        f"{API}/chan-subs",
        json={"channel_id": w["ch2"], "session": base._hs(sid, cwd=w["dir3"]), "keywords": ["оплата"]},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "codex"}})
    _pause(db, clock)

    async def _restored(new_id: int) -> list[int]:
        got = await _hsub(client, w, new_id, cwd=w["dir3"])
        return [row["channel_id"] for row in got.get("restored_channels", [])]

    on = (await _switch(client, w, sid))["session"]["id"]
    assert await _restored(on) == [w["ch2"]]
    await _host(client, w, "wait?timeout_s=0&pauses=%7B%7D")
    r = await ui.post(f"{API}/sessions/{on}/fallback/return")
    assert r.status_code == 200, r.text
    back = r.json()["session"]["id"]
    assert await _restored(back) == [w["ch2"]]
    r = await ui.post(f"{API}/sessions/{back}/handoff", {"engine": "cursor"})
    assert r.status_code == 200, r.text
    moved = r.json()["session"]["id"]
    assert await _restored(moved) == [w["ch2"]]
    with db.reader() as conn:
        reasons = [
            row[0]
            for row in conn.execute(
                "SELECT end_reason FROM main.session_chan_sub WHERE ended_ts IS NOT NULL ORDER BY id"
            ).fetchall()
        ]
    assert reasons == ["handoff"] * 3
    await ui.post(f"{API}/sessions/{moved}/close")
    with db.reader() as conn:
        last = conn.execute(
            "SELECT end_reason FROM main.session_chan_sub ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert last[0] == "ended_by_owner"


# --- карточка паузы ручной сессии (П6) ----------------------------------------------------------------------


async def test_paused_card_offers_fallback_and_switches(client, ui, db, app, clock, home, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w, mode="manual", model="opus")
    await _route(ui, w, {"states": "auto"})  # «Сразу да» — пауза всё равно у владельца
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "сделай отчёт"})
    assert r.status_code == 200, r.text
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'paused' WHERE id = ?", (sid,))
    )
    ref = f"paused:{sid}"
    # Запасного нет — только «Отправить всё равно».
    card = _refs(await _queue(client, ui=ui))[ref]
    assert [a["action"] for a in card["answers"]] == ["wake"]
    await _patch(ui, f"/sessions/{sid}", {"fallback": {"engine": "codex"}})
    # Запасной есть, но пауза — «работа сверх подписки»/retry — не повод.
    _pause(db, clock, kind="retry")
    card = _refs(await _queue(client, ui=ui))[ref]
    assert [a["action"] for a in card["answers"]] == ["wake"]
    _pause(db, clock, kind="limit")
    card = _refs(await _queue(client, ui=ui))[ref]
    assert [a["action"] for a in card["answers"]] == ["wake", "fallback"]
    assert card["answers"][1]["label"].startswith("Перейти на Codex")
    assert card["full"]["fallback"]["engine"] == "codex"
    with db.reader() as conn:
        assert card_route.for_card(conn, w["p3"], "paused", sid) == card_route.ME
    r = await ui.post(f"{API}/owner_channel/{ref}/answer", {"action": "fallback"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "switched"
    assert _rec(db, sid).closed_ts is not None
    with db.reader() as conn:
        (new,) = held.list_sessions(conn, project_id=w["p3"])
    assert new.engine == "codex" and new.mode == "manual"
    assert json.loads(new.on_fallback_json)["by"] == "owner_channel"
    # Повторный ответ по закрытой — 409/404, не вторая сессия.
    r = await ui.post(f"{API}/owner_channel/{ref}/answer", {"action": "fallback"})
    assert r.status_code in (404, 409)


async def test_engine_choice_unaffected(ui, db, home):  # noqa: F811
    # П3: `engine_choice` и `facts_engine` не трогаются записью запасного и «на запасном».
    def _keys():
        with db.reader() as conn:
            return {
                k: conn.execute("SELECT value FROM main.meta WHERE key = ?", (k,)).fetchone()
                for k in (engine_mod.META_CHOICE, engine_mod.META_FACTS)
            }

    before = _keys()
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex"})
    await ui.put(f"{API}/engine/fallback/facts", {"engine": "cursor"})
    assert _keys() == before
