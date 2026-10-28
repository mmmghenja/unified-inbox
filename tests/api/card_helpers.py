"""Помощники тестов карточек сессий хаба (переехали из test_f49_card_delegate.py: делегат Ф49 убран в
Ф66-1). Таблица маршрута проекта — `_route`; сессия хаба в авто — `_worker`; внешняя подписанная сессия —
`_external`."""

import hashlib

from hub.core import runner

from .test_hub_sessions import ROLE, B, _host, _mk, _sub

API = "/api/v1"
PROC = {"proc_pid": 9191, "proc_start": 555_000}
KEY = "k" * 64
DIGEST = hashlib.sha256(KEY.encode()).hexdigest()
BOSS = "оркестратор"
PUSH = {"kind": "permission", "tool": "Bash", "input": {"command": "git push"}, "reason": "гард"}
QUESTION = {
    "kind": "question",
    "tool": "AskUserQuestion",
    "input": {"questions": [{"question": "Какой вариант?", "options": [{"label": "А"}, {"label": "Б"}]}]},
    "reason": "вопрос",
}


def _now(clock) -> int:
    return int(clock() * 1000)


async def _route(ui, w, changes: dict, *, project="p3", expect=200):
    """Ф66-1: таблица маршрута карточек проекта (`PATCH /projects/{id}` с куки владельца)."""
    r = await ui.c.patch(f"{API}/projects/{w[project]}", json={"card_route": changes}, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r


async def _worker(client, ui, w, *, role=ROLE, mode="auto", project="p3") -> int:
    """Сессия хаба, чьи карточки решают: запущена и в авто."""
    sid = (await _mk(ui, w, role=role, mode=mode, project=project))["id"]
    await _host(client, w, f"sessions/{sid}/state", {"state": "running", **PROC})
    return sid


async def _external(client, ui, w, app, who=B, *, role=BOSS, protocol=7):
    """Внешняя сессия: подписка на проект с ролью, её ожидание «идёт» на протоколе `protocol`."""
    sub_id = (await _sub(client, w, who, role=role))["sub"]["id"]
    mark, _had = app.state.sub_waiters.enter(sub_id, protocol)
    return sub_id, mark


async def _card(client, w, sid, body=PUSH) -> dict:
    return await _host(client, w, f"sessions/{sid}/requests", body)


async def _session(ui, sid) -> dict:
    r = await ui.get(f"{API}/sessions/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def _sweep(db, clock, app) -> None:
    now = _now(clock)
    db.write_sync(
        lambda conn: runner._cleanup_all(conn, now, app.state.sub_waiters, app.state.chan_sub_waiters)
    )
