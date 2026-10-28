"""Ф48-1, раздел C на уровне ядра (docs/architecture.md): фоновые задачи постоянной
сессии — поле `bg` ручек хоста `…/state` и `…/turn`, выдача во всех трёх формах записи и в `/sync`, отказ на
лишние поля и длины, `seq` только при изменении, `asleep` и закрытие чистят; миграция 0066.

Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

import pytest

from .test_f41_held_work import (
    API,
    PROC,
    _get,
    _host,
    _mk,
    _rec,
    _run,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)

AGENT = {
    "id": "a93804abbb8d7ab37",
    "kind": "agent",
    "title": "счёт до двадцати",
    "agent_type": "worker",
    "parent": None,
    "state": "running",
    "started_ts": 1791558198000,
    "ended_ts": None,
    "doing": "Running Wait 20 seconds and print A",
    "last_tool": "Bash",
    "tool_uses": 1,
    "tokens": 87382,
    "note": None,
}
COMMAND = {"id": "bq2fvnjhk", "kind": "command", "title": "фоновый счёт", "state": "running", "started_ts": 5}
COMMAND_FULL = {
    **dict.fromkeys(
        ("agent_type", "parent", "ended_ts", "doing", "last_tool", "tool_uses", "tokens", "note")
    ),
    **COMMAND,
}


async def _state(client, w, sid, bg=..., *, state="idle", expect=200):  # noqa: F811
    body = {"state": state, **PROC}
    if bg is not ...:
        body["bg"] = bg
    return await _host(client, w, f"sessions/{sid}/state", body, expect=expect)


async def _sync_bg(ui, since, sid) -> list:  # noqa: F811
    r = await ui.get(f"{API}/sync", params={"since": since})
    assert r.status_code == 200, r.text
    return [s["bg"] for s in r.json()["hub_sessions"] if s["id"] == sid]


async def test_bg_is_stored_and_shown_in_all_forms_and_sync_only_on_change(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    assert (await _get(ui, sid))["session"]["bg"] is None  # задач нет и не было
    seq0 = _rec(db, sid).seq
    # «Задач нет» у сессии, где их и не было, — ничего не пишется и `seq` не двигается.
    await _state(client, w, sid, {"items": []})
    assert _rec(db, sid).seq == seq0 and _rec(db, sid).bg_json is None

    want = {"items": [AGENT, COMMAND_FULL]}
    row = (await _state(client, w, sid, {"items": [AGENT, COMMAND]}))["session"]
    assert row["bg"] == want  # форма «для хоста»; не названные ключи записи — `null`
    assert (await _get(ui, sid))["session"]["bg"] == want  # публичная
    r = await ui.get(f"{API}/sessions")
    assert [s["bg"] for s in r.json()["items"] if s["id"] == sid] == [want]
    assert await _sync_bg(ui, seq0, sid) == [want]  # элемент /sync
    seq1 = _rec(db, sid).seq
    assert seq1 > seq0

    # Тот же снимок, отсутствие ключа и `null` («не менять») — `seq` на месте, `/sync` молчит.
    await _state(client, w, sid, {"items": [AGENT, COMMAND]})
    await _state(client, w, sid)
    await _state(client, w, sid, None)
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle"})
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "bg": None})
    assert _rec(db, sid).seq == seq1 and await _sync_bg(ui, seq1, sid) == []
    assert (await _get(ui, sid))["session"]["bg"] == want

    # Изменение — через `turn` тоже (тот же путь, что расход): прогресс подагента.
    moved = {**AGENT, "tool_uses": 2, "tokens": 92_486}
    body = {"state": "idle", "bg": {"items": [moved]}}
    row = (await _host(client, w, f"sessions/{sid}/turn", body))["session"]
    assert row["bg"] == {"items": [moved]} and _rec(db, sid).seq > seq1
    assert await _sync_bg(ui, seq1, sid) == [{"items": [moved]}]

    # Явный пустой список — «задач нет»: пишется как есть (задачи были).
    seq2 = _rec(db, sid).seq
    await _state(client, w, sid, {"items": []})
    assert (await _get(ui, sid))["session"]["bg"] == {"items": []} and _rec(db, sid).seq > seq2


async def test_asleep_and_close_clear_bg(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    await _state(client, w, sid, {"items": [AGENT]})
    # `asleep` чистит сам, и присланный вместе с ним снимок не пишется.
    row = await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "bg": {"items": [AGENT]}})
    assert row["session"]["bg"] is None and _rec(db, sid).bg_json is None
    await _run(client, w, sid, state="idle")
    await _state(client, w, sid, {"items": [AGENT]})
    await _host(client, w, f"sessions/{sid}/state", {"state": "asleep"})
    assert (await _get(ui, sid))["session"]["bg"] is None

    await _run(client, w, sid, state="idle")
    await _state(client, w, sid, {"items": [AGENT]})
    out = await ui.post(f"{API}/sessions/{sid}/close")
    assert out["closed"] is True and out["session"]["bg"] is None and _rec(db, sid).bg_json is None


@pytest.mark.parametrize(
    "bad",
    [
        {"items": [{**AGENT, "extra": 1}]},
        {"items": [AGENT], "extra": 1},
        {"items": [{**AGENT, "kind": "other"}]},
        {"items": [{**AGENT, "id": "x" * 65}]},
        {"items": [{**AGENT, "id": ""}]},
        {"items": [{**AGENT, "title": "x" * 201}]},
        {"items": [{**AGENT, "agent_type": "x" * 65}]},
        {"items": [{**AGENT, "parent": "x" * 65}]},
        {"items": [{**AGENT, "state": "x" * 21}]},
        {"items": [{**AGENT, "doing": "x" * 201}]},
        {"items": [{**AGENT, "last_tool": "x" * 65}]},
        {"items": [{**AGENT, "note": "x" * 301}]},
        {"items": [{**AGENT, "tokens": -1}]},
        {"items": [{**AGENT, "tool_uses": 1.5}]},
        {"items": [{**AGENT, "started_ts": None}]},
        {"items": [{**AGENT, "id": f"t{n}"} for n in range(41)]},
        {},
        [],
    ],
)
async def test_bg_body_is_validated(client, ui, db, world, bad):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    seq = _rec(db, sid).seq
    await _state(client, w, sid, bad, expect=422)
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "bg": bad}, expect=422)
    assert _rec(db, sid).seq == seq and _rec(db, sid).bg_json is None


async def test_bg_accepts_limits_and_unknown_state(client, ui, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    edge = {
        **AGENT,
        "id": "i" * 64,
        "title": "т" * 200,
        "agent_type": "a" * 64,
        "parent": "p" * 64,
        "state": "killed_by_something_",
        "ended_ts": 1791558199000,
        "doing": "d" * 200,
        "last_tool": "l" * 64,
        "note": "n" * 300,
    }
    items = [edge, *({**AGENT, "id": f"t{n}"} for n in range(39))]
    row = (await _state(client, w, sid, {"items": items}))["session"]
    assert row["bg"] == {"items": items}


def test_migration_0066_on_previous_version(tmp_path, monkeypatch):
    from hub.core import db as db_module
    from hub.core.db import Db

    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 65}
    )
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            assert "bg_json" not in {r[1] for r in conn.execute("PRAGMA main.table_info(hub_session)")}
            work_before = conn.execute("PRAGMA work.user_version").fetchone()[0]
    finally:
        old.close()
    # Только 0066: более поздние общие миграции (0068, Ф48-2) двигают и вторую базу — они не об этом тесте.
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 66}
    )
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 66
            assert "bg_json" in {r[1] for r in conn.execute("PRAGMA main.table_info(hub_session)")}
            # Один номер — один файл: вторая база на 0066 не переходит.
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == work_before
    finally:
        new.close()
