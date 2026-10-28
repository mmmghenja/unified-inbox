"""Ф68-1 (docs/architecture.md): `GET /owner_channel/{ref}/session` — роль, файл
роли и хвост ленты сессии хаба, чью карточку решает агент владельца.

- карточка сессии хаба — сессия, файл роли, последние события, последний ответ модели; `before`/`more_before`;
- карточка без сессии хаба (черновик внешней сессии) — `{"session": null}`;
- файл роли: нет поля — `null`, нет файла — `{path, error}`, длинный — 4000 знаков и `truncated`;
- общий потолок 12 000 знаков: укорачиваются строки старых событий; маскировка секретов;
- допуск: кука и агент владельца (карточка «в канале») — да; прочий bearer и бот — 403; закрытая, чужая — 404.

Мир и помощники — test_hub_sessions.py, test_f56_queue.py; хост поддельный."""

from pathlib import Path

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto, _send
from .test_f56_queue import _agent, _answer, _bot, _channel_on, _patch_session, _permission, _queue, _refs
from .test_hub_sessions import A, _h, _host, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
ROLE_PATH = "docs/ROLE.md"


async def _ctx(client, ref, *, token=None, ui=None, expect=200, **params) -> dict:
    if ui is not None:
        r = await ui.get(f"{API}/owner_channel/{ref}/session", params=params or None)
    else:
        r = await client.get(f"{API}/owner_channel/{ref}/session", params=params, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _events(client, w, sid, events: list[dict]) -> None:
    await _host(client, w, f"sessions/{sid}/events", {"events": events})


def _role(w, text: str) -> None:
    target = Path(w["dir3"]) / ROLE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


async def _card(client, ui, w) -> tuple[int, str]:
    """Сессия хаба в канале владельца и её карточка разрешения."""
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    return sid, f"req:{await _permission(client, w, sid)}"


async def test_card_session_role_events_and_last_answer(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    _role(w, "# Роль\n\nПрограммист проекта: пушит в свои ветки.\n")
    await _patch_session(ui, sid, {"role_file": ROLE_PATH})
    await _events(
        client,
        w,
        sid,
        [
            {"kind": "say", "summary": "владелец (next): собери ветку f1"},
            {"kind": "tool", "tool": "Bash", "summary": "git status"},
            {"kind": "tool_done", "tool": "Bash", "summary": "готово (12 симв.)"},
            {"kind": "text", "summary": "Собрал ветку f1, пушу.\nНужен push."},
            {"kind": "tool", "tool": "Bash", "summary": "git push origin f1"},
        ],
    )
    body = await _ctx(client, ref, token=agent)
    assert body["session"] == {
        "id": sid,
        "project_id": w["p3"],
        "role": base.ROLE,
        "number": body["session"]["number"],
        "mode": "auto",  # Ф66-1: карточки ручной сессии — всегда владельцу, в канал идёт сессия в авто
        "engine": "claude",
        "title": body["session"]["title"],
    }
    assert body["role_file"] == {
        "path": ROLE_PATH,
        "text": "# Роль\n\nПрограммист проекта: пушит в свои ветки.\n",
        "truncated": False,
    }
    events = body["events"]
    assert events == sorted(events, key=lambda e: e["id"]) and len(events) <= 12
    assert set(events[0]) >= {"id", "ts", "kind", "brief"}
    briefs = [e["brief"] for e in events]
    assert "Bash: git push origin f1" in briefs and "Bash: git status" in briefs
    answer = next(e for e in events if e["kind"] == "text")
    assert answer["text"] == "Собрал ветку f1, пушу.\nНужен push."
    assert [e for e in events if "text" in e] == [answer]  # текст — только у последнего ответа
    assert body["last_answer"] is None and body["more_before"] is None
    assert all("detail" not in e and "summary" not in e for e in events)
    # Кука интерфейса — то же самое (для отладки).
    assert (await _ctx(client, ref, ui=ui))["events"] == events


async def test_before_more_before_and_last_answer_outside_window(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    await _events(client, w, sid, [{"kind": "text", "summary": "старый ответ"}])
    reads = [{"kind": "tool", "tool": "Read", "summary": f"f{i}.py"} for i in range(30)]
    await _events(client, w, sid, reads)
    first = await _ctx(client, ref, token=agent, limit=5)
    assert len(first["events"]) == 5 and first["events"][-1]["brief"] == "Read: f29.py"
    assert first["more_before"] == first["events"][0]["id"]
    # Последний ответ раньше окна — отдельным полем.
    assert first["last_answer"]["text"] == "старый ответ"
    page = await _ctx(client, ref, token=agent, limit=5, before=first["more_before"])
    assert [e["brief"] for e in page["events"]] == [f"Read: f{i}.py" for i in range(20, 25)]
    assert page["last_answer"] is None and page["events"][-1]["id"] < first["events"][0]["id"]
    # Листаем до начала ленты: на последней странице `more_before` — `null`.
    cursor, seen = page["more_before"], 0
    while cursor is not None:
        page = await _ctx(client, ref, token=agent, limit=40, before=cursor)
        cursor, seen = page["more_before"], seen + 1
    assert seen >= 1 and any(e["kind"] == "text" for e in page["events"])
    assert all("text" not in e for e in page["events"])  # текст ответа — только на странице с конца
    await _ctx(client, ref, token=agent, limit=41, expect=422)


async def test_role_file_none_missing_long(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    assert (await _ctx(client, ref, token=agent))["role_file"] is None
    await _patch_session(ui, sid, {"role_file": ROLE_PATH})
    missing = (await _ctx(client, ref, token=agent))["role_file"]
    assert missing["path"] == ROLE_PATH and missing["error"] and "text" not in missing
    _role(w, "я" * 5000)
    long = (await _ctx(client, ref, token=agent))["role_file"]
    assert long["truncated"] is True and len(long["text"]) == 4000 and long["text"].endswith("…")


async def test_total_cap_shortens_old_briefs_and_masks(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    _role(w, "р" * 1000)
    await _patch_session(ui, sid, {"role_file": ROLE_PATH})
    await _events(client, w, sid, [{"kind": "status", "summary": f"{i:02d}" + "с" * 998} for i in range(40)])
    await _events(
        client,
        w,
        sid,
        [
            {"kind": "text", "summary": "о" * 5000},
            {"kind": "tool", "tool": "Bash", "summary": "export API_KEY=sk-abc123 && deploy"},
        ],
    )
    body = await _ctx(client, ref, token=agent, limit=40)
    events = body["events"]
    total = sum(len(e["brief"]) + len(e.get("text") or "") for e in events) + len(body["role_file"]["text"])
    assert total <= 12_000
    assert len(events[-3]["brief"]) == 600  # новые строки целы
    assert len(events[0]["brief"]) < 600  # старые укорочены первыми
    assert all(len(e["brief"]) >= 80 for e in events if e["kind"] == "status")
    assert len(events[-2]["text"]) == 4000
    assert "sk-abc123" not in events[-1]["brief"] and "API_KEY=***" in events[-1]["brief"]


async def test_card_without_hub_session_is_null(client, ui, db, world):
    w = world
    await _sub(client, w, A, role="внешний")
    outbox_id = (await _send(client, w, w["ch"], {"session": base._sess(A)}))["outbox_id"]
    ref = f"draft:{outbox_id}"
    assert _refs(await _queue(client, ui=ui))[ref]["session_id"] is None
    assert await _ctx(client, ref, ui=ui) == {"session": None}


async def test_access_and_closed(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    only4, _ = _agent(db, project_ids=[w["p4"]])
    bot, _ = _bot(db)
    sid = await _auto(client, ui, w, mode="manual")
    ref = f"req:{await _permission(client, w, sid)}"
    # Не в канале — агенту 403 `not_in_channel`, кука читает.
    r = await client.get(f"{API}/owner_channel/{ref}/session", headers=_h(agent))
    assert r.status_code == 403 and r.json()["detail"] == "not_in_channel"
    assert (await _ctx(client, ref, ui=ui))["session"]["id"] == sid
    await _channel_on(ui, w, sid)
    assert (await _ctx(client, ref, token=agent))["session"]["id"] == sid
    for token in (bot, w["tok"]):
        await _ctx(client, ref, token=token, expect=403)
    await _ctx(client, ref, token=only4, expect=404)  # чужой проект — как нет карточки
    await _ctx(client, "req:999999", token=agent, expect=404)
    await _ctx(client, "nope:1", token=agent, expect=404)
    await _answer(client, ref, {"action": "allow"}, token=agent)
    await _ctx(client, ref, token=agent, expect=404)  # закрытая
