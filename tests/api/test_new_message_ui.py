"""Ф12-5: «Новое сообщение» из хаба — `POST /api/v1/channels/new` (пустая беседа-заготовка UI), первое
сообщение обычным `messages/send`, живая строка outbox и `can_reply`, судьба UI-заготовки после неудачи, файлы
в новое письмо, уборка, «Удалить беседу из хаба», `/status` (`docs/architecture.md`, тесты
D 1–10 и поправки П1–П4, П6).
"""

import json
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import draft_channels, ingest, new_recipient, outbox
from hub.core.draft_channels import TEMP_PREFIX

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_project,
    login_via_code,
    make_bearer_token,
)
from .test_send_files import _upload as _raw_upload


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
MIB = 1024 * 1024
NEW_REASON = "новая беседа — сначала уйдёт первое сообщение"


async def _upload(client, cookie, channel_id, body, *, name, content_type):
    """Имя файла — urlencoded UTF-8 в `X-Filename`, как шлёт UI."""
    return await _raw_upload(client, cookie, channel_id, body, name=quote(name), content_type=content_type)


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, source, *, config=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )


@pytest.fixture
async def bearer(app):
    """Второй клиент того же приложения — без cookie (cookie и bearer вместе — 400)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def env(client, db, clock):
    project = insert_project(db, name="Umbrella")
    other_project = insert_project(db, name="Initech")
    tg = _account(db, "telegram", config={"idempotent_send": True, "pace_window": "00:00-24:00"})
    mail = _account(db, "mail", config={"address": MAIL_ADDRESS, "pace_window": "00:00-24:00"})
    cookie, _csrf = await login_via_code(client, db, clock)
    return {"project": project, "other_project": other_project, "tg": tg, "mail": mail, "cookie": cookie}


async def _new(client, env, **body):
    return await client.post("/api/v1/channels/new", json=body, headers=csrf_headers(env["cookie"]))


async def _new_tg(client, env, to="@Petr_Ivanov", project=None):
    r = await _new(client, env, account_id=env["tg"], project_id=project or env["project"], to=to)
    assert r.status_code == 200, r.text
    return r.json()


async def _new_mail(client, env, to="client@example.com", subject="Подпись для ООО", project=None):
    r = await _new(
        client, env, account_id=env["mail"], project_id=project or env["project"], to=to, subject=subject
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _send(client, env, channel_id, *, text="Здравствуйте!", key="k1", files=None):
    body = {"channel_id": channel_id, "text": text, "idempotency_key": key}
    if files is not None:
        body["files"] = files
    return await client.post("/api/v1/messages/send", json=body, headers=csrf_headers(env["cookie"]))


def _row(db, table, row_id) -> dict:
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM main.{table} WHERE id = ?", (row_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _outbox_rows(db, channel_id) -> list[dict]:
    with db.reader() as conn:
        cur = conn.execute("SELECT * FROM main.outbox WHERE channel_id = ? ORDER BY id", (channel_id,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _audit(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, channel_id, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [{"result": r[0], "channel_id": r[1], "detail": json.loads(r[2] or "null")} for r in rows]


def _lease(db, clock, account_id):
    return db.write_sync(
        lambda conn: outbox.lease(conn, account_id=account_id, owner=OWNER, now_ms=_now(clock))
    )


def _report(db, clock, lease, outcome="sent", **kw):
    db.write_sync(
        lambda conn: outbox.report(
            conn, outbox_id=lease.outbox_id, owner=OWNER, lease_gen=lease.lease_gen, outcome=outcome,
            now_ms=_now(clock), **kw,
        )
    )  # fmt: skip


async def _channel(client, channel_id) -> dict:
    r = await client.get(f"/api/v1/channels/{channel_id}")
    assert r.status_code == 200, r.text
    return r.json()


# --- тест 1: пустая беседа-заготовка ----------------------------------------------------------------------


async def test_1_channels_new_telegram_and_mail(client, db, env):
    body = await _new_tg(client, env)
    assert body["created"] is True
    ch = body["channel"]
    assert ch["ext_id"].startswith(TEMP_PREFIX) and ch["kind"] == "dm" and ch["title"] == "@Petr_Ivanov"
    assert ch["project_id"] == env["project"] and ch["source"] == "telegram"
    assert ch["can_reply"] is True and "can_reply_reason" not in ch
    assert ch["new_to"] == {"kind": "tg_username", "username": "Petr_Ivanov"}
    row = _row(db, "channel", ch["id"])
    meta = json.loads(row["meta_json"])
    assert (row["map_source"], row["map_reason"]) == ("manual", "новая беседа")
    assert meta["new_draft"] == {"to": ch["new_to"], "origin": "ui"}
    assert _outbox_rows(db, ch["id"]) == []

    mail = (await _new_mail(client, env))["channel"]
    assert mail["kind"] == "mail_thread" and mail["title"] == "Подпись для ООО → client@example.com"
    assert mail["new_to"] == {
        "kind": "email",
        "to": "client@example.com",
        "subject": "Подпись для ООО",
        "from": None,
    }
    assert mail["can_reply"] is True

    audit = _audit(db, "channel_new")
    assert [a["detail"]["to_kind"] for a in audit] == ["tg_username", "email"]
    assert audit[0]["detail"] == {
        "account_id": env["tg"], "project_id": env["project"], "to_kind": "tg_username", "created": True,
    }  # fmt: skip
    raw = json.dumps([a["detail"] for a in audit], ensure_ascii=False)
    assert "Petr" not in raw and "client@" not in raw and "Подпись" not in raw
    # в списке проекта видна
    listed = (await client.get(f"/api/v1/projects/{env['project']}/channels")).json()
    assert {c["id"] for c in listed} >= {ch["id"], mail["id"]}


# --- тест 2: доступ и проверки ------------------------------------------------------------------------------


async def test_2_bearer_forbidden_with_audit(bearer, db, env):
    token = make_bearer_token(db, scopes=("read", "draft", "send", "organize"), name="mcp:all")
    r = await bearer.post(
        "/api/v1/channels/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@Petr_Ivanov"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    denied = _audit(db, "ui_only_denied")
    assert len(denied) == 1 and denied[0]["detail"]["path"] == "/api/v1/channels/new"
    assert _audit(db, "channel_new") == []


async def test_2_without_csrf_refused(client, db, env):
    r = await client.post(
        "/api/v1/channels/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@Petr_Ivanov"},
    )
    assert r.status_code == 403
    assert _audit(db, "channel_new") == []


@pytest.mark.parametrize(
    ("which", "body"),
    [
        ("tg", {"project_id": 0, "to": "@Petr_Ivanov"}),  # Ф14-2 A2: `null` — «по правилам», `≤ 0` — 422
        ("tg", {"project_id": -1, "to": "@Petr_Ivanov"}),
        ("tg", {"project_id": 9999, "to": "@Petr_Ivanov"}),
        ("tg", {"to": "@a b"}),
        ("tg", {"to": "@Petr_Ivanov", "subject": "Тема"}),
        ("mail", {"to": "client@example.com"}),
        ("mail", {"to": "Пётр <client@example.com>", "subject": "Тема"}),
        ("mail", {"to": "client@example.com", "subject": "Тема\r\nBcc: x@y.z"}),
    ],
)
async def test_2_validation_422(client, db, env, which, body):
    payload = {"account_id": env[which], "project_id": env["project"], **body}
    r = await _new(client, env, **payload)
    assert r.status_code == 422, r.text


async def test_2_wrong_source_and_receive_only_422(client, db, env):
    for source in ("slack", "fake"):
        acc = _account(db, source)
        r = await _new(client, env, account_id=acc, project_id=env["project"], to="@Petr_Ivanov")
        assert r.status_code == 422, (source, r.text)
    receive_only = _account(db, "mail", config={"address": "ro@gmail.com", "send_enabled": False})
    r = await _new(
        client, env, account_id=receive_only, project_id=env["project"], to="a@example.com", subject="Тема"
    )
    assert r.status_code == 422
    assert "только на приём" in r.json()["detail"]


async def test_2_limit_101st_is_429(client, db, clock, env):
    def _fill(conn):
        for i in range(new_recipient.MAX_OPEN_DRAFTS):
            new_recipient.create_channel(
                conn, account_id=env["tg"], project_id=env["project"], to=f"@user_{i:04d}", subject=None,
                now_ms=_now(clock),
            )  # fmt: skip

    db.write_sync(_fill)
    r = await _new(client, env, account_id=env["tg"], project_id=env["project"], to="@user_x101")
    assert r.status_code == 429


# --- тест 3: повтор формы тому же адресату ------------------------------------------------------------------


async def test_3_repeat_returns_same_channel(client, db, env):
    first = (await _new_tg(client, env, to="@Petr_Ivanov"))["channel"]
    again = await _new_tg(client, env, to="t.me/petr_ivanov")
    assert again["created"] is False and again["channel"]["id"] == first["id"]
    other_project = await _new_tg(client, env, to="@Petr_Ivanov", project=env["other_project"])
    assert other_project["created"] is True and other_project["channel"]["id"] != first["id"]

    mail = (await _new_mail(client, env, subject="Тема 1"))["channel"]
    assert (await _new_mail(client, env, subject="тема 1"))["channel"]["id"] == mail["id"]
    other_subject = await _new_mail(client, env, subject="Тема 2")
    assert other_subject["created"] is True and other_subject["channel"]["id"] != mail["id"]
    assert [a["result"] for a in _audit(db, "channel_new")] == [
        "created", "existing", "created", "created", "existing", "created",
    ]  # fmt: skip


# --- тест 4 + П2: первое сообщение, живая строка, повтор после неудачи --------------------------------------


async def test_4_send_live_row_and_retry_after_failed(client, db, clock, env):
    ch = (await _new_tg(client, env))["channel"]
    cursor = (await client.get("/api/v1/sync", params={"since": 0})).json()["max_seq"]

    r = await _send(client, env, ch["id"], key="k1")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued" and r.json()["paced"] is False
    (row,) = _outbox_rows(db, ch["id"])
    assert (row["status"], row["paced"], row["origin"]) == ("queued", 0, "ui")

    one = await _channel(client, ch["id"])
    assert one["can_reply"] is False and one["can_reply_reason"] == NEW_REASON
    listed = next(
        c
        for c in (await client.get(f"/api/v1/projects/{env['project']}/channels")).json()
        if c["id"] == ch["id"]
    )
    assert listed["can_reply"] is False
    # П2: `/sync` видит смену `can_reply` (постановка двигает seq беседы-заготовки)
    sync = (await client.get("/api/v1/sync", params={"since": cursor})).json()
    synced = next(c for c in sync["channels"] if c["id"] == ch["id"])
    assert synced["can_reply"] is False and synced["can_reply_reason"] == NEW_REASON
    cursor = sync["max_seq"]

    r = await _send(client, env, ch["id"], key="k2")
    assert r.status_code == 409

    (lease,) = _lease(db, clock, env["tg"])
    assert lease.new_to == {"kind": "tg_username", "username": "Petr_Ivanov"}
    _report(db, clock, lease, outcome="permanent", error="нет такого @username")
    assert _row(db, "outbox", lease.outbox_id)["status"] == "failed"
    assert _row(db, "channel", ch["id"])["archived"] == 0  # A3: UI-заготовка не в архиве

    sync = (await client.get("/api/v1/sync", params={"since": cursor})).json()
    synced = next(c for c in sync["channels"] if c["id"] == ch["id"])
    assert synced["can_reply"] is True and "can_reply_reason" not in synced
    assert (await _channel(client, ch["id"]))["can_reply"] is True

    r = await _send(client, env, ch["id"], text="Попробую ещё раз", key="k3")
    assert r.status_code == 200, r.text
    assert [o["status"] for o in _outbox_rows(db, ch["id"])] == ["failed", "queued"]


async def test_4_cancelled_ui_draft_stays_open(client, db, clock, env):
    ch = (await _new_mail(client, env))["channel"]
    sent = (await _send(client, env, ch["id"])).json()
    r = await client.post(f"/api/v1/outbox/{sent['outbox_id']}/cancel", headers=csrf_headers(env["cookie"]))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _row(db, "channel", ch["id"])["archived"] == 0
    assert (await _channel(client, ch["id"]))["can_reply"] is True


async def test_p2_sent_unbound_reason(client, db, clock, env):
    """Почта ушла, копию не нашли: поле закрыто своей причиной до привязки (П9)."""
    ch = (await _new_mail(client, env))["channel"]
    await _send(client, env, ch["id"])
    (lease,) = _lease(db, clock, env["mail"])
    _report(db, clock, lease, ext_id="<hub-x@gmail.com>")
    one = await _channel(client, ch["id"])
    assert one["ext_id"].startswith(TEMP_PREFIX)
    assert one["can_reply"] is False
    assert one["can_reply_reason"] == "сообщение ушло — беседа привяжется, когда придёт его копия"
    assert (await _send(client, env, ch["id"], key="k2")).status_code == 409


# --- тест 5: заготовка Claude — как было --------------------------------------------------------------------


async def test_5_claude_draft_failed_still_archived(bearer, client, db, clock, env):
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[env["project"]], name="mcp:iip")
    r = await bearer.post(
        "/api/v1/drafts/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@Claude_Target", "text": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    db.write_sync(lambda conn: outbox.approve(conn, body["outbox_id"], now_ms=_now(clock)))
    (lease,) = _lease(db, clock, env["tg"])
    _report(db, clock, lease, outcome="permanent", error="нет")
    assert _row(db, "channel", body["channel_id"])["archived"] == 1


# --- тест 6: файлы ------------------------------------------------------------------------------------------


def _pdf(tag: str, size: int = 400) -> bytes:
    head = b"%PDF-1.7\n" + tag.encode()
    return head + b"p" * (size - len(head))


async def test_6_mail_draft_with_two_files(client, db, clock, env):
    ch = (await _new_mail(client, env))["channel"]
    ids = []
    for i, name in enumerate(("счёт.pdf", "акт.pdf")):
        r = await _upload(
            client, env["cookie"], ch["id"], _pdf(f"f{i}"), name=name, content_type="application/pdf"
        )
        assert r.status_code == 200, r.text
        ids.append(r.json()["upload_id"])
    r = await _send(client, env, ch["id"], text="Во вложении", files=ids)
    assert r.status_code == 200, r.text
    rows = _outbox_rows(db, ch["id"])
    assert len(rows) == 1
    (lease,) = _lease(db, clock, env["mail"])
    assert [f["name"] for f in lease.files] == ["счёт.pdf", "акт.pdf"]
    assert lease.new_to["kind"] == "email" and lease.new_to["to"] == "client@example.com"


async def test_6_mail_draft_files_over_17_mib_422(client, db, env):
    ch = (await _new_mail(client, env))["channel"]
    ids = []
    for i in range(2):
        r = await _upload(
            client,
            env["cookie"],
            ch["id"],
            _pdf(f"b{i}", 9 * MIB),
            name=f"{i}.pdf",
            content_type="application/pdf",
        )
        assert r.status_code == 200, r.text
        ids.append(r.json()["upload_id"])
    r = await _send(client, env, ch["id"], text="", files=ids)
    assert r.status_code == 422
    assert _outbox_rows(db, ch["id"]) == []


# Ф14-2 A4: файлы в Telegram-заготовку теперь принимаются — тест 6 Ф14-2 (`test_new_anywhere.py`).


# --- тест 7 + П8: уборка ------------------------------------------------------------------------------------


def _cleanup(db, clock) -> int:
    return db.write_sync(lambda conn: draft_channels.cleanup_idle(conn, now_ms=_now(clock)))


async def test_7_cleanup_idle_ui_drafts(bearer, client, db, clock, env):
    idle = (await _new_tg(client, env, to="@Idle_Man"))["channel"]
    with_live = (await _new_tg(client, env, to="@Live_Man"))["channel"]
    await _send(client, env, with_live["id"])
    failed = (await _new_mail(client, env, subject="Не ушло"))["channel"]
    await _send(client, env, failed["id"], key="kf")
    (lease,) = _lease(db, clock, env["mail"])
    _report(db, clock, lease, outcome="permanent", error="smtp 550")
    with_upload = (await _new_mail(client, env, subject="С файлом"))["channel"]
    # заготовка Claude, чья строка ушла в конечный статус мимо `settle` (архива нет), — уборка её не трогает
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[env["project"]], name="mcp:iip")
    claude = (
        await bearer.post(
            "/api/v1/drafts/new",
            json={"account_id": env["tg"], "project_id": env["project"], "to": "@Claude_Man", "text": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET status = 'failed' WHERE id = ?", (claude["outbox_id"],)
        )
    )

    clock.advance_ms(draft_channels.IDLE_MS - 60_000)
    fresh = (await _new_tg(client, env, to="@Fresh_Man"))["channel"]
    r = await _upload(
        client, env["cookie"], with_upload["id"], _pdf("u"), name="u.pdf", content_type="application/pdf"
    )
    assert r.status_code == 200, r.text
    assert _cleanup(db, clock) == 0  # ещё никто не старше суток

    clock.advance_ms(2 * 60_000)
    assert _cleanup(db, clock) == 2
    archived = {cid for cid in (idle["id"], failed["id"]) if _row(db, "channel", cid)["archived"]}
    assert archived == {idle["id"], failed["id"]}
    for cid in (with_live["id"], with_upload["id"], fresh["id"], claude["channel_id"]):
        assert _row(db, "channel", cid)["archived"] == 0, cid
    assert {a["result"] for a in _audit(db, "draft_channel_archive")} == {"idle"}


async def test_7_cleanup_counts_late_failure_as_event(client, db, clock, env):
    """Ревью Ф12-5: `unknown`, через сутки владелец решает «не ушло» -> `failed` — беседа открыта ещё сутки от
    момента неудачи (`send_started_ts` — первая попытка, по нему одному уборка убрала бы её сразу)."""
    ch = (await _new_mail(client, env))["channel"]
    assert (await _send(client, env, ch["id"])).status_code == 200
    (lease,) = _lease(db, clock, env["mail"])
    _report(db, clock, lease, outcome="unknown")
    clock.advance_ms(draft_channels.IDLE_MS + 60 * 60_000)
    db.write_sync(
        lambda conn: outbox.resolve_unknown(conn, lease.outbox_id, decision="not_sent", now_ms=_now(clock))
    )
    assert _row(db, "channel", ch["id"])["archived"] == 0
    assert _cleanup(db, clock) == 0
    clock.advance_ms(draft_channels.IDLE_MS - 60_000)
    assert _cleanup(db, clock) == 0
    clock.advance_ms(2 * 60_000)
    assert _cleanup(db, clock) == 1
    assert _row(db, "channel", ch["id"])["archived"] == 1


# --- тест 8 + П3: «Удалить беседу из хаба» ------------------------------------------------------------------


async def _delete(client, env, channel_id):
    return await client.delete(f"/api/v1/channels/{channel_id}", headers=csrf_headers(env["cookie"]))


async def test_8_delete_empty_draft_archives_and_frees_limit(client, db, clock, env):
    def _fill(conn):
        for i in range(new_recipient.MAX_OPEN_DRAFTS - 1):
            new_recipient.create_channel(
                conn, account_id=env["tg"], project_id=env["project"], to=f"@user_{i:04d}", subject=None,
                now_ms=_now(clock),
            )  # fmt: skip

    db.write_sync(_fill)
    ch = (await _new_tg(client, env))["channel"]
    assert (
        await _new(client, env, account_id=env["tg"], project_id=env["project"], to="@One_More")
    ).status_code == 429

    r = await _delete(client, env, ch["id"])
    assert r.status_code == 204
    assert _row(db, "channel", ch["id"])["archived"] == 1
    assert [a["result"] for a in _audit(db, "draft_channel_archive")] == ["hidden"]
    listed = (await client.get(f"/api/v1/projects/{env['project']}/channels")).json()
    assert ch["id"] not in {c["id"] for c in listed}
    assert (
        await _new(client, env, account_id=env["tg"], project_id=env["project"], to="@One_More")
    ).status_code == 200


async def test_8_deleted_draft_same_recipient_gets_new_channel(client, db, env):
    ch = (await _new_tg(client, env))["channel"]
    assert (await _delete(client, env, ch["id"])).status_code == 204
    again = await _new_tg(client, env)  # архивная уникальности не мешает
    assert again["created"] is True and again["channel"]["id"] != ch["id"]


async def test_8_delete_draft_in_flight_409_and_regular_unchanged(client, db, clock, env):
    ch = (await _new_tg(client, env))["channel"]
    await _send(client, env, ch["id"])
    _lease(db, clock, env["tg"])  # выдана коннектору — в полёте
    r = await _delete(client, env, ch["id"])
    assert r.status_code == 409
    assert _row(db, "channel", ch["id"])["archived"] == 0

    plain = insert_channel(db, env["tg"], ext_id="777", kind="dm", project_id=env["project"])
    assert (await _delete(client, env, plain)).status_code == 204
    row = _row(db, "channel", plain)
    assert row["archived"] == 0 and row["hidden_before_ts"] is not None


async def test_p3_delete_draft_with_failed_file_message_scrubs_and_archives(client, db, clock, env):
    ch = (await _new_mail(client, env))["channel"]
    r = await _upload(
        client, env["cookie"], ch["id"], _pdf("s"), name="секрет.pdf", content_type="application/pdf"
    )
    sent = (await _send(client, env, ch["id"], text="тайный текст", files=[r.json()["upload_id"]])).json()
    (lease,) = _lease(db, clock, env["mail"])
    _report(db, clock, lease, outcome="permanent", error="smtp 552")
    with db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM main.media WHERE message_id = ?", (sent["message_id"],)
            ).fetchone()[0]
            == 1
        )

    assert (await _delete(client, env, ch["id"])).status_code == 204
    msg = _row(db, "message", sent["message_id"])
    assert msg["hidden"] == 1 and "тайный" not in (msg["text"] or "")
    ob = _row(db, "outbox", sent["outbox_id"])
    assert ob["text"] == "" and "секрет" not in (ob["file_json"] or "")
    with db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM main.media WHERE message_id = ?", (sent["message_id"],)
            ).fetchone()[0]
            == 0
        )
    assert _row(db, "channel", ch["id"])["archived"] == 1


# --- тест 9: `/status` --------------------------------------------------------------------------------------


async def test_9_status_new_message_fields(bearer, client, db, env):
    receive_only = _account(db, "mail", config={"address": "ro@gmail.com", "send_enabled": False})
    slack = _account(db, "slack")
    accounts = {a["id"]: a for a in (await client.get("/api/v1/status")).json()["accounts"]}
    assert (accounts[env["tg"]]["new_message"], accounts[env["tg"]]["address"]) == (True, None)
    assert (accounts[env["mail"]]["new_message"], accounts[env["mail"]]["address"]) == (True, MAIL_ADDRESS)
    assert (accounts[receive_only]["new_message"], accounts[receive_only]["address"]) == (
        False,
        "ro@gmail.com",
    )
    assert accounts[slack]["new_message"] is False

    token = make_bearer_token(db, scopes=("read",), name="ro")
    got = (await bearer.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})).json()[
        "accounts"
    ]
    assert got and all("new_message" not in a and "address" not in a for a in got)


# --- тест 10: привязка после отправки у UI-заготовки --------------------------------------------------------


async def test_10_ui_mail_draft_sent_with_peer_renames(client, db, clock, env):
    ch = (await _new_mail(client, env))["channel"]
    sent = (await _send(client, env, ch["id"])).json()
    (lease,) = _lease(db, clock, env["mail"])
    _report(db, clock, lease, ext_id="<hub-y@gmail.com>", peer_ext_id="18000000009")
    row = _row(db, "channel", ch["id"])
    assert row["ext_id"] == "18000000009" and row["archived"] == 0
    assert "new_to" not in (json.loads(row["meta_json"]).get("attrs") or {})
    assert _row(db, "message", sent["message_id"])["ext_id"] == "<hub-y@gmail.com>"
    one = await _channel(client, ch["id"])
    assert one["can_reply"] is True and "new_to" not in one


# --- П1: проект при слиянии — отсчёт по привязывающей строке ------------------------------------------------


def _dm_event(ext_id, chat, *, ts, is_me=False, text="Привет"):
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "dm", "title": "Пётр"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


async def test_p1_merge_after_failed_attempt_keeps_other_project(client, db, clock, env):
    ch = (await _new_tg(client, env))["channel"]
    await _send(client, env, ch["id"], key="k1")
    (lease,) = _lease(db, clock, env["tg"])  # первая попытка — send_started_ts
    _report(db, clock, lease, outcome="permanent", error="приватность")

    # человек сам написал владельцу (личка появилась ПОСЛЕ первой попытки), её разложили в другой проект
    clock.advance_ms(60_000)
    db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=env["tg"], events=[_dm_event("100", "555", ts=_now(clock))], cursor=None,
            now_ms=_now(clock),
        )
    )  # fmt: skip
    with db.reader() as conn:
        dm = conn.execute("SELECT id FROM main.channel WHERE ext_id = '555'").fetchone()[0]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'auto' WHERE id = ?",
            (env["other_project"], dm),
        )
    )

    clock.advance_ms(60_000)
    await _send(client, env, ch["id"], text="Ещё раз", key="k2")
    (lease2,) = _lease(db, clock, env["tg"])
    _report(db, clock, lease2, ext_id="9001", peer_ext_id="555", peer_title="Пётр")

    assert _row(db, "channel", dm)["project_id"] == env["other_project"]  # проект лички не тронут
    assert _row(db, "outbox", lease2.outbox_id)["channel_id"] == dm
    kept = _audit(db, "draft_merge_other_project")
    assert len(kept) == 1 and kept[0]["channel_id"] == dm and kept[0]["detail"]["fresh"] is False


# --- П4: `hub_draft_new` на пустую UI-заготовку -------------------------------------------------------------


async def test_p4_draft_new_on_empty_ui_draft_409_without_none(bearer, client, db, env):
    ch = (await _new_tg(client, env))["channel"]
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[env["project"]], name="mcp:iip")
    r = await bearer.post(
        "/api/v1/drafts/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@petr_ivanov", "text": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert f"беседа #{ch['id']} этому адресату уже заведена" in detail and "hub_draft" in detail
    assert "None" not in detail


# --- П6: bearer в пустую UI-заготовку — всегда по графику ---------------------------------------------------


async def test_p6_bearer_send_into_empty_ui_draft_is_paced(bearer, client, db, env):
    ch = (await _new_tg(client, env))["channel"]
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[env["project"]], name="mcp:iip")
    r = await bearer.post(
        "/api/v1/messages/send",
        json={"channel_id": ch["id"], "text": "Здравствуйте!", "idempotency_key": "b1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval" and body["paced"] is True
    assert _row(db, "outbox", body["outbox_id"])["paced"] == 1
    sends = _audit(db, "message_send")
    assert sends[-1]["detail"]["paced"] is True
    # повтор тем же ключом — тот же фактический `paced`
    r = await bearer.post(
        "/api/v1/messages/send",
        json={"channel_id": ch["id"], "text": "Здравствуйте!", "idempotency_key": "b1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["paced"] is True and r.json()["created"] is False
