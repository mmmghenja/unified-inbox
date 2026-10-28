"""Ф14-5, тесты ревьюера (`.runs/critic-f145/`), перенесены после исправления.

Находка 1 (П1 + П19): повторная подписка на ту же беседу (после /clear — `conversation_changed`, или после
смерти прежней сессии — `replaced`) внутри `chan_subs.subscribe` сначала завершает старую подписку. `end()`
делает добор 13-1 (П19) по беседе, пока новой подписки ещё нет, — подписке на проект (режим `cards`) ложился
сигнал карточки беседы, которую тут же снова слушает сессия; `subs.deliver` его выдавал. Теперь при выдаче
такой сигнал снимается с причиной `chan_sub`.

Находка 2 (П15 + П17): после /clear фоновое `hub sub wait --channels` прежнего разговора ещё висит в ядре.
Новый разговор зовёт `hub_subscribe(channel_id=…)`; ядро считало `waiting` по ключу процесса без
`session_id` и отвечало `waiting: true` — MCP велел «новую команду НЕ запускай», а устаревшее ожидание тут
же уходило 410 `conversation_changed`. Теперь `waiting` — только ожидание того же разговора."""

import asyncio

from . import test_chan_subs as base
from .test_chan_subs import PID, _card, _event, _h, _ingest, _msg_id, _now, _session, _sub, _wait

procs = base.procs  # фикстуры
world = base.world

OTHER_PID, OTHER_START = 5000, 1


async def _project_sub_other(client, w, procs):
    procs[OTHER_PID] = OTHER_START
    r = await client.post(
        "/api/v1/subs",
        json={"session": _session(pid=OTHER_PID, start=OTHER_START, sid="proj-sess", cwd=w["workdir"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _project_wait(client, w, sub_id):
    return await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={
            "session": {"pid": OTHER_PID, "proc_start": OTHER_START, "session_id": "proj-sess"},
            "waiter": "0123456789abcdef",
            "timeout_s": 1,
        },
        headers=_h(w["tok"]),
    )


async def _setup(client, db, w, procs, clock):
    now = _now(clock)
    _ingest(db, w, [_event("c1", "1", now)], clock)
    m1 = _msg_id(db, w["cid"], "1")
    assert (await _sub(client, w, w["cid"])).status_code == 200  # сессия слушает беседу
    project_sub = await _project_sub_other(client, w, procs)
    item = await _card(client, w, w["cid"], [m1])
    return project_sub, item


def _item_signals(db, item):
    with db.reader() as conn:
        return conn.execute(
            "SELECT sub_id, status, reason FROM main.session_signal WHERE item_id = ? ORDER BY id", (item,)
        ).fetchall()


# Ф26-1 (раздел 0, п.4): подписка на беседу глушит подписку на проект только ТОЙ ЖЕ сессии. Подписка на
# проект другой сессии получает карточку беседы сразу — и только один раз: добор П19 при переподписке беседы
# идёт лишь подписке на проект той же сессии, что держала подписку на беседу.


async def test_resubscribe_after_clear_leaks_card_signal(client, db, world, procs, clock):
    project_sub, item = await _setup(client, db, world, procs, clock)
    r0 = await _project_wait(client, world, project_sub)
    assert [s["item_id"] for s in r0.json()["signals"]] == [item]  # другая сессия — сигнал как обычно
    # /clear: тот же процесс, новый разговор снова подписывается на ту же беседу
    r = await _sub(client, world, world["cid"], sid="sess-new-after-clear")
    assert r.status_code == 200 and r.json()["created"] is True
    r = await _project_wait(client, world, project_sub)
    assert r.json()["signals"] == [], f"дубль: карточка #{item} выдана подписке на проект: {r.json()}"
    assert _item_signals(db, item) == [(project_sub, "delivered", None)]


async def test_replaced_dead_session_leaks_card_signal(client, db, world, procs, clock):
    project_sub, item = await _setup(client, db, world, procs, clock)
    procs[6000] = 2
    del procs[PID]  # прежняя сессия умерла, cleanup ещё не прошёл; новая сессия подписывается — `replaced`
    r = await _sub(client, world, world["cid"], pid=6000, start=2, sid="third")
    assert r.status_code == 200 and r.json()["created"] is True
    assert _item_signals(db, item) == [(project_sub, "pending", None)]  # один сигнал, без дубля добора


async def test_pending_card_signal_before_chan_sub_still_delivered(client, db, world, procs, clock):
    """Тот же корень: `pending` сигнал карточки, лёгший ДО подписки на беседу ТОЙ ЖЕ сессии, при выдаче
    снимается `chan_sub`."""
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    m1 = _msg_id(db, world["cid"], "1")
    project_sub = await _project_sub_other(client, world, procs)
    item = await _card(client, world, world["cid"], [m1])
    r = await _sub(client, world, world["cid"], pid=OTHER_PID, start=OTHER_START, sid="proj-sess")
    assert r.status_code == 200
    r = await _project_wait(client, world, project_sub)
    assert r.json()["signals"] == [], f"карточка #{item} беседы под подпиской на беседу выдана: {r.json()}"
    assert _item_signals(db, item) == [(project_sub, "skipped", "chan_sub")]


NEW_SID = "sess-new-after-clear"


async def test_new_conversation_told_waiting_by_stale_waiter(client, db, world, procs, clock):
    assert (await _sub(client, world, world["cid"])).status_code == 200  # старый разговор
    stale = asyncio.create_task(_wait(client, world, timeout_s=5))  # его фоновое ожидание
    await asyncio.sleep(0.2)
    r = await _sub(client, world, world["cid"], sid=NEW_SID)  # /clear -> новый разговор подписывается
    assert r.status_code == 200, r.text
    told_waiting = r.json()["sub"]["waiting"]
    stale_resp = await asyncio.wait_for(stale, 10)
    assert stale_resp.status_code == 410  # устаревшее ожидание ушло
    # после этого ожидания у нового разговора нет, а ему сказали, что оно идёт
    assert told_waiting is False, (
        "ядро ответило waiting=true по ожиданию ПРЕЖНЕГО разговора, которое тут же ушло 410"
    )


# --- п.4: поле `msg_signal` у карточек `/agent/items` ----------------------------------------------------


async def _items(client, w):
    r = await client.get("/api/v1/agent/items", params={"status": "new"}, headers=_h(w["triage"]))
    assert r.status_code == 200, r.text
    return {item["channel_id"]: item for item in r.json()}


async def test_msg_signal_of_chan_sub_pending_delivered_and_gone(client, db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    _ingest(db, world, [_event("c1", "2", now + 1)], clock)
    m2 = _msg_id(db, world["cid"], "2")
    await _card(client, world, world["cid"], [m2])
    item = (await _items(client, world))[world["cid"]]
    assert item["last_message_id"] == m2
    assert item["msg_signal"] == {
        "sub_ref": f"c{sub_id}",
        "status": "pending",
        "reason": None,
        "delivered_ts": None,
        "created_ts": now,
        "last_message_id": m2,
        # Ф20-1 D: категория и родитель треда
        "cat": "dm",
        "thread_ext": None,
        "ready_ts": None,  # Ф36-1 D: срока пачки у личного нет
    }
    clock.advance_ms(6_000)
    assert len((await _wait(client, world)).json()["signals"]) == 1
    signal = (await _items(client, world))[world["cid"]]["msg_signal"]
    assert signal["status"] == "delivered" and signal["delivered_ts"] == now + 6_000
    r = await client.post(
        "/api/v1/chan-subs/end",
        json={"session": _session(full=False), "reason": "unsubscribed"},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    assert (await _items(client, world))[world["cid"]]["msg_signal"] is None  # подписка снята


async def test_msg_signal_of_messages_mode_project_sub_only(client, db, world, procs, clock):
    async def _project_sub(mode):
        r = await client.post(
            "/api/v1/subs",
            json={"session": _session(cwd=world["workdir"]), "mode": mode},
            headers=_h(world["tok"]),
        )
        assert r.status_code == 200, r.text
        return r.json()["sub"]["id"]

    sub_id = await _project_sub("messages")
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now), _event("c2", "1", now)], clock)
    m1 = _msg_id(db, world["cid"], "1")
    await _card(client, world, world["cid"], [m1])
    await _card(client, world, world["cid2"], [_msg_id(db, world["cid2"], "1")])
    procs[OTHER_PID] = OTHER_START
    r = await _sub(client, world, world["cid2"], pid=OTHER_PID, start=OTHER_START, sid="other")
    assert r.status_code == 200, r.text  # беседу c2 слушает подписка на беседу
    items = await _items(client, world)
    assert items[world["cid"]]["msg_signal"]["sub_ref"] == str(sub_id)
    assert items[world["cid"]]["msg_signal"]["last_message_id"] == m1
    # у c2 подписка на беседу главнее: её сигналов ещё нет — сигнал режима `messages` не берётся
    assert items[world["cid2"]]["msg_signal"] is None
    await _project_sub("cards")
    assert (await _items(client, world))[world["cid"]]["msg_signal"] is None  # режим `cards` — поля нет
