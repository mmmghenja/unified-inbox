"""hub-mcp против НАСТОЯЩЕГО ядра: `hub core` дочерним процессом (uvicorn на свободном порту, временные
каталоги — не боевые 8787 и `~/.config/hub`). Спецификация: docs/architecture.md (CLI `hub channel`), П7, П9 (токен не в
stderr), A6 (CLI `hub token list/revoke`).

CLI-команды вызываются функцией `hub.cli.main()` в процессе теста (как в tests/test_cli.py); сам MCP-сервер в
тесте 15 — отдельным процессом по stdio, как его запускает Claude Code.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from hub import cli as hub_cli
from hub.mcp_server import profile as profile_mod
from hub.mcp_server import setup as mcp_setup
from hub.mcp_server.client import HubClient

_STARTUP_TIMEOUT_S = 15.0
_DEAD_PROXY = "http://127.0.0.1:9"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, what: str) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{what} не дождались за {_STARTUP_TIMEOUT_S} с")


def _connects_unix(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _connects_tcp(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


@pytest.fixture
def real_core(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    port = _free_port()
    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait(lambda: (config_dir / "admin.key").exists(), "admin.key")
        _wait(lambda: _connects_unix(data_dir / "internal.sock"), "internal.sock")
        _wait(lambda: _connects_tcp(port), f"порт {port}")
        monkeypatch.setenv("HUB_DATA_DIR", str(data_dir))
        monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
        yield data_dir, config_dir, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _run_cli(argv: list[str]) -> None:
    old_argv = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old_argv


def _api(port: int, token: str, path: str) -> httpx.Response:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", trust_env=False) as c:
        return c.get(path, headers={"Authorization": f"Bearer {token}"})


def _token_list(capsys) -> list[dict]:
    capsys.readouterr()
    _run_cli(["token", "list"])
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


# --- Тест 14 (+П7): hub mcp setup ------------------------------------------------------------------------


def test_7a_14_setup_writes_private_files_prints_command_and_rotates(real_core, capsys):
    _data_dir, config_dir, port = real_core

    _run_cli(["mcp", "setup", "--profile", "all", "--organize"])
    out = capsys.readouterr().out
    hub_path = mcp_setup.hub_executable()
    assert os.path.isabs(hub_path)
    assert f"claude mcp add -s user hub-all -- {hub_path} mcp --profile all" in out
    assert '"type": "local"' in out and "внешнему" in out

    mcp_dir = config_dir / "mcp"
    assert oct(mcp_dir.stat().st_mode & 0o777) == "0o700"
    token_file = mcp_dir / "all.token"
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    assert oct((mcp_dir / "all.json").stat().st_mode & 0o777) == "0o600"
    first_token = profile_mod.read_token(config_dir, "all")
    assert first_token is not None and first_token not in out  # значение токена не печатается
    meta = profile_mod.read_meta(config_dir, "all")
    assert meta["scopes"] == ["read", "draft", "organize"] and meta["projects"] is None
    assert _api(port, first_token, "/projects").status_code == 200

    _run_cli(["mcp", "setup", "--profile", "all", "--organize"])
    out = capsys.readouterr().out
    assert f"id={meta['token_id']}) отозван" in out
    second_token = profile_mod.read_token(config_dir, "all")
    assert second_token != first_token
    assert _api(port, first_token, "/projects").status_code == 401
    assert _api(port, second_token, "/projects").status_code == 200


def test_7a_14_organize_with_project_list_fails_before_creating_token(real_core, capsys):
    before = _token_list(capsys)
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(["mcp", "setup", "--profile", "personal", "--projects", "1", "--organize"])
    assert "--organize" in str(exc_info.value.code)
    assert _token_list(capsys) == before
    assert not (real_core[1] / "mcp" / "personal.token").exists()


def test_7a_p7_broken_meta_does_not_revoke_foreign_token(real_core, capsys):
    _data_dir, config_dir, port = real_core
    _run_cli(["token", "create", "--name", "other", "--kind", "interactive", "--scopes", "read"])
    other_token = capsys.readouterr().out.strip()
    other_id = next(t["id"] for t in _token_list(capsys) if t["name"] == "other")

    _run_cli(["mcp", "setup", "--profile", "p", "--projects", "all"])
    capsys.readouterr()
    meta_file = config_dir / "mcp" / "p.json"
    meta = json.loads(meta_file.read_text())
    meta["token_id"] = other_id  # испорченное описание указывает на чужой токен
    meta_file.write_text(json.dumps(meta))

    _run_cli(["mcp", "setup", "--profile", "p"])
    assert "отозван" not in capsys.readouterr().out
    assert _api(port, other_token, "/projects").status_code == 200


# --- A6 / П4: CLI hub token list/revoke, hub channel show/move --------------------------------------------


def test_7a_token_list_and_revoke_cli(real_core, capsys):
    _data_dir, _config_dir, port = real_core
    _run_cli(["token", "create", "--name", "t1", "--kind", "interactive", "--scopes", "read,draft"])
    token = capsys.readouterr().out.strip()
    tokens = _token_list(capsys)
    assert [(t["name"], t["scopes"], t["revoked"]) for t in tokens] == [("t1", ["read", "draft"], False)]
    assert "hash" not in json.dumps(tokens) and token not in json.dumps(tokens)

    _run_cli(["token", "revoke", str(tokens[0]["id"])])
    assert capsys.readouterr().out.strip() == "ok"
    assert _api(port, token, "/projects").status_code == 401
    with pytest.raises(SystemExit):
        _run_cli(["token", "revoke", "999"])


def test_7a_channel_show_and_move_cli(real_core, capsys):
    data_dir, config_dir, port = real_core
    _run_cli(["project", "add", "--name", "Личное"])
    project_id = int(capsys.readouterr().out.strip().split("=")[1])
    _run_cli(["account", "add", "--source", "fake", "--db", "main"])
    account_id = int(capsys.readouterr().out.splitlines()[0].split("=")[1])
    secret = (config_dir / "connectors" / f"{account_id}.key").read_text().strip()
    event = {
        "type": "upsert",
        "channel": {"ext_id": "chat-1", "kind": "dm", "title": "Вася"},
        "ext_id": "1",
        "ts": int(time.time() * 1000),
        "is_me": False,
        "kind": "text",
        "text": "привет",
        "mentions_me": False,
    }
    transport = httpx.HTTPTransport(uds=str(data_dir / "internal.sock"))
    with httpx.Client(transport=transport, base_url="http://hub-internal/internal/v1") as c:
        r = c.post("/events", json={"events": [event]}, headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200, r.text

    # Проектный токен на «Личное» есть — владельца правило П4 не ограничивает.
    _run_cli(
        ["token", "create", "--name", "p", "--kind", "interactive", "--scopes", "read"]
        + ["--project-ids", str(project_id)]
    )
    capsys.readouterr()
    _run_cli(["mcp", "setup", "--profile", "org", "--organize"])
    capsys.readouterr()
    org_token = profile_mod.read_token(config_dir, "org")
    channel_id = _api(port, org_token, "/projects/-1/channels").json()[0]["id"]

    _run_cli(["channel", "show", str(channel_id)])
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == channel_id and shown["title"] == "Вася" and shown["source"] == "fake"

    _run_cli(["channel", "move", str(channel_id), "--project", str(project_id)])
    assert json.loads(capsys.readouterr().out)["project_id"] == project_id
    _run_cli(["channel", "move", str(channel_id), "--unsorted"])
    assert json.loads(capsys.readouterr().out)["project_id"] is None
    _run_cli(["channel", "move", str(channel_id), "--rules"])
    moved = json.loads(capsys.readouterr().out)
    assert moved["map_reason"] == "нет правил"
    with pytest.raises(SystemExit):
        _run_cli(["channel", "move", "999", "--unsorted"])


# --- П3: токен не уходит в прокси окружения --------------------------------------------------------------


async def test_7a_p3_client_ignores_proxy_env(real_core, capsys, monkeypatch):
    _data_dir, config_dir, port = real_core
    _run_cli(["mcp", "setup", "--profile", "px"])
    capsys.readouterr()
    for var in _PROXY_VARS:
        monkeypatch.setenv(var, _DEAD_PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    url = f"http://127.0.0.1:{port}/api/v1"

    # Контроль: клиент, доверяющий окружению, в этих условиях до ядра не доходит — значит тест с зубами.
    async with httpx.AsyncClient(base_url=url, trust_env=True) as naive:
        with pytest.raises(httpx.TransportError):
            await naive.get("/healthz")

    client = HubClient(profile="px", config_dir=config_dir, base_url=url)
    try:
        projects = await client.get("/projects")
    finally:
        await client.aclose()
    assert projects == []


# --- Тест 15: сквозной stdio --------------------------------------------------------------------------------


def _reader(stream, sink: queue.Queue) -> None:
    for raw in iter(stream.readline, b""):
        sink.put(raw)
    sink.put(None)


def _next_line(sink: queue.Queue, timeout: float = 15.0) -> bytes:
    line = sink.get(timeout=timeout)
    assert line is not None, "сервер закрыл stdout раньше ответа"
    return line


def test_7a_15_stdio_end_to_end_only_jsonrpc_on_stdout(real_core, capsys):
    _data_dir, config_dir, port = real_core
    _run_cli(["mcp", "setup", "--profile", "e2e"])
    capsys.readouterr()
    token = profile_mod.read_token(config_dir, "e2e")

    env = {k: v for k, v in os.environ.items() if k not in ("NO_PROXY", "no_proxy")}
    env.update({var: _DEAD_PROXY for var in _PROXY_VARS})  # П3: и процесс целиком не идёт в прокси
    env["HUB_CONFIG_DIR"] = str(config_dir)
    argv = [
        sys.executable,
        "-c",
        "from hub.cli import main; main()",
        "mcp",
        "--profile",
        "e2e",
        "--url",
        f"http://127.0.0.1:{port}/api/v1",
    ]
    assert not any(token in arg for arg in argv)
    proc = subprocess.Popen(  # noqa: S603
        argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out_lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, out_lines), daemon=True).start()
    stdout_seen: list[bytes] = []

    def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()

    def response(request_id: int) -> dict:
        while True:
            raw = _next_line(out_lines)
            stdout_seen.append(raw)
            message = json.loads(raw)
            if message.get("id") == request_id:
                return message

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        init = response(1)
        assert init["result"]["serverInfo"]["name"] == "hub"
        assert "hub_draft" in init["result"]["instructions"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = response(2)["result"]["tools"]
        # 12 (7a) + 2 (7b) + hub_message, hub_media (Ф10-2) + hub_wait (Ф11-1)
        # + hub_draft_new (Ф12-1) + hub_subscribe, hub_unsubscribe, hub_subscriptions (Ф13-1)
        # + hub_react (Ф15-1) + hub_join, hub_joins (Ф17-1) + hub_leave (Ф17-2) + hub_draft_edits (Ф19-1)
        # + hub_people, hub_person (Ф21-1) + hub_download (Ф22-1) + hub_tg_search, hub_tg_peek (Ф29-1)
        # + hub_stage, hub_stages (Ф34-1) + hub_remind, hub_reminders, hub_remind_done (Ф34-2)
        # + hub_agents (Ф35-1b) + hub_agent_send, hub_agent_inbox (Ф35-2b)
        # + hub_propose_rule, hub_rule_requests (Ф38-2) + hub_role (Ф41-8)
        # + hub_request_session, hub_agent_requests (Ф41-5б) + hub_press, hub_presses (Ф44-1c)
        # + hub_agent_state (Ф50-4) + hub_load (Ф52-1) (hub_cards, hub_card_decide Ф49-1 убраны в Ф66-1)
        # + hub_human, hub_humans, hub_human_cancel (Ф54-1) + hub_ask_owner (Ф56-1б)
        # + hub_inbound_rules (Ф63-1)
        # + hub_group_create, hub_group_members, hub_group_link, hub_group_ops (Ф62-6)
        # + hub_digest (Ф86-1); + hub_mute (Ф99-2)
        expected = 60  # hub_propose_work убран в Ф78-1
        assert len(tools) == expected

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "hub_overview", "arguments": {}},
            }
        )
        call = response(3)["result"]
        text = call["content"][0]["text"]
        assert not call.get("isError"), text
        assert "Хаб (профиль e2e): max_seq=" in text
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()

    while True:
        try:
            raw = out_lines.get(timeout=2)
        except queue.Empty:
            break
        if raw is None:
            break
        stdout_seen.append(raw)

    assert stdout_seen
    for raw in stdout_seen:
        message = json.loads(raw)  # любая посторонняя строка в stdout здесь упала бы
        assert message["jsonrpc"] == "2.0"
    assert token not in stderr
    assert token not in b"".join(stdout_seen).decode()
