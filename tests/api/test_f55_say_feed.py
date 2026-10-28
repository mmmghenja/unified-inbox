"""Ф55-1 на уровне ядра (docs/architecture.md): событие ленты `say` пишется, когда
реплика доставлена, а не при отправке; ход, начатый пачкой реплик, возвращает прерванными все
(`interrupted_says`).

Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

from .test_f41_held_work import (
    API,
    _events,
    _get,
    _host,
    _mk,
    _rec,
    _run,
    _say,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)
from .test_f48_2_core import _turn


def _says(events: list[dict]) -> list[tuple[str, str]]:
    return [(e["summary"], e["ref"]) for e in events if e["kind"] == "say"]


async def test_say_event_is_written_on_delivery_not_on_send(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    seq0 = _rec(db, sid).seq
    first = (await _say(ui, sid, "первая"))["id"]
    second = (await _say(ui, sid, "вторая", "now"))["id"]
    # Отправка: реплики в очереди, `seq` сессии сдвинулся (интерфейс и хост узнают), события в ленте нет.
    rec = _rec(db, sid)
    assert rec.seq > seq0 and rec.last_event_id is None
    assert await _events(ui, sid) == []
    assert [s["id"] for s in (await _get(ui, sid))["says"]] == [first, second]

    await _run(client, w, sid, state="idle")
    seq1 = _rec(db, sid).seq
    await _host(client, w, f"session-says/{second}/delivered", {})
    events = await _events(ui, sid)
    assert _says(events) == [("владелец (now): вторая", str(second))]  # в порядке доставки, не отправки
    rec = _rec(db, sid)
    assert rec.seq > seq1 and rec.last_event_id == events[-1]["id"]
    # Повтор отметки события не двоит.
    await _host(client, w, f"session-says/{second}/delivered", {}, expect=409)
    await _host(client, w, f"session-says/{first}/delivered", {})
    assert _says(await _events(ui, sid)) == [
        ("владелец (now): вторая", str(second)),
        ("владелец (next): первая", str(first)),
    ]


async def test_new_say_still_resets_failed_launches(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _say(ui, sid, "первая")
    for _ in range(2):
        await _turn(client, w, sid, {"state": "asleep", "failure": "нет входа"})
    assert _rec(db, sid).launch_fails == 2
    await _say(ui, sid, "вторая")
    rec = _rec(db, sid)
    assert (rec.launch_fails, rec.failure) == (0, None)  # 2.5: сброс счётчика остался при отправке


async def test_dropped_say_leaves_only_the_service_event(client, ui, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    say_id = (await _say(ui, sid, "не дойдёт"))["id"]
    await ui.post(f"{API}/sessions/{sid}/close")
    events = await _events(ui, sid)
    # Служебное событие «снята» — как раньше; текста недоставленной реплики в ленте нет.
    assert [e["ref"] for e in events if e["kind"] == "say"] == [str(say_id)]
    assert not any("не дойдёт" in e["summary"] for e in events)


async def test_turn_of_a_batch_returns_every_say_interrupted(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    a = (await _say(ui, sid, "раз"))["id"]
    b = (await _say(ui, sid, "два"))["id"]
    c = (await _say(ui, sid, "три"))["id"]
    for say_id in (a, b):
        await _host(client, w, f"session-says/{say_id}/delivered", {})
    await _turn(client, w, sid, {"state": "running"})

    out = await _turn(client, w, sid, {"state": "idle", "interrupted_says": [a, b, 999_999]})
    assert out["interrupted_says"] == [a, b] and out["interrupted_say"] is None
    full = await _get(ui, sid)
    assert [(s["id"], s["status"]) for s in full["says"]] == [
        (a, "interrupted"),
        (b, "interrupted"),
        (c, "queued"),
    ]
    assert (full["session"]["queued_says"], full["session"]["interrupted_says"]) == (3, 2)
    events = await _events(ui, sid)
    cut = [e["ref"] for e in events if e["kind"] == "say" and "прервана" in e["summary"]]
    assert cut == [str(a), str(b)] and _rec(db, sid).last_event_id == events[-1]["id"]
    # Повтор того же конца хода ничего не меняет; запоздалая отметка «доставлено» прерванной — отказ.
    again = await _turn(client, w, sid, {"state": "idle", "interrupted_says": [a, b]})
    assert again["interrupted_says"] == [a, b] and len(await _events(ui, sid)) == len(events)
    await _host(client, w, f"session-says/{a}/delivered", {}, expect=409)

    # «Отправить снова» и новая доставка — реплика в ленте ещё раз (сессия получила её второй раз).
    await ui.post(f"{API}/sessions/{sid}/says/{a}/resend")
    await _host(client, w, f"session-says/{a}/delivered", {})
    assert [s for s, ref in _says(await _events(ui, sid)) if ref == str(a)].count("владелец (next): раз") == 2


async def test_interrupted_says_is_validated(client, world, ui):  # noqa: F811
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    await _turn(client, w, sid, {"state": "idle", "interrupted_says": ["1"]}, expect=422)
    await _turn(client, w, sid, {"state": "idle", "interrupted_says": [0]}, expect=422)
    out = await _turn(client, w, sid, {"state": "idle", "interrupted_says": []})
    assert out["interrupted_says"] == []
