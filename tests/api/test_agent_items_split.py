"""Ф11-2 (docs/architecture.md), раздел A: граница карточки — ответ или прочтение владельца.
Отвеченная открытая карточка -> `done`, прочитанная -> `seen`, новые сообщения — новая карточка;
`POST /agent/items/close-answered`. Тесты 1–5 спеки (тест 6 — существующие тесты выдачи)."""

import json

from .conftest import insert_account, insert_channel, insert_message, insert_project, insert_thread
from .test_agent_items import _bearer, _post, _post_headers, _rows, _setup, _token


def _mark_channel_read(db, cid, ts, sort, ext):
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET last_read_ts = ?, last_read_sort = ?, last_read_ext = ? WHERE id = ?",
            (ts, sort, ext, cid),
        )
    )


async def _close(client, token, cid):
    return await client.post(
        "/api/v1/agent/items/close-answered", json={"channel_id": cid}, headers=_post_headers(token)
    )


# --- Тест 1: отвечена -> старая done, новая только с новыми id, метка переданная -------------------------


async def test_f11_2_1_answered_open_card_closed_and_new_card(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, _m3) = _setup(db)
    first = (await _post(client, token, cid, [m1, m2], label="urgent", summary="старое")).json()
    insert_message(db, cid, ext_id="10", ts=2500, is_me=1)  # мой ответ после последнего сообщения карточки
    new_msg = insert_message(db, cid, ext_id="11", ts=4000)

    r = await _post(client, token, cid, [new_msg], label="fyi", summary="новое")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["noop"] is False
    assert body["closed_item_id"] == first["id"] and body["closed_status"] == "done"
    rows = _rows(db)
    assert [(row[0], row[7]) for row in rows] == [(first["id"], "done"), (body["id"], "new")]
    new_row = rows[1]
    assert json.loads(new_row[5]) == [new_msg] and new_row[6] == new_msg
    assert (new_row[2], new_row[3]) == ("fyi", "новое")  # метка — переданная, не максимум со старой


# --- Тест 2: прочитана без ответа -> seen, новая с меткой max(старая, новая) --------------------------------


async def test_f11_2_2_read_open_card_seen_new_card_keeps_label(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, m3) = _setup(db)
    first = (await _post(client, token, cid, [m1, m2], label="reply")).json()
    _mark_channel_read(db, cid, 2000, 2, "2")

    r = await _post(client, token, cid, [m3], label="fyi", summary="новое")
    body = r.json()
    assert body["created"] is True
    assert body["closed_item_id"] == first["id"] and body["closed_status"] == "seen"
    rows = _rows(db)
    assert [(row[0], row[7]) for row in rows] == [(first["id"], "seen"), (body["id"], "new")]
    assert rows[1][2] == "reply" and json.loads(rows[1][5]) == [m3]


# --- Тест 3: живая карточка -> слияние как раньше -----------------------------------------------------------


async def test_f11_2_3_live_open_card_merges(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, m3) = _setup(db)
    # Моё сообщение РАНЬШЕ последнего сообщения карточки и маркер ниже него — карточка живая.
    insert_message(db, cid, ext_id="0", ts=500, is_me=1)
    _mark_channel_read(db, cid, 1000, 1, "1")
    first = (await _post(client, token, cid, [m1, m2], label="urgent")).json()
    r = await _post(client, token, cid, [m3], label="fyi", summary="слито")
    assert r.json() == {"id": first["id"], "created": False, "noop": False, "status": "new"}
    (row,) = _rows(db)
    assert (row[2], row[3], row[7]) == ("urgent", "слито", "new")
    assert json.loads(row[5]) == [m1, m2, m3]


# --- Тест 4: ответ в треде — «прочитана» только по маркеру треда --------------------------------------------


async def test_f11_2_4_thread_card_split_only_by_thread_marker(client, db):
    token = _token(db, scopes=("read", "triage"))
    pid = insert_project(db)
    acc = insert_account(db, source="slack")
    cid = insert_channel(db, acc, project_id=pid, ext_id="C1", kind="group")
    insert_message(db, cid, ext_id="100.0", ts=1000)
    reply = insert_message(db, cid, ext_id="101.0", ts=1100, thread_ext_id="100.0")
    insert_thread(db, cid, ext_id="100.0", subscribed=1)
    first = (await _post(client, token, cid, [reply])).json()

    # Маркер беседы верхнего уровня выше ответа — карточку по ответу в треде это не делит.
    _mark_channel_read(db, cid, 5000, 0, "z")
    reply2 = insert_message(db, cid, ext_id="102.0", ts=1200, thread_ext_id="100.0")
    r = await _post(client, token, cid, [reply2])
    assert r.json() == {"id": first["id"], "created": False, "noop": False, "status": "new"}

    # Маркер треда дошёл до последнего сообщения карточки — делит.
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.thread SET last_read_ts = 1200, last_read_sort = 0, last_read_ext = '102.0' "
            "WHERE channel_id = ?",
            (cid,),
        )
    )
    reply3 = insert_message(db, cid, ext_id="103.0", ts=1300, thread_ext_id="100.0")
    body = (await _post(client, token, cid, [reply3])).json()
    assert body["created"] is True and body["closed_item_id"] == first["id"]
    assert body["closed_status"] == "seen"


# --- Тест 5: close-answered ---------------------------------------------------------------------------------


async def test_f11_2_5_close_answered_rights_and_effect(client, db):
    token = _token(db, scopes=("read", "triage"))
    pid, acc, cid, (m1, m2, _m3) = _setup(db)
    item = (await _post(client, token, cid, [m1, m2])).json()

    r = await _close(client, token, cid)
    assert r.status_code == 200 and r.json() == {"closed": []}
    assert _rows(db)[0][7] == "new"

    # Прочтение — не ответ: карточка остаётся открытой.
    _mark_channel_read(db, cid, 2000, 2, "2")
    assert (await _close(client, token, cid)).json() == {"closed": []}
    # Заготовка (без ext_id) — не ответ.
    insert_message(db, cid, ext_id=None, ts=3000, is_me=1)
    assert (await _close(client, token, cid)).json() == {"closed": []}

    insert_message(db, cid, ext_id="10", ts=2500, is_me=1)
    r = await _close(client, token, cid)
    assert r.status_code == 200 and r.json() == {"closed": [item["id"]]}
    assert _rows(db)[0][7] == "done"
    assert (await _close(client, token, cid)).json() == {"closed": []}  # прочие статусы не трогает

    # Права: беседа чужого проекта для проектного токена -> 404; без `triage` -> 403.
    other_pid = insert_project(db, name="Чужой")
    other_cid = insert_channel(db, acc, project_id=other_pid, ext_id="c2")
    proj = _token(db, scopes=("read", "triage"), project_ids=[pid], name="p")
    assert (await _close(client, proj, other_cid)).status_code == 404
    assert (await _close(client, proj, cid)).status_code == 200
    read_only = _token(db, scopes=("read",), kind="interactive", name="r")
    assert (await _close(client, read_only, cid)).status_code == 403
    assert (await _close(client, token, 999_999)).status_code == 404


async def test_f11_2_5_close_answered_leaves_other_statuses(client, db):
    token = _token(db, scopes=("read", "triage"))
    interactive = _token(db, scopes=("read",), kind="interactive", name="i")
    _pid, _acc, cid, (m1, _m2, _m3) = _setup(db)
    item = (await _post(client, token, cid, [m1])).json()
    r = await client.post(
        f"/api/v1/agent/items/{item['id']}/status",
        json={"status": "seen"},
        headers=_post_headers(interactive),
    )
    assert r.status_code == 200
    insert_message(db, cid, ext_id="10", ts=2500, is_me=1)
    assert (await _close(client, token, cid)).json() == {"closed": []}
    assert _rows(db)[0][7] == "seen"
    rows = (await client.get("/api/v1/agent/items?status=any", headers=_bearer(token))).json()
    assert rows[0]["answered"] is True


# --- Ф11-9 H4: новая карточка, рождённая уже отвеченной, сразу закрыта -----------------------------------


async def test_f11_9_h4_new_card_after_my_reply_is_done(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, _m3) = _setup(db)
    # Кандидаты копились до моего ответа, карточка по ним пришла после (дебаунс, очередь модели).
    insert_message(db, cid, ext_id="10", ts=2500, is_me=1)

    r = await _post(client, token, cid, [m1, m2], label="urgent")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"id": body["id"], "created": True, "noop": False, "status": "done"}
    assert [(row[0], row[7]) for row in _rows(db)] == [(body["id"], "done")]

    # Повтор той же карточки — no-op со статусом итоговой (закрытой) карточки.
    again = (await _post(client, token, cid, [m1, m2], label="urgent")).json()
    assert again == {"id": body["id"], "created": False, "noop": True, "status": "done"}


async def test_f11_9_h4_new_card_read_without_reply_stays_new(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, _m3) = _setup(db)
    _mark_channel_read(db, cid, 2000, 2, "2")

    body = (await _post(client, token, cid, [m1, m2])).json()
    assert body["created"] is True and body["status"] == "new"
    assert _rows(db)[0][7] == "new"


async def test_f11_9_h4_my_message_before_card_messages_stays_new(client, db):
    token = _token(db, scopes=("read", "triage"))
    _pid, _acc, cid, (m1, m2, _m3) = _setup(db)
    insert_message(db, cid, ext_id="0", ts=500, is_me=1)  # моё РАНЬШЕ сообщений карточки — не ответ на них

    body = (await _post(client, token, cid, [m1, m2])).json()
    assert body["created"] is True and body["status"] == "new"
    assert _rows(db)[0][7] == "new"
