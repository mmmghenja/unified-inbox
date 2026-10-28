"""Ф16-4 (docs/architecture.md): сессия OpenCode — `find_opencode_session`. `/proc`
поддельный (как в `test_session_registry.py`), каталог сессии — настоящий во временной папке."""

import pytest

from hub import hostid
from hub import session_registry as reg


class Tree:
    """Поддельный `/proc`: `add(pid, ppid, start, comm)` (как в `test_session_registry.py`)."""

    def __init__(self, tmp) -> None:
        self.proc = tmp / "proc"
        self.proc.mkdir()

    def add(self, pid: int, ppid: int, start: int, comm: str = "bash") -> None:
        fields = ["S", str(ppid), *["0"] * 17, str(start), *["0"] * 10]
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")


OC_PID, OC_START = 100, 5000
SID = "ses_2a9c1f0e7ffeAbCdEf1234"


@pytest.fixture
def tree(tmp_path):
    t = Tree(tmp_path)
    t.add(1, 0, 1, "systemd")
    t.add(OC_PID, 1, OC_START, "opencode")
    t.add(200, OC_PID, 6000, "hub")  # MCP `hub mcp` или ожидатель плагина: ppid == OPENCODE_PID
    t.add(300, 200, 7000, "python")
    t.add(400, 1, 8000, "bash")  # чужая ветка
    return t


def _find(tree, start, sid=SID, oc_dir=None, env=None):
    return reg.find_opencode_session(
        start,
        sid,
        str(tree.proc.parent) if oc_dir is None else oc_dir,
        environ={"OPENCODE_PID": str(OC_PID)} if env is None else env,
        proc_root=tree.proc,
    )


def test_ancestor_with_opencode_pid(tree):
    info = _find(tree, 300)
    assert info == reg.SessionInfo(
        pid=OC_PID,
        proc_start=OC_START,
        session_id=SID,
        cwd=str(tree.proc.parent),
        name=None,
        name_source=None,
        kind="opencode",
        host=hostid.host_id({}),  # Ф33-1: машина сессии
        boot=hostid.boot_id({}),  # Ф33-1: её загрузка
    )
    assert _find(tree, 200) == info  # прямой ребёнок OpenCode
    assert _find(tree, OC_PID) == info  # сам start_pid (П15)
    assert info.session_in()["kind"] == "opencode"


def test_not_an_ancestor_is_gone(tree):
    with pytest.raises(reg.RegistryError) as exc:
        _find(tree, 400)
    assert exc.value.kind == reg.GONE


@pytest.mark.parametrize(
    "sid", [None, "", "sess-1", "ses_short", "ses_" + "a" * 65, "ses_bad/../x", "0f6f6d6a"]
)
def test_bad_oc_session_names_plugin(tree, sid):
    with pytest.raises(reg.RegistryError) as exc:
        _find(tree, 300, sid=sid)
    assert exc.value.kind == reg.GONE and "плагин хаба" in exc.value.message
    assert "opencode-hub-install.sh" in exc.value.message


@pytest.mark.parametrize("oc_dir", ["relative/dir", "/nonexistent/hub-oc-test", ""])
def test_missing_dir_is_gone(tree, oc_dir):
    with pytest.raises(reg.RegistryError) as exc:
        _find(tree, 300, oc_dir=oc_dir)
    assert exc.value.kind == reg.GONE


@pytest.mark.parametrize("env", [{}, {"OPENCODE_PID": ""}, {"OPENCODE_PID": "abc"}, {"OPENCODE_PID": "0"}])
def test_no_opencode_pid_is_gone(tree, env):
    with pytest.raises(reg.RegistryError) as exc:
        _find(tree, 300, env=env)
    assert exc.value.kind == reg.GONE


def test_deeper_than_eight_steps_is_gone(tmp_path):
    t = Tree(tmp_path)
    t.add(1, 0, 1, "systemd")
    t.add(OC_PID, 1, OC_START, "opencode")
    parent = OC_PID
    for pid in range(1000, 1010):  # 10 уровней ниже OpenCode
        t.add(pid, parent, pid)
        parent = pid
    with pytest.raises(reg.RegistryError):
        _find(t, 1009)
    assert _find(t, 1007).pid == OC_PID  # 8 шагов вверх — найден


def test_in_opencode():
    assert reg.in_opencode({"OPENCODE_PID": "123"})
    assert not reg.in_opencode({})
    assert not reg.in_opencode({"OPENCODE": "1"})
