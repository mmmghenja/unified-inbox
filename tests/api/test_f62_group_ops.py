"""Ф62-6 (docs/architecture.md, E2–E6 и поправки П1–П16): операции над группами WhatsApp — ядро
и ручки.

- заявка сессии: `create`/`add`/`remove`/`link_reset` — карточка владельцу (`pending`), `link` — сразу; отказы на
  входе (дежурная, чужой проект, не WhatsApp, длина названия, участник `@имя`, беседа без проекта);
- решение — только кука (П8), «Отказать» до первой выдачи; заявка владельца из интерфейса — сразу;
- очередь коннектору: по одной на аккаунт, темп, отсрочка, бэкофф до `gave_up`, `drop`, чужой/устаревший лиз;
- итог: коды участников, сырые номера стёрты (П9); новая группа — беседа в проект заявки, НЕ заглушена (П12),
  при любом порядке отчёта и приёма;
- исход сигналом автору — один раз, только клиенту протокола 9;
- выключатель автопилота `wa_group`, канал владельца (агент отвечает за владельца), `/sync` и кадр SSE без текстов.

Мир и помощники — из test_hub_sessions.py, test_f41_autopilot_5b.py и test_f56_queue.py."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth, feed, queries, security
from hub.core import group_ops as go
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from . import test_hub_sessions as base
from .conftest import insert_channel
from .test_f41_autopilot import _put
from .test_f41_autopilot_5b import _auto, _me
from .test_f54_human_requests import _wait
from .test_f56_queue import _agent, _answer, _channel_on, _queue, _refs
from .test_hub_sessions import ROLE, A, B, _audits, _h, _now, _sess, _sub, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
GROUP = "120363000000000001@g.us"
NEW_GROUP = "120363000000000777@g.us"
LINK = "https://chat.whatsapp.com/AbCdEfGh12345678"
PHONE = "+7 900 000-11-33"
RAW = "79000001133"


@pytest.fixture
def internal_app(db, clock):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _wa_account(db, source="whatsapp") -> tuple[int, dict]:
    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, 'main')", (source,)).lastrowid
        )
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def wa(db, world):
    """Аккаунт WhatsApp с секретом коннектора, группа и личка в проекте p3."""
    acc, h = _wa_account(db)
    group = insert_channel(db, acc, project_id=world["p3"], ext_id=GROUP, kind="group", title="hub test")
    dm = insert_channel(
        db, acc, project_id=world["p3"], ext_id="79000001122@s.whatsapp.net", kind="dm", title="Пётр"
    )
    loose = insert_channel(db, acc, project_id=None, ext_id="120363000000000002@g.us", kind="group")
    return {"acc": acc, "h": h, "group": group, "dm": dm, "loose": loose}


async def _op(client, w, body, *, who=A, token=None, expect=201, session=True) -> dict:
    if session:
        body = {"session": _sess(who), **body}
    r = await client.post(f"{API}/group-ops", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


def _create_body(w, **extra) -> dict:
    return {"kind": "create", "project_id": w["p3"], "title": "hub test 2", "participants": [PHONE], **extra}


async def _lease(ic, wa) -> list[dict]:
    r = await ic.post(f"{INTERNAL}/group-op/lease", headers=wa["h"], json={})
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, wa, job, outcome="done", *, expect=200, **extra) -> dict:
    body = {"id": job["id"], "lease_gen": job["lease_gen"], "outcome": outcome, **extra}
    r = await ic.post(f"{INTERNAL}/group-op/report", headers=wa["h"], json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _row(db, op_id) -> go.Op:
    with db.reader() as conn:
        op = go.get(conn, op_id)
    assert op is not None
    return op


def _channel_count(db, ext_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def _chan(db, ext_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, project_id, map_source, muted, map_reason FROM main.channel WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()


async def _group_event(ic, wa, ext_id, clock):
    event = {
        "type": "chat_info",
        "channel": {"ext_id": ext_id, "kind": "group", "title": "hub test 2"},
    }
    r = await ic.post(f"{INTERNAL}/events", headers=wa["h"], json={"events": [event]})
    if r.status_code != 200 or r.json().get("dead"):
        # Беседа появляется и первым сообщением — как у приёма живой группы.
        msg = {
            "type": "upsert",
            "channel": {"ext_id": ext_id, "kind": "group", "title": "hub test 2"},
            "ext_id": "m1",
            "ts": _now(clock),
            "is_me": True,
            "kind": "text",
            "text": "группа создана",
            "mentions_me": False,
            "sender": {"ext_id": "me", "name": "я", "handle": None, "is_me": True},
        }
        r = await ic.post(f"{INTERNAL}/events", headers=wa["h"], json={"events": [msg]})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


# --- E3: заявка сессии --------------------------------------------------------------------------------------


async def test_session_card_kinds_pending_and_link_at_once(client, ui, db, clock, world, wa):
    w = world
    made = await _op(
        client, w, _create_body(w, participants=[PHONE, f"#{wa['dm']}", "+79000001122", "+" + RAW])
    )
    assert (made["status"], made["origin"], made["kind"], made["can_decline"]) == (
        "pending",
        "session",
        "create",
        True,
    )
    # Дубли схлопнуты (личка и тот же номер, номер в двух записях); номера наружу — только маской.
    assert [(p["kind"], p["mask"], p["name"]) for p in made["participants"]] == [
        ("phone", "+7 ••• ••• 11 33", None),
        ("chat", None, "Пётр"),
    ]
    assert RAW not in json.dumps(made) and "0001122" not in json.dumps(made)
    add = await _op(client, w, {"kind": "add", "channel_id": wa["group"], "participants": [PHONE]})
    remove = await _op(
        client, w, {"kind": "remove", "channel_id": wa["group"], "participants": [f"#{wa['dm']}"]}
    )
    reset = await _op(client, w, {"kind": "link_reset", "channel_id": wa["group"]})
    assert {x["status"] for x in (add, remove, reset)} == {"pending"}
    link = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    assert (link["status"], link["can_decline"]) == ("approved", True)  # чтение — без карточки (П5)
    # Ровно 25 символов — можно; 26 — отказ (П4).
    assert (await _op(client, w, _create_body(w, title="hub test " + "x" * 16)))[
        "title"
    ] == "hub test " + "x" * 16
    r = await _op(client, w, _create_body(w, title="hub test " + "x" * 17), expect=422)
    assert "25" in r["detail"]


async def test_session_refusals(client, ui, db, clock, world, wa):
    w = world
    bg, _ = _token(db, name="mcp:wake-3", scopes=("read", "draft"))
    await _op(client, w, _create_body(w), token=bg, expect=403)
    scoped, _ = _token(db, name="mcp:initech", project_ids=[w["p4"]])
    await _op(client, w, _create_body(w), token=scoped, expect=404)
    await _op(client, w, {"kind": "link", "channel_id": wa["group"]}, token=scoped, expect=404)
    # Беседа Telegram — не группа WhatsApp; беседа без проекта — только владелец.
    r = await _op(client, w, {"kind": "add", "channel_id": w["ch"], "participants": [PHONE]}, expect=422)
    assert r["detail"] == "это не группа WhatsApp"
    r = await _op(client, w, {"kind": "link", "channel_id": wa["loose"]}, expect=422)
    assert "без проекта" in r["detail"]
    # Участник `@имя` — не WhatsApp; личка Telegram — не личка этого аккаунта.
    r = await _op(client, w, _create_body(w, participants=["@durov"]), expect=422)
    assert r["detail"].startswith("participants[0]")
    await _op(client, w, _create_body(w, participants=[w["ch"]]), expect=422)
    # Аккаунт Telegram явно — отказ; «Неразобранное» сессии не дано; пусто — отказ.
    r = await _op(client, w, _create_body(w, account_id=w["acc"]), expect=422)
    assert "WhatsApp" in r["detail"]
    await _op(client, w, _create_body(w, project_id=None), expect=422)
    await _op(client, w, _create_body(w, participants=[]), expect=422)
    await _op(client, w, {"kind": "link", "channel_id": wa["group"], "participants": [PHONE]}, expect=422)


async def test_second_wa_account_needs_account_id(client, ui, db, clock, world, wa):
    w = world
    _wa_account(db)
    r = await _op(client, w, _create_body(w), expect=422)
    assert "несколько" in r["detail"]
    assert (await _op(client, w, _create_body(w, account_id=wa["acc"])))["account_id"] == wa["acc"]


# --- E3: решение владельца и заявка из интерфейса ----------------------------------------------------------------


async def test_decide_cookie_only_and_decline_until_leased(client, ui, db, clock, world, wa, ic):
    w = world
    made = await _op(client, w, _create_body(w))
    r = await client.post(f"{API}/group-ops/{made['id']}/approve", headers=_h(w["tok"]))
    assert r.status_code == 403
    r = await ui.post(f"{API}/group-ops/{made['id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = await ui.post(f"{API}/group-ops/{made['id']}/approve")
    assert r.status_code == 409
    [job] = await _lease(ic, wa)
    r = await ui.post(f"{API}/group-ops/{made['id']}/decline")
    assert r.status_code == 409 and r.json()["detail"] == "уже выполняю — отказать нельзя"
    other = await _op(client, w, {"kind": "add", "channel_id": wa["group"], "participants": [PHONE]})
    r = await ui.post(f"{API}/group-ops/{other['id']}/decline")
    assert r.status_code == 200 and r.json()["status"] == "declined"
    assert _row(db, other["id"]).participants_json is not None
    assert RAW not in (_row(db, other["id"]).participants_json or "")  # П9: сырой номер стёрт при отказе
    assert job["kind"] == "create"


async def test_ui_create_without_project_goes_at_once(client, ui, db, clock, world, wa):
    w = world
    r = await ui.post(
        f"{API}/group-ops/ui", {"kind": "create", "title": "hub test 3", "participants": [PHONE]}
    )
    assert r.status_code == 201, r.text
    assert (r.json()["status"], r.json()["origin"], r.json()["project_id"]) == ("approved", "ui", None)
    r = await client.post(
        f"{API}/group-ops/ui",
        json={"kind": "link", "channel_id": wa["loose"]},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 403
    r = await ui.post(f"{API}/group-ops/ui", {"kind": "link", "channel_id": wa["loose"]})
    assert r.status_code == 201 and r.json()["status"] == "approved"


# --- E4: очередь коннектору ---------------------------------------------------------------------------------


async def test_create_report_erases_numbers_and_binds_new_channel_unmuted(
    client, ui, db, clock, world, wa, ic
):
    w = world
    made = await _op(client, w, _create_body(w, participants=[PHONE, f"#{wa['dm']}"]))
    await ui.post(f"{API}/group-ops/{made['id']}/approve")
    [job] = await _lease(ic, wa)
    assert job["title"] == "hub test 2" and job["kind"] == "create" and job["first_leased_ts"] == _now(clock)
    assert job["participants"] == [
        {"i": 0, "jid": None, "phone": "+" + RAW},
        {"i": 1, "jid": "79000001122@s.whatsapp.net"},
    ]
    # Итог без jid группы — 422; устаревший лиз — 409; чужой аккаунт — 404.
    await _report(ic, wa, job, expect=422, result={"participants": []})
    await _report(ic, wa, {**job, "lease_gen": job["lease_gen"] + 1}, expect=409, result={"chat": NEW_GROUP})
    _other, other_h = _wa_account(db)
    r = await ic.post(
        f"{INTERNAL}/group-op/report",
        headers=other_h,
        json={
            "id": job["id"],
            "lease_gen": job["lease_gen"],
            "outcome": "done",
            "result": {"chat": NEW_GROUP},
        },
    )
    assert r.status_code == 404
    result = {
        "chat": NEW_GROUP,
        "title": "hub test 2 (мост)",
        "participants": [
            {"i": 0, "code": "privacy", "invite": True, "jid": RAW + "@s.whatsapp.net"},
            {"i": 1, "code": "ok"},
            {"i": 7, "code": "ok"},
            {"i": 1, "code": "мусор"},
        ],
    }
    assert await _report(ic, wa, job, result=result) == {"ok": True, "result": "done"}
    op = _row(db, made["id"])
    # Беседа группы есть сразу по отчёту, до первого сообщения (WhatsApp в новую группу ничего не шлёт).
    created_chan = _chan(db, NEW_GROUP)
    assert created_chan is not None
    assert (op.status, op.ext_id, op.channel_id) == ("done", NEW_GROUP, created_chan[0])
    with db.reader() as conn:
        title, kind = conn.execute(
            "SELECT title, kind FROM main.channel WHERE id = ?", (created_chan[0],)
        ).fetchone()
    assert (title, kind) == ("hub test 2 (мост)", "group")  # название — из ответа моста
    assert "+" + RAW not in (op.participants_json or "") and '"input": null' in (op.participants_json or "")
    out = go.op_dict(op)
    assert [(p["code"], p.get("invite")) for p in out["participants"]] == [("privacy", True), ("ok", None)]
    # Приём ПОСЛЕ отчёта (`chat_info`, первое сообщение) — та же беседа; в проекте заявки, не заглушена (П12).
    await _group_event(ic, wa, NEW_GROUP, clock)
    chan_id, project_id, source, muted, reason = _chan(db, NEW_GROUP)
    assert chan_id == created_chan[0] and _channel_count(db, NEW_GROUP) == 1
    # Повторный отчёт — 409, вторая беседа не появляется.
    await _report(ic, wa, job, expect=409, result={"chat": NEW_GROUP})
    assert _channel_count(db, NEW_GROUP) == 1
    assert (project_id, source, muted) == (w["p3"], "manual", 0)
    assert reason == f"группа создана по заявке #{made['id']}"
    assert _row(db, made["id"]).channel_id == chan_id


async def test_create_channel_before_report_is_bound_too(client, ui, db, clock, world, wa, ic):
    w = world
    made = await _op(client, w, _create_body(w))
    await ui.post(f"{API}/group-ops/{made['id']}/approve")
    [job] = await _lease(ic, wa)
    await _group_event(ic, wa, NEW_GROUP, clock)  # `chat_info` моста пришёл раньше отчёта
    chan_id, project_id, *_ = _chan(db, NEW_GROUP)
    assert project_id != w["p3"]
    await _report(ic, wa, job, result={"chat": NEW_GROUP, "participants": [{"i": 0, "code": "ok"}]})
    _id, project_id, source, muted, _reason = _chan(db, NEW_GROUP)
    assert (project_id, source, muted) == (w["p3"], "manual", 0)
    assert _row(db, made["id"]).channel_id == chan_id and _channel_count(db, NEW_GROUP) == 1


async def test_ui_create_without_project_channel_by_rules(client, ui, db, clock, world, wa, ic):
    """Заявка владельца без проекта: беседа создаётся отчётом и ложится по раскладке (правил нет —
    «Неразобранное»), не заглушена; `channel_id` — в заявке."""
    r = await ui.post(
        f"{API}/group-ops/ui", {"kind": "create", "title": "hub test 4", "participants": [PHONE]}
    )
    assert r.status_code == 201, r.text
    made = r.json()
    [job] = await _lease(ic, wa)
    await _report(ic, wa, job, result={"chat": NEW_GROUP, "participants": [{"i": 0, "code": "ok"}]})
    chan_id, project_id, _source, muted, _reason = _chan(db, NEW_GROUP)
    assert (project_id, muted) == (None, 0)
    assert _row(db, made["id"]).channel_id == chan_id


async def test_queue_one_at_a_time_link_pace_and_gap(client, ui, db, clock, world, wa, ic):
    w = world
    first = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    second = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    [job] = await _lease(ic, wa)
    assert job["id"] == first["id"] and job["participants"] == [] and job["ext_id"] == GROUP
    assert await _lease(ic, wa) == []  # первая не отчиталась — следующая не выдаётся
    await _report(ic, wa, job, result={"link": LINK})
    assert _row(db, first["id"]).status == "done" and go.op_dict(_row(db, first["id"]))["link"] == LINK
    assert await _lease(ic, wa) == []  # темп ссылок — 10 с
    clock.advance_ms(go.LINK_GAP_MS)
    [job2] = await _lease(ic, wa)
    assert job2["id"] == second["id"]
    # Ссылка не по образцу — 422.
    await _report(ic, wa, job2, expect=422, result={"link": "https://evil.example/AbCdEfGh12345678"})
    # Изменение после ссылки ждёт минуту от прошлого изменения и 10 с от ссылки.
    await _report(ic, wa, job2, result={"link": LINK})
    made = await _op(client, w, {"kind": "add", "channel_id": wa["group"], "participants": [PHONE]})
    await ui.post(f"{API}/group-ops/{made['id']}/approve")
    assert await _lease(ic, wa) == []
    clock.advance_ms(go.LINK_GAP_MS)
    assert [j["id"] for j in await _lease(ic, wa)] == [made["id"]]


async def test_retry_postpone_backoff_gave_up_and_drop(client, ui, db, clock, world, wa, ic):
    w = world
    made = await _op(client, w, {"kind": "add", "channel_id": wa["group"], "participants": [PHONE]})
    await ui.post(f"{API}/group-ops/{made['id']}/approve")
    [job] = await _lease(ic, wa)
    # Предел поиска номеров (П9) — отсрочка этой строки, не попытка.
    assert (await _report(ic, wa, job, "retry", postpone_ms=3_600_000))["result"] == "paused"
    assert _row(db, made["id"]).attempts == 0
    clock.advance_ms(3_600_000 - 1)
    assert await _lease(ic, wa) == []
    clock.advance_ms(1)
    for attempt in range(1, go.MAX_ATTEMPTS):
        [job] = await _lease(ic, wa)
        assert (await _report(ic, wa, job, "retry"))["result"] == "paused"
        assert _row(db, made["id"]).attempts == attempt
        clock.advance_ms(go.backoff_ms(attempt))
    [job] = await _lease(ic, wa)
    assert (await _report(ic, wa, job, "retry"))["result"] == "failed"
    op = _row(db, made["id"])
    assert (op.status, op.error) == ("failed", "gave_up") and "+" + RAW not in (op.participants_json or "")
    # `drop` — отказ с причиной; закрытая строка — 409.
    reset = await _op(client, w, {"kind": "link_reset", "channel_id": wa["group"]})
    await ui.post(f"{API}/group-ops/{reset['id']}/approve")
    clock.advance_ms(go.GAP_MS)
    [job] = await _lease(ic, wa)
    assert (await _report(ic, wa, job, "drop", reason="not_admin"))["result"] == "failed"
    assert _row(db, reset["id"]).error == "not_admin"
    await _report(ic, wa, job, "drop", expect=409)


async def test_floodwait_pauses_whole_account(client, ui, db, clock, world, wa, ic):
    w = world
    first = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    second = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    [job] = await _lease(ic, wa)
    await _report(ic, wa, job, "retry", retry_after_ms=600_000)
    clock.advance_ms(599_000)
    assert await _lease(ic, wa) == []
    third = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    assert _row(db, third["id"]).not_before_ms >= _now(clock) + 1000 - 1
    clock.advance_ms(1000)
    assert [j["id"] for j in await _lease(ic, wa)] == [first["id"]]
    assert second["id"] != first["id"]


# --- E5: исход сигналом автору ------------------------------------------------------------------------------


async def test_outcome_signal_once_only_protocol_9(client, ui, db, clock, world, wa, ic):
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    link = await _op(client, w, {"kind": "link", "channel_id": wa["group"]})
    declined = await _op(client, w, {"kind": "add", "channel_id": wa["group"], "participants": [PHONE]})
    await ui.post(f"{API}/group-ops/{declined['id']}/decline")
    [job] = await _lease(ic, wa)
    await _report(ic, wa, job, result={"link": LINK})
    assert await _wait(client, w, sub_id, A, proto=8) == []  # клиент протокола 8 вида не знает — не сгорает
    assert await _wait(client, w, sub_id, A, proto=9) == [
        {"kind": "group_op", "op_id": link["id"], "op": "link", "outcome": "done", "channel_id": wa["group"],
         "link": LINK},
        {"kind": "group_op", "op_id": declined["id"], "op": "add", "outcome": "declined",
         "channel_id": wa["group"]},
    ]  # fmt: skip
    assert await _wait(client, w, sub_id, A, proto=9) == []
    delivered = [a for a in _audits(db, "sub_signal") if a.get("kind") == "group_op"]
    assert len(delivered) == 2 and RAW not in json.dumps(delivered)
    # Заявка владельца из интерфейса сигнала никому не даёт; чужая сессия своих исходов не видит в списке.
    r = await ui.post(f"{API}/group-ops/ui", {"kind": "link", "channel_id": wa["group"]})
    assert r.status_code == 201
    other = await _op(client, w, {"kind": "link", "channel_id": wa["group"]}, who=B)
    me = {"pid": A[0], "proc_start": A[1], "session_id": A[2]}
    r = await client.get(f"{API}/group-ops", params=me, headers=_h(w["tok"]))
    assert {x["id"] for x in r.json()} == {link["id"], declined["id"]} and other["id"] not in {
        x["id"] for x in r.json()
    }
    r = await client.get(f"{API}/group-ops", params={"pid": A[0]}, headers=_h(w["tok"]))
    assert r.status_code == 422


# --- выключатель автопилота и канал владельца --------------------------------------------------------------------


async def test_autopilot_switch_wa_group(client, ui, db, clock, world, wa):
    w = world
    sid = await _auto(client, ui, w)
    plain = {"kind": "add", "channel_id": wa["group"], "participants": [PHONE]}
    body = {**plain, **_me(sid)}
    r = await client.post(f"{API}/group-ops", json=body, headers=_h(w["tok"]))
    assert r.status_code == 201 and r.json()["status"] == "pending"  # выключатель стоит
    await _put(ui, "global", "wa_group", False)
    r = await client.post(f"{API}/group-ops", json={**plain, **_me(sid, None)}, headers=_h(w["tok"]))
    assert r.json()["status"] == "pending"  # без ключа запуска — как обычно
    r = await client.post(f"{API}/group-ops", json=body, headers=_h(w["tok"]))
    assert (r.json()["status"], r.json()["auto"]) == ("approved", True)
    assert [a["hub_session_id"] for a in _audits(db, "group_op_auto")] == [sid]
    # `tg_join` / `tg_leave` к группам WhatsApp-операций отношения не имеют.
    assert _audits(db, "group_op_approved") == []


async def test_owner_channel_card_answered_by_agent(client, ui, db, clock, world, wa):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    made = await _op(
        client, w, {"kind": "remove", "channel_id": wa["group"], "participants": [PHONE], **_me(sid, None)},
        session=False,
    )  # fmt: skip
    ref = f"group_op:{made['id']}"
    card = _refs(await _queue(client, agent))[ref]
    assert (card["kind"], card["session_id"], card["in_channel"]) == ("group_op", sid, True)
    assert RAW not in json.dumps(card)
    await _answer(client, ref, {"action": "approve"}, token=agent)
    assert _row(db, made["id"]).status == "approved"
    await _answer(client, ref, {"action": "approve"}, token=agent, expect=409)
    assert ref not in _refs(await _queue(client, ui=ui))


# --- `/sync` и кадр SSE -------------------------------------------------------------------------------------


async def test_sync_and_stream_frame_without_texts(client, ui, db, clock, world, wa):
    w = world
    with db.reader() as conn:
        start = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    made = await _op(client, w, _create_body(w, reason="СЕКРЕТНАЯ ПРИЧИНА"))
    r = await ui.get(f"{API}/sync", params={"since": start})
    assert [x["id"] for x in r.json()["group_ops"]] == [made["id"]]
    r = await client.get(f"{API}/sync", params={"since": start}, headers=_h(w["tok"]))
    assert [x["id"] for x in r.json()["group_ops"]] == [made["id"]]
    principal = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    with db.reader() as conn:
        changes = queries.changes_since(conn, principal, since=start, limit=50)
    frames = [f for f in feed._frames(changes) if "event: group_op" in f]  # noqa: SLF001
    assert len(frames) == 1
    data = json.loads(frames[0].split("data: ", 1)[1])
    assert data == {
        "id": made["id"],
        "kind": "create",
        "project_id": w["p3"],
        "channel_id": None,
        "status": "pending",
        "seq": data["seq"],
    }
    assert "СЕКРЕТНАЯ" not in frames[0] and "hub test" not in frames[0]
