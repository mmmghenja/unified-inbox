"""Ф38-1 (docs/architecture.md): фильтр подписки на проект по
аккаунтам, ядро.

- D1: миграция 0053.
- D2: `POST /subs` — `accounts` («только») и `accounts_except` («кроме»), проверки формы.
- D3, D4: сообщения — подписка вне фильтра не получает НИ сигнала, НИ строки отсева.
- D5: карточки — `signal_for_item` и добор.
- D6: подписка на беседу вне фильтра той же сессии.
- D7 + П6: наследование.
- D8 + П2–П5: смена на месте.
- D9: переезд ждущих сигналов.
- D10: перепроверка ключей.
- D11: `sub_json`, соседи, `account_id` карточки.
- D12: сквозной.

Обвязка — `test_f26_multi_subs.py`; в мире два аккаунта (A и B) с беседами в одном проекте. Где сказано
«фильтр слышит A» — проверены оба вида: `accounts=[A]` и `accounts_except=[B]` (параметр `kind`)."""

import json

import pytest

from hub.core import chan_subs, ingest, subs
from hub.core import db as db_module
from hub.core.db import Db

from .conftest import insert_account, insert_channel, insert_project
from .test_f26_multi_subs import (
    A,
    B,
    C,
    _audit,
    _card,
    _card_signals,
    _chan_sub,
    _ev,
    _h,
    _msg_id,
    _msg_signals,
    _now,
    _row,
    _sub,
    _token,
    _wait,
)

KEYS = {"keywords": ["штраф"], "batch_min": 0}  # сигнал «по ключу» — сразу, без пачки
KINDS = ("only", "except")
ROLE = "маркетолог"
A2 = (A[0], A[1], "sess-aaaaaaaa-after-clear")


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1], C[0]: C[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc_a = insert_account(db, source="slack")
    acc_b = insert_account(db, source="slack")
    ch = {
        "GA": insert_channel(db, acc_a, project_id=pid, ext_id="GA", kind="group"),
        "MA": insert_channel(db, acc_a, project_id=pid, ext_id="MA", kind="group", muted=1),
        "GB": insert_channel(db, acc_b, project_id=pid, ext_id="GB", kind="group"),
        "DB": insert_channel(db, acc_b, project_id=pid, ext_id="DB", kind="dm"),
    }
    return {
        "pid": pid,
        "workdir": str(workdir),
        "A": acc_a,
        "B": acc_b,
        "ch": ch,
        "tok": _token(db),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


def _hears(w, kind: str, acc: str) -> dict:
    """Аргументы подписки, слышащей только аккаунт `acc` («A» или «B»), — видом `kind`."""
    other = "B" if acc == "A" else "A"
    return {"accounts": [w[acc]]} if kind == "only" else {"accounts_except": [w[other]]}


def _spec(w, kind: str, acc: str) -> dict:
    other = "B" if acc == "A" else "A"
    return {"only": [w[acc]]} if kind == "only" else {"except": [w[other]]}


def _ing(db, w, acc: str, events, now_ms):
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w[acc], events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _statuses(db, table="session_chan_signal", sub_col="project_sub_id"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT {sub_col}, channel_id, status FROM main.{table} ORDER BY id"  # noqa: S608
        ).fetchall()


# --- D1: миграция 0053 -----------------------------------------------------------------------------------


def test_d1_migration_0053(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 52}
    )
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts) VALUES (1, 1, 1, 1, 's', '/', 0)"
            )
        )
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 52
            work_version = conn.execute("PRAGMA work.user_version").fetchone()[0]
            cols = {r[1] for r in conn.execute("PRAGMA main.table_info(session_sub)")}
            assert "accounts_json" not in cols
    finally:
        old.close()
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 53}
    )
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 53
                assert conn.execute("PRAGMA work.user_version").fetchone()[0] == work_version
                assert conn.execute("SELECT accounts_json FROM main.session_sub").fetchall() == [(None,)]
        finally:
            new.close()
    # SubRec читается только на мигрированной базе (с 0100 в нём `session_host`) — до конца.
    monkeypatch.setattr(db_module, "_migrations", original)
    full = Db(tmp_path)
    full.start()
    try:
        with full.reader() as conn:
            rec = subs.get_sub(conn, 1)
            assert rec.accounts_json is None and subs.listens_account(rec, 7)
    finally:
        full.close()


# --- D2: POST /subs ---------------------------------------------------------------------------------------


async def test_d2_accounts_stored_sorted_unique(client, db, world, procs):
    a, b = world["A"], world["B"]
    body = await _sub(client, world, A, accounts=[b, a, b])
    assert body["sub"]["accounts"] == {"only": [a, b]}
    assert "accounts_inherited" not in body
    assert json.loads(_row(db, body["sub"]["id"]).accounts_json) == {"only": [a, b]}
    assert _audit(db, "sub_create")[-1]["accounts"] == {"only": [a, b]}
    body = await _sub(client, world, B, accounts_except=[b, b])
    assert body["sub"]["accounts"] == {"except": [b]}
    assert json.loads(_row(db, body["sub"]["id"]).accounts_json) == {"except": [b]}


async def test_d2_empty_list_is_no_filter(client, db, world, procs):
    for n, arg in enumerate(("accounts", "accounts_except")):
        who = (A, B)[n]
        body = await _sub(client, world, who, **{arg: []})
        assert body["sub"]["accounts"] is None
        assert _row(db, body["sub"]["id"]).accounts_json is None
        assert "accounts" not in _audit(db, "sub_create")[-1]


async def test_d2_refusals(client, db, world, procs):
    a = world["A"]
    r = await _sub(client, world, A, expect=422, accounts=[a, 99, 77])
    assert r["detail"] == "accounts: нет аккаунтов 77, 99"
    r = await _sub(client, world, A, expect=422, accounts_except=[99])
    assert r["detail"] == "accounts_except: нет аккаунтов 99"
    for bad in ([a] * 21, [0], [True], ["1"], [-1]):
        await _sub(client, world, A, expect=422, accounts=bad)
        await _sub(client, world, A, expect=422, accounts_except=bad)
    r = await _sub(client, world, A, expect=422, accounts=[a], accounts_except=[world["B"]])
    assert "что-то одно" in r["detail"]
    r = await _sub(client, world, A, expect=422, mode="direct", accounts=[a])
    assert r["detail"] == subs.ACCOUNTS_NO_DIRECT
    r = await _sub(client, world, A, expect=422, mode="direct", accounts_except=[a])
    assert r["detail"] == subs.ACCOUNTS_NO_DIRECT
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.session_sub").fetchone()[0] == 0
    # пустой список с `direct` — фильтра нет, отказа нет
    assert (await _sub(client, world, A, mode="direct", accounts=[]))["sub"]["accounts"] is None


async def test_d2_chan_sub_takes_no_accounts(client, db, world, procs):
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": world["ch"]["GA"], "session": {}, "accounts": [world["A"]]},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 422


def test_listens_rule_and_broken_json():
    rec = subs.SubRec(1, 1, 1, 1, 1, "s", "/", None, 0, None, None, None, None)
    assert subs.listens_account(rec, 5)
    assert subs.listens_account(rec._replace(accounts_json='{"only": [1, 5]}'), 5)
    assert not subs.listens_account(rec._replace(accounts_json='{"only": [1, 5]}'), 8)
    assert not subs.listens_account(rec._replace(accounts_json='{"except": [8]}'), 8)
    assert subs.listens_account(rec._replace(accounts_json='{"except": [8]}'), 5)
    for broken in ("{", "[1]", '{"only": []}', '{"both": [1]}', '{"only": ["1"]}', '{"only": [true]}', "8"):
        broken_rec = rec._replace(accounts_json=broken)
        assert subs.accounts_of(broken_rec) is None
        assert subs.listens_account(broken_rec, 8), broken
    # аккаунт беседы неизвестен — слышит
    assert subs.listens_account(rec._replace(accounts_json='{"only": [1]}'), None)


# --- D3, D4: сообщения -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_d3_messages_only_to_listening_sub(client, db, world, procs, clock, kind):
    ch = world["ch"]
    s1 = (await _sub(client, world, A, mode="personal", **KEYS, **_hears(world, kind, "A")))["sub"]["id"]
    s2 = (await _sub(client, world, B, mode="personal", **KEYS, **_hears(world, kind, "B")))["sub"]["id"]
    now = _now(clock)
    # ключ в беседе аккаунта A — только первой; у второй НЕТ строки (ни `pending`, ни `skipped`)
    _ing(db, world, "A", [_ev("GA", "1.0", now, text="пришёл штраф")], now)
    assert _msg_signals(db) == [(None, s1, ch["GA"], "pending", None, "keyword", ["штраф"])]
    # без ключа в беседе A — отсев только у первой
    _ing(db, world, "A", [_ev("GA", "2.0", now + 1, text="ни о чём")], now)
    assert s2 not in {r[1] for r in _msg_signals(db)}
    # личка в беседе аккаунта B — только второй
    _ing(db, world, "B", [_ev("DB", "1.0", now + 2, text="привет", kind="dm")], now)
    assert [r for r in _msg_signals(db) if r[2] == ch["DB"]] == [
        (None, s2, ch["DB"], "pending", None, "dm", None)
    ]
    # заглушённая беседа аккаунта A с ключом — только первой
    _ing(db, world, "A", [_ev("MA", "1.0", now + 3, text="штраф")], now)
    assert [r for r in _msg_signals(db) if r[2] == ch["MA"]] == [
        (None, s1, ch["MA"], "pending", None, "keyword", ["штраф"])
    ]
    # бот в беседе аккаунта B — `skipped bot` только у второй
    _ing(db, world, "B", [_ev("GB", "1.0", now + 4, bot=True, text="штраф")], now)
    assert [r for r in _msg_signals(db) if r[2] == ch["GB"]] == [
        (None, s2, ch["GB"], "skipped", "bot", None, None)
    ]
    # у первой за всё время — только беседы аккаунта A, у второй — только аккаунта B
    assert {r[2] for r in _msg_signals(db) if r[1] == s1} == {ch["GA"], ch["MA"]}
    assert {r[2] for r in _msg_signals(db) if r[1] == s2} == {ch["DB"], ch["GB"]}


@pytest.mark.parametrize("kind", KINDS)
async def test_d4_sub_without_filter_hears_all(client, db, world, procs, clock, kind):
    ch = world["ch"]
    s0 = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    s1 = (await _sub(client, world, B, mode="messages", msg_filter="all", **_hears(world, kind, "B")))["sub"][
        "id"
    ]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now + 1)], now)
    assert [(r[1], r[2], r[3]) for r in _msg_signals(db)] == [
        (s0, ch["GA"], "pending"),
        (s0, ch["GB"], "pending"),
        (s1, ch["GB"], "pending"),
    ]


# --- D5: карточки -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_d5_card_signal_only_to_listening_cards_sub(client, db, world, procs, clock, kind):
    ch = world["ch"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    s1 = (await _sub(client, world, A, **_hears(world, kind, "A")))["sub"]["id"]
    await _sub(client, world, B, **_hears(world, kind, "B"))
    s3 = (await _sub(client, world, C))["sub"]["id"]  # без фильтра — слышит всё
    item = await _card(client, world, ch["GA"], [_msg_id(db, ch["GA"], "1.0")])
    assert _card_signals(db) == [(s1, item, "pending", None), (s3, item, "pending", None)]


@pytest.mark.parametrize("kind", KINDS)
async def test_d5_backfill_only_cards_of_listened_accounts(client, db, world, procs, clock, kind):
    ch = world["ch"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now)], now)
    await _card(client, world, ch["GA"], [_msg_id(db, ch["GA"], "1.0")])
    item_b = await _card(client, world, ch["GB"], [_msg_id(db, ch["GB"], "1.0")])
    body = await _sub(client, world, A, **_hears(world, kind, "B"))
    assert body["backfilled"] == 1
    assert _card_signals(db) == [(body["sub"]["id"], item_b, "pending", None)]


# --- D6: подписка на беседу главнее фильтра ---------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_d6_chan_sub_outside_filter_signals(client, db, world, procs, clock, kind):
    ch = world["ch"]
    s1 = (await _sub(client, world, A, mode="messages", msg_filter="all", **_hears(world, kind, "B")))["sub"][
        "id"
    ]
    chan = await _chan_sub(client, world, A, ch["GA"])
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    assert [(r[0], r[1], r[3]) for r in _msg_signals(db)] == [(chan, None, "pending")]
    assert s1 not in {r[1] for r in _msg_signals(db)}


# --- D7 + П6: наследование --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_d7_inherit_by_role_after_restart(client, db, world, procs, kind):
    first = (await _sub(client, world, A, mode="personal", role=ROLE, **KEYS, **_hears(world, kind, "A")))[
        "sub"
    ]
    del procs[A[0]]
    body = await _sub(client, world, C, role=ROLE)  # новый pid и session_id
    assert body["sub"]["accounts"] == _spec(world, kind, "A")
    assert body["accounts_inherited"] is True
    assert body["inherited_from"] == first["id"]
    assert _audit(db, "sub_create")[-1]["accounts"] == _spec(world, kind, "A")


async def test_d7_p6_inherit_after_clear_from_cards_source(client, db, world, procs):
    """П6: источник в `cards` — `inherited_from` пуст, но фильтр унаследован и это названо."""
    await _sub(client, world, A, accounts_except=[world["B"]])
    body = await _sub(client, world, A2)  # /clear: тот же процесс, новый разговор
    assert body["created"] is True and "inherited_from" not in body
    assert body["sub"]["accounts"] == {"except": [world["B"]]}
    assert body["accounts_inherited"] is True


async def test_d7_p6_explicit_mode_and_keywords_take_filter_of_own_line(client, db, world, procs):
    await _sub(client, world, A, mode="personal", role=ROLE, **KEYS, accounts=[world["A"]])
    del procs[A[0]]
    body = await _sub(client, world, C, mode="personal", role=ROLE, keywords=["бан"])
    assert "inherited_from" not in body
    assert body["sub"]["accounts"] == {"only": [world["A"]]}
    assert body["accounts_inherited"] is True
    # и явный `cards` — тоже из своей линии
    del procs[C[0]]
    body = await _sub(client, world, B, mode="cards", role=ROLE)
    assert body["sub"]["accounts"] == {"only": [world["A"]]} and body["accounts_inherited"] is True


async def test_d7_empty_list_stops_inheritance(client, db, world, procs):
    await _sub(client, world, A, mode="personal", role=ROLE, **KEYS, accounts=[world["A"]])
    del procs[A[0]]
    body = await _sub(client, world, C, role=ROLE, accounts=[])
    assert body["sub"]["accounts"] is None and "accounts_inherited" not in body
    assert body["sub"]["mode"] == "messages"  # режим унаследован как раньше
    # и следующая в линии уже без фильтра
    del procs[C[0]]
    body = await _sub(client, world, B, role=ROLE)
    assert body["sub"]["accounts"] is None and "accounts_inherited" not in body


async def test_d7_explicit_accounts_replace_inherited(client, db, world, procs):
    await _sub(client, world, A, role=ROLE, accounts=[world["A"]])
    del procs[A[0]]
    body = await _sub(client, world, C, role=ROLE, accounts_except=[world["A"]])
    assert body["sub"]["accounts"] == {"except": [world["A"]]} and "accounts_inherited" not in body


async def test_d7_foreign_live_filter_is_not_taken(client, db, world, procs):
    await _sub(client, world, B, accounts=[world["A"]])  # живая чужая
    body = await _sub(client, world, C)
    assert body["sub"]["accounts"] is None and "accounts_inherited" not in body
    # и чужое завершённое (сосед умер, своей линии нет) при явном режиме не берётся
    del procs[B[0]]
    body = await _sub(client, world, A, mode="cards")
    assert body["sub"]["accounts"] is None and "accounts_inherited" not in body


async def test_d7_direct_source_gives_no_filter_and_refuses_accounts(client, db, world, procs):
    first = (await _sub(client, world, A, role=ROLE, accounts=[world["A"]]))["sub"]["id"]
    assert (await _sub(client, world, A, mode="direct"))["sub"]["accounts"] is None  # п.8: снят молча
    assert _row(db, first).accounts_json is None
    del procs[A[0]]
    # П3: итоговый режим новой — унаследованный `direct`: фильтр в запросе — 422, подписка не создана
    r = await _sub(client, world, C, expect=422, role=ROLE, accounts=[world["A"]])
    assert r["detail"] == subs.ACCOUNTS_NO_DIRECT
    body = await _sub(client, world, C, role=ROLE)
    assert body["sub"]["mode"] == "direct" and body["sub"]["accounts"] is None
    assert "accounts_inherited" not in body


# --- D8 + П2–П5: на месте ---------------------------------------------------------------------------------


async def test_d8_change_in_place_and_audit(client, db, world, procs):
    a, b = world["A"], world["B"]
    sub_id = (await _sub(client, world, A))["sub"]["id"]
    body = await _sub(client, world, A, accounts=[a])
    assert body["created"] is False and body["sub"]["id"] == sub_id
    assert body["sub"]["accounts"] == {"only": [a]} and "accounts_inherited" not in body
    assert _audit(db, "sub_mode") == [{"sub_id": sub_id, "accounts": {"only": [a]}}]
    # тот же список (в другом порядке, с повтором) — без записи аудита
    await _sub(client, world, A, accounts=[a, a])
    assert len(_audit(db, "sub_mode")) == 1
    # без аргумента — фильтр остаётся
    assert (await _sub(client, world, A))["sub"]["accounts"] == {"only": [a]}
    await _sub(client, world, A, accounts_except=[b])
    assert _audit(db, "sub_mode")[-1] == {"sub_id": sub_id, "accounts": {"except": [b]}}
    await _sub(client, world, A, accounts=[])
    assert _row(db, sub_id).accounts_json is None
    assert _audit(db, "sub_mode")[-1] == {"sub_id": sub_id, "accounts": []}
    assert len(_audit(db, "sub_mode")) == 3


async def test_d8_p3_direct_by_final_mode(client, db, world, procs):
    a = world["A"]
    sub_id = (await _sub(client, world, A, mode="direct"))["sub"]["id"]
    r = await _sub(client, world, A, expect=422, accounts=[a])  # живая `direct` и остаётся `direct`
    assert r["detail"] == subs.ACCOUNTS_NO_DIRECT
    assert _row(db, sub_id).accounts_json is None
    # выход из `direct` тем же вызовом — законно
    body = await _sub(client, world, A, mode="cards", accounts=[a])
    assert (body["sub"]["mode"], body["sub"]["accounts"]) == ("cards", {"only": [a]})
    # явный `direct` с фильтром — 422; без `accounts` — фильтр снят молча
    r = await _sub(client, world, A, expect=422, mode="direct", accounts=[a])
    assert r["detail"] == subs.ACCOUNTS_NO_DIRECT and _row(db, sub_id).accounts_json is not None
    body = await _sub(client, world, A, mode="direct")
    assert body["sub"]["mode"] == "direct" and body["sub"]["accounts"] is None
    assert _row(db, sub_id).accounts_json is None


@pytest.mark.parametrize("kind", KINDS)
async def test_d8_p2_filter_written_before_mode_backfill(client, db, world, procs, clock, kind):
    """П2: живая `messages` -> `cards` с фильтром одним вызовом — добор только по карточкам фильтра."""
    ch = world["ch"]
    now = _now(clock)
    sub_id = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now)], now)
    item_a = await _card(client, world, ch["GA"], [_msg_id(db, ch["GA"], "1.0")])
    await _card(client, world, ch["GB"], [_msg_id(db, ch["GB"], "1.0")])
    assert _card_signals(db) == []
    await _sub(client, world, A, mode="cards", **_hears(world, kind, "A"))
    assert _card_signals(db) == [(sub_id, item_a, "pending", None)]


@pytest.mark.parametrize("kind", KINDS)
async def test_d8_p4_narrowing_ends_pending_message_signal(client, db, world, procs, clock, kind):
    ch = world["ch"]
    sub_id = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now)], now)
    await _sub(client, world, A, **_hears(world, kind, "A"))
    assert _statuses(db) == [(sub_id, ch["GA"], "pending"), (sub_id, ch["GB"], "ended")]
    assert [s["channel_id"] for s in await _wait(client, world, sub_id, A)] == [ch["GA"]]


@pytest.mark.parametrize("kind", KINDS)
async def test_d8_p4_narrowing_ends_pending_card_signal(client, db, world, procs, clock, kind):
    ch = world["ch"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now)], now)
    sub_id = (await _sub(client, world, A))["sub"]["id"]
    item_a = await _card(client, world, ch["GA"], [_msg_id(db, ch["GA"], "1.0")])
    item_b = await _card(client, world, ch["GB"], [_msg_id(db, ch["GB"], "1.0")])
    await _sub(client, world, A, **_hears(world, kind, "A"))
    assert [(r[0], r[1], r[2]) for r in _card_signals(db)] == [
        (sub_id, item_a, "pending"),
        (sub_id, item_b, "ended"),
    ]
    assert [s["item_id"] for s in await _wait(client, world, sub_id, A)] == [item_a]


async def test_d8_p5_widening_backfills_newly_heard_cards_only(client, db, world, procs, clock):
    ch = world["ch"]
    a, b = world["A"], world["B"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now)], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now)], now)
    sub_id = (await _sub(client, world, A, accounts=[a]))["sub"]["id"]
    item_a = await _card(client, world, ch["GA"], [_msg_id(db, ch["GA"], "1.0")])
    item_b = await _card(client, world, ch["GB"], [_msg_id(db, ch["GB"], "1.0")])
    assert [s["item_id"] for s in await _wait(client, world, sub_id, A)] == [item_a]
    await _sub(client, world, A, accounts=[a, b])
    # карточка вновь слышимого аккаунта пришла; уже выданная карточка прежнего — второй раз не сигналит
    assert [(r[1], r[2]) for r in _card_signals(db)] == [(item_a, "delivered"), (item_b, "pending")]
    # у `messages` добора нет
    await _sub(client, world, A, mode="messages", msg_filter="all", accounts=[a])
    await _sub(client, world, A, accounts=[])
    assert _msg_signals(db) == []


# --- D9: переезд ждущих сигналов ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_d9_carry_only_signals_inside_new_filter(client, db, world, procs, clock, kind):
    ch = world["ch"]
    keys = {"keywords": ["штраф"]}  # пачка 15 минут — сигналы ждут срока
    old = (await _sub(client, world, A, mode="personal", role=ROLE, **keys))["sub"]["id"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1", now, text="штраф")], now)
    _ing(db, world, "B", [_ev("GB", "1", now, text="штраф")], now)
    assert _statuses(db) == [(old, ch["GA"], "pending"), (old, ch["GB"], "pending")]
    clock.advance_ms(20_000)
    new = (await _sub(client, world, A2, role=ROLE, **_hears(world, kind, "A")))["sub"]["id"]  # /clear
    assert new != old and _row(db, old).end_reason == "conversation_changed"
    assert _statuses(db) == [(new, ch["GA"], "pending"), (old, ch["GB"], "ended")]


# --- D10: перепроверка ключей -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_d10_recheck_keywords_skips_deaf_sub(client, db, world, procs, clock, kind):
    ch = world["ch"]
    deaf = (await _sub(client, world, A, mode="personal", **KEYS, **_hears(world, kind, "B")))["sub"]["id"]
    hears = (await _sub(client, world, B, mode="personal", **KEYS, **_hears(world, kind, "A")))["sub"]["id"]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now, text="ни о чём")], now)
    message_id = _msg_id(db, ch["GA"], "1.0")
    assert [(r[1], r[3], r[4]) for r in _msg_signals(db)] == [(hears, "skipped", "no_keyword")]

    def _fn(conn):
        conn.execute("UPDATE main.message SET text = 'теперь штраф' WHERE id = ?", (message_id,))
        return chan_subs.recheck_keywords(conn, message_id, now_ms=now + 1000)

    assert db.write_sync(_fn) is True
    rows = _msg_signals(db)
    assert (None, hears, ch["GA"], "pending", None, "keyword", ["штраф"]) in rows
    assert deaf not in {r[1] for r in rows}


# --- D11: выдача -------------------------------------------------------------------------------------------


async def test_d11_sub_json_neighbours_and_card_account(client, db, world, procs, clock):
    ch = world["ch"]
    a, b = world["A"], world["B"]
    first = (await _sub(client, world, A, accounts=[a]))["sub"]
    body = await _sub(client, world, B, accounts_except=[a])
    assert body["sub"]["accounts"] == {"except": [a]}
    assert [(n["id"], n["accounts"]) for n in body["neighbours"]] == [(first["id"], {"only": [a]})]
    r = await client.get("/api/v1/subs", params={"live": 1}, headers=_h(world["tok"]))
    assert r.status_code == 200, r.text
    assert {row["id"]: row["accounts"] for row in r.json()["items"]} == {
        first["id"]: {"only": [a]},
        body["sub"]["id"]: {"except": [a]},
    }
    now = _now(clock)
    _ing(db, world, "B", [_ev("GB", "1.0", now)], now)
    _ing(db, world, "A", [_ev("GA", "1.0", now + 1)], now)
    item_b = await _card(client, world, ch["GB"], [_msg_id(db, ch["GB"], "1.0")])
    clock.advance_ms(1000)
    item_a = await _card(client, world, ch["GA"], [_msg_id(db, ch["GA"], "1.0")])
    r = await client.get("/api/v1/agent/items", params={"project": world["pid"]}, headers=_h(world["triage"]))
    assert r.status_code == 200, r.text
    cards = r.json()
    assert [c["id"] for c in cards] == [item_a, item_b]  # П8: сортировка по позициям столбцов цела
    assert {c["id"]: c["account_id"] for c in cards} == {item_a: a, item_b: b}
    assert all(c["last_message_thread"] is None for c in cards)


# --- D12: сквозной -----------------------------------------------------------------------------------------


async def test_d12_two_roles_each_gets_only_own(client, db, world, procs, clock):
    """Маркетолог — «всё, кроме аккаунта исследователя», исследователь — «только свой аккаунт»: у обоих один
    ключ, беседы в одном проекте."""
    ch = world["ch"]
    b = world["B"]
    m = (await _sub(client, world, A, mode="personal", role=ROLE, **KEYS, accounts_except=[b]))["sub"]["id"]
    r = (await _sub(client, world, B, mode="personal", role="исследователь", **KEYS, accounts=[b]))["sub"][
        "id"
    ]
    now = _now(clock)
    _ing(db, world, "A", [_ev("GA", "1.0", now, text="штраф у продавца")], now)
    _ing(db, world, "B", [_ev("GB", "1.0", now + 1, text="штраф у бухгалтера")], now)
    got_m = await _wait(client, world, m, A)
    got_r = await _wait(client, world, r, B)
    assert [(s["channel_id"], s["keys"]) for s in got_m] == [(ch["GA"], ["штраф"])]
    assert [(s["channel_id"], s["keys"]) for s in got_r] == [(ch["GB"], ["штраф"])]
    assert await _wait(client, world, m, A) == [] and await _wait(client, world, r, B) == []
    assert sorted(_statuses(db)) == sorted([(m, ch["GA"], "delivered"), (r, ch["GB"], "delivered")])
