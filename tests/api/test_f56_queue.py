"""Ф56-1а (docs/architecture.md–5 и 7): очередь канала владельца.

- допуск: кука и агент владельца (bearer ровно `human`) — да; бот `human_bot` и прочий bearer — 403;
- признак «в канале» — по таблице маршрута проекта (Ф66-1: группа `agent`; ручная сессия хаба — всегда владельцу);
  агент видит только карточки «в канале» своих проектов;
- каждый вид появляется в `items` и исчезает после решения (из канала и прежней ручкой интерфейса);
- ответ агента — тот же эффект, что клик владельца; источник — строка `owner_item` только у агента; `/closed` —
  только куке; длинный опрос по общему `seq`.

Мир и помощники — из test_hub_sessions.py и test_f41_autopilot_5b.py; хост поддельный."""

import datetime as dt
import json

from hub.core import card_route, held, held_work, security

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto, _me, _send
from .test_f41_held_work import _ask
from .test_f54_human_requests import _create as _human_create
from .test_hub_sessions import ROLE, A, _audits, _h, _host, _hs, _now, _sub, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


def _iso(clock, minutes: int) -> str:
    return dt.datetime.fromtimestamp(clock() + minutes * 60, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent(db, project_ids=None) -> tuple[str, int]:
    return _token(db, name="human-agent", kind="background", scopes=("human",), project_ids=project_ids)


def _bot(db) -> tuple[str, int]:
    return _token(db, name="human-bot", kind="background", scopes=("human_bot",))


async def _queue(client, token=None, ui=None) -> dict:
    if ui is not None:
        r = await ui.get(f"{API}/owner_channel/queue")
    else:
        r = await client.get(f"{API}/owner_channel/queue", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


def _refs(queue: dict) -> dict[str, dict]:
    return {c["ref"]: c for c in queue["items"]}


async def _answer(client, ref, body, *, token=None, ui=None, expect=200) -> dict:
    if ui is not None:
        r = await ui.post(f"{API}/owner_channel/{ref}/answer", body)
    else:
        r = await client.post(f"{API}/owner_channel/{ref}/answer", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _patch_session(ui, sid, body, expect=200) -> dict:
    r = await ui.c.patch(f"{API}/sessions/{sid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


ALL_AGENT = {group: "agent" for group in card_route.GROUPS}


async def _channel_on(ui, w, sid=None, *, project="p3") -> None:
    """Ф66-1: карточки проекта — агенту (все группы таблицы маршрута `agent`); сессия хаба `sid` — в авто
    (карточки ручной сессии всегда владельцу)."""
    r = await ui.c.patch(f"{API}/projects/{w[project]}", json={"card_route": ALL_AGENT}, headers=ui.headers)
    assert r.status_code == 200, r.text
    if sid is not None:
        await _patch_session(ui, sid, {"mode": "auto"})


async def _permission(client, w, sid) -> int:
    return (await _ask(client, {"host": w["host"]}, sid))["id"]


def _items_rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT ref, kind, closed_by, closed_token_id, payload_json FROM main.owner_item ORDER BY id"
        ).fetchall()


# --- допуск (П10) -------------------------------------------------------------------------------------------


async def test_only_cookie_and_agent_are_let_in(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    queue = await _queue(client, ui=ui)
    assert queue["items"] == [] and isinstance(queue["seq"], int)
    assert queue["agent"] == {
        "online": False,
        "seen_ts": None,
        "talk_unread": 0,
        "talk_open": 0,
        "talk_last_id": 0,
        "talk_progress": 0,
    }
    assert (await _queue(client, agent))["items"] == []
    for token in (bot, w["tok"]):
        for method, path, body in (
            ("get", "/owner_channel/queue", None),
            ("get", "/owner_channel/changes", None),
            ("post", "/owner_channel/req:1/answer", {}),
            ("get", "/owner_channel/closed", None),
            ("put", "/owner_channel/agent-actions", {}),
        ):
            kw = {} if body is None else {"json": body}
            r = await client.request(method, f"{API}{path}", headers=_h(token), **kw)
            assert r.status_code == 403, (token, path, r.status_code)
    # `/closed` и поручения — только кука; объявление действий — только агент.
    r = await client.get(f"{API}/owner_channel/closed", headers=_h(agent))
    assert r.status_code == 403
    r = await ui.put(f"{API}/owner_channel/agent-actions", {"actions": []})
    assert r.status_code == 403
    r = await client.get(f"{API}/owner_channel/closed", headers={})
    assert r.status_code in (401, 403)


# --- признак «в канале» (3.1, П3, А11) ----------------------------------------------------------------------


async def test_in_channel_follows_route_table_and_manual_session_is_mine(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    req_id = await _permission(client, w, sid)
    ref = f"req:{req_id}"

    async def state():
        mine = _refs(await _queue(client, ui=ui))[ref]
        seen = ref in _refs(await _queue(client, agent))
        return mine["in_channel"], seen

    assert await state() == (False, False)  # таблица по умолчанию — всё владельцу
    r = await ui.c.patch(
        f"{API}/projects/{w['p3']}", json={"card_route": {"permission": "agent"}}, headers=ui.headers
    )
    assert r.status_code == 200, r.text
    assert await state() == (False, False)  # ручная сессия хаба — всегда владельцу
    await _patch_session(ui, sid, {"mode": "auto"})
    assert await state() == (True, True)
    r = await ui.c.patch(
        f"{API}/projects/{w['p3']}", json={"card_route": {"permission": "me"}}, headers=ui.headers
    )
    assert r.status_code == 200, r.text
    assert await state() == (False, False)

    # Карточка внешней сессии (черновик) — тоже по таблице проекта (П7).
    sub = await _sub(client, w, A, role="внешний")
    assert sub["sub"]["id"]
    outbox_id = (await _send(client, w, w["ch"], {"session": base._sess(A)}))["outbox_id"]
    card = _refs(await _queue(client, ui=ui))[f"draft:{outbox_id}"]
    assert card["in_channel"] is False and card["session_id"] is None
    assert f"draft:{outbox_id}" not in _refs(await _queue(client, agent))
    await _channel_on(ui, w)
    card = _refs(await _queue(client, ui=ui))[f"draft:{outbox_id}"]
    assert card["in_channel"] is True and card["auto_ok"] is True
    assert f"draft:{outbox_id}" in _refs(await _queue(client, agent))


async def test_agent_sees_only_projects_of_its_token(client, ui, db, world):
    w = world
    only4, _ = _agent(db, project_ids=[w["p4"]])
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    req_id = await _permission(client, w, sid)
    assert _refs(await _queue(client, only4)) == {}
    # Чужой проект — неотличим от несуществующей карточки.
    await _answer(client, f"req:{req_id}", {"action": "allow"}, token=only4, expect=404)


# --- ответ агента = клик владельца (П8, П9) ----------------------------------------------------------------------


async def test_agent_answer_on_permission_looks_like_owner(client, ui, db, clock, world):
    w = world
    agent, agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    first = await _permission(client, w, sid)
    second = await _permission(client, w, sid)
    ref = f"req:{first}"

    # Не в канале — 403 `not_in_channel`; кука решает всегда.
    await _answer(client, ref, {"action": "allow"}, token=agent, expect=403)
    await _channel_on(ui, w, sid)
    card = _refs(await _queue(client, agent))[ref]
    assert card["kind"] == "permission" and card["session_id"] == sid and card["role"] == ROLE
    assert card["path"] == {"project": "Umbrella", "head": None, "author": ROLE}
    assert [a["action"] for a in card["answers"]] == ["allow", "deny"]
    assert card["full"]["tool"] == "Bash" and "git push" in card["full"]["input_text"]

    await _answer(client, ref, {"action": "maybe"}, token=agent, expect=422)
    await _answer(client, ref, {"action": "allow", "text": "лишнее"}, token=agent, expect=422)
    done = await _answer(client, ref, {"action": "allow"}, token=agent)
    assert done == {"ref": ref, "status": "allowed"}
    # Второй ответ — 409 функции ядра.
    await _answer(client, ref, {"action": "allow"}, token=agent, expect=409)
    assert ref not in _refs(await _queue(client, ui=ui))

    with db.reader() as conn:
        request, _rec = held_work._request(conn, first)  # noqa: SLF001
    assert json.loads(request["answer_json"]) == {"decision": "allow"}  # без `by`: ответ владельца
    events = (await ui.get(f"{API}/sessions/{sid}/events")).json()
    assert any(e["kind"] == "decision" and e["summary"] == "владелец разрешил: Bash" for e in events)

    # Источник — только у ответа агента; владелец отвечает без строки.
    await _answer(client, f"req:{second}", {"action": "deny", "text": "не надо"}, ui=ui)
    rows = _items_rows(db)
    assert rows == [(ref, "permission", "agent", agent_id, json.dumps({"action": "allow"}))]
    audits = _audits(db, "owner_answer")
    assert audits == [{"ref": ref, "kind": "permission", "action": "allow", "project_id": w["p3"]}]
    closed = (await ui.get(f"{API}/owner_channel/closed")).json()["items"]
    assert [(c["ref"], c["closed_by"], c["closed_token_id"]) for c in closed] == [(ref, "agent", agent_id)]


async def test_question_answer_and_unknown_refs(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    body = {"questions": [{"question": "Какой цвет?", "options": [{"label": "синий"}, {"label": "красный"}]}]}
    asked = await _ask(
        client, {"host": w["host"]}, sid, kind="question", tool="AskUserQuestion", tool_input=body
    )
    ref = f"req:{asked['id']}"
    card = _refs(await _queue(client, agent))[ref]
    assert card["kind"] == "question" and card["full"]["questions"][0]["question"] == "Какой цвет?"
    assert [a["action"] for a in card["answers"]] == ["answer", "deny"]
    await _answer(client, ref, {"action": "answer"}, token=agent, expect=422)
    done = await _answer(client, ref, {"action": "answer", "answers": {"Какой цвет?": "синий"}}, token=agent)
    assert done["status"] == "answered"
    for bad in ("nope:1", "req:x", "req:999999", "say:77"):
        await _answer(client, bad, {"action": "allow"}, ui=ui, expect=404)


# --- черновик (П9) ------------------------------------------------------------------------------------------


def _outbox(db, outbox_id) -> dict:
    with db.reader() as conn:
        cur = conn.execute("SELECT * FROM main.outbox WHERE id = ?", (outbox_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _outcomes(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute("SELECT outbox_id, outcome FROM main.draft_outcome ORDER BY id").fetchall()


async def test_draft_answered_by_agent(app, client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    first = (await _send(client, w, w["ch"], _me(sid, None), text="черновик раз"))["outbox_id"]
    second = (await _send(client, w, w["ch"], _me(sid, None), text="черновик два"))["outbox_id"]
    queue = _refs(await _queue(client, agent))
    card = queue[f"draft:{first}"]
    assert card["session_id"] == sid and card["in_channel"] is True and card["full"]["text"] == "черновик раз"
    assert [a["action"] for a in card["answers"]] == ["approve", "cancel"]

    # Правка текста агентом: текст уходит новый, но в статистику правок владельца не входит, исход — `approved`.
    before = app.state.activity.last_ms
    clock.advance_ms(1000)
    done = await _answer(
        client, f"draft:{first}", {"action": "approve", "text": "черновик раз!"}, token=agent
    )
    assert done["status"] == "queued"
    row = _outbox(db, first)
    assert (row["status"], row["text"], row["draft_text"]) == ("queued", "черновик раз!", None)
    await _answer(client, f"draft:{first}", {"action": "cancel"}, token=agent, expect=409)
    await _answer(client, f"draft:{second}", {"action": "cancel"}, token=agent)
    assert _outbox(db, second)["status"] == "cancelled"
    assert _outcomes(db) == [(first, "approved"), (second, "declined")]
    with db.reader() as conn:
        actor, detail = conn.execute(
            "SELECT actor, detail_json FROM main.audit WHERE action = 'outbox_approve' AND outbox_id = ? "
            "AND actor != 'outbox'",
            (first,),
        ).fetchone()
    detail = json.loads(detail)
    assert actor == "background" and detail["source"] == "agent" and detail["edited"] is False
    # П9: «На связи» от ответа агента не зажигается; от ответа владельца — да.
    assert app.state.activity.last_ms == before
    third = (await _send(client, w, w["ch"], _me(sid, None), text="черновик три"))["outbox_id"]
    await _answer(client, f"draft:{third}", {"action": "approve", "text": "черновик три!"}, ui=ui)
    assert app.state.activity.last_ms == _now(clock)
    assert _outcomes(db)[-1] == (third, "edited") and _outbox(db, third)["draft_text"] == "черновик три"
    queue = _refs(await _queue(client, ui=ui))
    assert f"draft:{first}" not in queue and f"draft:{second}" not in queue


# --- «нужен человек» ----------------------------------------------------------------------------------------


async def test_human_request_answered_by_agent(client, ui, db, world):
    w = world
    agent, agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    made = (await _human_create(client, w, session=_hs(sid)))["request"]
    ref = f"human:{made['id']}"
    card = _refs(await _queue(client, ui=ui))[ref]
    assert (
        card["in_channel"] is False and card["session_id"] == sid and card["until_ts"] == made["expires_ts"]
    )
    await _answer(client, ref, {"action": "done"}, token=agent, expect=403)
    await _channel_on(ui, w, sid)
    await _answer(client, ref, {"action": "done"}, token=agent)
    with db.reader() as conn:
        status, by, token_id = conn.execute(
            "SELECT status, decided_by, decided_token_id FROM main.human_request WHERE id = ?", (made["id"],)
        ).fetchone()
    assert (status, by, token_id) == ("done", "agent", agent_id)
    await _answer(client, ref, {"action": "failed"}, token=agent, expect=409)


# --- заявки: правило, выход, вступление, нажатие, заявка агента ---------------------------------------------


def _pending(db, table, row_id) -> None:
    db.write_sync(
        lambda conn: conn.execute(f"UPDATE main.{table} SET status = 'pending' WHERE id = ?", (row_id,))  # noqa: S608
    )


async def test_request_kinds_bound_to_session_and_answered(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    me = _me(sid, None)

    # Правило раскладки — с привязкой к сессии хаба (А9).
    r = await client.post(
        f"{API}/rule-requests",
        json={"project_id": w["p3"], "account_id": w["acc"], "kind": "all", **me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    rule_ref = f"rule:{r.json()['id']}"
    # Выход из группы.
    r = await client.post(f"{API}/leaves", json={"channel_id": w["ch2"], **me}, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    leave_ref = f"leave:{r.json()['id']}"
    # Вступление: проверка коннектором пропущена — заявка сразу «ждёт владельца».
    r = await client.post(
        f"{API}/joins", json={"project_id": w["p3"], "target": "@some_channel", **me}, headers=_h(w["tok"])
    )
    assert r.status_code == 201, r.text
    join_id = r.json()["id"]
    _pending(db, "join_request", join_id)
    join_ref = f"join:{join_id}"
    # Заявка агента «напарник».
    body = {"kind": "new_session", "project_id": w["p3"], "role": "Тестер", "task": "прогнать тесты", **me}
    r = await client.post(f"{API}/agent-requests", json=body, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    agent_ref = f"agent_request:{r.json()['id']}"
    # Нажатие кнопки бота — строкой, как её кладёт `presses.create` (автор — подписка сессии).
    with db.reader() as conn:
        sub_id = conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]
    now = _now(clock)
    press_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.button_press (account_id, channel_id, project_id, message_id, ext_id, row, "
                "col, text, by, status, created_ts, updated_ts, author_sub_id, seq) "
                "VALUES (?, ?, ?, ?, '100', 0, 0, 'OK', 'session', 'pending', ?, ?, ?, 1)",
                (w["acc"], w["ch"], w["p3"], w["last"], now, now, sub_id),
            ).lastrowid
        )
    )
    press_ref = f"press:{press_id}"

    refs = {rule_ref, leave_ref, join_ref, agent_ref, press_ref}
    queue = _refs(await _queue(client, agent))
    assert refs <= set(queue)
    for ref in refs:
        assert queue[ref]["session_id"] == sid and queue[ref]["in_channel"] is True, ref
    kinds = [c["kind"] for c in (await _queue(client, agent))["items"]]
    assert kinds == sorted(kinds, key=["draft", "press", "join", "leave", "rule", "agent_request"].index)

    await _answer(client, rule_ref, {"action": "decline"}, token=agent)
    await _answer(client, leave_ref, {"action": "decline"}, token=agent)
    await _answer(client, join_ref, {"action": "decline"}, token=agent)
    await _answer(client, press_ref, {"action": "decline"}, token=agent)
    await _answer(client, agent_ref, {"action": "decline", "text": "позже"}, token=agent)
    assert not refs & set(_refs(await _queue(client, ui=ui)))
    for ref in refs:
        await _answer(client, ref, {"action": "approve"}, token=agent, expect=409)
    with db.reader() as conn:
        note = conn.execute("SELECT note FROM main.agent_request").fetchone()[0]
    assert note == "позже"
    assert sorted(r[0] for r in _items_rows(db)) == sorted(refs)


# --- сессия: прерванная реплика, пауза, напоминание ---------------------------------------------------------


async def test_session_waits_and_reminder(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "сделай отчёт"})
    assert r.status_code == 200, r.text
    say_id = r.json()["id"]
    db.write_sync(lambda conn: held_work.mark_interrupted(conn, held.get(conn, sid), say_id, _now(clock)))
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "ещё"})
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'paused' WHERE id = ?", (sid,))
    )
    queue = _refs(await _queue(client, agent))
    assert queue[f"say:{say_id}"]["kind"] == "interrupted" and queue[f"say:{say_id}"]["full"]["text"] == (
        "сделай отчёт"
    )
    assert queue[f"paused:{sid}"]["kind"] == "paused"
    await _answer(client, f"say:{say_id}", {"action": "resend"}, token=agent)
    await _answer(client, f"say:{say_id}", {"action": "drop"}, token=agent, expect=409)
    await _answer(client, f"paused:{sid}", {"action": "wake"}, token=agent)
    with db.reader() as conn:
        assert held.get(conn, sid).wake_requested_ts == _now(clock)

    # Сработавшее напоминание — к сессии, чьей подписке ушёл сигнал.
    with db.reader() as conn:
        sub_id = conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]
    r = await client.post(
        f"{API}/reminders",
        json={"channel_id": w["ch"], "at": _iso(clock, 60), "note": "проверить", **_me(sid, None)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    rem_id = r.json()["id"] if "id" in r.json() else r.json()["reminder"]["id"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.reminder SET status = 'fired', fired_ts = ?, signal_sub_id = ? WHERE id = ?",
            (_now(clock), sub_id, rem_id),
        )
    )
    ref = f"reminder:{rem_id}"
    assert _refs(await _queue(client, agent))[ref]["session_id"] == sid
    assert (await _answer(client, ref, {"action": "done"}, token=agent))["status"] == "done"
    await _answer(client, ref, {"action": "done"}, token=agent, expect=409)


# --- длинный опрос ------------------------------------------------------------------------------------------


async def test_changes_long_poll_by_seq(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    first = await _queue(client, agent)
    assert first["agent"]["online"] is True  # опрос агента отметил «на связи»
    seq = (await _queue(client, agent))["seq"]
    # Ничего не изменилось — ответ по истечении ожидания с тем же `seq`.
    r = await client.get(f"{API}/owner_channel/changes", params={"since": seq, "wait": 0.2}, headers=_h(agent))
    assert r.status_code == 200 and r.json()["seq"] == seq
    # Отметка позади — ответ сразу полным снимком.
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    req_id = await _permission(client, w, sid)
    r = await client.get(f"{API}/owner_channel/changes", params={"since": seq, "wait": 5}, headers=_h(agent))
    assert r.status_code == 200
    assert r.json()["seq"] > seq and f"req:{req_id}" in _refs(r.json())
    r = await ui.get(f"{API}/owner_channel/changes", params={"since": 0, "wait": 0})
    assert r.status_code == 200 and f"req:{req_id}" in _refs(r.json())


def test_principal_helper_rejects_bot_and_session_tokens():
    from hub.core import owner_queue as zq

    def p(scopes, kind="background"):
        return security.Principal(token_id=1, kind=kind, scopes=frozenset(scopes), project_ids=None)

    assert zq.is_agent(p({"human"})) is True
    assert zq.is_agent(p({"human_bot"})) is False
    assert zq.is_agent(p({"read", "draft"}, "interactive")) is False
    assert zq.is_agent(p(set(), "ui")) is False


async def test_changes_waits_through_unrelated_seq_moves(client, ui, db, clock, world):
    """Ревью Ф56 (3): общий `seq` двигает любая сессия — опрос, проснувшись, сверяет отпечаток очереди и
    без изменений ждёт дальше до срока; настоящее изменение возвращает снимок сразу."""
    import asyncio
    import time

    w = world
    sid = await _auto(client, ui, w, mode="auto")
    await _channel_on(ui, w, sid)
    await _permission(client, w, sid)
    token, _ = _agent(db)
    other = await _auto(client, ui, w, mode="manual")
    before = await _queue(client, token)

    async def nudge():
        await asyncio.sleep(0.3)
        await _host(client, w, f"sessions/{other}/turn", {"state": "running", "source": "signal"})
        await asyncio.sleep(0.3)
        await _host(client, w, f"sessions/{other}/turn", {"state": "idle", "last_turn_ts": 1_000})

    began = time.monotonic()
    poll = client.get(f"{API}/owner_channel/changes", params={"since": before["seq"], "wait": 2}, headers=_h(token))
    r, _ = await asyncio.gather(poll, nudge())
    took = time.monotonic() - began
    assert r.status_code == 200
    assert [c["ref"] for c in r.json()["items"]] == [c["ref"] for c in before["items"]]
    assert r.json()["seq"] > before["seq"]  # отметка ушла вперёд, снимок тот же
    assert took >= 1.8, f"опрос вернулся за {took:.2f} с с тем же составом очереди — холостой оборот"
    # Настоящее изменение (новая карточка) — возврат сразу.
    seq = r.json()["seq"]

    async def change():
        await asyncio.sleep(0.3)
        await _permission(client, w, sid)

    began = time.monotonic()
    poll = client.get(f"{API}/owner_channel/changes", params={"since": seq, "wait": 5}, headers=_h(token))
    r, _ = await asyncio.gather(poll, change())
    assert r.status_code == 200 and len(r.json()["items"]) == len(before["items"]) + 1
    assert time.monotonic() - began < 3
