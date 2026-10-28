"""Ф14-8 (docs/architecture.md): очередь «правка / удалить у всех» своих сообщений — раздел E,
пункты 1–7, с поправками П1 (скрытое — 404, скрытие со строкой `msg_op` — 409), П5 (лиз 120 с), П6 (что
правится в Telegram).

UI-операция идёт через публичное приложение под cookie-сессией, выдача и отчёт — через внутреннее (секрет
коннектора) на той же БД и тех же часах."""

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth
from hub.core import db as db_module
from hub.core import msg_ops as msg_ops_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db, db_of
from hub.core.feed import Notifier

from .conftest import csrf_headers, insert_account, login_via_code, make_bearer_token

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _account(db, *, source="telegram", db_name="main", status="ok") -> tuple[int, dict]:
    account_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, status) VALUES (?, ?, ?)", (source, db_name, status)
            ).lastrowid
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


def _set_account(db, account_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE main.account SET {sets} WHERE id = ?",  # noqa: S608 — имена колонок из теста
            (*cols.values(), account_id),
        )
    )


def _up(ch, ext_id, ts, *, kind="dm", is_me=True, event_kind="text", text="старый текст", **extra) -> dict:
    event = {
        "type": "upsert",
        "channel": {"ext_id": ch, "kind": kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": event_kind,
        "text": text,
        "mentions_me": False,
    }
    event.update(extra)
    return event


async def _events(ic, h, events):
    r = await ic.post(f"{INTERNAL}/events", headers=h, json={"events": events})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


def _channel_id(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (ext_id,),
        ).fetchone()[0]


def _mid(db, channel_id, ext_id):
    schema = db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext_id),
        ).fetchone()[0]


def _seq(db, message_id) -> int:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT seq FROM {db_of(message_id)}.message WHERE id = ?",  # noqa: S608
            (message_id,),
        ).fetchone()[0]


def _row(db, message_id):
    schema = db_of(message_id)
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.msg_op WHERE message_id = ?", (message_id,))  # noqa: S608
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row, strict=True))


def _count_rows(db) -> int:
    with db.reader() as conn:
        return sum(
            conn.execute(f"SELECT COUNT(*) FROM {schema}.msg_op").fetchone()[0]  # noqa: S608
            for schema in ("main", "work")
        )


def _now_ms(clock) -> int:
    return int(clock() * 1000)


async def _ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


async def _edit(client, ui, message_id, text, *, status=200):
    r = await client.post(f"{API}/messages/{message_id}/edit", json={"text": text}, headers=ui)
    assert r.status_code == status, r.text
    return r.json()


async def _revoke(client, ui, ids, *, status=200):
    r = await client.post(f"{API}/messages/revoke", json={"message_ids": ids}, headers=ui)
    assert r.status_code == status, r.text
    return r.json()


async def _cancel(client, ui, message_id, *, status=204):
    r = await client.delete(f"{API}/messages/{message_id}/op", headers=ui)
    assert r.status_code == status, r.text
    return r


async def _lease(ic, h, limit=None):
    body = {} if limit is None else {"limit": limit}
    r = await ic.post(f"{INTERNAL}/msg-ops/lease", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, message_id, lease_gen, outcome, *, status=200, **extra):
    body = {"lease_gen": lease_gen, "outcome": outcome, **extra}
    r = await ic.post(f"{INTERNAL}/msg-ops/{message_id}/report", headers=h, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, channel_id, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


async def _simple(db, ic, *, source="telegram", ch="c1", kind="dm", db_name="main", events=None):
    """Аккаунт и беседа: свои `1`, `2` и чужое `3` (или свои события `events`)."""
    acc, h = _account(db, source=source, db_name=db_name)
    if events is None:
        events = [_up(ch, "1", 1000, kind=kind), _up(ch, "2", 2000, kind=kind)]
        events.append(_up(ch, "3", 3000, kind=kind, is_me=False))
    await _events(ic, h, events)
    return acc, h, _channel_id(db, ch, db_name)


# --- E1: постановка, `op` в выдаче, смена строки двигает seq ---


async def test_e1_edit_queues_row_and_op_in_feed_sync_and_thread(client, ic, db, clock):
    acc, h, ch = await _simple(
        db,
        ic,
        source="slack",
        kind="group",
        events=[
            _up("C1", "100.000100", 1000, kind="group", thread_ext_id="100.000100"),
            _up("C1", "100.000200", 2000, kind="group", thread_ext_id="100.000100"),
        ],
        ch="C1",
    )
    parent, reply = _mid(db, ch, "100.000100"), _mid(db, ch, "100.000200")
    ui = await _ui(client, db, clock)
    seq0 = _seq(db, reply)
    t0 = _now_ms(clock)

    out = await _edit(client, ui, reply, "новый текст")
    op = {"kind": "edit", "state": "queued", "text": "новый текст", "error": None}
    assert out == {"status": "queued", "op": op}
    row = _row(db, reply)
    assert row["op"] == "edit" and row["text"] == "новый текст" and row["state"] == "queued"
    assert row["gen"] == 1 and row["attempts"] == 0 and row["account_id"] == acc and row["channel_id"] == ch
    assert row["not_before_ms"] == t0 and row["created_ms"] == t0  # без задержки (решение 8)
    assert _seq(db, reply) > seq0

    # тред (ответ), лента (родитель — без `op`), `/sync` — одинаково
    thread = (await client.get(f"{API}/channels/{ch}/threads/100.000100/messages")).json()
    assert [m["op"] for m in thread["messages"]] == [op]
    assert thread["parent"]["op"] is None
    feed = (await client.get(f"{API}/channels/{ch}/messages")).json()
    assert {m["id"]: m["op"] for m in feed["messages"]}[parent] is None
    sync = (await client.get(f"{API}/sync", params={"since": 0})).json()
    assert {m["id"]: m["op"] for m in sync["messages"]}[reply] == op
    single = (await client.get(f"{API}/messages/{reply}")).json()
    assert single["op"] == op
    # сам текст сообщения ядро не трогало (решение 3)
    assert single["text"] == "старый текст"

    # неудача -> вторая правка: gen вырос, попытки заново, ошибка снята, seq снова вырос
    (item,) = await _lease(ic, h)
    await _report(ic, h, reply, item["lease_gen"], "retry", detail="net")
    await _report(ic, h, reply, item["lease_gen"], "fail", code="expired", detail="x", status=409)  # лиз снят
    clock.advance_ms(30_000)
    (item,) = await _lease(ic, h)
    seq1 = _seq(db, reply)
    await _report(ic, h, reply, item["lease_gen"], "fail", code="expired", detail="MessageEditTimeExpired")
    assert _seq(db, reply) > seq1
    row = _row(db, reply)
    assert row["state"] == "failed" and row["error_code"] == "expired" and row["attempts"] == 1
    seq2 = _seq(db, reply)
    out = await _edit(client, ui, reply, "третий текст")
    row = _row(db, reply)
    assert row["gen"] == 2 and row["attempts"] == 0 and row["text"] == "третий текст"
    assert row["state"] == "queued" and row["error_code"] is None and row["error_detail"] is None
    assert _seq(db, reply) > seq2
    assert out["op"] == {"kind": "edit", "state": "queued", "text": "третий текст", "error": None}

    audit = _audit(db, "msg_edit")
    assert [(r[0], r[1], r[2]) for r in audit] == [
        ("queued", ch, {"message_id": reply, "len": len("новый текст")}),
        ("queued", ch, {"message_id": reply, "len": len("третий текст")}),
    ]
    assert "новый" not in json.dumps([r[2] for r in audit], ensure_ascii=False)


async def test_e1_text_kept_as_sent_without_strip(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "  с пробелами \n")
    assert _row(db, m)["text"] == "  с пробелами \n"


# --- E2: проверки правки ---


async def test_e2_not_editable_409(client, ic, db, clock):
    _acc, h, ch = await _simple(
        db,
        ic,
        events=[
            _up("c1", "1", 1000, is_me=False),  # чужое
            _up("c1", "2", 2000, event_kind="event", text="вошёл"),  # служебное
            _up("c1", "3", 3000, forward={"from_name": "Вася"}),  # пересланное
            _up("c1", "4", 4000),  # удалено ниже
            _up(
                "c1",
                "5",
                5000,
                event_kind="file",
                attachments=[{"type": "sticker"}],
            ),  # П6: стикер
            _up("c1", "6", 6000, event_kind="file", attachments=[{"type": "video", "round": True}]),  # кружок
            _up("c1", "7", 7000, event_kind="file", attachments=[{"type": "photo"}, {"type": "poll"}]),
        ],
    )
    await _events(ic, h, [{"type": "delete", "ext_ids": ["4"], "channel": {"ext_id": "c1", "kind": "dm"}}])
    ui = await _ui(client, db, clock)
    for ext in ("1", "2", "3", "4", "5", "6", "7"):
        m = _mid(db, ch, ext)
        r = await _edit(client, ui, m, "другое", status=409)
        assert r["detail"] == "not_editable", ext
        assert _row(db, m) is None


async def test_e2_no_ext_id_and_draft_channel_409(client, ic, db, clock):
    from .conftest import insert_channel, insert_message

    acc, _h = _account(db)
    ch = insert_channel(db, acc, ext_id="c1")
    no_ext = insert_message(db, ch, ext_id=None, ts=1000, is_me=1)
    draft_ch = insert_channel(db, acc, ext_id="new:abc")
    in_draft = insert_message(db, draft_ch, ext_id="9", ts=1000, is_me=1)
    ui = await _ui(client, db, clock)
    for mid in (no_ext, in_draft):
        r = await _edit(client, ui, mid, "другое", status=409)
        assert r["detail"] == "not_editable"
        r = await _revoke(client, ui, [mid], status=409)
        assert r["detail"] == "not_editable"
    assert _count_rows(db) == 0


async def test_e2_mail_unsupported_409(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic, source="mail", ch="t1", kind="mail_thread")
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    assert (await _edit(client, ui, m, "другое", status=409))["detail"] == "unsupported"
    assert (await _revoke(client, ui, [m], status=409))["detail"] == "unsupported"
    assert _row(db, m) is None


async def test_e2_telegram_file_caption_editable_for_photo_document(client, ic, db, clock):
    _acc, _h, ch = await _simple(
        db,
        ic,
        events=[
            _up(
                "c1",
                "1",
                1000,
                event_kind="file",
                attachments=[{"type": "photo"}, {"type": "document"}, {"type": "video"}],
            )
        ],
    )
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    assert (await _edit(client, ui, m, "подпись"))["status"] == "queued"


async def test_e2_slack_any_file_editable(client, ic, db, clock):
    _acc, _h, ch = await _simple(
        db,
        ic,
        source="slack",
        ch="C1",
        kind="group",
        events=[
            _up("C1", "1.000100", 1000, kind="group", event_kind="file", attachments=[{"type": "other"}])
        ],
    )
    m = _mid(db, ch, "1.000100")
    ui = await _ui(client, db, clock)
    assert (await _edit(client, ui, m, "подпись"))["status"] == "queued"


async def test_e2_hidden_404_unknown_404(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    assert (await client.delete(f"{API}/messages/{m}", headers=ui)).status_code == 204
    await _edit(client, ui, m, "другое", status=404)
    await _edit(client, ui, 999_999, "другое", status=404)
    assert _row(db, m) is None


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_e2_empty_422(client, ic, db, clock, text):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    r = await _edit(client, ui, m, text, status=422)
    assert r["detail"] == "empty"
    assert _row(db, m) is None


async def test_e2_too_long_telegram_utf16_and_caption_slack_chars(client, ic, db, clock):
    _acc, _h, tg = await _simple(
        db,
        ic,
        events=[
            _up("c1", "1", 1000),
            _up("c1", "2", 2000, event_kind="file", attachments=[{"type": "photo"}]),
        ],
    )
    _acc2, _h2, sl = await _simple(
        db, ic, source="slack", ch="C1", kind="group", db_name="work", events=[_up("C1", "1.0001", 1000)]
    )
    ui = await _ui(client, db, clock)
    text_m, file_m, slack_m = _mid(db, tg, "1"), _mid(db, tg, "2"), _mid(db, sl, "1.0001")

    # 4096 UTF-16 — можно; эмодзи — две единицы UTF-16: 2048 эмодзи + буква = 4097 при 2049 символах
    assert (await _edit(client, ui, text_m, "😀" * 2048))["status"] == "queued"
    r = await _edit(client, ui, text_m, "😀" * 2048 + "a", status=422)
    assert r["detail"].startswith("too_long") and "4096" in r["detail"]
    assert (await _edit(client, ui, file_m, "a" * 1024))["status"] == "queued"
    r = await _edit(client, ui, file_m, "a" * 1025, status=422)
    assert "1024" in r["detail"]
    assert (await _edit(client, ui, slack_m, "б" * 4000))["status"] == "queued"
    r = await _edit(client, ui, slack_m, "б" * 4001, status=422)
    assert "4000" in r["detail"]
    assert _row(db, text_m)["text"] == "😀" * 2048  # неудачная попытка ничего не записала


async def test_e2_same_text_without_row_unchanged(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    seq0 = _seq(db, m)
    ui = await _ui(client, db, clock)
    assert await _edit(client, ui, m, "старый текст") == {"status": "unchanged", "op": None}
    assert _row(db, m) is None and _seq(db, m) == seq0
    assert _audit(db, "msg_edit") == []


async def test_e2_bearer_403_ui_only(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    token = make_bearer_token(db, scopes=("read", "send", "draft", "organize"))
    bearer = {"Authorization": f"Bearer {token}"}
    r = await client.post(f"{API}/messages/{m}/edit", json={"text": "x"}, headers=bearer)
    assert r.status_code == 403
    r = await client.post(f"{API}/messages/revoke", json={"message_ids": [m]}, headers=bearer)
    assert r.status_code == 403
    r = await client.delete(f"{API}/messages/{m}/op", headers=bearer)
    assert r.status_code == 403
    assert _row(db, m) is None
    denied = _audit(db, "ui_only_denied")
    assert len(denied) == 3


async def test_e2_body_strict(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    for body in ({"text": 1}, {}, {"text": "x", "extra": 1}):
        r = await client.post(f"{API}/messages/{m}/edit", json=body, headers=ui)
        assert r.status_code == 422, body
    for body in ({"message_ids": []}, {"message_ids": ["1"]}, {"message_ids": list(range(1, 12))}):
        r = await client.post(f"{API}/messages/revoke", json=body, headers=ui)
        assert r.status_code == 422, body
    assert _count_rows(db) == 0


# --- E3: удалить у всех ---


async def test_e3_revoke_album_three_rows(client, ic, db, clock):
    _acc, _h, ch = await _simple(
        db,
        ic,
        events=[
            _up("c1", str(i), 1000 * i, event_kind="file", attachments=[{"type": "photo"}], grouped_id="g1")
            for i in (1, 2, 3)
        ],
    )
    ids = [_mid(db, ch, str(i)) for i in (1, 2, 3)]
    ui = await _ui(client, db, clock)
    out = await _revoke(client, ui, ids)
    assert out["status"] == "queued"
    assert out["ops"] == {str(i): {"kind": "delete", "state": "queued", "error": None} for i in ids}
    for mid in ids:
        row = _row(db, mid)
        assert row["op"] == "delete" and row["text"] is None and row["state"] == "queued"
    assert _audit(db, "msg_revoke") == [("queued", ch, {"message_ids": ids})]


async def test_e3_revoke_all_or_nothing(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    await _events(ic, h, [_up("c2", "9", 9000)])
    other_ch = _channel_id(db, "c2")
    mine1, mine2, foreign = _mid(db, ch, "1"), _mid(db, ch, "2"), _mid(db, ch, "3")
    other = _mid(db, other_ch, "9")
    ui = await _ui(client, db, clock)

    r = await _revoke(client, ui, [mine1, other], status=422)  # разные беседы
    assert "invalid" in r["detail"]
    r = await _revoke(client, ui, [mine1, mine2, foreign], status=409)  # одна чужая
    assert r["detail"] == "not_editable"
    await _revoke(client, ui, [mine1, 999_999], status=404)
    await _revoke(client, ui, list(range(mine1, mine1 + 11)), status=422)  # 11 id
    assert _count_rows(db) == 0


async def test_e3_delete_over_edit_replaces_and_edit_over_pending_delete_409(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка")
    await _revoke(client, ui, [m])
    row = _row(db, m)
    assert row["op"] == "delete" and row["text"] is None and row["gen"] == 2

    r = await _edit(client, ui, m, "передумал", status=409)
    assert r["detail"] == "delete_pending"
    assert _row(db, m)["op"] == "delete"

    # удаление не удалось -> правка поверх можно
    (item,) = await _lease(ic, h)
    await _report(ic, h, m, item["lease_gen"], "fail", code="forbidden", detail="MessageDeleteForbidden")
    assert _row(db, m)["state"] == "failed"
    out = await _edit(client, ui, m, "всё-таки правка")
    assert out["op"]["kind"] == "edit" and out["op"]["state"] == "queued"
    assert _row(db, m)["op"] == "edit"


async def test_e3_p1_revoke_hidden_404_and_hide_with_op_409(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    hidden, with_op = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    assert (await client.delete(f"{API}/messages/{hidden}", headers=ui)).status_code == 204
    await _revoke(client, ui, [hidden], status=404)
    assert _row(db, hidden) is None

    await _edit(client, ui, with_op, "правка")
    r = await client.delete(f"{API}/messages/{with_op}", headers=ui)
    assert r.status_code == 409 and r.json()["detail"] == "op_pending"
    r = await client.delete(f"{API}/channels/{ch}", headers=ui)
    assert r.status_code == 409 and r.json()["detail"] == "op_pending"
    with db.reader() as conn:
        assert conn.execute("SELECT hidden FROM main.message WHERE id = ?", (with_op,)).fetchone()[0] == 0

    # неудавшаяся операция тоже держит скрытие (П1: любое состояние); снята — скрыть можно
    await _cancel(client, ui, with_op)
    assert (await client.delete(f"{API}/messages/{with_op}", headers=ui)).status_code == 204


async def test_e3_p1_hide_parent_with_op_on_reply_409(client, ic, db, clock):
    _acc, _h, ch = await _simple(
        db,
        ic,
        source="slack",
        ch="C1",
        kind="group",
        events=[
            _up("C1", "100.000100", 1000, kind="group", is_me=False, thread_ext_id="100.000100"),
            _up("C1", "100.000200", 2000, kind="group", thread_ext_id="100.000100"),
        ],
    )
    parent, reply = _mid(db, ch, "100.000100"), _mid(db, ch, "100.000200")
    ui = await _ui(client, db, clock)
    await _revoke(client, ui, [reply])
    r = await client.delete(f"{API}/messages/{parent}", headers=ui)
    assert r.status_code == 409 and r.json()["detail"] == "op_pending"


# --- E4: выдача ---


async def test_e4_lease_shape_telegram_topic_and_slack_thread(client, ic, db, clock):
    _acc, h = _account(db)
    await _events(
        ic,
        h,
        [
            {
                **_up(
                    "-100500:7", "11", 1000, kind="topic", event_kind="file", attachments=[{"type": "photo"}]
                ),
                "channel": {
                    "ext_id": "-100500:7",
                    "kind": "topic",
                    "parent_ext_id": "-100500",
                    "parent": {"ext_id": "-100500", "kind": "group"},
                },
            }
        ],
    )
    ch = _channel_id(db, "-100500:7")
    m = _mid(db, ch, "11")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "подпись")
    (item,) = await _lease(ic, h)
    assert item == {
        "message_id": m,
        "gen": 1,
        "lease_gen": 1,
        "op": "edit",
        "text": "подпись",
        "channel": {"ext_id": "-100500:7", "kind": "topic", "parent_ext_id": "-100500"},
        "message_ext_id": "11",
        "thread_ext_id": None,
        "message_kind": "file",
        "has_files": True,
    }

    _acc2, h2, sl = await _simple(
        db,
        ic,
        source="slack",
        ch="C1",
        kind="group",
        db_name="work",
        events=[
            _up("C1", "100.000100", 1000, kind="group", is_me=False, thread_ext_id="100.000100"),
            _up("C1", "100.000200", 2000, kind="group", thread_ext_id="100.000100"),
        ],
    )
    reply = _mid(db, sl, "100.000200")
    await _revoke(client, ui, [reply])
    (item,) = await _lease(ic, h2)
    assert item["op"] == "delete" and "text" not in item
    assert item["thread_ext_id"] == "100.000100" and item["message_ext_id"] == "100.000200"
    assert item["message_kind"] == "text" and item["has_files"] is False
    assert db_of(reply) == "work"


async def test_e4_live_lease_pause_and_failed_not_leased(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m1, m2 = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m1, "правка")
    (item,) = await _lease(ic, h)
    assert await _lease(ic, h) == []  # живой лиз
    clock.advance_ms(119_999)
    assert await _lease(ic, h) == []
    clock.advance_ms(1)  # П5: лиз — 120 с
    (again,) = await _lease(ic, h)
    assert again["lease_gen"] == item["lease_gen"] + 1

    await _report(ic, h, m1, again["lease_gen"], "retry", retry_after_ms=90_000)
    clock.advance_ms(89_999)
    assert await _lease(ic, h) == []  # пауза
    clock.advance_ms(1)
    (third,) = await _lease(ic, h)
    await _report(ic, h, m1, third["lease_gen"], "fail", code="forbidden")
    clock.advance_ms(10**7)
    assert await _lease(ic, h) == []  # failed не выдаётся
    await _edit(client, ui, m2, "другая")
    assert [i["message_id"] for i in await _lease(ic, h, limit=5)] == [m2]


async def test_e4_deleted_at_source_after_queue_row_deleted_seq_bumped(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m1, m2 = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m1, "правка")
    await _revoke(client, ui, [m2])
    await _events(
        ic, h, [{"type": "delete", "ext_ids": ["1", "2"], "channel": {"ext_id": "c1", "kind": "dm"}}]
    )
    seqs = (_seq(db, m1), _seq(db, m2))
    assert await _lease(ic, h, limit=5) == []
    assert _row(db, m1) is None and _row(db, m2) is None
    assert _seq(db, m1) > seqs[0] and _seq(db, m2) > seqs[1]


@pytest.mark.parametrize("revoke", [False, True])
async def test_e4_p1_hidden_bypass_row_deleted_any_op(client, ic, db, clock, revoke):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    if revoke:
        await _revoke(client, ui, [m])
    else:
        await _edit(client, ui, m, "правка")
    # обходом (не через ручку — она отвечает 409)
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (m,)))
    assert await _lease(ic, h) == []
    assert _row(db, m) is None


async def test_e4_blocked_account_waits(client, ic, db, clock):
    acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка")
    for status in ("auth_required", "limited"):
        _set_account(db, acc, status=status)
        assert await _lease(ic, h) == []
        assert _row(db, m) is not None
        with db.reader() as conn:
            assert msg_ops_mod.wait_state(conn, account_id=acc, now_ms=_now_ms(clock)) == (False, None)
    _set_account(db, acc, status="ok")
    assert len(await _lease(ic, h)) == 1


async def test_e4_foreign_account_never_leases(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    _acc2, h2 = _account(db)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка")
    assert await _lease(ic, h2) == []
    assert _row(db, m) is not None


# --- E5: отчёт ---


async def test_e5_done_same_gen_deletes_row_and_bumps_seq(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка")
    (item,) = await _lease(ic, h)
    seq0 = _seq(db, m)
    assert (await _report(ic, h, m, item["lease_gen"], "done"))["result"] == "deleted"
    assert _row(db, m) is None and _seq(db, m) > seq0


async def test_e5_done_after_new_op_releases_and_new_op_leased(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка 1")
    (item,) = await _lease(ic, h)
    await _edit(client, ui, m, "правка 2")  # пока задание в работе
    assert _row(db, m)["lease_until_ms"] is not None  # лиз не тронут
    assert (await _report(ic, h, m, item["lease_gen"], "done"))["result"] == "released"
    row = _row(db, m)
    assert row["lease_until_ms"] is None and row["text"] == "правка 2" and row["gen"] == 2
    (again,) = await _lease(ic, h)
    assert again["text"] == "правка 2" and again["gen"] == 2


async def test_e5_fail_same_gen_failed_other_gen_released(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m1, m2 = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m1, "правка")
    (item,) = await _lease(ic, h)
    seq0 = _seq(db, m1)
    long_detail = "Д" * 500
    assert (await _report(ic, h, m1, item["lease_gen"], "fail", code="expired", detail=long_detail))[
        "result"
    ] == "failed"
    row = _row(db, m1)
    assert row["state"] == "failed" and row["error_code"] == "expired" and row["lease_until_ms"] is None
    assert row["error_detail"] == "Д" * 200
    assert _seq(db, m1) > seq0
    op = (await client.get(f"{API}/messages/{m1}")).json()["op"]
    assert op == {
        "kind": "edit",
        "state": "failed",
        "text": "правка",
        "error": {"code": "expired", "detail": "Д" * 200},
    }
    assert _audit(db, "msg_op_failed") == [("failed", None, {"message_id": m1, "code": "expired"})]

    await _edit(client, ui, m2, "правка")
    (item,) = await _lease(ic, h)
    await _edit(client, ui, m2, "правка 2")
    assert (await _report(ic, h, m2, item["lease_gen"], "fail", code="forbidden"))["result"] == "released"
    assert _row(db, m2)["state"] == "queued"

    # `fail` без кода — 422
    (item,) = await _lease(ic, h)
    await _report(ic, h, m2, item["lease_gen"], "fail", status=422)
    await _report(ic, h, m2, item["lease_gen"], "fail", code="weird", status=422)


async def test_e5_retry_backoff_retry_after_and_fifth_failed_other(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _revoke(client, ui, [m])
    pauses = []
    for attempt in range(1, 5):
        (item,) = await _lease(ic, h)
        now = _now_ms(clock)
        extra = {"retry_after_ms": 200_000} if attempt == 2 else {}
        assert (await _report(ic, h, m, item["lease_gen"], "retry", detail="OSError", **extra))[
            "result"
        ] == "paused"
        row = _row(db, m)
        assert row["attempts"] == attempt and row["lease_until_ms"] is None and row["state"] == "queued"
        pauses.append(row["pause_until_ms"] - now)
        clock.advance_ms(row["pause_until_ms"] - now)
    assert pauses == [30_000, 200_000, 120_000, 240_000]
    (item,) = await _lease(ic, h)
    seq0 = _seq(db, m)
    assert (await _report(ic, h, m, item["lease_gen"], "retry", detail="TimeoutError"))["result"] == "gave_up"
    row = _row(db, m)
    assert row["state"] == "failed" and row["error_code"] == "other"
    assert row["error_detail"] == "5 попыток: TimeoutError"
    assert _seq(db, m) > seq0
    assert _audit(db, "msg_op_failed") == [("gave_up", None, {"message_id": m, "code": "other"})]


async def test_e5_retry_other_gen_not_counted_pause_kept(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка 1")
    (item,) = await _lease(ic, h)
    await _edit(client, ui, m, "правка 2")
    now = _now_ms(clock)
    assert (await _report(ic, h, m, item["lease_gen"], "retry", retry_after_ms=7000))["result"] == "paused"
    row = _row(db, m)
    assert row["attempts"] == 0 and row["pause_until_ms"] == now + 30_000
    # новая правка паузу источника не обходит
    await _edit(client, ui, m, "правка 3")
    assert _row(db, m)["not_before_ms"] == now + 30_000


async def test_e5_stale_lease_409_foreign_404(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    _acc2, h2 = _account(db)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "правка")
    (first,) = await _lease(ic, h)
    clock.advance_ms(120_000)
    (second,) = await _lease(ic, h)
    await _report(ic, h, m, first["lease_gen"], "done", status=409)
    await _report(ic, h2, m, second["lease_gen"], "done", status=404)
    await _report(ic, h, 999_999, 1, "done", status=404)
    assert _row(db, m) is not None
    assert (await _report(ic, h, m, second["lease_gen"], "done"))["result"] == "deleted"


# --- E6: отмена ---


async def test_e6_cancel_queued_and_failed_bumps_seq(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m1, m2 = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m1, "правка")
    seq0 = _seq(db, m1)
    await _cancel(client, ui, m1)
    assert _row(db, m1) is None and _seq(db, m1) > seq0

    await _revoke(client, ui, [m2])
    (item,) = await _lease(ic, h)
    await _report(ic, h, m2, item["lease_gen"], "fail", code="forbidden")
    seq1 = _seq(db, m2)
    await _cancel(client, ui, m2)
    assert _row(db, m2) is None and _seq(db, m2) > seq1
    assert [r[2] for r in _audit(db, "msg_op_cancel")] == [{"message_id": m1}, {"message_id": m2}]


async def test_e6_p5_cancel_during_live_lease_409_at_61s(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _revoke(client, ui, [m])
    await _lease(ic, h)
    clock.advance_ms(61_000)  # таймаут push (60 с) уже прошёл, но запрос мог дойти — лиз 120 с
    r = await _cancel(client, ui, m, status=409)
    assert r.json()["detail"] == "in_flight"
    assert _row(db, m) is not None
    clock.advance_ms(59_000)
    await _cancel(client, ui, m)


async def test_review_cancel_new_delete_over_inflight_edit(client, ic, db, clock):
    """Ревью Ф14-8: «Удалить у всех» нажато, пока ПРАВКА у коннектора, — «Отменить» снимает удаление сразу
    (оно ещё не выдано), а не отвечает 409 «уже отправляется» и потом уходит; отчёт старой правки — 404."""
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _edit(client, ui, m, "стало")
    (job,) = await _lease(ic, h)
    assert job["op"] == "edit"
    await _revoke(client, ui, [m])
    await _cancel(client, ui, m)
    assert _row(db, m) is None
    await _report(ic, h, m, job["lease_gen"], "done", status=404)
    assert await _lease(ic, h) == []
    # удаление той же операции, выданное коннектору, — по-прежнему 409
    await _revoke(client, ui, [m])
    await _lease(ic, h)
    r = await _cancel(client, ui, m, status=409)
    assert r.json()["detail"] == "in_flight"


async def test_e6_cancel_no_row_404(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _cancel(client, ui, _mid(db, ch, "1"), status=404)
    await _cancel(client, ui, 999_999, status=404)


# --- long-poll ---


async def test_wait_ready_and_empty_times_out(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    started = time.monotonic()
    r = await ic.get(f"{INTERNAL}/msg-ops/wait", params={"timeout_s": 0.3}, headers=h)
    assert r.json() == {"ready": False}
    assert time.monotonic() - started >= 0.25
    ui = await _ui(client, db, clock)
    await _edit(client, ui, _mid(db, ch, "1"), "правка")
    r = await ic.get(f"{INTERNAL}/msg-ops/wait", params={"timeout_s": 5}, headers=h)
    assert r.json() == {"ready": True}


# --- E7: миграция 0028 на копии БД предыдущей версии, обе схемы ---


def test_e7_migration_0028_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_27(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 27}

    monkeypatch.setattr(db_module, "_migrations", _up_to_27)
    old = Db(tmp_path)
    old.start()
    try:
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                ).lastrowid
                seq = db_module.next_seq(conn)
                conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "  # noqa: S608
                    "ts, is_me, kind, text) VALUES (?, ?, ?, ?, '1', 1000, 1, 'text', 'привет')",
                    (seq, seq, acc, cid),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 27
                names = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master")}  # noqa: S608
                assert "msg_op" not in names
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 28
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(msg_op)")}
                assert cols == {
                    "message_id",
                    "channel_id",
                    "account_id",
                    "op",
                    "text",
                    "state",
                    "error_code",
                    "error_detail",
                    "gen",
                    "not_before_ms",
                    "pause_until_ms",
                    "attempts",
                    "lease_until_ms",
                    "lease_gen",
                    "leased_gen",
                    "created_ms",
                    "updated_ms",
                }
                indexes = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(msg_op)")}
                assert "msg_op_account" in indexes
                rows = conn.execute(f"SELECT ext_id, text FROM {schema}.message").fetchall()  # noqa: S608
                assert rows == [("1", "привет")]
        # CHECK-ограничения в силе
        import sqlite3

        def _bad(conn):
            conn.execute(
                "INSERT INTO main.msg_op (message_id, channel_id, account_id, op, not_before_ms, created_ms, "
                "updated_ms) VALUES (1, 1, 1, 'nuke', 0, 0, 0)"
            )

        with pytest.raises(sqlite3.IntegrityError):
            new.write_sync(_bad)
    finally:
        new.close()


async def test_p1_core_rejects_hidden_even_without_route_check(client, ic, db, clock):
    """П1 в глубину: сама очередь (не только `message_visible` ручки) не ставит операцию скрытому у себя."""
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (m,)))
    now = _now_ms(clock)
    with pytest.raises(msg_ops_mod.NotFound):
        db.write_sync(lambda conn: msg_ops_mod.enqueue_delete(conn, [m], now))
    with pytest.raises(msg_ops_mod.NotFound):
        db.write_sync(lambda conn: msg_ops_mod.enqueue_edit(conn, m, "x", now))
    assert _row(db, m) is None
