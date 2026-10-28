"""Ф41-4а (docs/architecture.md + П9, П10; раздел 6 «MCP»): MCP-сервер хаба внутри
сессии хаба (`HUB_SESSION`) сам оформляет её подписку по желаемому фильтру записи — без вызова инструмента.

Ядро — настоящее приложение в процессе (как в остальных тестах hub-mcp); подписку проверяем по базе."""

import pytest

from hub.core import held, hub_sessions, subs
from hub.mcp_server import server as mcp_server

from .conftest import insert_project, make_token, write_profile

ROLE = "маркетолог"
WANT = {"mode": "messages", "msg_filter": "personal", "keywords": ["договор"], "exclude": ["спам"]}


@pytest.fixture
def world(db, tmp_path, clock):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, project, True, now_ms=0))
    token, token_id = make_token(db, scopes=["read", "draft"], name="mcp:all")
    return {"project": project, "workdir": str(workdir), "token": token, "token_id": token_id}


def _session(db, w, *, want=WANT, role=ROLE) -> int:
    audit = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r"}
    rec = db.write_sync(
        lambda conn: hub_sessions.create(
            conn, project_id=w["project"], engine="claude", role=role, now_ms=1, audit=audit, want_sub=want
        )
    )
    held.current().add(rec.id)
    return rec.id


def _hub(make_server, config_dir, w, hub_id, monkeypatch, *, profile="all", env=None):
    write_profile(config_dir, profile, w["token"], w["token_id"])
    _server, hub = make_server(profile)
    hub.environ = {"HUB_SESSION": str(hub_id), **(env or {})}
    monkeypatch.chdir(w["workdir"])

    async def no_sleep(_s):
        return None

    hub.sleep = no_sleep
    return hub


def _subs(db) -> list[subs.SubRec]:
    with db.reader() as conn:
        rows = conn.execute("SELECT id FROM main.session_sub ORDER BY id").fetchall()
        return [subs.get_sub(conn, row[0]) for row in rows]


async def test_start_subscribes_by_want_sub_without_tool_call(
    db, world, make_server, config_dir, public_app, monkeypatch, capsys
):
    hub_id = _session(db, world)
    hub = _hub(make_server, config_dir, world, hub_id, monkeypatch)
    assert await hub.auto_subscribe() == "subscribed"
    (rec,) = _subs(db)
    assert (rec.session_pid, rec.session_proc_start, rec.session_id) == held.triple(hub_id)
    assert rec.api_token_id == world["token_id"]  # подписка — токена профиля самой сессии
    assert (rec.mode, rec.msg_filter, rec.role, rec.role_no) == ("messages", "personal", ROLE, 1)
    assert rec.keywords_json is not None and "договор" in rec.keywords_json and rec.accounts_json is None
    err = capsys.readouterr().err
    assert err.strip() == f"hub mcp: подписка сессии хаба #{hub_id} — оформлена"
    assert "договор" not in err  # в stderr — без текстов

    # Повторный старт (второй подъём процесса, второй сервер хаба) — второй подписки нет, запросов записи нет.
    assert await hub.auto_subscribe() == "kept"
    assert [r.id for r in _subs(db)] == [rec.id]
    assert capsys.readouterr().err == ""


async def test_changed_want_sub_updates_live_sub_in_place(
    db, world, make_server, config_dir, public_app, monkeypatch
):
    hub_id = _session(db, world)
    hub = _hub(make_server, config_dir, world, hub_id, monkeypatch)
    assert await hub.auto_subscribe() == "subscribed"
    audit = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r2"}
    for want, expected in (
        ({"mode": "messages", "msg_filter": "personal"}, ("messages", "personal", 0, None)),
        ({"mode": "cards"}, ("cards", "all", 0, None)),
        ({"mode": "direct"}, ("cards", "all", 1, None)),
    ):
        db.write_sync(
            lambda conn, want=want: hub_sessions.set_want(conn, hub_id, want, now_ms=2, audit=audit)
        )
        assert await hub.auto_subscribe() == "subscribed"
        (rec,) = [r for r in _subs(db) if r.ended_ts is None]
        assert (rec.mode, rec.msg_filter, rec.direct, rec.keywords_json) == expected
        assert await hub.auto_subscribe() == "kept"
    assert len(_subs(db)) == 1  # настройки меняются на месте — та же подписка


async def test_no_want_sub_does_nothing_and_keeps_own_sub(
    db, world, make_server, config_dir, public_app, monkeypatch, capsys
):
    hub_id = _session(db, world, want=None)
    hub = _hub(make_server, config_dir, world, hub_id, monkeypatch)
    assert await hub.auto_subscribe() == "none"
    assert _subs(db) == [] and capsys.readouterr().err == ""
    # Сессия подписалась сама (как в 41-2б) — сервер её подписку не снимает и не трогает.
    await hub.subscribe(None, mode="direct")
    assert await hub.auto_subscribe() == "none"
    (rec,) = _subs(db)
    assert rec.ended_ts is None and rec.direct == 1


async def test_refusal_does_not_break_server(
    db, world, make_server, config_dir, public_app, monkeypatch, capsys
):
    hub_id = _session(db, world)
    db.write_sync(lambda conn: subs.set_project_subs(conn, world["project"], False, now_ms=3))
    hub = _hub(make_server, config_dir, world, hub_id, monkeypatch)
    assert await hub.auto_subscribe() == "refused:403"
    assert _subs(db) == []
    assert capsys.readouterr().err.strip() == f"hub mcp: подписка сессии хаба #{hub_id} — refused:403"
    # Сервер жив: инструмент отвечает, вызов `hub_subscribe` моделью покажет причину словами.
    with pytest.raises(mcp_server.ToolError, match="подписки в проекте выключены"):
        await hub.subscribe(None)


async def test_core_down_retries_then_gives_up(db, world, make_server, config_dir, monkeypatch, capsys):
    import httpx

    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("нет ядра")

    write_profile(config_dir, "all", world["token"], world["token_id"])
    _server, hub = make_server("all", transport=httpx.MockTransport(handler))
    hub.environ = {"HUB_SESSION": "5"}
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    hub.sleep = sleep
    assert await hub.auto_subscribe(tries=3, retry_s=0.5) == "error"
    assert len(calls) == 3 and slept == [0.5, 0.5]
    assert "ядро не ответило" in capsys.readouterr().err


async def test_only_named_profile_subscribes(db, world, make_server, config_dir, public_app, monkeypatch):
    """П10: хост называет профиль (`HUB_SESSION_SUB_PROFILE`) — второй сервер хаба подписку не оформляет."""
    hub_id = _session(db, world)
    other_token, other_id = make_token(db, scopes=["read", "draft"], name="mcp:proj")
    env = {"HUB_SESSION_SUB_PROFILE": "all"}
    other = _hub(
        make_server,
        config_dir,
        {**world, "token": other_token, "token_id": other_id},
        hub_id,
        monkeypatch,
        profile="proj",
        env=env,
    )
    assert await other.auto_subscribe() == "other_profile"
    assert _subs(db) == []
    own = _hub(make_server, config_dir, world, hub_id, monkeypatch, env=env)
    assert await own.auto_subscribe() == "subscribed"
    assert [r.api_token_id for r in _subs(db)] == [world["token_id"]]
    # Переменной нет — оформляет любой (первый увидевший `sub == null`); второй видит подписку и молчит.
    assert await other.auto_subscribe() == "other_profile"


async def test_without_hub_session_nothing_changes(db, world, make_server, config_dir, public_app):
    """Внешняя сессия (нет `HUB_SESSION`): сервер к ядру не ходит и ничего не оформляет."""
    write_profile(config_dir, "all", world["token"], world["token_id"])
    _server, hub = make_server("all")
    hub.environ = {}

    async def boom(*_args, **_kwargs):
        raise AssertionError("без HUB_SESSION запросов к ядру быть не должно")

    hub.client.request = boom
    assert await hub.auto_subscribe() == "not_hub"
    hub.environ = {"HUB_SESSION": "7", "HUB_WORK": "1"}
    assert await hub.auto_subscribe() == "bad_env"
    assert _subs(db) == []


def test_want_sub_body_names_everything_explicitly():
    body = mcp_server.want_sub_body
    assert body({"mode": "direct", "accounts": {"only": [1]}}) == {"mode": "direct"}
    assert body({"mode": "cards", "accounts": {"only": [3, 1]}}) == {"mode": "cards", "accounts": [1, 3]}
    assert body({"mode": "messages", "msg_filter": "all", "keywords": ["x"]}) == {
        "mode": "messages",
        "msg_filter": "all",
        "accounts": [],
    }
    assert body(
        {
            "mode": "messages",
            "msg_filter": "personal",
            "keywords": ["цена"],
            "exclude": [],
            "batch_min": 0,
            "accounts": {"except": [8]},
        }
    ) == {
        "mode": "messages",
        "msg_filter": "personal",
        "keywords": ["цена"],
        "exclude": [],
        "batch_min": 0,
        "accounts_except": [8],
    }
    # «Лично мне» без ключей — `keywords: []`: новая подписка ключи прежней линии не наследует.
    assert body({"mode": "messages", "msg_filter": "personal", "keywords": [], "batch_min": 5}) == {
        "mode": "messages",
        "msg_filter": "personal",
        "keywords": [],
        "accounts": [],
    }
