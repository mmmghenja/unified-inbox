"""Ф50-4 (docs/architecture.md): состояние роли на уровне
ядра — начало и источник идущего хода, отметка «сигнал подан, ждёт конца инструмента» (`mid_queued_ts`),
состояние одной функцией (`agent_state`) во всех видах, поле `agent_state` в `GET /sessions`, доступ к
`GET /agents/states` и `GET /agents/state` только сессии с живой подпиской в проекте роли.
Ф50-6: строка ленты «делегат не ответил за 10 минут — карточка у владельца».

Хост поддельный; помощники — из test_f41_incoming.py, card_helpers.py и test_hub_sessions.py."""

import os

import pytest

from hub.core import agent_state, held

from . import test_hub_sessions as base
from .card_helpers import BOSS, _card, _worker
from .card_helpers import PROC as PROC_WORKER
from .test_f41_incoming import API, _host, _idle
from .test_hub_sessions import CODER, EXTRA, MIN, ROLE, A, B, _h, _hs, _hsub, _mk, _now, _sess, _sub, _token

procs = base.procs
ui = base.ui
world = base.world


def _rec(db, hub_id):
    with db.reader() as conn:
        return held.get(conn, hub_id)


async def _ui_state(ui, hub_id) -> dict:
    r = await ui.get(f"{API}/sessions/{hub_id}")
    assert r.status_code == 200, r.text
    return r.json()["session"]["agent_state"]


async def _turn(client, w, hub_id, state, **extra):
    return (await _host(client, w, f"sessions/{hub_id}/turn", {"state": state, **extra}))["session"]


async def _mid(client, w, hub_id, queued):
    return (await _host(client, w, f"sessions/{hub_id}/mid", {"queued": queued}))["session"]


async def _events(client, w, hub_id, *events):
    await _host(client, w, f"sessions/{hub_id}/events", {"events": list(events)})


async def _get(client, w, path, who, *, token=None, expect=200, **params):
    session = _sess(who) if isinstance(who, tuple) else who
    r = await client.get(
        f"{API}/agents/{path}", params={**params, **(session or {})}, headers=_h(token or w["tok"])
    )
    assert r.status_code == expect, r.text
    return r.json()


# --- запись сессии: начало хода, источник, отметка очереди движка -------------------------------------------


async def test_turn_start_and_origin_are_kept_for_the_running_turn(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _idle(client, w, hub_id)
    assert (_rec(db, hub_id).turn_ts, _rec(db, hub_id).turn_origin) == (None, None)
    began = _now(clock)
    row = await _turn(client, w, hub_id, "running", source="signal")
    assert (row["turn_ts"], row["turn_origin"]) == (began, "signal")
    # Повторное «в работе» того же хода начало не двигает.
    clock.advance_ms(3 * MIN)
    row = await _turn(client, w, hub_id, "running")
    assert (row["turn_ts"], row["turn_origin"]) == (began, "signal")
    # Публичная форма, список и элемент /sync несут те же поля; состояния словами в /sync нет.
    public = (await ui.get(f"{API}/sessions/{hub_id}")).json()["session"]
    assert (public["turn_ts"], public["turn_origin"], public["mid_queued_ts"]) == (began, "signal", None)
    listed = [s for s in (await ui.get(f"{API}/sessions")).json()["items"] if s["id"] == hub_id][0]
    assert listed["turn_ts"] == began and listed["agent_state"]["turn"] == {
        "since_ts": began,
        "origin": "signal",
    }
    synced = [s for s in (await ui.get(f"{API}/sync?since=0")).json()["hub_sessions"] if s["id"] == hub_id][0]
    assert (synced["turn_ts"], synced["turn_origin"]) == (began, "signal") and "agent_state" not in synced
    # Конец хода снимает оба.
    row = await _turn(client, w, hub_id, "idle")
    assert (row["turn_ts"], row["turn_origin"]) == (None, None)
    # Новый ход — новое начало и свой источник; сон тоже снимает.
    clock.advance_ms(MIN)
    row = await _turn(client, w, hub_id, "running", source="say")
    assert (row["turn_ts"], row["turn_origin"]) == (_now(clock), "say")
    row = await _turn(client, w, hub_id, "asleep")
    assert (row["turn_ts"], row["turn_origin"]) == (None, None)
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running", "source": "кто-то"}, expect=422)


async def test_mid_queued_mark_is_set_once_kept_at_idle_and_cleared(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _idle(client, w, hub_id)
    await _turn(client, w, hub_id, "running", source="say")
    seq = _rec(db, hub_id).seq
    queued = _now(clock)
    assert (await _mid(client, w, hub_id, True))["mid_queued_ts"] == queued
    assert _rec(db, hub_id).seq > seq  # интерфейс узнаёт по /sync
    clock.advance_ms(2 * MIN)
    assert (await _mid(client, w, hub_id, True))["mid_queued_ts"] == queued  # время первой постановки
    # Ход кончился, а строка ещё в очереди движка (окно П4) — отметка стоит, пока хост её не снимет.
    assert (await _turn(client, w, hub_id, "idle"))["mid_queued_ts"] == queued
    assert (await _mid(client, w, hub_id, False))["mid_queued_ts"] is None
    # Сон снимает отметку сам; спящей она не ставится.
    await _turn(client, w, hub_id, "running")
    await _mid(client, w, hub_id, True)
    assert (await _turn(client, w, hub_id, "asleep"))["mid_queued_ts"] is None
    assert (await _mid(client, w, hub_id, True))["mid_queued_ts"] is None
    # Только хост.
    r = await client.post(
        f"{API}/work/host/sessions/{hub_id}/mid", json={"queued": True}, headers=_h(w["tok"])
    )
    assert r.status_code == 403
    await _host(client, w, f"sessions/{hub_id}/mid", {"queued": "да"}, expect=422)


# --- состояние во всех видах --------------------------------------------------------------------------------


async def test_state_of_running_session_step_cards_signals_and_error(client, ui, db, clock, app, world):
    w = world
    sid = await _worker(client, ui, w)  # в авто, «в работе» по подтверждению хоста
    began = _rec(db, sid).turn_ts
    assert began == _now(clock)
    await _turn(client, w, sid, "running", source="signal")
    state = await _ui_state(ui, sid)
    assert state["held"] is True and state["hub_session_id"] == sid and state["state"] == "running"
    assert state["turn"] == {"since_ts": began, "origin": "signal"} and state["step"] is None
    assert state["cards"] == {"total": 0, "user": 0}
    assert state["signals"] == {"waiting": 0, "ready": 0, "urgent": 0}
    assert state["bg"] == {"running": 0, "total": 0} and state["last_error"] is None
    assert state["lines"] == ["в работе 0 мин (по сигналу)"]

    # Шаг — последний инструмент ЭТОГО хода: событие прошлого хода шагом не считается.
    await _events(client, w, sid, {"kind": "tool", "tool": "Read", "summary": "старое", "ts": began - 1})
    assert (await _ui_state(ui, sid))["step"] is None
    clock.advance_ms(14 * MIN)
    step_ts = _now(clock)
    await _events(
        client,
        w,
        sid,
        {"kind": "tool", "tool": "Edit", "summary": "a.py"},
        {"kind": "tool", "tool": "Bash", "summary": "uv run pytest -q"},
        {"kind": "text", "summary": "считаю"},
    )
    state = await _ui_state(ui, sid)
    assert state["step"] == {"tool": "Bash", "ts": step_ts}
    assert state["lines"] == ["в работе 14 мин (по сигналу), шаг: Bash только что"]
    clock.advance_ms(2 * MIN)
    assert (await _ui_state(ui, sid))["lines"] == ["в работе 16 мин (по сигналу), шаг: Bash 2 мин назад"]
    clock.advance_ms(-2 * MIN)

    # Карточка ждёт владельца (Ф66-1: делегата нет — строки «решает <роль>» не бывает).
    await _card(client, w, sid)
    state = await _ui_state(ui, sid)
    assert state["cards"] == {"total": 1, "user": 1}
    assert "ждёт владельца: карточка" in state["lines"] and not any("решает" in x for x in state["lines"])

    # Ждущие сигналы и срочные, отметка очереди движка, фоновые задачи.
    await _hsub(client, w, sid, mode="direct")
    await _sub(client, w, A, cwd=w["dir4"], role=CODER)
    r = await client.post(
        f"{API}/peer-msgs",
        json={
            "text": "блокер",
            "to": {"project_id": w["p3"], "role": ROLE},
            "session": _sess(A),
            "urgent": True,
        },
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    queued = _now(clock)
    await _mid(client, w, sid, True)
    bg = {"items": [{"id": "b1", "kind": "command", "title": "счёт", "state": "running", "started_ts": 5}]}
    await _host(client, w, f"sessions/{sid}/state", {"state": "running", **PROC_WORKER, "bg": bg})
    state = await _ui_state(ui, sid)
    assert state["signals"] == {"waiting": 1, "ready": 1, "urgent": 1}
    assert state["mid_queued_ts"] == queued and state["bg"] == {"running": 1, "total": 1}
    tail = state["lines"][2:]
    assert tail == [
        "сигналов ждёт: 1 (срочных 1)",
        f"сигнал подан, ждёт конца инструмента с {agent_state.clock(queued)}",
        "фоновых задач: 1",
    ]

    # Последняя ошибка — последнее предупреждение или ошибка ленты; сводка укорочена.
    await _events(
        client,
        w,
        sid,
        {"kind": "warn", "summary": "первое"},
        {"kind": "error", "summary": "x" * 900, "is_error": True},
        {"kind": "text", "summary": "дальше"},
    )
    error = (await _ui_state(ui, sid))["last_error"]
    assert error["kind"] == "error" and error["summary"] == "x" * agent_state.SUMMARY_MAX
    assert error["ts"] == _now(clock)


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("heavy", "спит: тяжёлая, кэш остыл — обычные сигналы не поднимают"),
        ("slot", "спит: нет свободного места среди живых сессий"),
        ("elsewhere", "спит: разговор открыт в другом окне"),
        ("paused", "спит: пауза лимита подписки"),
        (None, "спит"),
    ],
)
async def test_state_of_idle_and_asleep_session(client, ui, db, clock, world, reason, text):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    assert (await _ui_state(ui, hub_id))["lines"] == ["спит"]  # ещё не запускалась
    await _idle(client, w, hub_id)
    state = await _ui_state(ui, hub_id)
    assert state["state"] == "idle" and state["turn"] is None and state["sleep"] is None
    assert state["lines"] == ["ждёт входа"]
    await _turn(client, w, hub_id, "running", source="say")
    await _turn(client, w, hub_id, "asleep", wait_reason=reason)
    state = await _ui_state(ui, hub_id)
    assert state["state"] == "asleep" and state["turn"] is None and state["step"] is None
    assert state["sleep"] == (
        None if reason is None else {"reason": reason, "text": text.removeprefix("спит: ")}
    )
    assert state["lines"][0] == text
    assert "обычные сигналы не поднимают" in text or reason != "heavy"


async def test_last_turn_line_and_closed_session_has_no_state(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _idle(client, w, hub_id)
    ended = _now(clock) - 7 * MIN
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.hub_session SET last_turn_ts = ? WHERE id = ?", (ended, hub_id)
        )
    )
    assert (await _ui_state(ui, hub_id))["lines"] == ["ждёт входа", "последний ход 7 мин назад"]
    r = await ui.post(f"{API}/sessions/{hub_id}/close")
    assert r.status_code == 200, r.text
    assert "agent_state" not in (await ui.get(f"{API}/sessions/{hub_id}")).json()["session"]


async def test_state_of_external_session_listens_or_not(client, ui, db, clock, app, world):
    w = world
    mine = (await _sub(client, w, A, role=CODER))["sub"]["id"]
    other = (await _sub(client, w, B, role=BOSS))["sub"]["id"]
    body = await _get(client, w, "states", A)
    assert body["project_id"] == w["p3"]
    states = {item["sub_id"]: item["state"] for item in body["items"]}
    assert set(states) == {mine, other}
    assert states[other] == {
        "held": False,
        "listening": False,
        "silent_min": None,
        "signals": {"waiting": 0, "ready": 0, "urgent": 0},
        "lines": ["не слушает (ожидание не запускалось)"],
    }
    mark, _had = app.state.sub_waiters.enter(other, 7)
    states = {i["sub_id"]: i["state"] for i in (await _get(client, w, "states", A))["items"]}
    assert states[other]["listening"] is True and states[other]["lines"] == ["слушает"]
    # Ожидание кончилось давно — «не слушает N мин».
    app.state.sub_waiters.leave(other, mark)
    last = _now(clock)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.session_sub SET last_wait_ts = ? WHERE id = ?", (last, other))
    )
    clock.advance_ms(25 * MIN)
    states = {i["sub_id"]: i["state"] for i in (await _get(client, w, "states", A))["items"]}
    assert states[other]["silent_min"] == 25 and states[other]["lines"] == ["не слушает 25 мин"]
    # Подробно — без ленты: её у внешней сессии нет.
    body = await _get(client, w, "state", A, project_id=w["p3"], role=BOSS)
    assert body["agent"].pop("machine")["host"] is not None  # Ф77-1 (A5)
    assert body["agent"] == {
        "project_id": w["p3"],
        "role": BOSS,
        "role_no": 1,
        "engine": "claude",
        "held": False,
        "hub_session_id": None,
        "sub_id": other,
    }
    assert body["state"]["lines"] == ["не слушает 25 мин"] and body["events"] == []


# --- hub_agent_state: роль, номер, хвост ленты --------------------------------------------------------------


async def test_role_state_with_feed_tail_and_line_numbers(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _idle(client, w, hub_id)
    await _turn(client, w, hub_id, "running", source="say")
    await _events(
        client,
        w,
        hub_id,
        *[{"kind": "tool", "tool": "Bash", "summary": f"шаг {i}"} for i in range(20)],
        {
            "kind": "tool_done",
            "tool": "Edit",
            "summary": "д" * 900,
            "detail": {"diff": "-секрет\n+тело"},
            "brief": {"file": "a.py"},
        },
    )
    await _sub(client, w, A, role=CODER)
    body = await _get(client, w, "state", A, project_id=w["p3"], role=ROLE)
    # Сессия хаба без своей подписки (спит или ещё не подписалась) — роль всё равно видна.
    assert body["agent"].pop("machine")["core"] is True  # Ф77-1: сессия хаба — на машине ядра
    assert body["agent"] == {
        "project_id": w["p3"],
        "role": ROLE,
        "role_no": 1,
        "engine": "claude",
        "held": True,
        "hub_session_id": hub_id,
        "sub_id": None,
    }
    assert body["state"]["lines"][0] == "в работе 0 мин (по реплике владельца), шаг: Bash только что"
    events = body["events"]
    assert len(events) == agent_state.TAIL_EVENTS
    assert [set(e) for e in events] == [{"ts", "kind", "tool", "summary", "is_error"}] * len(events)
    assert events[-1]["kind"] == "tool_done" and events[-1]["summary"] == "д" * agent_state.SUMMARY_MAX
    assert [e["summary"] for e in events[:-1]] == [f"шаг {i}" for i in range(6, 20)]
    assert "секрет" not in str(body)  # тел и diff нет
    # Роль пишется как угодно — ключ один.
    assert (await _get(client, w, "state", A, project_id=w["p3"], role=f"  {ROLE.upper()} "))["agent"][
        "hub_session_id"
    ] == hub_id
    # Вторая линия роли — нужен номер.
    second = (await _mk(ui, w, role=ROLE))["id"]
    refused = await _get(client, w, "state", A, expect=409, project_id=w["p3"], role=ROLE)
    assert refused["detail"] == "у роли несколько линий — задай number: 1, 2"
    body = await _get(client, w, "state", A, project_id=w["p3"], role=ROLE, number=2)
    assert body["agent"]["hub_session_id"] == second and body["state"]["lines"] == ["спит"]
    assert (await _get(client, w, "state", A, expect=404, project_id=w["p3"], role=ROLE, number=3))[
        "detail"
    ] == agent_state.NO_ROLE
    assert (await _get(client, w, "state", A, expect=404, project_id=w["p3"], role="бухгалтер"))[
        "detail"
    ] == agent_state.NO_ROLE


# --- доступ (П8) --------------------------------------------------------------------------------------------


async def test_access_only_for_live_subscriber_of_the_same_project(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _idle(client, w, hub_id)
    want = {"project_id": w["p3"], "role": ROLE}

    # Нет подписки — 409; тройки нет вовсе — 403.
    assert (await _get(client, w, "state", A, expect=409, **want))["detail"]
    await _get(client, w, "states", A, expect=409)
    assert (await _get(client, w, "state", None, expect=403, **want))["detail"] == agent_state.NOT_YOURS
    await _get(client, w, "states", None, expect=403)

    # Свой проект — да: и внешней сессии, и сессии хаба (условная тройка).
    mine = (await _sub(client, w, A, role=CODER))["sub"]["id"]
    assert (await _get(client, w, "state", A, **want))["agent"]["hub_session_id"] == hub_id
    assert len((await _get(client, w, "states", A))["items"]) == 1
    boss = (await _mk(ui, w, role=BOSS))["id"]
    await _idle(client, w, boss)
    await _hsub(client, w, boss)
    assert (await _get(client, w, "state", _hs(boss), **want))["agent"]["hub_session_id"] == hub_id
    assert {i["state"]["held"] for i in (await _get(client, w, "states", _hs(boss)))["items"]} == {
        True,
        False,
    }

    # Подписка в ДРУГОМ проекте — отказ, хотя токен профиля `all` проект видит; чужие роли не перечисляются.
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    assert (await _get(client, w, "state", B, expect=403, **want))["detail"] == agent_state.NOT_YOURS
    assert (await _get(client, w, "state", B, expect=403, project_id=w["p3"], role="нет такой"))[
        "detail"
    ] == agent_state.NOT_YOURS
    assert [i["state"]["held"] for i in (await _get(client, w, "states", B))["items"]] == [
        False
    ]  # только свои

    # Агент вне проектов — отказ.
    free = os.path.join(os.path.dirname(w["dir3"]), "free")
    os.mkdir(free)
    loner = EXTRA[0]
    await _sub(client, w, loner, cwd=free, mode="direct", role="сисадмин")
    assert (await _get(client, w, "state", loner, expect=403, **want))["detail"] == agent_state.NO_PROJECT
    assert (await _get(client, w, "states", loner, expect=403))["detail"] == agent_state.NO_PROJECT
    assert (await _get(client, w, "state", loner, expect=403, project_id=0, role="сисадмин"))[
        "detail"
    ] == agent_state.NO_PROJECT

    # Дежурная и оркестратор 9c — отказ правилом подписки, с чужой живой тройкой тоже; кука интерфейса — тоже.
    plain = ("read", "draft")
    wake, _ = _token(db, name=f"mcp:wake-{w['p3']}", scopes=plain, project_ids=[w["p3"]])
    orch, _ = _token(db, name="mcp:orchestrator", scopes=plain)
    runs, _ = _token(db, name="mcp:x", scopes=(*plain, "runs"))
    for token in (wake, orch, runs):
        refused = await _get(client, w, "state", A, token=token, expect=403, **want)
        assert refused["detail"] == "токен hub-wake или оркестратора не подписывается"
        await _get(client, w, "states", A, token=token, expect=403)
    r = await ui.get(f"{API}/agents/state", params={**want, **_sess(A)})
    assert r.status_code == 403, r.text
    assert (await ui.get(f"{API}/agents/states", params=_sess(A))).status_code == 403

    # Подписка кончилась — доступа снова нет.
    r = await client.post(
        f"{API}/subs/{mine}/end", json={"session": _sess(A), "reason": "unsubscribed"}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    await _get(client, w, "state", A, expect=409, **want)
