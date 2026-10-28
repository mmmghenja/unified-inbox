"""Ф50-7, гард (docs/architecture.md и поправки П4, П7, П8, П9): сессия хаба работает
в подпапке проекта — `--home-dir`. Граница проекта одна (`--workdir`); своя память — слаг проекта, слаг папки
процесса и слаг верха её git-репозитория; чужая папка гард не расширяет; настройки Claude Code в папке запуска
сессия себе не создаёт; вложенный репозиторий — обычное основное дерево.

Раскладка: проект (git) с `proekty/z1` (чистая папка заказа), `proekty/z2/kod` (вложенный git), `proekty/z3`
(соседний заказ), `roli/`."""

import io
import json
import os
from pathlib import Path

import pytest

from hub.work import guard

from .conftest import make_repo


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    root = make_repo(tmp_path / "project")
    for sub in ("proekty/z1", "proekty/z3", "roli"):
        (root / sub).mkdir(parents=True)
    kod = make_repo(root / "proekty/z2/kod")
    (kod / "src/app").mkdir(parents=True)
    # Временный каталог тестов лежит в `/tmp` — там запись открыта всем; «вне проекта» — путь не оттуда
    # (гард по нему только сверяет пути, ничего не создаётся).
    outside = Path("/opt/hub-f507-outside")
    return {"home": home, "root": root, "z1": root / "proekty/z1", "kod": kod, "outside": outside}


def _hook(box, tool, tool_input, *, home_dir=None, cwd="", args=None):
    """`(вердикт, ключ правила)`: `pass` / `ask` / `deny`; `cwd` — папка события (`None` — без неё)."""
    event = {"tool_name": tool, "tool_input": tool_input, "session_id": "s"}
    if cwd is not None:
        event["cwd"] = str(cwd or home_dir or box["root"])
    argv = ["--workdir", str(box["root"]), "--profile", "held"] if args is None else list(args)
    if home_dir is not None:
        argv += ["--home-dir", str(home_dir)]
    code, out, _err = guard.run(argv, io.StringIO(json.dumps(event)), home=str(box["home"]))
    if code == 2:
        return "deny", None
    if not out:
        return "pass", None
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    key = reason.rsplit(guard.RULE_MARK, 1)[1].rstrip("]") if guard.RULE_MARK in reason else None
    return "ask", key


def _write(box, path, **kw):
    return _hook(box, "Write", {"file_path": str(path), "content": "x"}, **kw)


def _bash(box, command, **kw):
    return _hook(box, "Bash", {"command": command}, **kw)


def _memory(box, folder) -> str:
    return str(box["home"] / ".claude/projects" / guard.memory_slug(str(folder)) / "memory/note.md")


# --- граница проекта — одна (Р6) ----------------------------------------------------------------------------


def test_border_is_the_project_not_the_subdir(box):
    z1, root = box["z1"], box["root"]
    for kw in ({"home_dir": z1}, {"cwd": z1}):  # с `--home-dir` и по старым настройкам без него
        assert _write(box, root / "proekty/z3/zametka.md", **kw) == ("pass", None)  # соседний заказ
        assert _write(box, "../z3/zametka.md", **kw) == ("pass", None)  # относительный — от папки события
        assert _write(box, root / "roli/novaya.md", **kw) == ("pass", None)
        assert _bash(box, "echo x > ../../roli/novaya.md", **kw) == ("pass", None)
        assert _write(box, box["outside"] / "x.md", **kw) == ("ask", "outside_workdir")
        assert _bash(box, f"echo x > {box['outside']}/x.md", **kw) == ("ask", "outside_workdir")
        up = "../" * (len(z1.parts) - 1)
        assert _write(box, f"{up}opt/hub-f507-outside/x.md", **kw) == ("ask", "outside_workdir")


# --- своя память (Р6, П7) -----------------------------------------------------------------------------------


def test_memory_of_the_process_folder_is_own(box):
    z1, root = box["z1"], box["root"]
    assert _write(box, _memory(box, z1), home_dir=z1) == ("pass", None)
    assert _bash(box, f"echo x >> {_memory(box, z1)}", home_dir=z1) == ("pass", None)
    assert _write(box, _memory(box, root), home_dir=z1) == ("pass", None)  # память проекта — как была
    # Память третьей папки (соседний заказ, чужой проект) — карточка.
    assert _write(box, _memory(box, root / "proekty/z3"), home_dir=z1) == ("ask", "foreign_memory")
    assert _write(box, _memory(box, box["outside"]), home_dir=z1) == ("ask", "foreign_memory")
    # Без `--home-dir` (старые настройки, сессия без подпапки) память подпапки — чужая, как раньше.
    assert _write(box, _memory(box, z1), cwd=z1) == ("ask", "foreign_memory")


def test_memory_of_the_nested_repo_top_is_own(box):
    """П7 (проба 2): Claude Code держит память в слаге верха git-репозитория папки процесса."""
    kod = box["kod"]
    deep = kod / "src/app"
    assert _write(box, _memory(box, kod), home_dir=deep) == ("pass", None)
    assert _write(box, _memory(box, deep), home_dir=deep) == ("pass", None)
    # Промежуточная папка без своего репозитория и соседняя — не свои.
    assert _write(box, _memory(box, kod / "src"), home_dir=deep) == ("ask", "foreign_memory")
    assert _write(box, _memory(box, box["root"] / "proekty/z2"), home_dir=deep) == ("ask", "foreign_memory")
    # У чистой подпапки верх репозитория — сам проект: его слаг свой и без того, лишнего не добавилось.
    assert _write(box, _memory(box, box["root"] / "proekty"), home_dir=box["z1"]) == ("ask", "foreign_memory")


def test_home_dir_outside_the_project_is_ignored(box):
    """Р6, П8: значение принимается только внутри `--workdir` по настоящим путям обеих сторон."""
    outside, root = box["outside"], box["root"]
    assert _write(box, _memory(box, outside), home_dir=outside) == ("ask", "foreign_memory")
    assert _write(box, outside / "x.md", home_dir=outside) == ("ask", "outside_workdir")
    # Ссылка внутри проекта, ведущая наружу: написанный путь — внутри, настоящий — нет.
    link = root / "ssylka"
    os.symlink(outside, link)
    assert _write(box, _memory(box, link), home_dir=link) == ("ask", "foreign_memory")
    assert _write(box, _memory(box, outside), home_dir=link) == ("ask", "foreign_memory")
    assert _write(box, link / "x.md", home_dir=link) == ("ask", "outside_workdir")
    # Ссылка внутри проекта на папку внутри проекта — принимается (оба слага свои).
    inner = root / "korotko"
    os.symlink(box["z1"], inner)
    assert _write(box, _memory(box, box["z1"]), home_dir=inner) == ("pass", None)
    assert _write(box, _memory(box, inner), home_dir=inner) == ("pass", None)
    # Папка проекта как `--home-dir` — ничего не меняет.
    assert _write(box, _memory(box, box["z1"]), home_dir=root) == ("ask", "foreign_memory")
    assert _write(box, _memory(box, root), home_dir=root) == ("pass", None)


def test_arguments_old_settings_work_and_bad_ones_fail(box):
    """П9: файл настроек без `--home-dir` работает как раньше; негодный аргумент — сбой (карточка)."""
    root = box["root"]
    old = ["--workdir", str(root), "--profile", "held"]
    assert _write(box, root / "x.md", args=old) == ("pass", None)
    assert guard._parse_args(old) == (str(root), None, None, None)
    assert guard._parse_args([*old, "--home-dir", str(box["z1"])])[3] == str(box["z1"])
    assert _write(box, root / "x.md", args=[*old, "--home-dir", "proekty/z1"]) == ("ask", None)
    assert _write(box, root / "x.md", args=[*old, "--home-dir"]) == ("ask", None)
    # Ф78-1: профиль один — флаг `--profile held` в старых настройках не обязателен.
    new = ["--workdir", str(root), "--home-dir", str(box["z1"])]
    assert guard._parse_args(new) == (str(root), None, None, str(box["z1"]))
    assert _write(box, root / "x.md", args=new) == ("pass", None)


# --- запасная папка события (П9) ----------------------------------------------------------------------------


def test_event_without_cwd_counts_from_the_process_folder(box):
    z1 = box["z1"]
    # Относительная цель без `cwd` события: с `--home-dir` — от папки процесса (её `.mcp.json` — настройки
    # сессии), без него — от папки проекта (там это настройки агентов проекта).
    assert _write(box, ".mcp.json", home_dir=z1, cwd=None) == ("ask", "project_rules")
    assert _write(box, ".mcp.json", cwd=None) == ("ask", "agent_config")
    up = "../" * (len(z1.parts) - 1)
    assert _write(box, f"{up}opt/hub-f507-outside/x.md", home_dir=z1, cwd=None) == ("ask", "outside_workdir")
    assert _write(box, "../z3/x.md", home_dir=z1, cwd=None) == ("pass", None)


# --- настройки Claude Code в папке запуска (П4) -------------------------------------------------------------


def test_session_does_not_make_its_own_settings(box, free_sets):
    z1, root = box["z1"], box["root"]
    for name in (".claude/settings.json", ".claude/settings.local.json", ".claude/agents/a.md"):
        assert _write(box, z1 / name, home_dir=z1) == ("ask", "project_rules")
    # `.mcp.json` в папке запуска и выше неё внутри проекта — то же правило, что `.claude/` (не «настройки
    # агентов проекта»): снятый выключатель `agent_config` его не открывает.
    assert _write(box, z1 / ".mcp.json", home_dir=z1) == ("ask", "project_rules")
    assert _write(box, root / "proekty/.mcp.json", home_dir=z1) == ("ask", "project_rules")
    assert _bash(box, "echo '{}' > .mcp.json", home_dir=z1) == ("ask", "project_rules")
    free_sets.sets[7] = ["agent_config"]
    args = ["--workdir", str(root), "--profile", "held", *free_sets.args(7)]
    assert _write(box, z1 / ".mcp.json", home_dir=z1, args=args) == ("ask", "project_rules")
    assert _write(box, root / "proekty/z3/.mcp.json", home_dir=z1, args=args) == ("pass", None)
    # Вне цепочки (соседний заказ, корень проекта) и без `--home-dir` — прежнее правило настроек агентов.
    assert _write(box, root / "proekty/z3/.mcp.json", home_dir=z1) == ("ask", "agent_config")
    assert _write(box, root / ".mcp.json", home_dir=z1) == ("ask", "agent_config")
    assert _write(box, z1 / ".mcp.json", cwd=z1) == ("ask", "agent_config")


# --- вложенный репозиторий (Р6) -----------------------------------------------------------------------------


def test_nested_repo_is_a_main_tree(box):
    kod = box["kod"]
    for kw in ({"home_dir": kod}, {"home_dir": box["z1"], "cwd": kod}):
        assert _bash(box, "git reset --hard", **kw) == ("ask", "main_tree_git")
        assert _bash(box, "git checkout -b x", **kw) == ("ask", "main_tree_git")
        assert _bash(box, "git commit -am pravka", **kw) == ("pass", None)
        assert _bash(box, "git worktree add ../wt -b fix", **kw) == ("pass", None)
        assert _write(box, kod / "app.py", **kw) == ("pass", None)  # правка в основном дереве — как у проекта


# --- собственная проверка хоста (П9) ------------------------------------------------------------------------


def test_decide_takes_home_dir_directly(box):
    """Хост зовёт `guard.decide` сам (`_self_check`, `_oc_decide`) — тем же аргументом."""
    z1 = box["z1"]
    event = {"tool_name": "Write", "tool_input": {"file_path": _memory(box, z1)}, "cwd": str(z1)}
    kw = {"workdir": str(box["root"]), "home": str(box["home"])}
    guard.decide(event, **kw, home_dir=str(z1))
    with pytest.raises(guard.Verdict) as caught:
        guard.decide(event, **kw)
    assert caught.value.key == "foreign_memory"
    with pytest.raises(guard.Verdict):
        guard.decide(event, **kw, home_dir=str(box["outside"]))
