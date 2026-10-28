"""Ядро куска 7b (дежурный, ярус 1): таблица `agent_item`, ручки `/agent/items`, скоуп `triage`, поле
`unread` сообщения, ретенция карточек. Спецификация: docs/architecture.md/К3, К5, мелочи.

Стиль — как test_mcp_core.py: публичное приложение через ASGITransport (фикстуры `client`/`db`/`clock`).
"""

import json

from hub.core import agent_items, ingest, retention, security
from hub.core.db import WORK_ID_BASE, Db

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    insert_thread,
    login_via_code,
)

DAY_MS = 24 * 60 * 60 * 1000


def _token(db, *, scopes, kind="background", project_ids=None, name="agent:triage"):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _post_headers(token: str) -> dict:
    return {**_bearer(token), "Origin": "http://127.0.0.1:8787"}


def _item(channel_id, message_ids, *, label="reply", summary="сводка", action=None, provider="deepseek"):
    return {
        "channel_id": channel_id,
        "message_ids": message_ids,
        "label": label,
        "summary": summary,
        "action": action,
        "provider": provider,
        "model": "-" if provider == "rules" else "deepseek-flash",
    }


async def _post(client, token, channel_id, message_ids, **kw):
    return await client.post(
        "/api/v1/agent/items", json=_item(channel_id, message_ids, **kw), headers=_post_headers(token)
    )


async def _status(client, token, url, status):
    return await client.post(url, json={"status": status}, headers=_post_headers(token))


def _rows(db, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, channel_id, label, summary, action, message_ids_json, last_message_id, status, "  # noqa: S608
            f"provider, purged FROM {schema}.agent_item ORDER BY id"
        ).fetchall()


def _setup(db, *, db_name="main", project=True):
    pid = insert_project(db, name="P") if project else None
    acc = insert_account(db, db_name=db_name)
    cid = insert_channel(db, acc, project_id=pid, ext_id="c1", db_name=db_name, title="Вася")
    m1 = insert_message(db, cid, ext_id="1", ts=1000)
    m2 = insert_message(db, cid, ext_id="2", ts=2000)
    m3 = insert_message(db, cid, ext_id="3", ts=3000)
    return pid, acc, cid, (m1, m2, m3)


# --- Тест 1: миграция на обоих файлах, id в work от 2^40, повторный старт -------------------------------


def test_7b_1_migration_both_files_and_work_id_range(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    try:
        acc = insert_account(instance, db_name="work")
        cid = insert_channel(instance, acc, ext_id="w1", db_name="work")
        mid = insert_message(instance, cid, ext_id="1", ts=1000)
        with instance.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {schema}.sqlite_master WHERE name = 'agent_item'"  # noqa: S608
                ).fetchone() == (1,)
            version = conn.execute("PRAGMA work.user_version").fetchone()[0]
        result = instance.write_sync(
            lambda conn: agent_items.upsert(
                conn,
                channel_id=cid,
                message_ids=[mid],
                label="fyi",
                summary="s",
                action=None,
                provider="deepseek",
                model="m",
                api_token_id=None,
                now_ms=5000,
            )
        )
        assert result.item_id > WORK_ID_BASE
    finally:
        instance.close()

    again = Db(tmp_path)
    again.start()
    try:
        with again.reader() as conn:
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == version
            assert conn.execute("SELECT COUNT(*) FROM work.agent_item").fetchone() == (1,)
            dupes = conn.execute(
                "SELECT name FROM work.sqlite_sequence GROUP BY name HAVING COUNT(*) > 1"
            ).fetchall()
        assert dupes == []
    finally:
        again.close()


async def test_7b_1_work_card_via_api_gets_work_id(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, _m2, _m3) = _setup(db, db_name="work")
    r = await _post(client, token, cid, [m1])
    assert r.status_code == 200, r.text
    assert r.json()["id"] > WORK_ID_BASE and r.json()["created"] is True
    r = await client.get("/api/v1/agent/items", headers=_bearer(token))
    assert [row["id"] for row in r.json()] == [_rows(db, "work")[0][0]]


# --- Тест 2 (+П2): upsert открытой карточки, слияние без понижения метки, rules не затирает LLM -----------


async def test_7b_2_upsert_merges_open_card_and_new_card_after_seen(client, db, clock):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, m3) = _setup(db)

    r = await _post(client, token, cid, [m1], label="urgent", summary="A")
    first = r.json()["id"]
    r = await _post(client, token, cid, [m2], label="noise", summary="B")
    assert r.json() == {"id": first, "created": False, "noop": False, "status": "new"}
    rows = _rows(db)
    assert len(rows) == 1
    _id, _cid, label, summary, _action, ids_json, last_id, status, _prov, _purged = rows[0]
    assert (label, summary, json.loads(ids_json), last_id, status) == ("urgent", "B", [m1, m2], m2, "new")

    ui_cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/agent/items/{first}/status", json={"status": "seen"}, headers=csrf_headers(ui_cookie)
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    r = await _post(client, token, cid, [m3])
    assert r.json()["created"] is True and r.json()["id"] != first
    assert [row[7] for row in _rows(db)] == ["seen", "new"]


async def test_7b_p2_f57_rules_card_overwrites_llm_content(client, db):
    """Ф57-1 П6: карточка по правилам всегда перезаписывает пересказ открытой карточки — и карточки
    прежней модели тоже (раньше `rules` поверх LLM содержание не трогал, и оно оставалось навсегда)."""
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, m3) = _setup(db)
    await _post(client, token, cid, [m1], label="fyi", summary="от LLM", action="прочитать")
    await client.post(
        "/api/v1/agent/items",
        json=_item(cid, [m2], label="reply", summary="Пётр: вопрос", provider="rules"),
        headers=_post_headers(token),
    )
    (row,) = _rows(db)
    assert row[3:5] == ("Пётр: вопрос", None) and row[8] == "rules"
    assert json.loads(row[5]) == [m1, m2] and row[6] == m2
    assert row[2] == "reply"  # метка не понижается и берёт максимум

    # И обратно: карточка rules затирается карточкой другого провайдера.
    _pid2, _acc2, cid2, (n1, n2, _n3) = _setup(db, project=False)
    await _post(client, token, cid2, [n1], summary="не разобрано", provider="rules")
    await _post(client, token, cid2, [n2], label="fyi", summary="LLM")
    row2 = [r for r in _rows(db) if r[1] == cid2][0]
    assert (row2[2], row2[3], row2[8]) == ("reply", "LLM", "deepseek")


async def test_7b_p5_last_message_by_position_not_insert_order(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, _m2, _m3) = _setup(db)
    late_old = insert_message(db, cid, ext_id="0", ts=500)  # вставлено позже, но по позиции раньше всех
    r = await _post(client, token, cid, [late_old, m1])
    assert r.status_code == 200
    (row,) = _rows(db)
    assert row[6] == m1 and json.loads(row[5]) == [late_old, m1]


# --- Тест 3: чужой/невидимый message_id, невидимый канал, без скоупа -> ничего не записано ----------------


async def test_7b_3_invalid_messages_channel_scope_nothing_written(client, db):
    pid, acc, cid, (m1, _m2, _m3) = _setup(db)
    other_pid = insert_project(db, name="Чужой")
    other_cid = insert_channel(db, acc, project_id=other_pid, ext_id="c2")
    foreign = insert_message(db, other_cid, ext_id="9", ts=1000)
    proj_token = _token(db, scopes=("read", "triage"), project_ids=[pid])
    read_only = _token(db, scopes=("read",), name="r")

    r = await _post(client, proj_token, cid, [m1, foreign])
    assert r.status_code == 422
    r = await _post(client, proj_token, cid, [m1, 999])
    assert r.status_code == 422
    r = await _post(client, proj_token, other_cid, [foreign])
    assert r.status_code == 404
    r = await _post(client, read_only, cid, [m1])
    assert r.status_code == 403
    r = await _post(client, proj_token, cid, [])
    assert r.status_code == 422
    assert _rows(db) == []


async def test_7b_extra_message_id_from_other_file_is_422(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid_work, (w1, _w2, _w3) = _setup(db, db_name="work")
    _pid2, _acc2, _cid_main, (main_msg, _x, _y) = _setup(db)
    r = await _post(client, token, cid_work, [w1, main_msg])
    assert r.status_code == 422
    assert _rows(db, "work") == [] and _rows(db) == []


# --- Тест 4: проектный токен и «Неразобранное» ------------------------------------------------------------


async def test_7b_4_project_scope_and_unsorted(client, db):
    writer = _token(db, scopes=("read", "triage"))
    pid, acc, cid, (m1, _m2, _m3) = _setup(db)
    other_pid = insert_project(db, name="B")
    other_cid = insert_channel(db, acc, project_id=other_pid, ext_id="c2")
    om = insert_message(db, other_cid, ext_id="1", ts=1000)
    unsorted_cid = insert_channel(db, acc, project_id=None, ext_id="c3")
    um = insert_message(db, unsorted_cid, ext_id="1", ts=1000)
    for c, m in ((cid, m1), (other_cid, om), (unsorted_cid, um)):
        r = await _post(client, writer, c, [m])
        assert r.status_code == 200, r.text

    proj = _token(db, scopes=("read",), kind="interactive", project_ids=[pid], name="p")
    r = await client.get("/api/v1/agent/items", headers=_bearer(proj))
    assert [row["channel_id"] for row in r.json()] == [cid]
    assert (await client.get("/api/v1/agent/items?project=-1", headers=_bearer(proj))).status_code == 404
    assert (
        await client.get(f"/api/v1/agent/items?project={other_pid}", headers=_bearer(proj))
    ).status_code == 404

    all_no_org = _token(db, scopes=("read",), kind="interactive", name="all")
    assert (
        await client.get("/api/v1/agent/items?project=-1", headers=_bearer(all_no_org))
    ).status_code == 404
    organize = _token(db, scopes=("read", "organize"), kind="interactive", name="org")
    r = await client.get("/api/v1/agent/items?project=-1", headers=_bearer(organize))
    assert [row["channel_id"] for row in r.json()] == [unsorted_cid]
    r = await client.get(f"/api/v1/agent/items?project={pid}", headers=_bearer(organize))
    row = r.json()[0]
    assert (row["project_id"], row["channel_title"], row["kind"], row["source"]) == (pid, "Вася", "dm", "tg")


async def test_7b_get_filters_status_label_channel_any(client, db, clock):
    token = _token(db, scopes=("read", "triage"))
    interactive = _token(db, scopes=("read",), kind="interactive", name="i")
    _pid, acc, cid, (m1, m2, _m3) = _setup(db)
    cid2 = insert_channel(db, acc, ext_id="c9")
    n1 = insert_message(db, cid2, ext_id="1", ts=1000)
    first = (await _post(client, token, cid, [m1])).json()
    await _post(client, token, cid2, [n1], label="noise")
    await _status(client, interactive, f"/api/v1/agent/items/{first['id']}/status", "done")
    clock.advance_ms(1000)
    second = (await _post(client, token, cid, [m2])).json()

    def ids(resp):
        return [row["id"] for row in resp.json()]

    assert set(ids(await client.get("/api/v1/agent/items", headers=_bearer(token)))) == {
        second["id"],
        second["id"] - 1,
    }
    r = await client.get(f"/api/v1/agent/items?channel={cid}&status=any&limit=1", headers=_bearer(token))
    assert ids(r) == [second["id"]] and r.json()[0]["status"] == "new"
    r = await client.get("/api/v1/agent/items?status=done", headers=_bearer(token))
    assert ids(r) == [first["id"]]
    r = await client.get("/api/v1/agent/items?label=noise", headers=_bearer(token))
    assert [row["channel_id"] for row in r.json()] == [cid2]
    assert (await client.get("/api/v1/agent/items?status=bogus", headers=_bearer(token))).status_code == 422
    assert (await client.get("/api/v1/agent/items?label=bogus", headers=_bearer(token))).status_code == 422


# --- Тест 5: смена статуса --------------------------------------------------------------------------------


async def test_7b_5_status_transitions_and_rights(client, db, clock):
    writer = _token(db, scopes=("read", "triage"))
    interactive = _token(db, scopes=("read",), kind="interactive", name="i")
    pid, acc, cid, (m1, _m2, _m3) = _setup(db)
    item = (await _post(client, writer, cid, [m1])).json()
    url = f"/api/v1/agent/items/{item['id']}/status"

    r = await client.post(url, json={"status": "seen"}, headers=_post_headers(writer))
    assert r.status_code == 403  # background свои карточки не закрывает
    r = await client.post(url, json={"status": "new"}, headers=_post_headers(interactive))
    assert r.status_code == 422
    r = await client.post(url, json={"status": "seen"}, headers=_post_headers(interactive))
    assert r.status_code == 200 and r.json()["status"] == "seen"
    r = await client.post(url, json={"status": "done"}, headers=_post_headers(interactive))
    assert r.status_code == 200
    r = await client.post(url, json={"status": "dismissed"}, headers=_post_headers(interactive))
    assert r.status_code == 409

    other_pid = insert_project(db, name="B")
    invisible = _token(db, scopes=("read",), kind="interactive", project_ids=[other_pid], name="x")
    r = await client.post(url, json={"status": "done"}, headers=_post_headers(invisible))
    assert r.status_code == 404
    r = await _status(client, interactive, "/api/v1/agent/items/999/status", "done")
    assert r.status_code == 404

    # UI-сессия тоже может.
    cid2 = insert_channel(db, acc, project_id=pid, ext_id="c7")
    n1 = insert_message(db, cid2, ext_id="1", ts=1000)
    item2 = (await _post(client, writer, cid2, [n1])).json()
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/agent/items/{item2['id']}/status",
        json={"status": "dismissed"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()


# --- П10/К3: идемпотентность по позиции ------------------------------------------------------------------


async def test_7b_k3_noop_by_covered_seq_any_status(client, db):
    """К3 после приёмки (F1): no-op — если у ВСЕХ `message_ids` `first_seq` ≤ `covered_seq` самой свежей
    карточки беседы (любой статус)."""
    writer = _token(db, scopes=("read", "triage"))
    interactive = _token(db, scopes=("read",), kind="interactive", name="i")
    _pid, _acc, cid, (m1, m2, m3) = _setup(db)
    item = (await _post(client, writer, cid, [m1, m2])).json()
    await _status(client, interactive, f"/api/v1/agent/items/{item['id']}/status", "done")

    r = await _post(client, writer, cid, [m1])
    assert r.json() == {"id": item["id"], "created": False, "noop": True, "status": "done"}
    assert len(_rows(db)) == 1
    r = await _post(client, writer, cid, [m2, m3])
    assert r.json()["created"] is True and len(_rows(db)) == 2
    rows = (await client.get("/api/v1/agent/items?status=any", headers=_bearer(writer))).json()
    with db.reader() as conn:
        seqs = dict(conn.execute("SELECT id, first_seq FROM main.message").fetchall())
    assert sorted(row["covered_seq"] for row in rows) == [seqs[m2], seqs[m3]]


async def test_7b_f1_late_older_ts_message_is_not_covered_and_merges(client, db):
    """F1: утром живое сообщение 09:00 создаёт карточку; догон после сна вставляет сообщение 03:00 (позиция
    СТАРШЕ последнего сообщения карточки, но `first_seq` новее) — оно не no-op, а вливается в открытую
    карточку. Повторная подача того же — no-op."""
    writer = _token(db, scopes=("read", "triage"))
    pid = insert_project(db)
    acc = insert_account(db)
    cid = insert_channel(db, acc, project_id=pid, ext_id="c1")
    morning = insert_message(db, cid, ext_id="900", ts=9 * 3_600_000)
    first = (await _post(client, writer, cid, [morning], label="reply", summary="утро")).json()
    assert first["created"] is True

    night = insert_message(db, cid, ext_id="300", ts=3 * 3_600_000)  # догон: ts старше, first_seq новее
    r = await _post(client, writer, cid, [night], label="urgent", summary="ночь и утро")
    assert r.json() == {"id": first["id"], "created": False, "noop": False, "status": "new"}
    (row,) = _rows(db)
    assert (row[2], row[3]) == ("urgent", "ночь и утро")
    assert json.loads(row[5]) == [night, morning] and row[6] == morning  # last_message_id — по позиции

    r = await _post(client, writer, cid, [night, morning])
    assert r.json() == {"id": first["id"], "created": False, "noop": True, "status": "new"}


# --- П5: answered / read ---------------------------------------------------------------------------------


async def test_7b_p5_answered_and_read_computed_on_read(client, db):
    writer = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, _m3) = _setup(db)
    await _post(client, writer, cid, [m2])
    row = (await client.get("/api/v1/agent/items", headers=_bearer(writer))).json()[0]
    assert (row["answered"], row["read"]) == (False, False)

    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET last_read_ts = 2000, last_read_sort = 2, last_read_ext = '2' "
            "WHERE id = ?",
            (cid,),
        )
    )
    insert_message(db, cid, ext_id="10", ts=2500, is_me=1)
    row = (await client.get("/api/v1/agent/items", headers=_bearer(writer))).json()[0]
    assert (row["answered"], row["read"]) == (True, True)
    assert row["last_message_ts"] == 2000 and row["last_message_ext_sort"] == 2
    del m1


async def test_7b_p5_read_of_thread_reply_uses_thread_marker(client, db):
    writer = _token(db, scopes=("read", "triage"))
    pid = insert_project(db)
    acc = insert_account(db, source="slack")
    cid = insert_channel(db, acc, project_id=pid, ext_id="C1", kind="group")
    insert_message(db, cid, ext_id="100.0", ts=1000)
    reply = insert_message(db, cid, ext_id="101.0", ts=1100, thread_ext_id="100.0")
    insert_thread(db, cid, ext_id="100.0", subscribed=1)
    await _post(client, writer, cid, [reply])
    # Маркер канала выше ответа — но ответ в треде читается по маркеру треда.
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET last_read_ts = 5000, last_read_sort = 0, last_read_ext = 'z' "
            "WHERE id = ?",
            (cid,),
        )
    )
    row = (await client.get("/api/v1/agent/items", headers=_bearer(writer))).json()[0]
    assert row["read"] is False
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.thread SET last_read_ts = 1100, last_read_sort = 0, last_read_ext = '101.0' "
            "WHERE channel_id = ?",
            (cid,),
        )
    )
    row = (await client.get("/api/v1/agent/items", headers=_bearer(writer))).json()[0]
    assert row["read"] is True


# --- Тест 6 (+П4): ретенция карточек work ------------------------------------------------------------------


def _upsert_direct(db, cid, mids, now_ms, **kw):
    return db.write_sync(
        lambda conn: agent_items.upsert(
            conn,
            channel_id=cid,
            message_ids=mids,
            label=kw.get("label", "reply"),
            summary=kw.get("summary", "содержание"),
            action=kw.get("action", "действие"),
            provider="deepseek",
            model="m",
            api_token_id=None,
            now_ms=now_ms,
        )
    ).item_id


def test_7b_6_retention_purges_old_work_cards_not_main(db):
    now0 = 1_800_000_000_000
    old_ts = now0 - 100 * DAY_MS
    acc_w = insert_account(db, db_name="work", source="slack")
    cid_w = insert_channel(db, acc_w, ext_id="w", db_name="work")
    w_old = insert_message(db, cid_w, ext_id="1", ts=old_ts)
    acc_m = insert_account(db, db_name="main")
    cid_m = insert_channel(db, acc_m, ext_id="m")
    m_old = insert_message(db, cid_m, ext_id="1", ts=old_ts)
    old_open = _upsert_direct(db, cid_w, [w_old], old_ts)
    main_card = _upsert_direct(db, cid_m, [m_old], old_ts)
    # Опорное время источников — свежее (иначе отсечка не сдвинется).
    db.write_sync(lambda conn: retention.bump_source_time_hwm(conn, now0, now0))

    result = retention.run_once(db, now0)
    assert result is not None and result["agent_items"] == 1
    with db.reader() as conn:
        w = conn.execute(
            "SELECT summary, action, purged, status FROM work.agent_item WHERE id = ?", (old_open,)
        ).fetchone()
        m = conn.execute(
            "SELECT summary, action, purged, status FROM main.agent_item WHERE id = ?", (main_card,)
        ).fetchone()
    assert w == (None, None, 1, "dismissed")
    assert m == ("содержание", "действие", 0, "new")

    # Новый разбор беседы после ретенции — новая карточка, а не запись в стёртую.
    w_new = insert_message(db, cid_w, ext_id="2", ts=now0)
    new_id = _upsert_direct(db, cid_w, [w_new], now0)
    assert new_id != old_open
    assert retention.run_once(db, now0 + 1000) is None


def test_7b_p4_upsert_on_purged_open_card_creates_new(db):
    acc = insert_account(db, db_name="work", source="slack")
    cid = insert_channel(db, acc, ext_id="w", db_name="work")
    m1 = insert_message(db, cid, ext_id="1", ts=1000)
    m2 = insert_message(db, cid, ext_id="2", ts=2000)
    first = _upsert_direct(db, cid, [m1], 1000)
    # Стёртая, но почему-то открытая (страховка сверх ретенции).
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE work.agent_item SET purged = 1, summary = NULL WHERE id = ?", (first,)
        )
    )
    second = _upsert_direct(db, cid, [m2], 2000)
    assert second != first
    rows = _rows(db, "work")
    assert [(r[0], r[7]) for r in rows] == [(first, "dismissed"), (second, "new")]


# --- П1/К5: поле `unread` в выдачах совпадает со счётчиками ------------------------------------------------


async def test_7b_k5_unread_field_matches_counters(client, db):
    token = _token(db, scopes=("read",), kind="interactive", name="i")
    pid = insert_project(db)
    acc = insert_account(db, source="slack")

    def _events():
        base = {"is_me": False, "kind": "text", "text": "x", "mentions_me": False}
        ch = {"ext_id": "C1", "kind": "group", "title": "общий"}
        evs = [
            {"type": "upsert", "channel": ch, "ext_id": f"{i}.0", "ts": i * 1000, **base} for i in range(1, 6)
        ]
        evs.append({"type": "upsert", "channel": ch, "ext_id": "6.0", "ts": 6000, **base, "is_me": True})
        # Тред на 2.0: ответы, я отвечал — тред подписан.
        evs += [
            {
                "type": "upsert",
                "channel": ch,
                "ext_id": f"2.{i}",
                "ts": 2000 + i,
                "thread_ext_id": "2.0",
                **base,
            }
            for i in range(1, 4)
        ]
        evs.append(
            {
                "type": "upsert",
                "channel": ch,
                "ext_id": "2.9",
                "ts": 2009,
                "thread_ext_id": "2.0",
                **base,
                "is_me": True,
            }
        )
        evs.append(
            {"type": "upsert", "channel": ch, "ext_id": "2.10", "ts": 2010, "thread_ext_id": "2.0", **base}
        )
        evs.append({"type": "read", "channel": ch, "up_to_ext_id": "3.0"})
        return evs

    db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=acc, events=_events(), cursor=None, now_ms=10_000)
    )
    with db.reader() as conn:
        cid = conn.execute("SELECT id FROM main.channel WHERE ext_id = 'C1'").fetchone()[0]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (pid, cid))
    )
    with db.reader() as conn:
        unread_top, unread_threads = ingest.unread_breakdown(conn, cid)

    page = (await client.get(f"/api/v1/channels/{cid}/messages?limit=200", headers=_bearer(token))).json()
    assert sum(m["unread"] for m in page["messages"]) == unread_top
    assert [m["ext_id"] for m in page["messages"] if m["unread"]] == ["4.0", "5.0"]
    assert all(not m["unread"] for m in page["messages"] if m["is_me"])

    thread = (await client.get(f"/api/v1/channels/{cid}/threads/2.0/messages", headers=_bearer(token))).json()
    assert unread_threads > 0
    assert sum(m["unread"] for m in thread["messages"]) == unread_threads
    assert thread["parent"]["unread"] is False  # 2.0 до маркера канала

    sync = (await client.get("/api/v1/sync?since=0&limit=500", headers=_bearer(token))).json()
    by_ext = {m["ext_id"]: m["unread"] for m in sync["messages"]}
    for m in page["messages"] + thread["messages"]:
        assert by_ext[m["ext_id"]] == m["unread"]
