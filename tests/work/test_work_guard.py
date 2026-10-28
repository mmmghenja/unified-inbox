"""Грубый гард рабочей сессии `hub-work-guard` (Ф9-1a, раздел 2; тесты 11–13
`docs/architecture.md`). Временные git-репо: основное рабочее дерево + связанный `git worktree
add`."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hub.work import guard

from .conftest import git, make_repo

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repos(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "p" / "memory").mkdir(parents=True)
    workdir = tmp_path / "project"
    main = make_repo(workdir)
    linked = main / ".claude" / "worktrees" / "w"
    git("worktree", "add", "-q", "-b", "fix", str(linked), cwd=main)
    (main / "build").mkdir()
    (main / "build" / "out.js").write_text("x")
    return {"home": home, "workdir": workdir, "linked": linked, "tmp": tmp_path}


def _run(repos, tool, tool_input, *, cwd=None, workdir=None):
    event = {
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": str(cwd or repos["workdir"]),
        "session_id": "s",
    }
    code, out, err = guard.run(
        ["--workdir", str(workdir or repos["workdir"])],
        io.StringIO(json.dumps(event)),
        home=str(repos["home"]),
    )
    if code == 2:
        return "deny", err
    if out:
        data = json.loads(out)["hookSpecificOutput"]
        assert data["hookEventName"] == "PreToolUse"
        return data["permissionDecision"], data["permissionDecisionReason"]
    return "pass", ""


def _bash(repos, command, **kw):
    return _run(repos, "Bash", {"command": command}, **kw)[0]


# --- тест 11: Bash-кнопки этапа 1 — ask ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git -C x push origin b",
        "cd x && git push",
        "env GIT_SSH_COMMAND='ssh -i k' git push",
        'bash -c "git push"',
        "ssh host",
        "timeout 5 ssh host",
        "rsync a host:b",
        "scp a host:b",
        "sftp host",
        "sudo -u deploy ssh host uptime",
        "echo ok; ssh host",
        "docker push registry/app:1",
        "kubectl apply -f k.yaml",
        "helm upgrade app chart",
        "bash scripts/trello/trello.sh comment 12 -",
        "scripts/trello/trello.sh move 1 2",
        "gh pr create --fill",
        "gh pr merge 5",
        "gh workflow run deploy.yml",
        "gh release create v1",
        "gh api repos/o/r/issues -X POST",
        "gh api repos/o/r --method=PATCH",
        "./deploy.sh prod",
        "make deploy-stage",
        "yarn run deploy",
        "grep push file",  # принятая грубость — закреплена тестом
    ],
)
def test_11_stage1_buttons_ask(repos, command):
    assert _bash(repos, command) == "ask", command


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -3",
        "git status",
        "git -C sub diff",
        "kubectl get pods",
        "kubectl logs app-1",
        "helm list",
        "gh pr view 5",
        "gh api repos/o/r",
        "bash scripts/trello/trello.sh cards 1",
        "cat ~/.ssh/config",
        "ls -la",
        "npm test",
    ],
)
def test_11_reading_passes(repos, command):
    assert _bash(repos, command) == "pass", command


# --- тест 12: рабочая копия владельца ----------------------------------------------------------------------------


def test_12_main_tree_switches_ask_linked_free(repos):
    main, linked = repos["workdir"], repos["linked"]
    for command in (
        "git checkout -b x",
        "git switch main",
        "git reset --hard HEAD",
        "git stash",
        "git clean -fd",
        "git restore app.py",
    ):
        verdict, reason = _run(repos, "Bash", {"command": command}, cwd=main)
        assert verdict == "ask", command
        assert "основном дереве" in reason and reason.endswith("[hub-rule:main_tree_git]")
    assert _bash(repos, f"git -C {main} checkout -b y", cwd=repos["tmp"]) == "ask"
    assert _bash(repos, f"cd {main} && git checkout -b y", cwd=repos["tmp"]) == "ask"
    assert _bash(repos, "git stash list", cwd=main) == "pass"
    assert _bash(repos, "git reset HEAD~1", cwd=main) == "pass"  # без --hard
    assert _bash(repos, "git checkout -b x", cwd=linked) == "pass"
    assert _bash(repos, f"git -C {linked} reset --hard HEAD", cwd=main) == "pass"


def test_12_edit_in_main_and_linked_is_free(repos):
    """Ф78-1: правка основного дерева запрещалась только задачам по «Го» — у сессии хаба свободна."""
    main, linked = repos["workdir"], repos["linked"]
    assert _run(repos, "Edit", {"file_path": str(main / "app.py")})[0] == "pass"
    assert _run(repos, "Write", {"file_path": str(main / "new_module.py")})[0] == "pass"
    assert _run(repos, "Write", {"file_path": str(main / "build" / "out.js")})[0] == "pass"
    assert _run(repos, "Edit", {"file_path": str(linked / "app.py")})[0] == "pass"
    assert _run(repos, "Write", {"file_path": str(linked / "src" / "a.py")})[0] == "pass"
    assert _run(repos, "Write", {"file_path": "/tmp/scratch/a.txt"})[0] == "pass"  # вне папки проекта
    assert _run(repos, "NotebookEdit", {"notebook_path": str(main / "nb.ipynb")})[0] == "pass"


@pytest.mark.parametrize("broken", ["head", "config"])
def test_12_broken_git_asks(repos, broken):
    main = repos["workdir"]
    if broken == "head":
        (main / ".git" / "HEAD").write_text("garbage\n")  # git зовёт такой репо «не репо»
    else:
        (main / ".git" / "config").write_text("[core\n")
    verdict, reason = _run(repos, "Bash", {"command": "git checkout -b x"}, cwd=main)
    assert verdict == "ask" and "git" in reason and reason.endswith("[hub-rule:guard_failure]")
    assert _run(repos, "Edit", {"file_path": str(main / "app.py")})[0] == "pass"  # правку git не проверяет


def test_12_not_a_repo_is_free(repos, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run(repos, "Edit", {"file_path": str(plain / "a.py")}, workdir=plain)[0] == "pass"


# --- тест 13: правила, настройки, сам хаб -------------------------------------------------------------------


def test_13_rules_and_settings_cards_home_claude_denied(repos):
    main, home, linked = repos["workdir"], repos["home"], repos["linked"]
    assert _run(repos, "Edit", {"file_path": str(main / "CLAUDE.md")})[0] == "ask"
    assert _run(repos, "Write", {"file_path": str(linked / "CLAUDE.md")})[0] == "ask"
    assert _bash(repos, "echo x > .claude/settings.json", cwd=main) == "ask"
    assert _run(repos, "Write", {"file_path": str(home / ".claude" / "hub-rules.md")})[0] == "deny"
    assert _run(repos, "Write", {"file_path": "~/.claude/settings.json"})[0] == "deny"
    assert _bash(repos, "sed -i s/a/b/ AGENTS.md", cwd=main) == "ask"
    assert _bash(repos, "tee .clinerules/x.md < /dev/null", cwd=main) == "ask"
    assert _bash(repos, "cp /tmp/x .opencode/agent.md", cwd=main) == "ask"
    assert _bash(repos, "rm -rf .agents", cwd=main) == "ask"
    assert _bash(repos, f"echo x >> {linked}/.claude/settings.local.json", cwd=main) == "ask"
    # чужая автопамять — карточка; свободно: файлы связанного worktree (.claude/worktrees/<wt>/), чтение
    assert (
        _run(repos, "Write", {"file_path": str(home / ".claude" / "projects" / "p" / "memory" / "a.md")})[0]
        == "ask"
    )
    assert (
        _run(repos, "Write", {"file_path": str(main / ".claude" / "worktrees" / "w" / "src" / "a.py")})[0]
        == "pass"
    )
    assert _bash(repos, "cat CLAUDE.md .claude/settings.json", cwd=main) == "pass"
    assert _bash(repos, "cp CLAUDE.md /tmp/copy.md", cwd=main) == "pass"


def test_13_hub_itself_denied(repos):
    for command in (
        "systemctl --user restart hub-core",
        "systemctl --user stop hub-work.service",
        "systemctl --user disable hub-connector@1.service",
        "bash scripts/hub-deploy.sh",
        "./scripts/hub-units-install.sh",
        "pkill -f claude",
        "killall hub",
        "kill $(pgrep -f 'hub core')",
        f"echo x > {repos['home']}/projects/hub-prod/src/a.py",
        f"git -C {repos['home']}/projects/hub-prod checkout master",
    ):
        assert _bash(repos, command) == "deny", command
    prod_file = repos["home"] / "projects" / "hub-prod" / "x.py"
    assert _run(repos, "Write", {"file_path": str(prod_file)})[0] == "deny"
    assert _bash(repos, "systemctl --user status hub-core") == "pass"
    assert _bash(repos, "systemctl --user restart docker") == "pass"


def test_deny_wins_over_ask_in_one_command(repos):
    assert _bash(repos, "git push && systemctl --user restart hub-core") == "deny"


def test_other_tools_pass_and_bad_input_asks(repos):
    assert _run(repos, "mcp__hub-all__hub_draft", {"channel_id": 1, "text": "x"})[0] == "pass"
    assert _run(repos, "Read", {"file_path": str(repos["workdir"] / "CLAUDE.md")})[0] == "pass"
    code, out, _ = guard.run(
        ["--workdir", str(repos["workdir"])], io.StringIO("не json"), home=str(repos["home"])
    )
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    code, out, _ = guard.run([], io.StringIO("{}"), home=str(repos["home"]))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_log_without_input(repos, tmp_path):
    log = tmp_path / "guard.log"
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push SECRET-ARG"},
        "cwd": str(repos["workdir"]),
    }
    guard.run(
        ["--workdir", str(repos["workdir"]), "--log", str(log)], io.StringIO(json.dumps(event)), home="/x"
    )
    text = log.read_text()
    assert "tool=Bash\task" in text and "SECRET-ARG" not in text
    assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_entry_point_is_stdlib_and_fast(repos):
    """Точка входа поднимается без импорта остального хаба (как `hub-wake-guard`). Ф74-1: кроме профилей
    движков (`hub.engine.registry` — только данные и stdlib; `hub.engine/__init__` ничего не импортирует) и
    разбора патча (`hub.engine.oc_tools` — stdlib, ревью Ф74)."""
    event = {"tool_name": "Bash", "tool_input": {"command": "ssh prod"}, "cwd": str(repos["workdir"])}
    allowed = ("hub.work", "hub.work.guard", "hub.engine", "hub.engine.registry", "hub.engine.oc_tools")
    code = (
        "import sys, json; sys.argv=['g','--workdir',{workdir!r}]; import hub.work.guard as g; "
        f"mods=[m for m in sys.modules if m.startswith('hub.') and m not in {allowed!r}]; "
        "print(json.dumps(mods), file=sys.stderr); g.main()"
    ).format(workdir=str(repos["workdir"]))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert json.loads(proc.stderr.splitlines()[0]) == []


# --- ревью 9a (блокер): ключевые слова оболочки, подстановки, `bash -lc` ------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "for h in app1 app2; do ssh $h 'sudo systemctl restart api'; done",
        "if true; then ssh prod uptime; fi",
        "if false; then :; else scp a h:b; fi",
        "while read h; do scp dump.sql $h:/tmp; done < hosts",
        "until false; do rsync -a x h:y; done",
        "! ssh prod true",
        "{ ssh prod uptime; }",
        "echo `ssh prod cat /etc/hostname`",
        'echo "$(ssh prod uptime)"',
        "bash -lc 'ssh prod uptime'",
        "sh -xc 'sftp host'",
        "bash -ec 'timeout 5 ssh prod uptime'",
        "ls hosts | xargs -I{} ssh {} uptime",
        "cat hosts | xargs -n 1 ssh",
        "builtin command ssh prod",
        "/usr/bin/ssh prod",
    ],
)
def test_review_ssh_after_shell_keywords_ask(repos, command):
    assert _bash(repos, command) == "ask", command


@pytest.mark.parametrize(
    "command",
    ["ssh-keygen -t ed25519", "ssh-add -l", "grep ssh file", "echo 'ssh prod'", "cat ~/.ssh/config"],
)
def test_review_ssh_lookalikes_pass(repos, command):
    assert _bash(repos, command) == "pass", command


@pytest.mark.parametrize(
    "command",
    [
        "if true; then git checkout -b x; fi",
        "bash -lc 'git checkout -b x'",
        "for b in x; do git switch $b; done",
        "echo $(git stash)",
        "echo `git stash`",
        'echo "$(git checkout -b y)"',
    ],
)
def test_review_main_tree_ask_after_shell_keywords(repos, command):
    assert _bash(repos, command, cwd=repos["workdir"]) == "ask", command


def test_review_rules_and_hub_deny_after_shell_keywords(repos):
    home = repos["home"]
    for command in (
        f"for f in a; do rm -f {home}/.claude/hub-rules.md; done",
        "if true; then systemctl --user restart hub-core; fi",
        "if true; then pkill -f claude; fi",
        "bash -lc 'systemctl --user stop hub-work'",
    ):
        assert _bash(repos, command) == "deny", command
    assert _bash(repos, "while true; do echo x > CLAUDE.md; break; done") == "ask"  # правила проекта
    # deny — только точным разбором: упоминание в тексте без выполнения не запрещается
    assert _bash(repos, "echo 'systemctl --user restart hub-core'") == "pass"


# --- Ф41-2б: профиль `held` (постоянная сессия хаба) — таблица 4.2 + П7 -------------------------------------


def _held(repos, tool, tool_input, *, cwd=None):
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or repos["workdir"])}
    code, out, err = guard.run(
        ["--workdir", str(repos["workdir"])],
        io.StringIO(json.dumps(event)),
        home=str(repos["home"]),
    )
    if code == 2:
        return "deny"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "pass"


def test_rules_are_one_table_without_profiles():
    """Ф78-1: задач по «Го» нет — у гарда и политики одна таблица (прежний столбец `held`)."""
    from hub.work import policy

    assert not hasattr(guard, "PROFILES") and not hasattr(policy, "PROFILES")
    for key, kind in guard.RULES.items():
        assert kind in {"deny", "ask", None}, key
    assert "main_tree_edit" not in guard.RULES  # правило было только у задач (в `held` не действовало)
    for key in ("main_tree_git", "project_rules", "agent_config", "foreign_memory", "nested_agent"):
        assert guard.RULES[key] == "ask", key
    for key in ("home_claude", "hub_dirs", "hub_prod", "hub_scripts", "hub_units", "kill_agents"):
        assert guard.RULES[key] == "deny", key
    permissions = policy.session_permissions(["mcp__x__write"])
    assert permissions == {"ask": ["mcp__x__write"], "deny": list(policy.DEFAULT_DENY)}
    assert set(policy.PERMISSION_RULES) == {
        "mcp_write",
        "screen_windows",
        "hub_cli",
        "cross_session",
        "plan_mode",
    }


def test_old_profile_flag_is_accepted_other_profiles_ask(repos):
    """Ф78-1: настройки сессий прежней версии несут `--profile held` — флаг принимается и ничего не меняет;
    другой профиль (в том числе бывший `task`) — неверные аргументы, карточка."""
    main = repos["workdir"]
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(main / "app.py")}, "cwd": str(main)}
    for extra in ((), ("--profile", "held")):
        code, out, _err = guard.run(
            ["--workdir", str(main), *extra], io.StringIO(json.dumps(event)), home=str(repos["home"])
        )
        assert (code, out) == (0, ""), extra  # правка основного дерева свободна (как в `held`)
    for profile in ("auto", "task"):
        code, out, _err = guard.run(
            ["--workdir", str(main), "--profile", profile],
            io.StringIO(json.dumps(event)),
            home=str(repos["home"]),
        )
        assert code == 0 and "неверные аргументы" in out, profile


def test_held_works_in_main_tree(repos):
    main = repos["workdir"]
    assert _held(repos, "Edit", {"file_path": str(main / "app.py")}) == "pass"
    assert _held(repos, "Write", {"file_path": str(main / "new_module.py")}) == "pass"
    for command in (
        "git checkout -b x",
        "git switch main",
        "git reset --hard HEAD",
        "git stash",
        "git clean -fd",
        "git restore app.py",
    ):
        assert _held(repos, "Bash", {"command": command}) == "ask", command
    assert _held(repos, "Bash", {"command": "git stash list"}) == "pass"
    assert _held(repos, "Bash", {"command": "git commit -am x"}) == "pass"
    assert _held(repos, "Bash", {"command": "echo 1 >> app.py"}) == "pass"


def test_held_project_rules_and_agent_configs_are_cards(repos):
    main = repos["workdir"]
    for name in (
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/settings.json",
        ".claude/hub.md",
        ".opencode/agents/a.md",
    ):
        assert _held(repos, "Write", {"file_path": str(main / name)}) == "ask", name
    for command in (
        "echo x > CLAUDE.md",
        "sed -i s/a/b/ .claude/hub.md",
        "rm -rf .clinerules",
        "cp a AGENTS.md",
    ):
        assert _held(repos, "Bash", {"command": command}) == "ask", command
    # П7: настройки агентов проекта — карточка в `held` (у задачи — прежний запрет правки основного дерева)
    for name in (".mcp.json", "opencode.json", "OPENCODE.md"):
        assert _held(repos, "Write", {"file_path": str(main / name)}) == "ask", name
        assert _held(repos, "Bash", {"command": f"echo x > {name}"}) == "ask", name
    assert _held(repos, "Write", {"file_path": str(main / "docs" / "mcp.json")}) == "pass"


def test_held_home_claude_stays_denied_before_project_rules(repos):
    home = repos["home"]
    # порядок проверок (П7): ~/.claude/** раньше «файлов правил проекта» — в карточку не уходит
    for name in (
        "CLAUDE.md",
        "settings.json",
        "hub-rules.md",
        "hooks/guard.sh",
        "projects/p/memory/CLAUDE.md",
    ):
        path = str(home / ".claude" / name)
        assert _held(repos, "Write", {"file_path": path}) == "deny", name
    assert _held(repos, "Bash", {"command": f"echo x >> {home}/.claude/CLAUDE.md"}) == "deny"


def test_held_memory_own_free_foreign_is_card(repos):
    home, main = repos["home"], repos["workdir"]
    own = home / ".claude" / "projects" / guard.memory_slug(str(main)) / "memory" / "note.md"
    foreign = home / ".claude" / "projects" / "p" / "memory" / "note.md"
    assert guard.memory_slug("/home/owner/projects/hub") == "-home-owner-projects-hub"
    assert guard.memory_slug("/a/b.c_d") == "-a-b-c-d"
    assert _held(repos, "Write", {"file_path": str(own)}) == "pass"
    assert _held(repos, "Write", {"file_path": str(foreign)}) == "ask"
    assert _held(repos, "Bash", {"command": f"echo x >> {foreign}"}) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "claude -p 'сделай'",
        "timeout 60 claude --resume abc -p x",
        "opencode run x",
        "cd /tmp && ~/.local/bin/claude",
    ],
)
def test_held_nested_agent_is_card(repos, command):
    # вложенный процесс идёт без гарда и наследует личность сессии хаба
    assert _held(repos, "Bash", {"command": command}) == "ask", command


def test_held_nested_agent_lookalikes(repos):
    # Каталог `.claude` и файл `opencode.json` — не агент.
    for command in ("ls ~/.claude/projects", "cat opencode.json", "cat .claude/x.md", "ls src/claude.py"):
        assert _held(repos, "Bash", {"command": command}) == "pass", command
    # Ревью 2б: слово `claude` / `opencode` отдельным словом — карточка в `held` (цена грубости принята);
    # профиль `task` это слово не трогает.
    for command in ("echo claude", "grep -r claude src", "which opencode"):
        assert _held(repos, "Bash", {"command": command}) == "ask", command


def test_hub_dirs_denied_for_read_and_write(repos):
    home = repos["home"]
    config = home / ".config" / "hub"
    data = home / ".local" / "share" / "hub"
    for tool, tool_input in (
        ("Read", {"file_path": str(config / "work" / "token")}),
        ("Write", {"file_path": str(data / "work" / "held-1" / "settings.json")}),
        ("Edit", {"file_path": str(config / "work" / "config.json")}),
        ("Grep", {"pattern": "x", "path": str(data)}),
        ("Glob", {"pattern": "*", "path": str(config)}),
        ("Bash", {"command": f"cat {config}/work/token"}),
        ("Bash", {"command": "cat ~/.config/hub/work/token"}),
        ("Bash", {"command": "ls $HOME/.local/share/hub/work"}),
        ("Bash", {"command": f"cd {home}/.config && cat hub/work/token"}),
        ("Bash", {"command": "cp ~/.config/hub/work/token /tmp/t"}),
        ("Bash", {"command": 'bash -c "head -c 5 ~/.config/hub/work/token"'}),
        ("Bash", {"command": "echo $(cat ~/.local/share/hub/work/state.json)"}),
    ):
        assert _held(repos, tool, tool_input) == "deny", (tool, tool_input)
    link = repos["workdir"] / "build" / "cfg"
    config.mkdir(parents=True)
    link.symlink_to(config)
    assert _held(repos, "Read", {"file_path": str(link / "work" / "token")}) == "deny"
    # соседние каталоги и упоминания без пути — свободны
    for command in ("ls ~/.config/hub-other", "cat ~/.config/hubris/x", "ls ~/.config", "echo hub"):
        assert _held(repos, "Bash", {"command": command}) == "pass", command
    assert _held(repos, "Read", {"file_path": str(home / ".config" / "git" / "config")}) == "pass"


def test_held_hub_itself_and_kills_stay_denied_buttons_stay_cards(repos):
    home = repos["home"]
    for command in (
        "pkill -f claude",
        "kill $(pgrep -f hub)",
        "systemctl --user restart hub-core",
        "bash scripts/hub-deploy.sh",
        f"touch {home}/projects/hub-prod/x",
    ):
        assert _held(repos, "Bash", {"command": command}) == "deny", command
    for command in (
        "git push",
        "ssh host",
        "kubectl apply -f k.yaml",
        "./deploy.sh prod",
        "gh pr create --fill",
    ):
        assert _held(repos, "Bash", {"command": command}) == "ask", command
    # отказ по одной части команды сильнее карточки по другой
    assert _held(repos, "Bash", {"command": "git checkout main && pkill -f claude"}) == "deny"
    assert _held(repos, "Bash", {"command": "echo x > CLAUDE.md; cat ~/.config/hub/work/token"}) == "deny"


def test_held_rules_in_oc_decide(repos):
    """Те же правила — в решении вызовов OpenCode (`_oc_decide`)."""
    import types

    from hub.engine import oc_tools
    from hub.engine.oc_turn import Allow, Ask, Deny, PermissionRequest
    from hub.work import policy
    from hub.work.held import HeldRunner

    main = repos["workdir"]

    def decide(tool, args):
        runner = object.__new__(HeldRunner)
        runner.workdir = str(main)
        runner.host = types.SimpleNamespace(home=repos["home"], data_dir=repos["tmp"])
        runner.process = None
        runner._oc_settings = policy.settings_config(ask=policy.effective_ask({}), guard_command="")
        runner.event = lambda *a, **k: None
        req = PermissionRequest(id="per_1", session_id="ses_1", permission=tool, tool=tool, args=args)
        return type(runner._oc_decide(req, oc_tools.to_claude(tool, args)))

    assert decide("edit", {"filePath": str(main / "app.py"), "oldString": "1", "newString": "2"}) is Allow
    assert decide("write", {"filePath": str(main / "CLAUDE.md"), "content": "x"}) is Ask
    assert decide("bash", {"command": "git checkout -b x", "description": "d"}) is Ask
    assert decide("read", {"filePath": str(repos["home"] / ".config" / "hub" / "work" / "token")}) is Deny
    assert decide("bash", {"command": "claude -p x", "description": "d"}) is Ask
    assert decide("bash", {"command": "git push", "description": "d"}) is Ask
    assert not hasattr(HeldRunner, "guard_profile")


# --- ревью 2б: каталоги хаба через предка и шаблоны, запускалки агентов, формы записи -----------------------


@pytest.fixture
def box(tmp_path):
    """Домашняя папка с каталогами хаба и проектом внутри неё (как на машине владельца)."""
    home = tmp_path / "home"
    wd = home / "proj"
    wd.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(wd)], check=True)  # noqa: S603, S607
    (wd / "CLAUDE.md").write_text("x")
    for parts in guard.HUB_DIRS:
        (home.joinpath(*parts) / "work").mkdir(parents=True)
        (home.joinpath(*parts) / "work" / "state.json").write_text("{}")
    (home / ".claude").mkdir()
    return str(home), str(wd)


def _fill(box, command: str) -> str:
    """`{cfg}` и `{share}` — родители каталогов хаба в домашней папке стенда, `{home}` — она сама."""
    home, _ = box
    cfg = os.path.join(home, guard.HUB_DIRS[0][0])
    share = os.path.join(home, *guard.HUB_DIRS[1][:2])
    user = os.path.basename(home)
    for key, value in (("{home}", home), ("{cfg}", cfg), ("{share}", share), ("{user}", user)):
        command = command.replace(key, value)
    return command


def _verdict(box, tool, tool_input, cwd=None, workdir=None):
    home, wd = box
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd or wd}
    try:
        guard.decide(event, workdir=workdir or wd, home=home)
    except guard.Verdict as v:
        return v.kind
    return "pass"


def _box_bash(box, command, cwd=None):
    return _verdict(box, "Bash", {"command": _fill(box, command)}, cwd)


HUB_DIRS_AROUND = [
    "grep -r stage {cfg}",
    "rg stage {home}",
    "wc -c {cfg}/hu*/work/state.json",
    "cat {cfg}/h[tu]b/work/state.json",
    "cp -r {cfg} /tmp/x",
    "tar cf /tmp/x.tar {cfg}",
    "ls -R {share}",
    "ls -laR ~",
    "find / -name state.json",
    "du -sh ~",
    "rsync -a ~/ /tmp/x/",
    "zip -r /tmp/x.zip {cfg}",
    "cd {cfg}; wc -c hu?/work/state.json",
    "D=hub; wc -c {cfg}/$D/work/state.json",
    "cat ${HOME}/.config/{hub,x}/work/state.json",
    "grep -r stage $HOME",
    "grep -r stage ~{user}/.config",
    'bash -c "find ~ -name state.json"',
    "sudo tar czf /tmp/x.tgz /",
]


@pytest.mark.parametrize("command", HUB_DIRS_AROUND)
def test_hub_dirs_through_ancestor_or_pattern_are_denied(box, command):
    assert _box_bash(box, command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "ls ~",
        "ls -la {cfg}",
        "ls {cfg}/h*",  # шаблон в каталог хаба — запрет ниже; здесь проверяется сосед
        "cat ~/.bashrc",
        "grep -r stage .",
        "find . -name '*.py'",
        "cp -r src /tmp/x",
        "ls -R {cfg}/other",
        "cat {cfg}/hub-other/x",
        "wc -c {cfg}/hubx/*",
        "echo $X",
    ],
)
def test_hub_dirs_rule_leaves_neighbours_alone(box, command):
    expected = "deny" if "h*" in command else "pass"
    assert _box_bash(box, command) == expected


def test_hub_dirs_search_tools_on_ancestor_or_pattern(box):
    home, wd = box
    for tool, tool_input in (
        ("Grep", {"pattern": "stage", "path": _fill(box, "{cfg}")}),
        ("Grep", {"pattern": "stage", "path": "~"}),
        ("Grep", {"pattern": "stage", "path": "/"}),
        ("Glob", {"pattern": "*", "path": home}),
        ("Glob", {"pattern": _fill(box, "{cfg}/hub/**/*")}),
        ("Glob", {"pattern": _fill(box, "{cfg}/h*/work/*")}),
        ("Glob", {"pattern": _fill(box, "{home}/**/state.json")}),
        ("Glob", {"pattern": "~/.config/*"}),
        ("Glob", {"pattern": "hub/**", "path": _fill(box, "{cfg}")}),
        ("Grep", {"pattern": "stage", "path": _fill(box, "{cfg}/hu?")}),
    ):
        assert _verdict(box, tool, tool_input) == "deny", (tool, tool_input)
    for tool, tool_input in (
        ("Grep", {"pattern": "stage"}),
        ("Grep", {"pattern": "stage", "path": wd, "glob": "*.py"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Glob", {"pattern": "src/**/*.ts", "path": wd}),
        ("Read", {"file_path": _fill(box, "{cfg}/other.json")}),
        ("Read", {"file_path": home + "/notes.txt"}),
    ):
        assert _verdict(box, tool, tool_input) == "pass", (tool, tool_input)


def test_hub_dirs_home_rooted_session(box):
    """Сессия с рабочей папкой = домашний каталог («сисадмин»): явный путь-предок (`.`) у широкой команды и
    путь в каталог хаба — запрет."""
    home, _ = box
    for command in ("grep -rn stage .", "find . -name state.json", "cat .config/hub/work/state.json"):
        assert _verdict(box, "Bash", {"command": command}, cwd=home, workdir=home) == "deny", command
    assert _verdict(box, "Glob", {"pattern": ".config/hub/**"}, cwd=home, workdir=home) == "deny"


def test_hub_dirs_home_rooted_session_known_limit(box):
    """ИЗВЕСТНЫЙ ПРЕДЕЛ (спека 2б, «Известные пределы гарда и хоста»): в сессии с рабочей папкой = домашний
    каталог широкая команда и поиск БЕЗ явного пути идут от текущей папки — гард её не считает словом
    команды. Тест фиксирует нынешнее поведение."""
    home, _ = box
    for command in ("grep -rn stage", "rg stage", "ls -R"):
        assert _verdict(box, "Bash", {"command": command}, cwd=home, workdir=home) == "pass", command
    assert _verdict(box, "Grep", {"pattern": "stage"}, cwd=home, workdir=home) == "pass"
    assert _verdict(box, "Glob", {"pattern": "**/state.json"}, cwd=home, workdir=home) == "pass"


def test_hub_dirs_ancestor_reason_tells_what_to_do(box):
    """Приёмка 2б. Слово-предок у широкой команды бывает и шаблоном поиска (`grep -c / файл`): отказ остаётся
    (гард не отличает шаблон от пути), но причина говорит сессии, что делать, — не та же, что у чтения самих
    каталогов хаба."""
    home, wd = box

    def reason(command: str) -> str:
        event = {"tool_name": "Bash", "tool_input": {"command": _fill(box, command)}, "cwd": wd}
        with pytest.raises(guard.Verdict) as caught:
            guard.decide(event, workdir=wd, home=home)
        assert caught.value.kind == "deny"
        return caught.value.reason

    for command in ("grep -c / CLAUDE.md", 'rg "~" src', "grep -rn {home} .", "du -sh ~"):
        assert "Grep" in reason(command) and "уже" in reason(command), command
    inside = reason("cat {cfg}/hub/work/state.json")
    assert "не для сессии" in inside and "Grep" not in inside


# --- владелец 08.10.2026: запись вне папки проекта — карточкой (профиль `held`) ---------------------------------


@pytest.fixture
def outside(box, tmp_path, monkeypatch):
    """Каталоги тестов лежат под `/tmp`, а он открыт для записи: для проверки правила открытым остаётся
    только свой «временный» каталог рядом с домашней папкой стенда (устройства открыты всегда)."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(guard, "OPEN_WRITE_ROOTS", (str(scratch),))
    home, wd = box
    other = os.path.join(home, "other")
    os.makedirs(other)
    return box, other, str(scratch)


def test_outside_workdir_write_is_a_card_in_held(outside):
    box, other, scratch = outside
    home, wd = box
    for tool_input in (
        {"file_path": os.path.join(other, "a.py")},
        {"file_path": os.path.join(home, "notes.txt")},
        {"file_path": "../other/a.py"},
        {"file_path": "/etc/hosts"},
    ):
        assert _verdict(box, "Write", tool_input) == "ask", tool_input
        assert _verdict(box, "Edit", tool_input) == "ask", tool_input
    for command in (
        "echo x > ../other/a.txt",
        "echo x >> ~/notes.txt",
        "cp app.py {home}/other/",
        "mv app.py ../other/app.py",
        "touch {home}/new.txt",
        "cd {home}/other && echo x > a.txt",
        "tee {home}/other/log.txt < app.py",
    ):
        assert _box_bash(box, command) == "ask", command


def test_inside_workdir_temp_and_devices_stay_free(outside):
    box, other, scratch = outside
    home, wd = box
    os.makedirs(os.path.join(wd, "src"))
    for tool_input in (
        {"file_path": os.path.join(wd, "src", "a.py")},
        {"file_path": "src/b.py"},
        {"file_path": os.path.join(scratch, "x.txt")},
    ):
        assert _verdict(box, "Write", tool_input) == "pass", tool_input
    for command in (
        "echo x > src/a.txt",
        "ls -la > /dev/null 2>&1",
        "echo x | tee /dev/stderr",
        "echo x > /dev/stdout",
        f"echo x > {scratch}/y.txt",
        "mkdir -p build && cp app.py build/",
        "cat {home}/other/a.py",  # чтение вне папки — не запись
    ):
        assert _box_bash(box, command) == "pass", command
    # Своя память проекта — по-прежнему без карточки (у `~/.claude/**` свои правила).
    memory = os.path.join(home, ".claude", "projects", guard.memory_slug(wd), "memory", "note.md")
    assert _verdict(box, "Write", {"file_path": memory}) == "pass"


def test_outside_workdir_through_symlink_is_a_card(outside):
    box, other, _scratch = outside
    _home, wd = box
    os.symlink(other, os.path.join(wd, "link"))
    assert _verdict(box, "Write", {"file_path": os.path.join(wd, "link", "a.py")}) == "ask"
    assert _box_bash(box, "echo x > link/a.txt") == "ask"


def test_outside_workdir_denies_stay_stronger(outside):
    box, _other, _scratch = outside
    home, _wd = box
    # Запрет сильнее карточки: каталоги хаба и `~/.claude` (не память) — по-прежнему отказ.
    assert _verdict(box, "Write", {"file_path": _fill(box, "{cfg}/hub/x.json")}) == "deny"
    assert _verdict(box, "Write", {"file_path": os.path.join(home, ".claude", "settings.json")}) == "deny"


NESTED_THROUGH_RUNNERS = [
    "npx claude -p hi",
    "npx @anthropic-ai/claude-code -p hi",
    "bunx opencode run hi",
    "uvx opencode run hi",
    "pnpm dlx @anthropic-ai/claude-code -p hi",
    "yarn dlx opencode run hi",
    "$(which claude) -p hi",
    "`which claude` -p hi",
    "C=claude; $C -p hi",
    'echo "claude -p hi" | bash',
    'bash <<< "claude -p hi"',
    'script -qc "claude -p hi" /dev/null',
    "systemd-run --user --wait claude -p hi",
    "parallel claude -p ::: a b",
    "uv run claude -p hi",
    "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js -p hi",
]


@pytest.mark.parametrize("command", NESTED_THROUGH_RUNNERS)
def test_held_nested_agent_through_runners_is_card(box, command):
    assert _verdict(box, "Bash", {"command": command}) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "echo x > $HOME/.claude/CLAUDE.md",
        "echo x >> ${HOME}/.claude/hub-rules.md",
        "sed -i s/a/b/ $HOME/.claude/hub-rules.md",
        'cp /tmp/x "$HOME/.claude/agents/a.md"',
        "tee ~{user}/.claude/settings.json",
    ],
)
def test_held_home_claude_through_home_variable_is_deny_not_card(box, command):
    """П7: запрет домашнего каталога Claude раньше «правил проекта» — и через `$HOME`."""
    assert _box_bash(box, command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git apply /tmp/p.patch",
        "cp -t .claude /tmp/x",
        "cp /tmp/x --target-directory=.claude",
        "mv --target-directory .claude /tmp/x",
        "sed --in-place s/a/b/ CLAUDE.md",
        "sed --in-place=.bak s/a/b/ CLAUDE.md",
        "perl -pi -e s/a/b/ CLAUDE.md",
        "perl -i.bak -pe s/a/b/ .mcp.json",
        "pushd .claude && echo x > a.md",
        "git mv CLAUDE.md OLD.md",
        "git rm -f .claude/settings.json",
        "git checkout-index -a -f",
        "git read-tree -u --reset HEAD",
    ],
)
def test_held_rule_files_written_through_other_forms_is_card(box, command):
    assert _box_bash(box, command) == "ask"


def test_held_write_forms_do_not_card_harmless_commands(box):
    for command in (
        "git apply --check /tmp/p.patch",
        "git apply --stat /tmp/p.patch",
        "git mv a.py b.py",
        "git rm --cached build/out.js",
        "sed --in-place s/a/b/ src/a.py",
        "perl -pi -e s/a/b/ src/a.py",
        "cp -t build /tmp/x",
        "pushd src && echo x > a.md",
    ):
        assert _box_bash(box, command) == "pass", command


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"open('CLAUDE.md','w').write('x')\"",
        "patch -p1 < /tmp/p.patch",
        "F=CLAUDE.md; echo x > $F",
        "curl -o .mcp.json http://x",
        "tar xf /tmp/x.tar -C .claude",
    ],
)
def test_held_rule_files_write_known_limits(box, command):
    """ИЗВЕСТНЫЙ ПРЕДЕЛ (спека 2б, «Известные пределы гарда и хоста»): запись файлов правил интерпретатором,
    `patch`, через переменную в цели, `curl -o`, `tar -C` гард не разбирает — карточки нет. Тест фиксирует
    нынешнее поведение; закрывать — отдельным решением."""
    assert _box_bash(box, command) == "pass"


# --- ревью Ф41-3 правила `outside_workdir` (тесты ревьюера — `.runs/critic-f413/test_guard_outside.py`) ---


def _out(outside, command, **kw):
    box, other, scratch = outside
    _home, wd = box
    for key, value in (("{other}", other), ("{scratch}", scratch), ("{wd}", wd)):
        command = command.replace(key, value)
    return _verdict(box, "Bash", {"command": _fill(box, command)}, **kw)


@pytest.mark.parametrize(
    "command",
    [
        "exec 3<{other}; echo x > /dev/fd/3/a.txt",
        "exec 3<{other}; echo x > /proc/self/fd/3/a.txt",
        "exec 3<{other}; touch /dev/fd/3/a.txt",
        "exec 3<{other}; cp app.py /proc/self/fd/3/",
        "exec 3<{other}; cp app.py /dev/fd/3",
        "exec 3<{other}; tee /dev/fd/3/a.txt < app.py",
        "echo x > /dev/shm/a.txt",
        "echo x > /dev/../etc/x",
        "echo x > /proc/self/fd/../cwd/../other/a",
        "echo x > /proc/self/cwd/../a.txt",
    ],
)
def test_outside_through_fd_directory_or_dev_tree_is_a_card(outside, command):
    """Устройство — только точный путь: каталог за дескриптором и прочее под `/dev`, `/proc` — как все."""
    assert _out(outside, command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "echo x > /dev/null",
        "echo x > /dev/fd/2",
        "echo x > /proc/self/fd/1",
        "echo x | tee /dev/fd/2",
        "echo x | tee /dev/tty",
        "cp app.py /dev/null",
        "dd if=/dev/zero of=/dev/null count=1",
        "ls >&2",
        "ls 2>&1",
        "exec 3>&-",
    ],
)
def test_exact_devices_and_descriptors_stay_free(outside, command):
    assert _out(outside, command) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "ln -s {other} {scratch}/l && echo x > {scratch}/l/a.txt",
        "ln -s {other} l2 && echo x > l2/a.txt",
        "ln -s {other} l3; cp app.py l3/",
        "ln -sfn {home} {scratch}/h && tee {scratch}/h/.bashrc < app.py",
        "ln -s ../other l4",
    ],
)
def test_symlink_made_in_the_same_command_is_a_card(outside, command):
    assert _out(outside, command) == "ask"
    assert _out(outside, "ln -s src/a.py b.py") == "pass"  # ссылка внутри проекта — свободно


@pytest.mark.parametrize("command", ["echo x >& {other}/a.txt", "ls >& {home}/notes.txt"])
def test_redirect_both_streams_to_a_file_is_a_write_target(outside, command, box):
    assert _out(outside, command) == "ask"
    assert _out(outside, "ls >& src.log") == "pass"
    # Цель `>&` видят все правила цели записи.
    assert _box_bash(box, "echo x >& {home}/.claude/settings.json") == "deny"
    assert _box_bash(box, "echo x >& {cfg}/hub/x.json") == "deny"


@pytest.mark.parametrize(
    ("command", "verdict"),
    [
        ("cd $HOME && echo x > notes.txt", "ask"),
        ("cd ${HOME}/other && touch a.txt", "ask"),
        ("pushd $HOME && echo x > notes.txt", "ask"),
        ("cd ~{user}/other && echo x > a.txt", "ask"),
        # `cp … .` из домашней папки — широкая команда над каталогами хаба: запрет сильнее карточки.
        ('cd "$HOME" && cp {wd}/app.py .', "deny"),
        # Куда ушла команда — неизвестно: относительная цель записи — карточка.
        ("cd $TARGET && echo x > notes.txt", "ask"),
        ("cd ${DIR:-/} && touch a.txt", "ask"),
        ("cd $TARGET && echo x > {wd}/a.txt", "pass"),
        ("cd $TARGET && ls", "pass"),
        ("cd $HOME/proj && echo x > a.txt", "pass"),
        ("cd src && echo x > a.txt", "pass"),
    ],
)
def test_cd_through_home_variable_and_unknown_place(outside, command, verdict):
    assert _out(outside, command) == verdict


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '/^#/d' app.py",
        "sed -i -e '/DEBUG/d' src/a.py",
        "sed -i '/^import os/a import sys' app.py",
        "sed -n -i.bak -e '/x/p' -e '/y/d' app.py",
        "sed --in-place --expression='/x/d' app.py",
        "sed -ni '/x/p' app.py",
        "sed -i -f /etc/sed.rules app.py",
        "perl -pi -e '/x/ and s/a/b/' app.py",
        "perl -i -ne 'print unless /^#/' app.py src/a.py",
        "perl -0pi -e 's/\\n\\n+/\\n/g' app.py",
        "touch -r /etc/hostname app.py",
        "touch --reference /etc/hostname app.py",
        "chmod --reference=/etc/hostname app.py",
        "rm -rf ./build",
        "chmod +x run.sh",
        "(cd src && touch a.py)",
    ],
)
def test_no_false_card_for_expressions_and_reference_files(outside, command):
    """Цель записи у `sed -i` / `perl -pi` — файлы, а не выражение; у `touch -r` — не образец."""
    assert _out(outside, command) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "sed -i s/a/b/ {other}/a.py",
        "sed -i -e s/a/b/ app.py {other}/a.py",
        "sed -i '/x/d' {home}/notes.txt",
        "sed --in-place=.bak -e /x/d {other}/a.py",
        "perl -pi -e 's/a/b/' {other}/a.py",
        "touch -r app.py {other}/a.py",
        "dd if=app.py of={other}/a",
        "git mv app.py {other}/app.py",
        "mkdir {other}/d",
        "echo x &> {other}/a.txt",
    ],
)
def test_in_place_and_other_writes_outside_are_still_cards(outside, command):
    assert _out(outside, command) == "ask"


def test_in_place_expression_parsing(outside, box):
    assert _box_bash(box, "sed -i -e s/a/b/ {home}/.claude/settings.json") == "deny"
    assert _box_bash(box, "sed -i s/a/b/ CLAUDE.md") == "ask"


def test_workdir_through_symlink_is_not_a_card(outside):
    """Хост запускает сессию с рабочей папкой-симлинком; модель называет пути через симлинк."""
    box, other, _scratch = outside
    home, wd = box
    link = os.path.join(home, "wdlink")
    os.symlink(wd, link)
    os.makedirs(os.path.join(wd, "src"))
    for tool in ("Write", "Edit"):
        assert _verdict(box, tool, {"file_path": link + "/src/a.py"}, workdir=link, cwd=link) == "pass"
    for command in ("echo x > " + link + "/src/a.txt", "echo x > src/a.txt", "touch " + wd + "/src/b.txt"):
        assert _verdict(box, "Bash", {"command": command}, workdir=link, cwd=link) == "pass", command
    # Наружу — по-прежнему карточка, и через симлинк внутри такой папки тоже.
    assert _verdict(box, "Write", {"file_path": other + "/a.py"}, workdir=link, cwd=link) == "ask"
    os.symlink(other, os.path.join(wd, "out"))
    assert _verdict(box, "Write", {"file_path": link + "/out/a.py"}, workdir=link, cwd=link) == "ask"


def test_symlink_out_of_open_root_and_worktree_inside_project(outside):
    box, other, scratch = outside
    _home, wd = box
    os.symlink(other, scratch + "/pre")
    assert _out(outside, "echo x > {scratch}/pre/a.txt") == "ask"
    assert _verdict(box, "Write", {"file_path": scratch + "/pre/a.txt"}) == "ask"
    assert _verdict(box, "NotebookEdit", {"notebook_path": other + "/a.ipynb"}) == "ask"
    tree = os.path.join(wd, ".claude", "worktrees", "w1", "src")
    os.makedirs(tree)
    assert _verdict(box, "Write", {"file_path": tree + "/a.py"}, cwd=os.path.dirname(tree)) == "pass"
    assert _out(outside, "echo x > src/a.txt", cwd=os.path.dirname(tree)) == "pass"


CLAUDE_TMP = "/tmp/claude-1000/-home-owner-projects-hub/0f3a/scratchpad"  # noqa: S108 — текст команды


@pytest.mark.parametrize(
    "command",
    [
        f"ls {CLAUDE_TMP}",
        f"cat {CLAUDE_TMP}/notes.md",
        f"python3 {CLAUDE_TMP}/fix.py",
        f"echo x > {CLAUDE_TMP}/out.txt",
        "file /tmp/claude-1000/-home-owner-proj/0f3a/images/1.png",
        "ls /tmp/claude-0/x /var/tmp/claude-65534/y",
    ],
)
def test_claude_temp_directory_is_not_a_nested_agent(box, command):
    """Живой стенд: каталог `claude-<uid>/` во временной папке — свой у Claude Code, а не запуск агента."""
    assert _verdict(box, "Bash", {"command": command}) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "npx claude -p hi",
        "$(which claude) -p hi",
        "C=claude; $C -p hi",
        'echo "claude -p hi" | bash',
        "/usr/bin/claude -p hi",
        "claude-1000 -p hi",
        "ls /tmp/claude-1000 && claude -p hi",
        "/tmp/claude-1000/bin/claude -p hi",
        "echo claude-code",
        "cat /tmp/claude-x/y",
    ],
)
def test_real_nested_agent_next_to_temp_directory_is_still_a_card(box, command):
    assert _verdict(box, "Bash", {"command": command}) == "ask"


# --- 09.10: ложные карточки ручного режима (разбор записей сессий Initech) ------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git fetch origin claude/ABC-101 2>&1 | tail -1",
        "git log --oneline release-rc..origin/claude/ABC-101 | head -30",
        "git diff --stat $(git merge-base origin/feature/sprint-4 origin/claude/ABC-101) origin/claude/ABC-101",
        "ls /tmp/claude-1000 2>/dev/null | head",
        "ls docs/architecture.md",
    ],
)
def test_agent_name_as_path_part_is_not_a_nested_agent(box, command):
    """Ветка `claude/…` и каталог `claude-<uid>` последней частью пути — не запуск агента."""
    assert _verdict(box, "Bash", {"command": command}) == "pass"


HEREDOC_SCRIPT = """mkdir -p /tmp/an && cat > /tmp/an/an.py <<'EOF'
import re
print(set(re.findall(r'(?:src|href)=["\\'](https?://[^"\\']{0,80})', s)))
EOF
python3 -I /tmp/an/an.py x.html"""


def test_quoted_heredoc_body_for_cat_is_data(box):
    """Тело heredoc с ограничителем в кавычках у `cat` — данные: непарные кавычки в нём не «команду не
    разобрать»."""
    assert _verdict(box, "Bash", {"command": HEREDOC_SCRIPT}) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "bash <<'EOF'\ngit push origin main\nEOF",  # тело исполняет оболочка — разбирается как раньше
        "cat > /tmp/x.sh <<'EOF'\necho 'привет\nEOF\ngit push origin main",  # после тела — обычная команда
        "cat > /tmp/x.sh <<EOF\n$(git push origin main)\nEOF",  # ограничитель без кавычек — подстановки живы
        "cat <<'EOF' | bash\nclaude -p hi\nEOF",  # страховка по тексту видит тело
    ],
)
def test_heredoc_does_not_hide_commands(box, command):
    assert _verdict(box, "Bash", {"command": command}) == "ask"


def test_heredoc_write_target_is_still_checked(box):
    home, _ = box
    command = f"cat > {home}/.claude/CLAUDE.md <<'EOF'\nправило с ' кавычкой\nEOF"
    assert _verdict(box, "Bash", {"command": command}) == "deny"
