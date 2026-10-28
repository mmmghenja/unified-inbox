"""Ф32-1 (docs/architecture.md): подписка на проект с ролью
возвращает подписки на беседы прежней сессии этой роли.

- B1–B8: возврат с ключами, явная отписка — насовсем, без роли и с `restore_channels=False` — нет, пропуски
  (`busy`, `archived`, `not_visible`, `limit`), цепочка, `/clear`, ожидание бесед.
- П1: cleanup не прогонялся. П2: сигналы не теряются (ждавший `pending` и окно) и не двоятся. П3: источник
  роли отдельно от наследования режима. П4: чужое не переносится. П6: старше 7 дней. П7: причины конца
  источника, живой источник — `busy`, ветка OpenCode. П8: пометки «ключ сработал». П9: порядок записи.
  П10: сигнал невосстановленного кандидата закрыт.

Стиль — как test_f31_tails.py: публичное приложение через ASGITransport, события через `ingest.apply_batch`,
`/proc` подменён словарём, часы — `FakeClock`."""

import asyncio
import json

import pytest

from hub.core import chan_subs, ingest, media, security, subs
from hub.core import db as db_module

from .conftest import insert_account, insert_channel, insert_project

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
A2 = (4242, 777_000, "sess-aaaaaaaa-9999")  # тот же процесс, новый разговор (/clear)
B = (5151, 888_000, "sess-bbbbbbbb-2222")
C = (6161, 999_000, "sess-cccccccc-3333")
OC = (7171, 111_000)
OC_A = (*OC, "ses_AAAAAAAAAA1")
OC_B = (*OC, "ses_BBBBBBBBBB2")
ROLE = "маркетолог"
KEYS = {"keywords": ["штраф", "выплат"], "exclude": ["вакансия"]}
MIN_MS = 60_000
DAY_MS = 24 * 60 * MIN_MS
GROUPS = ("G1", "G2", "G3", "G4", "G5", "G6")


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1], C[0]: C[1], OC[0]: OC[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


def _token(db, *, name="mcp:all", project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name=name,
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=project_ids,
            expires_ts=None,
        )
    ).token


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    other = insert_project(db, name="Личное")
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="telegram")
    ch = {ext: insert_channel(db, acc, project_id=pid, ext_id=ext, kind="group") for ext in GROUPS}
    ch["X1"] = insert_channel(db, acc, project_id=other, ext_id="X1", kind="group")
    return {
        "pid": pid,
        "workdir": str(workdir),
        "acc": acc,
        "ch": ch,
        "tok": _token(db),
        "narrow": _token(db, name="mcp:iip", project_ids=[pid]),
    }


def _session(w, who, *, full=True, kind="interactive"):
    pid, start, sid = who
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": kind, "name": None, "name_source": None})
    return body


async def _sub(client, w, who, *, expect=200, token=None, kind="interactive", **extra):
    body: dict = {"session": _session(w, who, kind=kind), **extra}
    if extra.get("mode") == "personal":
        body["mode"], body["msg_filter"] = "messages", "personal"
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _chan_sub(client, w, who, ext, *, kind="interactive", **extra):
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch"][ext], "session": _session(w, who, kind=kind), **extra},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _chan_end(client, w, who, ext=None):
    body: dict = {"session": _session(w, who, full=False)}
    if ext is not None:
        body["channel_id"] = w["ch"][ext]
    r = await client.post("/api/v1/chan-subs/end", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text


async def _sub_end(client, w, who, sub_id, reason="unsubscribed"):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/end",
        json={"session": _session(w, who, full=False), "reason": reason},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text


async def _chan_wait(client, w, who, timeout_s=1):
    r = await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": timeout_s},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


def _ev(chat, ext, ts, *, text="x", edited_ts=None, type_="upsert"):
    event = {
        "type": type_,
        "channel": {"ext_id": chat, "kind": "group"},
        "ext_id": ext,
        "ts": ts,
        "sender": {"ext_id": "U2", "name": "Пётр"},
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    if edited_ts is not None:
        event["edited_ts"] = edited_ts
    return event


def _ingest(db, w, events, now_ms):
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _now(clock) -> int:
    return int(clock() * 1000)


def _cleanup(db, clock) -> None:
    """Фоновая задача ядра раз в минуту: подписки мёртвых сессий — `session_gone`."""
    now = _now(clock)
    db.write_sync(lambda conn: (subs.cleanup(conn, now_ms=now), chan_subs.cleanup(conn, now_ms=now)))


def _chan_rows(db) -> list[tuple]:
    """`(id, channel_id, session_id, end_reason, keywords_json)` всех подписок на беседы."""
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, channel_id, session_id, end_reason, keywords_json FROM main.session_chan_sub "
            "ORDER BY id"
        ).fetchall()


def _live(db, who) -> dict[int, int]:
    """Живые подписки разговора на беседы: `channel_id -> id`."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT channel_id, id FROM main.session_chan_sub WHERE ended_ts IS NULL AND session_id = ?",
            (who[2],),
        ).fetchall()
    return dict(rows)


def _signals(db) -> list[tuple]:
    """`(chan_sub_id, channel_id, status, msg_count)` сигналов подписок на беседы."""
    with db.reader() as conn:
        return conn.execute(
            "SELECT chan_sub_id, channel_id, status, msg_count FROM main.session_chan_signal "
            "WHERE chan_sub_id IS NOT NULL ORDER BY id"
        ).fetchall()


def _sub_reason(db, sub_id) -> str | None:
    with db.reader() as conn:
        return conn.execute("SELECT end_reason FROM main.session_sub WHERE id = ?", (sub_id,)).fetchone()[0]


def _restored(body) -> dict[int, dict]:
    return {row["channel_id"]: row for row in body.get("restored_channels", [])}


def _skipped(body) -> dict[int, str]:
    return {row["channel_id"]: row["reason"] for row in body.get("skipped_channels", [])}


async def _a_with_two(client, w, who=A, batch_min=None, **sub_extra):
    """Сессия роли «маркетолог» слушает G1 (с ключами) и G2 (каждое входящее). Ф36-1: `batch_min=0` — сигналы
    «по ключу» сразу, без пачки."""
    sub = await _sub(client, w, who, role=ROLE, **sub_extra)
    batch = {} if batch_min is None else {"batch_min": batch_min}
    c1 = await _chan_sub(client, w, who, "G1", **KEYS, **batch)
    c2 = await _chan_sub(client, w, who, "G2")
    return sub["sub"]["id"], c1, c2


# --- B1: возврат с теми же ключами ------------------------------------------------------------------------


async def test_b1_restored_with_keys_after_session_gone(client, db, world, procs, clock):
    _sub_a, c1, c2 = await _a_with_two(client, world)
    del procs[A[0]]
    clock.advance_ms(2 * MIN_MS)
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    live = _live(db, B)
    assert set(live) == {g1, g2}
    assert _restored(body) == {
        g1: {"chan_sub_id": live[g1], "channel_id": g1, "keys": 2, "exclude": 1},
        g2: {"chan_sub_id": live[g2], "channel_id": g2, "keys": 0, "exclude": 0},
    }
    assert body["skipped_channels"] == [] and "restore_blocked" not in body
    assert body["channels_waiting"] is False
    rows = {r[0]: r for r in _chan_rows(db)}
    assert rows[live[g1]][4] == rows[c1][4] and json.loads(rows[c1][4])["keys"]
    assert rows[live[g2]][4] is None
    assert (rows[c1][3], rows[c2][3]) == ("session_gone", "session_gone")
    with db.reader() as conn:
        audit = conn.execute(
            # Ф92-1 (B4): у первой подписки роли нет источника — строка `blocked`; здесь — только возвраты.
            "SELECT channel_id, detail_json FROM main.audit WHERE action = 'chan_sub_restore' "
            "AND result = 'ok' ORDER BY id"
        ).fetchall()
        created = conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'chan_sub_create'").fetchone()
    assert created[0] == 2  # возврат пишет свою строку аудита, не `chan_sub_create`
    assert {(cid, json.loads(d)["source"]) for cid, d in audit} == {(g1, c1), (g2, c2)}
    assert all("штраф" not in d for _cid, d in audit)  # тексты ключей в аудит не пишутся


# --- B2: явная отписка — насовсем --------------------------------------------------------------------------


async def test_b2_explicit_unsubscribe_one_channel(client, db, world, procs, clock):
    await _a_with_two(client, world)
    await _chan_end(client, world, A, "G2")
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert set(_restored(body)) == {world["ch"]["G1"]}
    assert set(_live(db, B)) == {world["ch"]["G1"]}
    assert body["skipped_channels"] == []  # снятая явно — не кандидат, а не «пропуск»


async def test_b2_unsubscribe_all_restores_nothing(client, db, world, procs, clock):
    sub_a, _c1, _c2 = await _a_with_two(client, world)
    # `hub_unsubscribe()` без аргумента: подписка на проект и все беседы — `unsubscribed`
    await _sub_end(client, world, A, sub_a)
    await _chan_end(client, world, A)
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE)
    assert "restored_channels" not in body and "restore_blocked" not in body
    assert _live(db, B) == {}


# --- B3: без роли и с выключателем -------------------------------------------------------------------------


async def test_b3_no_role_no_restore(client, db, world, procs, clock):
    await _sub(client, world, A, mode="messages")
    await _chan_sub(client, world, A, "G1")
    body = await _sub(client, world, A2)  # наследование по процессу (/clear), роли нет
    assert body["created"] is True and body["inherited_why"] == "process"
    assert "restored_channels" not in body
    assert _live(db, A2) == {}


async def test_b3_restore_channels_false(client, db, world, procs, clock):
    _sub_a, c1, c2 = await _a_with_two(client, world)
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE, restore_channels=False)
    assert body["created"] is True and "restored_channels" not in body
    assert _live(db, B) == {}
    rows = {r[0]: r for r in _chan_rows(db)}
    assert (rows[c1][3], rows[c2][3]) == (None, None)  # подписки источника не тронуты


# --- B4 + П4: пропуски и «не кандидат» ---------------------------------------------------------------------


async def test_b4_skips_archived_invisible_and_not_candidates(client, db, world, procs, clock):
    await _a_with_two(client, world)
    for ext in ("G3", "G4", "X1"):
        await _chan_sub(client, world, A, ext)
    g3, g4, x1 = world["ch"]["G3"], world["ch"]["G4"], world["ch"]["X1"]
    # G4 ушла из проекта: ленивое снятие `moved` — не кандидат
    now = _now(clock)
    with db.reader() as conn:
        live_a = chan_subs.session_subs(conn, 1, *A)
        g4_sub = next(r.id for r in live_a if r.channel_id == g4)
    db.write_sync(lambda conn: chan_subs.end(conn, g4_sub, "moved", now_ms=now, actor="core"))
    del procs[A[0]]
    _cleanup(db, clock)
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (g3,)))
    body = await _sub(client, world, B, role=ROLE, token=world["narrow"])
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    assert _skipped(body) == {g3: "archived", x1: "not_visible"}
    assert set(_live(db, B)) == {world["ch"]["G1"], world["ch"]["G2"]}


async def test_p4_channel_taken_by_other_role_is_not_ours(client, db, world, procs, clock):
    """Ф39 A2 п.4: кандидат — последняя строка беседы среди строк ИСТОЧНИКА; беседу после него слушала сессия
    другой роли — возврату не мешает (было: беседа «не наша» и не возвращалась)."""
    await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    g1 = world["ch"]["G1"]
    # беседу A после её смерти взяла сессия роли «программист» — и тоже умерла
    await _sub(client, world, C, role="программист")
    await _chan_sub(client, world, C, "G1")
    del procs[C[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert set(_restored(body)) == {g1, world["ch"]["G2"]}
    assert _skipped(body) == {} and g1 in _live(db, B)


async def test_b4_channel_listened_by_live_other_session(client, db, world, procs, clock):
    await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    c1 = await _chan_sub(client, world, C, "G1")  # живая чужая сессия тоже слушает беседу
    body = await _sub(client, world, B, role=ROLE)
    # Ф39 A2 п.4: чужая живая подписка возврату не мешает и сама не трогается — слушают обе
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    assert _skipped(body) == {}
    assert world["ch"]["G1"] in _live(db, C) and world["ch"]["G1"] in _live(db, B)
    assert {r[0]: r for r in _chan_rows(db)}[c1][3] is None


# --- B5 + П10: лимит ----------------------------------------------------------------------------------------


async def test_b5_limit_and_p10_signal_of_unrestored_closed(client, db, world, procs, clock, monkeypatch):
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 5)  # Ф35-0: поведение предела — на малом числе
    await _sub(client, world, A, role=ROLE)
    old = {ext: await _chan_sub(client, world, A, ext) for ext in GROUPS[:5]}
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="ждёт A")], now)  # `pending` самой старой подписки A
    assert _signals(db) == [(old["G1"], world["ch"]["G1"], "pending", 1)]
    await _chan_sub(client, world, B, "G6")
    del procs[A[0]]  # cleanup не прогонялся: живые подписки A снимает само восстановление
    body = await _sub(client, world, B, role=ROLE)
    ch = world["ch"]
    assert set(_restored(body)) == {ch[ext] for ext in ("G2", "G3", "G4", "G5")}  # свежие первыми
    assert _skipped(body) == {ch["G1"]: "limit"}
    assert len(_live(db, B)) == chan_subs.MAX_CHAN_SUBS
    # П10: подписка невосстановленного кандидата закрыта, её ждущий сигнал — `ended`, не висит
    rows = {r[0]: r for r in _chan_rows(db)}
    assert rows[old["G1"]][3] == "session_gone"
    assert _signals(db) == [(old["G1"], ch["G1"], "ended", 1)]


# --- B6: цепочка --------------------------------------------------------------------------------------------


async def test_b6_chain_through_two_resumes(client, db, world, procs, clock):
    await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    await _sub(client, world, B, role=ROLE)
    del procs[B[0]]
    clock.advance_ms(5 * MIN_MS)
    body = await _sub(client, world, C, role=ROLE)  # второй resume — без cleanup
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    assert _restored(body)[g1]["keys"] == 2 and set(_restored(body)) == {g1, g2}
    assert set(_live(db, C)) == {g1, g2} and _live(db, B) == {}
    # явная отписка в любой сессии цепочки обрывает её для беседы
    await _chan_end(client, world, C, "G2")
    del procs[C[0]]
    procs[B[0]] = B[1]
    body = await _sub(client, world, B, role=ROLE)
    assert set(_restored(body)) == {g1}


# --- B7: /clear того же процесса ----------------------------------------------------------------------------


async def test_b7_conversation_changed_same_process(client, db, world, procs, clock):
    sub_a, c1, c2 = await _a_with_two(client, world)
    body = await _sub(client, world, A2, role=ROLE)
    assert _sub_reason(db, sub_a) == "conversation_changed"
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    rows = {r[0]: r for r in _chan_rows(db)}
    assert (rows[c1][3], rows[c2][3]) == ("conversation_changed", "conversation_changed")
    assert set(_live(db, A2)) == {world["ch"]["G1"], world["ch"]["G2"]}


# --- B8: ожидание бесед -------------------------------------------------------------------------------------


async def test_b8_running_wait_picks_up_restored(client, db, world, procs, clock):
    _sub_a, c1, _c2 = await _a_with_two(client, world, batch_min=0)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф до смерти A")], now)
    await _chan_sub(client, world, B, "G6")
    waiting = asyncio.create_task(_chan_wait(client, world, B, timeout_s=30))
    await asyncio.sleep(0.2)  # ожидание B встало (своих сигналов нет)
    assert not waiting.done()
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE)
    assert body["channels_waiting"] is True
    signals = await asyncio.wait_for(waiting, 10)  # разбужено записью — сигнал A пришёл B
    g1 = world["ch"]["G1"]
    assert [(s["channel_id"], s["msg_count"], s["sub_id"]) for s in signals] == [(g1, 1, _live(db, B)[g1])]
    assert _signals(db) == [(_live(db, B)[g1], g1, "delivered", 1)]
    assert c1 not in [r[0] for r in _signals(db)]


# --- П1: cleanup не прогонялся ------------------------------------------------------------------------------


async def test_p1_dead_source_without_cleanup(client, db, world, procs, clock):
    sub_a, c1, c2 = await _a_with_two(client, world)
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE)
    assert _sub_reason(db, sub_a) == "replaced"  # источник снят этим же вызовом — сессии уже нет
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    rows = {r[0]: r for r in _chan_rows(db)}
    assert (rows[c1][3], rows[c2][3]) == ("session_gone", "session_gone")
    assert set(_live(db, B)) == {world["ch"]["G1"], world["ch"]["G2"]}


# --- П2 + П9: сигналы не теряются и не двоятся --------------------------------------------------------------


async def test_p2_p9_pending_and_window_one_signal(client, db, world, procs, clock):
    await _a_with_two(client, world)
    g2 = world["ch"]["G2"]
    t0 = _now(clock)
    _ingest(db, world, [_ev("G2", "1", t0, text="до смерти A")], t0)
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)  # `pending` источника -> `ended`
    assert [r[2] for r in _signals(db)] == ["ended"]
    clock.advance_ms(MIN_MS)
    t1 = _now(clock)
    _ingest(db, world, [_ev("G2", "2", t1, text="в окне"), _ev("G1", "1", t1, text="без ключа")], t1)
    assert len(_signals(db)) == 1  # живой подписки нет — окно без сигналов
    clock.advance_ms(MIN_MS)
    await _sub(client, world, B, role=ROLE)
    new = _live(db, B)
    # одна строка: ждавший сигнал источника — снова `pending` новой подписки, окно дописано в неё же (П9)
    assert [r for r in _signals(db) if r[2] == "pending"] == [(new[g2], g2, "pending", 2)]
    with db.reader() as conn:
        pending = conn.execute(
            "SELECT chan_sub_id, COUNT(*) FROM main.session_chan_signal WHERE status = 'pending' "
            "GROUP BY chan_sub_id"
        ).fetchall()
    assert pending == [(new[g2], 1)]  # ни одной второй `pending`
    signals = await _chan_wait(client, world, B)
    assert [(s["channel_id"], s["msg_count"]) for s in signals] == [(g2, 2)]
    assert await _chan_wait(client, world, B) == []  # дубля нет


async def test_p2_window_only_and_keys_rules(client, db, world, procs, clock):
    await _a_with_two(client, world)
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    clock.advance_ms(MIN_MS)
    t1 = _now(clock)
    _ingest(
        db,
        world,
        [
            _ev("G1", "1", t1, text="без ключа"),
            _ev("G1", "2", t1 + 1, text="вам штраф"),
            _ev("G1", "3", t1 + 2, text="штраф, вакансия"),
            _ev("G2", "1", t1 + 3, text="что угодно"),
        ],
        t1 + 3,
    )
    clock.advance_ms(MIN_MS)
    await _sub(client, world, B, role=ROLE)
    new = _live(db, B)
    signals = await _chan_wait(client, world, B)
    got = {s["channel_id"]: (s["msg_count"], s["cat"], s.get("keys")) for s in signals}
    assert got == {g1: (1, "keyword", ["штраф"]), g2: (1, "other", None)}
    assert {s["sub_id"] for s in signals} == {new[g1], new[g2]}


async def test_p2_window_skips_old_and_own(client, db, world, procs, clock):
    await _a_with_two(client, world)
    g2 = world["ch"]["G2"]
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    ended = _now(clock)
    late = ended + 2 * DAY_MS
    mine = _ev("G2", "2", ended + 5, text="мой ответ")
    mine["is_me"] = True
    mine["sender"] = {"ext_id": "U1", "is_me": True}
    _ingest(db, world, [_ev("G2", "1", ended + 1, text="старше суток"), mine], ended + 5)
    clock.advance_ms(2 * DAY_MS)
    _ingest(db, world, [_ev("G2", "3", late - MIN_MS, text="свежее")], late)
    await _sub(client, world, B, role=ROLE)
    assert _signals(db) == [(_live(db, B)[g2], g2, "pending", 1)]


# --- П3: источник роли отдельно от наследования режима ------------------------------------------------------


async def test_p3_role_in_cards_mode(client, db, world, procs, clock):
    await _a_with_two(client, world)  # режим по умолчанию — `cards`
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert "inherited_from" not in body  # режим не наследовался — источник роли найден отдельно
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}


async def test_p3_role_with_explicit_mode_and_keywords(client, db, world, procs, clock):
    await _a_with_two(client, world, mode="personal", keywords=["договор"])
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE, mode="personal", keywords=["претензия"])
    assert "inherited_from" not in body
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    assert body["sub"]["keywords"]["keys"] == ["претензия"]


# --- П6: источник старше 7 дней -----------------------------------------------------------------------------


async def test_p6_source_older_than_week(client, db, world, procs, clock):
    await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    clock.advance_ms(7 * DAY_MS + MIN_MS)
    body = await _sub(client, world, B, role=ROLE)
    assert body["restore_blocked"] == "old"
    assert body["restored_channels"] == [] and body["skipped_channels"] == []
    assert _live(db, B) == {}


async def test_p6_source_just_under_week(client, db, world, procs, clock):
    await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    clock.advance_ms(7 * DAY_MS - MIN_MS)
    body = await _sub(client, world, B, role=ROLE)
    assert len(body["restored_channels"]) == 2 and "restore_blocked" not in body


# --- П7: причины конца источника ----------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["unsubscribed", "ended_by_owner", "token_revoked", "subs_off"])
async def test_p7_source_reason_blocks(client, db, world, procs, clock, reason):
    sub_a, c1, c2 = await _a_with_two(client, world)
    now = _now(clock)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, reason, now_ms=now, actor="admin"))
    if reason == "subs_off":
        db.write_sync(lambda conn: subs.set_project_subs(conn, world["pid"], True, now_ms=now))
    del procs[A[0]]  # подписки на беседы ещё живые, сессии уже нет
    body = await _sub(client, world, B, role=ROLE)
    assert body["restore_blocked"] == reason
    assert body["restored_channels"] == [] and body["skipped_channels"] == []
    rows = {r[0]: r for r in _chan_rows(db)}
    assert (rows[c1][3], rows[c2][3]) == (None, None)  # живые подписки источника не тронуты
    assert _live(db, B) == {}


async def test_p7_replaced_source_blocks(client, db, world, procs, clock):
    """Источник, заменённый НЕ этим вызовом (роль у него забрала другая сессия), бесед не отдаёт."""
    sub_a, _c1, _c2 = await _a_with_two(client, world)
    now = _now(clock)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, "replaced", now_ms=now, actor="core"))
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert body["restore_blocked"] == "replaced" and _live(db, B) == {}


@pytest.mark.parametrize("reason", ["registry_unreadable", "session_gone"])
async def test_p7_live_source_is_busy(client, db, world, procs, clock, reason):
    sub_a, c1, c2 = await _a_with_two(client, world)
    now = _now(clock)
    _ingest(db, world, [_ev("G2", "1", now, text="ждёт A")], now)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, reason, now_ms=now, actor="core"))
    # процесс источника жив, разговор тот же: его подписки на беседы — чужая живая сессия
    body = await _sub(client, world, B, role=ROLE)
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    assert body["restored_channels"] == []
    assert _skipped(body) == {g1: f"busy:#{c1}", g2: f"busy:#{c2}"}
    assert _live(db, A) == {g1: c1, g2: c2} and _live(db, B) == {}
    assert _signals(db) == [(c2, g2, "pending", 1)]  # сигнал живого источника не тронут


async def test_p7_opencode_stale_source_restored(client, db, world, procs, clock):
    await _sub(client, world, OC_A, role=ROLE, kind="opencode")
    c1 = await _chan_sub(client, world, OC_A, "G1", kind="opencode", **KEYS)
    clock.advance_ms(subs.OC_LIVE_GRACE_MS + MIN_MS)  # ожидания нет дольше 10 мин — подписка брошена
    body = await _sub(client, world, OC_B, role=ROLE, kind="opencode")
    g1 = world["ch"]["G1"]
    assert set(_restored(body)) == {g1} and _restored(body)[g1]["keys"] == 2
    rows = {r[0]: r for r in _chan_rows(db)}
    assert rows[c1][3] == "session_gone" and set(_live(db, OC_B)) == {g1}


async def test_p7_opencode_live_source_same_process_is_busy(client, db, world, procs, clock):
    """У OpenCode на одном процессе много живых сессий: «тот же процесс, другой session_id» — не `/clear`."""
    sub_a = (await _sub(client, world, OC_A, role=ROLE, kind="opencode"))["sub"]["id"]
    c1 = await _chan_sub(client, world, OC_A, "G1", kind="opencode")
    now = _now(clock)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, "session_gone", now_ms=now, actor="core"))
    body = await _sub(client, world, OC_B, role=ROLE, kind="opencode")
    g1 = world["ch"]["G1"]
    assert body["restored_channels"] == [] and _skipped(body) == {g1: f"busy:#{c1}"}
    assert _live(db, OC_A) == {g1: c1}


async def test_p7_opencode_waiting_source_is_not_stale(client, db, world, procs, clock, app):
    sub_a = (await _sub(client, world, OC_A, role=ROLE, kind="opencode"))["sub"]["id"]
    c1 = await _chan_sub(client, world, OC_A, "G1", kind="opencode")
    clock.advance_ms(subs.OC_LIVE_GRACE_MS + MIN_MS)
    now = _now(clock)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, "session_gone", now_ms=now, actor="core"))
    # ожидание бесед источника идёт — подписка не брошена (как в `chan_subs.cleanup`)
    waiters = app.state.chan_sub_waiters = chan_subs.ChanWaiters()
    waiters.enter(chan_subs.session_key(1, *OC_A), OC_A[2])
    body = await _sub(client, world, OC_B, role=ROLE, kind="opencode")
    assert _skipped(body) == {world["ch"]["G1"]: f"busy:#{c1}"}


# --- П8: пометки «ключ сработал» переезжают -----------------------------------------------------------------


async def test_p8_edit_with_key_does_not_wake_new_sub(client, db, world, procs, clock):
    await _a_with_two(client, world, batch_min=0)
    g1 = world["ch"]["G1"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="вам штраф")], now)
    signals = await _chan_wait(client, world, A)  # A получила сигнал «по ключу»
    assert [(s["channel_id"], s["cat"]) for s in signals] == [(g1, "keyword")]
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    await _sub(client, world, B, role=ROLE)
    new = _live(db, B)[g1]
    schema = db_module.db_of(g1)
    with db.reader() as conn:
        message_id = conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = '1'",  # noqa: S608
            (g1,),
        ).fetchone()[0]
        hits = conn.execute(
            "SELECT sub_id FROM main.sub_keyword_hit WHERE sub_kind = 'channel' AND message_id = ?",
            (message_id,),
        ).fetchall()
    assert (new,) in hits
    before = _signals(db)
    later = _now(clock) + 10
    edit = _ev("G1", "1", now, text="вам штраф и выплаты", edited_ts=later, type_="edit")
    _ingest(db, world, [edit], later)
    assert _signals(db) == before  # правка сообщения, о котором A уже знала, B не будит
    assert await _chan_wait(client, world, B) == []


# --- П9: порядок записи -------------------------------------------------------------------------------------


async def test_p9_order_live_source_with_pending(client, db, world, procs, clock):
    """Живая подписка источника с `pending`: `end` -> вставка новой -> сигнал источника в `pending` новой. Ни
    второй живой подписки на беседу, ни второй `pending` — иначе `IntegrityError` откатил бы всю
    `POST /subs`."""
    sub_a, c1, c2 = await _a_with_two(client, world)
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф"), _ev("G2", "1", now, text="раз")], now)
    _ingest(db, world, [_ev("G2", "2", now + 1, text="два")], now + 1)
    assert _signals(db) == [(c1, g1, "pending", 1), (c2, g2, "pending", 2)]
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    body = await _sub(client, world, B, role=ROLE)
    new = _live(db, B)
    assert body["created"] is True and _sub_reason(db, sub_a) == "replaced"
    assert _signals(db) == [(new[g1], g1, "pending", 1), (new[g2], g2, "pending", 2)]
    with db.reader() as conn:
        live = conn.execute(
            "SELECT channel_id, COUNT(*) FROM main.session_chan_sub WHERE ended_ts IS NULL "
            "GROUP BY channel_id"
        ).fetchall()
        audit = conn.execute(
            "SELECT action FROM main.audit WHERE action IN ('chan_sub_end', 'chan_sub_restore') "
            "AND result != 'blocked' ORDER BY id"  # Ф92-1 (B4): `no_source` первой подписки роли — не возврат
        ).fetchall()
    assert sorted(live) == sorted([(g1, 1), (g2, 1)])
    # на каждую беседу: сначала конец строки источника, потом новая (свежие первыми)
    assert [a[0] for a in audit] == ["chan_sub_end", "chan_sub_restore"] * 2
    signals = await _chan_wait(client, world, B)
    assert {s["channel_id"]: s["msg_count"] for s in signals} == {g1: 1, g2: 2}


# --- ревью: восстановление по итоговой роли, повтор, границы окна -------------------------------------------


async def test_review_role_set_by_second_call(client, db, world, procs, clock):
    await _a_with_two(client, world)
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    first = await _sub(client, world, B)  # без роли — бесед нет
    assert "restored_channels" not in first and _live(db, B) == {}
    second = await _sub(client, world, B, role=ROLE)  # роль — вторым вызовом, на живую подписку
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    assert second["created"] is False and set(_restored(second)) == {g1, g2}
    # обычный повтор без изменений: возвращать нечего — ответ прежний, строк не прибавилось
    rows = _chan_rows(db)
    third = await _sub(client, world, B, role=ROLE)
    fourth = await _sub(client, world, B)
    assert "restored_channels" not in third and "restored_channels" not in fourth
    assert "restore_blocked" not in third and _chan_rows(db) == rows


async def test_review_take_role_from_dead_live_sub(client, db, world, procs, clock):
    """Роль у ещё не снятой подписки мёртвой сессии забирает `_take_role` (`replaced`) — источник годится."""
    sub_a, _c1, _c2 = await _a_with_two(client, world)
    await _sub(client, world, B)  # A жива — рядом, без роли
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE)
    assert _sub_reason(db, sub_a) == "replaced"
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}


async def test_review_inherited_role_on_resume(client, db, world, procs, clock):
    await _a_with_two(client, world)
    del procs[A[0]]
    resumed = (B[0], B[1], A[2])  # resume: новый процесс, тот же разговор
    body = await _sub(client, world, resumed)  # без аргументов — роль наследуется по разговору
    assert body["sub"]["role"] == ROLE
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}


async def test_review_limit_skipped_is_retried(client, db, world, procs, clock, monkeypatch):
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 5)  # Ф35-0: поведение предела — на малом числе
    await _sub(client, world, A, role=ROLE)
    for ext in GROUPS[:5]:
        await _chan_sub(client, world, A, ext)
    await _chan_sub(client, world, B, "G6")
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE)
    g1 = world["ch"]["G1"]
    assert _skipped(body) == {g1: "limit"}
    again = await _sub(client, world, B, role=ROLE)  # места нет — тот же пропуск
    assert again["restored_channels"] == [] and _skipped(again) == {g1: "limit"}
    await _chan_end(client, world, B, "G6")
    again = await _sub(client, world, B, role=ROLE)  # место освободилось — добрать
    assert set(_restored(again)) == {g1} and again["skipped_channels"] == []
    assert len(_live(db, B)) == chan_subs.MAX_CHAN_SUBS


async def test_review_window_slack_before_cleanup(client, db, world, procs, clock):
    """Отправлено за секунду до снятия подписки источника, в хаб пришло после — добор окна берёт (допуск по
    `ts`), а уже дававшее сигнал источнику не повторяет (`id > seen`)."""
    await _a_with_two(client, world)
    g2 = world["ch"]["G2"]
    now = _now(clock)
    _ingest(db, world, [_ev("G2", "1", now, text="получила A")], now)
    assert [(s["channel_id"], s["msg_count"]) for s in await _chan_wait(client, world, A)] == [(g2, 1)]
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    sent = _now(clock) - 1000
    _cleanup(db, clock)
    clock.advance_ms(1000)
    _ingest(db, world, [_ev("G2", "2", sent, text="на границе")], _now(clock))
    await _sub(client, world, B, role=ROLE)
    signals = await _chan_wait(client, world, B)
    assert [(s["channel_id"], s["msg_count"]) for s in signals] == [(g2, 1)]


async def _project_wait(client, w, sub_id, who):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


async def test_review_window_skips_signalled_by_source_project_sub(client, db, world, procs, clock):
    """Сообщение, о котором источник УЖЕ получил сигнал подпиской на проект, добор окна не повторяет."""
    sub = await _sub(client, world, A, role=ROLE, mode="messages")
    await _chan_sub(client, world, A, "G2")
    g2 = world["ch"]["G2"]
    now = _now(clock)
    live = _live(db, A)[g2]
    db.write_sync(lambda conn: chan_subs.end(conn, live, "registry_unreadable", now_ms=now, actor="core"))
    clock.advance_ms(MIN_MS)
    now = _now(clock)
    _ingest(db, world, [_ev("G2", "1", now, text="сигнал подписке A на проект")], now)
    got = await _project_wait(client, world, sub["sub"]["id"], A)
    assert [(s["channel_id"], s["msg_count"]) for s in got] == [(g2, 1)]
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE, mode="messages")
    assert g2 in _restored(body)
    assert await _chan_wait(client, world, B) == []


async def test_f36_window_takes_message_source_project_sub_never_got(client, db, world, procs, clock):
    """Ф36-1 П1: сигнал подписки на проект, который источник так и НЕ получил (ждал и закрылся вместе с ней),
    не пропадает — новая сессия роли получает сообщение ровно один раз, подпиской на беседу; перенесённая
    строка подписки на проект снимается как `chan_sub` (беседу слушает подписка на беседу той же сессии)."""
    sub = await _sub(client, world, A, role=ROLE, mode="messages")
    await _chan_sub(client, world, A, "G2")
    g2 = world["ch"]["G2"]
    now = _now(clock)
    live = _live(db, A)[g2]
    db.write_sync(lambda conn: chan_subs.end(conn, live, "registry_unreadable", now_ms=now, actor="core"))
    clock.advance_ms(MIN_MS)
    now = _now(clock)
    _ingest(db, world, [_ev("G2", "1", now, text="A не успела получить")], now)
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE, mode="messages")
    assert g2 in _restored(body)
    assert [(s["channel_id"], s["msg_count"]) for s in await _chan_wait(client, world, B)] == [(g2, 1)]
    assert await _project_wait(client, world, body["sub"]["id"], B) == []
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT project_sub_id, status, reason FROM main.session_chan_signal "
            "WHERE project_sub_id IS NOT NULL ORDER BY id"
        ).fetchall()
    assert rows == [(body["sub"]["id"], "skipped", "chan_sub")] and rows[0][0] != sub["sub"]["id"]


# --- Ф35-0 (план 11.40, П13): возврат двадцати бесед одной подпиской -------------------------------------


async def test_f35_restore_twenty_channels_with_window(client, db, world, procs, clock):
    """Предел 20: роль возвращает все двадцать бесед прежней сессии в одной транзакции `POST /subs`, и
    каждой — добор окна «источник снят -> новая подписка»."""
    acc, pid = world["acc"], world["pid"]
    exts = [f"N{i}" for i in range(20)]
    for ext in exts:
        world["ch"][ext] = insert_channel(db, acc, project_id=pid, ext_id=ext, kind="group")
    await _sub(client, world, A, role=ROLE)
    for ext in exts:
        await _chan_sub(client, world, A, ext)
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    clock.advance_ms(MIN_MS)
    t1 = _now(clock)
    _ingest(db, world, [_ev(ext, "1", t1, text="в окне") for ext in exts], t1)
    assert _signals(db) == []  # живой подписки нет — окно без сигналов
    clock.advance_ms(MIN_MS)
    body = await _sub(client, world, B, role=ROLE)
    ids = {world["ch"][ext] for ext in exts}
    assert set(_restored(body)) == ids and body["skipped_channels"] == []
    new = _live(db, B)
    assert set(new) == ids and len(new) == 20
    assert sorted(_signals(db)) == sorted((new[cid], cid, "pending", 1) for cid in ids)
    signals = await _chan_wait(client, world, B)
    assert sorted(s["channel_id"] for s in signals) == sorted(ids)  # все двадцать — одной выдачей
    assert await _chan_wait(client, world, B) == []


# --- 24.10: беседа-заготовка (первое сообщение новому человеку) — возвращается, не «archived» -----------


async def test_draft_channel_restored_not_skipped(client, db, world, procs, clock):
    temp = insert_channel(db, world["acc"], project_id=world["pid"], ext_id="new:draft-24", kind="dm")
    await _sub(client, world, A, role=ROLE)
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": temp, "session": _session(world, A)},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    del procs[A[0]]
    clock.advance_ms(2 * MIN_MS)
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert temp in _restored(body)
    assert body["skipped_channels"] == []
    assert temp in _live(db, B)
