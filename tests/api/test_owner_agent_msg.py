"""Ф56-1б (docs/architecture.md; А4): сообщения агентов владельцу — карточка
`agent_msg` в очереди канала владельца, и вопрос владельцу `POST /peer-msgs/ask` (`hub_ask_owner`).

- ответ агента владельцу (`peer_msg.to_user`) — карточка; `reply` — сообщение «ОТ ВЛАДЕЛЬЦА» автору с `reply_to`,
  исходное прочитано; `ok` — прочитано; второй ответ — 409;
- вопрос владельцу: без подписки и без роли — 409, UI — 422; с ролью — карточка с `about`; ответ позже суток всё
  равно уходит;
- «в канале» — по таблице маршрута проекта (Ф66-1, группа `agent_msg`); ручная сессия хаба — всегда владельцу.

Мир — test_hub_sessions.py."""

from hub.core import peer_msgs

from . import test_hub_sessions as base
from .card_helpers import _route
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _agent, _answer, _patch_session, _queue, _refs
from .test_hub_sessions import ROLE, A, B, _h, _hs, _sess, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
DAY_MS = 24 * 60 * 60 * 1000


async def _ask(client, w, session, text="Дашь доступ к серверу?", *, expect=201, token=None, **extra) -> dict:
    body = {"text": text, "session": session, **extra}
    r = await client.post(f"{API}/peer-msgs/ask", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _reply_to_owner(client, ui, w, session, role=ROLE) -> int:
    """Владелец пишет линии роли, агент отвечает ему — строка `to_user`."""
    r = await ui.post(f"{API}/peer-msgs", {"text": "как дела?", "to": {"project_id": w["p3"], "role": role}})
    assert r.status_code == 201, r.text
    owner_id = r.json()["msg"]["id"]
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": "готово, ветка f1", "reply_to": owner_id, "session": session},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


def _msg(db, msg_id) -> peer_msgs.Rec:
    with db.reader() as conn:
        return peer_msgs.get(conn, msg_id)


def _last(db) -> peer_msgs.Rec:
    with db.reader() as conn:
        return peer_msgs._select(conn, "1 = 1 ORDER BY m.id DESC LIMIT 1")[0]  # noqa: SLF001


async def test_agent_reply_to_owner_is_card_and_reply_goes_back(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    msg_id = await _reply_to_owner(client, ui, w, _hs(sid))
    ref = f"agent_msg:{msg_id}"
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["kind"] == "agent_msg" and card["session_id"] == sid
    assert card["role"] == ROLE and card["role_no"] == 1 and card["path"]["author"] == ROLE
    assert card["brief"] == "готово, ветка f1" and card["until_ts"] is None
    assert card["full"]["text"] == "готово, ветка f1" and card["full"]["ask"] is False
    assert [(a["action"], a["needs_text"]) for a in card["answers"]] == [("reply", True), ("ok", False)]
    assert card["in_channel"] is False

    done = await _answer(client, ref, {"action": "reply", "text": "спасибо, дальше f2"}, ui=ui)
    assert done == {"ref": ref, "status": "replied"}
    sent = _last(db)
    assert sent.from_user == 1 and sent.reply_to == msg_id and sent.to_role == ROLE
    assert _msg(db, msg_id).status == "read"
    assert ref not in _refs(await _queue(client, ui=ui))
    await _answer(client, ref, {"action": "reply", "text": "ещё"}, ui=ui, expect=409)
    # Ответ владельца — обычная реплика «ОТ ВЛАДЕЛЬЦА» во входящих сессии.
    r = await client.get(f"{API}/peer-msgs", params=_hs(sid), headers=_h(w["tok"]))
    mine = {m["id"]: m for m in r.json()["items"]}
    assert mine[sent.id]["from_user"] is True and mine[sent.id]["reply_to"] == msg_id


async def test_ok_marks_read(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    msg_id = await _reply_to_owner(client, ui, w, _sess(A))
    card = _refs(await _queue(client, ui=ui))[f"agent_msg:{msg_id}"]
    assert card["session_id"] is None  # автор — внешняя сессия
    done = await _answer(client, f"agent_msg:{msg_id}", {"action": "ok"}, ui=ui)
    assert done["status"] == "read" and _msg(db, msg_id).status == "read"
    await _answer(client, f"agent_msg:{msg_id}", {"action": "ok"}, ui=ui, expect=409)


async def test_ask_owner_refusals_and_card_with_about(client, ui, db, clock, world):
    w = world
    await _ask(client, w, _sess(A), expect=409)  # подписки нет
    await _sub(client, w, A)  # без роли
    assert "только сессия с ролью" in (await _ask(client, w, _sess(A), expect=409))["detail"]
    r = await ui.post(f"{API}/peer-msgs/ask", {"text": "x"})
    assert r.status_code == 422
    await _sub(client, w, B, role="оркестратор")
    await _ask(client, w, _sess(B), about_role="  ", expect=422)
    await _ask(client, w, _sess(B), about_role="р" * 129, expect=422)
    made = await _ask(client, w, _sess(B), about_role="программист")
    rec = _msg(db, made["id"])
    assert rec.to_user == 1 and rec.ask == 1 and rec.ask_about == "программист" and rec.reply_to is None
    assert rec.expires_ts is None and made["seq"] == rec.seq
    card = _refs(await _queue(client, ui=ui))[f"agent_msg:{made['id']}"]
    assert card["full"]["ask"] is True and card["full"]["about_role"] == "программист"
    assert card["path"] == {
        "project": "Umbrella",
        "head": None,
        "author": "оркестратор",
        "about": "программист",
    }
    # Во входящих самой сессии вопрос владельцу не показывается (Н6).
    r = await client.get(f"{API}/peer-msgs", params=_sess(B), headers=_h(w["tok"]))
    assert r.json()["items"] == []
    # Ответ владельца позже суток всё равно уходит: это вопрос, а не ответ на его сообщение.
    clock.advance_ms(3 * DAY_MS)
    done = await _answer(client, f"agent_msg:{made['id']}", {"action": "reply", "text": "да, держи"}, ui=ui)
    assert done["status"] == "replied"
    sent = _last(db)
    assert sent.from_user == 1 and sent.reply_to == made["id"] and sent.to_role == "оркестратор"
    # Сессия получает его рамкой «ОТ ВЛАДЕЛЬЦА» и может ответить в течение суток.
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": "спасибо", "reply_to": sent.id, "session": _sess(B)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text


async def test_external_agent_msgs_follow_route_table(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    await _sub(client, w, A, role="оркестратор")
    await _sub(client, w, B, role="программист")
    head = await _ask(client, w, _sess(A), about_role="программист")
    other = await _ask(client, w, _sess(B))

    def seen(queue):
        refs = _refs(queue)
        return f"agent_msg:{head['id']}" in refs, f"agent_msg:{other['id']}" in refs

    assert seen(await _queue(client, agent)) == (False, False)  # по умолчанию — владельцу
    await _answer(client, f"agent_msg:{other['id']}", {"action": "ok"}, token=agent, expect=403)
    await _route(ui, w, {"agent_msg": "agent"})
    assert seen(await _queue(client, agent)) == (True, True)
    done = await _answer(client, f"agent_msg:{head['id']}", {"action": "reply", "text": "да"}, token=agent)
    assert done["status"] == "replied"
    assert _last(db).from_user == 1 and _last(db).to_role == "оркестратор"
    await _route(ui, w, {"agent_msg": "me"})
    assert seen(await _queue(client, agent)) == (False, False)
    await _route(ui, w, {"agent_msg": "auto"}, expect=422)  # у сообщений агентов «сразу да» нет
    with db.reader() as conn:
        row = conn.execute(
            "SELECT kind, closed_by FROM main.owner_item WHERE ref = ?", (f"agent_msg:{head['id']}",)
        ).fetchone()
    assert row == ("agent_msg", "agent")


async def test_hub_session_author_in_channel_only_in_auto(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    made = await _ask(client, w, _hs(sid))
    ref = f"agent_msg:{made['id']}"
    await _route(ui, w, {"agent_msg": "agent"})
    assert ref not in _refs(await _queue(client, agent))  # ручная сессия — всегда владельцу
    await _patch_session(ui, sid, {"mode": "auto"})
    card = _refs(await _queue(client, agent))[ref]
    assert card["in_channel"] is True and card["session_id"] == sid


async def test_agent_msgs_before_migration_threshold_are_not_cards(client, ui, db, world):
    """Ревью Ф56 (9): миграция `0084_main.sql` пишет в `meta` первый номер `peer_msg`, с которого сообщения
    агентов владельцу идут в очередь; старые (ждавшие владельца до появления очереди) карточками не становятся."""
    from hub.core import engine, owner_queue

    w = world
    sid = await _auto(client, ui, w, mode="manual")
    with db.reader() as conn:
        assert owner_queue.agent_msg_from_id(conn) == 1  # пустая база: порог — первое же сообщение
    old_id = await _reply_to_owner(client, ui, w, _hs(sid))
    assert f"agent_msg:{old_id}" in _refs(await _queue(client, ui=ui))
    # Как на проде после деплоя: порог — следующий номер после уже лежащих сообщений.
    await db.write(lambda conn: engine._meta_set(conn, owner_queue.AGENT_MSG_FROM_META, str(old_id + 1)))  # noqa: SLF001
    assert f"agent_msg:{old_id}" not in _refs(await _queue(client, ui=ui))
    new_id = await _reply_to_owner(client, ui, w, _hs(sid))
    refs = _refs(await _queue(client, ui=ui))
    assert f"agent_msg:{new_id}" in refs and f"agent_msg:{old_id}" not in refs
    # Старое сообщение по-прежнему читается и отвечается обычным путём, просто не карточка.
    assert _msg(db, old_id).status == "pending"
