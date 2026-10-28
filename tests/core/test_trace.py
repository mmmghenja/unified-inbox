"""Ф97-1 (docs/architecture.md): `hub trace` — ядро, ручка, разбор CLI и печать ленты."""

import asyncio
import json
import time

import httpx
import pytest

from hub import cli as hub_cli
from hub import trace_text
from hub.core import trace
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

T0 = 1_760_000_000_000
MIN = 60_000
ADMIN = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


def _seed(db) -> None:
    def w(conn):
        conn.execute(
            "INSERT INTO main.human_request (project_id, kind, need, status, decided_ts, decided_by,"
            " decided_note,"
            " shot_size, shot_mime, created_ts, seq) VALUES (5, 'captcha', 'Реши капчу', 'done', ?, 'bot',"
            " 'готово', 1234, 'image/png', ?, 1)",
            (T0 + 9 * MIN, T0),
        )
        conn.execute(
            "INSERT INTO main.owner_item (id, ref, kind, project_id, session_id, created_ts, closed_ts, seq)"
            " VALUES (1, 'human:1', 'human', 5, 7, ?, ?, 1)",
            (T0, T0 + 9 * MIN),
        )
        conn.execute(
            "INSERT INTO main.owner_item (id, ref, kind, project_id, session_id, created_ts, payload_json,"
            " seq)"
            " VALUES (2, 'errand:2', 'errand', 5, 7, ?, ?, 2)",
            (T0 + MIN, json.dumps({"target": "human:1", "steps": ["открыл", "кликнул"], "text": "Реши"})),
        )
        talk = [
            (1, T0 + 2 * MIN, "owner_channel", "что там?\nвторая строка", "human:1", None, 0, None),
            (2, T0 + 3 * MIN, "agent", "кликаю", None, 1, 0, None),  # ответ без ref: найдётся по reply_to
            (3, T0 + 4 * MIN, "agent", "какой профиль?", "errand:2", None, 1, None),
            (4, T0 + 5 * MIN, "owner_channel", "hooli", "errand:2", 3, 0, "bot"),
            (5, T0 + 90 * MIN, "owner_channel", "чужое, поздно", "human:99", None, 0, None),
        ]
        for tid, ts, author, text, ref, reply_to, ask, via in talk:
            conn.execute(
                "INSERT INTO main.owner_talk (id, ts, author, text, ref, reply_to, ask, via)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, ts, author, text, ref, reply_to, ask, via),
            )
        conn.execute(
            "INSERT INTO main.talk_file (id, sha256, size, mime, kind, name, created_ts, talk_id)"
            " VALUES ('f1', 'x', 10, 'image/png', 'image', 'shot.png', ?, 1)",
            (T0,),
        )
        conn.execute(
            "INSERT INTO held_say (session_id, text, priority, status, created_ts)"
            " VALUES (7, 'сессии: стоп', 'now',"
            " 'delivered', ?)",
            (T0 + 2 * MIN,),
        )
        conn.execute(
            "INSERT INTO held_say (session_id, text, priority, status, created_ts)"
            " VALUES (7, 'вне окна', 'next',"
            " 'queued', ?)",
            (T0 + 600 * MIN,),
        )
        conn.execute(
            "INSERT INTO held_event (session_id, ts, kind, tool, summary, detail_json) VALUES (7, ?, 'tool',"
            " 'Bash', 'ls', '{\"diff\": \"СЕКРЕТНЫЙ-ДИФФ\"}')",
            (T0 + 3 * MIN,),
        )

    db.write_sync(w)


def _by_ref(db, ref):
    with db.reader() as conn:
        return trace.by_ref(conn, ref)


def test_by_ref_links_both_ways(db) -> None:
    _seed(db)
    for ref in ("human:1", "errand:2"):
        body = _by_ref(db, ref)
        assert body["refs"] == ["errand:2", "human:1"]
        assert [i["ref"] for i in body["items"]] == ["human:1", "errand:2"]
        assert body["items"][1]["payload"]["steps"] == ["открыл", "кликнул"]
        [h] = body["human_request"]
        assert h["decided_note"] == "готово" and h["shot_size"] == 1234 and "shot_sha256" not in h
        assert [t["id"] for t in body["talk"]] == [1, 2, 3, 4]  # 5 — чужая; 2 — ответ по reply_to
        assert body["talk"][0]["files"] == [
            {"name": "shot.png", "mime": "image/png", "size": 10, "purged_ts": None}
        ]
        assert body["talk"][2]["ask"] == 1 and body["talk"][3]["via"] == "bot"
        assert [s["text"] for s in body["held_say"]] == ["сессии: стоп"]  # «вне окна» отсечена
        assert [e["summary"] for e in body["held_event"]] == ["ls"]
        assert body["truncated"] == []


def test_detail_json_does_not_leak(db) -> None:
    _seed(db)
    assert "СЕКРЕТНЫЙ-ДИФФ" not in json.dumps(_by_ref(db, "human:1"), ensure_ascii=False)


def test_unknown_ref_is_none(db) -> None:
    _seed(db)
    assert _by_ref(db, "human:404") is None


def test_around_window_and_project(db) -> None:
    _seed(db)
    with db.reader() as conn:
        body = trace.around(conn, T0 + 3 * MIN, 15)
        assert [t["id"] for t in body["talk"]] == [1, 2, 3, 4]
        assert [i["id"] for i in body["items"]] == [1, 2]
        assert len(body["human_request"]) == 1 and body["held_say"] == [] and body["held_event"] == []
        far = trace.around(conn, T0 + 90 * MIN, 5)
        assert [t["id"] for t in far["talk"]] == [5] and far["items"] == [] and far["human_request"] == []
        assert trace.around(conn, T0, 15, project_id=6)["items"] == []
        assert trace.around(conn, T0, 15, project_id=5)["items"] != []


def test_truncated(db, monkeypatch) -> None:
    _seed(db)
    monkeypatch.setattr(trace, "LIMIT", 2)
    body = _by_ref(db, "human:1")
    assert body["truncated"] == ["talk"] and len(body["talk"]) == 2


def test_route(db) -> None:
    _seed(db)
    app = create_internal_app(
        db, clock=lambda: T0 / 1000, notifier=Notifier(), admin_key_hash=hash_secret(ADMIN)
    )

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t/internal/v1",
            headers={"Authorization": f"Bearer {ADMIN}"},
        ) as c:
            return (
                await c.get("/admin/trace", params={"ref": "human:1"}),
                await c.get("/admin/trace", params={"ref": "nope:1"}),
                await c.get("/admin/trace", params={"around_ms": T0, "min": 5}),
                await c.get("/admin/trace"),
            )

    ok, missing, around, bad = asyncio.run(go())
    assert ok.status_code == 200 and ok.json()["refs"] == ["errand:2", "human:1"]
    assert missing.status_code == 404 and around.status_code == 200 and bad.status_code == 422


def _hhmmss(ts_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000))


def test_render_ribbon(db) -> None:
    _seed(db)
    text = trace_text.render(_by_ref(db, "human:1"))
    print(text)
    lines = text.splitlines()
    assert lines[0] == "связанные ref: errand:2, human:1"
    assert any(f"{_hhmmss(T0)}  заявка  human:1 captcha  Реши капчу" in ln for ln in lines)
    assert any("владелец→агент  владелец [human:1]  что там?" in ln for ln in lines)
    assert "    вторая строка" in lines and "    [файл: shot.png, image/png, 10 Б]" in lines
    assert any("вопрос агента  агент [errand:2]  какой профиль?" in ln for ln in lines)
    assert any("владелец→агент  владелец, из бота [errand:2]  hooli" in ln for ln in lines)
    assert any("сессия #7: реплика владельца" in ln and "сессии: стоп" in ln for ln in lines)
    assert any("сессия #7: событие  tool Bash  ls" in ln for ln in lines)
    assert "        - открыл" in lines and "        - кликнул" in lines  # шаги поручения — по строке
    stamps = [ln[:8] for ln in lines if ln[:2].isdigit()]
    assert stamps == sorted(stamps)


def _fake_admin(monkeypatch, status, body):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), base_url="http://h/internal/v1"),
    )
    return seen


def _run(argv):
    import sys

    old = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old


def test_cli_ref_and_json(monkeypatch, capsys, db) -> None:
    _seed(db)
    body = _by_ref(db, "human:1")
    seen = _fake_admin(monkeypatch, 200, body)
    _run(["trace", "human:1"])
    assert "заявка" in capsys.readouterr().out
    assert dict(seen[0].url.params) == {"ref": "human:1"}
    _run(["trace", "human:1", "--json"])
    assert json.loads(capsys.readouterr().out)["refs"] == ["errand:2", "human:1"]


def test_cli_around_args(monkeypatch, capsys) -> None:
    seen = _fake_admin(monkeypatch, 200, {"items": [], "talk": [], "human_request": [], "truncated": []})
    _run(["trace", "--around", "2026-10-25 12:29", "--min", "7", "--project", "3"])
    params = dict(seen[0].url.params)
    expected = int(time.mktime(time.strptime("2026-10-25 12:29", "%Y-%m-%d %H:%M")) * 1000)
    assert params == {"around_ms": str(expected), "min": "7", "project_id": "3"}


def test_cli_bad_args(monkeypatch) -> None:
    _fake_admin(monkeypatch, 404, {"detail": "x"})
    with pytest.raises(SystemExit):
        _run(["trace"])
    with pytest.raises(SystemExit):
        _run(["trace", "human:1", "--around", "2026-10-25 12:29"])
    with pytest.raises(SystemExit):
        _run(["trace", "--around", "вчера"])
    with pytest.raises(SystemExit) as exc:
        _run(["trace", "human:1"])
    assert "ничего нет" in str(exc.value.code)
