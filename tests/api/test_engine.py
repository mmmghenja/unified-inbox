"""Ф16-1 (docs/architecture.md, кусок 16-1, поправки П1, П13, П15): глобальный
переключатель движка сессий — `GET/PUT /engine`, админ-сокет и CLI, `engine` в `/status` и в снимке хоста,
подписки обоих движков при любом положении переключателя (П18).

П1 (личность сессии OpenCode — `(pid, proc_start, session_id)`, а не процесс): две сессии `ses_…` на одном
живом pid не вытесняют друг друга — ни в подписке на проект, ни в подписке на беседу.

`/proc` подменяется (`subs.read_proc_start`); рабочая папка проекта — настоящий каталог (ядро сравнивает
`realpath`). Готовность OpenCode (П13) — флаг `engine.OPENCODE_READY` и файл `engine.json` в каталоге конфига:
тесты кладут его в `app.state.config_dir` во временной папке, боевой каталог конфига не трогают.
"""

import asyncio
import json
import sys
import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub import cli as hub_cli
from hub import paths
from hub.core import engine as engine_mod
from hub.core import remote as remote_mod
from hub.core import subs
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.remote import RemoteState

from .conftest import (
    PORT,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_project,
    login_via_code,
    make_bearer_token,
)

API = "/api/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
ORIGIN = f"http://127.0.0.1:{PORT}"
REMOTE_PORT = 8790
REMOTE_HOST = "hub.test.example:8787"
REMOTE_ORIGIN = f"http://{REMOTE_HOST}"
PASSWORD = "correct horse battery"
PID, START = 4242, 777_000
CLAUDE_SID = "0f6f6d6a-9c3d-4d3f-9c9c-2f1a0e5d4c3b"
CLAUDE_SID_2 = "1f6f6d6a-9c3d-4d3f-9c9c-2f1a0e5d4c3c"
OC_A, OC_B = "ses_AAAAAAAAAA1", "ses_BBBBBBBBBB2"


# --- обвязка ---------------------------------------------------------------------------------------------


@pytest.fixture
def procs(monkeypatch):
    """Живые процессы: pid -> starttime."""
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, tmp_path):
    """Два проекта с рабочими папками и включёнными подписками, две беседы, токены сессии, хоста и чтения."""
    dirs = []
    for name in ("umbrella", "initech"):
        folder = tmp_path / name
        folder.mkdir()
        dirs.append(folder)
    first = insert_project(db, name="Umbrella", workdir=str(dirs[0]))
    second = insert_project(db, name="Initech", workdir=str(dirs[1]))
    for project_id in (first, second):
        db.write_sync(lambda conn, pid=project_id: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="mail")
    chan = insert_channel(db, acc, project_id=first, ext_id="t1", kind="mail_thread", title="Клиент")
    other_chan = insert_channel(db, acc, project_id=second, ext_id="t2", kind="mail_thread", title="Задача")
    return {
        "first": first,
        "second": second,
        "dirs": [str(d) for d in dirs],
        "chan": chan,
        "other_chan": other_chan,
        "sess": make_bearer_token(db, kind="interactive", scopes=("read", "draft"), name="mcp:all"),
        "host": make_bearer_token(db, kind="background", scopes=("work_host", "read"), name="work:host"),
        "read": make_bearer_token(db, kind="background", scopes=("read",), name="mcp:read"),
    }


async def _ui(client, db, clock) -> dict:
    cookie, _csrf = await login_via_code(client, db, clock)
    client.cookies.clear()
    return {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


def _session(*, pid=PID, start=START, sid=CLAUDE_SID, cwd, kind="interactive"):
    return {"pid": pid, "proc_start": start, "session_id": sid, "cwd": cwd, "kind": kind}


async def _subscribe(client, world, *, sid=CLAUDE_SID, kind="interactive", project_id=None, cwd=None):
    body = {"project_id": project_id, "session": _session(sid=sid, kind=kind, cwd=cwd or world["dirs"][0])}
    return await client.post(f"{API}/subs", json=body, headers=_bearer(world["sess"]))


async def _chan_subscribe(client, world, channel_id, *, sid=CLAUDE_SID, kind="interactive", cwd=None):
    body = {
        "channel_id": channel_id,
        "session": _session(sid=sid, kind=kind, cwd=cwd or world["dirs"][0]),
    }
    return await client.post(f"{API}/chan-subs", json=body, headers=_bearer(world["sess"]))


async def _switch(client, db, clock, engine: str) -> httpx.Response:
    """Переключить движок UI-сессией (как владелец в панели)."""
    return await client.put(f"{API}/engine", json={"engine": engine}, headers=await _ui(client, db, clock))


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


@pytest.fixture
def oc_ready(monkeypatch, tmp_path, app):
    """OpenCode готов к переключению (П13): флаг куска 16-6 и `engine.json` в каталоге конфига. Каталог —
    во временной папке: и в `app.state.config_dir` (публичное приложение), и в `HUB_CONFIG_DIR` (внутреннее
    приложение и CLI берут его оттуда), боевой каталог конфига тесты не трогают."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / engine_mod.SETUP_FILE).write_text('{"opencode": {"path": "/bin/opencode"}}', "utf-8")
    app.state.config_dir = config_dir
    monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(engine_mod, "OPENCODE_READY", True)
    return config_dir


def _sub_rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, project_id, session_id, ended_ts, end_reason FROM main.session_sub ORDER BY id"
        ).fetchall()


def _chan_rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, channel_id, session_id, ended_ts, end_reason FROM main.session_chan_sub ORDER BY id"
        ).fetchall()


def _audit(db, action="engine.set") -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


def _engine_in_db(db) -> str:
    with db.reader() as conn:
        return engine_mod.get_engine(conn)


# --- GET/PUT /engine --------------------------------------------------------------------------------------


async def test_engine_default_claude(client, app, world, tmp_path):
    # Пустой каталог конфига во временной папке: без него ядро смотрит боевой (`~/.config/hub`), а там с 30.09
    # лежит `engine.json` владельца — тест падал от окружения.
    app.state.config_dir = tmp_path
    r = await client.get(f"{API}/engine", headers=_bearer(world["read"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "claude"
    assert body["changed_ts"] is None
    assert [e["name"] for e in body["engines"]] == ["claude", "opencode", "codex", "cursor"]
    # Ф78-2: модель и усилие текущего движка (умолчание подставлено), по движку — доступность и умолчания.
    assert body["model"] == "sonnet" and body["effort"] is None
    claude = body["engines"][0]
    assert claude["available"] is True and claude["default_model"] == "sonnet" and claude["model"] is None
    assert [m["id"] for m in claude["models"]] == ["sonnet", "opus"]  # haiku ниже минимума (владелец 22.10)
    # П13: с 16-6 флаг готовности стоит; без `engine.json` OpenCode недоступен — эта причина показывается у
    # неактивного варианта в UI.
    assert engine_mod.OPENCODE_READY is True
    assert body["opencode_available"] is False
    assert body["opencode_reason"] == engine_mod.NO_SETUP_TEXT
    # Ф78-2: таблицы ролей больше нет (она была про убранные задачи по «Го»).
    assert "roles" not in body


async def test_engine_get_visible_to_project_scoped_token_and_not_anonymous(client, db, world):
    """GET — любому авторизованному (дежурная и хост читают движок своим токеном), без токена — 401."""
    scoped = make_bearer_token(
        db, kind="background", scopes=("read",), project_ids=[world["first"]], name="mcp:wake-1"
    )
    assert (await client.get(f"{API}/engine", headers=_bearer(scoped))).status_code == 200
    assert (await client.get(f"{API}/engine")).status_code == 401


async def test_status_carries_engine(client, world, admin_client, oc_ready):
    r = await client.get(f"{API}/status", headers=_bearer(world["read"]))
    assert r.json()["engine"] == "claude"
    r = await admin_client.post("/internal/v1/admin/engine", json={"engine": "opencode"}, headers=_admin())
    assert r.status_code == 200, r.text
    r = await client.get(f"{API}/status", headers=_bearer(world["read"]))
    assert r.json()["engine"] == "opencode"


async def test_put_engine_ui_local_switches_and_audits(client, db, clock, world, oc_ready):
    ui = await _ui(client, db, clock)
    r = await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=ui)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "opencode" and body["old_engine"] == "claude"
    assert "ended_subs" not in body
    assert body["opencode_available"] is True and body["opencode_reason"] is None
    assert body["changed_ts"] == int(clock() * 1000)
    [(actor, result, detail)] = _audit(db)
    assert actor == "ui" and result == "ok"
    assert json.loads(detail) == {"old": "claude", "new": "opencode", "model": None, "effort": None}

    # Повторная установка того же значения — без изменений и без аудита (3.6).
    r = await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=ui)
    assert r.status_code == 200 and r.json()["old_engine"] == "opencode"
    assert len(_audit(db)) == 1
    assert _engine_in_db(db) == "opencode"


async def test_put_engine_bearer_refused(client, db, world, oc_ready):
    r = await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=_bearer(world["sess"]))
    assert r.status_code == 403
    assert (await client.get(f"{API}/engine", headers=_bearer(world["sess"]))).status_code == 200
    # Токен хоста hub-work тоже не переключает (он движок только читает).
    r = await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=_bearer(world["host"]))
    assert r.status_code == 403
    assert _engine_in_db(db) == "claude"


async def test_put_engine_from_remote_listener_allowed(db, clock, tmp_path, monkeypatch):
    """Ф78-2: переключатель работает и с телефона — `PUT /engine` с кукой удалённого
    слушателя проходит (прежде — 403 «local only»)."""
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)
    notifier = Notifier()
    db.on_commit(notifier.notify)
    state = RemoteState(port=REMOTE_PORT)
    with db.reader() as conn:
        state.load(conn)
    rapp = create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=state, notifier=notifier
    )
    rapp.state.config_dir = tmp_path
    (tmp_path / engine_mod.SETUP_FILE).write_text("{}", "utf-8")
    monkeypatch.setattr(engine_mod, "OPENCODE_READY", True)

    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=ORIGIN) as local:
        cookie, csrf = await login_via_code(local, db, clock)
        local_headers = {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}
        local.cookies.clear()
        r = await local.put(
            f"{API}/remote/password", json={"password": PASSWORD}, headers=local_headers
        )
        assert r.status_code == 200, r.text
        r = await local.put(
            f"{API}/remote", json={"enabled": True, "hosts": [REMOTE_HOST]}, headers=local_headers
        )
        assert r.status_code == 200, r.text
        # Локально переключить можно.
        r = await local.put(f"{API}/engine", json={"engine": "opencode"}, headers=local_headers)
        assert r.status_code == 200, r.text

        async with AsyncClient(
            transport=ASGITransport(app=rapp),
            base_url=f"http://127.0.0.1:{REMOTE_PORT}",
            headers={"Host": REMOTE_HOST},
        ) as phone:
            r = await phone.post(
                f"{API}/auth/password", json={"password": PASSWORD}, headers={"Origin": REMOTE_ORIGIN}
            )
            assert r.status_code == 200, r.text
            csrf_phone = r.json()["csrf"]
            cookie_phone = phone.cookies.get("hub_session")
            phone.cookies.clear()
            phone_headers = {
                "Cookie": f"hub_session={cookie_phone}",
                "Origin": REMOTE_ORIGIN,
                "X-Hub-CSRF": csrf_phone,
            }
            assert (await phone.get(f"{API}/engine", headers=phone_headers)).status_code == 200
            r = await phone.put(f"{API}/engine", json={"engine": "claude"}, headers=phone_headers)
            assert r.status_code == 200, r.text
            assert r.json()["engine"] == "claude"
            # Ф78-2 (раздел 12): свой выбор фактов — тоже с телефона.
            r = await phone.put(f"{API}/engine/facts", json={"engine": "opencode"}, headers=phone_headers)
            assert r.status_code == 200, r.text
            assert r.json()["facts"]["engine"] == "opencode" and r.json()["engine"] == "claude"
            r = await phone.put(f"{API}/engine/facts", json={"engine": None}, headers=phone_headers)
            assert r.status_code == 200 and r.json()["facts"]["follows_agent"] is True
    assert _engine_in_db(db) == "claude"


async def test_put_engine_bad_body_422(client, db, clock):
    ui = await _ui(client, db, clock)
    for body in ({"engine": "gemini"}, {}, {"engine": "claude", "x": 1}, {"engine": 1}):
        assert (await client.put(f"{API}/engine", json=body, headers=ui)).status_code == 422
    assert _engine_in_db(db) == "claude"


async def test_put_engine_opencode_not_ready(client, db, clock, monkeypatch, tmp_path, app):
    """П13: нет флага готовности — 409 «ещё не готов»; флаг есть, но нет `engine.json` — 409 про setup."""
    ui = await _ui(client, db, clock)
    monkeypatch.setattr(engine_mod, "OPENCODE_READY", False)  # окно до 16-6 — ветка остаётся проверенной
    r = await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=ui)
    assert r.status_code == 409 and r.json()["detail"] == engine_mod.NOT_READY_TEXT

    monkeypatch.setattr(engine_mod, "OPENCODE_READY", True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    app.state.config_dir = config_dir
    r = await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=ui)
    assert r.status_code == 409 and r.json()["detail"] == engine_mod.NO_SETUP_TEXT
    body = (await client.get(f"{API}/engine", headers=ui)).json()
    assert body["opencode_available"] is False and body["opencode_reason"] == engine_mod.NO_SETUP_TEXT

    (config_dir / engine_mod.SETUP_FILE).write_text("{}", "utf-8")
    assert (await client.put(f"{API}/engine", json={"engine": "opencode"}, headers=ui)).status_code == 200
    # Обратно на Claude — всегда можно: готовность OpenCode не важна.
    monkeypatch.setattr(engine_mod, "OPENCODE_READY", False)
    assert (await client.put(f"{API}/engine", json={"engine": "claude"}, headers=ui)).status_code == 200
    assert _engine_in_db(db) == "claude"


async def test_admin_engine_get_and_set(admin_client, db, oc_ready):
    r = await admin_client.get("/internal/v1/admin/engine", headers=_admin())
    assert r.status_code == 200 and r.json()["engine"] == "claude"
    r = await admin_client.post("/internal/v1/admin/engine", json={"engine": "opencode"}, headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "opencode"
    [(actor, _result, _detail)] = _audit(db)
    assert actor == "admin"
    r = await admin_client.post("/internal/v1/admin/engine", json={"engine": "claude"}, headers=_admin())
    assert r.status_code == 200 and r.json()["engine"] == "claude"
    assert len(_audit(db)) == 2
    r = await admin_client.post("/internal/v1/admin/engine", json={"engine": "claude"})
    assert r.status_code == 401


async def test_admin_engine_facts_set_and_off(admin_client, db, oc_ready):
    url = "/internal/v1/admin/engine/facts"
    r = await admin_client.post(url, json={"engine": "opencode", "effort": "low"}, headers=_admin())
    assert r.status_code == 200, r.text
    assert (r.json()["facts"]["engine"], r.json()["facts"]["effort"]) == ("opencode", "low")
    assert r.json()["engine"] == "claude"
    assert (await admin_client.post(url, json={"engine": "gemini"}, headers=_admin())).status_code == 422
    r = await admin_client.post(url, json={"engine": None}, headers=_admin())
    assert r.status_code == 200 and r.json()["facts"]["follows_agent"] is True
    [(actor, _r1, _d1), (actor2, _r2, _d2)] = _audit(db, engine_mod.FACTS_AUDIT_ACTION)
    assert actor == actor2 == "admin"
    assert (await admin_client.post(url, json={"engine": None})).status_code == 401


# --- CLI `hub engine` -------------------------------------------------------------------------------------


def _fake_admin(monkeypatch, responses: list[tuple[int, dict]], tmp_path) -> list[httpx.Request]:
    """Подменный транспорт админ-сокета (образец — tests/test_cli.py `_fake_admin`)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, payload = responses.pop(0)
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1"
        ),
    )
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(hub_cli.paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hub_cli.paths, "data_dir", lambda: tmp_path)
    (tmp_path / "admin.key").write_text(ADMIN_SECRET, "utf-8")
    return seen


def _run_cli(argv: list[str]) -> None:
    old = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old


def _row(name, model, effort, default_model, default_effort, reason=None) -> dict:
    return {
        "name": name,
        "available": reason is None,
        "reason": reason,
        "default_model": default_model,
        "default_effort": default_effort,
        "model": model,
        "effort": effort,
    }


_ENGINE_BODY = {
    "engine": "claude",
    "model": "sonnet",
    "effort": None,
    "changed_ts": None,
    "engines": [
        _row("claude", None, None, "sonnet", None),
        _row("opencode", None, None, "deepseek/deepseek-flash", "max", reason=engine_mod.NOT_READY_TEXT),
        _row("codex", "gpt-6.1-sol", "high", "gpt-6-luna", "low"),
    ],
    "opencode_available": False,
    "opencode_reason": engine_mod.NOT_READY_TEXT,
}


def test_cli_engine_show(monkeypatch, capsys, tmp_path):
    seen = _fake_admin(monkeypatch, [(200, _ENGINE_BODY)], tmp_path)
    _run_cli(["engine"])
    out = capsys.readouterr().out
    assert "движок: claude · sonnet" in out
    assert "переключён: не переключался" in out
    assert "  claude: sonnet (умолчание)" in out
    reason = engine_mod.NOT_READY_TEXT
    assert f"  opencode: deepseek/deepseek-flash (умолчание) · max — недоступен: {reason}" in out
    assert "  codex: gpt-6.1-sol · high" in out
    assert "роль" not in out
    [request] = seen
    assert request.method == "GET" and request.url.path == "/internal/v1/admin/engine"


def test_cli_engine_set_refused_prints_reason(monkeypatch, capsys, tmp_path):
    seen = _fake_admin(monkeypatch, [(409, {"detail": engine_mod.NO_SETUP_TEXT})], tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_cli(["engine", "set", "opencode"])
    assert engine_mod.NO_SETUP_TEXT in str(exc.value.code)
    [request] = seen
    assert request.method == "POST" and request.url.path == "/internal/v1/admin/engine"


def test_cli_engine_set_ok(monkeypatch, capsys, tmp_path):
    changed = 1_700_000_000_000
    payload = {
        **_ENGINE_BODY,
        "engine": "opencode",
        "changed_ts": changed,
        "old_engine": "claude",
    }
    _fake_admin(monkeypatch, [(200, payload)], tmp_path)
    seen = _fake_admin(monkeypatch, [(200, {**payload, "model": "deepseek/deepseek-flash"})], tmp_path)
    _run_cli(["engine", "set", "opencode"])
    out = capsys.readouterr().out
    assert "движок: opencode · deepseek/deepseek-flash" in out
    assert "снято подписок" not in out and "hub-project" not in out
    assert json.loads(seen[0].content) == {"engine": "opencode"}  # без флагов — прежний выбор движка

    reply = {**payload, "engine": "codex", "model": "gpt-6-luna"}
    seen = _fake_admin(monkeypatch, [(200, reply)], tmp_path)
    _run_cli(["engine", "set", "codex", "--model", "gpt-6-luna", "--effort", ""])
    out = capsys.readouterr().out
    assert "движок: codex · gpt-6-luna" in out and "сняты" not in out
    # Ф78-2: `--model`/`--effort` уходят в тело; пустое — `null` (умолчание движка).
    assert json.loads(seen[0].content) == {"engine": "codex", "model": "gpt-6-luna", "effort": None}


_FACTS_OWN = {"engine": "codex", "model": "gpt-6-luna", "effort": "low", "follows_agent": False, "ts": 1}
_FACTS_AGENT = {"engine": "claude", "model": "sonnet", "effort": None, "follows_agent": True, "ts": None}


def test_cli_engine_show_has_facts_line(monkeypatch, capsys, tmp_path):
    _fake_admin(monkeypatch, [(200, {**_ENGINE_BODY, "facts": _FACTS_AGENT})], tmp_path)
    _run_cli(["engine"])
    assert "факты о людях: claude · sonnet (как у агента)" in capsys.readouterr().out
    _fake_admin(monkeypatch, [(200, _ENGINE_BODY)], tmp_path)  # старое ядро без блока — строки нет
    _run_cli(["engine"])
    assert "факты" not in capsys.readouterr().out


def test_cli_engine_facts_show_set_off(monkeypatch, capsys, tmp_path):
    seen = _fake_admin(monkeypatch, [(200, {**_ENGINE_BODY, "facts": _FACTS_AGENT})], tmp_path)
    _run_cli(["engine", "facts"])
    assert capsys.readouterr().out.strip() == "факты о людях: claude · sonnet (как у агента)"
    assert seen[0].method == "GET" and seen[0].url.path == "/internal/v1/admin/engine"

    seen = _fake_admin(monkeypatch, [(200, {**_ENGINE_BODY, "facts": _FACTS_OWN})], tmp_path)
    _run_cli(["engine", "facts", "set", "codex", "--model", "gpt-6-luna", "--effort", ""])
    assert "факты о людях: codex · gpt-6-luna · low (своё)" in capsys.readouterr().out
    assert seen[0].method == "POST" and seen[0].url.path == "/internal/v1/admin/engine/facts"
    assert json.loads(seen[0].content) == {"engine": "codex", "model": "gpt-6-luna", "effort": None}

    seen = _fake_admin(monkeypatch, [(200, {**_ENGINE_BODY, "facts": _FACTS_AGENT})], tmp_path)
    _run_cli(["engine", "facts", "off"])
    assert "(как у агента)" in capsys.readouterr().out
    assert json.loads(seen[0].content) == {"engine": None}

    seen = _fake_admin(monkeypatch, [(409, {"detail": "cursor-agent не найден"})], tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_cli(["engine", "facts", "set", "cursor"])
    assert "cursor-agent не найден" in str(exc.value.code)
    assert json.loads(seen[0].content) == {"engine": "cursor"}


# --- смена движка и подписки (3.6, П1) ---------------------------------------------------------------------


async def test_switch_keeps_subs_of_both_engines(client, db, clock, world, procs, oc_ready):
    """П18: подписки Claude (UUID) и OpenCode (`ses_…`) принимаются при любом движке, смена их не трогает."""
    r = await _subscribe(client, world, sid=CLAUDE_SID)
    assert r.status_code == 200, r.text
    claude_sub = r.json()["sub"]["id"]
    r = await _chan_subscribe(client, world, world["chan"], sid=CLAUDE_SID)
    assert r.status_code == 200, r.text
    claude_chan = r.json()["sub"]["id"]
    # Хаб на Claude, подписка OpenCode — принимается.
    r = await _subscribe(
        client, world, sid=OC_A, kind="opencode", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert r.status_code == 200, r.text
    oc_sub = r.json()["sub"]["id"]
    r = await _chan_subscribe(
        client, world, world["other_chan"], sid=OC_A, kind="opencode", cwd=world["dirs"][1]
    )
    assert r.status_code == 200, r.text
    oc_chan = r.json()["sub"]["id"]

    for target in ("opencode", "claude"):
        body = (await _switch(client, db, clock, target)).json()
        assert "ended_subs" not in body
        assert {row[0] for row in _sub_rows(db) if row[3] is None} == {claude_sub, oc_sub}
        assert {row[0] for row in _chan_rows(db) if row[3] is None} == {claude_chan, oc_chan}
    # Аудит — по строке на каждую настоящую смену, без числа снятых.
    details = [json.loads(row[2]) for row in _audit(db)]
    assert details == [
        {"old": "claude", "new": "opencode", "model": None, "effort": None},
        {"old": "opencode", "new": "claude", "model": None, "effort": None},
    ]


async def test_new_subs_of_either_engine_accepted_at_any_switch(client, db, clock, world, procs, oc_ready):
    """П18: нет 409 `engine_inactive` — ни для сессии OpenCode при хабе на Claude, ни наоборот."""
    r = await _subscribe(client, world, sid=OC_A, kind="opencode")
    assert r.status_code == 200, r.text
    r = await _chan_subscribe(client, world, world["chan"], sid=OC_A, kind="opencode")
    assert r.status_code == 200, r.text
    await _switch(client, db, clock, "opencode")
    r = await _subscribe(
        client, world, sid=CLAUDE_SID, kind="interactive", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert r.status_code == 200, r.text
    r = await _chan_subscribe(
        client, world, world["other_chan"], sid=CLAUDE_SID, kind="interactive", cwd=world["dirs"][1]
    )
    assert r.status_code == 200, r.text


async def test_kind_opencode_accepted_unknown_kind_refused(client, db, clock, world, procs, oc_ready):
    await _switch(client, db, clock, "opencode")
    r = await _subscribe(client, world, sid=OC_A, kind="opencode")
    assert r.status_code == 200, r.text
    r = await _chan_subscribe(
        client, world, world["other_chan"], sid=OC_A, kind="opencode", cwd=world["dirs"][1]
    )
    assert r.status_code == 200, r.text
    # Вид сессии: не интерактивная и не OpenCode — 403 (подписка на беседу тем же правилом).
    r = await _subscribe(
        client, world, sid=OC_B, kind="background", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert r.status_code == 403 and "интерактивная" in r.json()["detail"]
    r = await _chan_subscribe(
        client, world, world["other_chan"], sid=OC_B, kind="background", cwd=world["dirs"][1]
    )
    assert r.status_code == 403
    # Порядок проверок (П15): сначала живость — мёртвый процесс с верным движком всё равно «не найдена».
    dead = _session(pid=9999, start=1, sid=OC_B, kind="opencode", cwd=world["dirs"][1])
    r = await client.post(
        f"{API}/subs", json={"project_id": world["second"], "session": dead}, headers=_bearer(world["sess"])
    )
    assert r.status_code == 409 and r.json()["detail"] == "сессия не найдена"


# --- П1: две сессии OpenCode на одном pid ------------------------------------------------------------------


async def test_two_oc_sessions_same_pid_do_not_displace(client, db, clock, world, procs, oc_ready):
    await _switch(client, db, clock, "opencode")

    # Один проект: вторая сессия того же процесса — рядом (Ф26-1: не 409 и не вытеснение — обе живые).
    a = await _subscribe(client, world, sid=OC_A, kind="opencode", project_id=world["first"])
    assert a.status_code == 200, a.text
    b = await _subscribe(client, world, sid=OC_B, kind="opencode", project_id=world["first"])
    assert b.status_code == 200 and b.json()["created"] is True, b.text
    assert [n["id"] for n in b.json()["neighbours"]] == [a.json()["sub"]["id"]]
    rows = _sub_rows(db)
    assert len(rows) == 2 and all(row[3] is None for row in rows)
    assert {row[2] for row in rows} == {OC_A, OC_B}

    # Разные проекты — тоже живы.
    c = await _subscribe(
        client, world, sid=OC_B, kind="opencode", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert c.status_code == 200, c.text
    rows = _sub_rows(db)
    assert len(rows) == 3 and all(row[3] is None for row in rows)

    # Тот же `session_id` — идемпотентно, подписка не плодится.
    again = await _subscribe(client, world, sid=OC_A, kind="opencode", project_id=world["first"])
    assert again.status_code == 200 and again.json()["created"] is False
    assert len(_sub_rows(db)) == 3

async def test_two_oc_chan_subs_without_conversation_changed(client, db, clock, world, procs, oc_ready):
    await _switch(client, db, clock, "opencode")

    a = await _chan_subscribe(client, world, world["chan"], sid=OC_A, kind="opencode")
    assert a.status_code == 200, a.text
    # Другая сессия того же процесса на ДРУГУЮ беседу — подписка первой не снимается (правило П5 не для OC).
    b = await _chan_subscribe(
        client, world, world["other_chan"], sid=OC_B, kind="opencode", cwd=world["dirs"][1]
    )
    assert b.status_code == 200, b.text
    rows = _chan_rows(db)
    assert len(rows) == 2 and all(row[3] is None for row in rows)

    # Ф39 A0: та же беседа другой живой сессией — своя подписка (не 409 и не «replaced» первой).
    c = await _chan_subscribe(client, world, world["chan"], sid=OC_B, kind="opencode", cwd=world["dirs"][1])
    assert c.status_code == 200 and c.json()["created"] is True
    assert c.json()["sub"]["id"] != a.json()["sub"]["id"]
    assert [o["id"] for o in c.json()["others"]] == [a.json()["sub"]["id"]]
    assert len(_chan_rows(db)) == 3 and all(row[3] is None for row in _chan_rows(db))

    # Своя же сессия на ту же беседу — идемпотентно.
    again = await _chan_subscribe(client, world, world["chan"], sid=OC_A, kind="opencode")
    assert again.status_code == 200 and again.json()["created"] is False

    # У Claude правило П5 прежнее: новый разговор того же процесса снимает подписки прежнего.
    await _switch(client, db, clock, "claude")
    first = await _chan_subscribe(client, world, world["chan"], sid=CLAUDE_SID)
    assert first.status_code == 200, first.text
    other = await _chan_subscribe(client, world, world["other_chan"], sid=CLAUDE_SID_2, cwd=world["dirs"][1])
    assert other.status_code == 200, other.text
    ended = [row for row in _chan_rows(db) if row[3] is not None and row[4] == "conversation_changed"]
    # П18: смена движка подписки OpenCode не снимала, поэтому в том же (тестовом) процессе П5 снимает и их.
    assert [row[2] for row in ended if row[2] == CLAUDE_SID] == [CLAUDE_SID]


# --- ожидание хоста (3.1, П11) ------------------------------------------------------------------------------


async def test_host_wait_carries_engine_and_exits_on_switch(client, world, oc_ready, admin_client):
    headers = _bearer(world["host"])
    r = await client.get(f"{API}/work/host/wait", params={"since": 0, "timeout_s": 0}, headers=headers)
    assert r.status_code == 200 and r.json()["engine"] == "claude"
    r = await client.get(f"{API}/work/host/open", headers=headers)
    assert r.status_code == 200 and r.json()["engine"] == "claude"

    # Хост знает `claude`, а хаб уже на `opencode` — ответ сразу, не через `timeout_s`.
    await admin_client.post("/internal/v1/admin/engine", json={"engine": "opencode"}, headers=_admin())
    started = time.monotonic()
    r = await client.get(
        f"{API}/work/host/wait", params={"since": 0, "timeout_s": 10, "engine": "claude"}, headers=headers
    )
    assert r.status_code == 200 and r.json()["engine"] == "opencode"
    assert time.monotonic() - started < 5
    # Совпало — ждёт положенное и отдаёт пустой снимок.
    started = time.monotonic()
    r = await client.get(
        f"{API}/work/host/wait", params={"since": 0, "timeout_s": 1, "engine": "opencode"}, headers=headers
    )
    assert r.status_code == 200 and r.json()["sessions"]["items"] == [] and r.json()["engine"] == "opencode"
    assert time.monotonic() - started >= 0.9


async def test_sub_wait_survives_engine_switch(client, db, clock, world, procs, oc_ready):
    """П18: смена движка подписку не снимает — идущее ожидание доживает до своего срока (200), а не 410."""
    await _switch(client, db, clock, "opencode")
    r = await _subscribe(client, world, sid=OC_A, kind="opencode")
    sub_id = r.json()["sub"]["id"]
    ui = await _ui(client, db, clock)

    waiter = asyncio.ensure_future(
        client.post(
            f"{API}/subs/{sub_id}/wait",
            json={
                "session": {"pid": PID, "proc_start": START, "session_id": OC_A},
                "waiter": "0" * 16,
                "timeout_s": 1,
            },
            headers=_bearer(world["sess"]),
        )
    )
    await asyncio.sleep(0.05)
    assert (await client.put(f"{API}/engine", json={"engine": "claude"}, headers=ui)).status_code == 200
    response = await asyncio.wait_for(waiter, timeout=10)
    assert response.status_code == 200, response.text
    assert [row for row in _sub_rows(db) if row[0] == sub_id][0][3] is None


# --- `engine_of_session` (П15) ------------------------------------------------------------------------------


def test_engine_of_session():
    assert engine_mod.engine_of_session("opencode", "whatever") == "opencode"
    assert engine_mod.engine_of_session(None, OC_A) == "opencode"
    assert engine_mod.engine_of_session("interactive", OC_A) == "opencode"
    assert engine_mod.engine_of_session("interactive", CLAUDE_SID) == "claude"
    assert engine_mod.engine_of_session(None, "") == "claude"
    assert engine_mod.engine_of_session(None, None) == "claude"


def test_engine_garbage_in_meta_reads_as_default(db):
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.meta (key, value) VALUES (?, ?)", (engine_mod.META_ENGINE, "gemini")
        )
    )
    assert _engine_in_db(db) == "claude"
