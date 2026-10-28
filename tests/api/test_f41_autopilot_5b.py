"""Ф41-5б (docs/architecture.md): заявки агентов (перенос
беседы, напарник), отправка людям под выключателями `hub_send` / `hub_send_new`, прежние заявки (вступление,
выход, правило раскладки) под выключателями. Доказательства критика (1), (5), (6), (7), (8) — здесь.

Помощники и «мир» — из test_hub_sessions.py и test_f41_autopilot.py; хост поддельный."""

import hashlib
import json

from hub.core import agent_requests, autosend, held, peer_msgs
from hub.core import join as join_mod

from . import test_hub_sessions as base
from .conftest import insert_channel
from .test_f41_autopilot import KEY, PROC, _put
from .test_hub_sessions import ROLE, A, _audits, _h, _host, _hsub, _mk, _now, _sess, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
DIGEST = hashlib.sha256(KEY.encode()).hexdigest()


def _me(sid: int, key: str | None = KEY) -> dict:
    pid, start, session_id = held.triple(sid)
    body: dict = {"session": {"pid": pid, "proc_start": start, "session_id": session_id}}
    if key is not None:
        body["session_key"] = key
    return body


async def _auto(client, ui, w, *, role=ROLE, mode="auto", project="p3", **sub) -> int:  # noqa: F811
    """Сессия хаба с подпиской на проект, идущим процессом и ключом запуска."""
    sid = (await _mk(ui, w, role=role, mode=mode, project=project))["id"]
    await _hsub(client, w, sid, cwd=w["dir3" if project == "p3" else "dir4"], **sub)
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "launch_key_hash": DIGEST})
    return sid


async def _send(client, w, channel, ident, *, token=None, expect=200, **extra) -> dict:
    body = {"channel_id": channel, "text": "ответ", "idempotency_key": f"k-{_send.n}", **ident, **extra}
    _send.n += 1
    r = await client.post(f"{API}/messages/send", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


_send.n = 0


def _outbox(db, outbox_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, auto_role, auto_session FROM main.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()


def _channel(db, channel_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT project_id, archived, last_read_seq, last_read_ts FROM main.channel WHERE id = ?",
            (channel_id,),
        ).fetchone()


def _peers(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT from_user, from_sub_id, to_project_id, to_role_key, to_no, text FROM main.peer_msg "
            "ORDER BY id"
        ).fetchall()


# --- отправка людям (5.4, П7, П8) ---------------------------------------------------------------------------


async def test_hub_send_each_refusal_and_the_happy_path(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)
    manual = await _auto(client, ui, w, role="тестер", mode="manual")

    # Выключатель стоит — черновик; ручная сессия — черновик и при снятом.
    assert (await _send(client, w, w["ch"], _me(sid)))["status"] == "pending_approval"
    await _put(ui, "global", "hub_send", False)
    assert (await _send(client, w, w["ch"], _me(manual)))["status"] == "pending_approval"

    # (1) тройка `hub:N` без ключа, с чужим ключом и внешняя сессия — черновик.
    assert (await _send(client, w, w["ch"], _me(sid, None)))["status"] == "pending_approval"
    assert (await _send(client, w, w["ch"], _me(sid, "x" * 64)))["status"] == "pending_approval"
    r = await client.post(
        f"{API}/subs", json={"session": _sess(A, cwd=w["dir3"]), "role": "внешний"}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    outside = {"session": _sess(A), "session_key": KEY}
    assert (await _send(client, w, w["ch"], outside))["status"] == "pending_approval"

    # (8) беседа чужого проекта и беседа без проекта — черновик.
    other = insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-chat", kind="group", title="Чужая")
    loose = insert_channel(db, w["acc"], project_id=None, ext_id="loose", kind="group", title="Ничья")
    assert (await _send(client, w, other, _me(sid)))["status"] == "pending_approval"
    assert (await _send(client, w, loose, _me(sid)))["status"] == "pending_approval"

    # `confirm=True` (MCP кладёт `author_session`) — черновик при любом выключателе.
    asked = {"author_session": _me(sid)["session"]}
    assert (await _send(client, w, w["ch"], asked))["status"] == "pending_approval"
    assert _audits(db, "outbox_autosend") == []

    # Удачный путь: в очереди без подтверждения, отметки и свой аудит; побочных действий клика нет.
    read_before = _channel(db, w["ch"])
    sent = await _send(client, w, w["ch"], _me(sid))
    assert sent["status"] == "queued" and sent["auto"] == {"role": ROLE, "by": "autopilot", "mode": None}
    assert _outbox(db, sent["outbox_id"]) == ("queued", ROLE, sid)
    (audit,) = _audits(db, "outbox_autosend")
    assert audit["by"] == "autopilot" and audit["hub_session_id"] == sid and audit["role"] == ROLE
    assert _audits(db, "outbox_approve") == [] and _channel(db, w["ch"]) == read_before

    # Предохранитель по сессии хаба (60 в час): сверх — обычный черновик с причиной.
    limit = autosend.AUTOPILOT_SESSION_PER_HOUR
    autosend.AUTOPILOT_SESSION_PER_HOUR = 1
    try:
        denied = await _send(client, w, w["ch2"], _me(sid))
    finally:
        autosend.AUTOPILOT_SESSION_PER_HOUR = limit
    assert denied["status"] == "pending_approval"
    assert (denied["auto_denied"], denied["auto_limit"]) == ("limit", "session")

    # Сессия без роли (подписки с ролью нет) — черновик.
    plain = (await _mk(ui, w, mode="auto"))["id"]
    await _hsub(client, w, plain, cwd=w["dir3"])
    await _host(client, w, f"sessions/{plain}/state", {"state": "idle", **PROC, "launch_key_hash": DIGEST})
    assert (await _send(client, w, w["ch2"], _me(plain)))["status"] == "pending_approval"

    # Выключатель проекта придержал — снова черновик; сон стёр ключ — тоже.
    await _put(ui, w["p3"], "hub_send", True)
    assert (await _send(client, w, w["ch2"], _me(sid)))["status"] == "pending_approval"
    await _put(ui, w["p3"], "hub_send", None)
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep"})
    assert (await _send(client, w, w["ch2"], _me(sid)))["status"] == "pending_approval"
    r = await client.post(
        f"{API}/messages/send",
        json={"channel_id": w["ch"], "text": "x", "idempotency_key": "nokey", "session_key": KEY},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 422  # ключ — только вместе с `session`


async def test_send_at_goes_in_its_time_without_the_side_effects_of_a_click(client, ui, db, clock, world):  # noqa: F811
    """(7): после авто-отправки со сроком беседа не прочитана, записи `outbox_approve` нет, исхода
    «подтверждено владельцем» автору нет."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "hub_send", False)
    before = _channel(db, w["ch"])
    when = _now(clock) + 3 * 60 * base.MIN
    body = await _send(client, w, w["ch"], _me(sid), send_at=_iso(when))
    assert body["status"] == "queued" and body["auto"]["mode"] == "send_at" and body["send_at_ts"]
    assert _outbox(db, body["outbox_id"]) == ("queued", ROLE, sid)
    assert _audits(db, "outbox_approve") == [] and _channel(db, w["ch"]) == before
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.draft_outcome").fetchone()[0] == 0
        assert conn.execute(
            "SELECT not_before_ts >= ? FROM main.outbox WHERE id = ?", (when - base.MIN, body["outbox_id"])
        ).fetchone()[0]


def _iso(ts_ms: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).isoformat()


async def test_list_branch_is_closed_for_a_session_with_autosend_off(client, ui, db, clock, world):  # noqa: F811
    """3.3: `autosend_ok = 0` закрывает ТОЛЬКО ветку списка Ф37-1; ветку выключателя держит выключатель."""
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": ROLE, "on": True})
    assert r.status_code == 200, r.text
    listed = await _send(client, w, w["ch"], _me(sid, None))
    assert listed["status"] == "queued" and listed["auto"] == {"role": ROLE}
    assert _outbox(db, listed["outbox_id"]) == ("queued", ROLE, None)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET autosend_ok = 0 WHERE id = ?", (sid,))
    )
    assert (await _send(client, w, w["ch"], _me(sid, None)))["status"] == "pending_approval"
    # Внешней сессии с той же ролью список по-прежнему открыт (проверка — по тройке запроса).
    # Ветка выключателя для той же сессии работает сама по себе.
    r = await ui.c.patch(f"{API}/sessions/{sid}", json={"mode": "auto"}, headers=ui.headers)
    assert r.status_code == 200, r.text
    await _put(ui, "global", "hub_send", False)
    sent = await _send(client, w, w["ch"], _me(sid))
    assert sent["status"] == "queued" and sent["auto"]["by"] == "autopilot"


async def test_hub_send_new_first_message(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)

    async def _new(to, ident, expect=200):
        body = {"account_id": w["acc"], "project_id": w["p3"], "to": to, "text": "привет", **ident}
        r = await client.post(f"{API}/drafts/new", json=body, headers=_h(w["tok"]))
        assert r.status_code == expect, r.text
        return r.json()

    await _put(ui, "global", "hub_send", False)  # другой выключатель первому сообщению ничего не даёт
    assert (await _new("@first_one", _me(sid)))["status"] == "pending_approval"
    await _put(ui, "global", "hub_send_new", False)
    assert (await _new("@second_one", _me(sid, None)))["status"] == "pending_approval"
    sent = await _new("@third_one", _me(sid))
    assert (
        sent["status"] == "queued" and sent["auto"]["by"] == "autopilot" and sent["auto"]["mode"] == "paced"
    )
    assert _outbox(db, sent["outbox_id"]) == ("queued", ROLE, sid)
    (audit,) = _audits(db, "outbox_autosend")
    assert audit["new_account"] == w["acc"] and audit["by"] == "autopilot"
    assert _audits(db, "outbox_approve") == []
    # Потолок первых сообщений на аккаунт (`auto_new_daily`).
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = json_set(config_json, '$.auto_new_daily', 1) WHERE id = ?",
            (w["acc"],),
        )
    )
    denied = await _new("@fourth_one", _me(sid))
    assert denied["status"] == "pending_approval"
    assert (denied["auto_denied"], denied["auto_limit"]) == ("limit", "new_daily")


# --- заявки агентов: перенос (5.1, 5.2, П5, П9) -------------------------------------------------------------


async def _move(client, token, channel, project, ident=None, expect=200):
    body = {"project_id": project, **(ident or {})}
    r = await client.patch(f"{API}/channels/{channel}", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _ask(client, token, body, expect=201) -> dict:
    r = await client.post(f"{API}/agent-requests", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def test_move_request_instead_of_refusal_and_owners_decision(client, ui, db, clock, world):  # noqa: F811
    w = world
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    _token(db, name="mcp:p4", scopes=("read", "draft"), project_ids=[w["p4"]])
    sid = await _auto(client, ui, w, mode="manual", token=org)

    refused = await _move(client, org, w["ch"], w["p4"], expect=409)
    assert refused["detail"].startswith("widens_scope:") and refused["request_hint"] is True

    ident = _me(sid, None)
    body = {"kind": "move_channel", "project_id": w["p4"], "channel_id": w["ch"], "reason": "там\nего место"}
    item = await _ask(client, org, {**body, **ident})
    assert (item["status"], item["auto"], item["kind"]) == ("pending", False, "move_channel")
    assert item["who"] == ROLE and item["reason"] == "там его место"
    assert item["payload"] == {"channel_id": w["ch"], "channel_title": "Команда", "from_project_id": w["p3"]}
    again = await _ask(client, org, {**body, **ident}, expect=200)
    assert again["id"] == item["id"]

    # Отказы: интерфейсу нельзя, вид, проект, беседа уже в проекте, чужая беседа.
    r = await ui.post(f"{API}/agent-requests", body)
    assert r.status_code == 403
    await _ask(client, org, {**body, "kind": "нет"}, expect=422)
    await _ask(client, org, {**body, "project_id": 999}, expect=404)
    await _ask(client, org, {**body, "project_id": w["p3"]}, expect=422)
    await _ask(client, org, {**body, "channel_id": 987654}, expect=404)

    # Выдача: интерфейсу — все, токену — свои; `/sync` — только интерфейсу.
    listed = (await ui.get(f"{API}/agent-requests", params={"status": "pending"})).json()["requests"]
    assert [r["id"] for r in listed] == [item["id"]]
    mine = await client.get(f"{API}/agent-requests", headers=_h(org))
    assert [r["id"] for r in mine.json()["requests"]] == [item["id"]]
    other = await client.get(f"{API}/agent-requests", headers=_h(w["tok"]))
    assert other.json()["requests"] == []
    synced = (await ui.get(f"{API}/sync", params={"since": 0})).json()
    assert [r["id"] for r in synced["agent_requests"]] == [item["id"]]
    bearer_sync = (await client.get(f"{API}/sync", params={"since": 0}, headers=_h(org))).json()
    assert "agent_requests" not in bearer_sync

    # Решает только владелец; перенос — тем же кодом, исход — сообщением от владельца из номеров и постоянных слов.
    r = await client.post(f"{API}/agent-requests/{item['id']}/approve", json={}, headers=_h(org))
    assert r.status_code == 403
    done = (await ui.post(f"{API}/agent-requests/{item['id']}/approve")).json()["request"]
    assert (done["status"], done["auto"]) == ("approved", False)
    assert _channel(db, w["ch"])[0] == w["p4"]
    (note,) = _peers(db)
    assert note[0] == 1 and note[2:5] == (w["p3"], "маркетолог", 1)
    assert note[5] == f"Заявка №{item['id']} (перенос беседы #{w['ch']} в проект #{w['p4']}) — выполнена"
    r = await ui.post(f"{API}/agent-requests/{item['id']}/approve")
    assert r.status_code == 409
    assert [a["agent_request_id"] for a in _audits(db, "agent_request_decide")] == [item["id"]]

    # «Нет» со словами владельца; устаревшая заявка закрывается сама.
    second = await _ask(client, org, {**body, "channel_id": w["ch2"], **ident})
    declined = (await ui.post(f"{API}/agent-requests/{second['id']}/decline", {"note": "не сейчас"})).json()
    assert (declined["request"]["status"], declined["request"]["note"]) == ("declined", "не сейчас")
    assert _peers(db)[-1][5] == (
        f"Заявка №{second['id']} (перенос беседы #{w['ch2']} в проект #{w['p4']}) — отклонена. не сейчас"
    )
    third = await _ask(client, org, {**body, "channel_id": w["ch2"], **ident})
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (w["ch2"],)))
    stale = (await ui.post(f"{API}/agent-requests/{third['id']}/approve")).json()["request"]
    assert (stale["status"], stale["note"]) == ("declined", agent_requests.STALE_NOTE)
    assert _channel(db, w["ch2"])[0] == w["p3"]


async def test_request_caps(client, ui, db, clock, world):  # noqa: F811
    w = world
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    chans = [
        insert_channel(db, w["acc"], project_id=w["p3"], ext_id=f"c{i}", kind="group", title=f"Б{i}")
        for i in range(agent_requests.OPEN_PER_KIND + 1)
    ]
    for channel in chans[:-1]:
        await _ask(client, org, {"kind": "move_channel", "project_id": w["p4"], "channel_id": channel})
    last = {"kind": "move_channel", "project_id": w["p4"], "channel_id": chans[-1]}
    r = await client.post(f"{API}/agent-requests", json=last, headers=_h(org))
    assert r.status_code == 429 and "открытых заявок" in r.json()["detail"]
    db.write_sync(lambda conn: conn.execute("UPDATE main.agent_request SET status = 'declined'"))
    limit = agent_requests.TOKEN_PER_DAY
    agent_requests.TOKEN_PER_DAY = agent_requests.OPEN_PER_KIND
    try:
        r = await client.post(f"{API}/agent-requests", json=last, headers=_h(org))
    finally:
        agent_requests.TOKEN_PER_DAY = limit
    assert r.status_code == 429 and "за сутки" in r.json()["detail"]


async def test_auto_session_moves_only_under_the_switch(client, ui, db, clock, world):  # noqa: F811
    """П5 и доказательства (5), (8): перенос сессией хаба в авто регулирует один выключатель."""
    w = world
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    _token(db, name="mcp:p3", scopes=("read", "draft"), project_ids=[w["p3"]])
    sid = await _auto(client, ui, w, token=org)
    await _put(ui, "global", "hub_send", False)
    theirs = insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-chat", kind="group", title="Чужая")

    # (5) выключатель `move_channel` стоит: перенос — 409 и заявка, отправка в ту же беседу — черновик.
    held_move = await _move(client, org, theirs, w["p3"], _me(sid), expect=409)
    assert held_move["detail"].startswith("move_channel:") and held_move["request_hint"] is True
    assert _channel(db, theirs)[0] == w["p4"]
    assert _audits(db, "channel_update")[-1]["project_id"] == w["p3"]
    body = {"kind": "move_channel", "project_id": w["p3"], "channel_id": theirs}
    item = await _ask(client, org, {**body, **_me(sid)})
    assert (item["status"], item["auto"]) == ("pending", False)
    assert (await _send(client, w, theirs, _me(sid), token=org))["status"] == "pending_approval"
    # Без ключа та же сессия в авто — тоже под выключателем (ревью 41-5: обычным путём не идёт).
    plain = await _move(client, org, theirs, w["p3"], _me(sid, None), expect=409)
    assert plain["detail"].startswith("move_channel:")
    # В «Неразобранное» в авто при стоящем выключателе — отказ без заявки.
    r = await client.patch(f"{API}/channels/{theirs}", json={"project_id": None, **_me(sid)}, headers=_h(org))
    assert r.status_code == 409 and r.json()["request_hint"] is False

    # Выключатель снят: перенос проходит, в том числе расширяющий видимость; (8) в чужой проект — нет.
    await _put(ui, "global", "move_channel", False)
    await _move(client, org, theirs, w["p3"], _me(sid))
    assert _channel(db, theirs)[0] == w["p3"] and _audits(db, "channel_update")[-1]["autopilot"] is True
    away = await _move(client, org, w["ch2"], w["p4"], _me(sid), expect=409)
    assert away["detail"].startswith("move_channel:") and _channel(db, w["ch2"])[0] == w["p3"]
    foreign = await _ask(
        client, org, {"kind": "move_channel", "project_id": w["p4"], "channel_id": w["ch2"], **_me(sid)}
    )
    assert (foreign["status"], foreign["auto"]) == ("pending", False)
    # Заявка в свой проект при снятом выключателе проходит сразу (без сообщения от владельца).
    back = insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-two", kind="group", title="Ещё")
    auto = await _ask(
        client, org, {"kind": "move_channel", "project_id": w["p3"], "channel_id": back, **_me(sid)}, 200
    )
    assert (auto["status"], auto["auto"]) == ("approved", True) and _channel(db, back)[0] == w["p3"]
    assert [a["agent_request_id"] for a in _audits(db, "agent_request_auto")] == [auto["id"]]
    assert _peers(db) == []
    # Интерфейсу поля личности не положены.
    r = await ui.c.patch(f"{API}/channels/{back}", json={"session_key": KEY}, headers=ui.headers)
    assert r.status_code == 422


# --- заявки агентов: напарник (5.3, П13) --------------------------------------------------------------------


async def test_partner_request_task_message_and_closed_list(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    task = "Прогони регресс и напиши итог"
    body = {
        "kind": "new_session",
        "project_id": w["p3"],
        "role": "Тестер",
        "task": task,
        "engine": "opencode",
    }

    # Просит только сессия с живой подпиской на проект с ролью.
    await _ask(client, w["tok"], body, expect=409)
    await _ask(client, w["tok"], {**body, "session": _sess(A)}, expect=409)
    await _ask(client, w["tok"], {**body, **_me(sid), "task": " "}, expect=422)
    await _ask(client, w["tok"], {**body, **_me(sid), "engine": "gpt"}, expect=422)

    item = await _ask(client, w["tok"], {**body, **_me(sid)})
    assert (item["status"], item["who"], item["auto"]) == ("pending", ROLE, False)
    assert item["payload"] == {
        "role": "Тестер", "task": task, "engine": "opencode", "model": None, "effort": None, "mode": None,
        "subdir": None, "role_file": None,  # Ф50-7
    }  # fmt: skip
    same = await _ask(client, w["tok"], {**body, **_me(sid), "role": "тестер", "task": "другое"}, expect=200)
    assert same["id"] == item["id"]

    answer = (await ui.post(f"{API}/agent-requests/{item['id']}/approve", {"engine": "claude"})).json()
    partner = answer["session"]
    assert answer["request"]["status"] == "approved"
    assert (partner["role"], partner["engine"], partner["mode"]) == ("Тестер", "claude", "manual")
    assert partner["autosend_ok"] is False and partner["want_sub"]["mode"] == "direct"
    with db.reader() as conn:
        rec = held.get(conn, partner["id"])
    assert (rec.created_by, rec.autosend_ok, rec.role_no) == ("request", 0, 1)
    # Первая задача — сообщением агента от просителя линии напарника; исход — сообщением от владельца.
    task_msg, note = _peers(db)
    assert task_msg[0] == 0 and task_msg[1] is not None
    assert task_msg[2:] == (w["p3"], "тестер", 1, task)
    assert note[0] == 1 and note[5] == f"Заявка №{item['id']} (напарник) — выполнена"
    for forbidden in ("Тестер", "регресс"):
        assert forbidden not in note[5]
    # Напарник подписался — задача ждёт его как обычное сообщение агента.
    sub = (await _hsub(client, w, partner["id"], cwd=w["dir3"], mode="direct"))["sub"]
    with db.reader() as conn:
        assert peer_msgs.signal_count(conn, base.subs.get_sub(conn, sub["id"]), now_ms=_now(clock)) == 1

    # Линия занята — «Завести» отвечает 409, заявка остаётся ждать.
    again = await _ask(client, w["tok"], {**body, **_me(sid)})
    r = await ui.post(f"{API}/agent-requests/{again['id']}/approve", {"role": "Тестер"})
    assert r.status_code == 200  # наименьший свободный номер линии — второй тестер
    assert r.json()["session"]["role_no"] == 2


async def test_partner_in_auto_takes_the_requesters_parameters(client, ui, db, clock, world):  # noqa: F811
    """П13: при авто-принятии движок, модель и режим — как у просителя; сессия, заведённая агентом, сама
    напарника сразу не получает."""
    w = world
    sid = await _auto(client, ui, w)
    body = {
        "kind": "new_session",
        "project_id": w["p3"],
        "role": "тестер",
        "task": "проверь",
        "engine": "opencode",
    }
    held_item = await _ask(client, w["tok"], {**body, **_me(sid)})
    assert (held_item["status"], held_item["auto"]) == ("pending", False)
    await ui.post(f"{API}/agent-requests/{held_item['id']}/decline")
    await _put(ui, "global", "new_session", False)
    # (1) без ключа — заявка ждёт владельца.
    waiting = await _ask(client, w["tok"], {**body, **_me(sid, None)})
    assert (waiting["status"], waiting["auto"]) == ("pending", False)
    await ui.post(f"{API}/agent-requests/{waiting['id']}/decline")

    auto = await _ask(client, w["tok"], {**body, "mode": "manual", **_me(sid)}, expect=200)
    assert (auto["status"], auto["auto"]) == ("approved", True)
    with db.reader() as conn:
        partner = [r for r in held.list_sessions(conn, project_id=w["p3"]) if r.created_by == "agent"]
    (partner,) = partner
    assert (partner.engine, partner.mode, partner.autosend_ok, partner.role_key) == (
        "claude",
        "auto",
        0,
        "тестер",
    )
    assert json.loads(partner.want_sub_json)["mode"] == "direct"
    assert [p[0] for p in _peers(db)] == [1, 1, 0]  # два отказа владельца и задача; исхода авто-заявки нет

    # Заведённая агентом сессия в авто просит ещё одного — заявка идёт владельцу.
    await _hsub(client, w, partner.id, cwd=w["dir3"], mode="direct")
    await _host(
        client, w, f"sessions/{partner.id}/state", {"state": "idle", **PROC, "launch_key_hash": DIGEST}
    )
    more = await _ask(client, w["tok"], {**body, "role": "ревьюер", **_me(partner.id)})
    assert (more["status"], more["auto"]) == ("pending", False)


# --- прежние заявки под выключателями (5.5, П6, П7) ---------------------------------------------------------


async def test_leave_and_rule_go_at_once_only_for_a_free_session(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)

    async def _leave(channel, ident, expect=201):
        r = await client.post(f"{API}/leaves", json={"channel_id": channel, **ident}, headers=_h(w["tok"]))
        assert r.status_code == expect, r.text
        return r.json()

    assert (await _leave(w["ch"], _me(sid)))["status"] == "pending"  # выключатель стоит
    await _put(ui, "global", "tg_leave", False)
    assert (await _leave(w["ch2"], _me(sid, None)))["status"] == "pending"  # (1) без ключа
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.leave_request"))
    left = await _leave(w["ch2"], _me(sid))
    assert (left["status"], left["auto"]) == ("approved", True)
    assert [a["hub_session_id"] for a in _audits(db, "leave_auto")] == [sid]
    assert _audits(db, "leave_request_approved") == []

    async def _rule(ident, expect=201, **extra):
        body = {"project_id": w["p3"], "account_id": w["acc"], "kind": "all", **ident, **extra}
        r = await client.post(f"{API}/rule-requests", json=body, headers=_h(w["tok"]))
        assert r.status_code == expect, r.text
        return r.json()

    loose = insert_channel(db, w["acc"], project_id=None, ext_id="loose", kind="group", title="Ничья")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET map_source = 'auto' WHERE id = ?", (loose,))
    )
    # (6) `map_rule` снят, а `move_channel` стоит — заявка ждёт владельца.
    await _put(ui, "global", "map_rule", False)
    pending = await _rule(_me(sid))
    assert pending["status"] == "pending" and "auto" not in pending
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.rule_request"))
    await _put(ui, "global", "move_channel", False)
    done = await _rule(_me(sid))
    assert (done["status"], done["auto"]) == ("approved", True) and done["rule_id"]
    (audit,) = _audits(db, "rule_request_auto")
    assert audit["hub_session_id"] == sid and audit["moved"] >= 1 and loose in audit["channels"]
    assert _audits(db, "rule_request_decide") == [] and _channel(db, loose)[0] == w["p3"]


async def test_join_is_accepted_after_the_peek_while_the_switch_is_still_off(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "tg_join", False)

    async def _join(target, ident):
        body = {"project_id": w["p3"], "target": target, **ident}
        r = await client.post(f"{API}/joins", json=body, headers=_h(w["tok"]))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def _peek(join_id):
        clock.advance_ms(10 * base.MIN)  # паузы между предпросмотрами держит хаб
        now = _now(clock)

        def _fn(conn):
            leased = join_mod.lease(conn, account_id=w["acc"], now_ms=now, limit=5)
            (item,) = [x for x in leased if x["id"] == join_id]
            peek = {"title": "Канал", "kind": "broadcast", "member": False, "ext_id": f"-100{join_id}"}
            return join_mod.report(
                conn, account_id=w["acc"], request_id=join_id, lease_gen=item["lease_gen"], outcome="done",
                now_ms=now, peek=peek,
            )  # fmt: skip

        return db.write_sync(_fn)

    free = await _join("@free_one", _me(sid))
    no_key = await _join("@no_key", _me(sid, None))
    late = await _join("@late_one", _me(sid))
    with db.reader() as conn:
        marks = [join_mod.get(conn, i).autopilot_session for i in (free, no_key, late)]
    assert marks == [sid, None, sid]
    assert _peek(free) == "approved" and _peek(no_key) == "pending"
    assert [a["hub_session_id"] for a in _audits(db, "join_auto")] == [sid]
    await _put(ui, "global", "tg_join", True)  # выключатель вернули до предпросмотра
    assert _peek(late) == "pending"
    with db.reader() as conn:
        assert [join_mod.get(conn, i).auto for i in (free, no_key, late)] == [1, 0, 0]


# --- правки по ревью 41-5 -----------------------------------------------------------------------------------


async def test_auto_session_without_confirmed_key_does_not_move_the_usual_way(client, ui, db, clock, world):  # noqa: F811
    """Ревью 41-5 (важное 2): карточку `hub_move_channel` хост у сессии в авто снял — тройка `hub:N` такой
    сессии без подтверждённого ключа (не прислан, чужой, хеш стёрт сном) обычным путём не переносит."""
    w = world
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    sid = await _auto(client, ui, w, token=org)
    theirs = insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-chat", kind="group", title="Чужая")
    for ident in (_me(sid, None), _me(sid, "f" * 64)):
        refused = await _move(client, org, theirs, w["p3"], ident, expect=409)
        assert refused["detail"].startswith("move_channel:") and refused["request_hint"] is True
    assert _channel(db, theirs)[0] == w["p4"]
    # Ядро само стёрло хеш (сон): процесс жив и шлёт верный ключ — всё равно не обычным путём.
    await _host(client, w, f"sessions/{sid}/state", {"state": "asleep"})
    await _move(client, org, theirs, w["p3"], _me(sid), expect=409)
    assert _channel(db, theirs)[0] == w["p4"]
    # Прав тройка без ключа не даёт: при СНЯТОМ выключателе перенос тоже не проходит.
    await _put(ui, "global", "move_channel", False)
    await _move(client, org, theirs, w["p3"], _me(sid, None), expect=409)
    assert _channel(db, theirs)[0] == w["p4"]
    # Ручная сессия и запрос без тройки — как прежде, обычным путём.
    manual = await _auto(client, ui, w, role="тестер", mode="manual", token=org)
    await _move(client, org, theirs, w["p3"], _me(manual, None))
    assert _channel(db, theirs)[0] == w["p3"]
    await _move(client, org, theirs, w["p4"])
    assert _channel(db, theirs)[0] == w["p4"]
    # Закрытая сессия в авто тройкой уже ничего не ужесточает.
    await ui.post(f"{API}/sessions/{sid}/close")
    await _move(client, org, theirs, w["p3"], _me(sid, None))
    assert _channel(db, theirs)[0] == w["p3"]


async def test_move_request_is_not_taken_at_once_without_organize(client, ui, db, clock, world):  # noqa: F811
    """Ревью 41-5 (мелкое 3): раскладку меняет только токен со скоупом `organize` — заявка на перенос от
    сессии в авто с токеном без него ждёт владельца и при снятом выключателе."""
    w = world
    sid = await _auto(client, ui, w)  # токен мира: read, draft, attach
    await _put(ui, "global", "move_channel", False)
    theirs = insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-chat", kind="group", title="Чужая")
    body = {"kind": "move_channel", "project_id": w["p3"], "channel_id": theirs, **_me(sid)}
    row = await _ask(client, w["tok"], body)
    assert (row["status"], row["auto"]) == ("pending", False) and _channel(db, theirs)[0] == w["p4"]
    # С `organize` та же сессия проходит сразу.
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    other = insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-chat-2", kind="group", title="Ещё")
    row = await _ask(client, org, {**body, "channel_id": other}, expect=200)
    assert (row["status"], row["auto"]) == ("approved", True) and _channel(db, other)[0] == w["p3"]
