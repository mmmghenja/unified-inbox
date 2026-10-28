"""Ф88-1 (docs/architecture.md): руки агента владельца над
сессиями.

- E1: агент (bearer ровно `human`) проходит каждую ручку раздела B (не 401/403); бот `human_bot`, ключ
  терминала и токен сессии с `read`+`draft` — 403 на записи; на чтении токен с `read` — как раньше;
- П1: `POST /subs/{id}/end` — агент снимает подписку как кука (`ended_by_owner`), токен сессии по-прежнему
  снимает свою, бот — 403;
- E2 + П3 + П8: реплика агента — `held_say.source='agent'`, `typed=0`, карточка `turn` закрыта `agent` с его
  токеном, его поручение по ней — `agent`;
- П3: «Поднять» агентом по карточке `paused:` при открытом своём поручении — поручение закрыто `agent`, не
  `ui`, и через 5 минут не возвращается «завис»;
- E4: `GET /sessions` и `GET /sessions/{id}` агенту — тот же вид, что куке (и `today`);
- E5 + П4: `POST /peer-msgs` агентом внешней сессии — у получателя сообщение «от владельца» (`from_user`),
  `to.sub_id` разрешён; бот — 403;
- E6 + П2: ручки вне списка (`/outbox/{id}/approve`, `/session-requests/{id}/decide`, `/sessions/live`,
  `/sessions/autosend-count`) агенту — 403.

Мир и помощники — test_hub_sessions.py, test_f41_autopilot_5b.py, test_f56_queue.py, test_owner_turn.py."""

import re

import pytest

from hub.core import owner_queue as zq
from hub.core import subs

from . import test_hub_sessions as base
from .agent_hands import AGENT_HANDS
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _agent, _bot, _channel_on, _permission, _queue, _refs
from .test_f65_errand_life import _errand_row, _take
from .test_hub_sessions import ROLE, A, _h, _mk, _sess, _sub, _token
from .test_owner_turn import ASK, _turn_rows, _Turns

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000

# Ручки раздела B (с П1, П2, П5): (метод, путь с {sid}/{sub}, тело). Тела — как у интерфейса.
WRITES = [
    ("POST", "/sessions", "create"),
    ("POST", "/sessions/{sid}/close", {}),
    ("POST", "/sessions/{sid}/handoff", {}),
    ("POST", "/sessions/{sid}/fallback/return", {}),
    ("PATCH", "/sessions/{sid}", {"mode": "auto"}),
    ("POST", "/sessions/{sid}/compact", {}),
    ("POST", "/sessions/{sid}/wake", {}),
    ("POST", "/sessions/{sid}/mcp-reload", {}),
    ("POST", "/sessions/{sid}/say", {"text": "сделай"}),
    ("POST", "/sessions/{sid}/files?name=a.txt", b"hello"),
    ("DELETE", "/sessions/{sid}/files/f0123456789abcdef0123456789abcdef", None),
    ("POST", "/sessions/{sid}/says/1/resend", {}),
    ("DELETE", "/sessions/{sid}/says/1", None),
    ("POST", "/sessions/{sid}/interrupt", {}),
    ("POST", "/sessions/{sid}/sleep", {}),
    ("POST", "/work/pause/clear", {}),
    ("GET", "/sessions/{sid}/role-text", None),  # чтение, но как прежде — только владелец и агент
]
READS = [
    "/sessions",
    "/sessions/{sid}",
    "/sessions/{sid}/events",
    "/sessions/{sid}/events/1",
    "/subs?live=1",
    "/subs?offline=1",
]


def test_agent_hands_match_tested_routes(app):
    """Список исключений структурного теста совпадает с тем, что проверяют тесты этого файла, и каждая
    ручка в нём существует."""
    tested = {(m.lower(), "/api/v1" + p.split("?")[0]) for m, p, _ in WRITES}
    tested |= {("get", "/api/v1" + p.split("?")[0]) for p in READS}
    tested |= {("post", "/api/v1/subs/{sid}/end"), ("post", "/api/v1/peer-msgs")}

    def norm(path):
        return re.sub(r"/(\d+|f[0-9a-f]{32})(?=/|$)", "/X", re.sub(r"\{[^}]+\}", "X", path))

    assert {(m, norm(p)) for m, p in tested} == {(m, norm(p)) for m, p in AGENT_HANDS}
    paths = app.openapi()["paths"]
    for method, template in AGENT_HANDS:
        assert method in paths[template], (method, template)


async def _call(client, method, path, body, headers):
    if isinstance(body, bytes):
        return await client.request(method, f"{API}{path}", content=body, headers=headers)
    if body is None:
        return await client.request(method, f"{API}{path}", headers=headers)
    return await client.request(method, f"{API}{path}", json=body, headers=headers)


def _fill(w, sid, path, body):
    if body == "create":
        body = {"project_id": w["p3"], "engine": "claude"}
    return path.format(sid=sid), body


def _term(db) -> str:
    return _token(db, name="term-agent", kind="background", scopes=("term",))[0]


# --- E1 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), WRITES, ids=[f"{m} {p}" for m, p, _ in WRITES])
async def test_agent_passes_every_write(client, ui, db, world, method, path, body):
    w = world
    agent, _ = _agent(db)
    sid = (await _mk(ui, w, role=ROLE))["id"]
    path, body = _fill(w, sid, path, body)
    r = await _call(client, method, path, body, _h(agent))
    assert r.status_code not in (401, 403), r.text


@pytest.mark.parametrize(("method", "path", "body"), WRITES, ids=[f"{m} {p}" for m, p, _ in WRITES])
async def test_others_still_denied_on_writes(client, ui, db, world, method, path, body):
    w = world
    bot, _ = _bot(db)
    term = _term(db)
    sid = (await _mk(ui, w, role=ROLE))["id"]
    path, body = _fill(w, sid, path, body)
    for token in (bot, term, w["tok"], w["host"]):
        r = await _call(client, method, path, body, _h(token))
        assert r.status_code == 403, (token == bot, r.text)


@pytest.mark.parametrize("path", READS)
async def test_reads_agent_and_read_token_pass_bot_and_term_denied(client, ui, db, world, path):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sid = (await _mk(ui, w, role=ROLE))["id"]
    path = path.format(sid=sid)
    for token in (agent, w["tok"]):
        r = await client.get(f"{API}{path}", headers=_h(token))
        assert r.status_code not in (401, 403), r.text
    for token in (bot, _term(db)):
        r = await client.get(f"{API}{path}", headers=_h(token))
        assert r.status_code == 403, r.text


# --- П1: снять подписку -------------------------------------------------------------------------------------


async def test_sub_end_agent_as_owner_session_own_bot_denied(client, ui, db, world):
    w = world
    agent, agent_id = _agent(db)
    bot, _ = _bot(db)
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]

    r = await client.post(f"{API}/subs/{sub_id}/end", json={}, headers=_h(bot))
    assert r.status_code == 403, r.text
    # Агенту, как куке, — без `session` и причины.
    r = await client.post(f"{API}/subs/{sub_id}/end", json={"reason": "unsubscribed"}, headers=_h(agent))
    assert r.status_code == 422, r.text
    r = await client.post(f"{API}/subs/{sub_id}/end", json={}, headers=_h(agent))
    assert r.status_code == 200, r.text
    assert r.json()["sub"]["end_reason"] == "ended_by_owner"
    with db.reader() as conn:
        assert subs.get_sub(conn, sub_id).end_reason == "ended_by_owner"
        token_id = conn.execute(
            "SELECT api_token_id FROM main.audit WHERE action = 'sub_end' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert token_id == agent_id  # C5: в аудите видно, что снял агент

    # Токен сессии — по-прежнему свою подписку.
    again = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    r = await client.post(
        f"{API}/subs/{again}/end", json={"session": _sess(A), "reason": "unsubscribed"}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["sub"]["end_reason"] == "unsubscribed"


# --- E2 + П3 + П8: реплика агента ---------------------------------------------------------------------------


async def test_agent_say_is_agent_untyped_and_closes_turn_and_own_errand(client, ui, db, world):
    w = world
    agent, agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    ref = f"turn:{sid}:1"
    made = (await _take(client, agent, ref))["errand"]

    r = await client.post(f"{API}/sessions/{sid}/say", json={"text": "сделай так"}, headers=_h(agent))
    assert r.status_code == 200, r.text
    say_id = r.json()["id"]
    with db.reader() as conn:
        source, typed = conn.execute(
            "SELECT source, typed FROM main.held_say WHERE id = ?", (say_id,)
        ).fetchone()
    assert (source, typed) == ("agent", 0)
    ((row_ref, closed_by, closed_token, payload),) = _turn_rows(db)
    assert (row_ref, closed_by, closed_token, payload["say_id"]) == (ref, "agent", agent_id, say_id)
    by, errand_payload = _errand_row(db, made["id"])
    assert by == "agent" and errand_payload.get("returned") is None


async def test_cookie_say_unchanged_typed_ui(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "из окна"})
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        assert conn.execute(
            "SELECT source, typed FROM main.held_say WHERE id = ?", (r.json()["id"],)
        ).fetchone() == ("ui", 1)


# --- П3: «Поднять» агентом по своей карточке ----------------------------------------------------------------


async def test_agent_wake_closes_own_errand_as_agent_and_it_does_not_hang(client, ui, db, clock, world):
    w = world
    agent, agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    assert (await ui.post(f"{API}/sessions/{sid}/say", {"text": "ждёт паузы"})).status_code == 200
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'paused' WHERE id = ?", (sid,))
    )
    ref = f"paused:{sid}"
    made = (await _take(client, agent, ref))["errand"]

    r = await client.post(f"{API}/sessions/{sid}/wake", json={}, headers=_h(agent))
    assert r.status_code == 200, r.text
    by, payload = _errand_row(db, made["id"])
    assert by == "agent" and payload.get("returned") != zq.TAKEN_BY_OWNER
    with db.reader() as conn:
        token = conn.execute(
            "SELECT closed_token_id FROM main.owner_item WHERE id = ?", (made["id"],)
        ).fetchone()[0]
    assert token == agent_id

    clock.advance_ms(6 * MIN)
    card = _refs(await _queue(client, ui=ui)).get(ref)
    assert card is None or card["errand_returned"] is None or card["errand_returned"]["by"] == "agent"
    assert _errand_row(db, made["id"])[0] == "agent"


# --- E4: вид как у куки -------------------------------------------------------------------------------------


async def test_agent_sees_sessions_as_cookie(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _mk(ui, w, project="p4")

    mine = (await client.get(f"{API}/sessions", headers=_h(agent))).json()
    cookie = (await ui.get(f"{API}/sessions")).json()
    assert mine == cookie and "today" in mine and len(mine["items"]) == 2
    item = next(i for i in mine["items"] if i["id"] == sid)
    for key in ("state", "engine", "model", "role", "agent_state", "workdir"):
        assert key in item, key

    one = (await client.get(f"{API}/sessions/{sid}", headers=_h(agent))).json()
    assert one == (await ui.get(f"{API}/sessions/{sid}")).json()
    assert not one["session"].get("sub_stale")


# --- E5 + П4: письмо внешней сессии -------------------------------------------------------------------------


async def test_agent_writes_external_session_as_owner(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]

    body = {"text": "от владельца через агента", "to": {"project_id": w["p3"], "role": ROLE}}
    r = await client.post(f"{API}/peer-msgs", json=body, headers=_h(agent))
    assert r.status_code == 201, r.text
    assert r.json()["msg"]["from_user"] is True
    first = r.json()["msg"]["id"]
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": "ещё", "to": {"project_id": w["p3"], "sub_id": sub_id}},
        headers=_h(agent),
    )
    assert r.status_code == 201, r.text
    second = r.json()["msg"]["id"]

    r = await client.post(f"{API}/peer-msgs", json=body, headers=_h(bot))
    assert r.status_code == 403, r.text
    # Сессии `to.sub_id` по-прежнему нельзя.
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": "x", "to": {"project_id": w["p3"], "sub_id": sub_id}, "session": _sess(A)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 422, r.text

    params = {"pid": A[0], "proc_start": A[1], "session_id": A[2]}
    r = await client.get(f"{API}/peer-msgs", params=params, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    got = {m["id"]: m for m in r.json()["items"]}
    assert got[first]["from_user"] is True and got[second]["from_user"] is True


# --- E6 + П2: ручки вне списка ------------------------------------------------------------------------------


async def test_routes_outside_list_still_denied_to_agent(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    rid = await _permission(client, w, sid)
    for method, path, body in (
        ("POST", "/outbox/1/approve", {}),
        ("POST", f"/session-requests/{rid}/decide", {"decision": "allow"}),
        ("GET", "/sessions/autosend-count?project_id=1", None),
        ("GET", "/sessions/live", None),
    ):
        r = await _call(client, method, path, body, _h(agent))
        assert r.status_code == 403, (path, r.text)
