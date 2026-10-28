"""Ф56-1а (docs/architecture.md, П17): объявленные действия агента
владельца, «не на связи» через 60 с, поручение по карточке, прогресс, возврат, отзыв, закрытие `system` при ответе
по целевой карточке и при её закрытии другим путём, 409 на второе поручение.

Мир и помощники — из test_hub_sessions.py, test_f41_autopilot_5b.py и test_f56_queue.py."""

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _agent, _answer, _channel_on, _permission, _queue, _refs
from .test_hub_sessions import _audits, _h, _host

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
ACTIONS = [
    {
        "id": "approve_safe",
        "label": "Разрешить, если безопасно",
        "kinds": ["permission"],
        "needs_text": False,
    },
    {"id": "solve", "label": "Решить по-своему", "kinds": [], "needs_text": True},
    {"id": "human_only", "label": "Только заявкам", "kinds": ["human"]},
]


async def _actions(client, token, actions, expect=200) -> dict:
    r = await client.put(f"{API}/owner_channel/agent-actions", json={"actions": actions}, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _errand(ui, ref, body, expect=200) -> dict:
    r = await ui.post(f"{API}/owner_channel/{ref}/errand", body)
    assert r.status_code == expect, r.text
    return r.json()


async def _agent_post(client, token, path, body, expect=200) -> dict:
    r = await client.post(f"{API}{path}", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


def _errand_rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, ref, closed_by FROM main.owner_item WHERE kind = 'errand' ORDER BY id"
        ).fetchall()


async def test_agent_actions_validation_and_online_window(client, ui, db, clock, world):
    agent, _ = _agent(db)
    for bad in (
        [{"id": "Плохой", "label": "x"}],
        [{"id": "a", "label": ""}],
        [{"id": "a", "label": "x", "kinds": ["no_such_kind"]}],
        [{"id": "a", "label": "x"}, {"id": "a", "label": "y"}],
        [{"id": f"a{i}", "label": "x"} for i in range(21)],
    ):
        await _actions(client, agent, bad, expect=422)
    state = await _actions(client, agent, ACTIONS)
    assert state["online"] is True and [a["id"] for a in state["actions"]] == [a["id"] for a in ACTIONS]
    assert state["actions"][2] == {
        "id": "human_only",
        "label": "Только заявкам",
        "kinds": ["human"],
        "needs_text": False,
    }
    seen = (await ui.get(f"{API}/owner_channel/agent-actions")).json()
    assert seen == state
    # 60 с без опроса — «не на связи»: действий нет.
    clock.advance_ms(61_000)
    assert (await ui.get(f"{API}/owner_channel/agent-actions")).json()["online"] is False
    queue = await _queue(client, ui=ui)
    assert queue["agent"]["online"] is False
    # Опрос агента — снова на связи.
    await _queue(client, agent)
    assert (await _queue(client, ui=ui))["agent"]["online"] is True


async def test_errand_lifecycle(client, ui, db, clock, world):
    w = world
    agent, agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    first = f"req:{await _permission(client, w, sid)}"

    # Ф66-1 (B.2): «Реши» — у любой карточки, и не в канале (поручение проверено в test_f65_errand_life.py).
    await _actions(client, agent, ACTIONS)
    await _channel_on(ui, w, sid)
    card = _refs(await _queue(client, ui=ui))[first]
    assert [a["id"] for a in card["agent_actions"]] == ["approve_safe", "solve"]  # по видам
    assert card["errand"] is None

    await _errand(ui, first, {"action": "human_only"}, expect=422)  # не для этого вида
    await _errand(ui, first, {"action": "solve"}, expect=422)  # действию нужен текст
    await _errand(ui, first, {}, expect=422)  # без действия — только текстом
    await _errand(ui, "req:999999", {"text": "x"}, expect=404)
    made = (await _errand(ui, first, {"action": "approve_safe", "text": "смотри внимательно"}))["errand"]
    assert made["action"] == "approve_safe" and made["label"] == "Разрешить, если безопасно"
    await _errand(ui, first, {"text": "ещё одно"}, expect=409)  # одно открытое на карточку
    # Поручение — агенту карточкой `errand`; владельцу — полем целевой карточки.
    queue = _refs(await _queue(client, agent))
    ecard = queue[f"errand:{made['id']}"]
    assert ecard["kind"] == "errand" and ecard["full"]["target"] == first and ecard["answers"] == []
    assert f"errand:{made['id']}" not in _refs(await _queue(client, ui=ui))

    progress = f"/owner_channel/errands/{made['id']}/progress"
    await _agent_post(client, agent, progress, {"text": ""}, expect=422)
    r = await ui.post(f"{API}{progress}", {"text": "смотрю"})
    assert r.status_code == 403
    await _agent_post(client, agent, progress, {"text": "проверяю  команду token=abc123"})
    shown = _refs(await _queue(client, ui=ui))[first]["errand"]
    assert shown["progress"] == "проверяю команду token=***"

    # Возврат агентом — карточка снова у владельца без поручения.
    back = await _agent_post(
        client, agent, f"/owner_channel/errands/{made['id']}/return", {"reason": "не вышло: нет доступа"}
    )
    assert back["errand"]["returned"] == "не вышло: нет доступа"
    assert _refs(await _queue(client, ui=ui))[first]["errand"] is None
    await _agent_post(client, agent, progress, {"text": "ещё"}, expect=409)

    # Отзыв владельцем.
    second = (await _errand(ui, first, {"text": "сделай сам"}))["errand"]
    r = await client.delete(f"{API}/owner_channel/errands/{second['id']}", headers=_h(agent))
    assert r.status_code == 403
    r = await ui.c.delete(f"{API}/owner_channel/errands/{second['id']}", headers=ui.headers)
    assert r.status_code == 200, r.text
    r = await ui.c.delete(f"{API}/owner_channel/errands/{second['id']}", headers=ui.headers)
    assert r.status_code == 409

    # Ответ агента по целевой карточке закрывает его поручение (Ф65-1: `agent`, было `system`).
    third = (await _errand(ui, first, {"action": "approve_safe"}))["errand"]
    await _answer(client, first, {"action": "allow"}, token=agent)
    assert [(row[0], row[2]) for row in _errand_rows(db)] == [
        (made["id"], "agent"),
        (second["id"], "ui"),
        (third["id"], "agent"),
    ]
    assert [row[1] for row in _errand_rows(db)] == [f"errand:{x['id']}" for x in (made, second, third)]
    with db.reader() as conn:
        results = [
            r[0]
            for r in conn.execute("SELECT result FROM main.audit WHERE action = 'owner_errand' ORDER BY id")
        ]
    assert results == ["created", "returned", "created", "cancelled", "created"]
    assert all("text" not in a and "reason" not in a for a in _audits(db, "owner_errand"))  # без текстов
    closed = (await ui.get(f"{API}/owner_channel/closed")).json()["items"]
    assert {c["ref"] for c in closed} >= {f"errand:{made['id']}", first}
    assert next(c for c in closed if c["ref"] == f"errand:{made['id']}")["closed_token_id"] == agent_id


async def test_errand_closes_when_card_is_closed_elsewhere(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    request_id = await _permission(client, w, sid)
    ref = f"req:{request_id}"
    await _actions(client, agent, ACTIONS)
    made = (await _errand(ui, ref, {"text": "разберись"}))["errand"]
    # Карточку закрыл другой путь (срок у хоста) — поручение закрывается при следующем чтении очереди.
    assert (await _host(client, w, f"session-requests/{request_id}/expire", {}))["status"] == "expired"
    assert f"errand:{made['id']}" not in _refs(await _queue(client, agent))
    assert _errand_rows(db) == [(made["id"], f"errand:{made['id']}", "system")]


async def test_offline_agent_gets_no_action_errands(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    ref = f"req:{await _permission(client, w, sid)}"
    await _actions(client, agent, ACTIONS)
    clock.advance_ms(61_000)
    assert _refs(await _queue(client, ui=ui))[ref]["agent_actions"] == []
    await _errand(ui, ref, {"action": "approve_safe"}, expect=409)
    # Свободный текст — можно и без связи: агент заберёт поручение при следующем опросе.
    await _errand(ui, ref, {"text": "когда вернёшься — разреши"})
