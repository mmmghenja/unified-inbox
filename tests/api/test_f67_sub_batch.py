"""Ф67-1 (docs/architecture.md): интервал пачки `batch_min` у любой подписки — на проект и на
беседу, с ключами и без.

- Подписка на беседу без ключей с `batch_min=5`: три сообщения подряд — один сигнал после срока; личное
  посреди срока — сразу и с накопленным; `batch_min=0` — как раньше.
- Подписка на проект «каждое сообщение» с пачкой — то же; личка в другой беседе забирает накопленное.
- Старые подписки с ключами — интервал 15, как был.
- Карточки (`cards`) с пачкой ждут срока; срочная карточка — сразу.
- Хранение: пара без ключей в `keywords_json`, наследование после /clear, смена фильтра и `keywords=[]`
  интервал не теряют; `direct` — 422; строки подписок несут действующий `batch_min`.

Окружение и помощники — из test_f36_keyword_batch.py (тот же мир, часы `FakeClock`)."""

import json

import pytest

from hub.core import keywords, security, subs

from .conftest import insert_account, insert_channel, insert_project
from .test_f36_keyword_batch import (
    A2,
    KEYS,
    MIN_MS,
    A,
    B,
    _chan_sub,
    _chan_wait,
    _ev,
    _grid,
    _h,
    _ingest,
    _msg_id,
    _now,
    _rows,
    _sub,
    _sub_rec,
    _wait,
)


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, tmp_path):
    """Тот же мир, что у test_f36_keyword_batch.py: проект с подписками, беседы G1–G3 и личка D1."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Hooli", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="telegram")
    groups = ("G1", "G2", "G3")
    ch = {ext: insert_channel(db, acc, project_id=pid, ext_id=ext, kind="group") for ext in groups}
    ch["D1"] = insert_channel(db, acc, project_id=pid, ext_id="D1", kind="dm")
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    return {"pid": pid, "workdir": str(workdir), "acc": acc, "ch": ch, "tok": token}


def _chan_rec(db, chan_sub_id):
    from hub.core import chan_subs

    with db.reader() as conn:
        return chan_subs.get(conn, chan_sub_id)


# --- подписка на беседу без ключей -----------------------------------------------------------------------


async def test_chan_sub_without_keys_batches_three_messages_into_one_signal(client, db, world, procs, clock):
    body = await _chan_sub(client, world, A, "G1", batch_min=5)
    sub = body["sub"]
    assert body["created"] is True
    assert sub["keywords"] is None and sub["batch_min"] == 5
    assert json.loads(_chan_rec(db, sub["id"]).keywords_json) == {"keys": [], "exclude": [], "batch_min": 5}
    now = _now(clock)
    for i in range(3):
        _ingest(db, world, [_ev("G1", str(i), now + i * 1000, text=f"заявка {i}")], now + i * 1000)
    ready = _grid(now, 5)
    (row,) = _rows(db)
    assert (row["status"], row["cat"], row["msg_count"], row["ready_ts"]) == ("pending", "other", 3, ready)
    assert await _chan_wait(client, world, A) == []  # до срока — тишина
    clock.advance_ms(ready - _now(clock) - 1)
    assert await _chan_wait(client, world, A) == []
    clock.advance_ms(1)
    signals = await _chan_wait(client, world, A)
    assert [(s["channel_id"], s["cat"], s["msg_count"]) for s in signals] == [(world["ch"]["G1"], "other", 3)]
    assert [r["status"] for r in _rows(db)] == ["delivered"]


async def test_chan_sub_personal_mid_batch_goes_at_once_with_accumulated(client, db, world, procs, clock):
    await _chan_sub(client, world, A, "G1", batch_min=5)
    await _chan_sub(client, world, A, "G2", batch_min=5)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="заявка")], now)
    _ingest(db, world, [_ev("G2", "1", now + 1, text="заявка")], now + 1)
    assert await _chan_wait(client, world, A) == []
    # упоминание в G2: строка G2 личная и готова, выход сразу; накопленное в G1 — тем же выходом
    _ingest(db, world, [_ev("G2", "2", now + 2, text="ты тут?", mention=True)], now + 2)
    g2 = _rows(db, world["ch"]["G2"])[-1]
    assert (g2["cat"], g2["ready_ts"], g2["msg_count"]) == ("mention", None, 2)
    signals = await _chan_wait(client, world, A)
    assert [(s["channel_id"], s["cat"]) for s in signals] == [
        (world["ch"]["G2"], "mention"),
        (world["ch"]["G1"], "other"),
    ]


async def test_chan_sub_batch_zero_is_immediate_as_before(client, db, world, procs, clock):
    body = await _chan_sub(client, world, A, "G1", batch_min=0)
    assert body["sub"]["batch_min"] == 0
    assert _chan_rec(db, body["sub"]["id"]).keywords_json is None  # 0 без ключей — как без интервала
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="заявка")], now)
    assert _rows(db)[0]["ready_ts"] is None
    assert [s["cat"] for s in await _chan_wait(client, world, A)] == ["other"]


async def test_chan_sub_interval_alone_and_then_keys_keep_it(client, db, world, procs, clock):
    sub = (await _chan_sub(client, world, A, "G1"))["sub"]
    assert sub["batch_min"] == 0
    body = await _chan_sub(client, world, A, "G1", batch_min=7)
    assert body["created"] is False and body["sub"]["batch_min"] == 7
    # ключи без `batch_min` — интервал прежний; снятие ключей — интервал тоже остаётся
    body = await _chan_sub(client, world, A, "G1", **KEYS)
    assert body["sub"]["keywords"]["batch_min"] == 7 and body["sub"]["batch_min"] == 7
    body = await _chan_sub(client, world, A, "G1", keywords=[])
    assert body["sub"]["keywords"] is None and body["sub"]["batch_min"] == 7


# --- подписка на проект «каждое сообщение» ----------------------------------------------------------------


async def test_project_messages_batch_and_dm_takes_accumulated(client, db, world, procs, clock):
    body = await _sub(client, world, A, mode="messages", batch_min=5)
    sub = body["sub"]["id"]
    assert body["sub"]["keywords"] is None and body["sub"]["batch_min"] == 5
    now = _now(clock)
    for i in range(3):
        _ingest(db, world, [_ev("G1", str(i), now + i, text="заявка")], now + i)
    ready = _grid(now, 5)
    (row,) = _rows(db)
    assert (row["cat"], row["msg_count"], row["ready_ts"]) == ("other", 3, ready)
    assert await _wait(client, world, sub, A) == []
    _ingest(db, world, [_ev("D1", "1", now + 10, kind="dm", text="привет")], now + 10)
    signals = await _wait(client, world, sub, A)
    assert [(s["channel_id"], s["cat"], s["msg_count"]) for s in signals] == [
        (world["ch"]["D1"], "dm", 1),
        (world["ch"]["G1"], "other", 3),
    ]


async def test_project_messages_batch_goes_out_on_time(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="messages", batch_min=5))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="заявка")], now)
    ready = _grid(now, 5)
    clock.advance_ms(ready - now - 1)
    assert await _wait(client, world, sub, A) == []
    clock.advance_ms(1)
    assert [s["cat"] for s in await _wait(client, world, sub, A)] == ["other"]


async def test_project_messages_without_batch_unchanged(client, db, world, procs, clock):
    body = await _sub(client, world, A, mode="messages")
    assert body["sub"]["batch_min"] == 0 and _sub_rec(db, body["sub"]["id"]).keywords_json is None
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="заявка")], now)
    assert _rows(db)[0]["ready_ts"] is None
    assert [s["cat"] for s in await _wait(client, world, body["sub"]["id"], A)] == ["other"]


async def test_old_keyed_subscription_keeps_default_15(client, db, world, procs, clock):
    body = await _sub(client, world, A, mode="personal", **KEYS)
    assert body["sub"]["batch_min"] == keywords.BATCH_MIN_DEFAULT == 15
    assert "batch_min" not in json.loads(_sub_rec(db, body["sub"]["id"]).keywords_json)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    assert _rows(db)[0]["ready_ts"] == _grid(now, 15)


# --- хранение, наследование, смена фильтра -----------------------------------------------------------------


async def test_inherit_after_clear_and_filter_change_keep_interval(client, db, world, procs, clock):
    first = (await _sub(client, world, A, mode="messages", batch_min=9, role="менеджер"))["sub"]
    # /clear: новый разговор того же процесса, hub_subscribe() без аргументов — режим и интервал прежние
    procs[A2[0]] = A2[1]
    body = await _sub(client, world, A2)
    assert body["sub"]["id"] != first["id"]
    got = body["sub"]
    assert (got["mode"], got["msg_filter"], got["batch_min"]) == ("messages", "all", 9)
    # «лично мне» с ключами и своим интервалом -> «каждое сообщение»: ключи сняты, интервал остался
    await _sub(client, world, A2, mode="personal", batch_min=4, **KEYS)
    body = await _sub(client, world, A2, mode="messages")
    assert body["sub"]["keywords"] is None and body["sub"]["batch_min"] == 4
    # интервал без mode у подписки без ключей — меняется на месте
    body = await _sub(client, world, A2, batch_min=12)
    assert body["created"] is False and body["sub"]["batch_min"] == 12
    # в `direct` пачки нет: интервал снимается, `batch_min` с direct — 422
    body = await _sub(client, world, A2, mode="direct")
    assert body["sub"]["batch_min"] == 0 and _sub_rec(db, body["sub"]["id"]).keywords_json is None
    r = await _sub(client, world, A2, expect=422, batch_min=3)
    assert r["detail"] == subs.BATCH_MIN_DIRECT


async def test_want_form_accepts_batch_without_keys(db, world, procs):
    with db.reader() as conn:
        want = subs.want_form(conn, {"mode": "messages", "msg_filter": "all", "batch_min": 10})
        cards = subs.want_form(conn, {"mode": "cards", "batch_min": 10})
    assert want["batch_min"] == 10 and want["keywords"] == []
    assert cards["batch_min"] == 10
    rec = subs.SubRec(*([None] * len(subs.SubRec._fields)))._replace(
        mode="messages", msg_filter="all", direct=0, keywords_json=keywords.with_batch(None, 10)
    )
    assert subs.want_differs({**want, "batch_min": 10}, rec) is False
    assert subs.want_differs({**want, "batch_min": 5}, rec) is True


def test_keywords_storage_helpers():
    only = keywords.with_batch(None, 5)
    assert json.loads(only) == {"keys": [], "exclude": [], "batch_min": 5}
    assert keywords.parse_spec(only) is None and not keywords.has_keys(only)
    assert keywords.batch_ms(only) == 5 * MIN_MS and keywords.stored_batch(only) == 5
    assert keywords.with_batch(None, 0) is None and keywords.with_batch(None, None) is None
    keyed = keywords.dump_spec(["штраф"], [])
    assert keywords.batch_ms(keyed) == 15 * MIN_MS and keywords.stored_batch(keyed) is None
    assert keywords.batch_only(keywords.with_batch(keyed, 3)) == keywords.with_batch(None, 3)
    assert keywords.batch_only(keyed) is None
    assert keywords.batch_ms(None) == 0


# --- карточки -------------------------------------------------------------------------------------------


@pytest.fixture
def triage(db):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="agent:triage",
            kind="background",
            scopes=["read", "triage"],
            project_ids=None,
            expires_ts=None,
        )
    ).token


async def _card(client, token, channel_id, message_ids, label="reply"):
    r = await client.post(
        "/api/v1/agent/items",
        json={
            "channel_id": channel_id,
            "message_ids": message_ids,
            "label": label,
            "summary": "сводка",
            "action": None,
            "provider": "test",
            "model": "m",
        },
        headers=_h(token),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_cards_batch_waits_and_urgent_goes_at_once(client, db, world, procs, clock, triage):
    body = await _sub(client, world, A, mode="cards", batch_min=5)
    sub = body["sub"]["id"]
    assert body["sub"]["batch_min"] == 5
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="вопрос")], now)
    await _card(client, triage, world["ch"]["G1"], [_msg_id(db, world["ch"]["G1"], "1")])
    assert await _wait(client, world, sub, A) == []  # карточка ждёт срока пачки
    with db.reader() as conn:
        ready, held = subs.wait_state(conn, sub, now_ms=_now(clock))
    assert ready == 0 and 0 < held <= _grid(now, 5) - now
    with db.reader() as conn:  # то же — для сессий хаба (`held_signals.waiting`)
        rec = _sub_rec(db, sub)
        state = subs.cards_state(conn, rec, visible_projects=None, presence=[], now_ms=_now(clock))
    assert state[:2] == (0, 1) and state[2] == held
    # срочная карточка в другой беседе — сразу и забирает накопленную
    world["ch"]["G9"] = insert_channel(db, world["acc"], project_id=world["pid"], ext_id="G9", kind="group")
    _ingest(db, world, [_ev("G9", "1", now + 5, text="срочно")], now + 5)
    await _card(client, triage, world["ch"]["G9"], [_msg_id(db, world["ch"]["G9"], "1")], label="urgent")
    signals = await _wait(client, world, sub, A)
    assert [(s["channel_id"], s["label"]) for s in signals] == [
        (world["ch"]["G9"], "urgent"),
        (world["ch"]["G1"], "reply"),
    ]


async def test_cards_batch_goes_out_on_time_and_without_batch_unchanged(
    client, db, world, procs, clock, triage
):
    sub = (await _sub(client, world, A, mode="cards", batch_min=5))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="вопрос")], now)
    await _card(client, triage, world["ch"]["G1"], [_msg_id(db, world["ch"]["G1"], "1")])
    clock.advance_ms(_grid(now, 5) - now)
    assert [s["label"] for s in await _wait(client, world, sub, A)] == ["reply"]
    # пачка снята — следующая карточка сразу (после дебаунса выдачи)
    await _sub(client, world, A, batch_min=0)
    clock.advance_ms(subs.SUB_DEBOUNCE_MS)
    _ingest(db, world, [_ev("G2", "1", _now(clock), text="вопрос")], _now(clock))
    await _card(client, triage, world["ch"]["G2"], [_msg_id(db, world["ch"]["G2"], "1")])
    assert [s["channel_id"] for s in await _wait(client, world, sub, A)] == [world["ch"]["G2"]]
