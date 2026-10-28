"""Ф12-1: черновик новому адресату — `POST /api/v1/drafts/new`, беседа-заготовка, привязка после отправки,
судьба заготовки, невидимость коннекторам (`docs/architecture.md` + П1–П4, П7).
"""

import json

import pytest

from hub import msgid
from hub.core import ingest, media, new_recipient, outbox, queries, reactions, read_ack
from hub.core.draft_channels import TEMP_PREFIX

from .conftest import (
    ORIGIN,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    make_bearer_token,
)


class _NoPause:
    """Нижняя граница случайности графика: первая строка уходит без паузы (24.10 пауза `[0, gap_min]` —
    случайная; эти тесты проверяют не график, а путь первого сообщения)."""

    def randint(self, a: int, b: int) -> int:
        return a


@pytest.fixture(autouse=True)
def _no_first_pause(monkeypatch):
    monkeypatch.setattr("hub.core.outbox._rng", _NoPause())


OWNER = "connector:1"
MAIL_ADDRESS = "me@gmail.com"


def _account(db, source, *, config=None, db_name="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, ?, ?)",
                (source, db_name, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )


@pytest.fixture
def env(db):
    project = insert_project(db, name="Umbrella")
    other_project = insert_project(db, name="Initech")
    # Ф12-2: окно графика — круглые сутки, первый слот — «сейчас» (график здесь не проверяется).
    tg = _account(db, "telegram", config={"idempotent_send": True, "pace_window": "00:00-24:00"})
    mail = _account(
        db,
        "mail",
        config={"address": MAIL_ADDRESS, "aliases": ["sales@umbrella.example.com"], "pace_window": "00:00-24:00"},
    )
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project], name="mcp:iip")
    all_token = make_bearer_token(db, scopes=("read", "draft"), name="mcp:all")
    return {
        "project": project,
        "other_project": other_project,
        "tg": tg,
        "mail": mail,
        "token": token,
        "all_token": all_token,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _new(client, token, **body):
    return await client.post("/api/v1/drafts/new", json=body, headers=_auth(token))


async def _tg_draft(client, env, to="@Petr_Ivanov", text="Здравствуйте! Пишу по поводу подписи.", **kw):
    r = await _new(
        client, env["token"], account_id=env["tg"], project_id=env["project"], to=to, text=text, **kw
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _mail_draft(client, env, to="client@example.com", subject="Подпись для ООО", **kw):
    r = await _new(
        client,
        env["token"],
        account_id=env["mail"],
        project_id=env["project"],
        to=to,
        subject=subject,
        text="Добрый день!",
        **kw,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _channel(db, channel_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT ext_id, kind, title, project_id, map_source, map_reason, archived, meta_json, "
            "my_msg_count, "
            "last_msg_ts, seq, first_seen_ts FROM main.channel WHERE id = ?",
            (channel_id,),
        ).fetchone()
    keys = (
        "ext_id kind title project_id map_source map_reason archived meta_json my_msg_count last_msg_ts seq "
        "first_seen_ts"
    ).split()
    out = dict(zip(keys, row, strict=True))
    out["meta"] = json.loads(out.pop("meta_json") or "{}")
    return out


def _outbox(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT status, channel_id, ext_id, send_token, error FROM main.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
    return dict(zip(("status", "channel_id", "ext_id", "send_token", "error"), row, strict=True))


def _message(db, message_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT channel_id, ext_id, outbox_id, deleted, seq, is_me FROM main.message WHERE id = ?",
            (message_id,),
        ).fetchone()
    return dict(zip(("channel_id", "ext_id", "outbox_id", "deleted", "seq", "is_me"), row, strict=True))


def _audit(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, channel_id, outbox_id, detail_json FROM main.audit WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()
    return [
        {"result": r[0], "channel_id": r[1], "outbox_id": r[2], "detail": json.loads(r[3] or "null")}
        for r in rows
    ]


def _now(clock) -> int:
    return int(clock() * 1000)


def _approve_and_lease(db, clock, outbox_id, account_id):
    # Ф12-2: черновик новому адресату — по графику; часы теста дотягиваются до его слота.
    slot = db.write_sync(lambda conn: outbox.approve(conn, outbox_id, now_ms=_now(clock)))
    if slot is not None and slot > _now(clock):
        clock.advance_ms(slot - _now(clock))
    leases = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=account_id, owner=OWNER, now_ms=_now(clock))
    )
    return next(item for item in leases if item.outbox_id == outbox_id)


def _report(db, clock, lease, outcome="sent", **kw):
    db.write_sync(
        lambda conn: outbox.report(
            conn,
            outbox_id=lease.outbox_id,
            owner=OWNER,
            lease_gen=lease.lease_gen,
            outcome=outcome,
            now_ms=_now(clock),
            **kw,
        )
    )


def _ingest(db, clock, account_id, events):
    return db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=_now(clock)
        )
    )


def _dm_event(ext_id, chat, *, is_me=True, ts=1_700_000_000_000, text="hi", title="Пётр"):
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "dm", "title": title},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


def _mail_event(mid, thrid, *, send_token=None, ts=1_700_000_000_000, text="Добрый день!"):
    event = {
        "type": "upsert",
        "channel": {
            "ext_id": str(thrid),
            "kind": "mail_thread",
            "title": "Подпись для ООО",
            "own_id_space": True,
        },
        "ext_id": mid,
        "ts": ts,
        "is_me": True,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    if send_token is not None:
        event["send_token"] = send_token
    return event


# --- тест 1: беседа-заготовка Telegram ---------------------------------------------------------------------


async def test_1_tg_draft_creates_temp_channel(client, db, env):
    body = await _tg_draft(client, env)
    assert body["status"] == "pending_approval"
    assert body["existing"] is False
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"].startswith(TEMP_PREFIX) and len(ch["ext_id"]) == len(TEMP_PREFIX) + 32
    assert ch["kind"] == "dm"
    assert ch["title"] == "@Petr_Ivanov"
    assert ch["project_id"] == env["project"]
    assert (ch["map_source"], ch["map_reason"]) == ("manual", "новая беседа")
    assert ch["meta"]["attrs"]["new_to"] == {"kind": "tg_username", "username": "Petr_Ivanov"}
    ob = _outbox(db, body["outbox_id"])
    assert ob["status"] == "pending_approval" and ob["channel_id"] == body["channel_id"]
    placeholder = _message(db, body["message_id"])
    assert placeholder["channel_id"] == body["channel_id"] and placeholder["ext_id"] is None

    # лента беседы и выдача канала: заготовка видна, ответить нельзя — с причиной
    r = await client.get(f"/api/v1/channels/{body['channel_id']}", headers=_auth(env["token"]))
    assert r.status_code == 200
    channel = r.json()
    assert channel["can_reply"] is False
    assert channel["can_reply_reason"] == "новая беседа — сначала уйдёт первое сообщение"
    assert channel["new_to"] == {"kind": "tg_username", "username": "Petr_Ivanov"}
    r = await client.get(f"/api/v1/channels/{body['channel_id']}/messages", headers=_auth(env["token"]))
    assert [m["id"] for m in r.json()["messages"]] == [body["message_id"]]
    r = await client.get(f"/api/v1/projects/{env['project']}/channels", headers=_auth(env["token"]))
    listed = next(c for c in r.json() if c["id"] == body["channel_id"])
    assert listed["new_to"]["username"] == "Petr_Ivanov" and listed["can_reply"] is False

    audit = _audit(db, "draft_new")
    assert len(audit) == 1
    detail = audit[0]["detail"]
    assert detail["to_kind"] == "tg_username" and detail["text_len"] == len(
        "Здравствуйте! Пишу по поводу подписи."
    )
    raw = json.dumps(detail, ensure_ascii=False)
    assert "Petr" not in raw and "Здравствуйте" not in raw


# --- тест 2: разбор `to` Telegram --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "to", ["@Petr_Ivanov", "Petr_Ivanov", "t.me/Petr_Ivanov", "https://t.me/Petr_Ivanov"]
)
def test_2_parse_username_forms(to):
    assert new_recipient.parse_tg_username(to) == "Petr_Ivanov"


@pytest.mark.parametrize(
    "to", ["@a b", "@123abc", "", "   ", "@abc", "http://t.me/Petr_Ivanov", "t.me/", "@a" * 20]
)
async def test_2_bad_username_422(client, env, to):
    r = await _new(client, env["token"], account_id=env["tg"], project_id=env["project"], to=to, text="x")
    assert r.status_code == 422, r.text


# --- тест 3: почта — адрес, тема, from ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("to", "subject", "sender"),
    [
        ("Пётр <petr@example.com>", "Тема", None),
        ("a@example.com, b@example.com", "Тема", None),
        ("petr@localhost", "Тема", None),
        ("petr", "Тема", None),
        ("petr@example.com", None, None),
        ("petr@example.com", "", None),
        ("petr@example.com", "Тема\r\nBcc: x@y.z", None),
        ("petr@example.com", "Тема", "boss@gmail.com"),
    ],
)
async def test_3_mail_validation_422(client, env, to, subject, sender):
    body = {"account_id": env["mail"], "project_id": env["project"], "to": to, "text": "x"}
    if subject is not None:
        body["subject"] = subject
    if sender is not None:
        body["from"] = sender
    r = await _new(client, env["token"], **body)
    assert r.status_code == 422, r.text


async def test_3_mail_draft_with_alias(client, db, env):
    body = await _mail_draft(client, env, to="Client@Example.com", **{"from": "Sales@umbrella.example.com"})
    ch = _channel(db, body["channel_id"])
    assert ch["kind"] == "mail_thread"
    assert ch["title"] == "Подпись для ООО → client@example.com"
    assert ch["meta"]["attrs"]["new_to"] == {
        "kind": "email",
        "to": "client@example.com",
        "subject": "Подпись для ООО",
        "from": "sales@umbrella.example.com",
    }
    assert ch["meta"]["own_id_space"] is True


# --- тест 4 и П7: граница проектов, источник, UI ------------------------------------------------------------


async def test_4_project_scope_and_source(client, db, env):
    base = {"account_id": env["tg"], "to": "@Petr_Ivanov", "text": "x"}
    r = await _new(client, env["token"], project_id=env["other_project"], **base)
    assert r.status_code == 422  # проектный токен — только свой проект
    r = await _new(client, env["token"], project_id=None, **base)
    assert r.status_code == 422
    r = await _new(client, env["all_token"], project_id=-1, **base)
    assert r.status_code == 422
    r = await _new(client, env["token"], project_id=9999, **base)
    assert r.status_code == 422
    slack = _account(db, "slack")
    r = await _new(client, env["token"], project_id=env["project"], **{**base, "account_id": slack})
    assert r.status_code == 422
    r = await _new(client, env["token"], project_id=env["project"], **{**base, "account_id": 9999})
    assert r.status_code == 422
    read_only = make_bearer_token(db, scopes=("read",), project_ids=[env["project"]], name="ro")
    r = await _new(client, read_only, project_id=env["project"], **base)
    assert r.status_code == 403
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


async def test_p7_ui_session_forbidden(client, db, clock, env):
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/drafts/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@Petr_Ivanov", "text": "x"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 403
    assert ORIGIN  # тот же Origin, что у прочих UI-запросов
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


# --- тест 5: повтор, уникальность (П1), лимит ---------------------------------------------------------------


async def test_5_idempotent_repeat_and_conflict(client, db, env):
    first = await _tg_draft(client, env, idempotency_key="k1")
    again = await _tg_draft(client, env, idempotency_key="k1")
    assert again == first
    r = await _new(
        client, env["token"], account_id=env["tg"], project_id=env["project"], to="@Petr_Ivanov",
        text="другой текст", idempotency_key="k1",
    )  # fmt: skip
    assert r.status_code == 409
    r = await _new(
        client, env["token"], account_id=env["tg"], project_id=env["project"], to="@Other_Man",
        text="Здравствуйте! Пишу по поводу подписи.", idempotency_key="k1",
    )  # fmt: skip
    assert r.status_code == 409
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1


async def test_5_second_draft_same_recipient_409_other_project_ok(client, db, env):
    first = await _tg_draft(client, env, to="@Petr_Ivanov")
    r = await _new(
        client, env["token"], account_id=env["tg"], project_id=env["project"], to="t.me/petr_ivanov", text="y"
    )
    assert r.status_code == 409
    assert f"#{first['outbox_id']}" in r.json()["detail"]
    # П1: та же персона из другого проекта — своя заготовка (заготовка первого проекта этому токену не видна)
    r = await _new(
        client, env["all_token"], account_id=env["tg"], project_id=env["other_project"], to="@Petr_Ivanov",
        text="y",
    )  # fmt: skip
    assert r.status_code == 200
    assert r.json()["channel_id"] != first["channel_id"]


async def test_5_limit_100_open_drafts(client, db, clock, env):
    token_id = db.write_sync(
        lambda conn: conn.execute("SELECT id FROM main.api_token WHERE name = 'mcp:iip'").fetchone()[0]
    )

    def _fill(conn):
        for i in range(new_recipient.MAX_OPEN_DRAFTS):
            new_recipient.create_draft(
                conn, account_id=env["tg"], project_id=env["project"], to=f"@user_{i:04d}", text="x",
                subject=None, from_address=None, api_token_id=token_id, idempotency_key=None,
                now_ms=_now(clock),
            )  # fmt: skip

    db.write_sync(_fill)
    r = await _new(
        client, env["token"], account_id=env["tg"], project_id=env["project"], to="@user_x101", text="x"
    )
    assert r.status_code == 429
    # архивная (отменённая) в лимит не входит
    with db.reader() as conn:
        oid = conn.execute("SELECT MIN(id) FROM main.outbox").fetchone()[0]
    db.write_sync(lambda conn: outbox.cancel(conn, oid, now_ms=_now(clock)))
    r = await _new(
        client, env["token"], account_id=env["tg"], project_id=env["project"], to="@user_x101", text="x"
    )
    assert r.status_code == 200


# --- тест 6 (П1): личка с этим username уже есть — всё равно новая заготовка


async def test_6_existing_dm_still_new_draft(client, db, env):
    existing = insert_channel(
        db, env["tg"], ext_id="555", kind="dm", title="Пётр", project_id=env["other_project"]
    )
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.contact (account_id, ext_id, name, handle) "
            "VALUES (?, '555', 'Пётр', '@petr_ivanov')",
            (env["tg"],),
        )
    )
    body = await _tg_draft(client, env, to="@PETR_IVANOV")
    assert body["existing"] is False
    assert body["channel_id"] != existing
    assert _channel(db, body["channel_id"])["ext_id"].startswith(TEMP_PREFIX)


# --- тест 7: задание несёт new_to; второй черновик и файл — отказ -------------------------------------------


async def test_7_lease_new_to_second_enqueue_409_file_422(client, db, clock, env):
    body = await _tg_draft(client, env)
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": body["channel_id"], "text": "ещё", "idempotency_key": "x2"},
        headers=_auth(env["token"]),
    )
    assert r.status_code == 409
    # Ф14-2 A4: файлы в Telegram-заготовку больше не запрещены — второе сообщение (с файлом тоже) — 409
    with pytest.raises(outbox.DraftChannelBusy):
        db.write_sync(
            lambda conn: outbox.enqueue(
                conn, channel_id=body["channel_id"], text="", origin="ui", api_token_id=1,
                idempotency_key=None, now_ms=_now(clock), upload_ids=["u1"],
            )
        )  # fmt: skip
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    assert lease.new_to == {"kind": "tg_username", "username": "Petr_Ivanov"}
    assert lease.channel_ext_id.startswith(TEMP_PREFIX)
    # у обычной беседы — None
    plain = insert_channel(db, env["tg"], ext_id="777", kind="dm")
    db.write_sync(
        lambda conn: outbox.enqueue(
            conn, channel_id=plain, text="t", origin="ui", api_token_id=1, idempotency_key=None,
            now_ms=_now(clock),
        )
    )  # fmt: skip
    leases = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=env["tg"], owner=OWNER, now_ms=_now(clock))
    )
    assert [item.new_to for item in leases] == [None]


# --- тест 8: отчёт sent + peer, беседы нет -> переименование


async def test_8_report_sent_rename(client, db, clock, env):
    body = await _tg_draft(client, env)
    seq_before = _channel(db, body["channel_id"])["seq"]
    msg_seq_before = _message(db, body["message_id"])["seq"]
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр Иванов")

    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"] == "555"
    assert "new_to" not in (ch["meta"].get("attrs") or {})
    assert ch["title"] == "Пётр Иванов" and ch["archived"] == 0
    assert ch["seq"] > seq_before and ch["my_msg_count"] == 1
    msg = _message(db, body["message_id"])
    assert msg["ext_id"] == "9001" and msg["channel_id"] == body["channel_id"] and msg["seq"] > msg_seq_before
    assert _outbox(db, body["outbox_id"])["status"] == "sent"
    r = await client.get(f"/api/v1/channels/{body['channel_id']}", headers=_auth(env["token"]))
    assert r.json()["can_reply"] is True and "new_to" not in r.json()
    assert [a["result"] for a in _audit(db, "draft_bind")] == ["rename"]
    # живое эхо той же отправки ложится на ту же строку — без дубля беседы
    _ingest(db, clock, env["tg"], [_dm_event("9001", "555", text="Здравствуйте! Пишу по поводу подписи.")])
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = '555'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM main.message WHERE ext_id = '9001'").fetchone()[0] == 1


# --- тест 9: отчёт sent + peer, беседа есть -> слияние; проект — по П3


async def test_9_merge_into_echo_created_channel_gets_project(client, db, clock, env):
    body = await _tg_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    clock.advance_ms(1000)
    # эхо пришло раньше отчёта: беседа создана им (в «Неразобранном», авто), строка эха — отдельная
    _ingest(db, clock, env["tg"], [_dm_event("9001", "555", ts=_now(clock), text="Здравствуйте!")])
    with db.reader() as conn:
        target = conn.execute("SELECT id FROM main.channel WHERE ext_id = '555'").fetchone()[0]
    assert _channel(db, target)["project_id"] is None and _channel(db, target)["my_msg_count"] == 1
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр")

    ob = _outbox(db, body["outbox_id"])
    assert ob["status"] == "sent" and ob["channel_id"] == target
    draft = _channel(db, body["channel_id"])
    assert draft["archived"] == 1 and draft["meta"]["merged_into"] == "555"
    with db.reader() as conn:
        alias = conn.execute(
            "SELECT channel_id FROM main.channel_alias WHERE old_ext_id = ?", (draft["ext_id"],)
        ).fetchone()
        real = conn.execute("SELECT id, outbox_id FROM main.message WHERE ext_id = '9001'").fetchall()
    assert alias == (target,)
    assert len(real) == 1 and real[0][1] == body["outbox_id"]  # одна строка — эхо, с outbox
    assert _message(db, body["message_id"])["deleted"] == 1
    t = _channel(db, target)
    assert (t["project_id"], t["map_source"], t["map_reason"]) == (env["project"], "manual", "новая беседа")
    assert t["my_msg_count"] == 1  # эхо уже засчитано, заготовка не удваивает
    assert draft["my_msg_count"] == 0


async def test_9_merge_placeholder_moves_counters(client, db, clock, env):
    """Беседа есть, эха ещё нет: строка заготовки переезжает, счётчики — у найденной беседы."""
    target = insert_channel(db, env["tg"], ext_id="555", kind="dm", first_seen_ts=_now(clock) + 1)
    body = await _tg_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555")
    msg = _message(db, body["message_id"])
    assert (msg["channel_id"], msg["ext_id"], msg["deleted"]) == (target, "9001", 0)
    t = _channel(db, target)
    assert t["my_msg_count"] == 1 and t["last_msg_ts"] is not None
    assert t["project_id"] == env["project"]
    assert _channel(db, body["channel_id"])["my_msg_count"] == 0


async def test_9_merge_old_or_manual_channel_keeps_project(client, db, clock, env):
    # прошлая переписка в «Неразобранном» (появилась раньше черновика) — проект не переезжает (П3)
    old = insert_channel(db, env["tg"], ext_id="555", kind="dm", first_seen_ts=1)
    body = await _tg_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555")
    assert _channel(db, old)["project_id"] is None
    assert _outbox(db, body["outbox_id"])["channel_id"] == old
    kept = _audit(db, "draft_merge_other_project")
    assert len(kept) == 1 and kept[0]["channel_id"] == old

    # ручная привязка — не трогается, даже если беседа свежая
    manual = insert_channel(
        db, env["tg"], ext_id="556", kind="dm", project_id=env["other_project"], first_seen_ts=_now(clock) + 5
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET map_source = 'manual' WHERE id = ?", (manual,))
    )
    body2 = await _tg_draft(client, env, to="@Other_Man")
    lease2 = _approve_and_lease(db, clock, body2["outbox_id"], env["tg"])
    _report(db, clock, lease2, ext_id="9002", peer_ext_id="556")
    assert _channel(db, manual)["project_id"] == env["other_project"]


# --- тест 10: peer_ext_id не к месту — игнор + аудит


async def test_10_peer_ignored_for_regular_channel_and_non_sent(client, db, clock, env):
    plain = insert_channel(db, env["tg"], ext_id="777", kind="dm")
    res = db.write_sync(
        lambda conn: outbox.enqueue(
            conn, channel_id=plain, text="t", origin="ui", api_token_id=1, idempotency_key=None,
            now_ms=_now(clock),
        )
    )  # fmt: skip
    lease = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=env["tg"], owner=OWNER, now_ms=_now(clock))
    )[0]
    _report(db, clock, lease, ext_id="42", peer_ext_id="999", peer_title="Чужой")
    assert _channel(db, plain)["ext_id"] == "777"
    assert _outbox(db, res.outbox_id)["status"] == "sent"
    ignored = _audit(db, "outbox_peer_ignored")
    assert len(ignored) == 1 and ignored[0]["detail"]["draft"] is False

    body = await _tg_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="temporary", error="net", peer_ext_id="555")
    assert _channel(db, body["channel_id"])["ext_id"].startswith(TEMP_PREFIX)
    assert len(_audit(db, "outbox_peer_ignored")) == 2
    # временный ext_id в peer_ext_id — тоже игнор (отчёт не отклоняется)
    clock.advance_ms(10 * 60 * 1000)
    lease = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=env["tg"], owner=OWNER, now_ms=_now(clock))
    )[0]
    _report(db, clock, lease, ext_id="9001", peer_ext_id="new:abc")
    assert _outbox(db, body["outbox_id"])["status"] == "sent"
    assert _channel(db, body["channel_id"])["ext_id"].startswith(TEMP_PREFIX)
    assert len(_audit(db, "outbox_peer_ignored")) == 3


# --- тест 11 + П4: почта — привязка по копии письма


async def test_11_mail_sent_without_peer_then_ingest_renames(client, db, clock, env):
    body = await _mail_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["mail"])
    mid = msgid.our_message_id(lease.send_token, MAIL_ADDRESS)
    _report(db, clock, lease, ext_id=mid)  # копию не нашли — без peer_ext_id
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"].startswith(TEMP_PREFIX) and ch["archived"] == 0
    assert _outbox(db, body["outbox_id"])["status"] == "sent"

    _ingest(db, clock, env["mail"], [_mail_event(mid, 18_000_000_001, send_token=lease.send_token)])
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"] == "18000000001"
    assert "new_to" not in (ch["meta"].get("attrs") or {})
    with db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = '18000000001'").fetchone()[0] == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM main.message WHERE ext_id = ?", (mid,)).fetchone()[0] == 1
    assert _message(db, body["message_id"])["ext_id"] == mid
    assert ch["my_msg_count"] == 1


async def test_p4b_mail_echo_before_report_binds_and_glues(client, db, clock, env):
    body = await _mail_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["mail"])
    mid = msgid.our_message_id(lease.send_token, MAIL_ADDRESS)
    _ingest(
        db,
        clock,
        env["mail"],
        [_mail_event(mid, 18_000_000_002, send_token=lease.send_token, ts=_now(clock))],
    )
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"] == "18000000002"
    msg = _message(db, body["message_id"])
    assert msg["ext_id"] == mid and msg["channel_id"] == body["channel_id"]
    assert _outbox(db, body["outbox_id"])["status"] == "sent"
    # поздний отчёт с тем же ext_id — тихий успех, без дубля
    _report(db, clock, lease, ext_id=mid, peer_ext_id="18000000002")
    assert _channel(db, body["channel_id"])["my_msg_count"] == 1


async def test_p4b_mail_echo_into_existing_thread_merges(client, db, clock, env):
    body = await _mail_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["mail"])
    mid = msgid.our_message_id(lease.send_token, MAIL_ADDRESS)
    thread = insert_channel(db, env["mail"], ext_id="18000000003", kind="mail_thread", first_seen_ts=1)
    _ingest(
        db,
        clock,
        env["mail"],
        [_mail_event(mid, 18_000_000_003, send_token=lease.send_token, ts=_now(clock))],
    )
    ob = _outbox(db, body["outbox_id"])
    assert ob["channel_id"] == thread and ob["status"] == "sent"
    assert _channel(db, body["channel_id"])["archived"] == 1
    with db.reader() as conn:
        rows = conn.execute("SELECT channel_id FROM main.message WHERE ext_id = ?", (mid,)).fetchall()
    assert rows == [(thread,)]


async def test_p4a_mail_report_merges_into_echo_channel(client, db, clock, env):
    """П4а: копия письма уже лежит в другой беседе (эхо пришло раньше и не привязалось) — отчёт без peer
    сливает заготовку в неё."""
    body = await _mail_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["mail"])
    mid = msgid.our_message_id(lease.send_token, MAIL_ADDRESS)
    thread = insert_channel(
        db, env["mail"], ext_id="18000000004", kind="mail_thread", first_seen_ts=_now(clock)
    )
    echo = insert_message(db, thread, ext_id=mid, ts=_now(clock), is_me=1, text="Добрый день!")
    _report(db, clock, lease, ext_id=mid)
    ob = _outbox(db, body["outbox_id"])
    assert ob["channel_id"] == thread and ob["status"] == "sent"
    assert _message(db, echo)["outbox_id"] == body["outbox_id"]
    assert _message(db, body["message_id"])["deleted"] == 1
    assert _channel(db, body["channel_id"])["archived"] == 1
    assert _channel(db, thread)["project_id"] == env["project"]


async def test_p4_telegram_echo_does_not_bind_at_ingest(client, db, clock, env):
    """П4: для Telegram привязка при приёме не применяется — эхо создаёт свою беседу, слияние — отчётом."""
    body = await _tg_draft(client, env)
    _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _ingest(db, clock, env["tg"], [_dm_event("9001", "555")])
    assert _channel(db, body["channel_id"])["ext_id"].startswith(TEMP_PREFIX)


async def test_p4b_resolve_sent_archived_then_bound(client, db, clock, env):
    body = await _mail_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["mail"])
    _report(db, clock, lease, outcome="unknown")
    db.write_sync(
        lambda conn: outbox.resolve_unknown(conn, body["outbox_id"], decision="sent", now_ms=_now(clock))
    )
    assert _channel(db, body["channel_id"])["archived"] == 1
    mid = msgid.our_message_id(lease.send_token, MAIL_ADDRESS)
    _ingest(db, clock, env["mail"], [_mail_event(mid, 18_000_000_005, send_token=lease.send_token)])
    ch = _channel(db, body["channel_id"])
    assert (ch["ext_id"], ch["archived"], ch["my_msg_count"]) == ("18000000005", 0, 1)
    assert _message(db, body["message_id"])["ext_id"] == mid


# --- тест 12 + П2: судьба заготовки


async def test_12_cancel_archives_attempted_unknown_does_not(client, db, clock, env):
    body = await _tg_draft(client, env)
    r = await client.post(f"/api/v1/outbox/{body['outbox_id']}/cancel", headers=_auth(env["token"]))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _channel(db, body["channel_id"])["archived"] == 1

    body2 = await _tg_draft(client, env)  # отменённая не мешает уникальности
    lease = _approve_and_lease(db, clock, body2["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="temporary", error="net")
    status = db.write_sync(lambda conn: outbox.cancel(conn, body2["outbox_id"], now_ms=_now(clock)))
    assert status == "unknown"
    assert _channel(db, body2["channel_id"])["archived"] == 0


async def test_p2_failed_archives_and_allows_new_draft(client, db, clock, env):
    body = await _tg_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(
        db, clock, lease, outcome="permanent", error="человек принимает личку только от контактов/Premium"
    )
    assert _outbox(db, body["outbox_id"])["status"] == "failed"
    assert _channel(db, body["channel_id"])["archived"] == 1
    again = await _tg_draft(client, env)
    assert again["channel_id"] != body["channel_id"]

    lease = _approve_and_lease(db, clock, again["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="unknown")
    db.write_sync(
        lambda conn: outbox.resolve_unknown(conn, again["outbox_id"], decision="not_sent", now_ms=_now(clock))
    )
    assert _channel(db, again["channel_id"])["archived"] == 1


# --- тест 13: коннекторам не видна


async def test_13_invisible_to_connectors(client, db, clock, env):
    body = await _mail_draft(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["mail"])
    mid = msgid.our_message_id(lease.send_token, MAIL_ADDRESS)
    _report(db, clock, lease, ext_id=mid)  # у строки заготовки есть ext_id, беседа — всё ещё заготовка
    temp_ext = _channel(db, body["channel_id"])["ext_id"]
    plain = insert_channel(db, env["mail"], ext_id="18000000009", kind="mail_thread")
    plain_msg = insert_message(db, plain, ext_id="<m1@x>", ts=1)

    with db.reader() as conn:
        heads = queries.connector_channel_heads(conn, env["mail"])
        assert [h["ext_id"] for h in heads] == ["18000000009"]
        assert queries.connector_channel_window(conn, env["mail"], temp_ext, 30) == []
    assert db.write_sync(
        lambda conn: read_ack.enqueue(conn, body["channel_id"], body["message_id"], _now(clock))
    ) == ("unsupported")
    with pytest.raises(reactions.NotReactable):
        db.write_sync(lambda conn: reactions.enqueue(conn, body["message_id"], "👍", True, _now(clock)))

    def _media_rows(conn):
        for message_id in (body["message_id"], plain_msg):
            conn.execute(
                "INSERT INTO main.media (message_id, account_id, idx, kind, status, created_ts) "
                "VALUES (?, ?, 0, 'photo', 'pending', 0)",
                (message_id, env["mail"]),
            )

    db.write_sync(_media_rows)
    jobs = db.write_sync(lambda conn: media.lease(conn, account_id=env["mail"], now_ms=_now(clock), limit=10))
    assert [job["message"]["channel_ext_id"] for job in jobs] == ["18000000009"]

    # событие коннектора с временным ext_id — ядовитое, в беседу-заготовку не попадает
    result = _ingest(
        db,
        clock,
        env["mail"],
        [_mail_event("<x@y>", 1) | {"channel": {"ext_id": temp_ext, "kind": "mail_thread"}}],
    )
    assert result.dead == 1
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.message WHERE ext_id = '<x@y>'").fetchone()[0] == 0
