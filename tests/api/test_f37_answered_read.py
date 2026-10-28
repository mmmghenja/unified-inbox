"""Ф37-2 (docs/architecture.md): подтвердил черновик — значит прочитал;
карточки беседы закрываются одним вызовом.

- D1: владелец подтвердил черновик сессии — беседы нет в `GET /home/react`, `unread = 0`, задание отметки в
  источник выдаётся коннектору, цель — последнее чужое сообщение до черновика.
- D2: входящее, пришедшее ПОСЛЕ создания черновика, остаётся непрочитанным.
- D3: `approve-batch` — каждая беседа по своему черновику; «Отправить сейчас» у `send_at` — тоже.
- D4: ответ в тред — маркер треда, маркер беседы и `read_ack` не трогаются.
- D5: чужих сообщений нет, заготовка `new:…`, аккаунт с `read_ack=false`.
- D7 (ядро): `POST /agent/items/channel-status` — все открытые карточки беседы и только её; чужой проект —
  404.

Авто-отправка (D6) — `test_f37_autosend.py` (`test_p9_…`); MCP — `tests/hub_mcp/test_f37_mcp_answered.py`."""

import datetime as dt
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import outbox, read_ack, security
from hub.core.db import db_of

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    insert_thread,
    login_via_code,
)

MIN = 60_000


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, source="telegram", **config):
    full = {"idempotent_send": True, "pace_window": "00:00-24:00", **config}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(full)),
            ).lastrowid
        )
    )


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(db, clock):
    project = insert_project(db, name="Umbrella")
    other = insert_project(db, name="Initech")
    acc = _account(db)
    token, token_id = _token(db)
    return {"p": project, "other": other, "acc": acc, "tok": token, "tok_id": token_id}


@pytest.fixture
async def ui(app, db, clock):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        headers = csrf_headers(cookie)

        class _Ui:
            async def get(self, path, **kw):
                return await c.get(path, **kw)

            async def post(self, path, body):
                return await c.post(path, json=body, headers=headers)

        yield _Ui()


def _dm(db, w, ext_id="dm1", **kw):
    return insert_channel(db, w["acc"], project_id=w["p"], ext_id=ext_id, kind="dm", **kw)


def _draft(db, clock, w, channel_id, *, text="ответ сессии", thread=None, send_at=None, key=None) -> int:
    result = db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            thread_ext_id=thread,
            origin="api",
            api_token_id=w["tok_id"],
            idempotency_key=key,
            now_ms=_now(clock),
            send_at=send_at,
        )
    )
    assert result.status == "pending_approval"
    return result.outbox_id


async def _channel(ui, channel_id) -> dict:
    r = await ui.get(f"/api/v1/channels/{channel_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def _react_ids(ui) -> set[int]:
    r = await ui.get("/api/v1/home/react")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    return {item["channel"]["id"] for item in items if item.get("channel")}


def _ack_row(db, channel_id):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT message_id FROM {db_of(channel_id)}.read_ack WHERE channel_id = ?",  # noqa: S608
            (channel_id,),
        ).fetchone()


def _approve_detail(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'outbox_approve' AND actor = 'ui' "
            "AND outbox_id = ? ORDER BY id DESC LIMIT 1",
            (outbox_id,),
        ).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


# --- D1: подтвердил — прочитал, отметка уходит в источник ---------------------------------------------------


async def test_d1_approve_marks_channel_read_and_queues_read_ack(ui, db, clock, world):
    w = world
    ch = _dm(db, w)
    now = _now(clock)
    insert_message(db, ch, ext_id="100", ts=now - 10 * MIN, text="первый вопрос")
    last = insert_message(db, ch, ext_id="101", ts=now - 5 * MIN, text="второй вопрос")
    draft = _draft(db, clock, w, ch)
    assert (await _channel(ui, ch))["unread"] == 2
    assert ch in await _react_ids(ui)

    clock.advance_ms(MIN)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200 and r.json()["status"] == "queued"

    channel = await _channel(ui, ch)
    assert channel["unread"] == 0 and channel["unread_react"] == 0
    assert ch not in await _react_ids(ui)
    assert _ack_row(db, ch) == (last,)
    assert _approve_detail(db, draft) == {"read_up_to": last, "read": "changed", "read_ack": "queued"}

    clock.advance_ms(read_ack.MAX_DELAY_MS + 1)
    leased = db.write_sync(lambda conn: read_ack.lease(conn, account_id=w["acc"], now_ms=_now(clock)))
    assert [(item["channel_id"], item["message"]["ext_id"]) for item in leased] == [(ch, "101")]


async def test_d1_own_messages_are_not_the_target(ui, db, clock, world):
    """Цель — последнее ЧУЖОЕ сообщение: своё более позднее (с телефона) маркер дальше не тянет."""
    w = world
    ch = _dm(db, w)
    now = _now(clock)
    foreign = insert_message(db, ch, ext_id="100", ts=now - 10 * MIN)
    insert_message(db, ch, ext_id="101", ts=now - 5 * MIN, is_me=1, text="сам с телефона")
    draft = _draft(db, clock, w, ch)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    assert _approve_detail(db, draft)["read_up_to"] == foreign
    assert (await _channel(ui, ch))["unread"] == 0


# --- D2: пришедшее после создания черновика остаётся непрочитанным -----------------------------------------


async def test_d2_message_after_draft_stays_unread(ui, db, clock, world):
    w = world
    ch = _dm(db, w)
    before = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN, text="вопрос")
    draft = _draft(db, clock, w, ch)
    clock.advance_ms(MIN)
    insert_message(db, ch, ext_id="101", ts=_now(clock), text="а ещё вот что")
    clock.advance_ms(MIN)

    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    channel = await _channel(ui, ch)
    assert channel["unread"] == 1 and channel["unread_react"] == 1
    assert ch in await _react_ids(ui)
    assert _ack_row(db, ch) == (before,)
    assert _approve_detail(db, draft)["read_up_to"] == before


async def test_d2_late_delivery_with_old_source_time_stays_unread(ui, db, clock, world):
    """Сообщение дошло до хаба ПОСЛЕ создания черновика, хотя по времени источника оно раньше (догон после
    сна): сессия его не видела — прочитанным оно не становится (нет другого чужого до заготовки)."""
    w = world
    ch = _dm(db, w)
    draft = _draft(db, clock, w, ch)
    insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN, text="дошло поздно")
    clock.advance_ms(MIN)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    assert (await _channel(ui, ch))["unread"] == 1
    assert _ack_row(db, ch) is None and _approve_detail(db, draft) == {}


# --- D3: пачка и «Отправить сейчас» -------------------------------------------------------------------------


async def test_d3_approve_batch_marks_each_channel_by_its_own_draft(ui, db, clock, world):
    w = world
    channels, targets, drafts = [], [], []
    for i in range(3):
        ch = _dm(db, w, ext_id=f"dm{i}")
        targets.append(insert_message(db, ch, ext_id=str(100 + i), ts=_now(clock) - 5 * MIN))
        drafts.append(_draft(db, clock, w, ch))
        channels.append(ch)
    # В третьей беседе после черновика пришло ещё одно — оно остаётся.
    clock.advance_ms(MIN)
    insert_message(db, channels[2], ext_id="200", ts=_now(clock))

    r = await ui.post("/api/v1/outbox/approve-batch", {"ids": drafts})
    assert r.status_code == 200
    assert [item["status"] for item in r.json()["results"]] == ["queued"] * 3
    unread = [(await _channel(ui, ch))["unread"] for ch in channels]
    assert unread == [0, 0, 1]
    assert [_ack_row(db, ch) for ch in channels] == [(t,) for t in targets]
    assert _approve_detail(db, drafts[1]) == {
        "batch": True,
        "read_up_to": targets[1],
        "read": "changed",
        "read_ack": "queued",
    }


async def test_d3_batch_error_item_marks_nothing(ui, db, clock, world):
    """Ошибка одного id пачки откатывает только его — и его отметку прочтения тоже."""
    w = world
    ch = _dm(db, w)
    insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    draft = _draft(db, clock, w, ch)
    r = await ui.post(f"/api/v1/outbox/{draft}/cancel", {})
    assert r.status_code == 200
    r = await ui.post("/api/v1/outbox/approve-batch", {"ids": [draft]})
    assert r.json()["results"][0]["status"] == "error"
    assert (await _channel(ui, ch))["unread"] == 1 and _ack_row(db, ch) is None


async def test_d3_send_now_marks_read(ui, db, clock, world):
    w = world
    ch = _dm(db, w)
    target = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    moment = dt.datetime.fromtimestamp(clock() + 3600, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    draft = _draft(db, clock, w, ch, send_at=moment)
    clock.advance_ms(outbox.SEND_AT_GRACE_MS + 61 * MIN + 1)

    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 409 and r.json()["error"] == "send_at_passed"
    assert (await _channel(ui, ch))["unread"] == 1 and _ack_row(db, ch) is None  # отказ ничего не отмечает

    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {"send_now": True})
    assert r.status_code == 200
    assert (await _channel(ui, ch))["unread"] == 0 and _ack_row(db, ch) == (target,)


# --- D4: ответ в тред ---------------------------------------------------------------------------------------


async def test_d4_thread_reply_moves_thread_marker_only(ui, db, clock, world):
    w = world
    acc = _account(db, "slack")
    ch = insert_channel(db, acc, project_id=w["p"], ext_id="C1", kind="group")
    now = _now(clock)
    insert_message(db, ch, ext_id="1700000000.000100", ts=now - 20 * MIN, is_me=1, text="родитель")
    top = insert_message(db, ch, ext_id="1700000000.000200", ts=now - 15 * MIN, text="в ленте канала")
    parent = "1700000000.000100"
    insert_thread(db, ch, ext_id=parent, subscribed=1, first_seen_ts=now - 20 * MIN, last_reply_ts=now)
    insert_message(db, ch, ext_id="1700000000.000300", ts=now - 10 * MIN, thread_ext_id=parent)
    reply = insert_message(db, ch, ext_id="1700000000.000400", ts=now - 5 * MIN, thread_ext_id=parent)
    before = await _channel(ui, ch)
    assert (before["unread_top"], before["unread_threads"]) == (1, 2)

    draft = _draft(db, clock, w, ch, thread=parent)
    clock.advance_ms(MIN)
    late = insert_message(db, ch, ext_id="1700000000.000500", ts=_now(clock), thread_ext_id=parent)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200

    after = await _channel(ui, ch)
    # Ответы треда до черновика прочитаны, пришедший после — нет; лента канала и отметка в источник целы.
    assert (after["unread_top"], after["unread_threads"]) == (1, 1)
    assert _ack_row(db, ch) is None
    assert _approve_detail(db, draft) == {"thread_read_up_to": reply, "thread_read": "changed"}
    with db.reader() as conn:
        marker = conn.execute(
            "SELECT last_read_ext FROM main.thread WHERE channel_id = ? AND ext_id = ?", (ch, parent)
        ).fetchone()[0]
        channel_marker = conn.execute(
            "SELECT last_read_ext FROM main.channel WHERE id = ?", (ch,)
        ).fetchone()[0]
    assert marker == "1700000000.000400" and channel_marker is None
    assert top and late


async def test_d4_thread_without_foreign_replies_does_nothing(ui, db, clock, world):
    w = world
    acc = _account(db, "slack")
    ch = insert_channel(db, acc, project_id=w["p"], ext_id="C1", kind="group")
    now = _now(clock)
    parent = "1700000000.000100"
    insert_message(db, ch, ext_id=parent, ts=now - 20 * MIN, text="чужой родитель")
    draft = _draft(db, clock, w, ch, thread=parent)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    assert (await _channel(ui, ch))["unread_top"] == 1  # родитель в ленте — не ответ треда, маркер беседы цел
    assert _ack_row(db, ch) is None and _approve_detail(db, draft) == {}


# --- D5: нечего отмечать ------------------------------------------------------------------------------------


async def test_d5_no_foreign_messages(ui, db, clock, world):
    w = world
    ch = _dm(db, w)
    insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN, is_me=1)
    draft = _draft(db, clock, w, ch)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    assert _ack_row(db, ch) is None and _approve_detail(db, draft) == {}


async def test_d5_draft_channel_new(ui, db, clock, world):
    w = world
    ch = insert_channel(db, w["acc"], project_id=w["p"], ext_id="new:abc", kind="dm")
    insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    draft = _draft(db, clock, w, ch)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200, r.text
    assert (await _channel(ui, ch))["unread"] == 1
    assert _ack_row(db, ch) is None
    assert "read_up_to" not in _approve_detail(db, draft)


async def test_d5_no_reply_channel(ui, db, clock, world):
    """Беседа трекера стала `no_reply` после черновика: подтверждение отклоняется (422, как раньше) и ничего
    не отмечает."""
    w = world
    ch = _dm(db, w)
    insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    draft = _draft(db, clock, w, ch)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET meta_json = ? WHERE id = ?",
            (json.dumps({"attrs": {"no_reply": True}}), ch),
        )
    )
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 422
    assert (await _channel(ui, ch))["unread"] == 1 and _ack_row(db, ch) is None


async def test_d5_account_with_read_ack_off(ui, db, clock, world):
    w = world
    acc = _account(db, read_ack=False)
    ch = insert_channel(db, acc, project_id=w["p"], ext_id="dm-off", kind="dm")
    target = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    draft = _draft(db, clock, w, ch)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    assert (await _channel(ui, ch))["unread"] == 0  # маркер двигается
    assert _ack_row(db, ch) is None  # задания в источник нет
    assert _approve_detail(db, draft) == {"read_up_to": target, "read": "changed", "read_ack": "off"}


async def test_already_read_channel_is_unchanged(ui, db, clock, world):
    w = world
    ch = _dm(db, w)
    target = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    r = await ui.post(f"/api/v1/channels/{ch}/read", {"up_to_message_id": target})
    assert r.status_code == 200
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.read_ack"))
    draft = _draft(db, clock, w, ch)
    r = await ui.post(f"/api/v1/outbox/{draft}/approve", {})
    assert r.status_code == 200
    assert _approve_detail(db, draft) == {"read_up_to": target, "read": "unchanged"}
    assert _ack_row(db, ch) is None


# --- D7 (ядро): карточки беседы одним вызовом --------------------------------------------------------------


def _card(db, channel_id, message_id, status, label="reply"):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.agent_item (channel_id, label, summary, message_ids_json, "
                "last_message_id, covered_seq, status, provider, model, created_ts, updated_ts) "
                "VALUES (?, ?, 'сводка', ?, ?, 1, ?, 'p', 'm', 1, 1)",
                (channel_id, label, json.dumps([message_id]), message_id, status),
            ).lastrowid
        )
    )


def _statuses(db) -> dict[int, str]:
    with db.reader() as conn:
        return dict(conn.execute("SELECT id, status FROM main.agent_item").fetchall())


async def test_d7_channel_status_closes_open_cards_of_this_channel_only(client, db, clock, world):
    w = world
    ch, other = _dm(db, w), _dm(db, w, ext_id="dm2")
    m1 = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    m2 = insert_message(db, other, ext_id="200", ts=_now(clock) - 5 * MIN)
    new, seen = _card(db, ch, m1, "new"), _card(db, ch, m1, "seen", label="urgent")
    done, dismissed = _card(db, ch, m1, "done"), _card(db, ch, m1, "dismissed")
    foreign = _card(db, other, m2, "new")

    r = await client.post(
        "/api/v1/agent/items/channel-status",
        json={"channel_id": ch, "status": "done"},
        headers=_bearer(w["tok"]),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"channel_id": ch, "status": "done", "ids": [new, seen], "handled_up_to": None}
    assert _statuses(db) == {new: "done", seen: "done", done: "done", dismissed: "dismissed", foreign: "new"}

    # Повтор — открытых больше нет: пустой список, не ошибка.
    r = await client.post(
        "/api/v1/agent/items/channel-status",
        json={"channel_id": ch, "status": "done"},
        headers=_bearer(w["tok"]),
    )
    assert r.status_code == 200 and r.json()["ids"] == []


async def test_d7_channel_status_seen_only_from_new(client, db, clock, world):
    w = world
    ch = _dm(db, w)
    m1 = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    new, seen = _card(db, ch, m1, "new"), _card(db, ch, m1, "seen")
    r = await client.post(
        "/api/v1/agent/items/channel-status",
        json={"channel_id": ch, "status": "seen"},
        headers=_bearer(w["tok"]),
    )
    assert r.status_code == 200 and r.json()["ids"] == [new]
    assert _statuses(db) == {new: "seen", seen: "seen"}


async def test_d7_channel_status_refusals(client, db, clock, world):
    w = world
    ch = _dm(db, w)
    m1 = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN)
    card = _card(db, ch, m1, "new")
    path = "/api/v1/agent/items/channel-status"

    # Токен другого проекта беседу не видит — 404, карточка цела.
    foreign, _ = _token(db, name="mcp:initech", project_ids=[w["other"]])
    r = await client.post(path, json={"channel_id": ch, "status": "done"}, headers=_bearer(foreign))
    assert r.status_code == 404
    # Фоновый токен (разборщик) карточки не закрывает.
    triage, _ = _token(db, name="agent:triage", kind="background", scopes=("read", "triage"))
    r = await client.post(path, json={"channel_id": ch, "status": "done"}, headers=_bearer(triage))
    assert r.status_code == 403
    # `new` целевым статусом не бывает; лишние поля — 422.
    for body in (
        {"channel_id": ch, "status": "new"},
        {"channel_id": ch},
        {"channel_id": ch, "status": "done", "item_ids": [card]},
    ):
        r = await client.post(path, json=body, headers=_bearer(w["tok"]))
        assert r.status_code == 422, body
    r = await client.post(path, json={"channel_id": 999_999, "status": "done"}, headers=_bearer(w["tok"]))
    assert r.status_code == 404
    assert _statuses(db) == {card: "new"}
