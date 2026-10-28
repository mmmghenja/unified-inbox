"""Ф36-1 (docs/architecture.md), ядро.

- F1–F3: сигнал «по ключу» ждёт срока пачки (`ready_ts` по сетке интервала), личное — сразу и забирает
  накопленное; личное в беседе со ждущим `keyword` снимает срок.
- F4 + П4: `batch_min=0` — сразу; без ключей — 422; смена интервала обнуляет срок; повтор с теми же ключами
  срок не трогает; `batch_min` без `mode` и `keywords` меняет только интервал.
- F5: то же у подписки на беседу (`/chan-subs/wait`).
- F6 + П1: наследование несёт `batch_min`, ждущие сигналы (бесед — `restore_for_role`, проекта — перенос
  новой подписке линии) сохраняют `ready_ts`.
- F7 + П2, П3: удалённые к моменту выдачи не сигналят; пересчёт числа по мёртвым пометкам.
- F8: напоминание во время пачки — выход сразу, накопленное «по ключу» в том же ответе.
- F9: дежурная считает запасной путь от `ready_ts`.
- F10: короткие слова во фразах ключей.
- П5: готовые — первыми (21 ждущая строка «по ключу» плюс личное).

Стиль — как test_f31_tails.py: публичное приложение через ASGITransport, события через `ingest.apply_batch`,
`/proc` подменён словарём, часы — `FakeClock`."""

import datetime as dt
import json

import pytest

from hub.core import chan_subs, ingest, keywords, media, reminders, security, subs
from hub.core import db as db_module

from .conftest import insert_account, insert_channel, insert_message, insert_project

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
A2 = (4242, 777_000, "sess-aaaaaaaa-9999")  # тот же процесс, новый разговор (/clear)
B = (5151, 888_000, "sess-bbbbbbbb-2222")
ROLE = "маркетолог"
KEYS = {"keywords": ["штраф", "выплат"], "exclude": ["вакансия"]}
MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
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


def _session(w, who, *, full=True):
    pid, start, sid = who
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


async def _sub(client, w, who, *, expect=200, **extra):
    body: dict = {"session": _session(w, who), **extra}
    if extra.get("mode") == "personal":
        body["mode"], body["msg_filter"] = "messages", "personal"
    r = await client.post("/api/v1/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _chan_sub(client, w, who, ext, *, expect=200, **extra):
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch"][ext], "session": _session(w, who), **extra},
        headers=_h(w["tok"]),
    )
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, who):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


async def _chan_wait(client, w, who):
    r = await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


def _ev(chat, ext, ts, *, kind="group", text="x", mention=False):
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind},
        "ext_id": ext,
        "ts": ts,
        "sender": {"ext_id": "U2", "name": "Пётр"},
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": mention,
    }


def _delete(chat, *exts, kind="group"):
    return {"type": "delete", "ext_ids": list(exts), "channel": {"ext_id": chat, "kind": kind}}


def _ingest(db, w, events, now_ms):
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _now(clock) -> int:
    return int(clock() * 1000)


def _grid(now_ms: int, minutes: int) -> int:
    """Срок пачки по B3 — независимо от кода: ближайший узел сетки интервала не раньше чем через минуту."""
    step = minutes * MIN_MS
    node = (now_ms + MIN_MS) // step * step
    return node if node == now_ms + MIN_MS else node + step


def _msg_id(db, channel_id, ext):
    schema = db_module.db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext),
        ).fetchone()[0]


def _rows(db, channel_id=None) -> list[dict]:
    """Строки сигналов сообщений (по порядку вставки), при `channel_id` — только этой беседы."""
    names = (
        "id chan_sub_id project_sub_id channel_id status reason msg_count cat keys_json ready_ts created_ts "
        "last_message_id"
    ).split()
    with db.reader() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(names)} FROM main.session_chan_signal ORDER BY id"  # noqa: S608
        ).fetchall()
    out = [dict(zip(names, row, strict=True)) for row in rows]
    return [r for r in out if channel_id is None or r["channel_id"] == channel_id]


def _audit(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _sub_rec(db, sub_id):
    with db.reader() as conn:
        return subs.get_sub(conn, sub_id)


# --- F1–F3: срок пачки у подписки на проект --------------------------------------------------------------


def test_b3_grid():
    step = 15 * MIN_MS
    assert chan_subs.batch_ready_ts(10 * step, step) == 11 * step  # минута запаса переносит на следующий узел
    assert chan_subs.batch_ready_ts(10 * step - MIN_MS, step) == 10 * step  # ровно за минуту — этот узел
    assert chan_subs.batch_ready_ts(10 * step - MIN_MS + 1, step) == 11 * step
    assert chan_subs.batch_ready_ts(7, 1 * MIN_MS) == 2 * MIN_MS


async def test_f1_keyword_waits_for_batch_and_goes_out_once(client, db, world, procs, clock):
    body = await _sub(client, world, A, mode="personal", **KEYS)
    sub = body["sub"]["id"]
    assert body["sub"]["keywords"]["batch_min"] == keywords.BATCH_MIN_DEFAULT == 15  # A4: действующее
    assert json.loads(_sub_rec(db, sub).keywords_json) == {  # A1: умолчание в JSON не пишется
        "keys": ["штраф", "выплат"],
        "exclude": ["вакансия"],
    }
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="пришёл штраф")], now)
    clock.advance_ms(20_000)
    _ingest(db, world, [_ev("G2", "1", now + 20_000, text="задержка выплаты")], now + 20_000)
    ready = _grid(now, 15)
    rows = _rows(db)
    assert [(r["status"], r["cat"], r["ready_ts"]) for r in rows] == [("pending", "keyword", ready)] * 2
    assert ready % (15 * MIN_MS) == 0 and MIN_MS <= ready - now <= 16 * MIN_MS
    assert body["sub"]["pending"] == 0
    assert await _wait(client, world, sub, A) == []  # до срока ожидание не выходит
    assert [r["status"] for r in _rows(db)] == ["pending", "pending"]
    clock.advance_ms(ready - _now(clock) - 1)
    assert await _wait(client, world, sub, A) == []  # за миллисекунду до срока — тоже
    clock.advance_ms(1)
    signals = await _wait(client, world, sub, A)
    assert [(s["channel_id"], s["cat"], s["keys"]) for s in signals] == [
        (world["ch"]["G1"], "keyword", ["штраф"]),
        (world["ch"]["G2"], "keyword", ["выплат"]),
    ]
    assert [r["status"] for r in _rows(db)] == ["delivered", "delivered"]


async def test_f2_personal_elsewhere_goes_at_once_and_takes_batch(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    assert await _wait(client, world, sub, A) == []
    _ingest(db, world, [_ev("D1", "1", now + 1, kind="dm", text="привет")], now + 1)
    signals = await _wait(client, world, sub, A)
    # П5: готовое (личное) — первым, несозревшее «по ключу» — тем же выходом
    assert [(s["channel_id"], s["cat"]) for s in signals] == [
        (world["ch"]["D1"], "dm"),
        (world["ch"]["G1"], "keyword"),
    ]
    assert signals[1]["keys"] == ["штраф"]


async def test_f3_personal_in_same_channel_clears_ready_ts(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    assert _rows(db)[0]["ready_ts"] == _grid(now, 15)
    _ingest(db, world, [_ev("G1", "2", now + 1, text="а ты что скажешь?", mention=True)], now + 1)
    (row,) = _rows(db)
    assert (row["cat"], row["ready_ts"], row["msg_count"]) == ("mention", None, 2)
    # ключ после личного: категория остаётся личной, срок — снятым (строка уже готова)
    _ingest(db, world, [_ev("G1", "3", now + 2, text="ещё штраф")], now + 2)
    (row,) = _rows(db)
    assert (row["cat"], row["ready_ts"], row["msg_count"]) == ("mention", None, 3)
    signals = await _wait(client, world, sub, A)
    assert [(s["cat"], s["msg_count"], s["keys"]) for s in signals] == [("mention", 3, ["штраф"])]


async def test_p5_ready_first_when_more_than_limit(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    events = []
    for i in range(chan_subs.MAX_SIGNALS_PER_DELIVERY + 1):
        ext = f"N{i}"
        world["ch"][ext] = insert_channel(db, world["acc"], project_id=world["pid"], ext_id=ext, kind="group")
        events.append(_ev(ext, "1", now + i, text="штраф"))
    _ingest(db, world, events, now)
    _ingest(db, world, [_ev("D1", "1", now + 100, kind="dm", text="привет")], now + 100)
    signals = await _wait(client, world, sub, A)
    assert len(signals) == chan_subs.MAX_SIGNALS_PER_DELIVERY
    assert (signals[0]["channel_id"], signals[0]["cat"]) == (world["ch"]["D1"], "dm")
    assert {s["cat"] for s in signals[1:]} == {"keyword"}
    left = [r for r in _rows(db) if r["status"] == "pending"]
    assert len(left) == 2 and all(r["ready_ts"] is not None for r in left)


# --- F4 + П4: параметр `batch_min` ----------------------------------------------------------------------


async def test_f4_batch_zero_is_immediate(client, db, world, procs, clock):
    body = await _sub(client, world, A, mode="personal", batch_min=0, **KEYS)
    sub = body["sub"]["id"]
    assert body["sub"]["keywords"]["batch_min"] == 0
    assert json.loads(_sub_rec(db, sub).keywords_json)["batch_min"] == 0
    assert _audit(db, "sub_create")[-1]["batch_min"] == 0  # аудит: число рядом с числами ключей
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    assert _rows(db)[0]["ready_ts"] is None
    signals = await _wait(client, world, sub, A)
    assert [(s["cat"], s["keys"]) for s in signals] == [("keyword", ["штраф"])]


async def test_f4_batch_min_bounds(client, db, world, procs, clock):
    """Ф67-1: `batch_min` без ключей больше не отказ (его случаи — test_f67_sub_batch.py); пределы прежние,
    у `direct` пачки нет."""
    for bad in (-1, 121, "5", True):
        await _sub(client, world, A, expect=422, mode="personal", batch_min=bad, **KEYS)
    r = await _sub(client, world, A, expect=422, mode="direct", batch_min=10)
    assert r["detail"] == subs.BATCH_MIN_DIRECT


async def test_p4_same_keys_keep_ready_ts_and_interval_alone_changes(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", batch_min=30, **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    ready = _grid(now, 30)
    assert _rows(db)[0]["ready_ts"] == ready
    # П4(а): повтор с теми же ключами без `batch_min` — интервал прежний, срок не сброшен, аудита нет
    body = await _sub(client, world, A, mode="personal", **KEYS)
    assert body["created"] is False and body["sub"]["keywords"]["batch_min"] == 30
    assert _rows(db)[0]["ready_ts"] == ready and _audit(db, "sub_mode") == []
    # П4(б): `batch_min` без `mode` и `keywords` — меняется только интервал; B4: срок ждущих обнулён
    body = await _sub(client, world, A, batch_min=5)
    assert body["sub"]["keywords"] == {"keys": ["штраф", "выплат"], "exclude": ["вакансия"], "batch_min": 5}
    assert body["sub"]["mode"] == "messages" and body["sub"]["msg_filter"] == "personal"
    assert _rows(db)[0]["ready_ts"] is None
    assert _audit(db, "sub_mode")[-1]["batch_min"] == 5
    assert [s["cat"] for s in await _wait(client, world, sub, A)] == ["keyword"]
    # тот же интервал ещё раз — ничего не пишется
    await _sub(client, world, A, batch_min=5)
    assert len(_audit(db, "sub_mode")) == 1
    # новые сигналы — уже по новой сетке
    clock.advance_ms(10_000)
    now = _now(clock)
    _ingest(db, world, [_ev("G2", "1", now, text="выплаты")], now)
    assert _rows(db, world["ch"]["G2"])[0]["ready_ts"] == _grid(now, 5)
    # B4: смена ключей тоже обнуляет срок; интервал при этом остаётся
    body = await _sub(client, world, A, mode="personal", keywords=["штраф"])
    assert body["sub"]["keywords"] == {"keys": ["штраф"], "exclude": [], "batch_min": 5}
    assert _rows(db, world["ch"]["G2"])[0]["ready_ts"] is None


# --- F5: подписка на беседу ------------------------------------------------------------------------------


async def test_f5_chan_sub_batch(client, db, world, procs, clock):
    body = await _chan_sub(client, world, A, "G1", **KEYS)
    assert body["sub"]["keywords"]["batch_min"] == 15
    await _chan_sub(client, world, A, "D1")
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    ready = _grid(now, 15)
    (row,) = _rows(db)
    assert (row["cat"], row["ready_ts"]) == ("keyword", ready)
    assert await _chan_wait(client, world, A) == []
    clock.advance_ms(ready - now)
    signals = await _chan_wait(client, world, A)
    assert [(s["channel_id"], s["cat"], s["keys"]) for s in signals] == [
        (world["ch"]["G1"], "keyword", ["штраф"])
    ]
    # личное в другой беседе во время пачки — выход сразу, с накопленным «по ключу»
    clock.advance_ms(10_000)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "2", now, text="опять штраф")], now)
    assert await _chan_wait(client, world, A) == []
    _ingest(db, world, [_ev("D1", "1", now + 1, kind="dm", text="привет")], now + 1)
    signals = await _chan_wait(client, world, A)
    assert [(s["channel_id"], s["cat"]) for s in signals] == [
        (world["ch"]["D1"], "dm"),
        (world["ch"]["G1"], "keyword"),
    ]
    # личное в беседе, где ждёт `keyword`, — категория личная, срок снят, выход сразу
    clock.advance_ms(10_000)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "3", now, text="и снова штраф")], now)
    assert _rows(db, world["ch"]["G1"])[-1]["ready_ts"] == _grid(now, 15)
    _ingest(db, world, [_ev("G1", "4", now + 1, text="ты тут?", mention=True)], now + 1)
    row = _rows(db, world["ch"]["G1"])[-1]
    assert (row["cat"], row["ready_ts"], row["msg_count"]) == ("mention", None, 2)
    assert [(s["cat"], s["msg_count"]) for s in await _chan_wait(client, world, A)] == [("mention", 2)]


async def test_f5_chan_sub_batch_min_rules(client, db, world, procs, clock):
    body = await _chan_sub(client, world, A, "G1", batch_min=0, **KEYS)
    assert body["sub"]["keywords"]["batch_min"] == 0
    assert _audit(db, "chan_sub_create")[-1]["batch_min"] == 0
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    assert _rows(db)[0]["ready_ts"] is None
    assert [s["cat"] for s in await _chan_wait(client, world, A)] == ["keyword"]
    # П4(б): только интервал у своей живой; П4(а): повтор ключей интервал и срок не трогает
    body = await _chan_sub(client, world, A, "G1", batch_min=20)
    assert body["created"] is False
    assert body["sub"]["keywords"] == {"keys": ["штраф", "выплат"], "exclude": ["вакансия"], "batch_min": 20}
    assert _audit(db, "chan_sub_keys")[-1]["batch_min"] == 20
    clock.advance_ms(10_000)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "2", now, text="штраф")], now)
    ready = _grid(now, 20)
    assert _rows(db)[-1]["ready_ts"] == ready
    body = await _chan_sub(client, world, A, "G1", **KEYS)
    assert body["sub"]["keywords"]["batch_min"] == 20 and _rows(db)[-1]["ready_ts"] == ready
    assert len(_audit(db, "chan_sub_keys")) == 1
    # B4: смена ключей обнуляет срок
    await _chan_sub(client, world, A, "G1", keywords=["штраф"])
    assert _rows(db)[-1]["ready_ts"] is None
    # своя живая без ключей: только интервал — Ф67-1: меняется (раньше — отказ)
    await _chan_sub(client, world, A, "G2")
    body = await _chan_sub(client, world, A, "G2", batch_min=10)
    assert body["sub"]["keywords"] is None and body["sub"]["batch_min"] == 10


# --- F6 + П1: наследование -------------------------------------------------------------------------------


async def test_f6_role_inherits_batch_min_and_pending_keep_ready_ts(client, db, world, procs, clock):
    old = (await _sub(client, world, A, mode="personal", role=ROLE, batch_min=30, **KEYS))["sub"]["id"]
    old_chan = (await _chan_sub(client, world, A, "G2", batch_min=20, **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф"), _ev("G2", "1", now + 1, text="выплаты")], now)
    before = {r["channel_id"]: r for r in _rows(db)}
    assert before[world["ch"]["G1"]]["ready_ts"] == _grid(now, 30)
    assert before[world["ch"]["G2"]]["ready_ts"] == _grid(now, 20)
    assert before[world["ch"]["G2"]]["chan_sub_id"] == old_chan
    procs.pop(A[0])  # сессии A не стало
    clock.advance_ms(30_000)
    body = await _sub(client, world, B, role=ROLE)
    new = body["sub"]["id"]
    assert new != old and body["inherited_from"] == old
    assert body["sub"]["keywords"] == {"keys": ["штраф", "выплат"], "exclude": ["вакансия"], "batch_min": 30}
    (restored,) = body["restored_channels"]
    with db.reader() as conn:
        new_chan = chan_subs.get(conn, restored["chan_sub_id"])
    assert keywords.stored_batch(new_chan.keywords_json) == 20  # `restore_for_role` несёт интервал
    after = {r["channel_id"]: r for r in _rows(db)}
    g1, g2 = after[world["ch"]["G1"]], after[world["ch"]["G2"]]
    # П1: сигнал проекта переехал новой подписке; B7: сигнал беседы — новой подписке на беседу; сроки прежние
    assert (g1["project_sub_id"], g1["status"], g1["ready_ts"]) == (new, "pending", _grid(now, 30))
    assert (g2["chan_sub_id"], g2["status"], g2["ready_ts"]) == (new_chan.id, "pending", _grid(now, 20))
    assert g1["created_ts"] == before[world["ch"]["G1"]]["created_ts"]
    assert await _wait(client, world, new, B) == []
    clock.advance_ms(_grid(now, 30) - _now(clock))
    assert [(s["channel_id"], s["keys"]) for s in await _wait(client, world, new, B)] == [
        (world["ch"]["G1"], ["штраф"])
    ]
    assert [(s["channel_id"], s["keys"]) for s in await _chan_wait(client, world, B)] == [
        (world["ch"]["G2"], ["выплат"])
    ]


async def test_a3_new_keys_keep_interval_of_line(client, db, world, procs, clock):
    """A3: новая подписка линии с явными ключами без `batch_min` берёт интервал прежнего `keywords_json`."""
    await _sub(client, world, A, mode="personal", role=ROLE, batch_min=30, **KEYS)
    procs.pop(A[0])
    body = await _sub(client, world, B, mode="personal", role=ROLE, keywords=["бан"])
    assert body["created"] is True
    assert body["sub"]["keywords"] == {"keys": ["бан"], "exclude": [], "batch_min": 30}
    # явный интервал — он; линия без прежней подписки — умолчание, в JSON не пишется
    procs.pop(B[0])
    procs[A[0]] = A[1]
    body = await _sub(client, world, A2, mode="personal", role=ROLE, keywords=["бан"], batch_min=7)
    assert body["sub"]["keywords"]["batch_min"] == 7
    procs[B[0]] = B[1]
    body = await _sub(client, world, B, mode="personal", role="юрист", keywords=["иск"])
    assert body["sub"]["keywords"]["batch_min"] == 15
    assert "batch_min" not in json.loads(_sub_rec(db, body["sub"]["id"]).keywords_json)


async def test_p1_pending_survives_clear(client, db, world, procs, clock):
    old = (await _sub(client, world, A, mode="personal", role=ROLE, **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф"), _ev("G1", "2", now + 1, text="выплата")], now)
    ready = _grid(now, 15)
    clock.advance_ms(20_000)
    new = (await _sub(client, world, A2, role=ROLE))["sub"]["id"]  # /clear: прежняя — `conversation_changed`
    assert _sub_rec(db, old).end_reason == "conversation_changed"
    (row,) = _rows(db)
    assert (row["project_sub_id"], row["status"]) == (new, "pending")
    assert (row["ready_ts"], row["msg_count"]) == (ready, 2)
    assert row["created_ts"] == now
    with db.reader() as conn:
        hits = conn.execute(
            "SELECT sub_id, COUNT(*), MIN(ts), MAX(ts) FROM main.sub_keyword_hit WHERE sub_kind = 'project' "
            "GROUP BY sub_id ORDER BY sub_id"
        ).fetchall()
    assert hits == [(old, 2, now, now), (new, 2, now, now)]  # пометки скопированы с исходным `ts`
    assert await _wait(client, world, new, A2) == []
    clock.advance_ms(ready - _now(clock))
    signals = await _wait(client, world, new, A2)
    assert [(s["channel_id"], s["msg_count"], s["keys"]) for s in signals] == [
        (world["ch"]["G1"], 2, ["штраф", "выплат"])
    ]


async def test_p1_pending_survives_clear_with_explicit_keys(client, db, world, procs, clock):
    """П1 (приёмка): после /clear сессия зовёт подписку с явными режимом и ключами — источник наследования
    при этом не ищется, но ждущий сигнал линии всё равно переезжает новой подписке."""
    old = (await _sub(client, world, A, mode="personal", role=ROLE, **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    ready = _grid(now, 15)
    clock.advance_ms(20_000)
    new = (await _sub(client, world, A2, mode="personal", role=ROLE, **KEYS))["sub"]["id"]
    assert new != old
    (row,) = _rows(db)
    assert (row["project_sub_id"], row["status"], row["ready_ts"]) == (new, "pending", ready)
    clock.advance_ms(ready - _now(clock))
    assert [s["channel_id"] for s in await _wait(client, world, new, A2)] == [world["ch"]["G1"]]


async def test_p1_no_carry_from_old_source_or_into_cards(client, db, world, procs, clock):
    await _sub(client, world, A, mode="personal", role=ROLE, **KEYS)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    procs.pop(A[0])
    db.write_sync(lambda conn: subs.cleanup(conn, now_ms=now + 1))
    assert _rows(db)[0]["status"] == "ended"
    # источник завершён раньше 6 часов назад — сигнал остаётся закрытым
    clock.advance_ms(chan_subs.CARRY_MAX_AGE_MS + MIN_MS)
    late = (await _sub(client, world, B, role=ROLE))["sub"]["id"]
    assert _sub_rec(db, late).keywords_json is not None
    assert [(r["status"], r["project_sub_id"] == late) for r in _rows(db)] == [("ended", False)]


async def test_review_carried_row_follows_new_sub_without_batch(client, db, world, procs, clock):
    """Ревью: линия была «лично мне + ключи, пачка 120»; после /clear сессия подписалась `messages` (все
    сообщения, пачки нет) — перенесённая строка готова сразу и новое сообщение беседы её не придерживает."""
    await _sub(client, world, A, mode="personal", role=ROLE, batch_min=120, **KEYS)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    clock.advance_ms(20_000)
    new = (await _sub(client, world, A2, mode="messages", role=ROLE))["sub"]["id"]
    assert [(r["project_sub_id"], r["status"], r["ready_ts"]) for r in _rows(db)] == [(new, "pending", None)]
    t = _now(clock)
    _ingest(db, world, [_ev("G1", "2", t, text="обычное сообщение")], t)
    assert [(s["channel_id"], s["msg_count"]) for s in await _wait(client, world, new, A2)] == [
        (world["ch"]["G1"], 2)
    ]


async def test_review_carried_row_follows_batch_zero_and_shorter_grid(client, db, world, procs, clock):
    """Ревью: после /clear те же ключи с `batch_min=0` — «сразу» действует и на перенесённое; с пачкой короче
    прежней срок перенесённой строки — не позже узла новой сетки."""
    await _sub(client, world, A, mode="personal", role=ROLE, batch_min=120, **KEYS)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    clock.advance_ms(20_000)
    new = (await _sub(client, world, A2, mode="personal", role=ROLE, batch_min=0, **KEYS))["sub"]["id"]
    assert [s["msg_count"] for s in await _wait(client, world, new, A2)] == [1]

    t = _now(clock)
    await _sub(client, world, A2, role=ROLE, batch_min=120)
    _ingest(db, world, [_ev("G2", "1", t, text="штраф")], t)
    clock.advance_ms(20_000)
    third = (await _sub(client, world, A, mode="personal", role=ROLE, batch_min=5, **KEYS))["sub"]["id"]
    (row,) = [r for r in _rows(db) if r["status"] == "pending"]
    assert (row["project_sub_id"], row["ready_ts"]) == (third, _grid(_now(clock), 5))


async def test_review_no_carry_after_explicit_unsubscribe(client, db, world, procs, clock):
    """Ревью: явная отписка — насовсем (как у возврата бесед): её ждущие сигналы новой не переезжают."""
    old = (await _sub(client, world, A, mode="personal", role=ROLE, **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    r = await client.post(
        f"/api/v1/subs/{old}/end",
        json={"session": _session(world, A, full=False), "reason": "unsubscribed"},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    clock.advance_ms(20_000)
    new = (await _sub(client, world, A, mode="personal", role=ROLE, **KEYS))["sub"]["id"]
    assert new != old
    assert [(r["project_sub_id"], r["status"]) for r in _rows(db)] == [(old, "ended")]


# --- F7 + П2, П3: удалённые к моменту выдачи -------------------------------------------------------------


async def test_f7_single_deleted_message_does_not_signal(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="спам про штраф")], now)
    ready = _grid(now, 15)
    _ingest(db, world, [_delete("G1", "1")], now + 30_000)  # спам удалили до срока
    clock.advance_ms(ready - now)
    assert await _wait(client, world, sub, A) == []
    (row,) = _rows(db)
    assert (row["status"], row["reason"]) == ("skipped", "deleted")
    assert "deleted" not in chan_subs.FILTER_REASONS  # C3


async def test_f7_one_of_three_deleted(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    events = [_ev("G1", str(i), now + i, text=f"штраф {i}") for i in (1, 2, 3)]
    _ingest(db, world, events, now)
    last = _msg_id(db, world["ch"]["G1"], "3")
    _ingest(db, world, [_delete("G1", "3")], now + 30_000)
    clock.advance_ms(_grid(now, 15) - now)
    (signal,) = await _wait(client, world, sub, A)
    # в ответе — число живых и последнее живое; П3: в строке `last_message_id` не переписан
    assert (signal["msg_count"], signal["message_id"]) == (2, _msg_id(db, world["ch"]["G1"], "2"))
    (row,) = _rows(db)
    assert (row["status"], row["msg_count"], row["last_message_id"]) == ("delivered", 3, last)
    assert _audit(db, "sub_signal")[-1]["msg_count"] == 2


async def test_f7_all_three_deleted_and_chan_sub(client, db, world, procs, clock):
    await _chan_sub(client, world, A, "G1", **KEYS)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", str(i), now + i, text="штраф") for i in (1, 2, 3)], now)
    _ingest(db, world, [_delete("G1", "1", "2", "3")], now + 30_000)
    clock.advance_ms(_grid(now, 15) - now)
    assert await _chan_wait(client, world, A) == []
    (row,) = _rows(db)
    assert (row["status"], row["reason"], row["msg_count"]) == ("skipped", "deleted", 3)


async def test_f7_c2_without_hits(client, db, world, procs, clock):
    """Подписка без ключей — пометок нет: снимается только строка из одного удалённого сообщения."""
    sub = (await _sub(client, world, A, mode="messages"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="раз")], now)
    _ingest(db, world, [_ev("G2", "1", now + 1), _ev("G2", "2", now + 2)], now)
    _ingest(db, world, [_delete("G1", "1"), _delete("G2", "2")], now + 10)
    signals = await _wait(client, world, sub, A)
    assert [(s["channel_id"], s["msg_count"]) for s in signals] == [(world["ch"]["G2"], 2)]  # как есть
    by_channel = {r["channel_id"]: r for r in _rows(db)}
    g1 = by_channel[world["ch"]["G1"]]
    assert (g1["status"], g1["reason"]) == ("skipped", "deleted")


async def test_p2_hits_fewer_than_messages(client, db, world, procs, clock):
    """Ключи поставлены живой подписке на месте: у первого сообщения строки пометки нет."""
    sub = (await _sub(client, world, A, mode="personal"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="ты где?", mention=True)], now)
    await _sub(client, world, A, mode="personal", **KEYS)
    _ingest(db, world, [_ev("G1", "2", now + 1, text="штраф")], now + 1)
    (row,) = _rows(db)
    assert (row["msg_count"], row["cat"]) == (2, "mention")
    _ingest(db, world, [_delete("G1", "2")], now + 2)
    (signal,) = await _wait(client, world, sub, A)
    # мёртвых пометок (1) меньше числа сообщений (2) — строка выдаётся, число — за вычетом мёртвых
    assert (signal["msg_count"], signal["message_id"]) == (1, _msg_id(db, world["ch"]["G1"], "1"))


# --- F8: напоминание во время пачки ----------------------------------------------------------------------


async def test_f8_reminder_takes_batch_along(client, db, world, procs, clock):
    sub = (await _sub(client, world, A, mode="personal", role=ROLE, batch_min=120, **KEYS))["sub"]["id"]
    now = _now(clock)
    insert_message(db, world["ch"]["G3"], ext_id="m0", ts=now - 5 * MIN_MS, is_me=1)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    ready = _rows(db)[0]["ready_ts"]
    at = dt.datetime.fromtimestamp(clock() + 120, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = await client.post(
        "/api/v1/reminders",
        json={
            "channel_id": world["ch"]["G3"],
            "at": at,
            "note": "спросить про оплату",
            "session": _session(world, A, full=False),
        },
        headers=_h(world["tok"]),
    )
    assert r.status_code == 201, r.text
    assert await _wait(client, world, sub, A) == []
    clock.advance_ms(3 * MIN_MS)
    assert _now(clock) < ready  # срок пачки ещё не пришёл
    db.write_sync(lambda conn: reminders.fire_due(conn, _now(clock)))
    signals = await _wait(client, world, sub, A)
    assert [s.get("kind") for s in signals] == ["messages", "reminder"]
    assert (signals[0]["channel_id"], signals[0]["cat"]) == (world["ch"]["G1"], "keyword")


# --- F9: дежурная ----------------------------------------------------------------------------------------


def test_f10_short_words_inside_phrase():
    assert keywords.normalize(["А/Б-тест", "к оплате", "с НДС"], 10) == ["а б тест", "к оплате", "с ндс"]
    for bad in (["а"], ["а б"], ["штраф", "я"]):
        with pytest.raises(keywords.Invalid, match="слово короче 2 букв — можно только внутри фразы"):
            keywords.normalize(bad, 10)
    assert keywords.safe_keys(["а б тест", "к оплате"]) == ["а б тест", "к оплате"]
    assert keywords.safe_keys(["а  б"]) is None and keywords.safe_keys(["к оплате\n"]) is None

    def hit(keys, text):
        compiled = keywords.compile({"keys": keys, "exclude": []})
        found = keywords.check(compiled, text) or []
        assert found == keywords.matches(keys, keywords.words(text)), text  # раскладка = простая проверка
        return found

    assert hit(["а б тест"], "запускаем а/б-тесты завтра") == ["а б тест"]  # длинное слово — по началу
    assert hit(["а б тест"], "автор берёт тест") == []  # короткое — только целиком
    assert hit(["а б тест"], "а бы тест") == []
    assert hit(["к оплате"], "счёт к оплате до пятницы") == ["к оплате"]
    assert hit(["к оплате"], "как оплатить") == [] and hit(["к оплате"], "ок оплате") == []
    assert hit(["тест а б"], "тесты а б готовы") == ["тест а б"]
    assert hit(["тест а б"], "тест аб") == []
    # старые ключи совпадают как раньше
    assert hit(["штраф", "задерж выплат"], "Штрафы и задержали выплату") == ["штраф", "задерж выплат"]
    assert hit(["штраф"], "оштрафовали") == []


def test_a2_spec_helpers():
    raw = keywords.dump_spec(["штраф"], [], 30)
    assert keywords.parse_spec(raw) == {"keys": ["штраф"], "exclude": [], "batch_min": 30}
    assert keywords.batch_ms(raw) == 30 * MIN_MS
    assert keywords.batch_ms(keywords.parse_spec(raw)) == 30 * MIN_MS
    plain = keywords.dump_spec(["штраф"], [])
    assert "batch_min" not in keywords.parse_spec(plain)
    assert keywords.batch_ms(plain) == keywords.BATCH_MIN_DEFAULT * MIN_MS
    assert keywords.batch_ms(keywords.dump_spec(["штраф"], [], 0)) == 0
    assert keywords.batch_ms(None) == 0 and keywords.batch_ms("{}") == 0  # нет ключей — пачки нет
    for bad in (121, -1, "5", True, None):
        spoiled = json.dumps({"keys": ["штраф"], "exclude": [], "batch_min": bad})
        assert "batch_min" not in keywords.parse_spec(spoiled)
        assert keywords.batch_ms(spoiled) == keywords.BATCH_MIN_DEFAULT * MIN_MS
    assert keywords.public_spec(plain)["batch_min"] == 15 and keywords.public_spec(None) is None


async def test_f10_short_word_key_via_api(client, db, world, procs, clock):
    body = await _sub(client, world, A, mode="personal", keywords=["А/Б-тест"], batch_min=0)
    assert body["sub"]["keywords"]["keys"] == ["а б тест"]
    r = await _sub(client, world, A, expect=422, mode="personal", keywords=["а"])
    assert "ключ 1: слово короче 2 букв — можно только внутри фразы" in r["detail"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="автор берёт тест")], now)
    _ingest(db, world, [_ev("G2", "1", now + 1, text="результаты А/Б-теста")], now + 1)
    signals = await _wait(client, world, body["sub"]["id"], A)
    assert [(s["channel_id"], s["keys"]) for s in signals] == [(world["ch"]["G2"], ["а б тест"])]


def test_b1_migration_0050_main_only(db):
    with db.reader() as conn:
        main = [row[1] for row in conn.execute("PRAGMA main.table_info(session_chan_signal)")]
        work = conn.execute("SELECT 1 FROM work.sqlite_master WHERE name = 'session_chan_signal'").fetchone()
    assert "ready_ts" in main and work is None



async def test_review_alive_keeps_row_when_hits_outnumber_messages(client, db, world, procs, clock):
    """Ревью: A (ключ), B (без ключа — отфильтровано), C (ключ) — строка из двух сообщений. Правка B добавила
    ключ: пометка есть, в число не вошло (перепроверка внутри диапазона). A и B удалили, C жив — сигнал о C
    доходит, а не снимается как `deleted`."""
    sub = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    events = [
        _ev("G1", "1", now + 1, text="штраф раз"),
        _ev("G1", "2", now + 2, text="просто слова"),
        _ev("G1", "3", now + 3, text="штраф три"),
    ]
    _ingest(db, world, events, now)
    edit = {**_ev("G1", "2", now + 2, text="а тут выплата"), "type": "edit", "edited_ts": now + 5_000}
    _ingest(db, world, [edit], now + 5_000)
    _ingest(db, world, [_delete("G1", "1", "2")], now + 30_000)
    clock.advance_ms(_grid(now, 15) - now)
    signals = await _wait(client, world, sub, A)
    assert [(s["message_id"], s["msg_count"]) for s in signals] == [(_msg_id(db, world["ch"]["G1"], "3"), 1)]
