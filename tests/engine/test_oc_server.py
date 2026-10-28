"""`hub.engine.oc_server`: старт, готовность, окружение, остановка группы, клиент (Ф16, 3.3, F1, F9, F18)."""

import asyncio
import os

import httpx
import pytest

from hub.engine.oc_server import EngineStartError, OcServer, SessionNotFound


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            state = f.read().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return False
    return state != "Z"


async def _gone(pid: int, wait_s: float = 3.0) -> bool:
    for _ in range(int(wait_s / 0.05)):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def test_start_ready_and_env_filtered(fake_oc):
    fake_oc.scenario()
    server = await fake_oc.start()
    assert server.alive()
    env = fake_oc.json("env.json")
    assert env["OPENCODE_CONFIG"] == str(fake_oc.config_path)
    assert env["OPENCODE_SERVER_PASSWORD"] == server.password != "чужой-пароль"
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    leaked = [k for k in env if k.startswith(("ANTHROPIC_", "CLAUDE"))]
    assert leaked == []
    assert "OPENCODE_PERMISSION" not in env and "OPENCODE_CONFIG_CONTENT" not in env
    assert env["FAKE_OC_DIR"] == str(fake_oc.dir)
    assert fake_oc.json("config.json") == fake_oc.config()
    # Готовность проверялась с `directory` и паролем; без пароля — 401.
    calls = fake_oc.jsonl("calls.jsonl")
    assert calls[0]["path"] == "/agent" and calls[0]["query"]["directory"] == str(fake_oc.workdir)
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{server.base_url}/agent", params={"directory": str(fake_oc.workdir)})
        assert r.status_code == 401
    log_mode = os.stat(server.log_file).st_mode & 0o777
    assert log_mode == 0o600


async def test_start_timeout_when_not_listening(fake_oc):
    fake_oc.scenario(no_listen=True)
    with pytest.raises(EngineStartError) as err:
        await fake_oc.start(ready_timeout_s=1.0)
    text = str(err.value)
    assert "не ответил за 1 с" in text
    assert "не слушаю порт" in text  # хвост журнала сервера
    pid = fake_oc.json("pids.json")["server"]
    assert await _gone(pid)
    assert fake_oc.config()  # конфиг не менялся


async def test_start_fails_fast_when_process_exits(fake_oc):
    fake_oc.scenario(exit_rc=3)
    with pytest.raises(EngineStartError, match="(?s)кодом 3.*падаю по сценарию"):
        await fake_oc.start(ready_timeout_s=10.0)


async def test_start_missing_binary(tmp_path):
    server = OcServer(str(tmp_path / "нет-такого"), log_file=tmp_path / "l.log")
    with pytest.raises(EngineStartError, match="не запустился"):
        await server.start(str(tmp_path), {}, tmp_path / "c.json")


@pytest.mark.parametrize("setsid", [False, True], ids=["group", "own-session"])
async def test_stop_kills_group_and_grandchild(fake_oc, setsid):
    fake_oc.scenario(**({"grandchild_setsid": True} if setsid else {"grandchild": True}))
    server = await fake_oc.start()
    pids = fake_oc.json("pids.json")
    assert _alive(pids["server"]) and _alive(pids["grandchild"])
    await server.stop(grace_s=1)
    assert not server.alive()
    # Внук в своей сессии killpg не достаёт — его добивает обход /proc.
    assert await _gone(pids["grandchild"])
    await server.stop()  # повторно — без эффекта


async def test_client_sessions_messages_status_and_404(fake_oc):
    fake_oc.scenario(turns=[[{"text": "привет"}]])
    server = await fake_oc.start()
    async with server.client() as client:
        info = await client.create_session("hub-test", agent="hub-test")
        sid = info["id"]
        assert sid.startswith("ses_")
        assert await client.status(sid) == "idle"
        assert (await client.get_session(sid))["id"] == sid
        assert await client.get_session("ses_nope") is None
        with pytest.raises(SessionNotFound):
            await client.prompt("ses_nope", text="x")
        await client.prompt(
            sid,
            text="hi",
            system="правила",
            agent="hub-test",
            model=("deepseek", "deepseek-flash"),
            variant="max",
        )
        for _ in range(100):
            msgs = await client.messages(sid)
            if any(
                m["info"].get("time", {}).get("completed") for m in msgs if m["info"]["role"] == "assistant"
            ):
                break
            await asyncio.sleep(0.05)
        assert [m["info"]["role"] for m in msgs] == ["user", "assistant"]
        with pytest.raises(ValueError):
            await client.reply_permission("per_x", "always")
    [prompt] = [
        c
        for c in fake_oc.jsonl("calls.jsonl")
        if c["path"].endswith("/prompt_async") and c["body"]["parts"][0]["text"] == "hi"
    ]
    assert prompt["body"] == {
        "parts": [{"type": "text", "text": "hi"}],
        "agent": "hub-test",
        "model": {"providerID": "deepseek", "modelID": "deepseek-flash"},
        "variant": "max",
        "system": "правила",
    }
    assert all(c["query"].get("directory") == str(fake_oc.workdir) for c in fake_oc.jsonl("calls.jsonl"))


async def test_saved_permissions_warns(fake_oc, caplog):
    fake_oc.scenario(saved_permissions=[{"id": "x", "permission": "bash", "pattern": "*"}])
    with caplog.at_level("WARNING", logger="hub.engine.oc_server"):
        await fake_oc.start()
    assert any("сохранённые разрешения" in r.getMessage() for r in caplog.records)


async def test_saved_permissions_wrapped_119(fake_oc, caplog):
    """Ф99-0: OpenCode 1.19 отдаёт `{"data": [...]}` — пустая обёртка не предупреждает, полная — да."""
    from hub.engine.oc_server import _saved_rules

    assert not _saved_rules({"data": []}) and not _saved_rules([]) and not _saved_rules({})
    assert _saved_rules({"data": [{"permission": "bash"}]}) and _saved_rules([{"permission": "bash"}])


async def test_pending_permissions_400_is_empty(fake_oc):
    fake_oc.scenario(turns=[[{"permission_list_400": True}, {"text": "x"}]])
    server = await fake_oc.start()
    async with server.client() as client:
        sid = (await client.create_session("t"))["id"]
        await client.prompt(sid, text="go")
        await asyncio.sleep(0.3)
        assert await client.pending_permissions() == []


def test_server_env_config_dir_only_from_hub(tmp_path):
    """Ф71-3: `OPENCODE_CONFIG_DIR` родителя не проходит; свой — только по `config_dir`."""
    from hub.engine.oc_server import server_env

    parent = {"PATH": "/bin", "OPENCODE_CONFIG_DIR": "/чужой"}
    plain = server_env(parent, config_path=tmp_path / "c.json", password="p")
    assert "OPENCODE_CONFIG_DIR" not in plain
    own = server_env(parent, config_path=tmp_path / "c.json", password="p", config_dir=tmp_path / "rules")
    assert own["OPENCODE_CONFIG_DIR"] == str(tmp_path / "rules") and own["PATH"] == "/bin"
