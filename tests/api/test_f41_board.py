"""Ф41-6 (docs/architecture.md, пункты 2–5): ядро доски — расход сессий хаба за сегодня,
линии роли «не в сети», «Забрать в хаб», режим сессии в `/sync`.

Помощники и «мир» — из test_hub_sessions.py; хост поддельный (bearer `work_host`)."""

from hub.core import adopt, subs

from . import test_hub_sessions as base
from .test_hub_sessions import (
    CODER,
    EXTRA,
    MIN,
    ROLE,
    A,
    B,
    _cleanup,
    _h,
    _host,
    _mk,
    _now,
    _sess,
    _sub,
    _token,
)

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
PROC = {"proc_pid": 4242, "proc_start": 777_000}
DAY = 24 * 60 * MIN


async def _lines(ui_or_client, *, headers=None) -> list[dict]:
    kw = {"headers": headers} if headers is not None else {}
    r = await ui_or_client.get(f"{API}/subs", params={"offline": 1}, **kw)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"lines"}
    return r.json()["lines"]


def _die(db, clock, app, procs_table, who) -> None:
    """Окно внешней сессии закрыли: процесса нет, чистка ядра завершает подписку (`session_gone`)."""
    procs_table.pop(who[0], None)
    _cleanup(db, clock, app)


def _set(db, sub_id: int, **cols) -> None:
    names = list(cols)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE main.session_sub SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
            [*(cols[n] for n in names), sub_id],
        )
    )


# --- п.2: расход за сегодня ---------------------------------------------------------------------------------


async def test_today_sums_turn_costs_since_local_midnight_and_is_cached(client, ui, db, clock, world):  # noqa: F811
    w = world
    r = await ui.get(f"{API}/sessions")
    assert r.json()["today"] == {"cost_usd": 0, "turns": 0}
    clock.advance_ms(30_000)
    sid = (await _mk(ui, w))["id"]
    other = (await _mk(ui, w, project="p4"))["id"]
    now = _now(clock)
    midnight = adopt.day_start_ms(now)
    assert midnight <= now < midnight + DAY + 3_600_000

    def _end(cost, ts=None):
        ev = {"kind": "turn_end", "summary": "готово"}
        if cost is not None:
            ev["brief"] = {"type": "usage", "cost_usd": cost}
        if ts is not None:
            ev["ts"] = ts
        return ev

    events = [_end(0.03), _end(None), {"kind": "text", "summary": "x", "brief": {"cost_usd": 5}}]
    await _host(client, w, f"sessions/{sid}/events", {"events": events})
    await _host(client, w, f"sessions/{other}/events", {"events": [_end(0.02), _end(7.0, midnight - 1)]})
    r = await ui.get(f"{API}/sessions", params={"project_id": w["p3"]})
    assert [s["id"] for s in r.json()["items"]] == [sid]  # список — по проекту, расход — общий
    assert r.json()["today"] == {"cost_usd": 0.05, "turns": 3}

    # Кэш 30 с: новый ход виден не сразу; закрытая сессия из расхода не выпадает.
    await _host(client, w, f"sessions/{sid}/events", {"events": [_end(0.1)]})
    await ui.post(f"{API}/sessions/{other}/close")
    clock.advance_ms(29_000)
    assert (await ui.get(f"{API}/sessions")).json()["today"] == {"cost_usd": 0.05, "turns": 3}
    clock.advance_ms(1_000)
    assert (await ui.get(f"{API}/sessions")).json()["today"] == {"cost_usd": 0.15, "turns": 4}

    # Не интерфейсу сумма по всем проектам не отдаётся.
    r = await client.get(f"{API}/sessions", headers=_h(w["tok"]))
    assert r.status_code == 200 and "today" not in r.json()

    # Новые сутки — с нуля, не дожидаясь срока кэша.
    cache = adopt.TodayCache()
    cache.put(now, {"cost_usd": 1.0, "turns": 1})
    assert cache.get(now + 29_999) == {"cost_usd": 1.0, "turns": 1}
    assert cache.get(now + 30_000) is None and cache.get(now - 1) is None
    assert cache.get(midnight + DAY + 3_600_000) is None


# --- п.3: линии «не в сети» ---------------------------------------------------------------------------------


async def test_offline_lines_show_dead_role_lines_only(client, ui, db, clock, app, world, procs):  # noqa: F811
    w = world
    role_key = subs.role_form(ROLE)[1]
    one = (await _sub(client, w, A, role=ROLE, mode="messages"))["sub"]["id"]
    await _sub(client, w, B, role=CODER)
    bare = (await _sub(client, w, EXTRA[0]))["sub"]["id"]  # без роли — линии нет
    assert await _lines(ui) == []  # жива — в списке нет

    clock.advance_ms(MIN)
    ended = _now(clock)
    _die(db, clock, app, procs, A)
    _die(db, clock, app, procs, EXTRA[0])
    assert bare != one
    line = {
        "key": f"p{w['p3']}:{role_key}:1",
        "project_id": w["p3"],
        "role": ROLE,
        "role_no": 1,
        "engine": "claude",
        "session_name": None,
        "cwd": w["dir3"],
        "ended_ts": ended,
        "end_reason": "session_gone",
        "pending": 0,
        "sub_id": one,
    }
    assert await _lines(ui) == [line]

    # Ждущее линии: сообщение агента, адресованное роли.
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": "передай", "to": {"project_id": w["p3"], "role": ROLE}, "session": _sess(B)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    assert await _lines(ui) == [{**line, "pending": 1}]

    # Видимость — по проектам токена.
    scoped, _ = _token(db, name="mcp:initech", project_ids=[w["p4"]])
    assert await _lines(client, headers=_h(scoped)) == []
    assert await _lines(client, headers=_h(w["tok"])) == [{**line, "pending": 1}]

    # Линию заняла сессия хаба (самой записью, без подписки) — в списке нет; закрыли — снова есть.
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    assert await _lines(ui) == []
    await ui.post(f"{API}/sessions/{hub_id}/close")
    assert await _lines(ui) == [{**line, "pending": 1}]

    # Линию заняла новая внешняя подписка — нет; она же, завершённая, становится последней подпиской линии.
    procs[A[0]] = A[1]
    two = (await _sub(client, w, A, role=ROLE, number=1))["sub"]["id"]
    assert await _lines(ui) == []
    _die(db, clock, app, procs, A)
    assert [(row["sub_id"], row["key"]) for row in await _lines(ui)] == [(two, line["key"])]

    # Старше 7 суток — нет.
    clock.advance_ms(7 * DAY - 1)
    assert len(await _lines(ui)) == 1
    clock.advance_ms(2)
    assert await _lines(ui) == []


async def test_offline_lines_skip_sub_ended_by_owner(client, ui, db, clock, app, world, procs):  # noqa: F811
    """владелец 14.10: «Отписать от хаба» (`POST /subs/{id}/end` из интерфейса) — линия не в «не в сети»."""
    w = world
    one = (await _sub(client, w, A, role=ROLE, mode="messages"))["sub"]["id"]
    r = await ui.post(f"{API}/subs/{one}/end", {})
    assert r.status_code == 200, r.text
    assert r.json()["sub"]["end_reason"] == "ended_by_owner"
    assert await _lines(ui) == []
    # Та же линия, кончившаяся сама (окно закрыли), — в «не в сети» есть.
    procs[A[0]] = A[1]
    two = (await _sub(client, w, A, role=ROLE, number=1))["sub"]["id"]
    _die(db, clock, app, procs, A)
    assert [row["sub_id"] for row in await _lines(ui)] == [two]
