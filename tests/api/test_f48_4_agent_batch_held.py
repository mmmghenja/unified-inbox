"""Ф48-4 (docs/architecture.md): сообщения агентов пачкой — постоянная сессия хаба.
До срока пачки сообщение агента не готовый сигнал (спящую не поднимает, свободную не будит: `ready` 0 и
`next_ms` до срока), выдачи хосту нет; срок пришёл, пришло срочное или другой немедленный сигнал — всё
накопленное одной выдачей. Плюс поле проекта в интерфейсе (`PATCH /projects/{id}`).

Хост поддельный; помощники — из test_f41_incoming.py и test_hub_sessions.py."""

from . import test_hub_sessions as base
from .conftest import insert_channel, insert_message
from .test_f41_incoming import API, _card, _idle, _peer, _session, _signals, _triage, _wait_host
from .test_hub_sessions import (
    CODER,
    MIN,
    ROLE,
    B,
    _ev,
    _h,
    _hs,
    _hsub,
    _ingest,
    _mk,
    _now,
    _sess,
    _sub,
)

procs = base.procs
ui = base.ui
world = base.world


async def _hub_with_batch(client, ui, w, *, mode="direct", minutes=5):
    """Сессия хаба с интервалом пачки; первое сообщение агента уже выдано — у подписки стоит срок."""
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    body = await _hsub(client, w, hub_id, mode=mode, agent_batch_min=minutes)
    assert body["sub"]["agent_batch_min"] == minutes
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)  # отправитель
    first = await _peer(client, w)
    data = await _wait_host(client, w, known={})
    # срока ещё нет
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}]
    await _idle(client, w, hub_id)
    assert [s["msg_id"] for s in (await _signals(client, w, hub_id))["signals"]] == [first]
    return hub_id


async def test_hub_session_not_raised_before_deadline_and_raised_at_deadline(client, ui, db, clock, world):
    w = world
    hub_id = await _hub_with_batch(client, ui, w)
    clock.advance_ms(MIN)
    second = await _peer(client, w, text="второе")
    clock.advance_ms(MIN)
    third = await _peer(client, w, text="третье")

    # до срока: готового нет, хосту назван срок; выдачи нет ни свободной, ни спящей сессии
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 0, "urgent": 0, "next_ms": 4 * MIN}]
    assert (await _session(ui, hub_id))["sub"]["pending"] == 2  # ждут — видно в записи
    assert (await _signals(client, w, hub_id))["signals"] == []
    clock.advance_ms(4 * MIN - 1)
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 0, "urgent": 0, "next_ms": 1}]

    # срок пришёл — готово, всё накопленное одной выдачей
    clock.advance_ms(1)
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 2, "urgent": 0, "next_ms": None}]
    assert [s["msg_id"] for s in (await _signals(client, w, hub_id))["signals"]] == [second, third]
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []


async def test_hub_session_urgent_takes_accumulated(client, ui, db, clock, world):
    w = world
    hub_id = await _hub_with_batch(client, ui, w)
    clock.advance_ms(MIN)
    slow = await _peer(client, w, text="не к спеху")
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"][0]["ready"] == 0
    r = await client.post(
        f"{API}/peer-msgs",
        json={
            "text": "блокер",
            "to": {"project_id": w["p3"], "role": ROLE},
            "session": _sess(B),
            "urgent": True,
        },
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201 and "batch_ts" not in r.json()["recipient"], r.text
    urgent = r.json()["msg"]["id"]
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 1, "urgent": 1, "next_ms": None}]
    assert [s["msg_id"] for s in (await _signals(client, w, hub_id))["signals"]] == [urgent, slow]


async def test_hub_session_card_takes_accumulated(client, ui, db, clock, world):
    """Любой сигнал, который идёт сразу (здесь — карточка), забирает накопленные сообщения агентов."""
    w = world
    hub_id = await _hub_with_batch(client, ui, w, mode="cards")
    clock.advance_ms(2 * MIN)  # дебаунс карточек — минута
    slow = await _peer(client, w, text="не к спеху")
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"][0]["ready"] == 0
    assert (await _signals(client, w, hub_id))["signals"] == []

    channel = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="cards", kind="dm", title="Клиент")
    message = insert_message(db, channel, ext_id="10", ts=_now(clock) - MIN)
    await _card(client, _triage(db), channel, [message], label="urgent")
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"][0]["ready"] == 1
    signals = (await _signals(client, w, hub_id))["signals"]
    assert len(signals) == 2 and "item_id" in signals[0]  # карточка…
    assert (signals[1]["kind"], signals[1]["msg_id"]) == ("peer", slow)  # …и накопленное сообщение агента
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []


async def test_ui_project_field(client, ui, db, world):
    """Поле «Сообщения агентов пачкой, мин» панели «Раскладка»: `PATCH /projects/{id}` куки интерфейса."""
    w = world
    url = f"{API}/projects/{w['p3']}"
    r = await ui.c.patch(url, json={"agent_batch_min": 61}, headers=ui.headers)
    assert r.status_code == 422
    r = await ui.c.patch(url, json={"agent_batch_min": 7}, headers=ui.headers)
    assert r.status_code == 200, r.text
    projects = (await ui.get(f"{API}/projects")).json()
    assert {p["id"]: p["agent_batch_min"] for p in projects if p["id"] in (w["p3"], w["p4"])} == {
        w["p3"]: 7,
        w["p4"]: 0,
    }
    # умолчание проекта действует на подписку сессии хаба без своего интервала
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    assert (await _hsub(client, w, hub_id, mode="direct"))["sub"]["agent_batch_min"] == 7
    r = await ui.c.patch(url, json={"agent_batch_min": 0}, headers=ui.headers)
    assert r.status_code == 200
    assert (
        next(p for p in (await ui.get(f"{API}/projects")).json() if p["id"] == w["p3"])["agent_batch_min"]
        == 0
    )


async def test_hub_session_channel_signal_takes_accumulated(client, ui, db, clock, world):
    """Сессию будит сигнал её подписки на беседу — накопленные сообщения агентов уходят той же выдачей."""
    w = world
    hub_id = await _hub_with_batch(client, ui, w)
    r = await client.post(
        f"{API}/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(hub_id, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    clock.advance_ms(MIN)
    slow = await _peer(client, w, text="не к спеху")
    assert (await _signals(client, w, hub_id))["signals"] == []
    _ingest(db, w, [_ev("other", "301", _now(clock), text="привет")], clock)
    body = await _signals(client, w, hub_id)
    assert len(body["chan_signals"]) == 1
    assert [(s["kind"], s["msg_id"]) for s in body["signals"]] == [("peer", slow)]
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []
