"""Ф40-1a (`docs/architecture.md`): то, что проверяется через HTTP. Раздел D — файлы в отправке для
источника `mattermost` (до 10 файлов одним сообщением, не альбом); раздел A — время источника в отчёте об
отправке (`ts_ms`) через внутреннюю ручку и у альбома (одно на все элементы). Остальное —
`tests/core/test_f40_mattermost_core.py`."""

import json

from .conftest import login_via_code
from .test_send_files import (
    _auth,
    _connector_secret,
    _message,
    _outbox,
    _send,
    _setup,
    _upload,
    iclient,  # noqa: F401 — фикстура внутреннего приложения
)
from .test_send_multi_files import (
    _album,
    _audit_actions,
    _channel_rows,
    _lease,
    _pdf,
    _png,
    _report,
    _uploads,
)

MM_POST = "own" + "x" * 23  # id поста Mattermost: 26 символов `[a-z0-9]`
TS = 1_700_000_000_123


# --- D: файлы в отправке -----------------------------------------------------------------------------------


async def test_mattermost_upload_and_ten_mixed_files_in_one_message(client, db, clock):
    """Раньше — 422 «файлы в эту беседу не отправляются». Смешанные виды (картинки и документы) — можно, как у
    Slack (у Telegram в одном альбоме — нет)."""
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="mattermost")
    bodies = [_png(f"p{i}") if i % 2 == 0 else _pdf(f"d{i}") for i in range(11)]
    names = [f"f{i}.png" if i % 2 == 0 else f"f{i}.pdf" for i in range(11)]
    ids = await _uploads(client, cookie, channel_id, bodies, names=names)

    r = await _send(client, cookie, channel_id, files=ids, key="k11")
    assert r.status_code == 422  # 11 файлов — больше одного сообщения
    r = await _send(client, cookie, channel_id, text="десять", files=ids[:10], key="k10")
    assert r.status_code == 200, r.text
    body = r.json()

    message = _message(db, body["message_id"])
    assert message["kind"] == "file"
    assert [att["name"] for att in json.loads(message["attachments_json"])] == names[:10]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1

    (lease,) = _lease(db, account_id, int(clock() * 1000))
    assert [f["name"] for f in lease.files] == names[:10]


async def test_mattermost_long_caption_with_file_is_allowed(client, db, clock):
    """Лимит подписи 1024 UTF-16 — только у Telegram; у Mattermost текст с файлом — обычный текст поста."""
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db, source="mattermost")
    r = await _upload(client, cookie, channel_id)
    assert r.status_code == 200, r.text
    s = await _send(client, cookie, channel_id, text="я" * 3_000, files=[r.json()["upload_id"]])
    assert s.status_code == 200, s.text


async def test_mattermost_files_are_not_an_album(client, db, clock):
    """Отчёт с `ext_ids` у Mattermost не раскладывает заготовку на строки альбома (одно сообщение с N
    вложениями), но принимается; время источника из отчёта строке ставится."""
    a = await _album(client, db, clock, source="mattermost", n=3)
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    _report(db, oid, lease, now_ms + 200, ext_id=MM_POST, ext_ids=[MM_POST, "b" * 26, "c" * 26], ts_ms=TS)

    assert _outbox(db, oid)["status"] == "sent"
    assert json.loads(_audit_actions(db, "outbox_album_ignored")[0][1])["reason"] == "source"
    (row,) = _channel_rows(db, a["channel_id"])
    assert row["id"] == placeholder and row["ext_id"] == MM_POST and row["ts"] == TS
    assert len(json.loads(row["attachments_json"])) == 3
    assert row["grouped_id"] is None


# --- A: время источника в отчёте ---------------------------------------------------------------------------


async def test_report_ts_ms_via_internal_route(client, iclient, db, clock):  # noqa: F811
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="mattermost")
    s = await _send(client, cookie, channel_id, text="из хаба")
    assert s.status_code == 200, s.text
    secret = _connector_secret(db, account_id)
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    r = await iclient.post(
        "/internal/v1/outbox/report",
        headers=_auth(secret),
        json={
            "outbox_id": item["outbox_id"], "lease_gen": item["lease_gen"], "outcome": "sent",
            "ext_id": MM_POST, "ts_ms": TS,
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    row = _message(db, s.json()["message_id"])
    assert row["ext_id"] == MM_POST and row["ts"] == TS


async def test_report_bad_ts_ms_via_internal_route_does_not_reject_report(client, iclient, db, clock):  # noqa: F811
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="mattermost")
    s = await _send(client, cookie, channel_id, text="из хаба")
    before = _message(db, s.json()["message_id"])["ts"]
    secret = _connector_secret(db, account_id)
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    r = await iclient.post(
        "/internal/v1/outbox/report",
        headers=_auth(secret),
        json={
            "outbox_id": item["outbox_id"], "lease_gen": item["lease_gen"], "outcome": "sent",
            "ext_id": MM_POST, "ts_ms": {"bad": True},
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text  # «отправлено» не теряется
    assert _outbox(db, item["outbox_id"])["status"] == "sent"
    assert _message(db, s.json()["message_id"])["ts"] == before


async def test_album_report_ts_ms_is_one_for_all_elements(client, db, clock):
    """A п. 2: альбом — время одно на все элементы (и у заготовки, и у строк, созданных раскладкой)."""
    a = await _album(client, db, clock)  # Telegram, три картинки
    oid = a["body"]["outbox_id"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    _report(
        db, oid, lease, now_ms + 200, ext_id="100", ext_ids=["100", "101", "102"], grouped_id="g1", ts_ms=TS
    )
    rows = _channel_rows(db, a["channel_id"])
    assert [row["ext_id"] for row in rows] == ["100", "101", "102"]
    assert [row["ts"] for row in rows] == [TS, TS, TS]


async def test_album_report_without_ts_ms_keeps_placeholder_time(client, db, clock):
    a = await _album(client, db, clock)
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    before = _message(db, placeholder)["ts"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    _report(db, oid, lease, now_ms + 200, ext_id="100", ext_ids=["100", "101", "102"], grouped_id="g1")
    assert [row["ts"] for row in _channel_rows(db, a["channel_id"])] == [before, before, before]
