"""Ф41-3, части 3а и 3б на уровне ядра (docs/architecture.md):
подробности событий (`brief`/`detail`), ручка одного события, расход в `turn`, текст по мере набора — приём
`live` и поток `GET /sessions/live`.

Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

import asyncio
import json

from hub.core import held, held_work, live
from hub.core.feed import Notifier

from .test_f41_held_work import (
    API,
    _bearer,
    _events,
    _get,
    _host,
    _mk,
    _rec,
    _run,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)

DIFF_BRIEF = {"type": "diff", "file": "src/a.py", "added": 2, "removed": 1}
DIFF_DETAIL = {
    "type": "diff",
    "file": "src/a.py",
    "patch": "@@ -1,1 +1,2 @@\n-x = 1\n+x = 2\n+y = 3",
    "cut": False,
}


async def _post_events(client, w, sid, events, *, expect=200):  # noqa: F811
    return await _host(client, w, f"sessions/{sid}/events", {"events": events}, expect=expect)


def _detail_json(db, event_id):
    with db.reader() as conn:
        return conn.execute("SELECT detail_json FROM main.held_event WHERE id = ?", (event_id,)).fetchone()[0]


# --- 2.1: brief и detail ------------------------------------------------------------------------------------


async def test_event_brief_and_detail_are_stored_listed_and_read_one_by_one(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    other = await _mk(ui, w, project="p4")
    await _post_events(
        client,
        w,
        sid,
        [
            {"kind": "tool", "tool": "Edit", "summary": "src/a.py"},
            {"kind": "tool_done", "summary": "готово (10 симв.)", "brief": DIFF_BRIEF, "detail": DIFF_DETAIL},
            {
                "kind": "tool",
                "tool": "Bash",
                "summary": "ls",
                "detail": {"type": "input", "command": "ls -la"},
            },
            {"kind": "turn_end", "summary": "ход окончен", "brief": {"type": "usage", "steps": 2}},
        ],
    )
    plain, done, tool, end = await _events(ui, sid)
    # Список: `brief` и признак, самого `detail` нет.
    assert all("detail" not in e for e in (plain, done, tool, end))
    assert (plain["brief"], plain["has_detail"]) == (None, False)
    assert (done["brief"], done["has_detail"]) == (DIFF_BRIEF, True)
    assert (tool["brief"], tool["has_detail"]) == (None, True)
    assert (end["brief"], end["has_detail"]) == ({"type": "usage", "steps": 2}, False)
    assert _detail_json(db, plain["id"]) is None  # без подробностей столбец остаётся пустым
    assert json.loads(_detail_json(db, done["id"])) == {"brief": DIFF_BRIEF, "detail": DIFF_DETAIL}

    # Одно событие — целиком.
    r = await ui.get(f"{API}/sessions/{sid}/events/{done['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {**done, "detail": DIFF_DETAIL}
    assert set(r.json()) == {
        "id",
        "ts",
        "kind",
        "tool",
        "summary",
        "is_error",
        "ref",
        "brief",
        "has_detail",
        "detail",
    }
    r = await ui.get(f"{API}/sessions/{sid}/events/{plain['id']}")
    assert (r.json()["detail"], r.json()["has_detail"], r.json()["brief"]) == (None, False, None)

    # Чужое событие, чужая сессия, нет события — 404.
    for path in (
        f"sessions/{other}/events/{done['id']}",
        f"sessions/{sid}/events/{done['id'] + 1000}",
        f"sessions/999/events/{done['id']}",
        f"sessions/{sid}/events/0",
    ):
        r = await ui.get(f"{API}/{path}")
        assert (r.status_code, r.json()) == (404, {"detail": "not found"}), path

    # Bearer `read`: свой проект — можно, чужой — 404; без токена — отказ входа.
    r = await client.get(f"{API}/sessions/{sid}/events/{done['id']}", headers=_bearer(w["tok3"]))
    assert r.status_code == 200 and r.json()["detail"] == DIFF_DETAIL
    r = await client.get(f"{API}/sessions/{sid}/events/{done['id']}", headers=_bearer(w["tok4"]))
    assert r.status_code == 404
    r = await client.get(f"{API}/sessions/{sid}/events", headers=_bearer(w["tok4"]))
    assert r.status_code == 404
    r = await client.get(f"{API}/sessions/{sid}/events/{done['id']}")
    assert r.status_code == 401


async def test_event_detail_limits_are_a_safety_net_not_a_refusal(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    big_detail = {"type": "output", "text": "я" * 40_000}  # 80 000 байт в utf-8 — больше 60 000
    ok_detail = {"type": "output", "text": "я" * 29_000}  # 58 000 байт — влезает
    big_brief = {"type": "diff", "file": "f" * 1100}
    accepted = await _post_events(
        client,
        w,
        sid,
        [
            {
                "kind": "tool_done",
                "summary": "а",
                "brief": {"type": "output", "lines": 3},
                "detail": big_detail,
            },
            {"kind": "tool_done", "summary": "б", "brief": big_brief, "detail": ok_detail},
            {"kind": "tool_done", "summary": "в", "detail": big_detail},
            {
                "kind": "tool_done",
                "summary": "г",
                "brief": {"type": "output", "lines": 1},
                "detail": ok_detail,
            },
        ],
    )
    assert accepted == {"accepted": 4}  # 422 на всю пачку нет
    a, b, c, d = await _events(ui, sid)
    assert (a["brief"], a["has_detail"]) == ({"type": "output", "lines": 3, "cut": True}, False)
    assert (b["brief"], b["has_detail"]) == ({"cut": True}, True)
    assert (c["brief"], c["has_detail"]) == ({"cut": True}, False)
    assert (d["brief"], d["has_detail"]) == ({"type": "output", "lines": 1}, True)
    r = await ui.get(f"{API}/sessions/{sid}/events/{b['id']}")
    assert r.json()["detail"] == ok_detail
    # Не объект — отказ модели запроса (хост таких не шлёт).
    await _post_events(client, w, sid, [{"kind": "tool", "summary": "x", "brief": "строка"}], expect=422)
    await _post_events(client, w, sid, [{"kind": "tool", "summary": "x", "detail": [1]}], expect=422)


def test_pack_detail_units():
    assert held_work.pack_detail(None, None) is None
    assert held_work.pack_detail({}, None) == '{"brief": {}, "detail": null}'
    exact = {"t": "x" * (held_work.BRIEF_MAX - len('{"t": ""}'))}
    assert json.loads(held_work.pack_detail(exact, None))["brief"] == exact  # ровно предел — влезает
    over = {"t": exact["t"] + "x"}
    assert json.loads(held_work.pack_detail(over, None))["brief"] == {"cut": True}
    # Предел — в байтах после сериализации, а не в символах.
    cyr = {"t": "я" * 600}
    assert json.loads(held_work.pack_detail(cyr, None))["brief"] == {"cut": True}


# --- 2.3: расход в записи сессии ----------------------------------------------------------------------------


async def test_turn_usage_accumulates_cost_and_shows_in_all_three_forms(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    assert (await _get(ui, sid))["session"]["usage"] is None
    await _run(client, w, sid)
    seq0 = _rec(db, sid).seq
    usage = {"context_tokens": 84_000, "context_limit": 200_000, "turn_cost_usd": 0.03}
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "usage": usage}))["session"]
    now = int(clock() * 1000)
    want = {"context_tokens": 84_000, "context_limit": 200_000, "cost_usd": 0.03, "updated_ts": now}
    assert row["usage"] == want  # форма «для хоста»
    assert (await _get(ui, sid))["session"]["usage"] == want  # публичная
    r = await ui.get(f"{API}/sessions")
    assert [s["usage"] for s in r.json()["items"] if s["id"] == sid] == [want]
    r = await ui.get(f"{API}/sync", params={"since": seq0})
    assert [s["usage"] for s in r.json()["hub_sessions"] if s["id"] == sid] == [want]  # элемент /sync
    host_items = (await _host(client, w, "sessions"))["items"]
    assert [s["usage"] for s in host_items if s["id"] == sid] == [want]
    seq1 = _rec(db, sid).seq
    assert seq1 > seq0

    # Тот же `turn` с тем же расходом — `seq` всё равно двигается (обновилось время и сумма).
    clock.advance_ms(1000)
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "usage": usage})
    rec = _rec(db, sid)
    assert rec.seq > seq1
    assert (await _get(ui, sid))["session"]["usage"] == {**want, "cost_usd": 0.06, "updated_ts": now + 1000}

    # Ход без цены: контекст и предел обновились, сумма прежняя; `turn` без `usage` расход не трогает.
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "usage": {"context_tokens": 90_000}})
    got = (await _get(ui, sid))["session"]["usage"]
    assert (got["context_tokens"], got["context_limit"], got["cost_usd"]) == (90_000, None, 0.06)
    seq2 = _rec(db, sid).seq
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle"})
    assert _rec(db, sid).seq == seq2 and (await _get(ui, sid))["session"]["usage"] == got


async def test_turn_usage_without_any_price_keeps_cost_null_and_validates(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    body = {"state": "idle", "usage": {"context_tokens": 10, "context_limit": 100, "turn_cost_usd": None}}
    row = (await _host(client, w, f"sessions/{sid}/turn", body))["session"]
    assert row["usage"]["cost_usd"] is None  # ни одной цены не было
    for bad in (
        {"context_tokens": -1},
        {"context_tokens": 1, "turn_cost_usd": -0.1},
        {"context_tokens": 1, "context_limit": -5},
        {"context_tokens": 1, "extra": 2},
        {"context_limit": 5},
        {"context_tokens": "много"},
    ):
        await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "usage": bad}, expect=422)
    assert _rec(db, sid).usage_json == json.dumps(
        {
            "context_tokens": 10,
            "context_limit": 100,
            "cost_usd": None,
            "updated_ts": row["usage"]["updated_ts"],
        }
    )


# --- 3.1: текст по мере набора ------------------------------------------------------------------------------


def _live_body(text="при", *, block="msg_1:0", kind="text", **kw) -> dict:
    return {"block": block, "kind": kind, "text": text, **kw}


async def _next(gen) -> str:
    return await asyncio.wait_for(gen.__anext__(), timeout=2.0)


def _data(frame: str) -> dict:
    assert frame.startswith("event: live\ndata: ") and frame.endswith("\n\n"), frame
    assert "\nid:" not in frame and not frame.startswith("id:")  # поток не возобновляется
    return json.loads(frame.split("data: ", 1)[1])


async def test_live_is_memory_only_and_does_not_touch_the_record(client, ui, db, clock, app, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    before = _rec(db, sid)
    with db.reader() as conn:
        audits = conn.execute("SELECT COUNT(*) FROM main.audit").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM main.held_event").fetchone()[0]
    clock.advance_ms(5000)
    assert await _host(client, w, f"sessions/{sid}/live", _live_body("привет")) == {"ok": True}
    after = _rec(db, sid)
    assert after == before  # ни `seq`, ни `host_seen_ts`, ни `updated_ts`
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.audit").fetchone()[0] == audits
        assert conn.execute("SELECT COUNT(*) FROM main.held_event").fetchone()[0] == events
        dump = "\n".join(conn.iterdump())
    assert "привет" not in dump
    entry = app.state.live.get(sid, int(clock() * 1000))
    assert entry == {
        "block": "msg_1:0",
        "kind": "text",
        "text": "привет",
        "cut": False,
        "done": False,
        "ts": int(clock() * 1000),
    }

    # Отказы — как у прочих ручек хоста; кука UI и обычный bearer сюда не ходят.
    await _host(client, w, "sessions/999/live", _live_body(), expect=404)
    for bad in (
        _live_body(kind="tool_use"),
        _live_body("я" * 8001),
        _live_body(block=""),
        _live_body(block="b" * 201),
        {**_live_body(), "extra": 1},
        {"block": "b", "kind": "text"},
    ):
        await _host(client, w, f"sessions/{sid}/live", bad, expect=422)
    r = await ui.c.post(f"{API}/work/host/sessions/{sid}/live", json=_live_body(), headers=ui.headers)
    assert r.status_code == 403
    r = await client.post(
        f"{API}/work/host/sessions/{sid}/live", json=_live_body(), headers=_bearer(w["tok"])
    )
    assert r.status_code == 403
    await ui.post(f"{API}/sessions/{sid}/close")
    await _host(client, w, f"sessions/{sid}/live", _live_body(), expect=410)
    assert app.state.live.get(sid, int(clock() * 1000)) is None  # закрытие сняло запись


async def test_live_stream_gives_current_state_then_changes_and_collapses(client, ui, clock, app, world):  # noqa: F811
    w = world
    now_ms = lambda: int(clock() * 1000)  # noqa: E731
    first = await _mk(ui, w)
    second = await _mk(ui, w, project="p4")
    await _run(client, w, first)
    await _run(client, w, second)
    await _host(client, w, f"sessions/{first}/live", _live_body("раз"))
    hub = app.state.live
    gen = live.stream(hub, now_ms=now_ms, heartbeat_s=0.3)
    try:
        # При подключении — текущие записи всех сессий.
        assert _data(await _next(gen)) == {
            "session_id": first,
            "block": "msg_1:0",
            "kind": "text",
            "text": "раз",
            "cut": False,
            "done": False,
        }
        assert hub.subscribers == 1
        # Дальше — каждое изменение.
        await _host(client, w, f"sessions/{second}/live", _live_body("думаю", block="prt_9", kind="thinking"))
        got = _data(await _next(gen))
        assert (got["session_id"], got["block"], got["kind"], got["text"]) == (
            second,
            "prt_9",
            "thinking",
            "думаю",
        )
        # Медленный клиент: три состояния одной сессии, пока он не читал, — приходит только последнее.
        for text in ("раз д", "раз два", "раз два три"):
            await _host(client, w, f"sessions/{first}/live", _live_body(text))
        await _host(
            client,
            w,
            f"sessions/{second}/live",
            _live_body("думаю", block="prt_9", kind="thinking", done=True),
        )
        frames = [_data(await _next(gen)), _data(await _next(gen))]
        assert {(f["session_id"], f["text"], f["done"]) for f in frames} == {
            (first, "раз два три", False),
            (second, "думаю", True),
        }
        # Тишина — комментарий-пульс, а не кадр.
        assert await _next(gen) == ": hb\n\n"
        # Длинный текст хост режет сам; страховка ядра — последние 8000 символов и `cut`.
        hub.publish(
            first, block="b", kind="text", text="а" + "я" * 8000, cut=False, done=False, now_ms=now_ms()
        )
        got = _data(await _next(gen))
        assert (got["text"], got["cut"]) == ("я" * 8000, True)
    finally:
        await gen.aclose()
    assert hub.subscribers == 0  # подписка снята вместе с потоком


async def test_live_entry_is_dropped_by_done_by_state_and_by_age(client, ui, clock, app, world):  # noqa: F811
    w = world
    hub = app.state.live
    now_ms = lambda: int(clock() * 1000)  # noqa: E731
    sid = await _mk(ui, w)

    # Сессия не в ходу — `live` принят и отброшен.
    await _host(client, w, f"sessions/{sid}/live", _live_body())
    assert hub.get(sid, now_ms()) is None
    await _run(client, w, sid, state="idle")
    await _host(client, w, f"sessions/{sid}/live", _live_body())
    assert hub.get(sid, now_ms()) is None

    # `done: true` — запись живёт ещё 10 с (опоздавший клиент), потом снята.
    await _run(client, w, sid)
    await _host(client, w, f"sessions/{sid}/live", _live_body("готово", done=True))
    clock.advance_ms(live.DONE_KEEP_MS - 5)
    assert hub.get(sid, now_ms())["done"] is True
    gen = live.stream(hub, now_ms=now_ms, heartbeat_s=0.2)
    try:
        assert _data(await _next(gen))["text"] == "готово"
    finally:
        await gen.aclose()
    clock.advance_ms(10)
    assert hub.get(sid, now_ms()) is None and hub.snapshot(now_ms()) == []

    # Состояние перестало быть `running`: `turn`, `state`, `recover`.
    for path, body in (
        (f"sessions/{sid}/turn", {"state": "idle"}),
        (f"sessions/{sid}/state", {"state": "idle"}),
        (f"sessions/{sid}/turn", {"state": "asleep"}),
        ("sessions/recover", {}),
    ):
        await _run(client, w, sid)
        await _host(client, w, f"sessions/{sid}/live", _live_body("пишу"))
        assert hub.get(sid, now_ms()) is not None, path
        await _host(client, w, path, body)
        assert hub.get(sid, now_ms()) is None, path
    # Подтверждение `running` запись не снимает.
    await _run(client, w, sid)
    await _host(client, w, f"sessions/{sid}/live", _live_body("пишу"))
    await _host(client, w, f"sessions/{sid}/turn", {"state": "running"})
    await _run(client, w, sid)
    assert hub.get(sid, now_ms())["text"] == "пишу"

    # Старше 120 с — снята (хост замолчал).
    clock.advance_ms(live.STALE_MS - 5)
    assert hub.get(sid, now_ms()) is not None
    clock.advance_ms(10)
    assert hub.get(sid, now_ms()) is None
    assert held.STATES == ("running", "idle", "asleep")


async def test_live_stream_is_ui_cookie_only_and_declared_before_session_route(client, ui, db, app, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    await _host(client, w, f"sessions/{sid}/live", _live_body("видно"))
    # Bearer любого скоупа — 403 и аудит, как у действий владельца (а не 422 разбора номера сессии).
    for token in (w["tok"], w["tok3"], w["host"]):
        r = await client.get(f"{API}/sessions/live", headers=_bearer(token))
        assert (r.status_code, r.json()) == (403, {"detail": "forbidden: ui session required"})
    with db.reader() as conn:
        denied = conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'ui_only_denied'").fetchone()[0]
    assert denied == 3
    r = await client.get(f"{API}/sessions/live")
    assert r.status_code == 401
    assert app.state.live.subscribers == 0

    # Кука UI: поток SSE с текущей записью; остановка ядра закрывает его (ASGITransport отдаёт тело целиком).
    asyncio.get_running_loop().call_later(0.3, app.state.live.close)
    r = await asyncio.wait_for(ui.get(f"{API}/sessions/live"), timeout=5)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"
    assert _data(r.text)["text"] == "видно" and _data(r.text)["session_id"] == sid
    assert app.state.live.subscribers == 0


async def test_live_stream_closes_on_core_stop_and_on_revoked_session(app):
    hub = live.LiveHub()
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    notifier.on_close(hub.close)
    gen = live.stream(hub, now_ms=lambda: 0, heartbeat_s=30)
    waiting = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    assert not waiting.done() and hub.subscribers == 1
    notifier.close()  # остановка ядра
    try:
        await asyncio.wait_for(waiting, timeout=2)
        raise AssertionError("поток не закрылся")
    except StopAsyncIteration:
        pass
    assert hub.subscribers == 0 and hub.closed
    # Приложение вешает закрытие само (`Notifier.on_close`).
    assert app.state.live.closed is False
    app.state.notifier.close()
    assert app.state.live.closed is True

    # Сессия UI отозвана или доступ с телефона выключен — поток закрывается на ближайшей проверке, и под
    # потоком кадров тоже (проверка — по сроку, а не только при тишине).
    hub = live.LiveHub()
    valid = {"ok": True}
    gen = live.stream(hub, now_ms=lambda: 0, heartbeat_s=0.2, still_valid=lambda: valid["ok"])
    hub_task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    hub.publish(1, block="b", kind="text", text="а", cut=False, done=False, now_ms=0)
    assert _data(await asyncio.wait_for(hub_task, 2))["text"] == "а"
    valid["ok"] = False
    got = []
    for i in range(40):
        await asyncio.sleep(0.02)
        hub.publish(1, block="b", kind="text", text=f"а{i}", cut=False, done=False, now_ms=0)
        try:
            got.append(await _next(gen))
        except StopAsyncIteration:
            break
    else:
        raise AssertionError("поток не закрылся по отзыву сессии")
    assert hub.subscribers == 0

    # Не действует с самого начала — ни одного кадра.
    hub.publish(1, block="b", kind="text", text="тайна", cut=False, done=False, now_ms=0)
    gen = live.stream(hub, now_ms=lambda: 0, still_valid=lambda: False)
    assert [frame async for frame in gen] == []


async def test_live_stream_cap(app, ui):  # noqa: F811
    for _ in range(live.MAX_SUBSCRIBERS):
        assert app.state.live.subscribe() is not None
    r = await ui.get(f"{API}/sessions/live")
    assert r.status_code == 429 and "вкладки" in r.json()["detail"]

    hub = live.LiveHub()
    gens = [live.stream(hub, now_ms=lambda: 0, heartbeat_s=30) for _ in range(live.MAX_SUBSCRIBERS)]
    tasks = [asyncio.ensure_future(g.__anext__()) for g in gens]
    await asyncio.sleep(0.05)
    assert hub.full and hub.subscribers == live.MAX_SUBSCRIBERS
    extra = live.stream(hub, now_ms=lambda: 0)
    assert [frame async for frame in extra] == []
    hub.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert hub.subscribers == 0
