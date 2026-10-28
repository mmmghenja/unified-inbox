"""Ф41-5, правки по ревью (docs/architecture.md): собственная проверка
хоста не знает текущей папки сессии; метка и тип причины — не основание для авто-«разрешить»; окружение
процессов через `ps e`; упоминание пути в содержимом файла; ключ правила настроек — у стоящего правила."""

import pytest

from hub import autopilot
from hub.work import guard, policy

from .test_held_autopilot import _bash, _delivered, _free, _requests
from .test_held_host import _running, _session, _turn_done


async def _card(env, host, sid) -> dict:
    async def pending():
        return [r for r in await _requests(env, sid) if r["status"] == "pending"]

    [request] = await env.until(host, pending, what="карточка")
    return request


async def _deny(env, host, request) -> None:
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "deny"})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: _delivered(env, request["id"]), what="отказ доставлен")


def _verdict(tmp_path, tool, tool_input, **kwargs):
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(workdir)}
    try:
        guard.decide(event, workdir=str(workdir), home=str(tmp_path / "home"), **kwargs)
    except guard.Verdict as verdict:
        return verdict.kind, verdict.key
    return None


# --- 1. хост не знает текущей папки сессии ------------------------------------------------------------------


async def test_host_check_cards_a_relative_write_target(env):
    """`cd .claude` держится между вызовами Bash: запись в `settings.local.json` хук хаба спросил бы под
    `project_rules`, а запрос приходит типом `rule` без причины. Хост, считая путь от папки проекта,
    разрешал; теперь относительная цель записи — карточка под стоящим правилом путей."""
    command = "git commit -m x; echo '{\"disableAllHooks\":true}' > settings.local.json"
    env.scenario(turns=[[_bash(command), _bash("git commit -m y; echo 1 > notes.txt"), {"text": "готово"}]])
    await _free(env, "project_permissions", "foreign_hook")  # `project_rules` стоит
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    request = await _card(env, host, sid)
    assert request["rule"] == "project_rules" and "папка сессии хосту неизвестна" in request["reason"]
    assert env.responses() == []
    await _deny(env, host, request)
    request = await _card(env, host, sid)  # и безобидное имя: хост не знает, где оно окажется
    assert request["rule"] == "project_rules"
    await _deny(env, host, request)
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny", "deny"]


async def test_host_check_allows_relative_target_when_path_rules_are_lifted(env):
    env.scenario(turns=[[_bash("git commit -m x; echo 1 > notes.txt"), {"text": "готово"}]])
    await _free(env, "project_permissions", "foreign_hook", *guard.BLIND_KEYS)
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["allow"]
    assert await _requests(env, sid) == []


def test_blind_check_in_the_guard(tmp_path):
    repo = tmp_path / "repo"
    blind = {"cwd_unknown": True}
    # Хук (папка известна) — как прежде: относительная цель в папке проекта проходит.
    assert _verdict(tmp_path, "Bash", {"command": "echo 1 > notes.txt"}) is None
    assert _verdict(tmp_path, "Bash", {"command": "echo 1 > notes.txt"}, **blind) == ("ask", "project_rules")
    assert _verdict(tmp_path, "Bash", {"command": "touch a b"}, **blind) == ("ask", "project_rules")
    # Стоящее правило — первое по списку; все сняты — проходит.
    free = frozenset({"project_rules", "agent_config"})
    assert _verdict(tmp_path, "Bash", {"command": "echo 1 > n"}, free=free, **blind) == (
        "ask",
        "foreign_memory",
    )
    free = frozenset(guard.BLIND_KEYS)
    assert _verdict(tmp_path, "Bash", {"command": "echo 1 > n"}, free=free, **blind) is None
    # Абсолютная цель, команда без записи и переход в известную папку — не слепые.
    assert _verdict(tmp_path, "Bash", {"command": f"echo 1 > {repo}/notes.txt"}, **blind) is None
    assert _verdict(tmp_path, "Bash", {"command": "git commit -m x && ls"}, **blind) is None
    assert _verdict(tmp_path, "Bash", {"command": f"cd {repo} && echo 1 > notes.txt"}, **blind) is None
    assert _verdict(tmp_path, "Bash", {"command": "cd sub && echo 1 > notes.txt"}, **blind) == (
        "ask",
        "project_rules",
    )
    # Правка файла по относительному пути — так же.
    assert _verdict(tmp_path, "Write", {"file_path": "notes.txt", "content": "x"}, **blind) == (
        "ask",
        "project_rules",
    )
    assert (
        _verdict(tmp_path, "Write", {"file_path": str(repo / "notes.txt"), "content": "x"}, **blind) is None
    )


# --- 4 и подозрение (а): метка и тип причины ----------------------------------------------------------------


async def test_mark_alone_and_engine_safety_checks_are_cards(env):
    marked = "общий гард: похоже на секрет в команде\n[hub-rule:guard_failure]"
    env.scenario(
        turns=[
            [
                _bash("make test", reason_type="hook", reason=marked),  # метку дописал чужой хук
                _bash("make lint", reason_type="safetyCheck", reason="встроенная проверка движка"),
                _bash("make docs", reason_type="rule"),  # контроль: правило проекта — отвечает сам
                {"text": "готово"},
            ]
        ]
    )
    await _free(env, "guard_failure", "project_permissions", "foreign_hook")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    request = await _card(env, host, sid)
    assert request["rule"] == "guard_failure" and "[hub-rule" not in request["reason"]
    await _deny(env, host, request)
    request = await _card(env, host, sid)
    assert request["rule"] == "project_permissions" and "встроенная проверка" in request["reason"]
    await _deny(env, host, request)
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny", "deny", "allow"]
    assert "всегда карточкой" in autopilot.BY_KEY["project_permissions"].note


# --- 5 и 6. окружение процессов -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ps eww -u $USER",
        "ps e",
        "ps auxe",
        "ps -p 1 e",
        "ps -E",
        "ps axE",
        "bash -c 'ps ewww'",
        "ps aux; ps e 1",
        "/bin/ps ef | grep HUB",
    ],
)
def test_ps_printing_the_environment_is_denied(tmp_path, command):
    assert _verdict(tmp_path, "Bash", {"command": command}) == ("deny", "proc_environ")


@pytest.mark.parametrize(
    "command",
    [
        "ps aux",
        "ps -ef",
        "ps -e -o pid,etime",
        "ps -C node",
        "ps -u user",
        "ps --sort etime",
        "ps -o etime -p 1",
    ],
)
def test_ordinary_ps_passes(tmp_path, command):
    assert _verdict(tmp_path, "Bash", {"command": command}) is None


def test_proc_environ_reads_paths_and_commands_not_file_content(tmp_path):
    repo = tmp_path / "repo"
    text = "ключ лежит в /proc/<pid>/environ, см. /proc/self/environ"
    target = str(repo / "notes.md")
    assert _verdict(tmp_path, "Write", {"file_path": target, "content": text}) is None
    edit = {"file_path": target, "old_string": "a", "new_string": text}
    assert _verdict(tmp_path, "Edit", edit) is None
    # Обращения — по-прежнему запрет.
    assert _verdict(tmp_path, "Bash", {"command": "cat /proc/self/environ"}) == ("deny", "proc_environ")
    assert _verdict(tmp_path, "Read", {"file_path": "/proc/1/environ"}) == ("deny", "proc_environ")
    assert _verdict(tmp_path, "Grep", {"pattern": "KEY", "path": "/proc/1/environ"}) == (
        "deny",
        "proc_environ",
    )
    assert _verdict(tmp_path, "Write", {"file_path": "/proc/1/environ", "content": "x"}) == (
        "deny",
        "proc_environ",
    )


# --- подозрение (б): ключ — у стоящего правила --------------------------------------------------------------


def test_rule_key_names_a_standing_rule():
    """`SendMessage` добавлен проектом под кнопку (`work.ask`), а запрет `cross_session` снят: спрашивает
    `mcp_write` — его ключ и называется (прежде — снятый `cross_session`, и OpenCode разрешал сам)."""
    free = frozenset({"cross_session"})
    settings = policy.settings_config(ask=["SendMessage"], guard_command="", free=free)
    assert policy.decide_tool("SendMessage", settings, {}, free) == "ask"
    assert policy.rule_key("SendMessage", {}) == "cross_session"
    assert policy.rule_key("SendMessage", {}, free) == policy.MCP_WRITE
    assert policy.rule_key("EnterPlanMode", {}, free) == "plan_mode"
