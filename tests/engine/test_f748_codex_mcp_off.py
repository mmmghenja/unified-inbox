"""Ф74-8 (живой стенд `.runs/f748-stand/`): у нити app-server Codex с `config.mcp_servers` флаги
`-c mcp_servers.<имя>.enabled=false` теряются — сессия хаба поднимала все серверы config.toml владельца
(проба `.runs/f748-stand/probe_codex_thread_mcp.py`). Выключение — записью `enabled: false` в `config` нити;
выключенные серверы не показываются «✓» в итоге «Перечитать MCP»."""

from hub.engine import codex_process


def _proc(**spec) -> codex_process.CodexProcess:
    base = {
        "binary": "codex",
        "cwd": "/w",
        "env": {},
        "model": "gpt-6.1-sol",
        "effort": "low",
        "system": None,
        "decide": lambda req: None,
        "hook_command": ["true"],
    }
    return codex_process.CodexProcess(codex_process.CodexSpec(**{**base, **spec}), on_message=lambda m: None)


def test_thread_config_disables_user_servers():
    own = {"stand": {"command": "x"}, "hub-all": {"command": "hub"}}
    proc = _proc(mcp_servers=own, disable_mcp=["ru-edit", "a.b", "hub-all"])
    servers = proc._thread_config()["mcp_servers"]  # noqa: SLF001
    assert servers["ru-edit"] == {"enabled": False}
    assert servers["a.b"] == {"enabled": False}  # флагом не выразить — нитью можно
    assert servers["hub-all"] == {"command": "hub"}  # свой сервер сессии не выключается
    assert servers["stand"] == {"command": "x"}
    assert proc.spec.mcp_servers == own  # набор сессии не тронут
    # Своих серверов нет, а выключать есть что — записи всё равно идут в нить.
    assert _proc(disable_mcp=["searxng"])._thread_config()["mcp_servers"] == {"searxng": {"enabled": False}}  # noqa: SLF001
    assert "mcp_servers" not in _proc()._thread_config()  # noqa: SLF001


class _Rpc:
    async def call(self, method, params=None, *_wait):
        assert method == "mcpServerStatus/list"
        return {"data": [{"name": "stand"}, {"name": "ru-edit"}, {"name": "hub-all"}]}


async def test_disabled_servers_not_listed_as_ready():
    proc = _proc(mcp_servers={"stand": {}, "hub-all": {}}, disable_mcp=["ru-edit"])
    proc._rpc = _Rpc()  # noqa: SLF001
    proc._mcp = {"stand": "ready", "hub-all": "ready"}  # noqa: SLF001
    names = [s["name"] for s in await proc._mcp_list()]  # noqa: SLF001
    assert names == ["hub-all", "stand"]
