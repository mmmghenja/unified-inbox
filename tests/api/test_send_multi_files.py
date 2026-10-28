"""Ф11-3b (docs/architecture.md): несколько файлов одним сообщением — ядро (раздел A, тесты
D 1–12 и поправки П3, П4, П5, П6). Один `send` с N `upload_id` -> одна строка outbox, `file_json` — список,
заготовка с N вложениями; задание `files` и ручка байтов `?idx=K`; отчёт `sent` с `ext_ids`/`grouped_id`
раскладывает заготовку Telegram на N строк альбома; эхо альбома Telegram по тексту не склеивается; вложения
письма (почта в `uploads.SOURCES`, лимит 17 МиБ)."""

import hashlib
import json

from hub.core import draft_channels, hiding, media, uploads
from hub.core import outbox as outbox_mod

from .conftest import csrf_headers, insert_account, insert_channel, login_via_code, make_bearer_token
from .test_send_files import (
    _auth,
    _connector_secret,
    _ingest,
    _media_rows,
    _message,
    _outbox,
    _schema,
    _send,
    _setup,
    _upload,
    _upload_count,
    iclient,  # noqa: F401 — фикстура внутреннего приложения
)

MIB = 1024 * 1024


def _png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + tag.encode()


def _pdf(tag: str) -> bytes:
    return b"%PDF-1.7\n" + tag.encode() * 40


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def _uploads(client, cookie, channel_id, bodies, names=None):
    ids = []
    for i, body in enumerate(bodies):
        name = names[i] if names else f"f{i}.png"
        r = await _upload(client, cookie, channel_id, body, name=name)
        assert r.status_code == 200, r.text
        ids.append(r.json()["upload_id"])
    return ids


async def _album(client, db, clock, *, source="telegram", text="", n=3, db_name="main"):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source=source, db_name=db_name)
    bodies = [_png(f"p{i}") for i in range(n)]
    ids = await _uploads(client, cookie, channel_id, bodies)
    r = await _send(client, cookie, channel_id, text=text, files=ids)
    assert r.status_code == 200, r.text
    return {
        "cookie": cookie,
        "account_id": account_id,
        "channel_id": channel_id,
        "bodies": bodies,
        "ids": ids,
        "body": r.json(),
    }


def _set_upload_size(db, upload_id, size):
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.upload SET size = ? WHERE id = ?", (size, upload_id))
    )


def _lease(db, account_id, now_ms, owner="o"):
    return db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner=owner, now_ms=now_ms)
    )


def _report(db, oid, lease, now_ms, **kw):
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn, outbox_id=oid, owner="o", lease_gen=lease.lease_gen, outcome=kw.pop("outcome", "sent"),
            now_ms=now_ms, **kw,
        )
    )  # fmt: skip


def _channel_rows(db, channel_id):
    with db.reader() as conn:
        cur = conn.execute(
            "SELECT * FROM main.message WHERE channel_id = ? AND deleted = 0 ORDER BY id", (channel_id,)
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _audit_actions(db, action):
    with db.reader() as conn:
        return conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


def _all_media(db, account_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, message_id, idx, sha256 FROM main.media WHERE account_id = ? ORDER BY id",
            (account_id,),
        ).fetchall()


# --- тест 1: одна строка outbox, список файлов, заготовка с тремя вложениями --------------------------------


async def test_1_three_uploads_one_outbox_row_list_placeholder_media(client, db, clock):
    a = await _album(client, db, clock, text="три")
    body = a["body"]
    row = _outbox(db, body["outbox_id"])
    files = json.loads(row["file_json"])
    assert [f["upload_id"] for f in files] == a["ids"]
    assert [f["sha256"] for f in files] == [_sha(b) for b in a["bodies"]]
    assert [f["name"] for f in files] == ["f0.png", "f1.png", "f2.png"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1
    message = _message(db, body["message_id"])
    assert message["kind"] == "file"
    assert [att["name"] for att in json.loads(message["attachments_json"])] == ["f0.png", "f1.png", "f2.png"]
    rows = _media_rows(db, body["message_id"])
    assert [(m["idx"], m["status"], m["sha256"]) for m in rows] == [
        (i, "ready", _sha(b)) for i, b in enumerate(a["bodies"])
    ]
    assert _upload_count(db) == 0


# --- тест 2: лимиты -----------------------------------------------------------------------------------------


async def test_2_limits_count_duplicates_sum_mail_mixed_bearer(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    ids = await _uploads(client, cookie, channel_id, [_png(f"x{i}") for i in range(11)])

    r = await _send(client, cookie, channel_id, files=ids, key="k11")
    assert r.status_code == 422  # 11 файлов
    r = await _send(client, cookie, channel_id, files=[ids[0], ids[0]], key="kdup")
    assert r.status_code == 422  # повтор upload_id
    assert _upload_count(db) == 11

    # Сумма больше 50 МиБ (размер строки загрузки — то, что считает ядро).
    _set_upload_size(db, ids[0], 25 * MIB)
    _set_upload_size(db, ids[1], 25 * MIB + 1)
    r = await _send(client, cookie, channel_id, files=ids[:2], key="ksum")
    assert r.status_code == 422
    assert "50" in r.json()["detail"]
    _set_upload_size(db, ids[1], 25 * MIB)
    r = await _send(client, cookie, channel_id, files=ids[:2], key="ksum-ok")
    assert r.status_code == 200, r.text  # ровно 50 МиБ — можно

    # Telegram: картинка + документ в одном альбоме — 422.
    pdf_id = (await _upload(client, cookie, channel_id, _pdf("d"), name="d.pdf")).json()["upload_id"]
    r = await _send(client, cookie, channel_id, files=[ids[2], pdf_id], key="kmix")
    assert r.status_code == 422

    # Bearer с файлами — 422.
    client.cookies.clear()
    token = make_bearer_token(db, scopes=("read", "send", "draft"))
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": channel_id, "text": "x", "idempotency_key": "b", "files": ids[2:4]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


async def test_2_mail_sum_over_17_mib_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db, source="mail")
    ids = await _uploads(client, cookie, channel_id, [_pdf("a"), _pdf("b")])
    _set_upload_size(db, ids[0], 9 * MIB)
    _set_upload_size(db, ids[1], 8 * MIB + 1)
    r = await _send(client, cookie, channel_id, text="", files=ids, key="m1")
    assert r.status_code == 422
    assert "17" in r.json()["detail"]
    _set_upload_size(db, ids[1], 8 * MIB)
    r = await _send(client, cookie, channel_id, text="", files=ids, key="m2")
    assert r.status_code == 200, r.text  # ровно 17 МиБ, письмо только с вложениями — можно


async def test_2_slack_mixed_kinds_is_allowed(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db, source="slack")
    ids = await _uploads(client, cookie, channel_id, [_png("a"), _pdf("b")], names=["a.png", "b.pdf"])
    r = await _send(client, cookie, channel_id, files=ids)
    assert r.status_code == 200, r.text


# --- тест 3: идемпотентность по списку ----------------------------------------------------------------------


async def test_3_idempotency_same_list_same_result_other_order_or_set_409(client, db, clock):
    a = await _album(client, db, clock, text="подпись")
    cookie, channel_id, ids = a["cookie"], a["channel_id"], a["ids"]
    again = await _send(client, cookie, channel_id, text="подпись", files=ids, key="k1")
    assert again.status_code == 200, again.text
    assert again.json()["outbox_id"] == a["body"]["outbox_id"]
    assert again.json()["created"] is False

    reordered = [ids[1], ids[0], ids[2]]
    assert (
        await _send(client, cookie, channel_id, text="подпись", files=reordered, key="k1")
    ).status_code == 409
    assert (
        await _send(client, cookie, channel_id, text="подпись", files=ids[:2], key="k1")
    ).status_code == 409
    extra = (await _upload(client, cookie, channel_id, _png("new"))).json()["upload_id"]
    r = await _send(client, cookie, channel_id, text="подпись", files=[*ids, extra], key="k1")
    assert r.status_code == 409
    assert _upload_count(db) == 1  # новая загрузка не тронута


# --- тест 4: старая строка со словарём в file_json ----------------------------------------------------------


async def test_4_old_dict_row_lease_bytes_retry_scrub(client, iclient, db, clock):  # noqa: F811
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    body = _png("old")
    (upload_id,) = await _uploads(client, cookie, channel_id, [body], names=["old.png"])
    sent = (await _send(client, cookie, channel_id, text="", files=[upload_id])).json()
    oid = sent["outbox_id"]
    old = {"sha256": _sha(body), "size": len(body), "mime": "image/png", "name": "old.png", "kind": "photo",
           "upload_id": upload_id}  # fmt: skip
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.outbox SET file_json = ? WHERE id = ?", (json.dumps(old), oid))
    )

    secret = _connector_secret(db, account_id)
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    assert item["files"] == [
        {"size": len(body), "mime": "image/png", "name": "old.png", "kind": "photo", "sha256": _sha(body)}
    ]
    r = await iclient.get(
        f"/internal/v1/outbox/{oid}/file?lease_gen={item['lease_gen']}&idx=0", headers=_auth(secret)
    )
    assert r.status_code == 200
    assert r.content == body
    r = await iclient.get(
        f"/internal/v1/outbox/{oid}/file?lease_gen={item['lease_gen']}&idx=1", headers=_auth(secret)
    )
    assert r.status_code == 404

    # unknown -> «Повторить»: файл без подписи повторяется, `maybe_sent` сброшен.
    r = await iclient.post(
        "/internal/v1/outbox/report",
        headers=_auth(secret),
        json={"outbox_id": oid, "lease_gen": item["lease_gen"], "outcome": "unknown"},
    )
    assert r.status_code == 200
    db.write_sync(lambda conn: conn.execute("UPDATE main.outbox SET maybe_sent = 1 WHERE id = ?", (oid,)))
    now_ms = int(clock() * 1000)
    status = db.write_sync(
        lambda conn: outbox_mod.resolve_unknown(conn, oid, decision="retry", now_ms=now_ms)
    )
    assert status == "queued"
    assert _outbox(db, oid)["maybe_sent"] == 0

    # sent -> «удалить у себя»: у файла остаётся `{kind, size}`, запись — списком.
    (lease,) = _lease(db, account_id, now_ms)
    _report(db, oid, lease, now_ms, ext_id="501")
    from hub.core import hiding

    db.write_sync(lambda conn: hiding.hide_message(conn, sent["message_id"], now_ms=now_ms + 1))
    assert json.loads(_outbox(db, oid)["file_json"]) == [{"kind": "photo", "size": len(body)}]


# --- тест 5: ручка байтов по idx ----------------------------------------------------------------------------


async def test_5_file_bytes_by_idx_out_of_range_foreign_stale(client, iclient, db, clock):  # noqa: F811
    a = await _album(client, db, clock)
    oid = a["body"]["outbox_id"]
    secret = _connector_secret(db, a["account_id"])
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    assert [f["sha256"] for f in item["files"]] == [_sha(b) for b in a["bodies"]]
    gen = item["lease_gen"]
    for idx, body in enumerate(a["bodies"]):
        r = await iclient.get(
            f"/internal/v1/outbox/{oid}/file?lease_gen={gen}&idx={idx}", headers=_auth(secret)
        )
        assert r.status_code == 200
        assert r.content == body
        assert r.headers["content-length"] == str(len(body))
    r = await iclient.get(f"/internal/v1/outbox/{oid}/file?lease_gen={gen}", headers=_auth(secret))
    assert r.content == a["bodies"][0]  # idx по умолчанию 0
    for bad in (3, -1):
        r = await iclient.get(
            f"/internal/v1/outbox/{oid}/file?lease_gen={gen}&idx={bad}", headers=_auth(secret)
        )
        assert r.status_code == 404
    r = await iclient.get(f"/internal/v1/outbox/{oid}/file?lease_gen={gen - 1}&idx=1", headers=_auth(secret))
    assert r.status_code == 404
    other_secret = _connector_secret(db, insert_account(db, source="telegram"))
    r = await iclient.get(
        f"/internal/v1/outbox/{oid}/file?lease_gen={gen}&idx=1", headers=_auth(other_secret)
    )
    assert r.status_code == 404


# --- тест 6: отчёт Telegram с ext_ids — три строки альбома --------------------------------------------------


async def test_6_telegram_report_with_ext_ids_lays_out_album(client, iclient, db, clock):  # noqa: F811
    a = await _album(client, db, clock, text="подпись")
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    secret = _connector_secret(db, a["account_id"])
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    with db.reader() as conn:
        count_before = conn.execute(
            "SELECT my_msg_count FROM main.channel WHERE id = ?", (a["channel_id"],)
        ).fetchone()[0]
    r = await iclient.post(
        "/internal/v1/outbox/report",
        headers=_auth(secret),
        json={
            "outbox_id": oid,
            "lease_gen": item["lease_gen"],
            "outcome": "sent",
            "ext_id": "100",
            "ext_ids": ["100", "101", "102"],
            "grouped_id": "13579",
        },
    )
    assert r.status_code == 200, r.text
    assert _outbox(db, oid)["status"] == "sent"
    rows = _channel_rows(db, a["channel_id"])
    assert [row["ext_id"] for row in rows] == ["100", "101", "102"]
    assert rows[0]["id"] == placeholder
    assert {row["grouped_id"] for row in rows} == {"13579"}
    assert [row["outbox_id"] for row in rows] == [oid, None, None]
    assert [row["text"] for row in rows] == ["подпись", "", ""]
    assert all(row["is_me"] == 1 and row["kind"] == "file" for row in rows)
    for k, row in enumerate(rows):
        (att,) = json.loads(row["attachments_json"])
        assert att["name"] == f"f{k}.png"
        (m,) = _media_rows(db, row["id"])
        assert m["idx"] == 0
        assert m["status"] == "ready"
        assert m["sha256"] == _sha(a["bodies"][k])
        got = await client.get(f"/api/v1/media/{m['id']}")
        assert got.status_code == 200
        assert got.content == a["bodies"][k]
    with db.reader() as conn:
        count_after = conn.execute(
            "SELECT my_msg_count FROM main.channel WHERE id = ?", (a["channel_id"],)
        ).fetchone()[0]
    assert count_after == count_before + 1  # одно отправление
    assert len(_audit_actions(db, "outbox_album")) == 1


async def test_6_telegram_album_without_grouped_id_gets_out_id(client, db, clock):
    a = await _album(client, db, clock, n=2)
    oid = a["body"]["outbox_id"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    _report(db, oid, lease, now_ms, ext_id="7", ext_ids=["7", "8"])
    rows = _channel_rows(db, a["channel_id"])
    assert [row["grouped_id"] for row in rows] == [f"out:{oid}", f"out:{oid}"]


async def test_6_work_schema_album(client, db, clock):
    a = await _album(client, db, clock, n=2, db_name="work")
    oid = a["body"]["outbox_id"]
    assert _schema(oid) == "work"
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    _report(db, oid, lease, now_ms, ext_id="7", ext_ids=["7", "8"], grouped_id="g")
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT m.ext_id, m.grouped_id, md.sha256 FROM work.message m JOIN work.media md "
            "ON md.message_id = m.id WHERE m.channel_id = ? ORDER BY m.id",
            (a["channel_id"],),
        ).fetchall()
    assert rows == [("7", "g", _sha(a["bodies"][0])), ("8", "g", _sha(a["bodies"][1]))]


# --- тест 7 + П3: сверка успела принести элементы 0 и 2 -----------------------------------------------------


async def test_7_reconcile_brought_elements_0_and_2_without_media_each_row_own_file(client, db, clock):
    """П3: эхо элемента 0 пришло раньше отчёта — на его строку уходит только `media idx=0`, остальные — к
    строкам своих элементов (у каждой строки альбома своя картинка)."""
    a = await _album(client, db, clock)
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    real0 = _ingest(db, a["account_id"], ext_id="100", ts=now_ms + 100)
    real2 = _ingest(db, a["account_id"], ext_id="102", ts=now_ms + 100)
    _report(db, oid, lease, now_ms + 200, ext_id="100", ext_ids=["100", "101", "102"], grouped_id="g1")

    rows = _channel_rows(db, a["channel_id"])
    assert [row["ext_id"] for row in rows] == ["100", "102", "101"]
    assert rows[0]["id"] == real0 and rows[1]["id"] == real2
    assert _message(db, placeholder)["deleted"] == 1
    assert _media_rows(db, placeholder) == []
    assert rows[0]["outbox_id"] == oid
    by_ext = {row["ext_id"]: row for row in rows}
    for k, ext in enumerate(["100", "101", "102"]):
        (m,) = _media_rows(db, by_ext[ext]["id"])
        assert (m["idx"], m["sha256"]) == (0, _sha(a["bodies"][k]))
    assert by_ext["100"]["grouped_id"] == "g1"
    assert by_ext["101"]["grouped_id"] == "g1"
    assert all(media.file_path(db.data_dir, a["account_id"], _sha(b)).is_file() for b in a["bodies"])


async def test_7_reconcile_rows_with_own_media_no_orphans_no_duplicates(client, db, clock):
    a = await _album(client, db, clock)
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    (lease,) = _lease(db, a["account_id"], now_ms)
    for ext in ("100", "102"):
        _ingest(
            db, a["account_id"], ext_id=ext, ts=now_ms + 100, text="" if ext == "102" else "подпись",
            attachments=[{"type": "photo", "src_id": f"TG{ext}", "mime": "image/jpeg"}],
        )  # fmt: skip
    _report(db, oid, lease, now_ms + 200, ext_id="100", ext_ids=["100", "101", "102"], grouped_id="g1")

    rows = _channel_rows(db, a["channel_id"])
    assert sorted(row["ext_id"] for row in rows) == ["100", "101", "102"]  # без дублей
    assert _message(db, placeholder)["deleted"] == 1
    assert _media_rows(db, placeholder) == []
    message_ids = {row["id"] for row in rows}
    all_media = _all_media(db, a["account_id"])
    assert {m[1] for m in all_media} <= message_ids  # осиротевших строк media нет
    for row in rows:
        assert [m["idx"] for m in _media_rows(db, row["id"])] == [0]
    by_ext = {row["ext_id"]: row for row in rows}
    (m1,) = _media_rows(db, by_ext["101"]["id"])
    assert m1["sha256"] == _sha(a["bodies"][1])
    # Файлы заготовки для элементов 0 и 2 больше никому не нужны (у эха свои строки) — удалены.
    assert not media.file_path(db.data_dir, a["account_id"], _sha(a["bodies"][0])).exists()
    assert not media.file_path(db.data_dir, a["account_id"], _sha(a["bodies"][2])).exists()
    assert media.file_path(db.data_dir, a["account_id"], _sha(a["bodies"][1])).is_file()


async def test_7_reconcile_rows_hidden_by_user_get_no_placeholder_media(client, db, clock):
    """Ревью Ф11-3b: элементы 0 и 1 пришли сверкой, владелец удалил их у себя до отчёта — строки `media`
    заготовки на них не перевешиваются, файлы удаляются по правилу ссылок (удалённое не хранится)."""
    a = await _album(client, db, clock, text="подпись")
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    real0 = _ingest(db, a["account_id"], ext_id="100", ts=now_ms + 100, text="подпись")
    real1 = _ingest(db, a["account_id"], ext_id="101", ts=now_ms + 100)
    for message_id in (real0, real1):
        db.write_sync(lambda conn, mid=message_id: hiding.hide_message(conn, mid, now_ms=now_ms + 150))
    _report(db, oid, lease, now_ms + 200, ext_id="100", ext_ids=["100", "101", "102"], grouped_id="g1")

    assert _media_rows(db, real0) == [] and _media_rows(db, real1) == []
    assert _media_rows(db, placeholder) == []
    for k in (0, 1):
        assert not media.file_path(db.data_dir, a["account_id"], _sha(a["bodies"][k])).exists()
    by_ext = {row["ext_id"]: row for row in _channel_rows(db, a["channel_id"])}
    (m2,) = _media_rows(db, by_ext["102"]["id"])
    assert m2["sha256"] == _sha(a["bodies"][2])
    assert media.file_path(db.data_dir, a["account_id"], _sha(a["bodies"][2])).is_file()


# --- тест 8: негодные ext_ids -------------------------------------------------------------------------------


async def _assert_ignored(db, a, lease, now_ms, **kw):
    oid, placeholder = a["body"]["outbox_id"], a["body"]["message_id"]
    _report(db, oid, lease, now_ms, **kw)
    assert _outbox(db, oid)["status"] == "sent"
    rows = _channel_rows(db, a["channel_id"])
    assert [row["id"] for row in rows] == [placeholder]
    assert len(json.loads(rows[0]["attachments_json"])) == len(a["bodies"])
    assert rows[0]["grouped_id"] is None
    assert len(_media_rows(db, placeholder)) == len(a["bodies"])


async def test_8_invalid_ext_ids_are_ignored_with_audit(client, db, clock):
    cases = [
        {"ext_id": "100", "ext_ids": ["100", "101"]},  # длина не N
        {"ext_id": "100", "ext_ids": ["101", "100", "102"]},  # ext_ids[0] != ext_id
        {"ext_id": "100", "ext_ids": ["100", "100", "102"]},  # не разные
        {"ext_id": "100", "ext_ids": ["100", "", "102"]},  # пустая
        {"ext_id": "100", "ext_ids": ["100", 101, "102"]},  # не строка
        {"ext_id": "100", "ext_ids": ["100", "1" * 65, "102"]},  # длиннее 64
        {"ext_id": "100", "ext_ids": ["100", "101", "102"], "grouped_id": "g" * 65},
        {"ext_id": "100", "ext_ids": "100,101,102"},
    ]
    for i, case in enumerate(cases):
        a = await _album(client, db, clock)
        now_ms = int(clock() * 1000)
        (lease,) = _lease(db, a["account_id"], now_ms)
        await _assert_ignored(db, a, lease, now_ms, **case)
        assert len(_audit_actions(db, "outbox_album_ignored")) == i + 1
        client.cookies.clear()


async def test_8_ext_ids_via_internal_route_bad_types_do_not_reject_report(client, iclient, db, clock):  # noqa: F811
    a = await _album(client, db, clock)
    secret = _connector_secret(db, a["account_id"])
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    r = await iclient.post(
        "/internal/v1/outbox/report",
        headers=_auth(secret),
        json={
            "outbox_id": item["outbox_id"], "lease_gen": item["lease_gen"], "outcome": "sent",
            "ext_id": "100", "ext_ids": {"bad": True}, "grouped_id": 5,
        },
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert _outbox(db, item["outbox_id"])["status"] == "sent"
    ((result, detail),) = _audit_actions(db, "outbox_album_ignored")
    assert result == "sent"
    assert json.loads(detail)["reason"] == "count"


async def test_8_slack_and_single_file_ext_ids_ignored(client, db, clock):
    a = await _album(client, db, clock, source="slack", n=2)
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    await _assert_ignored(
        db, a, lease, now_ms, ext_id="1700000000.000100", ext_ids=["1700000000.000100", "x"]
    )
    assert json.loads(_audit_actions(db, "outbox_album_ignored")[0][1])["reason"] == "source"
    client.cookies.clear()

    b = await _album(client, db, clock, n=1)
    (lease,) = _lease(db, b["account_id"], now_ms)
    await _assert_ignored(db, b, lease, now_ms, ext_id="100", ext_ids=["100"], grouped_id="g")
    assert json.loads(_audit_actions(db, "outbox_album_ignored")[1][1])["reason"] == "files"


async def test_8_non_sent_outcome_with_ext_ids_ignored(client, db, clock):
    a = await _album(client, db, clock)
    oid = a["body"]["outbox_id"]
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, a["account_id"], now_ms)
    _report(db, oid, lease, now_ms, outcome="unknown", ext_ids=["1", "2", "3"])
    assert _outbox(db, oid)["status"] == "unknown"
    assert json.loads(_audit_actions(db, "outbox_album_ignored")[0][1])["reason"] == "outcome"


# --- тест 9: match_echo -------------------------------------------------------------------------------------


async def test_9_telegram_album_echo_by_caption_does_not_glue(client, db, clock):
    a = await _album(client, db, clock, text="смотри", n=2)
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    _lease(db, a["account_id"], now_ms)
    echo = _ingest(
        db, a["account_id"], ext_id="300", ts=now_ms + 100, text="смотри",
        attachments=[{"type": "photo", "src_id": "TG300", "mime": "image/jpeg"}],
    )  # fmt: skip
    assert echo != a["body"]["message_id"]
    placeholder = _message(db, a["body"]["message_id"])
    assert placeholder["ext_id"] is None
    assert len(_media_rows(db, a["body"]["message_id"])) == 2
    assert _outbox(db, a["body"]["outbox_id"])["status"] == "sending"


async def test_9_slack_multi_echo_by_caption_glues_with_all_attachments(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="slack")
    bodies = [_png("s0"), _pdf("s1")]
    ids = await _uploads(client, cookie, channel_id, bodies, names=["a.png", "b.pdf"])
    sent = (await _send(client, cookie, channel_id, text="смотри", files=ids)).json()
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    _lease(db, account_id, now_ms)
    merged = _ingest(
        db, account_id, ext_id="1700000000.000200", ts=now_ms + 100, text="смотри",
        attachments=[
            {"type": "photo", "src_id": "F0", "name": "a.png", "size": len(bodies[0]), "mime": "image/png"},
            {"type": "document", "src_id": "F1", "name": "b.pdf", "size": len(bodies[1]),
             "mime": "application/pdf"},
        ],
    )  # fmt: skip
    assert merged == sent["message_id"]
    assert _outbox(db, sent["outbox_id"])["status"] == "sent"
    rows = _media_rows(db, merged)
    assert [(m["idx"], m["status"], m["sha256"], m["src_id"]) for m in rows] == [
        (0, "ready", _sha(bodies[0]), "F0"),
        (1, "ready", _sha(bodies[1]), "F1"),
    ]
    assert len(json.loads(_message(db, merged)["attachments_json"])) == 2


async def test_9_telegram_single_file_echo_by_caption_still_glues(client, db, clock):
    a = await _album(client, db, clock, text="одна", n=1)
    now_ms = int(clock() * 1000)
    _lease(db, a["account_id"], now_ms)
    merged = _ingest(
        db, a["account_id"], ext_id="301", ts=now_ms + 100, text="одна",
        attachments=[{"type": "photo", "src_id": "TG301", "mime": "image/png"}],
    )  # fmt: skip
    assert merged == a["body"]["message_id"]


# --- П5: эхо Slack с переставленными файлами — чужих байтов у строк нет -------------------------------------


async def test_p5_slack_echo_swapped_order_no_foreign_bytes(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="slack")
    bodies = [_png("q0"), _pdf("q1-longer")]
    names = ["a.png", "b.pdf"]
    ids = await _uploads(client, cookie, channel_id, bodies, names=names)
    sent = (await _send(client, cookie, channel_id, text="два", files=ids)).json()
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    _lease(db, account_id, now_ms)
    merged = _ingest(
        db, account_id, ext_id="1700000000.000300", ts=now_ms + 100, text="два",
        attachments=[
            {"type": "document", "src_id": "F1", "name": "b.pdf", "size": len(bodies[1]),
             "mime": "application/pdf"},
            {"type": "photo", "src_id": "F0", "name": "a.png", "size": len(bodies[0]), "mime": "image/png"},
        ],
    )  # fmt: skip
    assert merged == sent["message_id"]
    rows = _media_rows(db, merged)
    assert [r["name"] for r in rows] == ["b.pdf", "a.png"]
    by_name = dict(zip(names, bodies, strict=True))
    for row in rows:
        if row["sha256"] is not None:
            assert row["sha256"] == _sha(by_name[row["name"]])  # своя картинка, не чужая
        else:
            assert row["status"] != "ready"  # сброшена на скачивание
    assert any(r["sha256"] is None for r in rows)


async def test_p5_slack_echo_same_order_keeps_bytes(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="slack")
    bodies = [_png("r0"), _pdf("r1")]
    ids = await _uploads(client, cookie, channel_id, bodies, names=["a.png", "b.pdf"])
    sent = (await _send(client, cookie, channel_id, text="два", files=ids)).json()
    now_ms = int(clock() * 1000)
    _lease(db, account_id, now_ms)
    merged = _ingest(
        db, account_id, ext_id="1700000000.000301", ts=now_ms + 100, text="два",
        attachments=[
            {"type": "photo", "src_id": "F0", "name": "a.png", "size": len(bodies[0]), "mime": "image/png"},
            {"type": "document", "src_id": "F1", "name": "b.pdf", "size": len(bodies[1]),
             "mime": "application/pdf"},
        ],
    )  # fmt: skip
    assert merged == sent["message_id"]
    assert [(m["status"], m["sha256"]) for m in _media_rows(db, merged)] == [
        ("ready", _sha(bodies[0])),
        ("ready", _sha(bodies[1])),
    ]


async def test_p5_single_file_echo_other_size_keeps_bytes(client, db, clock):
    """П5 — только у сообщения с несколькими вложениями: фото Telegram в эхе пережато (другой размер), у
    одного вложения путать не с чем — байты загрузки остаются, перекачки нет."""
    a = await _album(client, db, clock, text="одна", n=1)
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    _lease(db, a["account_id"], now_ms)
    merged = _ingest(
        db, a["account_id"], ext_id="302", ts=now_ms + 100, text="одна",
        attachments=[{"type": "photo", "src_id": "TG302", "size": 12345, "mime": "image/jpeg"}],
    )  # fmt: skip
    assert merged == a["body"]["message_id"]
    ((status, sha256),) = [(m["status"], m["sha256"]) for m in _media_rows(db, merged)]
    assert status == "ready" and sha256 is not None


# --- П4: known_own_ext_ids строки-альбома -------------------------------------------------------------------


async def test_p4_album_known_own_ext_ids_only_bound_to_outbox(client, db, clock):
    a = await _album(client, db, clock, n=2)
    now_ms = int(clock() * 1000)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = ? WHERE id = ?",
            (json.dumps({"idempotent_send": True}), a["account_id"]),
        )
    )
    _lease(db, a["account_id"], now_ms)
    # Сверка принесла элементы альбома раньше отчёта — свои, без outbox_id.
    _ingest(db, a["account_id"], ext_id="200", ts=now_ms + 100)
    _ingest(db, a["account_id"], ext_id="201", ts=now_ms + 100)
    clock.advance_ms(outbox_mod.FILE_LEASE_MS + 1_000)
    (lease,) = _lease(db, a["account_id"], int(clock() * 1000))
    assert lease.maybe_sent is True
    assert lease.mode == "send"
    assert "200" not in lease.known_own_ext_ids and "201" not in lease.known_own_ext_ids


async def test_p4_single_file_known_own_ext_ids_unchanged(client, db, clock):
    a = await _album(client, db, clock, n=1)
    now_ms = int(clock() * 1000)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = ? WHERE id = ?",
            (json.dumps({"idempotent_send": True}), a["account_id"]),
        )
    )
    _lease(db, a["account_id"], now_ms)
    _ingest(db, a["account_id"], ext_id="210", ts=now_ms + 100)
    clock.advance_ms(outbox_mod.FILE_LEASE_MS + 1_000)
    (lease,) = _lease(db, a["account_id"], int(clock() * 1000))
    assert "210" in lease.known_own_ext_ids  # у одиночного файла Telegram — все свои, как было


# --- тест 11: отмена до попытки -----------------------------------------------------------------------------


async def test_11_cancel_before_attempt_three_files_drops_media_and_files(client, db, clock):
    a = await _album(client, db, clock)
    now_ms = int(clock() * 1000)
    status = db.write_sync(lambda conn: outbox_mod.cancel(conn, a["body"]["outbox_id"], now_ms=now_ms))
    assert status == "cancelled"
    assert _media_rows(db, a["body"]["message_id"]) == []
    for body in a["bodies"]:
        assert not media.file_path(db.data_dir, a["account_id"], _sha(body)).exists()


async def test_11_cancel_keeps_file_shared_with_other_draft(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    same = _png("same")
    ids = await _uploads(client, cookie, channel_id, [same, _png("other")])
    first = (await _send(client, cookie, channel_id, files=ids, key="a")).json()
    (keep,) = await _uploads(client, cookie, channel_id, [same])
    (await _send(client, cookie, channel_id, files=[keep], key="b")).json()
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: outbox_mod.cancel(conn, first["outbox_id"], now_ms=now_ms))
    assert media.file_path(db.data_dir, account_id, _sha(same)).is_file()
    assert not media.file_path(db.data_dir, account_id, _sha(_png("other"))).exists()


# --- тест 12: загрузки почты, беседа-заготовка нового письма ------------------------------------------------


async def test_12_mail_upload_limit_17_mib(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db, source="mail")

    async def one_byte():
        yield b"x"

    r = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=one_byte(),
        headers={**csrf_headers(cookie), "Content-Length": str(uploads.MAIL_MAX_BYTES + 1)},
    )
    assert r.status_code == 413
    assert "17" in r.json()["detail"]
    assert _upload_count(db) == 0
    r = await _upload(
        client, cookie, channel_id, b"%PDF-" + b"\x00" * (uploads.MAIL_MAX_BYTES - 5), name="a.pdf"
    )
    assert r.status_code == 200, r.text
    assert uploads.MAIL_MAX_BYTES == 17 * MIB  # П6


async def test_12_mail_reply_with_files_goes_to_lease(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="mail")
    ids = await _uploads(client, cookie, channel_id, [_pdf("m0"), _png("m1")], names=["a.pdf", "b.png"])
    sent = (await _send(client, cookie, channel_id, text="", files=ids)).json()
    now_ms = int(clock() * 1000)
    (lease,) = _lease(db, account_id, now_ms)
    assert lease.outbox_id == sent["outbox_id"]
    assert lease.text == ""
    assert [f["name"] for f in lease.files] == ["a.pdf", "b.png"]


async def test_12_new_mail_draft_channel_send_with_file_is_accepted(client, db, clock):
    # Ф12-5 A2 (решение 3) отменило П7 Ф11-3b: новое письмо из хаба — с вложениями (было 422).
    cookie, _ = await login_via_code(client, db, clock)
    account_id = insert_account(db, source="mail")
    draft = insert_channel(db, account_id, ext_id=draft_channels.new_temp_ext_id(), kind="mail_thread")
    r = await _upload(client, cookie, draft, _pdf("n"), name="n.pdf")
    assert r.status_code == 200, r.text
    r = await _send(client, cookie, draft, text="Здравствуйте", files=[r.json()["upload_id"]])
    assert r.status_code == 200, r.text
