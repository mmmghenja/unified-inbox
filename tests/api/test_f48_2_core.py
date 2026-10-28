"""Ф48-2 на уровне ядра (docs/architecture.md, D): прерванная
реплика возвращается в очередь и ждёт решения владельца («Отправить снова» / «Убрать»), причина ожидания
`compact`, свой порог и выключатель авто-сжатия сессии, действующий порог от хоста, умолчания роли; миграции
0067 и 0068.

Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

import dataclasses

import pytest

from hub import roles
from hub.core import held, held_work

from .test_f41_held_work import (
    API,
    _events,
    _get,
    _host,
    _mk,
    _now,
    _rec,
    _run,
    _say,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)


async def _turn(client, w, sid, body, *, expect=200):  # noqa: F811
    return await _host(client, w, f"sessions/{sid}/turn", body, expect=expect)


async def _fed(client, ui, w, text="первая", priority="next") -> tuple[int, int]:  # noqa: F811
    """Сессия, у которой идёт ход по реплике владельца: (id сессии, id реплики)."""
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    say_id = (await _say(ui, sid, text, priority))["id"]
    await _host(client, w, f"session-says/{say_id}/delivered", {})
    await _turn(client, w, sid, {"state": "running"})
    return sid, say_id


async def _interrupted(client, ui, w, **kw) -> tuple[int, int]:  # noqa: F811
    """То же, и ход оборван: реплика уже в очереди прерванной."""
    sid, say_id = await _fed(client, ui, w, **kw)
    out = await _turn(client, w, sid, {"state": "idle", "interrupted_say": say_id})
    assert out["interrupted_say"] == say_id
    return sid, say_id


async def _queue(ui, sid) -> list[tuple]:  # noqa: F811
    return [(s["id"], s["status"], s["priority"]) for s in (await _get(ui, sid))["says"]]


async def _delete(ui, sid, say_id, *, expect=200) -> dict:  # noqa: F811
    r = await ui.c.delete(f"{API}/sessions/{sid}/says/{say_id}", headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _patch(ui, sid, body, *, expect=200) -> dict:  # noqa: F811
    r = await ui.c.patch(f"{API}/sessions/{sid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


# --- A: возврат прерванной реплики --------------------------------------------------------------------------


async def test_interrupted_say_returns_to_the_head_of_the_queue(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid, first = await _fed(client, ui, w)
    second = (await _say(ui, sid, "вторая"))["id"]
    assert (await _get(ui, sid))["session"]["interrupted_says"] == 0
    seq0 = _rec(db, sid).seq

    out = await _turn(client, w, sid, {"state": "idle", "interrupted_say": first})
    assert out["interrupted_say"] == first and out["session"]["state"] == "idle"
    full = await _get(ui, sid)
    # Прерванная — в НАЧАЛЕ очереди, раньше реплик, набранных после неё.
    assert await _queue(ui, sid) == [(first, "interrupted", "next"), (second, "queued", "next")]
    assert full["says"][0]["interrupted_ts"] == _now(clock) and full["says"][1]["interrupted_ts"] is None
    assert (full["session"]["queued_says"], full["session"]["interrupted_says"]) == (2, 1)
    assert _rec(db, sid).seq > seq0
    last = (await _events(ui, sid))[-1]
    assert (last["kind"], last["ref"], last["summary"]) == ("say", str(first), held_work.NOTE_SAY_INTERRUPTED)
    # `/sync` и список несут то же число.
    r = await ui.get(f"{API}/sync", params={"since": seq0})
    assert [s["interrupted_says"] for s in r.json()["hub_sessions"] if s["id"] == sid] == [1]
    # Хосту очередь приходит с той же отметкой — он прерванную не подаёт.
    says = (await _host(client, w, "open"))["sessions"]["says"]
    assert [(s["id"], s["status"]) for s in says if s["session_id"] == sid] == [
        (first, "interrupted"),
        (second, "queued"),
    ]
    # Запоздалая отметка доставки прерванную не «подаёт».
    out = await _host(client, w, f"session-says/{first}/delivered", {}, expect=409)
    assert "прервана" in out["detail"]
    # Повтор того же конца хода — без второго события; чужой и несуществующий id — `null`, ход закрыт.
    n = len(await _events(ui, sid))
    again = await _turn(client, w, sid, {"state": "idle", "interrupted_say": first})
    assert again["interrupted_say"] == first
    assert len(await _events(ui, sid)) == n
    other, _ = await _fed(client, ui, w, text="чужая")
    for bad in (first, 999_999):
        out = await _turn(client, w, other, {"state": "idle", "interrupted_say": bad})
        assert out["interrupted_say"] is None and out["session"]["state"] == "idle"
    assert (await _get(ui, other))["says"] == []
    await _turn(client, w, sid, {"state": "idle", "interrupted_say": 0}, expect=422)


async def test_say_not_yet_marked_delivered_also_returns(client, ui, world):  # noqa: F811
    """Отметка доставки хоста могла не дойти до ядра: реплика ещё `queued` — прерванной становится и она."""
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    say_id = (await _say(ui, sid))["id"]
    out = await _turn(client, w, sid, {"state": "idle", "interrupted_say": say_id})
    assert out["interrupted_say"] == say_id
    assert await _queue(ui, sid) == [(say_id, "interrupted", "next")]


async def test_resend_makes_it_a_plain_next_say(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid, first = await _interrupted(client, ui, w, priority="now")
    second = (await _say(ui, sid, "вторая"))["id"]
    # Обычная реплика в очереди — не прерванная: 409; чужая сессия и несуществующая — 404.
    path = f"{API}/sessions/{sid}/says"
    assert (await ui.post(f"{path}/{second}/resend", expect=409))["detail"] == "реплика не прервана"
    other = await _mk(ui, w)
    await ui.post(f"{API}/sessions/{other}/says/{first}/resend", expect=404)
    await ui.post(f"{path}/999999/resend", expect=404)

    out = await ui.post(f"{path}/{first}/resend")
    assert (out["id"], out["status"]) == (first, "queued")
    assert (out["session"]["queued_says"], out["session"]["interrupted_says"]) == (2, 0)
    # Срочность снята: второй раз ход она не прерывает; порядок — по id, она снова первая.
    assert await _queue(ui, sid) == [(first, "queued", "next"), (second, "queued", "next")]
    # Время «Отправить снова» — хосту: повтор идёт движку под новым uuid.
    assert [s["resent_ts"] for s in (await _get(ui, sid))["says"]] == [_now(clock), None]
    last = (await _events(ui, sid))[-1]
    assert (last["kind"], last["ref"], last["summary"]) == ("say", str(first), held_work.NOTE_SAY_RESENT)
    await ui.post(f"{path}/{first}/resend", expect=409)  # уже не прервана
    # Дальше — обычная доставка.
    await _host(client, w, f"session-says/{first}/delivered", {})
    assert await _queue(ui, sid) == [(second, "queued", "next")]
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result FROM main.audit WHERE action = 'hub_session_say_interrupted' ORDER BY id"
        ).fetchall()
    assert [r[0] for r in rows] == ["resent"]


async def test_remove_drops_only_the_interrupted_say(client, ui, world):  # noqa: F811
    w = world
    sid, first = await _interrupted(client, ui, w)
    second = (await _say(ui, sid, "вторая"))["id"]
    assert (await _delete(ui, sid, second, expect=409))["detail"] == "реплика не прервана"
    other = await _mk(ui, w)
    await _delete(ui, other, first, expect=404)
    await _delete(ui, sid, 999_999, expect=404)
    # Хосту ручки владельца закрыты.
    r = await client.delete(
        f"{API}/sessions/{sid}/says/{first}", headers={"Authorization": f"Bearer {w['host']}"}
    )
    assert r.status_code == 403

    out = await _delete(ui, sid, first)
    assert (out["id"], out["status"]) == (first, "dropped")
    assert (out["session"]["queued_says"], out["session"]["interrupted_says"]) == (1, 0)
    assert await _queue(ui, sid) == [(second, "queued", "next")]
    last = (await _events(ui, sid))[-1]
    assert (last["kind"], last["ref"], last["summary"]) == ("say", str(first), held_work.NOTE_SAY_REMOVED)
    await _delete(ui, sid, first, expect=409)
    await ui.post(f"{API}/sessions/{sid}/says/{first}/resend", expect=409)


async def test_interrupted_say_survives_sleep_and_ends_with_close(client, ui, world):  # noqa: F811
    w = world
    sid, first = await _interrupted(client, ui, w)
    second = (await _say(ui, sid, "вторая"))["id"]
    out = await ui.post(f"{API}/sessions/{sid}/sleep")
    assert out["dropped_says"] == 1  # обычная снята, прерванная ждёт
    await _turn(client, w, sid, {"state": "asleep"})
    assert await _queue(ui, sid) == [(first, "interrupted", "next")]
    notes = [e["summary"] for e in await _events(ui, sid) if e["kind"] == "say" and e["ref"] == str(second)]
    assert notes[-1] == held_work.NOTE_SLEEP_DROP
    # Спящую сессию прерванная реплика сама не поднимает; «Поднять» (10.10) поднимает спящую всегда —
    # под постоянную строку, прерванная реплика остаётся ждать решения владельца.
    await ui.post(f"{API}/sessions/{sid}/wake")
    assert await _queue(ui, sid) == [(first, "interrupted", "next")]
    # Закрытие снимает и её; после него решать нечего.
    await ui.post(f"{API}/sessions/{sid}/close")
    assert (await _get(ui, sid))["says"] == []
    await ui.post(f"{API}/sessions/{sid}/says/{first}/resend", expect=409)
    await _delete(ui, sid, first, expect=409)


async def test_urgent_passes_the_interrupted_plain_waits(client, ui, db, world):  # noqa: F811
    """Прерванная держит несрочные реплики за собой; срочная `now` проходит — правило ядра то же, что у
    хоста."""
    w = world
    sid, first = await _interrupted(client, ui, w)

    def _feedable() -> int:
        with db.reader() as conn:
            return held_work.feedable_says(conn, held.get(conn, sid))

    assert _feedable() == 0
    await _say(ui, sid, "обычная")
    assert _feedable() == 0
    await ui.post(f"{API}/sessions/{sid}/wake", expect=409)
    urgent = (await _say(ui, sid, "срочная", "now"))["id"]
    assert _feedable() == 1
    await ui.post(f"{API}/sessions/{sid}/wake")  # срочная — повод поднять
    await _host(client, w, f"session-says/{urgent}/delivered", {})
    assert _feedable() == 0
    await _delete(ui, sid, first)
    assert _feedable() == 1  # прерванной нет — обычная снова подаётся


# --- B: причина ожидания «ждёт конца сжатия» ----------------------------------------------------------------


async def test_wait_reason_compact(client, ui, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    row = (await _turn(client, w, sid, {"state": "idle", "wait_reason": "compact"}))["session"]
    assert row["wait_reason"] == "compact"
    assert (await _get(ui, sid))["session"]["wait_reason"] == "compact"
    row = (await _turn(client, w, sid, {"state": "running"}))["session"]
    assert row["wait_reason"] is None  # начало хода снимает причину
    await _turn(client, w, sid, {"state": "idle", "wait_reason": "squeeze"}, expect=422)


# --- D: порог и выключатель сессии --------------------------------------------------------------------------


async def test_compact_settings_patch(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    row = (await _get(ui, sid))["session"]
    assert (row["compact_tokens"], row["compact_auto"], row["compact_at"]) == (None, True, None)
    seq0 = _rec(db, sid).seq

    row = (await _patch(ui, sid, {"compact_tokens": 120_000}))["session"]
    assert (row["compact_tokens"], row["compact_auto"]) == (120_000, True)
    seq1 = _rec(db, sid).seq
    assert seq1 > seq0
    last = (await _events(ui, sid))[-1]
    assert last["kind"] == "status"
    assert last["brief"] == {"type": "compact_settings", "compact_tokens": 120_000, "compact_auto": True}
    # То же значение — ничего не пишется.
    await _patch(ui, sid, {"compact_tokens": 120_000, "compact_auto": True})
    assert _rec(db, sid).seq == seq1
    row = (await _patch(ui, sid, {"compact_auto": False}))["session"]
    assert (row["compact_tokens"], row["compact_auto"]) == (120_000, False)
    # `null` порога — снова умолчание хоста; выключатель не трогается. Форма хоста несёт то же.
    row = (await _patch(ui, sid, {"compact_tokens": None}))["session"]
    assert (row["compact_tokens"], row["compact_auto"]) == (None, False)
    rows = {r["id"]: r for r in (await _host(client, w, "sessions"))["items"]}
    assert (rows[sid]["compact_tokens"], rows[sid]["compact_auto"]) == (None, False)
    # Границы 50 000 – 900 000 включительно.
    for good in (held.COMPACT_TOKENS_MIN, held.COMPACT_TOKENS_MAX):
        assert (await _patch(ui, sid, {"compact_tokens": good}))["session"]["compact_tokens"] == good
    seq2 = _rec(db, sid).seq
    for bad in (
        {"compact_tokens": held.COMPACT_TOKENS_MIN - 1},
        {"compact_tokens": held.COMPACT_TOKENS_MAX + 1},
        {"compact_tokens": "200000"},
        {"compact_tokens": 200_000.5},
        {"compact_auto": None},
        {"compact_auto": 1},
    ):
        await _patch(ui, sid, bad, expect=422)
    assert _rec(db, sid).seq == seq2
    with db.reader() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM main.audit WHERE action = 'hub_session_compact_settings'"
        ).fetchone()[0]
    assert n == 5
    # Хосту настройка закрыта; закрытая сессия — 409.
    r = await client.patch(
        f"{API}/sessions/{sid}", json={"compact_auto": True}, headers={"Authorization": f"Bearer {w['host']}"}
    )
    assert r.status_code == 403
    await ui.post(f"{API}/sessions/{sid}/close")
    await _patch(ui, sid, {"compact_auto": True}, expect=409)


async def test_compact_at_from_host(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    seq0 = _rec(db, sid).seq
    row = (await _turn(client, w, sid, {"state": "idle", "compact_at": 210_000}))["session"]
    assert row["compact_at"] == 210_000
    assert (await _get(ui, sid))["session"]["compact_at"] == 210_000
    seq1 = _rec(db, sid).seq
    assert seq1 > seq0
    await _turn(client, w, sid, {"state": "idle", "compact_at": 210_000})  # то же — `seq` на месте
    await _turn(client, w, sid, {"state": "idle"})  # ключа нет — не менять
    assert _rec(db, sid).seq == seq1 and _rec(db, sid).compact_at == 210_000
    row = (await _turn(client, w, sid, {"state": "idle", "compact_at": None}))["session"]
    assert row["compact_at"] is None
    await _turn(client, w, sid, {"state": "idle", "compact_at": -1}, expect=422)


async def test_role_defaults_at_create(client, ui, db, world, monkeypatch):  # noqa: F811
    w = world
    plain = (await _get(ui, await _mk(ui, w, role="программист")))["session"]
    assert (plain["compact_tokens"], plain["compact_auto"]) == (None, True)  # в ролях значения не заданы

    known = roles.find("программист")
    tuned = dataclasses.replace(known, compact_tokens=300_000, compact_auto=False)
    monkeypatch.setattr(roles, "find", lambda text: tuned if text == "тестер" else None)
    row = (await _get(ui, await _mk(ui, w, role="тестер")))["session"]
    assert (row["compact_tokens"], row["compact_auto"]) == (300_000, False)
    row = (await _get(ui, await _mk(ui, w, role="кто-то")))["session"]
    assert (row["compact_tokens"], row["compact_auto"]) == (None, True)


async def test_roles_list_carries_compact_fields(ui):  # noqa: F811
    items = (await ui.get(f"{API}/roles")).json()
    assert items and all(
        (i["compact_tokens"], i["compact_auto"]) == (None, None) for i in items
    )  # место для настроек есть, значений в ролях нет
    one = (await ui.get(f"{API}/roles/programmer")).json()
    assert (one["compact_tokens"], one["compact_auto"]) == (None, None)


@pytest.mark.parametrize(
    ("head", "want"),
    [
        ("; compact_tokens: 300000; compact_auto: off", (300_000, False)),
        ("; compact_auto: on", (None, True)),
        ("; compact_tokens: 50000", (50_000, None)),
        ("", (None, None)),
    ],
)
def test_role_header_compact_fields(head, want):
    role = roles._parse(f"<!-- key: x; names: икс; title: Икс; sub: direct{head} -->\nтекст роли\n")
    assert (role.compact_tokens, role.compact_auto) == want and role.text == "текст роли"


@pytest.mark.parametrize("head", ["compact_tokens: 49999", "compact_tokens: много", "compact_auto: да"])
def test_role_header_compact_fields_refused(head):
    with pytest.raises(ValueError, match="роли"):
        roles._parse(f"<!-- key: x; names: икс; {head} -->\nтекст\n")


# --- миграции -----------------------------------------------------------------------------------------------


def test_migrations_0067_0068_on_previous_version(tmp_path, monkeypatch):
    from hub.core import db as db_module
    from hub.core.db import Db

    def _cols(conn, schema, table) -> set:
        return {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")}

    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 66}
    )
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            assert not {"compact_tokens", "compact_auto", "compact_at"} & _cols(conn, "main", "hub_session")
            for schema in ("main", "work"):
                assert not {"interrupted_ts", "resent_ts"} & _cols(conn, schema, "held_say")
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 68
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] >= 68
            assert {"compact_tokens", "compact_auto", "compact_at", "wait_reason"} <= _cols(
                conn, "main", "hub_session"
            )
            # Отметка прерванной реплики — в обеих базах (0068 общий).
            for schema in ("main", "work"):
                assert {"interrupted_ts", "resent_ts"} <= _cols(conn, schema, "held_say")
    finally:
        new.close()
