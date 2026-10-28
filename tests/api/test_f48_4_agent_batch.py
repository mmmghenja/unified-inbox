"""Ф48-4 (docs/architecture.md): сообщения агентов пачкой — внешняя сессия на
`hub sub wait`: интервал подписки и умолчание проекта, срок в базе, срочное и «ОТ ВЛАДЕЛЬЦА» мимо пачки, переезд
интервала и срока с подпиской линии, рестарт ядра, миграция 0070, CLI.

Помощники и «мир» — из test_f35_peer_msgs.py."""

import json
import sys

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub import cli as hub_cli
from hub.core import db as db_module
from hub.core import peer_msgs, subs
from hub.core.app_public import create_public_app
from hub.core.db import Db

from . import test_f35_peer_msgs as base
from .conftest import BASE_URL, PORT
from .test_f35_peer_msgs import (
    ADMIN_SECRET,
    CODER,
    MARKETER,
    MIN,
    A,
    B,
    C,
    D,
    _h,
    _inbox,
    _now,
    _send,
    _sub,
    _sub_id,
    _to,
    _ui_list,
    _ui_send,
    _wait,
)

procs = base.procs
world = base.world
ui = base.ui
admin_client = base.admin_client


def _state(db, clock, sub_id) -> tuple[int, int]:
    """(готово к выдаче, мс до ближайшего срока) — то, что видит цикл `/subs/{id}/wait`."""
    with db.reader() as conn:
        return subs.wait_state(conn, sub_id, now_ms=_now(clock), peers=True, drafts=True, presses=True)


def _gate(db, sub_id) -> tuple[int | None, int | None]:
    with db.reader() as conn:
        return subs.agent_batch_row(conn, sub_id)


def _ids(signals) -> list[int]:
    assert all(s["kind"] == "peer" for s in signals), signals
    return [s["msg_id"] for s in signals]


async def _batched_pair(client, w, minutes=5):
    """Маркетолог (A) пишет программисту (B) с интервалом пачки; первое сообщение уже ушло сигналом —
    у подписки B стоит срок «не раньше»."""
    await _sub_id(client, w, A, "w1", role=MARKETER)
    body = await _sub(client, w, B, "w2", role=CODER, agent_batch_min=minutes)
    b = body["sub"]["id"]
    assert body["sub"]["agent_batch_min"] == minutes
    first = await _send(client, w, A, to=_to(w))
    assert first["recipient"] == {"live": True, "waiting": False}  # срока ещё нет — сигнал сразу
    assert _ids(await _wait(client, w, b, B)) == [first["msg"]["id"]]
    return b


# --- п.1: копит до срока и отдаёт одним сигналом -----------------------------------------------------------


async def test_accumulates_until_deadline_then_one_signal(client, ui, db, clock, world):
    w = world
    b = await _batched_pair(client, w)
    t0 = _now(clock)
    assert _gate(db, b) == (5, t0 + 5 * MIN)  # выдача поставила «не раньше»

    clock.advance_ms(MIN)
    second = await _send(client, w, A, to=_to(w), text="второе")
    deadline = _now(clock) + 5 * MIN  # первое ждущее сообщение пачки: «сейчас + N минут»
    assert second["recipient"] == {"live": True, "waiting": False, "batch_ts": deadline}
    assert (second["msg"]["batch_ts"], second["msg"]["urgent"]) == (deadline, False)
    clock.advance_ms(MIN)
    third = await _send(client, w, A, to=_to(w), text="третье")
    assert third["recipient"]["batch_ts"] == deadline  # копится к тому же сроку
    assert _gate(db, b) == (5, deadline)

    # до срока готового нет — ожидание спит ровно до срока
    assert _state(db, clock, b) == (0, 4 * MIN)
    row = next(m for m in await _ui_list(ui) if m["id"] == second["msg"]["id"])
    assert (row["batch_ts"], row["signals"]) == (deadline, 0)  # интерфейс: «в пачке до …»
    clock.advance_ms(4 * MIN - 1)
    assert _state(db, clock, b) == (0, 1)
    clock.advance_ms(1)
    assert _state(db, clock, b) == (2, 0)

    # срок пришёл — один сигнал с обоими сообщениями
    assert _ids(await _wait(client, w, b, B)) == [second["msg"]["id"], third["msg"]["id"]]
    assert _gate(db, b) == (5, _now(clock) + 5 * MIN)
    assert [m["text"] for m in await _inbox(client, w, B)] == ["привет", "второе", "третье"]
    row = next(m for m in await _ui_list(ui) if m["id"] == second["msg"]["id"])
    assert row["batch_ts"] is None  # доставлено — пометки пачки нет


async def test_interval_passed_since_last_signal_goes_at_once(client, db, clock, world):
    w = world
    b = await _batched_pair(client, w)
    clock.advance_ms(5 * MIN)  # с прошлого сигнала прошло не меньше интервала
    sent = await _send(client, w, A, to=_to(w), text="позже")
    assert sent["recipient"] == {"live": True, "waiting": False}
    assert sent["msg"]["batch_ts"] is None or sent["msg"]["batch_ts"] <= _now(clock)
    assert _state(db, clock, b) == (1, 0)
    assert _ids(await _wait(client, w, b, B)) == [sent["msg"]["id"]]


# --- п.2: срочное и «ОТ ВЛАДЕЛЬЦА» — сразу и забирают накопленное ---------------------------------------------


async def test_urgent_and_owner_pass_and_take_accumulated(client, ui, db, clock, world):
    w = world
    b = await _batched_pair(client, w)
    clock.advance_ms(MIN)
    slow = await _send(client, w, A, to=_to(w), text="не к спеху")
    assert "batch_ts" in slow["recipient"]
    assert _state(db, clock, b)[0] == 0

    urgent = await _send(client, w, A, to=_to(w), text="блокер", urgent=True)
    assert urgent["recipient"] == {"live": True, "waiting": False}  # срока отправителю не называют
    assert (urgent["msg"]["urgent"], urgent["msg"]["batch_ts"]) == (True, None)
    assert _state(db, clock, b)[0] == 1  # готово только срочное…
    # …а выдача забирает и накопленное; срочное — первым
    assert _ids(await _wait(client, w, b, B)) == [urgent["msg"]["id"], slow["msg"]["id"]]

    clock.advance_ms(MIN)
    slow2 = await _send(client, w, A, to=_to(w), text="ещё не к спеху")
    assert slow2["recipient"]["batch_ts"] == _now(clock) + 5 * MIN
    assert _state(db, clock, b)[0] == 0
    owner_side = await _ui_send(ui, to=_to(w))
    assert _state(db, clock, b)[0] == 1
    signals = await _wait(client, w, b, B)
    assert _ids(signals) == [owner_side["msg"]["id"], slow2["msg"]["id"]]
    assert signals[0].get("from_user") == 1


async def test_take_signals_not_woken_keeps_batch(client, db, clock, world):
    """Выдача, которой сессии больше ничего не ушло (карточки отсеяны), накопленное до срока не отдаёт."""
    w = world
    b = await _batched_pair(client, w)
    clock.advance_ms(MIN)
    slow = await _send(client, w, A, to=_to(w), text="не к спеху")
    now = _now(clock)

    def _take(conn, woken):
        return peer_msgs.take_signals(
            conn,
            subs.get_sub(conn, b),
            now_ms=now,
            limit=10,
            api_token_id=1,
            client_name=None,
            request_id="r",
            woken=woken,
        )

    assert db.write_sync(lambda conn: _take(conn, False)) == []
    assert _gate(db, b)[1] == slow["recipient"]["batch_ts"]  # срок цел
    assert _ids(db.write_sync(lambda conn: _take(conn, True))) == [slow["msg"]["id"]]


# --- 0 и «не задано» — как раньше ---------------------------------------------------------------------------


async def test_zero_and_unset_behave_as_before(client, db, clock, world):
    w = world
    await _sub_id(client, w, A, "w1", role=MARKETER)
    body = await _sub(client, w, B, "w2", role=CODER)
    b = body["sub"]["id"]
    assert "agent_batch_min" not in body["sub"]  # строка подписки прежняя
    for text in ("раз", "два"):
        sent = await _send(client, w, A, to=_to(w), text=text)
        assert sent["recipient"] == {"live": True, "waiting": False}
        assert sent["msg"]["batch_ts"] is None
        assert _ids(await _wait(client, w, b, B)) == [sent["msg"]["id"]]
        clock.advance_ms(MIN)
    assert _gate(db, b) == (None, None)  # срок без интервала не ставится

    # агент вне проектов: умолчания нет — 0
    free = await _sub(client, w, C, "free", mode="direct", role="вольный")
    assert "agent_batch_min" not in free["sub"]

    for bad in (61, -1, True, "5"):
        await _sub(client, w, D, "w2", role="тест", agent_batch_min=bad, expect=422)


# --- п.4: умолчание проекта и перекрытие подпиской ---------------------------------------------------------


async def test_project_default_and_subscription_override(client, admin_client, db, clock, world):
    w = world
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    url = f"/internal/v1/admin/projects/{w['p2']}"
    r = await admin_client.patch(url, json={"agent_batch_min": 61}, headers=headers)
    assert r.status_code == 422
    r = await admin_client.patch(url, json={"agent_batch_min": 3}, headers=headers)
    assert r.status_code == 200 and r.json()["agent_batch_min"] == 3, r.text
    r = await client.get("/api/v1/projects", headers=_h(w["tok"]))
    assert {p["id"]: p["agent_batch_min"] for p in r.json() if p["id"] > 0} == {w["p1"]: 0, w["p2"]: 3}

    await _sub_id(client, w, A, "w1", role=MARKETER)
    body = await _sub(client, w, B, "w2", role=CODER)  # своего интервала нет — умолчание проекта
    b = body["sub"]["id"]
    assert body["sub"]["agent_batch_min"] == 3 and _gate(db, b) == (None, None)
    r = await client.get("/api/v1/subs", params={"live": 1}, headers=_h(w["tok"]))
    assert {s["id"]: s.get("agent_batch_min") for s in r.json()["items"]}[b] == 3

    first = await _send(client, w, A, to=_to(w))
    assert _ids(await _wait(client, w, b, B)) == [first["msg"]["id"]]
    clock.advance_ms(MIN)
    slow = await _send(client, w, A, to=_to(w), text="в пачку")
    assert slow["recipient"]["batch_ts"] == _now(clock) + 3 * MIN
    assert _state(db, clock, b) == (0, 3 * MIN)

    # подписка со своим значением главнее проекта; смена интервала снимает срок — ждущее уходит сразу
    body = await _sub(client, w, B, "w2", role=CODER, agent_batch_min=0)
    assert body["sub"]["id"] == b and "agent_batch_min" not in body["sub"]
    assert _gate(db, b) == (0, None) and _state(db, clock, b) == (1, 0)
    assert _ids(await _wait(client, w, b, B)) == [slow["msg"]["id"]]
    clock.advance_ms(MIN)
    again = await _send(client, w, A, to=_to(w), text="сразу")
    assert again["recipient"] == {"live": True, "waiting": False}
    assert _ids(await _wait(client, w, b, B)) == [again["msg"]["id"]]

    # умолчание проекта 0 — ключ политики снят, сроки подписок без своего интервала обнулены
    c = (await _sub(client, w, C, "w2", role="тестер"))["sub"]["id"]
    first_c = await _send(client, w, A, to=_to(w, role="тестер"))
    assert _ids(await _wait(client, w, c, C)) == [first_c["msg"]["id"]]
    clock.advance_ms(MIN)
    held = await _send(client, w, A, to=_to(w, role="тестер"), text="ждёт")
    assert "batch_ts" in held["recipient"] and _state(db, clock, c)[0] == 0
    r = await admin_client.patch(url, json={"agent_batch_min": 0}, headers=headers)
    assert r.status_code == 200
    with db.reader() as conn:
        policy = conn.execute(
            "SELECT agent_policy_json FROM main.project WHERE id = ?", (w["p2"],)
        ).fetchone()
    assert "agent_batch_min" not in json.loads(policy[0] or "{}")
    assert _gate(db, c) == (None, None) and _state(db, clock, c) == (1, 0)


# --- п.1, п.6: интервал и срок переезжают с подпиской линии ------------------------------------------------


async def test_interval_and_deadline_move_on_resume_and_next_line_sub(client, db, clock, world, procs):
    w = world
    b = await _batched_pair(client, w)
    first_id = (await _inbox(client, w, B))[0]["id"]
    clock.advance_ms(MIN)
    slow = await _send(client, w, A, to=_to(w), text="в пачку")
    deadline = slow["recipient"]["batch_ts"]

    # resume: прежний процесс умер, новый процесс, тот же разговор — интервал и срок у новой подписки линии
    del procs[B[0]]
    resumed = (C[0], C[1], B[2])
    body = await _sub(client, w, resumed, "w2", role=CODER)
    r1 = body["sub"]["id"]
    assert r1 != b and body["sub"]["agent_batch_min"] == 5
    assert _gate(db, r1) == (5, deadline)
    assert _state(db, clock, r1) == (0, deadline - _now(clock))  # ждущее сообщение линии не готово до срока
    clock.advance_ms(deadline - _now(clock))
    assert _ids(await _wait(client, w, r1, resumed)) == [slow["msg"]["id"]]

    # сессии не стало — следующая подписка той же линии роли берёт интервал и стоящий срок
    assert _gate(db, r1)[1] == _now(clock) + 5 * MIN
    clock.advance_ms(MIN)
    slow2 = await _send(client, w, A, to=_to(w), text="снова в пачку")
    deadline2 = slow2["recipient"]["batch_ts"]
    del procs[C[0]]
    body = await _sub(client, w, D, "w2", role=CODER)
    r2 = body["sub"]["id"]
    assert body["sub"]["role_no"] == 1 and body["sub"]["agent_batch_min"] == 5
    assert _gate(db, r2) == (5, deadline2)
    assert _state(db, clock, r2)[0] == 0
    clock.advance_ms(deadline2 - _now(clock))
    # тем же сигналом — повторный сигнал о первом, не прочитанном за 10 минут (П4 Ф35: тоже ждал срока)
    assert _ids(await _wait(client, w, r2, D)) == [first_id, slow2["msg"]["id"]]

    # заданное в запросе значение главнее унаследованного
    del procs[D[0]]
    procs[C[0]] = C[1]
    body = await _sub(client, w, C, "w2", role=CODER, agent_batch_min=2)
    assert body["sub"]["agent_batch_min"] == 2 and _gate(db, body["sub"]["id"]) == (2, None)


# --- п.6: срок переживает рестарт ядра ----------------------------------------------------------------------


async def test_deadline_survives_core_restart(client, db, clock, world):
    w = world
    b = await _batched_pair(client, w)
    clock.advance_ms(MIN)
    slow = await _send(client, w, A, to=_to(w), text="в пачку")
    deadline = slow["recipient"]["batch_ts"]

    fresh = create_public_app(db, clock=clock, port=PORT)  # новое ядро на той же базе
    async with AsyncClient(transport=ASGITransport(app=fresh), base_url=BASE_URL) as c2:
        clock.advance_ms(MIN)
        assert _state(db, clock, b) == (0, deadline - _now(clock))
        assert await _wait(c2, w, b, B) == []  # до срока ожидание нового ядра пусто
        clock.advance_ms(deadline - _now(clock))
        assert _ids(await _wait(c2, w, b, B)) == [slow["msg"]["id"]]


# --- миграция 0070 ------------------------------------------------------------------------------------------


def test_migration_0070(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 69}
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
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 69
            assert "agent_batch_min" not in {
                r[1] for r in conn.execute("PRAGMA main.table_info(session_sub)")
            }
            assert "urgent" not in {r[1] for r in conn.execute("PRAGMA main.table_info(peer_msg)")}
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 70
                assert subs.agent_batch_row(conn, 1) == (None, None)
                assert subs.agent_batch_of(conn, subs.get_sub(conn, 1)) == 0
                info = {r[1]: r for r in conn.execute("PRAGMA main.table_info(peer_msg)")}
                assert info["urgent"][3] == 1 and str(info["urgent"][4]) == "0"  # NOT NULL DEFAULT 0
        finally:
            new.close()


# --- CLI ----------------------------------------------------------------------------------------------------


def test_cli_project_set_agent_batch_min(monkeypatch, capsys):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"project_id": 3, **json.loads(request.content)})

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1"
        ),
    )
    for value in ("10", "0"):
        monkeypatch.setattr(sys, "argv", ["hub", "project", "set", "3", "--agent-batch-min", value])
        hub_cli.main()
    assert [(r.method, r.url.path, json.loads(r.content)) for r in seen] == [
        ("PATCH", "/internal/v1/admin/projects/3", {"agent_batch_min": 10}),
        ("PATCH", "/internal/v1/admin/projects/3", {"agent_batch_min": 0}),
    ]
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out == [{"project_id": 3, "agent_batch_min": 10}, {"project_id": 3, "agent_batch_min": 0}]
    monkeypatch.setattr(sys, "argv", ["hub", "project", "set", "3", "--agent-batch-min", "61"])
    with pytest.raises(SystemExit) as exc:
        hub_cli.main()
    assert "0–60" in str(exc.value) and len(seen) == 2


def test_cli_project_list_names_batch_only_when_set(monkeypatch, capsys):
    rows = [
        {"id": 1, "name": "A", "muted": False, "workdir": None, "agent_batch_min": 0},
        {"id": 3, "name": "B", "muted": False, "workdir": None, "agent_batch_min": 10},
    ]
    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows)),
            base_url="http://hub-internal/internal/v1",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["hub", "project", "list"])
    hub_cli.main()
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "agent_batch_min" not in out[0]
    assert out[1]["agent_batch_min"] == 10
