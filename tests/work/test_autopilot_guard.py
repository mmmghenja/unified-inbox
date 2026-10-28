"""Ф41-5а (docs/architecture.md): каталог выключателей
автопилота, набор снятых правил (`free`) в гарде и в правилах настроек сессии, метка правила в ответе хука."""

import io
import json
import os

import pytest

from hub import autopilot
from hub.work import guard, policy

from .conftest import make_repo

# Ключи каталога вне гарда и настроек процесса: действуют в ядре (5б) или в хосте.
OUTSIDE = {
    "hub_send",
    "hub_send_new",
    "hub_press",
    "bot_chat",  # Ф63-2: беседы с ботами — ядро (`autosend`, `presses`)
    "tg_join",
    "tg_leave",
    "wa_group",  # Ф62-6: группы WhatsApp — ядро (`group_ops`)
    "map_rule",
    "move_channel",
    "new_session",
    "question",
    "project_permissions",
    "foreign_hook",
    "oc_subagents",
}


def test_catalog_matches_held_rules_both_ways():
    held_guard = {key for key, kind in guard.RULES.items() if kind in ("ask", "deny")}
    assert "write_forms" in guard.RULES and "main_tree_edit" not in guard.RULES  # Ф78-1: профиль один
    held_guard -= {"write_forms"}  # переключатель разбора, не правило
    held_policy = {key for key, rule in policy.PERMISSION_RULES.items() if rule[1] in ("ask", "deny")}
    assert held_guard | held_policy | OUTSIDE == set(autopilot.KEYS)
    assert not (held_guard | held_policy) & OUTSIDE
    assert len(autopilot.KEYS) == len(set(autopilot.KEYS))
    # Вид в ручном режиме — как в таблицах правил.
    for key in held_guard:
        assert autopilot.BY_KEY[key].manual == guard.RULES[key], key
    for key in held_policy:
        assert autopilot.BY_KEY[key].manual == policy.PERMISSION_RULES[key][1], key
    for switch in autopilot.SWITCHES:
        assert switch.group in autopilot.GROUPS and switch.manual in ("ask", "deny") and switch.title
    # П10: мета-выключатели — сняв такой, сессия может снять и остальные.
    assert autopilot.META == {
        "hub_dirs",
        "hub_cli",
        "hub_prod",
        "hub_scripts",
        "hub_units",
        "kill_agents",
        "home_claude",
        "project_rules",
        "agent_config",
        "nested_agent",
        "oc_private_server",
        "oc_subagents",
        "proc_environ",
    }
    rows = autopilot.public()
    assert [r["group"] for r in rows] == sorted((r["group"] for r in rows), key=autopilot.GROUPS.index)
    assert set(rows[0]) == {"key", "group", "title", "manual", "meta", "note", "scope"}
    # Ф66-1: ключ `human_agent` (Ф54-1) убран — кому заявки «нужен человек», решает таблица маршрута проекта.
    assert {r["key"] for r in rows if r["scope"] == "project"} == set()
    assert "human_agent" not in autopilot.KEYS and "human_agent" not in autopilot.BY_KEY
    assert autopilot.free_keys({"human_agent": False, "push": False}, {}) == ["push"]
    assert autopilot.G_HUMAN not in autopilot.GROUPS


def test_effective_value_and_mark():
    assert autopilot.effective("push", None, None) is True  # умолчание — стоит
    assert autopilot.effective("push", {"push": False}, {}) is False
    assert autopilot.effective("push", {"push": False}, {"push": True}) is True  # проект главнее общего
    assert autopilot.effective("push", {"push": True}, {"push": False}) is False
    assert autopilot.effective("push", {"push": "no"}, {"push": 0}) is True  # не bool — как отсутствие
    assert autopilot.free_keys({"push": False, "server": False, "нет такого": False}, {"server": True}) == [
        "push"
    ]
    marked = autopilot.mark("гард хаба: push — решает владелец", "push")
    assert autopilot.split_mark(marked) == ("гард хаба: push — решает владелец", "push")
    assert autopilot.split_mark("текст\n[hub-rule:нет такого]") == ("текст\n[hub-rule:нет такого]", None)
    assert autopilot.split_mark("без метки") == ("без метки", None)
    assert autopilot.split_mark(None) == (None, None)
    assert autopilot.RULE_MARK == guard.RULE_MARK


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    workdir = home / "proj"
    make_repo(workdir)
    (home / ".claude" / "projects" / "other" / "memory").mkdir(parents=True)
    for parts in guard.HUB_DIRS:
        home.joinpath(*parts).mkdir(parents=True)
    return {"home": str(home), "wd": str(workdir)}


def _verdict(box, tool, tool_input, free=frozenset(), *, oc_port=None):
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": box["wd"]}
    try:
        guard.decide(event, workdir=box["wd"], home=box["home"], free=free, oc_port=oc_port)
    except guard.Verdict as verdict:
        return verdict
    return None


def _bash(command):
    return "Bash", {"command": command}


def _write(path):
    return "Write", {"file_path": path, "content": "x"}


# Ключ правила -> вызов, на котором оно срабатывает первым (пути: `{home}`, `{wd}`).
EXAMPLES = {
    "main_tree_git": _bash("git checkout main"),
    "project_rules": _write("{wd}/CLAUDE.md"),
    "agent_config": _write("{wd}/.mcp.json"),
    "home_claude": _write("{home}/.claude/settings.json"),
    "foreign_memory": _write("{home}/.claude/projects/other/memory/note.md"),
    "outside_workdir": _write("/opt/hub-test-outside/notes.txt"),  # стенд сам лежит в /tmp — он открыт
    "nested_agent": _bash("npx claude -p привет"),
    "hub_dirs": ("Read", {"file_path": "{home}/.config/hub/host.json"}),
    "hub_prod": _write("{home}/projects/hub-prod/a.py"),
    "hub_scripts": _bash("scripts/hub-deploy.sh"),
    "hub_units": _bash("systemctl --user restart hub-core.service"),
    "kill_agents": _bash("pkill -f hub-work"),
    "oc_private_server": _bash("curl http://127.0.0.1:43210/session"),
    "proc_environ": _bash("cat /proc/4242/environ"),
    "push": _bash("git push origin main"),
    "server": _bash("ssh prod uptime"),
    "kube_write": _bash("kubectl apply -f deploy.yaml"),
    "helm_write": _bash("helm install api ./chart"),
    "gh_write": _bash("gh pr create --fill"),
    "trello_write": _bash("./trello.sh comment abc текст"),
    "deploy": _bash("make deploy"),
    "unparsed_command": _bash('echo "не закрыта'),
}


def _fill(box, call):
    tool, tool_input = call
    filled = {
        k: v.replace("{home}", box["home"]).replace("{wd}", box["wd"]) if isinstance(v, str) else v
        for k, v in tool_input.items()
    }
    return tool, filled


def test_examples_cover_every_held_guard_rule():
    held = {key for key, kind in guard.RULES.items() if kind is not None}
    assert set(EXAMPLES) == held - {"write_forms", guard.FAILURE}


@pytest.mark.parametrize("key", sorted(EXAMPLES))
def test_freed_rule_does_not_fire_and_the_rest_stay(box, key):
    tool, tool_input = _fill(box, EXAMPLES[key])
    port = 43210 if key == "oc_private_server" else None
    held = _verdict(box, tool, tool_input, oc_port=port)
    assert held is not None and held.key == key, (key, held and held.key)
    assert held.kind == guard.RULES[key]
    freed = _verdict(box, tool, tool_input, frozenset({key}), oc_port=port)
    assert freed is None or freed.key != key, key
    # Снятие ДРУГИХ правил этого не трогает: вердикт тот же.
    others = frozenset(EXAMPLES) - {key}
    still = _verdict(box, tool, tool_input, others, oc_port=port)
    assert still is not None and (still.key, still.kind) == (key, held.kind)
    # Все сняты — вызов проходит.
    assert _verdict(box, tool, tool_input, frozenset(EXAMPLES), oc_port=port) is None


def test_compound_command_keeps_the_held_part(box):
    """П2, доказательство критика: `git commit … && git push` при стоящем `push` — карточка `push`."""
    command = "git commit -m x && git push"
    verdict = _verdict(box, "Bash", {"command": command}, frozenset({"server", "deploy"}))
    assert verdict is not None and (verdict.kind, verdict.key) == ("ask", "push")
    assert _verdict(box, "Bash", {"command": command}, frozenset({"push"})) is None
    # Первый по порядку накопленных — ключ карточки; запрет сильнее карточки и при снятых соседях.
    both = _verdict(box, "Bash", {"command": "ssh prod 'git push'"})
    assert both is not None and both.key == "push"
    deny = _verdict(box, "Bash", {"command": "git push && pkill -f hub-work"}, frozenset({"push"}))
    assert deny is not None and (deny.kind, deny.key) == ("deny", "kill_agents")


def test_free_only_known_keys(box):
    verdict = _verdict(box, "Bash", {"command": "ls"}, frozenset({"нет такого"}))
    assert verdict is not None and (verdict.kind, verdict.key) == ("ask", guard.FAILURE)
    assert "неверные аргументы хука" in verdict.reason
    # Сбой проверки `free` не снимает: решает хост.
    broken = _verdict(box, "Bash", {"command": 'echo "x'}, frozenset({guard.FAILURE}))
    assert broken is not None and broken.key == "unparsed_command"
    assert _verdict(box, "Bash", {"command": "ls"}) is None


def test_proc_environ_by_any_tool(box):
    for command in ("cat /proc/self/environ", "strings /proc/$PPID/environ", "cat /proc/*/env*"):
        verdict = _verdict(box, "Bash", {"command": command})
        assert verdict is not None and (verdict.kind, verdict.key) == ("deny", "proc_environ"), command
    read = _verdict(box, "Read", {"file_path": "/proc/77/environ"})
    assert read is not None and read.key == "proc_environ"
    assert _verdict(box, "Bash", {"command": "cat /proc/cpuinfo && env | sort"}) is None


def _hook(box, command, *argv_extra, tool="Bash"):
    event = {"tool_name": tool, "tool_input": {"command": command}, "cwd": box["wd"]}
    argv = ["--workdir", box["wd"], *argv_extra]
    return guard.run(argv, io.StringIO(json.dumps(event)), home=box["home"])


def _reason(out):
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_free_argument_and_rule_mark(box, free_sets):
    def free(*keys, sid=[0]):  # noqa: B006 — счётчик сессий теста
        sid[0] += 1
        free_sets.sets[sid[0]] = list(keys)
        return ("--profile", "held", *free_sets.args(sid[0]))


    code, out, _err = _hook(box, "git push", "--profile", "held")
    assert code == 0 and _reason(out) == "гард хаба: push — решает владелец\n[hub-rule:push]"
    assert autopilot.split_mark(_reason(out)) == ("гард хаба: push — решает владелец", "push")
    # Снятый ключ: хук молчит; соседний стоящий — спрашивает со своей меткой.
    assert _hook(box, "git push", *free("push", "server")) == (0, "", "")
    code, out, _err = _hook(box, "git push && ssh prod ls", *free("push"))
    assert _reason(out).endswith("[hub-rule:server]")
    assert _hook(box, "git push", *free())[1] != ""  # ничего не снято
    # Запрет при снятом правиле не действует; стоящий — exit 2, как раньше.
    assert _hook(box, "pkill -f hub-work", "--profile", "held")[0] == guard.EXIT_DENY
    kill = free("kill_agents")
    assert _hook(box, "pkill -f hub-work", *kill) == (0, "", "")
    # Ф78-1: профиль один — без флага `--profile` та же метка и тот же сокет набора; `--free` и `--free-file`
    # нет вовсе.
    code, out, _err = _hook(box, "git push")
    assert _reason(out) == "гард хаба: push — решает владелец\n[hub-rule:push]"
    assert _hook(box, "pkill -f hub-work", *kill[2:]) == (0, "", "")
    name, pid = free_sets.name, str(os.getpid())
    for bad in (
        ("--profile", "held", "--free", "push"),
        ("--profile", "held", "--free-file", "/x/free.json"),  # файла с набором больше нет
        ("--profile", "held", "--free-sock", name),  # три аргумента — только вместе
        ("--profile", "held", "--free-sock", name, "--free-pid", pid),
        ("--profile", "held", "--free-sock", name, "--free-pid", pid, "--free-session"),
        ("--profile", "held", "--free-sock", "/tmp/x", "--free-pid", pid, "--free-session", "1"),
        ("--profile", "held", "--free-sock", name, "--free-pid", "0", "--free-session", "1"),
        ("--profile", "held", "--free-sock", name, "--free-pid", pid, "--free-session", "-1"),
        ("--profile", "held", "--free-sock", name, "--free-pid", "x", "--free-session", "1"),
    ):
        code, out, _err = _hook(box, "ls", *bad)
        assert code == 0 and "неверные аргументы хука" in _reason(out)
    # Сбой проверки у постоянной сессии — метка `guard_failure`.
    code, out, _err = guard.run(["--workdir", box["wd"], "--profile", "held"], io.StringIO("не json"))
    assert _reason(out).endswith("[hub-rule:guard_failure]")


# --- настройки процесса и правила хоста (4.2, П3г, П5) ------------------------------------------------------


def test_settings_drop_freed_rules_only():
    ask = policy.effective_ask({})
    full = policy.settings_config(ask=ask, guard_command="g")
    assert full == policy.settings_config(ask=ask, guard_command="g", free=frozenset())
    free = frozenset({"mcp_write", "plan_mode", "push"})
    cut = policy.settings_config(ask=ask, guard_command="g", free=free)["permissions"]
    assert cut["ask"] == []
    assert cut["deny"] == ["mcp__playwright-show__*", "Bash(hub *)", "Bash(*/.venv/bin/hub *)", "SendMessage"]
    # `hub_cli` и `screen_windows` не снимаются, пока их нет в `free`.
    assert "Bash(hub *)" in cut["deny"] and "mcp__playwright-show__*" in cut["deny"]
    freed = frozenset({"hub_cli", "screen_windows"})
    gone = policy.settings_config(ask=ask, guard_command="g", free=freed)["permissions"]
    assert gone["deny"] == ["SendMessage", "EnterPlanMode", "ExitPlanMode"] and gone["ask"] == ask


def test_decide_tool_with_freed_mcp_write_and_rule_key():
    ask = policy.effective_ask({})
    held = policy.settings_config(ask=ask, guard_command="")
    freed = policy.settings_config(ask=ask, guard_command="", free={"mcp_write"})
    exact, other_server, unknown = (
        "mcp__claude_ai_Slack__slack_send_message",
        "mcp__slack__slack_send_message",
        "mcp__globex-gitlab__gl_new_writer",
    )
    for tool in (exact, other_server, unknown):
        assert policy.decide_tool(tool, held) == "ask", tool
        assert policy.decide_tool(tool, freed, None, {"mcp_write"}) is None, tool
        assert policy.rule_key(tool) == "mcp_write"
    # Без `free` эвристика серверов спрашивает и при пустом списке `ask` (П3г — затем и параметр).
    assert policy.decide_tool(unknown, freed) == "ask"
    assert policy.decide_tool("SendMessage", freed, None, {"mcp_write"}) == "deny"
    assert policy.rule_key("SendMessage") == "cross_session"
    assert policy.rule_key("Bash", {"command": "hub work status"}) == "hub_cli"
    assert policy.rule_key("EnterPlanMode") == "plan_mode"
    assert policy.rule_key("mcp__playwright-show__browser_navigate") == "screen_windows"
    # П5: у сессии в авто перенос беседы — не карточка инструмента.
    assert "mcp__hub-all__hub_move_channel" in ask
    auto = policy.without_move(ask)
    assert "mcp__hub-all__hub_move_channel" not in auto and len(auto) == len(ask) - 1
    settings = policy.settings_config(ask=auto, guard_command="")
    assert policy.decide_tool("mcp__hub-all__hub_move_channel", settings) is None
    assert policy.decide_tool("mcp__hub-all__hub_cancel_draft", settings) == "ask"


def test_push_method_in_code_is_not_push():
    """Живые карточки 10.10: `arr.push(x)` в heredoc node-скрипта и `sources\\.push` в шаблоне grep."""
    assert not guard._is_push("node - <<'EOF'\nconst a=[]; (by[g]??=[]).push(cp); unc.push(g);\nEOF")
    assert not guard._is_push('grep -n -E "register\\b|sources\\.push|reset" node_modules/font/lib/x.js')
    assert guard._is_push("git -C x push")
    assert guard._is_push('bash -c "docker push img"')
    assert guard._is_push("grep push file")  # голое слово — по-прежнему карточка
    assert guard._is_push("python3 -c 'import git; git.Repo(\".\").remotes.origin.push()'")


def test_push_word_in_note_written_by_cat_is_not_push():
    """Живая карточка 10.10 12:17: заметка в файл через `cat >> файл <<EOF` со словом «push» в тексте."""
    def push(command: str) -> bool:
        return guard._is_push(guard._push_text(command))

    note = "cat >> notes.md <<EOF\n- $(date '+%H:%M') правило дня\n  слово «push» и «.push(» не писать\nEOF"
    assert not push(note)
    assert not push("cat > x.cjs <<'EOF'\n// git push тут просто текст\nEOF")
    assert push("cat >> notes.md <<EOF\n- $(git push origin main)\nEOF")  # подстановку оболочка исполнит
    assert push("cat >> notes.md <<EOF\n- $(git \\\n push)\nEOF")  # не закрыта на строке — тело как есть
    assert push("bash <<EOF\ngit push\nEOF")  # получатель исполняет тело
    assert push("cat > x.sh <<'EOF'\necho 1\nEOF\ngit push")  # push после heredoc
