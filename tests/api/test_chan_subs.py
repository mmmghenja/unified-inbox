"""Ф14-5 (docs/architecture.md): подписка сессии на беседу — ядро. Тесты раздела E 1–5 и перечня
«Тесты добавить» П1–П19: кто может, одна живая на беседу, предел на разговор, сигнал из приёма (только новое
чужое входящее, id сообщений), ожидание одно на сессию (дебаунс 5 с, вытеснение, новая подписка во время
ожидания), пропуск только `answered` (П3), `moved`/`channel_gone` (П10, П13), смена разговора (П5, П17),
миграция беседы (П11, П18), добор 13-1 при снятии (П19), `GET /subs` поле `channels`, `/admin/subs`.

`/proc` подменяется (`subs.read_proc_start`) словарём «pid -> starttime».
"""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import chan_subs, security, subs
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.ingest import apply_batch

from .conftest import csrf_headers, insert_account, insert_channel, insert_project, login_via_code

ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
MIN_MS = 60_000


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )


def _h(token: str) -> dict:
    # Ф14-6b п.7: клиент ожидания нового протокола (`hub sub wait` шлёт этот заголовок).
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "2"}


def _session(*, pid=PID, start=START, sid=SID, full=True, kind="interactive", cwd="/home/owner/x"):
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update({"cwd": cwd, "kind": kind, "name": None, "name_source": None})
    return body


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    other = insert_project(db, name="Другой")
    personal = insert_project(db, name="Личное")  # без workdir
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="tg")
    cid = insert_channel(db, acc, project_id=pid, ext_id="c1", title="Клиент")
    cid2 = insert_channel(db, acc, project_id=pid, ext_id="c2")
    c_personal = insert_channel(db, acc, project_id=personal, ext_id="c3")
    c_unsorted = insert_channel(db, acc, project_id=None, ext_id="c4")
    c_other = insert_channel(db, acc, project_id=other, ext_id="c5")
    c_archived = insert_channel(db, acc, project_id=pid, ext_id="c6", archived=1)
    c_temp = insert_channel(db, acc, project_id=pid, ext_id="new:abc")
    tok = _token(db)
    triage = _token(db, name="agent:triage", kind="background", scopes=("read", "triage"))
    return {
        "pid": pid,
        "other": other,
        "personal": personal,
        "workdir": str(workdir),
        "acc": acc,
        "cid": cid,
        "cid2": cid2,
        "c_personal": c_personal,
        "c_unsorted": c_unsorted,
        "c_other": c_other,
        "c_archived": c_archived,
        "c_temp": c_temp,
        "tok": tok.token,
        "tok_id": tok.token_id,
        "triage": triage.token,
    }


def _now(clock) -> int:
    return int(clock() * 1000)


async def _sub(client, w, channel_id, *, token=None, **session_kw):
    return await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": channel_id, "session": _session(**session_kw)},
        headers=_h(token or w["tok"]),
    )


async def _wait(client, w, *, token=None, timeout_s=1, waiter="0123456789abcdef", **session_kw):
    return await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(full=False, **session_kw), "waiter": waiter, "timeout_s": timeout_s},
        headers=_h(token or w["tok"]),
    )


async def _end(client, w, *, channel_id=None, token=None, **session_kw):
    body = {"session": _session(full=False, **session_kw)}
    if channel_id is not None:
        body["channel_id"] = channel_id
    return await client.post("/api/v1/chan-subs/end", json=body, headers=_h(token or w["tok"]))


def _event(
    channel_ext, ext_id, ts, *, is_me=False, kind="text", etype="upsert", text="привет", edited_ts=None
):
    event = {
        "type": etype,
        "channel": {"ext_id": channel_ext, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": kind,
        "text": text,
        "mentions_me": False,
    }
    if edited_ts is not None:
        event["edited_ts"] = edited_ts
    return event


def _ingest(db, w, events, clock):
    now = _now(clock)
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now)
    )


def _msg_id(db, channel_id, ext_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.message WHERE channel_id = ? AND ext_id = ?", (channel_id, ext_id)
        ).fetchone()[0]


def _signals(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT chan_sub_id, project_sub_id, channel_id, first_message_id, last_message_id, msg_count, "
            "status, reason FROM main.session_chan_signal ORDER BY id"
        ).fetchall()


def _rec(db, sub_id):
    with db.reader() as conn:
        return chan_subs.get(conn, sub_id)


def _audit(db, action):
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, result, channel_id, detail_json FROM main.audit WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()


# --- подписка и кто может (E2) ---------------------------------------------------------------------------


async def test_subscribe_idempotent_count_and_audit(client, db, world, procs):
    r = await _sub(client, world, world["cid"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["count"] == 1
    sub = body["sub"]
    assert (sub["channel_id"], sub["project_id"], sub["session_pid"], sub["live"], sub["waiting"]) == (
        world["cid"],
        world["pid"],
        PID,
        True,
        False,
    )
    again = await _sub(client, world, world["cid"])
    assert again.json()["created"] is False and again.json()["sub"]["id"] == sub["id"]
    [(actor, result, channel_id, detail)] = _audit(db, "chan_sub_create")
    assert (actor, result, channel_id) == ("interactive", "ok", world["cid"])
    assert json.loads(detail) == {
        "sub_id": sub["id"],
        "channel_id": world["cid"],
        "project_id": world["pid"],
        "pid": PID,
        "session_id8": SID[:8],
    }


async def test_who_may_subscribe(client, db, world, procs, clock):
    assert (await _sub(client, world, world["cid"], pid=999)).status_code == 409  # мёртвая сессия
    assert (await _sub(client, world, world["cid"], start=START + 1)).status_code == 409
    assert (await _sub(client, world, world["cid"], kind="sdk-cli")).status_code == 403
    bg = _token(db, kind="background", scopes=("read", "draft"))
    assert (await _sub(client, world, world["cid"], token=bg.token)).status_code == 403
    wake = _token(db, name=f"mcp:wake-{world['pid']}", project_ids=[world["pid"]])
    assert (await _sub(client, world, world["cid"], token=wake.token)).status_code == 403
    runs = _token(db, name="mcp:x", scopes=("read", "draft", "runs"))
    assert (await _sub(client, world, world["cid"], token=runs.token)).status_code == 403
    orch = _token(db, name="mcp:orchestrator")
    assert (await _sub(client, world, world["cid"], token=orch.token)).status_code == 403
    no_draft = _token(db, scopes=("read",))
    assert (await _sub(client, world, world["cid"], token=no_draft.token)).status_code == 403
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": world["cid"], "session": _session()},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 403
    client.cookies.clear()
    # невидимая беседа проектному токену и несуществующая — 404
    scoped = _token(db, project_ids=[world["pid"]])
    assert (await _sub(client, world, world["c_other"], token=scoped.token)).status_code == 404
    assert (await _sub(client, world, world["c_unsorted"], token=scoped.token)).status_code == 404
    assert (await _sub(client, world, 10**9)).status_code == 404
    # архив — 422
    assert (await _sub(client, world, world["c_archived"])).status_code == 422
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.session_chan_sub").fetchone()[0] == 0
    # 24.10: беседа-заготовка (первое сообщение новому человеку) — можно
    assert (await _sub(client, world, world["c_temp"])).status_code == 200
    # «Неразобранное» и проект без workdir — можно; cwd и `subs_enabled` не применяются
    assert (await _sub(client, world, world["c_unsorted"])).status_code == 200
    assert (await _sub(client, world, world["c_personal"])).status_code == 200
    assert (await _sub(client, world, world["cid"], token=scoped.token)).status_code == 200


async def test_sixth_channel_refused(client, db, world, procs, monkeypatch):
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 5)  # Ф35-0: поведение предела — на малом числе
    acc = world["acc"]
    extra = [insert_channel(db, acc, project_id=world["pid"], ext_id=f"x{i}") for i in range(6)]
    for channel_id in extra[:5]:
        assert (await _sub(client, world, channel_id)).status_code == 200
    r = await _sub(client, world, extra[5])
    assert r.status_code == 409 and "5 бесед" in r.json()["detail"]
    # повтор своей — не шестая
    assert (await _sub(client, world, extra[0])).json()["created"] is False


async def test_one_live_sub_per_channel_other_session(client, db, world, procs):
    """Ф39 A0: подписка на беседу своя у каждой сессии — вторая сессия не получает 409 и первую не снимает
    (было: «беседу уже слушает сессия», мёртвого держателя заменяла `replaced`)."""
    first = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    procs[5000] = 1
    r = await _sub(client, world, world["cid"], pid=5000, start=1, sid="sess-other")
    assert r.status_code == 200 and r.json()["created"] is True
    second = r.json()["sub"]["id"]
    assert second != first and _rec(db, first).ended_ts is None
    # мёртвого держателя никто не «заменяет» — его снимет `cleanup`
    del procs[PID]
    r = await _sub(client, world, world["cid"], pid=5000, start=1, sid="sess-other")
    assert r.status_code == 200 and r.json()["created"] is False and r.json()["sub"]["id"] == second
    assert _rec(db, first).ended_ts is None
    ended = db.write_sync(lambda conn: chan_subs.cleanup(conn, now_ms=10))
    assert ended == [(first, "session_gone")] and _rec(db, second).ended_ts is None


async def test_project_and_channel_subs_together_p1(client, db, world, procs):
    """П1: у одной сессии подписка на проект и на беседы — можно (в обе стороны)."""
    r = await client.post(
        "/api/v1/subs",
        json={"session": _session(cwd=world["workdir"])},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    assert (await _sub(client, world, world["cid"])).status_code == 200
    # и наоборот: беседы уже слушает — на проект тоже можно (повтор своей)
    r = await client.post(
        "/api/v1/subs",
        json={"session": _session(cwd=world["workdir"])},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200 and r.json()["created"] is False


async def test_new_conversation_ends_all_old_p5(client, db, world, procs):
    a = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    b = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    r = await _sub(client, world, world["c_personal"], sid="sess-new")
    assert r.status_code == 200 and r.json()["count"] == 1
    assert (_rec(db, a).end_reason, _rec(db, b).end_reason) == (
        "conversation_changed",
        "conversation_changed",
    )


# --- приём (E3, П12, П19) --------------------------------------------------------------------------------


async def test_ingest_signal_accumulates_and_filters(client, db, world, procs, clock):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    m1 = _msg_id(db, world["cid"], "1")
    assert _signals(db) == [(sub_id, None, world["cid"], m1, m1, 1, "pending", None)]
    _ingest(db, world, [_event("c1", "2", now + 1), _event("c1", "3", now + 2)], clock)
    m3 = _msg_id(db, world["cid"], "3")
    assert _signals(db) == [(sub_id, None, world["cid"], m1, m3, 3, "pending", None)]
    # своё, правка, служебное, беседа без подписки — сигнала нет
    _ingest(
        db,
        world,
        [
            _event("c1", "4", now + 3, is_me=True),
            _event("c1", "2", now + 1, etype="edit", text="правка", edited_ts=now + 10),
            _event("c1", "5", now + 4, kind="event"),
            _event("c2", "1", now + 5),
        ],
        clock,
    )
    assert _signals(db) == [(sub_id, None, world["cid"], m1, m3, 3, "pending", None)]
    # сверка вставила старое: старее подписки больше 10 мин — нет; в пределах 10 мин — да (почта), но
    # «последнее» остаётся самым поздним по времени
    _ingest(db, world, [_event("c1", "0", now - 11 * MIN_MS)], clock)
    assert _signals(db)[0][5] == 3
    _ingest(db, world, [_event("c1", "00", now - 9 * MIN_MS)], clock)
    assert _signals(db)[0][4:6] == (m3, 4)


async def test_ingest_hidden_message_no_signal(client, db, world, procs, clock):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    now = _now(clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET hidden_before_ts = ?, hidden_before_sort = 0 WHERE id = ?",
            (now + MIN_MS, world["cid"]),
        )
    )
    _ingest(db, world, [_event("c1", "1", now)], clock)
    assert _signals(db) == []
    _ingest(db, world, [_event("c1", "2", now + 2 * MIN_MS)], clock)
    assert [s[0] for s in _signals(db)] == [sub_id]


# --- ожидание и выдача (E4, П3, П8) ----------------------------------------------------------------------


async def test_wait_both_channels_one_delivery_then_debounce(client, db, world, procs, clock):
    a = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    b = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now), _event("c1", "2", now), _event("c2", "1", now)], clock)
    r = await _wait(client, world)
    assert r.status_code == 200, r.text
    signals = r.json()["signals"]
    assert r.json() == {"signals": signals}
    assert sorted((s["sub_id"], s["channel_id"], s["msg_count"]) for s in signals) == [
        (a, world["cid"], 2),
        (b, world["cid2"], 1),
    ]
    for s in signals:
        # Ф20-1 D: плюс категория и родитель треда (только `ts` Slack или `null`)
        numbers = {"sub_id", "channel_id", "account_id", "msg_count", "message_id"}
        assert set(s) == numbers | {"cat", "thread_ext", "others"}  # Ф39 A3: сколько ещё сессий слушает
        assert all(isinstance(v, int) for k, v in s.items() if k not in ("cat", "thread_ext"))
        assert s["others"] == 0
        assert s["cat"] in ("dm", "mention", "thread", "other") and s["thread_ext"] is None
        assert s["account_id"] == world["acc"]
    assert {s["message_id"] for s in signals} == {
        _msg_id(db, world["cid"], "2"),
        _msg_id(db, world["cid2"], "1"),
    }
    [(actor, result, channel_id, detail), _] = _audit(db, "chan_sub_signal")
    assert (actor, result) == ("api", "delivered")
    assert set(json.loads(detail)) == {"sub_id", "account_id", "msg_count", "message_id", "cat"}  # Ф20-1
    # вторая выдача — не раньше 5 с после первой (часы ядра стоят)
    _ingest(db, world, [_event("c1", "3", now + 1)], clock)
    r = await _wait(client, world, timeout_s=1)
    assert r.status_code == 200 and r.json() == {"signals": []}
    clock.advance_ms(5000)
    r = await _wait(client, world, timeout_s=1)
    assert [s["channel_id"] for s in r.json()["signals"]] == [world["cid"]]


async def test_skip_answered_but_client_after_answer_delivered(client, db, world, procs, clock):
    await _sub(client, world, world["cid"])
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now), _event("c1", "2", now + 1, is_me=True)], clock)
    r = await _wait(client, world, timeout_s=1)
    assert r.json() == {"signals": []}
    assert [s[6:] for s in _signals(db)] == [("skipped", "answered")]
    # клиент дописал после ответа владельца — выдан
    _ingest(db, world, [_event("c1", "3", now + 2)], clock)
    r = await _wait(client, world, timeout_s=1)
    assert [s["msg_count"] for s in r.json()["signals"]] == [1]


async def test_edit_or_reaction_of_old_own_is_not_answer(client, db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now - 5 * MIN_MS, is_me=True)], clock)
    await _sub(client, world, world["cid"])
    _ingest(db, world, [_event("c1", "2", now)], clock)
    _ingest(
        db,
        world,
        [
            _event("c1", "1", now - 5 * MIN_MS, is_me=True, etype="edit", text="правка", edited_ts=now + 5),
            {
                "type": "reaction",
                "channel": {"ext_id": "c1", "kind": "dm"},
                "ext_id": "1",
                "reactions": {"x": 1},
            },
        ],
        clock,
    )
    r = await _wait(client, world, timeout_s=1)
    assert [s["msg_count"] for s in r.json()["signals"]] == [1]


async def test_read_and_open_channel_do_not_skip_p3(client, app, db, world, procs, clock):
    await _sub(client, world, world["cid"])
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET last_read_ts = ?, last_read_sort = 999, last_read_ext = '1' "
            "WHERE id = ?",
            (now + 1, world["cid"]),
        )
    )
    app.state.presence.set(
        token_id=1, client_id="ui", channel_id=world["cid"], thread_ext_id=None, now_ms=now
    )
    r = await _wait(client, world, timeout_s=1)
    assert [s["channel_id"] for s in r.json()["signals"]] == [world["cid"]]


async def test_new_sub_during_wait_is_picked_up_p8(client, db, world, procs, clock):
    await _sub(client, world, world["cid"])
    task = asyncio.create_task(_wait(client, world, timeout_s=5))
    await asyncio.sleep(0.2)
    await _sub(client, world, world["cid2"])
    _ingest(db, world, [_event("c2", "1", _now(clock))], clock)
    r = await asyncio.wait_for(task, 5)
    assert [s["channel_id"] for s in r.json()["signals"]] == [world["cid2"]]


async def test_new_wait_supersedes_previous(client, db, world, procs):
    await _sub(client, world, world["cid"])
    first = asyncio.create_task(_wait(client, world, timeout_s=5))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(_wait(client, world, timeout_s=1, waiter="fedcba9876543210"))
    r1 = await asyncio.wait_for(first, 5)
    assert r1.status_code == 409 and r1.json()["detail"] == "superseded"
    assert (await asyncio.wait_for(second, 5)).status_code == 200


async def test_moved_ends_sub_even_without_pending(client, db, world, procs):
    a = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    b = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (world["other"], world["cid"])
        )
    )
    r = await _wait(client, world, timeout_s=1)
    assert r.status_code == 200 and r.json() == {"signals": []}
    assert _rec(db, a).end_reason == "moved" and _rec(db, b).ended_ts is None
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = NULL WHERE id = ?", (world["cid2"],))
    )
    r = await _wait(client, world, timeout_s=1)
    assert r.status_code == 410 and r.json() == {"detail": {"end_reason": "moved"}}
    # дальше — 410 с последней причиной
    assert (await _wait(client, world)).json()["detail"]["end_reason"] == "moved"


async def test_moved_when_channel_no_longer_visible_to_token(client, db, world, procs):
    scoped = _token(db, project_ids=[world["pid"], world["other"]])
    sub_id = (await _sub(client, world, world["cid"], token=scoped.token)).json()["sub"]["id"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (world["personal"], world["cid"])
        )
    )
    r = await _wait(client, world, token=scoped.token)
    assert r.status_code == 410 and _rec(db, sub_id).end_reason == "moved"


async def test_unsorted_to_project_keeps_sub_p13(client, db, world, procs, clock):
    sub_id = (await _sub(client, world, world["c_unsorted"])).json()["sub"]["id"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (world["pid"], world["c_unsorted"])
        )
    )
    _ingest(db, world, [_event("c4", "1", _now(clock))], clock)
    r = await _wait(client, world, timeout_s=1)
    assert [s["channel_id"] for s in r.json()["signals"]] == [world["c_unsorted"]]
    rec = _rec(db, sub_id)
    assert rec.ended_ts is None and rec.project_id == world["pid"]


async def test_archived_is_channel_gone_hidden_is_not_p10(client, db, world, procs, clock):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    now = _now(clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET hidden_before_ts = ?, hidden_before_sort = 0 WHERE id = ?",
            (now, world["cid"]),
        )
    )
    _ingest(db, world, [_event("c1", "1", now + 1)], clock)  # новое входящее беседу возвращает
    r = await _wait(client, world, timeout_s=1)
    assert [s["channel_id"] for s in r.json()["signals"]] == [world["cid"]]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (world["cid"],))
    )
    r = await _wait(client, world, timeout_s=1)
    assert r.status_code == 410 and r.json()["detail"]["end_reason"] == "channel_gone"
    assert _rec(db, sub_id).end_reason == "channel_gone"


async def test_dead_session_ends_all_subs(client, db, world, procs):
    a = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    b = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    del procs[PID]
    r = await _wait(client, world)
    assert r.status_code == 410 and r.json()["detail"]["end_reason"] == "session_gone"
    assert {_rec(db, a).end_reason, _rec(db, b).end_reason} == {"session_gone"}


async def test_stale_wait_and_end_do_not_touch_new_conversation_p17(client, db, world, procs):
    await _sub(client, world, world["cid"], sid="sess-old")
    a = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    b = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    r = await _wait(client, world, sid="sess-old")
    assert r.status_code == 410 and r.json()["detail"]["end_reason"] == "conversation_changed"
    r = await _end(client, world, sid="sess-old")
    assert r.status_code == 410 and r.json()["detail"]["end_reason"] == "conversation_changed"
    r = await _end(client, world, sid="sess-old", channel_id=world["cid"])
    assert r.status_code == 410
    assert _rec(db, a).ended_ts is None and _rec(db, b).ended_ts is None


async def test_wait_without_any_subs_404_and_ui_403(client, db, world, procs, clock):
    assert (await _wait(client, world)).status_code == 404
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 403


async def test_revoked_token_wakes_waiter_with_410(client, db, world, procs):
    await _sub(client, world, world["cid"])
    task = asyncio.create_task(_wait(client, world, timeout_s=10))
    await asyncio.sleep(0.2)
    db.write_sync(lambda conn: security.revoke_api_token(conn, world["tok_id"]))
    assert db.write_sync(lambda conn: chan_subs.cleanup(conn, now_ms=1)) == [(1, "token_revoked")]
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == 410 and r.json()["detail"]["end_reason"] == "token_revoked"


async def test_cleanup_dead_session(db, world, procs, client):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    assert db.write_sync(lambda conn: chan_subs.cleanup(conn, now_ms=1)) == []
    del procs[PID]
    assert db.write_sync(lambda conn: chan_subs.cleanup(conn, now_ms=1)) == [(sub_id, "session_gone")]


# --- снятие -----------------------------------------------------------------------------------------------


async def test_end_one_and_all_and_foreign(client, db, world, procs, clock):
    a = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    b = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    _ingest(db, world, [_event("c1", "1", _now(clock))], clock)
    procs[5000] = 1
    # Ф39 A3: чужие подписки беседы сессии не касаются — своей нет: 404 (было 403), подписка цела
    r = await _end(client, world, channel_id=world["cid"], pid=5000, start=1)
    assert r.status_code == 404
    other_tok = _token(db, name="mcp:other")
    assert (await _end(client, world, channel_id=world["cid"], token=other_tok.token)).status_code == 404
    assert _rec(db, a).ended_ts is None
    r = await _end(client, world, channel_id=world["cid"])
    assert r.status_code == 200 and [row["id"] for row in r.json()["items"]] == [a]
    assert r.json()["items"][0]["end_reason"] == "unsubscribed"
    assert [s[6] for s in _signals(db)] == ["ended"]
    # повтор — 200 с той же строкой
    r = await _end(client, world, channel_id=world["cid"])
    assert r.status_code == 200 and r.json()["items"][0]["id"] == a
    assert (await _end(client, world, channel_id=world["c_other"])).status_code == 404
    r = await _end(client, world)
    assert [row["id"] for row in r.json()["items"]] == [b]
    assert _rec(db, b).end_reason == "unsubscribed"
    [(_actor, result, _cid, detail), _] = _audit(db, "chan_sub_end")
    assert result == "unsubscribed" and json.loads(detail) == {"sub_id": a, "channel_id": world["cid"]}


# --- GET /subs, /admin/subs (E5) -------------------------------------------------------------------------


async def test_get_subs_channels_scoped_by_token(client, db, world, procs):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    await _sub(client, world, world["c_personal"])
    body = (await client.get("/api/v1/subs?live=1", headers=_h(world["tok"]))).json()
    assert body["items"] == []
    assert {row["channel_id"] for row in body["channels"]} == {world["cid"], world["c_personal"]}
    row = next(r for r in body["channels"] if r["id"] == sub_id)
    assert set(row) == {
        "machine",  # Ф77-1
        "id",
        "channel_id",
        "project_id",
        "session_name",
        "session_pid",
        "created_ts",
        "last_wait_ts",
        "last_delivery_ts",
        "live",
        "listening",
        "waiting",
        "ended_ts",
        "end_reason",
        "pending",
        "skey",  # Ф26-1 П4: ключ сессии
        "keywords",  # Ф31-1 решение 4: ключи подписки на беседу
        "batch_min",  # Ф67-1: действующий интервал пачки
        "role",  # Ф39 A2 п.9: роль сессии подписки
        "role_no",
    }
    assert row["role"] is None and row["role_no"] is None
    scoped = _token(db, project_ids=[world["pid"]], scopes=("read",))
    body = (await client.get("/api/v1/subs", headers=_h(scoped.token))).json()
    assert [row["channel_id"] for row in body["channels"]] == [world["cid"]]


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


async def test_admin_subs_waiting_and_end(client, admin_client, db, world, procs):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    task = asyncio.create_task(_wait(client, world, timeout_s=3))
    await asyncio.sleep(0.2)
    body = (await admin_client.get("/internal/v1/admin/subs", headers=_admin())).json()
    [row] = body["channels"]
    assert row["waiting"] is True and row["session_id8"] == SID[:8]
    public = (await client.get("/api/v1/subs", headers=_h(world["tok"]))).json()["channels"]
    assert public[0]["waiting"] is True
    await asyncio.wait_for(task, 5)
    [row] = (await admin_client.get("/internal/v1/admin/subs", headers=_admin())).json()["channels"]
    assert row["waiting"] is False and row["listening"] is True
    r = await admin_client.post(f"/internal/v1/admin/chan-subs/{sub_id}/end", headers=_admin())
    assert r.status_code == 200 and r.json()["sub"]["end_reason"] == "ended_by_owner"
    assert (await admin_client.get("/internal/v1/admin/subs", headers=_admin())).json()["channels"] == []
    assert (
        await admin_client.post("/internal/v1/admin/chan-subs/999/end", headers=_admin())
    ).status_code == 404


# --- подписка на проект рядом (П1, П19) ------------------------------------------------------------------


async def _project_sub(client, w):
    r = await client.post("/api/v1/subs", json={"session": _session(cwd=w["workdir"])}, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def _card(client, w, channel_id, message_ids):
    r = await client.post(
        "/api/v1/agent/items",
        json={
            "channel_id": channel_id,
            "message_ids": message_ids,
            "label": "reply",
            "summary": "сводка",
            "action": None,
            "provider": "test",
            "model": "m",
        },
        headers=_h(w["triage"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _card_signals(db):
    with db.reader() as conn:
        return conn.execute("SELECT sub_id, item_id, status FROM main.session_signal ORDER BY id").fetchall()


async def test_card_signal_not_for_channel_under_chan_sub_p1(client, db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now), _event("c2", "1", now)], clock)
    m1, m2 = _msg_id(db, world["cid"], "1"), _msg_id(db, world["cid2"], "1")
    # Ф26-1: беседу слушает ТА ЖЕ сессия, что подписывается на проект, — добор 13-1 её пропускает
    await _sub(client, world, world["cid"])
    await _card(client, world, world["cid"], [m1])
    project = await _project_sub(client, world)
    assert project["backfilled"] == 0
    neighbour = await _card(client, world, world["cid2"], [m2])
    await _card(client, world, world["cid"], [m1])  # дополнение карточки подписанной беседы
    # сигнал карточки — только по соседней беседе
    assert _card_signals(db) == [(project["sub"]["id"], neighbour, "pending")]


async def test_ending_chan_sub_backfills_project_sub_p19(client, db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    m1 = _msg_id(db, world["cid"], "1")
    # Ф26-1: подписка на беседу и на проект — одной сессии
    chan_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    project = await _project_sub(client, world)
    item = await _card(client, world, world["cid"], [m1])
    assert _card_signals(db) == []
    db.write_sync(lambda conn: chan_subs.end(conn, chan_id, "unsubscribed", now_ms=now, actor="core"))
    assert _card_signals(db) == [(project["sub"]["id"], item, "pending")]


async def test_f26_chan_sub_of_other_session_does_not_silence_project_sub(client, db, world, procs, clock):
    """Ф26-1 (раздел 0, п.4; C6): подписка на беседу сессии A глушит только подписку на проект сессии A —
    подписка на проект сессии B по этой беседе получает сигнал карточки сразу; снятие подписки на беседу —
    добор только подписке A."""
    now = _now(clock)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    m1 = _msg_id(db, world["cid"], "1")
    procs[5000] = 1
    chan_a = (await _sub(client, world, world["cid"], pid=5000, start=1, sid="sess-a")).json()["sub"]["id"]
    sub_a = (
        await client.post(
            "/api/v1/subs",
            json={"session": _session(pid=5000, start=1, sid="sess-a", cwd=world["workdir"])},
            headers=_h(world["tok"]),
        )
    ).json()["sub"]["id"]
    sub_b = (await _project_sub(client, world))["sub"]["id"]
    item = await _card(client, world, world["cid"], [m1])
    assert _card_signals(db) == [(sub_b, item, "pending")]
    with db.reader() as conn:
        rec_a, rec_b = subs.get_sub(conn, sub_a), subs.get_sub(conn, sub_b)
        skip = subs._skip_reason  # noqa: SLF001
        assert skip(conn, item_id=item, sub=rec_a, visible_projects=None, presence=[]) == "chan_sub"
        assert skip(conn, item_id=item, sub=rec_b, visible_projects=None, presence=[]) is None
    db.write_sync(lambda conn: chan_subs.end(conn, chan_a, "unsubscribed", now_ms=now, actor="core"))
    assert sorted(_card_signals(db)) == sorted([(sub_b, item, "pending"), (sub_a, item, "pending")])


# --- миграция беседы (П11, П18) --------------------------------------------------------------------------


def _migrate(db, w, clock, old_ext, new_ext):
    return _ingest(
        db,
        w,
        [{"type": "channel_migrate", "old_ext_id": old_ext, "channel": {"ext_id": new_ext, "kind": "group"}}],
        clock,
    )


async def test_channel_migrate_moves_sub_and_pending_p11(client, db, world, procs, clock):
    sub_id = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    _ingest(db, world, [_event("c1", "1", _now(clock))], clock)
    result = _migrate(db, world, clock, "c1", "super1")
    assert result.dead == 0
    with db.reader() as conn:
        new_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = 'super1'").fetchone()[0]
    rec = _rec(db, sub_id)
    assert rec.ended_ts is None and rec.channel_id == new_id
    assert [(s[2], s[6]) for s in _signals(db)] == [(new_id, "pending")]
    # ответ владельца в НОВОЙ беседе снимает сигнал (`ANSWERED_SQL` по беседе сигнала)
    _ingest(db, world, [_event("super1", "2", _now(clock) + 1, is_me=True)], clock)
    r = await _wait(client, world, timeout_s=1)
    assert r.json() == {"signals": []}
    assert [s[6:] for s in _signals(db)] == [("skipped", "answered")]


async def test_channel_migrate_both_subscribed_p18(client, db, world, procs, clock):
    old_sub = (await _sub(client, world, world["cid"])).json()["sub"]["id"]
    new_sub = (await _sub(client, world, world["cid2"])).json()["sub"]["id"]
    result = _migrate(db, world, clock, "c1", "c2")
    assert result.dead == 0 and result.applied == 1
    with db.reader() as conn:
        assert conn.execute(
            "SELECT channel_id FROM main.channel_alias WHERE old_ext_id = 'c1'"
        ).fetchone() == (world["cid2"],)
        assert conn.execute("SELECT COUNT(*) FROM main.dead_letter").fetchone()[0] == 0
    assert _rec(db, old_sub).end_reason == "channel_gone"
    assert _rec(db, new_sub).ended_ts is None


# --- Ф35-0 (план 11.40): предел 20 бесед на разговор ----------------------------------------------------


async def test_f35_twenty_channels_one_delivery(client, db, world, procs, clock, monkeypatch):
    """Двадцать подписок одной сессии: 21-я — отказ, а входящее в каждую из двадцати уходит ОДНОЙ выдачей
    (на подписку один `pending`, предел выдачи — 20) и ничего не остаётся ждать. С 16.10 предел — 500 (слово
    владельца), поведение предела проверяется на 20."""
    assert chan_subs.MAX_CHAN_SUBS == 500 and chan_subs.MAX_SIGNALS_PER_DELIVERY == 20
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 20)
    acc = world["acc"]
    extra = [insert_channel(db, acc, project_id=world["pid"], ext_id=f"z{i}") for i in range(21)]
    for channel_id in extra[:20]:
        assert (await _sub(client, world, channel_id)).status_code == 200
    r = await _sub(client, world, extra[20])
    assert r.status_code == 409 and "20 бесед" in r.json()["detail"]
    now = _now(clock)
    _ingest(db, world, [_event(f"z{i}", "1", now) for i in range(20)], clock)
    r = await _wait(client, world)
    assert r.status_code == 200, r.text
    signals = r.json()["signals"]
    assert sorted(s["channel_id"] for s in signals) == sorted(extra[:20])
    assert all(s["msg_count"] == 1 for s in signals)
    assert [row[6] for row in _signals(db)] == ["delivered"] * 20
