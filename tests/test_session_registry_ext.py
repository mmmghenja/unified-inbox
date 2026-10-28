"""Ф74-2 (docs/architecture.md; план 6.2 Б1): личность внешнего Codex/Cursor по
предку процесса. `/proc` поддельный (как в `test_session_registry_oc.py`), но с `cmdline` и `comm`: Codex
узнаётся по `comm`, `cursor-agent` — по `argv[0]` (у него `comm` — `MainThread`, `exe` — свой `node`)."""

import json

import pytest

from hub import hostid
from hub import session_registry as reg
from hub.engine import registry as engine_registry


class Tree:
    def __init__(self, tmp) -> None:
        self.proc = tmp / "proc"
        self.proc.mkdir()

    def add(self, pid: int, ppid: int, start: int, comm: str = "bash", argv0: str | None = None) -> None:
        fields = ["S", str(ppid), *["0"] * 17, str(start), *["0"] * 10]
        d = self.proc / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm[:15]}) " + " ".join(fields) + "\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes((argv0 or comm).encode() + b"\0--flag\0")


CX_PID, CX_START = 100, 5000
CU_PID, CU_START = 110, 5100
CLAUDE_PID, CLAUDE_START = 50, 4000
CONV = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"


@pytest.fixture
def tree(tmp_path):
    t = Tree(tmp_path)
    t.add(1, 0, 1, "systemd")
    t.add(CLAUDE_PID, 1, CLAUDE_START, "claude")  # Claude Code, из которого запустили Codex
    t.add(CX_PID, CLAUDE_PID, CX_START, "codex", "/home/u/.local/share/codex/0.163.0/bin/codex")
    t.add(200, CX_PID, 6000, "hub")  # MCP-сервер хаба — потомок Codex
    t.add(CU_PID, 1, CU_START, "MainThread", "/home/u/.local/bin/cursor-agent")
    t.add(210, CU_PID, 6100, "hub")
    t.add(300, 1, 7000, "bash")  # чужая ветка
    return t


def _claude_registry(tmp_path):
    sessions = tmp_path / "claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{CLAUDE_PID}.json").write_text(
        json.dumps(
            {"pid": CLAUDE_PID, "procStart": CLAUDE_START, "sessionId": "claude-uuid", "cwd": "/"}
            | {"kind": "x"}
        )
    )
    return tmp_path / "claude"


def test_codex_ancestor_by_comm_cursor_by_argv0(tree):
    assert reg.ext_ancestor(200, tree.proc) == ("codex", CX_PID, CX_START)
    assert reg.ext_ancestor(210, tree.proc) == ("cursor", CU_PID, CU_START)
    assert reg.ext_ancestor(300, tree.proc) is None


def test_find_ext_session_identity(tree, tmp_path):
    info = reg.find_ext_session(200, CONV, str(tmp_path), environ={}, proc_root=tree.proc)
    assert info == reg.SessionInfo(
        pid=CX_PID,
        proc_start=CX_START,
        session_id=f"codex:{CONV}",
        cwd=str(tmp_path),
        name=None,
        name_source=None,
        kind="codex",
        host=hostid.host_id({}),
        boot=hostid.boot_id({}),
    )
    cursor = reg.find_ext_session(210, CONV, str(tmp_path), environ={}, proc_root=tree.proc)
    assert (cursor.pid, cursor.kind, cursor.session_id) == (CU_PID, "cursor", f"cursor:{CONV}")
    assert reg.plugin_waits(info) and reg.plugin_waits(cursor)


def test_no_engine_ancestor_is_ext_absent(tree, tmp_path):
    with pytest.raises(reg.RegistryError) as err:
        reg.find_ext_session(300, CONV, str(tmp_path), environ={}, proc_root=tree.proc)
    assert err.value.ext_absent and err.value.kind == reg.GONE


@pytest.mark.parametrize(
    ("pid", "installer"), [(200, "codex-hub-install.sh"), (210, "cursor-hub-install.sh")]
)
def test_without_hook_args_names_installer(tree, tmp_path, pid, installer):
    for bad in (None, "", "x" * 3, "a/b" * 5):
        with pytest.raises(reg.RegistryError) as err:
            reg.find_ext_session(pid, bad, str(tmp_path), environ={}, proc_root=tree.proc)
        assert installer in err.value.message and not err.value.ext_absent


def test_dir_must_exist_and_be_absolute(tree, tmp_path):
    for bad in (None, "rel/dir", str(tmp_path / "missing")):
        with pytest.raises(reg.RegistryError) as err:
            reg.find_ext_session(200, CONV, bad, environ={}, proc_root=tree.proc)
        assert "каталог сессии Codex" in err.value.message


def test_explicit_ext_pid_must_be_engine_process(tree, tmp_path):
    # Ожидатель хука Codex отвязан от дерева процессов: процесс движка называется явно.
    info = reg.find_ext_session(300, CONV, str(tmp_path), ext_pid=CX_PID, environ={}, proc_root=tree.proc)
    assert (info.pid, info.proc_start, info.kind) == (CX_PID, CX_START, "codex")
    for wrong in (300, 999, 0, -5):
        with pytest.raises(reg.RegistryError) as err:
            reg.find_ext_session(1, CONV, str(tmp_path), ext_pid=wrong, environ={}, proc_root=tree.proc)
        assert not err.value.ext_absent


def test_claude_walk_stops_at_nearest_engine(tree, tmp_path):
    """Claude Code запустил Codex: MCP-сервер под Codex — сессия Codex, а не того Claude Code (ближайший
    движок решает); сам Claude Code по-прежнему находится от своих потомков."""
    claude = _claude_registry(tmp_path)
    with pytest.raises(reg.RegistryError) as err:
        reg.find_session(200, proc_root=tree.proc, claude_config=claude, sleep=lambda _s: None)
    assert err.value.kind == reg.GONE
    tree.add(400, CLAUDE_PID, 8000, "hub")  # MCP-сервер прямо под Claude Code
    info = reg.find_session(400, proc_root=tree.proc, claude_config=claude, sleep=lambda _s: None)
    assert info.session_id == "claude-uuid"


def test_profile_ext_helpers():
    assert engine_registry.EXTERNAL == ("cursor", "codex")
    assert engine_registry.ext_engine_of("codex:abc") == "codex"
    assert engine_registry.ext_engine_of("cursor:abc") == "cursor"
    assert engine_registry.ext_engine_of("claude:abc") is None
    assert engine_registry.ext_engine_of("codex:") is None
    assert engine_registry.ext_engine_of("ses_abc") is None
    assert engine_registry.ext_engine_by_binary(["node", "MainThread", "cursor-agent"]) == "cursor"
    assert engine_registry.ext_engine_by_binary(["python3", "claude"]) is None
