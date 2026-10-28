"""Ф34-2 (docs/architecture.md 1–7 и поправки П1–П3, П6–П10): напоминание по беседе в
срок — ядро.

- B1 + П1: поставить -> в срок `fired`, сигнал `{"kind": "reminder"}` подписке роли (оба режима), строка на
  главной; сигнал отдаётся один раз.
- B2 + П8: `cancel_if_reply` — что считается ответом.
- B3 + П11: ядро лежало — срабатывает на первом тике, один раз; пустой тик ничего не пишет.
- B4: сессии роли нет — ждёт; `POST /subs` говорит о ждущих; «новым» отдаётся один раз.
- B5 + П3: перенос, закрытие, чужая роль и чужой токен, лимиты, срок вне окна.
- B6: архив -> `cancelled`; перенос беседы в другой проект — срабатывает в проекте постановки.
- B7: уведомление — с ролью через 20 минут и пока не закрыто, без роли сразу, один раз.
- П2: беседа в другом проекте; неподписанная сессия, дежурная, оркестратор; напоминания владельца.
- П6: ответ, принятый после срабатывания. П7: слияние заготовки и `channel_migrate`. П9: `/sync` и SSE.
- П10: название и заметка — по видимости беседы.

Стиль — как test_f26_multi_subs.py: публичное приложение через ASGITransport, `/proc` подменён словарём,
время — `FakeClock`; тик ядра — `reminders.fire_due` (цикл `_reminders_loop` — отдельным тестом)."""

import asyncio
import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import db as db_module
from hub.core import draft_channels, feed, queries, reminders, runner, security, subs
from hub.core.db import Db
from hub.core.ingest import apply_batch

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
C = (6161, 999_000, "sess-cccccccc-3333")
MIN = 60_000
NOTE = "обещал ответить после 14:00"


@pytest.fixture(autouse=True)
def _old_limits(monkeypatch):
    """Тесты пределов писались под прежние 5 / 50 / 200 (владелец 23.10 поднял пределы) — те же значения здесь."""
    monkeypatch.setattr(reminders, "MAX_PENDING_PER_CHANNEL", 5)
    monkeypatch.setattr(reminders, "MAX_PENDING_PER_PROJECT", 50)
    monkeypatch.setattr(reminders, "NOTE_MAX", 200)



@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1], C[0]: C[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    ).token


def _now(clock) -> int:
    return int(clock() * 1000)


def _at(clock, minutes: float) -> str:
    moment = dt.datetime.fromtimestamp(clock() + minutes * 60, tz=dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def world(db, tmp_path, clock, procs):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    p3 = insert_project(db, name="Umbrella", workdir=str(workdir))
    p4 = insert_project(db, name="Личное")
    db.write_sync(lambda conn: subs.set_project_subs(conn, p3, True, now_ms=0))
    acc = insert_account(db, source="mail")
    ch3 = insert_channel(db, acc, project_id=p3, ext_id="t3", title="Партнёр")
    ch4 = insert_channel(db, acc, project_id=p4, ext_id="t4", title="Ответ мимо хаба")
    for i, ch in enumerate((ch3, ch4)):
        insert_message(db, ch, ext_id=f"m0-{i}", ts=_now(clock) - 5 * MIN, is_me=1)
    return {
        "p3": p3,
        "p4": p4,
        "workdir": str(workdir),
        "acc": acc,
        "ch3": ch3,
        "ch4": ch4,
        "tok": _token(db),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


@pytest.fixture
async def ui(app, db, clock):
    """UI-сессия владельца — своим клиентом (bearer и cookie в одном запросе ядро не принимает)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        headers = csrf_headers(cookie)

        class _Ui:
            async def get(self, path, **kw):
                return await c.get(path, **kw)

            async def post(self, path, body):
                return await c.post(path, json=body, headers=headers)

            async def patch(self, path, body):
                return await c.patch(path, json=body, headers=headers)

        yield _Ui()


def _session(w, who, *, full=True):
    pid, start, sid = who
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


async def _sub(client, w, who, *, expect=200, token=None, **extra):
    body: dict = {"session": _session(w, who), **extra}
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, who, *, token=None):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(token or w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


async def _remind(client, w, who, channel_id, at, *, expect=201, token=None, note=NOTE, **extra):
    body: dict = {"channel_id": channel_id, "at": at, "note": note, **extra}
    if who is not None:
        body["session"] = _session(w, who, full=False)
    r = await client.post("/api/v1/reminders", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _patch(client, w, who, reminder_id, *, expect=200, token=None, **body):
    body["session"] = _session(w, who, full=False)
    r = await client.patch(f"/api/v1/reminders/{reminder_id}", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _list(client, w, who=None, *, token=None, **params):
    if who is not None:
        params.update(_session(w, who, full=False))
    r = await client.get("/api/v1/reminders", params=params, headers=_h(token or w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _tick(db, clock) -> dict:
    return db.write_sync(lambda conn: reminders.fire_due(conn, _now(clock)))


def _rec(db, reminder_id) -> reminders.Rec:
    with db.reader() as conn:
        rec = reminders.get(conn, reminder_id)
    assert rec is not None
    return rec


def _sub_rec(db, sub_id):
    with db.reader() as conn:
        return subs.get_sub(conn, sub_id)


async def _due(client, w) -> list[dict]:
    r = await client.get("/api/v1/agent/reminders/due", headers=_h(w["triage"]))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _home_reminders(ui_or_client, headers=None) -> list[dict]:
    r = await ui_or_client.get("/api/v1/home/react", **({"headers": headers} if headers else {}))
    assert r.status_code == 200, r.text
    return [item for item in r.json() if item["reminder"] is not None]


# --- B1 + П1: срабатывание, сигнал, главная ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["cards", "messages"])
async def test_b1_fires_in_time_signals_role_once_and_shows_on_home(client, ui, db, clock, world, mode):
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог", mode=mode))["sub"]["id"]
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30))
    assert (item["status"], item["project_id"], item["role"], item["by"]) == (
        "pending",
        w["p3"],
        "маркетолог",
        "session",
    )
    assert item["note"] == NOTE and item["channel"]["title"] == "Партнёр" and item["cancel_if_reply"] is True
    assert item["due_ts"] == _now(clock) + 30 * MIN

    clock.advance_ms(29 * MIN)
    with db.reader() as conn:
        assert reminders.has_work(conn, _now(clock)) is False
    assert await _home_reminders(ui) == []

    clock.advance_ms(2 * MIN)
    assert _tick(db, clock) == {"fired": 1, "answered": 0, "cancelled": 0}
    rec = _rec(db, item["id"])
    assert (rec.status, rec.fired_ts, rec.signaled_ts) == ("fired", _now(clock), None)

    # сигнал — только числа, заметки в нём нет
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "reminder", "reminder_id": item["id"], "channel_id": w["ch3"]}
    ]
    rec = _rec(db, item["id"])
    assert (rec.signal_sub_id, rec.signaled_ts, rec.delivered_ts) == (sub_id, _now(clock), None)
    with db.reader() as conn:
        audit = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action IN ('sub_signal', 'reminder_create')"
        ).fetchall()
    assert len(audit) == 2 and all(NOTE not in row[0] for row in audit)

    # П1: сигнал отдаётся один раз — ожидание не зацикливается
    clock.advance_ms(2 * MIN)
    assert await _wait(client, w, sub_id, A) == []
    with db.reader() as conn:
        assert subs.wait_state(conn, sub_id, now_ms=_now(clock))[0] == 0

    home = await _home_reminders(ui)
    assert len(home) == 1
    row = home[0]
    assert row["project"]["id"] == w["p3"] and row["channel"]["id"] == w["ch3"]
    assert (row["last"], row["card"]) == (None, None)
    assert (row["reminder"]["note"], row["reminder"]["role"], row["reminder"]["by"]) == (
        NOTE,
        "маркетолог",
        "session",
    )
    assert row["reminder"]["status"] == "fired" and row["reminder"]["waiting"] is True


# --- B2 + П8: что считается ответом ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "cancel_if_reply", "expected"),
    [
        ("foreign", True, "answered"),
        ("mine", True, "fired"),
        ("event", True, "fired"),
        ("bot", True, "fired"),
        ("deleted", True, "fired"),
        ("hidden", True, "fired"),
        ("unsent", True, "fired"),
        ("none", True, "fired"),
        ("foreign", False, "fired"),
    ],
)
async def test_b2_cancel_if_reply(client, db, clock, world, kind, cancel_if_reply, expected):
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30), cancel_if_reply=cancel_if_reply)
    clock.advance_ms(10 * MIN)
    if kind != "none":
        mid = insert_message(
            db,
            w["ch3"],
            ext_id=None if kind == "unsent" else "reply-1",
            ts=_now(clock),
            is_me=1 if kind == "mine" else 0,
            kind="event" if kind == "event" else "text",
            deleted=1 if kind == "deleted" else 0,
        )
        column = {"bot": "is_bot", "hidden": "hidden"}.get(kind)
        if column:
            db.write_sync(
                lambda conn: conn.execute(f"UPDATE main.message SET {column} = 1 WHERE id = ?", (mid,))  # noqa: S608
            )
    clock.advance_ms(21 * MIN)
    counts = _tick(db, clock)
    rec = _rec(db, item["id"])
    assert rec.status == expected, counts
    if expected == "answered":
        assert (rec.end_reason, rec.closed_ts, rec.fired_ts) == ("answered", _now(clock), None)
        # никого не будит
        with db.reader() as conn:
            assert subs.wait_state(conn, sub_id, now_ms=_now(clock))[0] == 0
            assert reminders.home_items(conn, _ui_principal()) == []
    else:
        with db.reader() as conn:
            assert subs.wait_state(conn, sub_id, now_ms=_now(clock))[0] == 1


def _ui_principal() -> security.Principal:
    return security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)


# --- B3 + П11: ядро лежало; пустой тик не пишет ------------------------------------------------------------


async def test_b3_missed_while_core_was_down_fires_once_on_first_tick(client, db, clock, world):
    w = world
    await _sub(client, w, A, role="маркетолог")
    item = await _remind(client, w, A, w["ch3"], _at(clock, 5))
    clock.advance_ms(3 * 60 * MIN)  # ядро «лежало» три часа

    commits: list[int] = []
    db.on_commit(lambda: commits.append(1))
    stop = asyncio.Event()
    ticks: list[float] = []

    async def _sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) >= 4:
            stop.set()

    await runner._reminders_loop(db, clock, sleep=_sleep, stop_event=stop)

    assert ticks == [60.0] * 4
    rec = _rec(db, item["id"])
    assert (rec.status, rec.fired_ts) == ("fired", _now(clock))
    assert len(commits) == 1  # первый тик записал, остальные только прочитали (П11)


async def test_p11_loop_without_due_reminders_never_writes(db, clock, world):
    commits: list[int] = []
    db.on_commit(lambda: commits.append(1))
    stop = asyncio.Event()

    async def _sleep(_seconds: float) -> None:
        stop.set()

    await runner._reminders_loop(db, clock, sleep=_sleep, stop_event=stop)
    assert commits == []


# --- B4: сессии роли нет — ждёт -----------------------------------------------------------------------------


async def test_b4_waits_for_role_session_and_is_new_only_once(client, db, clock, world):
    w = world
    first = await _sub(client, w, A, role="маркетолог")
    assert first["reminders_waiting"] == 0
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30))
    r = await client.post(
        f"/api/v1/subs/{first['sub']['id']}/end",
        json={"session": _session(w, A, full=False), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text

    clock.advance_ms(31 * MIN)
    assert _tick(db, clock)["fired"] == 1
    rec = _rec(db, item["id"])
    assert (rec.status, rec.signaled_ts, rec.delivered_ts) == ("fired", None, None)

    # сессия без роли о напоминании роли не узнаёт
    assert (await _sub(client, w, C))["reminders_waiting"] == 0
    # новая сессия роли — узнаёт при подписке
    second = await _sub(client, w, B, role="Маркетолог")
    assert second["reminders_waiting"] == 1

    items = await _list(client, w, B)
    assert [(i["id"], i["status"], i["waiting"], i["note"]) for i in items] == [
        (item["id"], "fired", True, NOTE)
    ]
    body = {"ids": [item["id"]], "session": _session(w, B, full=False)}
    r = await client.post("/api/v1/reminders/delivered", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200 and r.json() == {"marked": 1}
    assert _rec(db, item["id"]).delivered_ts == _now(clock)

    # повторно — без пометки
    assert [(i["id"], i["waiting"]) for i in await _list(client, w, B)] == [(item["id"], False)]
    r = await client.post("/api/v1/reminders/delivered", json=body, headers=_h(w["tok"]))
    assert r.json() == {"marked": 0}
    # чужая (безрольная) сессия отметить не может и в своём списке его не видит
    body_c = {"ids": [item["id"]], "session": _session(w, C, full=False)}
    assert await _list(client, w, C) == []
    r = await client.post("/api/v1/reminders/delivered", json=body_c, headers=_h(w["tok"]))
    assert r.json() == {"marked": 0}


# --- B5 + П3: перенос, закрытие, права, лимиты, окно --------------------------------------------------------


async def test_b5_move_fired_makes_it_pending_and_it_fires_again(client, ui, db, clock, world):
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30))
    clock.advance_ms(31 * MIN)
    _tick(db, clock)
    assert len(await _wait(client, w, sub_id, A)) == 1
    body = {"ids": [item["id"]], "session": _session(w, A, full=False)}
    await client.post("/api/v1/reminders/delivered", json=body, headers=_h(w["tok"]))
    db.write_sync(lambda conn: reminders.mark_notified(conn, [item["id"]], _ui_principal(), now_ms=1))

    # правка одной заметки сработавшее не трогает
    kept = await _patch(client, w, A, item["id"], note="  новая\nзаметка ")
    assert (kept["status"], kept["note"]) == ("fired", "новая заметка")

    moved = await _patch(client, w, A, item["id"], at=_at(clock, 60))
    assert (moved["status"], moved["due_ts"], moved["fired_ts"]) == ("pending", _now(clock) + 60 * MIN, None)
    rec = _rec(db, item["id"])
    assert (rec.signaled_ts, rec.signal_sub_id, rec.delivered_ts, rec.notified_ts) == (None, None, None, None)
    assert await _home_reminders(ui) == []

    clock.advance_ms(59 * MIN)
    assert _tick(db, clock)["fired"] == 0
    clock.advance_ms(2 * MIN)
    assert _tick(db, clock)["fired"] == 1
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "reminder", "reminder_id": item["id"], "channel_id": w["ch3"]}
    ]

    # закрытие: сработавшее — done (уходит с главной), ждущее — cancelled; повтор — как есть
    assert len(await _home_reminders(ui)) == 1
    done = await _patch(client, w, A, item["id"], close=True)
    assert (done["status"], done["closed_ts"], done["end_reason"]) == ("done", _now(clock), "session")
    assert await _home_reminders(ui) == []
    assert (await _patch(client, w, A, item["id"], close=True))["status"] == "done"
    await _patch(client, w, A, item["id"], expect=409, at=_at(clock, 10))
    await _patch(client, w, A, item["id"], expect=422, close=True, note="x")

    other = await _remind(client, w, A, w["ch3"], _at(clock, 10))
    cancelled = await _patch(client, w, A, other["id"], close=True)
    assert (cancelled["status"], cancelled["fired_ts"]) == ("cancelled", None)
    clock.advance_ms(11 * MIN)
    assert _tick(db, clock) == {"fired": 0, "answered": 0, "cancelled": 0}
    # закрытые — только с all=1
    assert await _list(client, w, A) == []
    assert {i["status"] for i in await _list(client, w, A, all=1)} == {"done", "cancelled"}


async def test_p3_other_role_on_same_token_403_other_scope_404(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role="маркетолог")
    await _sub(client, w, B, role="программист")
    await _sub(client, w, C)
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30))

    for who in (B, C):  # чужая роль и безрольная — на том же токене
        for body in ({"close": True}, {"at": _at(clock, 60)}, {"note": "x"}):
            await _patch(client, w, who, item["id"], expect=403, **body)
    assert _rec(db, item["id"]).status == "pending" and _rec(db, item["id"]).note == NOTE

    # токен без проекта напоминания — 404; несуществующее — 404; без сессии — 422
    narrow = _token(db, name="mcp:narrow", project_ids=[w["p4"]])
    await _patch(client, w, A, item["id"], expect=404, token=narrow, close=True)
    await _patch(client, w, A, 9999, expect=404, close=True)
    r = await client.patch(f"/api/v1/reminders/{item['id']}", json={"close": True}, headers=_h(w["tok"]))
    assert r.status_code == 422, r.text
    # токен без draft — 403
    reader = _token(db, name="mcp:reader", scopes=("read",))
    await _patch(client, w, A, item["id"], expect=403, token=reader, close=True)

    # безрольное напоминание правит любая подписанная безрольная сессия проекта; ролевая — нет
    roleless = await _remind(client, w, C, w["ch3"], _at(clock, 30))
    assert (roleless["role"], roleless["by"]) == (None, "session")
    await _patch(client, w, A, roleless["id"], expect=403, close=True)
    assert (await _patch(client, w, C, roleless["id"], close=True))["status"] == "cancelled"

    # Владелец в UI правит и закрывает любые
    r = await ui.patch(f"/api/v1/reminders/{item['id']}", {"at": _at(clock, 90), "note": "от владельца"})
    assert r.status_code == 200 and r.json()["note"] == "от владельца", r.text
    r = await ui.patch(f"/api/v1/reminders/{item['id']}", {"close": True})
    assert (r.json()["status"], r.json()["end_reason"]) == ("cancelled", "owner")


async def test_b5_limits_and_window(client, db, clock, world):
    w = world
    await _sub(client, w, A, role="маркетолог")
    for bad, text in (
        (_at(clock, 0.5), "at: время уже прошло или меньше чем через минуту"),
        (_at(clock, 91 * 24 * 60), "at: не позже чем через 90 дней"),
        ("завтра в десять", 'at: нужно время вида "2026-10-05 14:00"'),
        ("2026-13-45 10:00", "at: нет такого времени"),
    ):
        r = await _remind(client, w, A, w["ch3"], bad, expect=422)
        assert r["detail"].startswith(text), r
    await _remind(client, w, A, w["ch3"], _at(clock, 30), expect=422, note="я" * 201)
    await _remind(client, w, A, 999_999, _at(clock, 30), expect=404)

    # время без смещения — в поясе аккаунта беседы (без графика — Europe/Moscow, П12)
    local = dt.datetime.fromtimestamp(clock() + 3600, tz=dt.timezone(dt.timedelta(hours=3)))
    item = await _remind(client, w, A, w["ch3"], local.strftime("%Y-%m-%d %H:%M"))
    assert item["due_ts"] == int(local.replace(second=0, microsecond=0).timestamp() * 1000)

    for _ in range(4):
        await _remind(client, w, A, w["ch3"], _at(clock, 30))
    r = await _remind(client, w, A, w["ch3"], _at(clock, 30), expect=429)
    assert "5" in r["detail"]

    def _fill(conn):
        for i in range(45):
            conn.execute(
                "INSERT INTO main.reminder (channel_id, project_id, due_ts, cancel_if_reply, "
                "base_message_ts, status, created_ts, updated_ts, seq) "
                "VALUES (?, ?, ?, 1, 0, 'pending', 0, 0, 0)",
                (10_000 + i, w["p3"], _now(clock) + 60 * MIN),
            )

    db.write_sync(_fill)
    # лимит проекта — по проекту ПОДПИСКИ (беседа лежит в другом проекте)
    r = await _remind(client, w, A, w["ch4"], _at(clock, 30), expect=429)
    assert "50" in r["detail"]


# --- B6: архив и перенос беседы -----------------------------------------------------------------------------


async def test_b6_archived_channel_cancels_moved_channel_fires_in_project_of_reminder(
    client, db, clock, world
):
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    gone = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="t5", title="Ушла в архив")
    archived = await _remind(client, w, A, gone, _at(clock, 30))
    moved = await _remind(client, w, A, w["ch3"], _at(clock, 30))

    def _change(conn):
        conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (gone,))
        conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (w["p4"], w["ch3"]))

    db.write_sync(_change)
    await _remind(client, w, A, gone, _at(clock, 30), expect=409)  # на архивную не поставить

    clock.advance_ms(31 * MIN)
    assert _tick(db, clock) == {"fired": 1, "answered": 0, "cancelled": 1}
    rec = _rec(db, archived["id"])
    assert (rec.status, rec.end_reason) == ("cancelled", "archived")
    rec = _rec(db, moved["id"])
    assert (rec.status, rec.project_id, rec.channel_id) == ("fired", w["p3"], w["ch3"])
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "reminder", "reminder_id": moved["id"], "channel_id": w["ch3"]}
    ]


# --- B7: уведомление владельцу -----------------------------------------------------------------------------------


async def test_b7_notify_role_after_20_min_if_open_roleless_at_once_and_only_once(
    client, ui, db, clock, world
):
    w = world
    await _sub(client, w, A, role="маркетолог")
    await _sub(client, w, C)
    with_role = await _remind(client, w, A, w["ch3"], _at(clock, 30), note="с ролью")
    closed = await _remind(client, w, A, w["ch3"], _at(clock, 30), note="закроет сессия")
    roleless = await _remind(client, w, C, w["ch3"], _at(clock, 30), note="без роли")
    r = await ui.post("/api/v1/reminders", {"channel_id": w["ch4"], "at": _at(clock, 30), "note": "своё"})
    assert r.status_code == 201, r.text
    mine = r.json()
    assert (mine["by"], mine["role"], mine["project_id"]) == ("me", None, w["p4"])

    assert await _due(client, w) == []
    clock.advance_ms(31 * MIN)
    _tick(db, clock)

    due = await _due(client, w)
    assert sorted(i["id"] for i in due) == sorted([roleless["id"], mine["id"]])  # с ролью — ещё рано
    by_id = {i["id"]: i for i in due}
    assert by_id[mine["id"]]["note"] == "своё" and by_id[mine["id"]]["channel"]["title"] == "Ответ мимо хаба"

    r = await client.post(
        "/api/v1/agent/reminders/notified", json={"ids": [i["id"] for i in due]}, headers=_h(w["triage"])
    )
    assert r.status_code == 200 and r.json() == {"marked": 2}
    assert await _due(client, w) == []
    r = await client.post(
        "/api/v1/agent/reminders/notified", json={"ids": [roleless["id"]]}, headers=_h(w["triage"])
    )
    assert r.json() == {"marked": 0}  # один раз

    await _patch(client, w, A, closed["id"], close=True)
    clock.advance_ms(19 * MIN)
    assert await _due(client, w) == []
    clock.advance_ms(1 * MIN)
    assert [i["id"] for i in await _due(client, w)] == [with_role["id"]]  # закрытое не уведомляется

    # ручки разборщика — только скоуп triage
    r = await client.get("/api/v1/agent/reminders/due", headers=_h(w["tok"]))
    assert r.status_code == 403
    r = await client.post("/api/v1/agent/reminders/notified", json={"ids": [1]}, headers=_h(w["tok"]))
    assert r.status_code == 403


# --- П2: привязка к подписке сессии -------------------------------------------------------------------------


async def test_p2_channel_in_other_project_signals_role_of_subscription(client, ui, db, clock, world):
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    other = (await _sub(client, w, B, role="программист"))["sub"]["id"]
    item = await _remind(client, w, A, w["ch4"], _at(clock, 30))  # беседа в «Личном», роль — в проекте 3
    assert (item["project_id"], item["channel"]["project_id"], item["role"]) == (
        w["p3"],
        w["p4"],
        "маркетолог",
    )
    clock.advance_ms(31 * MIN)
    _tick(db, clock)
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "reminder", "reminder_id": item["id"], "channel_id": w["ch4"]}
    ]
    assert await _wait(client, w, other, B) == []  # другой роли — нет
    home = await _home_reminders(ui)
    assert [(h["project"]["id"], h["channel"]["id"]) for h in home] == [(w["p3"], w["ch4"])]


async def test_p2_only_subscribed_session_can_put(client, db, clock, world, procs):
    w = world
    at = _at(clock, 30)
    r = await _remind(client, w, A, w["ch3"], at, expect=409)  # не подписана
    assert "hub_subscribe" in r["detail"]
    r = await _remind(client, w, None, w["ch3"], at, expect=422)  # bearer без сессии
    await _sub(client, w, A)
    del procs[A[0]]
    await _remind(client, w, A, w["ch3"], at, expect=409)  # сессия умерла
    procs[A[0]] = A[1]

    # дежурная и оркестратор — отказ тем же правилом, что подписка
    wake = _token(db, name=f"mcp:wake-{w['p3']}", project_ids=[w["p3"]])
    orch = _token(db, name="mcp:orchestrator", scopes=("read", "draft", "organize", "orchestrate"))
    runs = _token(db, name="mcp:runs", scopes=("read", "draft", "runs"))
    background = _token(db, name="bg", kind="background", scopes=("read", "draft"))
    reader = _token(db, name="mcp:reader", scopes=("read",))
    for token in (wake, orch, runs, background, reader):
        await _remind(client, w, A, w["ch3"], at, expect=403, token=token)
    # чужой токен: подписка сессии — на другом токене
    await _remind(client, w, A, w["ch3"], at, expect=409, token=_token(db, name="mcp:other"))
    # беседа не видна токену
    narrow = _token(db, name="mcp:narrow", project_ids=[w["p3"]])
    await _sub(client, w, B, token=narrow)
    await _remind(client, w, B, w["ch4"], at, expect=404, token=narrow)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.reminder").fetchone()[0] == 0


async def test_p2_reminders_of_owner_never_signal_sessions_roleless_goes_to_same_token(
    client, ui, db, clock, world
):
    w = world
    role_sub = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    plain_sub = (await _sub(client, w, C))["sub"]["id"]
    other_tok = _token(db, name="mcp:other")
    foreign_sub = (await _sub(client, w, B, token=other_tok))["sub"]["id"]

    r = await ui.post("/api/v1/reminders", {"channel_id": w["ch3"], "at": _at(clock, 30)})
    assert r.status_code == 201, r.text
    mine = r.json()
    assert (mine["note"], mine["by"], mine["project_id"]) == (None, "me", w["p3"])
    # UI не шлёт session
    r = await ui.post(
        "/api/v1/reminders",
        {"channel_id": w["ch3"], "at": _at(clock, 30), "session": _session(w, A, full=False)},
    )
    assert r.status_code == 422, r.text
    # сессия напоминание владельца не правит
    await _patch(client, w, C, mine["id"], expect=403, close=True)

    clock.advance_ms(31 * MIN)
    _tick(db, clock)
    with db.reader() as conn:
        for sub_id in (role_sub, plain_sub, foreign_sub):
            assert reminders.signal_count(conn, subs.get_sub(conn, sub_id)) == 0
            assert reminders.waiting_count(conn, subs.get_sub(conn, sub_id)) == 0
    assert [h["reminder"]["by"] for h in await _home_reminders(ui)] == ["me"]

    # безрольное напоминание сессии — безрольным подпискам проекта ТОГО ЖЕ токена
    roleless = await _remind(client, w, C, w["ch3"], _at(clock, 30))
    clock.advance_ms(31 * MIN)
    _tick(db, clock)
    with db.reader() as conn:
        counts = [
            reminders.signal_count(conn, subs.get_sub(conn, s)) for s in (role_sub, plain_sub, foreign_sub)
        ]
    assert counts == [0, 1, 0]
    assert await _wait(client, w, plain_sub, C) == [
        {"kind": "reminder", "reminder_id": roleless["id"], "channel_id": w["ch3"]}
    ]

    # напоминание владельца в «Неразобранном» — общий блок на главной (проект -1)
    unsorted = insert_channel(db, w["acc"], ext_id="u1", title="Без проекта")
    r = await ui.post("/api/v1/reminders", {"channel_id": unsorted, "at": _at(clock, 5), "note": "n"})
    assert r.status_code == 201 and r.json()["project_id"] is None, r.text
    clock.advance_ms(6 * MIN)
    _tick(db, clock)
    home = await _home_reminders(ui)
    assert queries.UNSORTED_PROJECT_ID in {h["project"]["id"] for h in home}


# --- П6: ответ, принятый с опозданием -----------------------------------------------------------------------


@pytest.mark.parametrize("via", ["home", "list", "due", "deliver", "tick"])
async def test_p6_reply_accepted_after_firing_turns_fired_into_answered(client, ui, db, clock, world, via):
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    await _sub(client, w, C)
    item = await _remind(client, w, C if via == "due" else A, w["ch3"], _at(clock, 30))
    replied_ts = _now(clock) + 10 * MIN  # ответ написан до срока…
    clock.advance_ms(31 * MIN)
    assert _tick(db, clock)["fired"] == 1  # …но тик опередил догон коннектора
    fired_ts = _now(clock)
    clock.advance_ms(MIN)
    insert_message(db, w["ch3"], ext_id="late", ts=replied_ts)

    if via == "home":
        assert await _home_reminders(ui) == []
    elif via == "list":
        assert await _list(client, w, A) == []
    elif via == "due":
        assert await _due(client, w) == []
    elif via == "deliver":
        assert await _wait(client, w, sub_id, A) == []
    else:
        with db.reader() as conn:
            assert reminders.has_work(conn, _now(clock)) is True
        assert _tick(db, clock) == {"fired": 0, "answered": 1, "cancelled": 0}
    rec = _rec(db, item["id"])
    assert (rec.status, rec.end_reason, rec.fired_ts, rec.signaled_ts) == (
        "answered",
        "answered_late",
        fired_ts,
        None,
    )


async def test_p6_reply_written_after_firing_does_not_close(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role="маркетолог")
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30))
    clock.advance_ms(31 * MIN)
    _tick(db, clock)
    clock.advance_ms(5 * MIN)
    insert_message(db, w["ch3"], ext_id="after", ts=_now(clock))  # написан уже после срабатывания
    with db.reader() as conn:
        assert reminders.has_work(conn, _now(clock)) is False
    assert len(await _home_reminders(ui)) == 1
    assert _rec(db, item["id"]).status == "fired"


# --- П7: слияние заготовки и миграция беседы ----------------------------------------------------------------


async def test_p7_merge_draft_channel_moves_reminder(client, db, clock, world):
    """Напоминание на первое письмо (беседа-заготовка `new:…`): после слияния с настоящей беседой оно на
    ней и срабатывает, а не снимается вместе с ушедшей в архив заготовкой."""
    w = world
    sub_id = (await _sub(client, w, A, role="маркетолог"))["sub"]["id"]
    draft = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="new:abc", title="petr@example.com")
    target = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="thr-9", title="Пётр")
    item = await _remind(client, w, A, draft, _at(clock, 30))
    seq0 = _rec(db, item["id"]).seq

    db.write_sync(
        lambda conn: draft_channels._merge(
            conn, "main", draft, target, outbox_id=None, now_ms=_now(clock), via="report"
        )
    )
    rec = _rec(db, item["id"])
    assert (rec.channel_id, rec.status) == (target, "pending") and rec.seq > seq0
    with db.reader() as conn:
        assert conn.execute("SELECT archived FROM main.channel WHERE id = ?", (draft,)).fetchone() == (1,)

    clock.advance_ms(31 * MIN)
    assert _tick(db, clock) == {"fired": 1, "answered": 0, "cancelled": 0}
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "reminder", "reminder_id": item["id"], "channel_id": target}
    ]


async def test_p7_channel_migrate_moves_open_reminders_only(client, db, clock, world):
    w = world
    await _sub(client, w, A, role="маркетолог")
    old = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="-100", kind="group", title="Группа")
    item = await _remind(client, w, A, old, _at(clock, 30))
    closed = await _remind(client, w, A, old, _at(clock, 30))
    await _patch(client, w, A, closed["id"], close=True)

    event = {"type": "channel_migrate", "old_ext_id": "-100", "channel": {"ext_id": "-1001", "kind": "group"}}
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=_now(clock))
    )
    assert result.applied == 1
    with db.reader() as conn:
        new_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = '-1001'").fetchone()[0]
    assert (_rec(db, item["id"]).channel_id, _rec(db, item["id"]).status) == (new_id, "pending")
    assert _rec(db, closed["id"]).channel_id == old  # закрытое остаётся историей старой беседы

    clock.advance_ms(31 * MIN)
    assert _tick(db, clock)["fired"] == 1


# --- П9: /sync и SSE ----------------------------------------------------------------------------------------


async def test_p9_sync_carries_reminder_changes(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role="маркетолог")
    seq = (await ui.get("/api/v1/sync", params={"since": 0})).json()["max_seq"]

    async def _changes():
        nonlocal seq
        body = (await ui.get("/api/v1/sync", params={"since": seq})).json()
        seq = body["max_seq"]
        return body["reminders"]

    assert await _changes() == []
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30))
    got = await _changes()
    assert [(r["id"], r["status"], r["note"], r["channel"]["title"]) for r in got] == [
        (item["id"], "pending", NOTE, "Партнёр")
    ]
    assert seq == got[0]["seq"]

    clock.advance_ms(31 * MIN)
    _tick(db, clock)
    assert [(r["id"], r["status"]) for r in await _changes()] == [(item["id"], "fired")]
    await _patch(client, w, A, item["id"], at=_at(clock, 60))
    assert [(r["id"], r["status"]) for r in await _changes()] == [(item["id"], "pending")]
    await _patch(client, w, A, item["id"], close=True)
    changed = await _changes()
    assert [(r["id"], r["status"]) for r in changed] == [(item["id"], "cancelled")]
    assert await _changes() == []

    # кадр SSE — без заметки и названия
    frames = feed._frames(queries.Changes([], [], seq, False, [], [], reminders=changed))
    assert len(frames) == 1 and frames[0].startswith(f"id: {seq}\nevent: reminder\n")
    assert NOTE not in frames[0] and "Партнёр" not in frames[0]

    # bearer видит напоминания своего скоупа; чужого проекта — нет
    r = await client.get("/api/v1/sync", params={"since": 0}, headers=_h(w["tok"]))
    assert [x["id"] for x in r.json()["reminders"]] == [item["id"]]
    narrow = _token(db, name="mcp:narrow", project_ids=[w["p4"]])
    r = await client.get("/api/v1/sync", params={"since": 0}, headers=_h(narrow))
    assert r.json()["reminders"] == []


# --- П10: видимость -----------------------------------------------------------------------------------------


async def test_p10_title_and_note_only_while_channel_is_visible_to_token(client, ui, db, clock, world):
    w = world
    narrow = _token(db, name="mcp:narrow", project_ids=[w["p3"]])
    await _sub(client, w, A, role="маркетолог", token=narrow)
    item = await _remind(client, w, A, w["ch3"], _at(clock, 30), token=narrow)
    assert item["note"] == NOTE and item["channel"]["title"] == "Партнёр"

    # беседу унесли в проект, которого токен не видит
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (w["p4"], w["ch3"]))
    )
    clock.advance_ms(31 * MIN)
    _tick(db, clock)

    for items in (
        await _list(client, w, token=narrow),
        await _list(client, w, A, token=narrow),
        [h["reminder"] for h in await _home_reminders(client, _h(narrow))],
        (await client.get("/api/v1/sync", params={"since": 0}, headers=_h(narrow))).json()["reminders"],
    ):
        assert [(i["id"], i["channel_id"], i["channel"], i["note"]) for i in items] == [
            (item["id"], w["ch3"], None, None)
        ]
    home = await _home_reminders(client, _h(narrow))
    assert home[0]["channel"] is None and home[0]["project"]["id"] == w["p3"]

    # Владелец видит всё
    home = await _home_reminders(ui)
    assert (home[0]["reminder"]["note"], home[0]["channel"]["title"]) == (NOTE, "Партнёр")
    # значок в шапке беседы: ждущие напоминания беседы — фильтром по беседе
    again = await ui.post("/api/v1/reminders", {"channel_id": w["ch3"], "at": _at(clock, 30), "note": "n"})
    r = await ui.get("/api/v1/reminders", params={"channel_id": w["ch3"]})
    assert [(i["id"], i["status"]) for i in r.json()["items"]] == [
        (item["id"], "fired"),
        (again.json()["id"], "pending"),
    ]
    assert (await ui.get("/api/v1/reminders", params={"channel_id": w["ch4"]})).json() == {"items": []}
    # session-параметры: все три или ни одного; UI — без них
    r = await client.get("/api/v1/reminders", params={"pid": 1}, headers=_h(narrow))
    assert r.status_code == 422
    r = await client.get("/api/v1/reminders", params=_session(w, C, full=False), headers=_h(narrow))
    assert r.status_code == 409  # у сессии нет подписки


# --- миграция 0047: только главный файл ---------------------------------------------------------------------


def test_migration_0047_main_only(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    instance = Db(data_dir)
    instance.start()
    try:
        with instance.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 47
            assert conn.execute("SELECT COUNT(*) FROM main.reminder").fetchone()[0] == 0
            in_work = conn.execute(
                "SELECT COUNT(*) FROM work.sqlite_master WHERE name = 'reminder'"
            ).fetchone()[0]
            assert in_work == 0
            assert "reminder" not in db_module._SHARED_TABLES_WITH_OWN_ID
    finally:
        instance.close()


# --- ревью Ф34-2 (находки, воспроизведённые тестом) ---


async def test_review_f1_merge_old_foreign_message_is_not_a_reply(client, db, clock, world):
    """Напоминание на заготовку первого сообщения (base = «сейчас»). Пока черновик ждал «да» владельца, в уже
    существующей беседе собеседник написал что-то своё (ДО нашего письма). После слияния напоминание
    переезжает со старым base — это сообщение считается «ответом», напоминание молча снимается."""
    w = world
    await _sub(client, w, A, role="маркетолог")
    draft = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="new:abc", title="@petr")
    target = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="dm-9", title="Пётр")
    item = await _remind(client, w, A, draft, _at(clock, 120))
    clock.advance_ms(5 * MIN)
    insert_message(db, target, ext_id="old-1", ts=_now(clock), is_me=0)  # до нашего первого сообщения
    clock.advance_ms(5 * MIN)
    db.write_sync(
        lambda conn: draft_channels._merge(
            conn, "main", draft, target, outbox_id=None, now_ms=_now(clock), via="report"
        )
    )
    insert_message(db, target, ext_id="mine-1", ts=_now(clock), is_me=1)  # наше письмо ушло
    clock.advance_ms(111 * MIN)
    counts = _tick(db, clock)
    assert _rec(db, item["id"]).status == "fired", counts


async def test_review_f1b_migrate_old_target_messages_are_not_a_reply(client, db, clock, world):
    """То же через `channel_migrate`: в новой беседе уже лежат чужие сообщения новее base старой."""
    w = world
    await _sub(client, w, A, role="маркетолог")
    old = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="thr-old", kind="mail_thread", title="T")
    insert_message(db, old, ext_id="o1", ts=_now(clock) - 60 * MIN, is_me=1)
    new = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="jira:x:K-1", kind="issue", title="K-1")
    insert_message(db, new, ext_id="n1", ts=_now(clock) - 10 * MIN, is_me=0)  # уже было до постановки
    item = await _remind(client, w, A, old, _at(clock, 30))
    event = {
        "type": "channel_migrate",
        "old_ext_id": "thr-old",
        "channel": {"ext_id": "jira:x:K-1", "kind": "issue"},
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=_now(clock))
    )
    assert _rec(db, item["id"]).channel_id == new
    clock.advance_ms(31 * MIN)
    counts = _tick(db, clock)
    assert _rec(db, item["id"]).status == "fired", counts



async def test_review_f4_move_bypasses_channel_limit(client, db, clock, world):
    """5 ждущих на беседу: сработавшее шестое переносом снова становится ждущим — лимит не проверяется."""
    w = world
    await _sub(client, w, A, role="маркетолог")
    first = await _remind(client, w, A, w["ch3"], _at(clock, 2), cancel_if_reply=False)
    clock.advance_ms(3 * MIN)
    _tick(db, clock)
    for _ in range(5):
        await _remind(client, w, A, w["ch3"], _at(clock, 30))
    await _patch(client, w, A, first["id"], expect=429, at=_at(clock, 60))
