"""Ф31-1 B6 (docs/architecture.md + П5): SIGTERM при открытом `/stream` (локальный
http, удалённый слушатель по https и http) и при открытом долгом ожидании сессии (`/channels/{id}/wait`,
`/subs/{id}/wait`) — ядро гаснет по флагу остановки `Notifier`, заметно быстрее запасного
`timeout_graceful_shutdown=5` (до правки — ~25 с, `.runs/critic-f14-9/test_sse_shutdown.py`). Ожидание,
разбуженное остановкой, отвечает пусто и ничего не выдаёт.

`hub core` — отдельным процессом на свободных портах и временных каталогах (обвязка
`test_remote_tls_core._Core`); БД заполняется до старта ядра."""

import os
import ssl
import threading
import time

import httpx
import pytest
from api.conftest import insert_account, insert_channel, insert_project
from test_remote_tls_core import PASSWORD, _Core, _enable_remote

from hub.core import security, subs
from hub.core.db import Db
from hub.dev.selfsigned import write_pair

FAST_EXIT_S = 2.0  # запас `timeout_graceful_shutdown` — 5 с


@pytest.fixture
def core(tmp_path, monkeypatch):
    c = _Core(tmp_path)
    monkeypatch.setenv("HUB_DATA_DIR", str(c.data_dir))
    monkeypatch.setenv("HUB_CONFIG_DIR", str(c.config_dir))
    yield c
    if c.proc is not None and c.proc.poll() is None:
        c.proc.kill()
        c.proc.wait()


def _seed(core: _Core, tmp_path) -> dict:
    """Проект с подписками, беседа, токен сессии — до старта ядра."""
    workdir = tmp_path / "proj"
    workdir.mkdir()
    db = Db(core.data_dir)
    db.start()
    try:
        pid = insert_project(db, name="P", workdir=str(workdir))
        db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
        acc = insert_account(db, source="telegram")
        cid = insert_channel(db, acc, project_id=pid, ext_id="c1")
        token = db.write_sync(
            lambda conn: security.create_api_token(
                conn,
                name="mcp:all",
                kind="interactive",
                scopes=["read", "draft"],
                project_ids=None,
                expires_ts=None,
            )
        ).token
    finally:
        db.close()
    return {"pid": pid, "cid": cid, "token": token, "workdir": str(workdir)}


def _hold(open_request) -> tuple[threading.Thread, dict]:
    """Держать долгий запрос в потоке; `out` — статус и тело, когда ответ пришёл (или ошибка)."""
    started = threading.Event()
    out: dict = {}

    def run():
        try:
            open_request(started, out)
        except Exception as exc:  # noqa: BLE001 — разрыв при остановке тоже результат
            out["error"] = repr(exc)
        finally:
            started.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(10)
    time.sleep(0.5)  # запрос дошёл до ожидания
    return thread, out


def _stop_fast(core: _Core, started: list | None = None) -> float:
    t0 = time.monotonic()
    if started is not None:
        started.append(t0)
    core.proc.terminate()
    try:
        code = core.proc.wait(timeout=20)
    except Exception:
        core.proc.kill()
        core.proc.wait()
        pytest.fail("ядро не погасло за 20 с")
    took = time.monotonic() - t0
    assert code == 0
    assert took < FAST_EXIT_S, f"ядро гасло {took:.1f} с"
    return took


def _stream(client: httpx.Client):
    """Поток SSE до конца; конец — время и закрытие клиента (как браузер, заметивший закрытый поток: иначе
    простаивающее TLS-соединение пула httpx не отвечает на close_notify, и uvicorn ждёт его до запасных
    5 с)."""

    def open_request(started, out):
        with client.stream("GET", "/api/v1/stream") as s:
            out["status"] = s.status_code
            started.set()
            for _ in s.iter_bytes():
                pass
        out["done"] = time.monotonic()
        client.close()

    return open_request


def _stream_closed_by_flag(out: dict, stop_ts: float) -> None:
    """Поток закрыло ядро сразу после SIGTERM, а не обрыв по запасному таймауту uvicorn."""
    assert "done" in out, out
    assert out["done"] - stop_ts < 1.0


def test_b6_local_stream(core, tmp_path):
    seed = _seed(core, tmp_path)
    core.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{core.port}",
        headers={"Authorization": f"Bearer {seed['token']}"},
        trust_env=False,
        timeout=60,
    )
    thread, out = _hold(_stream(client))
    assert out.get("status") == 200, out
    stop: list = []
    _stop_fast(core, stop)
    thread.join(5)
    _stream_closed_by_flag(out, stop[0])


@pytest.mark.parametrize("tls", [True, False])
def test_b6_remote_stream(core, capsys, tls):
    ca = write_pair(core.tls_dir, ["hub.test", "127.0.0.1"]) if tls else None
    core.start()
    host = f"hub.test:{core.remote_port}"
    _enable_remote(core, capsys, host)
    scheme = "https" if tls else "http"
    verify = ssl.create_default_context(cafile=str(ca)) if tls else False
    phone = httpx.Client(
        base_url=f"{scheme}://127.0.0.1:{core.remote_port}",
        headers={"Host": host},
        verify=verify,
        trust_env=False,
        timeout=60,
    )
    r = phone.post(
        "/api/v1/auth/password", json={"password": PASSWORD}, headers={"Origin": f"{scheme}://{host}"}
    )
    assert r.status_code == 200, r.text
    thread, out = _hold(_stream(phone))
    assert out.get("status") == 200, out
    stop: list = []
    _stop_fast(core, stop)
    thread.join(5)
    _stream_closed_by_flag(out, stop[0])


def test_b6_channel_wait(core, tmp_path):
    seed = _seed(core, tmp_path)
    core.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{core.port}",
        headers={"Authorization": f"Bearer {seed['token']}"},
        trust_env=False,
        timeout=60,
    )

    def open_request(started, out):
        started.set()
        r = client.get(f"/api/v1/channels/{seed['cid']}/wait", params={"timeout_s": 60})
        out["status"], out["body"] = r.status_code, r.json()

    thread, out = _hold(open_request)
    _stop_fast(core)
    thread.join(5)
    # ответ пришёл до закрытия соединения — пустой, как по таймауту
    assert out.get("status") == 200, out
    assert out["body"]["messages"] == []


def test_b6_sub_wait(core, tmp_path):
    seed = _seed(core, tmp_path)
    core.start()
    me = os.getpid()
    session = {"pid": me, "proc_start": subs.read_proc_start(me), "session_id": "sess-b6"}
    headers = {"Authorization": f"Bearer {seed['token']}", "X-Hub-Sub-Wait": "3"}
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{core.port}", headers=headers, trust_env=False, timeout=60
    )
    r = client.post(
        "/api/v1/subs",
        json={
            "project_id": seed["pid"],
            "session": {
                **session,
                "cwd": seed["workdir"],
                "kind": "interactive",
                "name": None,
                "name_source": None,
            },
        },
    )
    assert r.status_code == 200, r.text
    sub_id = r.json()["sub"]["id"]

    def open_request(started, out):
        started.set()
        r = client.post(
            f"/api/v1/subs/{sub_id}/wait",
            json={"session": session, "waiter": "0123456789abcdef", "timeout_s": 60},
        )
        out["status"], out["body"] = r.status_code, r.json()

    thread, out = _hold(open_request)
    _stop_fast(core)
    thread.join(5)
    assert out.get("status") == 200, out
    assert out["body"]["signals"] == []  # пусто и без выдачи (П5)


def test_f322_tg_lookup_next_and_wait(core, tmp_path):
    """Ф32-2 п.4 + П3: SIGTERM при висящем `lookup/next` коннектора Telegram и ждущей ответа сессии
    (`/tg/search`, до 40 с) — ядро гаснет быстро; коннектору — пусто (204), сессии — 503."""
    from hub.core import connector_auth

    seed = _seed(core, tmp_path)
    now_ms = int(time.time() * 1000)
    db = Db(core.data_dir)
    db.start()
    try:
        polled = insert_account(db, source="telegram")
        asked = insert_account(db, source="telegram")
        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.account SET status = 'ok', last_ok_ts = ? WHERE id IN (?, ?)",
                (now_ms, polled, asked),
            )
        )
        secret = connector_auth.generate_connector_secret()
        db.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
                (polled, connector_auth.hash_secret(secret)),
            )
        )
    finally:
        db.close()
    core.start()
    connector = httpx.Client(
        transport=httpx.HTTPTransport(uds=str(core.data_dir / "internal.sock")),
        base_url="http://hub-internal",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=60,
    )
    session = httpx.Client(
        base_url=f"http://127.0.0.1:{core.port}",
        headers={"Authorization": f"Bearer {seed['token']}"},
        trust_env=False,
        timeout=60,
    )

    def open_next(started, out):
        started.set()
        r = connector.post("/internal/v1/lookup/next", json={"timeout_s": 25})
        out["status"], out["done"] = r.status_code, time.monotonic()

    def open_search(started, out):
        started.set()
        r = session.post("/api/v1/tg/search", json={"query": "продавцы", "account_id": asked})
        out["status"], out["body"], out["done"] = r.status_code, r.text, time.monotonic()

    next_thread, next_out = _hold(open_next)
    search_thread, search_out = _hold(open_search)
    assert "status" not in next_out and "status" not in search_out, (next_out, search_out)
    stop: list = []
    _stop_fast(core, stop)
    next_thread.join(5)
    search_thread.join(5)
    assert next_out.get("status") == 204, next_out
    assert search_out.get("status") == 503, search_out
    assert "останавливается" in search_out["body"]
    assert next_out["done"] - stop[0] < 1.0 and search_out["done"] - stop[0] < 1.0
