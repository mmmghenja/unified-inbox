"""Ф41-6 (ядро): ручки раскладки доски `GET/PUT /ui/layout` (docs/architecture.md)."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import ui_layout

from .conftest import BASE_URL, csrf_headers, login_via_code, make_bearer_token

URL = "/api/v1/ui/layout"
DEFAULT = {
    "v": 1,
    "list_collapsed": False,
    "columns": 3,
    "wait_open": False,
    "open": [],
    "hidden_projects": [],
    "hidden_lines": [],
    "view": "own",
    "view_project": None,
    "picks": {},
}
MIXED_RESULT = [{"kind": "channel", "id": 5}, {"kind": "agent", "key": "k"}, {"kind": "orch"}]
MIXED_OPEN = [
    {"kind": "session", "id": 0},
    {"kind": "channel", "id": "5"},
    {"kind": "channel"},
    {"kind": "bad"},
    "x",
    {"kind": "channel", "id": 5},
    {"kind": "agent", "key": ""},
    {"kind": "agent", "key": "a" * 201},
    {"kind": "agent", "key": "k", "id": 3},
    {"kind": "orch", "id": 9},
    {"kind": "orch"},
    {"kind": "session", "id": True},
]


@pytest.fixture
async def ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return client, csrf_headers(cookie)


async def _put(ui, layout):
    c, h = ui
    return await c.put(URL, json={"layout": layout}, headers=h)


async def test_none_when_absent(ui):
    r = await ui[0].get(URL)
    assert r.status_code == 200 and r.json() == {"layout": None}


async def test_put_then_get(ui):
    layout = {
        "v": 1,
        "list_collapsed": True,
        "columns": 2,
        "wait_open": True,
        "open": [{"kind": "session", "id": 12}, {"kind": "agent", "key": "3:маркетолог:1"}, {"kind": "orch"}],
        "hidden_projects": [4],
        "hidden_lines": [{"key": "p3:маркетолог:1", "ended_ts": 1_760_000_000_000}],
        "view": "projects",
        "view_project": 3,
        "picks": {"3": {"kind": "session", "id": 12}, "4": {"kind": "agent", "key": "p4:тестер:1"}},
    }
    r = await _put(ui, layout)
    assert r.status_code == 200 and r.json() == {"layout": layout}
    assert (await ui[0].get(URL)).json() == {"layout": layout}


async def test_put_persists_in_meta(ui, db):
    await _put(ui, {"columns": 4})
    with db.reader() as conn:
        raw = conn.execute("SELECT value FROM main.meta WHERE key = 'ui_layout'").fetchone()[0]
    assert json.loads(raw)["columns"] == 4


async def test_put_without_csrf_refused(client, db, clock):
    await login_via_code(client, db, clock)
    r = await client.put(URL, json={"layout": {}})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, DEFAULT),
        ({"columns": 0}, {**DEFAULT, "columns": 1}),
        ({"columns": 9}, {**DEFAULT, "columns": 5}),
        ({"columns": "x"}, DEFAULT),
        ({"columns": True}, DEFAULT),
        ({"list_collapsed": 1, "wait_open": "yes"}, DEFAULT),
        ({"list_collapsed": True, "wait_open": True}, {**DEFAULT, "list_collapsed": True, "wait_open": True}),
        ({"extra": 1, "v": 7}, DEFAULT),
        (
            {"open": [{"kind": "session", "id": 1, "x": 2}, {"kind": "session", "id": 1}]},
            {**DEFAULT, "open": [{"kind": "session", "id": 1}]},
        ),
        (
            {"open": MIXED_OPEN},
            {**DEFAULT, "open": MIXED_RESULT},
        ),
        (
            {"open": [{"kind": "channel", "id": i} for i in range(1, 60)]},
            {**DEFAULT, "open": [{"kind": "channel", "id": i} for i in range(1, 41)]},
        ),
        ({"open": "x", "hidden_projects": "x"}, DEFAULT),
        ({"hidden_projects": [0, 3, 3, "a", True, 2]}, {**DEFAULT, "hidden_projects": [3, 2]}),
        ({"hidden_projects": list(range(1, 60))}, {**DEFAULT, "hidden_projects": list(range(1, 41))}),
        (
            {
                "hidden_lines": [
                    {"key": "a", "ended_ts": 5},
                    {"key": "a", "ended_ts": 9},
                    {"key": "", "ended_ts": 1},
                    {"key": "b", "ended_ts": True},
                    {"key": "c"},
                    "x",
                    {"key": "d", "ended_ts": 7},
                ]
            },
            {**DEFAULT, "hidden_lines": [{"key": "a", "ended_ts": 5}, {"key": "d", "ended_ts": 7}]},
        ),
        (
            {"hidden_lines": [{"key": f"k{i}", "ended_ts": i} for i in range(130)]},
            {**DEFAULT, "hidden_lines": [{"key": f"k{i}", "ended_ts": i} for i in range(30, 130)]},
        ),
        # Ф47-1: вид главной, проект «Одного проекта», выбор сессии в столбце проекта
        ({"view": "projects"}, {**DEFAULT, "view": "projects"}),
        ({"view": "project", "view_project": 3}, {**DEFAULT, "view": "project", "view_project": 3}),
        ({"view": "grid", "view_project": 0}, DEFAULT),
        ({"view": 5, "view_project": "3"}, DEFAULT),
        ({"view_project": True}, DEFAULT),
        ({"view_project": -2}, DEFAULT),
        (
            {
                "picks": {
                    "3": {"kind": "session", "id": 12, "x": 1},
                    "4": {"kind": "agent", "key": "p4:a:1"},
                    "5": {"kind": "channel", "id": 7},
                    "6": {"kind": "orch"},
                    "x": {"kind": "session", "id": 1},
                    "0": {"kind": "session", "id": 1},
                    "7": {"kind": "session", "id": 0},
                    "8": "мусор",
                }
            },
            {
                **DEFAULT,
                "picks": {"3": {"kind": "session", "id": 12}, "4": {"kind": "agent", "key": "p4:a:1"}},
            },
        ),
        ({"picks": [1, 2]}, DEFAULT),
        (
            {"picks": {str(i): {"kind": "session", "id": i} for i in range(1, 60)}},
            {**DEFAULT, "picks": {str(i): {"kind": "session", "id": i} for i in range(1, 41)}},
        ),
    ],
)
async def test_normalization(ui, raw, expected):
    r = await _put(ui, raw)
    assert r.status_code == 200, r.text
    assert r.json() == {"layout": expected}


@pytest.mark.parametrize("bad", [None, [], "x", 5])
async def test_not_object_422(ui, bad):
    assert (await _put(ui, bad)).status_code == 422


async def test_too_large_422(ui):
    keys = [{"kind": "agent", "key": f"{i:03d}" + "я" * 197} for i in range(40)]
    assert (await _put(ui, {"open": keys})).status_code == 422
    assert (await ui[0].get(URL)).json() == {"layout": None}


def test_normalize_raises():
    with pytest.raises(ValueError, match="object"):
        ui_layout.normalize([])


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("scope", ["read", "organize", "draft"])
async def test_bearer_forbidden(app, db, method, scope):
    token = make_bearer_token(db, scopes=(scope,), name=f"t-{scope}")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        r = await c.request(
            method,
            URL,
            json={"layout": {}} if method == "PUT" else None,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 403


async def test_hidden_lines_survive_stale_tab(ui):
    """21.10: «×» у линии «не в сети», сделанный на ноутбуке, не теряется, когда телефон со старой копией
    раскладки (без этой линии) сохраняет своё; у общего ключа остаётся более поздний `ended_ts`."""
    await _put(
        ui, {"hidden_lines": [{"key": "p5:проба:1", "ended_ts": 20}, {"key": "p3:клод:1", "ended_ts": 5}]}
    )
    r = await _put(
        ui,
        {
            "columns": 2,
            "hidden_lines": [{"key": "p3:клод:1", "ended_ts": 3}, {"key": "p1:x:1", "ended_ts": 1}],
        },
    )
    expected = [
        {"key": "p5:проба:1", "ended_ts": 20},
        {"key": "p3:клод:1", "ended_ts": 5},
        {"key": "p1:x:1", "ended_ts": 1},
    ]
    assert r.json()["layout"]["hidden_lines"] == expected
    assert r.json()["layout"]["columns"] == 2
    assert (await ui[0].get(URL)).json()["layout"]["hidden_lines"] == expected
