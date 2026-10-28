"""Ф78-1 (docs/architecture.md, A5): ручек задач по «Го» нет; хостовые ручки сессий
хаба остаются и не несут ключей задач. Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

import pytest

from .test_engine import _admin, admin_client  # noqa: F401 — фикстура `admin_client`
from .test_f41_held_work import API, _bearer, _host, _mk, ui, world  # noqa: F401 — фикстуры `ui`, `world`

TASK_KEYS = {"tasks", "requests", "says"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/work/tasks"),
        ("post", "/work/tasks"),
        ("get", "/work/tasks/1"),
        ("get", "/work/tasks/1/events"),
        ("post", "/work/tasks/1/go"),
        ("post", "/work/tasks/1/say"),
        ("post", "/work/propose"),
        ("post", "/work/requests/1/decide"),
    ],
)
async def test_ui_task_routes_are_gone(ui, method, path):  # noqa: F811
    if method == "get":
        r = await ui.get(f"{API}{path}")
    else:
        r = await ui.c.post(f"{API}{path}", json={}, headers=ui.headers)
    assert r.status_code in (404, 405), (path, r.status_code)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/work/host/claim"),
        ("get", "/work/host/tasks/1"),
        ("post", "/work/tasks/1/unclaim"),
        ("patch", "/work/tasks/1"),
        ("post", "/work/tasks/1/events"),
        ("post", "/work/tasks/1/requests"),
        ("post", "/work/requests/1/expire"),
        ("post", "/work/says/1/delivered"),
    ],
)
async def test_host_task_routes_are_gone(client, world, method, path):  # noqa: F811
    r = await client.request(method.upper(), f"{API}{path}", json={}, headers=_bearer(world["host"]))
    assert r.status_code in (404, 405), (path, r.status_code)


async def test_admin_ask_route_is_gone(admin_client):  # noqa: F811
    r = await admin_client.post("/internal/v1/admin/work/tasks/1/ask", json={"text": "x"}, headers=_admin())
    assert r.status_code in (404, 405)


async def test_host_open_wait_recover_keep_sessions_without_task_keys(client, ui, world):  # noqa: F811
    sid = await _mk(ui, world)
    opened = await _host(client, world, "open")
    assert not TASK_KEYS & set(opened)
    assert [s["id"] for s in opened["sessions"]["items"]] == [sid]
    waited = await _host(client, world, "wait", params={"since": 0, "timeout_s": 0})
    assert not TASK_KEYS & set(waited)
    assert set(waited) >= {"sessions", "max_seq", "engine", "pause_cleared_ts"}
    # Хост прежней версии при старте зовёт `recover` — ручка отвечает и ничего не трогает.
    assert await _host(client, world, "recover", {}) == {"touched": 0}
