"""Постоянные сессии хаба на OpenCode (Ф41-2б-1б; docs/architecture.md). Ядро — в процессе, `opencode` — ПОДДЕЛЬНЫЙ
сервер `tests/engine/fake_opencode.py`, `claude` —
`fake_claude.py`. Настоящие OpenCode и `claude` здесь не запускаются."""

import json

from hub.engine import oc_config
from hub.work import files
from hub.work import held as held_mod

from .conftest import FORBIDDEN_ENV, pid_alive
from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    set_engine,
    started,
)
from .test_held_host import (
    _alive,
    _events,
    _full,
    _has_event,
    _rec,
    _say,
    _session,
    _state,
    _turn_done,
    _users,
)

MODEL = "deepseek/deepseek-flash"


async def _oc_session(env, **body):
    return await _session(env, engine="opencode", **body)


async def _running(env, host, oc, sid, text="привет", **kw):  # noqa: F811 — фикстура
    before = len(oc.prompts())
    say_id = await _say(env, sid, text, **kw)
    await env.until(
        host, lambda: sid in _alive(host) and len(oc.prompts()) > before, what="сервер и первый ход"
    )
    return say_id


async def test_oc_held_launch_config_env_and_feed(env, oc):  # noqa: F811
    oc.scenario(
        [{"text": "Смотрю"}, {"ask": {"tool": "bash", "args": {"command": "ls"}}}, {"text": "Готово"}]
    )
    host = await started(env)
    assert host.engine == "claude"  # движок хоста — Claude, движок сессии — её свойство
    sid, _pid, workdir = await _oc_session(env, role="тестер", model="deepseek/deepseek-chat", effort="low")
    for _ in range(3):
        await env.cycle(host)
    assert oc.jsonl("starts.jsonl") == [] and host.held.runners == {}  # лениво
    await _running(env, host, oc, sid, "посмотри")
    await _turn_done(env, host, sid)
    rec = await _rec(env, sid)
    assert env.calls() == []  # claude не запускался
    assert rec["engine_session_id"].startswith("ses_") and rec["state"] == "idle" and rec["queued_says"] == 0
    # сессия OpenCode: заголовок, агент hub-project, модель и вариант — из записи
    [created] = oc.created()
    assert created["body"]["title"] == f"hub #{sid}: тестер 1"
    [prompt] = oc.prompts()
    body = prompt["body"]
    assert prompt["path"] == f"/session/{rec['engine_session_id']}/prompt_async"
    assert body["agent"] == "hub-project" and body["variant"] == "low"
    assert body["model"] == {"providerID": "deepseek", "modelID": "deepseek-chat"}
    assert f"постоянная сессия хаба владельца #{sid}" in body["system"] and "OpenCode" in body["system"]
    assert "сама на проект не подписывайся" in body["system"]
    assert body["parts"][0]["text"] == "посмотри"
    assert oc.jsonl("calls.jsonl")[0]["query"]["directory"] == str(workdir)
    # окружение сервера: личность сессии хаба, без признаков задачи по «Го»
    server_env = oc.json("env.json")
    assert server_env["HUB_SESSION"] == str(sid)
    assert "HUB_WORK" not in server_env and "HUB_WORK_TASK" not in server_env
    assert not set(FORBIDDEN_ENV) & set(server_env)
    # конфиг — в своём каталоге сессии; права агента — «ask» на всё, подагенты — только свои
    config = oc.json("config.json")
    assert config["agent"]["hub-project"]["permission"] == oc_config.PERM_WORK
    assert config["agent"]["hub-project"]["model"] == "deepseek/deepseek-chat"
    assert config["agent"]["build"] == config["agent"]["plan"] == {"disable": True}
    assert (files.held_dir(env.data_dir, sid) / "opencode-held.json").exists()
    # `ls` — гард и правила молчат -> once
    assert [(r["tool"], r["reply"]) for r in oc.replies()] == [("bash", "once")]
    kinds = [(e["kind"], e["summary"]) for e in await _events(env, sid)]
    assert ("text", "Смотрю") in kinds and ("text", "Готово") in kinds
    assert any(
        k == "start" and "запуск новой сессии OpenCode (deepseek/deepseek-chat)" in s for k, s in kinds
    )
    assert any(k == "turn_end" and "$" in s for k, s in kinds) and not any(k == "error" for k, _s in kinds)


async def test_oc_held_guard_profile_cards_and_denials(env, oc):  # noqa: F811
    host = await started(env)
    sid, _pid, workdir = await _oc_session(env, model=MODEL)
    edit = {"filePath": str(workdir / "app.py"), "oldString": "1", "newString": "2"}
    rules = {"filePath": str(workdir / "CLAUDE.md"), "content": "x"}
    secret = {"filePath": str(env.home / ".config" / "hub" / "work" / "token")}
    oc.scenario(
        [
            {"ask": {"tool": "edit", "args": edit}},  # основное дерево — можно (профиль held)
            {"ask": {"tool": "read", "args": secret}},  # каталоги хаба — отказ
            {"ask": {"tool": "write", "args": rules}},  # правила проекта — карточка
            {"text": "всё"},
        ]
    )
    await _running(env, host, oc, sid)

    async def pending():
        return (await _full(env, sid))["requests"]

    [request] = await env.until(host, pending, what="карточка")
    assert request["tool"] == "Write" and request["input"]["file_path"] == rules["filePath"]
    assert "правка правил и настроек проекта" in request["reason"]
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "deny"})
    assert r.status_code == 200
    await _turn_done(env, host, sid)
    replies = [(r["tool"], r["reply"]) for r in oc.replies()]
    assert replies == [("edit", "once"), ("read", "reject"), ("write", "reject")]  # `always` — никогда
    assert await _has_event(env, sid, "warn", "каталоги конфига и данных хаба")()
    assert await _has_event(env, sid, "decision", "владелец отказал: Write")()


async def test_oc_held_now_aborts_and_is_not_error(env, oc):  # noqa: F811
    oc.scenario([{"hang": True}], [{"text": "срочное принято"}])
    host = await started(env)
    sid, _, _ = await _oc_session(env, model=MODEL)
    await _running(env, host, oc, sid, "долгое")
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _say(env, sid, "срочно", priority="now")
    await env.until(host, lambda: len(oc.prompts()) == 2, what="срочная реплика подана")
    await _turn_done(env, host, sid)
    assert len(oc.aborts()) == 1
    events = await _events(env, sid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("interrupt") == 1 and "error" not in kinds and not any(e["is_error"] for e in events)
    assert any(e["summary"] == "срочное принято" for e in events)
    assert len(oc.jsonl("starts.jsonl")) == 1  # сервер жив, разговор тот же


async def test_oc_held_sleep_and_resume_same_conversation(env, oc):  # noqa: F811
    oc.scenario([{"text": "раз"}], [{"text": "два"}])
    host = await started(env)
    sid, _, _ = await _oc_session(env, model=MODEL)
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    ses = (await _rec(env, sid))["engine_session_id"]
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    await env.until(
        host, lambda: sid not in host.held.runners and not oc.server_alive(), what="сервер погашен"
    )
    assert await _has_event(env, sid, "status", "сессия усыплена")()
    await _running(env, host, oc, sid, "ещё")
    await _turn_done(env, host, sid)
    assert len(oc.created()) == 1 and len(oc.jsonl("starts.jsonl")) == 2  # новая сессия не создавалась
    assert oc.prompts()[-1]["path"] == f"/session/{ses}/prompt_async"
    assert (await _rec(env, sid))["engine_session_id"] == ses
    assert await _has_event(env, sid, "start", "продолжение сессии OpenCode")()


async def test_oc_held_missing_conversation_is_failure_not_new_session(env, oc):  # noqa: F811
    oc.scenario([{"text": "раз"}])
    host = await started(env, held_launch_retry_s=3600)
    sid, _, _ = await _oc_session(env, model=MODEL)
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    ses = (await _rec(env, sid))["engine_session_id"]
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(
        host, lambda: sid not in host.held.runners and not oc.server_alive(), what="сервер погашен"
    )
    (oc.dir / "sessions.json").write_text("[]")  # разговор удалили в OpenCode
    await _say(env, sid, "ещё")

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert "разговора сессии в OpenCode больше нет" in failure
    rec = await _rec(env, sid)
    assert rec["engine_session_id"] == ses and rec["state"] == "asleep" and rec["queued_says"] == 1
    assert len(oc.prompts()) == 1 and not oc.server_alive() and sid not in host.held.runners
    # разговор, который OpenCode создал взамен пропавшего, из общего списка убран
    [stray] = [c["path"] for c in oc.jsonl("calls.jsonl") if c["method"] == "DELETE"]
    assert stray.startswith("/session/ses_") and stray != f"/session/{ses}"
    assert oc.json("sessions.json") == []


async def test_oc_held_off_switch_and_bad_model(env, oc):  # noqa: F811
    host = await started(env, held_opencode=False, held_launch_retry_s=0)
    sid, _, _ = await _oc_session(env, model=MODEL)
    await _say(env, sid)

    async def failed():
        return (await _rec(env, sid))["failure"]

    assert await env.until(host, failed, what="причина") == held_mod.OPENCODE_OFF
    for _ in range(5):
        await env.cycle(host)
    errors = [e for e in await _events(env, sid) if e["kind"] == "error"]
    assert len(errors) == 1 and oc.jsonl("starts.jsonl") == []  # одна причина на реплику, без повторов
    # модель без «провайдер/» — причина, а не молчаливая подмена
    await env.ui("POST", f"/sessions/{sid}/close")
    await env.cycle(host)
    host.config["held_opencode"] = True
    bad, _, _ = await _oc_session(env, name="B", model="flash")
    await _say(env, bad)

    async def bad_failed():
        return (await _rec(env, bad))["failure"]

    assert "нужно «провайдер/модель»" in await env.until(host, bad_failed, what="причина модели")
    assert oc.jsonl("starts.jsonl") == []


async def test_oc_held_own_cap_and_two_engines_side_by_side(env, oc):  # noqa: F811
    oc.scenario([{"text": "ок"}])
    env.scenario(turns=[[{"text": "клод"}]])
    host = await started(env, held_max_parallel_opencode=1)
    project = env.project("Initech")
    first, _, _ = await _oc_session(env, project=project, model=MODEL)
    second, _, _ = await _oc_session(env, project=project, model=MODEL)
    claude, _, _ = await _session(env, project=project)
    await _running(env, host, oc, first)
    await _turn_done(env, host, first)
    await _say(env, claude)
    await env.until(host, lambda: claude in _alive(host) and _users(env), what="сессия claude")
    await _turn_done(env, host, claude)
    assert _alive(host) == [first, claude]  # два движка одновременно
    assert {host.held.runners[first].engine, host.held.runners[claude].engine} == {"opencode", "claude"}
    # второй сервер OpenCode — сверх своего потолка: усыпляется простаивающая OpenCode, не claude
    await _say(env, second)
    await env.until(host, lambda: second in _alive(host), what="вторая сессия OpenCode")
    await _turn_done(env, host, second)
    assert _alive(host) == [second, claude]
    assert (await _rec(env, first))["state"] == "asleep" and (await _rec(env, claude))["state"] == "idle"
    assert await _has_event(env, first, "status", "место понадобилось другой сессии")()
    # переключатель движка хоста и пауза OpenCode постоянные сессии не трогают
    set_engine(env, "opencode")
    await env.until(host, lambda: host.engine == "opencode", what="движок хоста opencode")
    host.on_rate_limit({"status": "rejected", "engine": "opencode", "reason": "лимит провайдера"})
    assert host.state["pauses"]["opencode"]["reason"] == "лимит провайдера"
    for _ in range(3):
        await env.cycle(host)
    assert _alive(host) == [second, claude]
    pid = env.calls()[0]["pid"]
    assert pid_alive(pid) and json.loads(files.state_path(env.data_dir).read_text())["engine"] == "opencode"
    # сессия claude поднимается и при движке хоста opencode с паузой на нём (П6)
    await env.ui("POST", f"/sessions/{claude}/sleep")
    await env.until(host, lambda: claude not in host.held.runners, what="claude усыплена")
    await _say(env, claude, "ещё")
    await env.until(
        host, lambda: len(env.calls()) == 2 and claude in _alive(host), what="claude поднята снова"
    )


async def test_oc_held_parity_rules_and_mcp_like_claude(env, oc):  # noqa: F811
    """Ф71-3: у сессии хаба на OpenCode — `~/.claude/CLAUDE.md` в начале системного текста, без глобального
    `AGENTS.md` OpenCode (свой `OPENCODE_CONFIG_DIR` с пустым `AGENTS.md`), MCP — набор Claude Code минус
    `disabledMcpServers` папки проекта, свой MCP хаба, прочие глобальные — выключены."""
    oc.scenario([{"text": "ок"}])
    host = await started(env)
    sid, _pid, workdir = await _oc_session(env, model="qwen/qwen3.8-max")
    (env.home / ".claude").mkdir(exist_ok=True)
    (env.home / ".claude" / "CLAUDE.md").write_text("ОБЩЕЕ-ПРАВИЛО-ВЛАДЕЛЬЦА", encoding="utf-8")
    (env.home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ru-edit": {"type": "stdio", "command": "/bin/ru", "args": ["x"]},
                    "stage-k8s": {"type": "http", "url": "https://k", "headers": {"Authorization": "B t"}},
                    "hub-all": {"type": "stdio", "command": "/bin/чужой-hub"},
                },
                "projects": {str(workdir): {"disabledMcpServers": ["stage-k8s", "claude.ai Notion - Initech"]}},
            }
        ),
        encoding="utf-8",
    )
    (env.home / ".cursor").mkdir(exist_ok=True)
    (env.home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": "https://n"}, "globex-gitlab": {"url": "https://g"}}}),
        encoding="utf-8",
    )
    (env.home / ".config" / "opencode").mkdir(parents=True, exist_ok=True)
    (env.home / ".config" / "opencode" / "opencode.jsonc").write_text(
        '{// глобальный конфиг владельца\n "mcp": {"searxng": {}, "globex-gitlab": {}, "hub-all": {}}}',
        encoding="utf-8",
    )
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    [prompt] = oc.prompts()
    system = prompt["body"]["system"]
    assert system.startswith("Общие правила владельца") and "ОБЩЕЕ-ПРАВИЛО-ВЛАДЕЛЬЦА" in system
    assert "Ты работаешь в OpenCode" in system and "резерв" not in system.lower()
    assert held_mod.NO_RU_EDIT_NOTE not in system
    assert system.index("ОБЩЕЕ-ПРАВИЛО-ВЛАДЕЛЬЦА") < system.index(f"постоянная сессия хаба владельца #{sid}")
    mcp = oc.json("config.json")["mcp"]
    assert mcp["ru-edit"] == {"type": "local", "command": ["/bin/ru", "x"], "enabled": True}
    assert mcp["globex-gitlab"] == {"type": "remote", "url": "https://g", "enabled": True}
    assert mcp["hub-all"]["command"][1:] == ["mcp", "--profile", "all"]
    assert mcp["hub-all"]["command"][0] != "/bin/чужой-hub"
    assert mcp["hub-all"]["environment"] == {
        "HUB_CONFIG_DIR": str(env.config_dir),
        "HUB_DATA_DIR": str(env.data_dir),
    }
    # выключены: глобальное без пары у Claude, `disable_mcp` из engine.json, отключённые в проекте
    for name in ("searxng", "playwright"):
        assert mcp[name] == {"enabled": False}
    assert "stage-k8s" not in mcp and "notion" not in mcp
    server_env = oc.json("env.json")
    rules_dir = env.data_dir / "engine" / held_mod.OC_RULES_DIR
    assert server_env["OPENCODE_CONFIG_DIR"] == str(rules_dir)
    assert (rules_dir / "AGENTS.md").read_text(encoding="utf-8") == ""


async def test_oc_held_deepseek_has_no_ru_edit(env, oc):  # noqa: F811
    """Слово владельца: у сессии OpenCode на DeepSeek нет сервера ru-edit (редактор — та же модель), а в
    правилах — строка, что правило про ru_edit к ней не относится."""
    oc.scenario([{"text": "ок"}])
    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL)
    (env.home / ".claude").mkdir(exist_ok=True)
    (env.home / ".claude" / "CLAUDE.md").write_text("ОБЩЕЕ-ПРАВИЛО-ВЛАДЕЛЬЦА", encoding="utf-8")
    (env.home / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"ru-edit": {"type": "stdio", "command": "/bin/ru"}, "searxng": {"command": "/s"}}}
        ),
        encoding="utf-8",
    )
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    [prompt] = oc.prompts()
    system = prompt["body"]["system"]
    assert held_mod.NO_RU_EDIT_NOTE in system
    assert system.index("ОБЩЕЕ-ПРАВИЛО-ВЛАДЕЛЬЦА") < system.index(held_mod.NO_RU_EDIT_NOTE)
    mcp = oc.json("config.json")["mcp"]
    assert "ru-edit" not in mcp
    assert mcp["searxng"]["enabled"] is True
