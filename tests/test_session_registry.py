"""Ф13-1 C: кто сессия Claude Code — реестр `<claude_dir>/sessions/<pid>.json` и `/proc/<pid>/stat`. Каталог
`/proc` и реестр — во временной папке (настоящие `/proc` и `~/.claude/sessions` не читаются)."""

import json
from pathlib import Path

import pytest

from hub import hostid
from hub import session_registry as reg


class Tree:
    """Поддельный `/proc` и каталог Claude Code: `proc(pid, ppid, start, comm)`, `entry(pid, **поля)`."""

    def __init__(self, tmp: Path) -> None:
        self.proc = tmp / "proc"
        self.claude = tmp / "claude"
        (self.claude / "sessions").mkdir(parents=True)
        self.proc.mkdir()

    def add(self, pid: int, ppid: int, start: int, comm: str = "bash") -> None:
        fields = ["S", str(ppid), *["0"] * 17, str(start), *["0"] * 10]
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")

    def entry(self, pid: int, /, **fields) -> Path:
        data = {
            "pid": pid,
            "sessionId": "sess-1",
            "cwd": "/work/iip",
            "procStart": 5000,
            "name": "МАРКЕТОЛОГ",
            "nameSource": "user",
            "kind": "interactive",
            "status": "idle",
        }
        data.update(fields)
        path = self.claude / "sessions" / f"{pid}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def find(self, start_pid: int, sleep=None):
        sleeps: list[float] = []
        info = reg.find_session(
            start_pid,
            proc_root=self.proc,
            claude_config=self.claude,
            sleep=sleep or sleeps.append,
        )
        return info


@pytest.fixture
def tree(tmp_path):
    t = Tree(tmp_path)
    t.add(1, 0, 1, "systemd")
    t.add(100, 1, 5000, "claude")
    t.add(200, 100, 6000, "bash")
    t.add(300, 200, 7000, "hub")
    return t


def test_walks_up_ppid_to_claude_and_reads_entry(tree):
    tree.entry(100)
    info = tree.find(200)
    assert info == reg.SessionInfo(
        pid=100,
        proc_start=5000,
        session_id="sess-1",
        cwd="/work/iip",
        name="МАРКЕТОЛОГ",
        name_source="user",
        kind="interactive",
        host=hostid.host_id(),  # Ф33-1: машина сессии
        boot=hostid.boot_id(),  # Ф33-1: её загрузка
    )
    assert tree.find(300) == info  # CLI: hub -> bash -c -> claude
    host = {"host": info.host} if info.host else {}
    host |= {"boot": info.boot} if info.boot else {}
    assert info.session() == {"pid": 100, "proc_start": 5000, "session_id": "sess-1", **host}
    assert info.session_in() == {
        "pid": 100,
        "proc_start": 5000,
        "session_id": "sess-1",
        **host,
        "cwd": "/work/iip",
        "name": "МАРКЕТОЛОГ",
        "name_source": "user",
        "kind": "interactive",
    }


def test_name_sent_as_is_but_cut_to_64(tree):
    """Уточнение B5 от ядра: имя и источник — как в реестре («только от /rename» решает ядро), имя длиннее 64
    ядро отвергает 422 — клиент обрезает."""
    tree.entry(100, name="auto-title", nameSource="auto")
    info = tree.find(200)
    assert info.session_in()["name"] == "auto-title" and info.session_in()["name_source"] == "auto"
    tree.entry(100, name="x" * 200)
    assert tree.find(200).session_in()["name"] == "x" * reg.NAME_MAX == "x" * 64
    tree.entry(100, name=None, nameSource=None)
    assert tree.find(200).session_in()["name"] is None


def test_comm_with_parens_and_spaces_is_parsed_after_last_paren(tmp_path):
    t = Tree(tmp_path)
    t.add(100, 1, 5000, "cl) (aude x")
    t.entry(100)
    assert t.find(100).pid == 100
    assert reg.proc_stat(100, t.proc) == (1, 5000)


def test_stale_entry_with_other_starttime_is_gone(tree):
    """Файл остался от прежнего процесса с тем же pid — сессии нет (после повторов)."""
    tree.entry(100, procStart=4999)
    sleeps: list[float] = []
    with pytest.raises(reg.RegistryError) as exc:
        tree.find(200, sleep=sleeps.append)
    assert exc.value.kind == reg.GONE


def test_no_session_among_ancestors_is_gone(tree):
    """`kill -9` claude: процесса нет, файл реестра остался, `hub` переподвешен к pid 1."""
    tree.entry(100)
    (tree.proc / "100" / "stat").unlink()
    (tree.proc / "100").rmdir()
    sleeps: list[float] = []
    with pytest.raises(reg.RegistryError) as exc:
        tree.find(1, sleep=sleeps.append)
    assert exc.value.kind == reg.GONE
    assert sleeps == [reg.RETRY_DELAY_S] * reg.RETRIES


def test_broken_json_once_or_twice_is_retried(tree):
    path = tree.entry(100)
    good = path.read_text()
    path.write_text("{битый")
    calls: list[float] = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            path.write_text(good)  # Claude Code дописал файл

    assert tree.find(200, sleep=sleep).session_id == "sess-1"
    assert calls == [0.2, 0.2]


def test_missing_file_for_a_moment_is_retried(tree):
    path = tree.entry(100)
    good = path.read_text()
    path.unlink()
    calls: list[float] = []

    def sleep(seconds):
        calls.append(seconds)
        path.write_text(good)

    assert tree.find(200, sleep=sleep).pid == 100
    assert calls == [0.2]


def test_persistently_broken_is_unreadable(tree):
    tree.entry(100).write_text("{битый")
    sleeps: list[float] = []
    with pytest.raises(reg.RegistryError) as exc:
        tree.find(200, sleep=sleeps.append)
    assert exc.value.kind == reg.UNREADABLE
    assert len(sleeps) == reg.RETRIES == 3
    for fields in ({"sessionId": None}, {"cwd": ""}, {"kind": 5}, {"pid": 101}, {"sessionId": "s" * 129}):
        tree.entry(100, **fields)
        with pytest.raises(reg.RegistryError) as exc:
            tree.find(200, sleep=lambda _s: None)
        assert exc.value.kind == reg.UNREADABLE, fields


def test_walk_is_bounded(tmp_path):
    t = Tree(tmp_path)
    t.add(1, 0, 1)
    for pid in range(2, 14):
        t.add(pid, pid - 1, 10 + pid)
    t.entry(2, procStart=12)  # 11 шагов вверх от 13 — дальше 8
    with pytest.raises(reg.RegistryError) as exc:
        t.find(13, sleep=lambda _s: None)
    assert exc.value.kind == reg.GONE
    assert t.find(10, sleep=lambda _s: None).pid == 2  # 8 шагов — ещё находится


def test_claude_config_dir_and_work_env():
    assert reg.claude_dir({"CLAUDE_CONFIG_DIR": "/x/y"}) == Path("/x/y")
    assert reg.claude_dir({}) == Path.home() / ".claude"
    assert reg.in_work_session({"HUB_WORK": "1"})
    assert not reg.in_work_session({})
    assert not reg.in_work_session({"HUB_WORK": ""})
