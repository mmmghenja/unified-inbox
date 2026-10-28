"""Слово владельца: у сессии Cursor — MCP Claude Code и общий `~/.claude/CLAUDE.md`."""

import json

from hub.engine import cursor_mcp
from hub.engine.cursor_process import write_plugin


def _claude(home, servers):
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))


def test_claude_servers_are_copied_except_hub_oauth_and_cursor_own(tmp_path):
    _claude(
        tmp_path,
        {
            "ru-edit": {"type": "stdio", "command": "/bin/ru", "args": [], "env": {}},
            "searxng": {"command": "/bin/sx", "args": ["-a"], "env": {"SEARXNG_URL": "http://x"}},
            "hub-all": {"type": "stdio", "command": "/bin/hub", "args": ["mcp"]},
            "stage-k8s": {"type": "http", "url": "https://k", "headers": {"Authorization": "Bearer t"}},
            "umbrella-oauth": {"type": "http", "url": "https://i/mcp"},
            "notion": {"type": "stdio", "command": "/bin/n"},
        },
    )
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": "https://n"}}})
    )
    got = cursor_mcp.claude_user_servers(tmp_path)
    assert got == {
        "ru-edit": {"command": "/bin/ru", "args": []},
        "searxng": {"command": "/bin/sx", "args": ["-a"], "env": {"SEARXNG_URL": "http://x"}},
        "stage-k8s": {"url": "https://k", "headers": {"Authorization": "Bearer t"}},
    }


def test_no_or_broken_claude_config_gives_nothing(tmp_path):
    assert cursor_mcp.claude_user_servers(tmp_path) == {}
    (tmp_path / ".claude.json").write_text("{не json")
    assert cursor_mcp.claude_user_servers(tmp_path) == {}


def test_plugin_gets_extra_servers_and_hub_wins_its_name(tmp_path):
    write_plugin(
        tmp_path / "p",
        name="s1",
        hook_command=["/bin/hook"],
        mcp_name="hub-all",
        mcp_command=["/bin/hub", "mcp"],
        mcp_env={},
        extra_servers={"ru-edit": {"command": "/bin/ru", "args": []}, "hub-all": {"command": "/bin/evil"}},
    )
    servers = json.loads((tmp_path / "p" / "mcp.json").read_text())["mcpServers"]
    assert servers["ru-edit"] == {"command": "/bin/ru", "args": []}
    assert servers["hub-all"]["command"] == "/bin/hub"


def test_global_rules_block(tmp_path):
    assert cursor_mcp.global_rules(tmp_path, None, None) == ""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("# Общие\nправило А")
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("правило проекта Б")
    same = cursor_mcp.global_rules(tmp_path, str(proj), str(proj))
    assert "правило А" in same and "истина для всех моделей" in same and "правило проекта Б" not in same
    sub = cursor_mcp.global_rules(tmp_path, str(proj), str(proj / "sub"))
    assert "правило А" in sub and "правило проекта Б" in sub


def test_workspace_slug():
    assert cursor_mcp.workspace_slug("/home/owner/Hooli") == "home-owner-Hooli"
    assert cursor_mcp.workspace_slug("/home/owner/projects/hub/.runs/x") == "home-owner-projects-hub-runs-x"


def test_auth_is_shared_from_freshest_login(tmp_path):
    import os

    projects = tmp_path / ".cursor" / "projects"
    (projects / "home-user").mkdir(parents=True)
    (projects / "home-owner-Hooli").mkdir()
    src = projects / "home-user" / "mcp-auth.json"
    src.write_text(
        json.dumps({"yt": {"tokens": {"a": 1}, "clientInfo": {"c": 1}}, "nt": {"clientInfo": {"c": 2}}})
    )
    dest = projects / "home-owner-Hooli" / "mcp-auth.json"
    dest.write_text(json.dumps({"yt": {"clientInfo": {"c": 9}, "extra": 5}}))
    os.utime(dest, (1, 1))
    assert cursor_mcp.share_auth(tmp_path, "/home/owner/Hooli") == ["yt"]
    got = json.loads(dest.read_text())
    assert got["yt"] == {"tokens": {"a": 1}, "clientInfo": {"c": 1}, "extra": 5}
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    # свой файл теперь свежее — ничего не переносится; новая папка получает вход
    assert cursor_mcp.share_auth(tmp_path, "/home/owner/Hooli") == []
    assert cursor_mcp.share_auth(tmp_path, "/home/owner/Globex") == ["yt"]
    assert cursor_mcp.share_auth(tmp_path / "nohome", "/x") == []


def test_project_servers_join_and_cursor_project_config_wins(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".cursor").mkdir(parents=True)
    _claude(tmp_path, {"glob": {"command": "/bin/g"}, "kb": {"command": "/bin/old"}})
    (proj / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"kb": {"command": "/bin/kb"}, "bridge": {"command": "/bin/b"}}})
    )
    (proj / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"bridge": {"command": "/bin/b"}}}))
    got = cursor_mcp.claude_user_servers(tmp_path, str(proj))
    assert got == {"glob": {"command": "/bin/g", "args": []}, "kb": {"command": "/bin/kb", "args": []}}


# --- Ф71-3: паритет OpenCode и разнос MCP по проектам ----------------------------------------------------


def _cursor(home, servers):
    (home / ".cursor").mkdir(exist_ok=True)
    (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": servers}))


def test_opencode_servers_format(tmp_path):
    _claude(
        tmp_path,
        {
            "ru-edit": {"type": "stdio", "command": "/bin/ru", "args": ["-x", 1], "env": {}},
            "searxng": {"command": "/bin/sx", "env": {"SEARXNG_URL": "http://x"}},
            "hub-all": {"type": "stdio", "command": "/bin/hub", "args": ["mcp"]},
            "stage-k8s": {"type": "http", "url": "https://k", "headers": {"Authorization": "Bearer t"}},
            "umbrella-oauth": {"type": "http", "url": "https://i/mcp"},
            "bad": {"type": "http"},
        },
    )
    _cursor(
        tmp_path,
        {"umbrella-oauth": {"url": "https://other"}, "notion": {"url": "https://n"}, "hubx": {"url": "u"}},
    )
    assert cursor_mcp.opencode_servers(tmp_path) == {
        "ru-edit": {"type": "local", "command": ["/bin/ru", "-x", "1"], "enabled": True},
        "searxng": {
            "type": "local",
            "command": ["/bin/sx"],
            "environment": {"SEARXNG_URL": "http://x"},
            "enabled": True,
        },
        "stage-k8s": {
            "type": "remote",
            "url": "https://k",
            "headers": {"Authorization": "Bearer t"},
            "enabled": True,
        },
        # сервер OAuth Claude Code — remote без заголовков; имя Claude выигрывает у Cursor
        "umbrella-oauth": {"type": "remote", "url": "https://i/mcp", "enabled": True},
        # серверы OAuth Cursor (коннекторы claude.ai у Claude Code)
        "notion": {"type": "remote", "url": "https://n", "enabled": True},
    }


def test_disabled_servers_of_workdir_and_cwd(tmp_path):
    proj = tmp_path / "proj"
    sub = proj / "sub"
    sub.mkdir(parents=True)
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "stage-k8s": {"type": "http", "url": "https://k", "headers": {"A": "b"}},
                    "ru-edit": {"type": "stdio", "command": "/bin/ru"},
                    "searxng": {"type": "stdio", "command": "/bin/sx"},
                },
                "projects": {
                    str(proj): {
                        "disabledMcpServers": [
                            "stage-k8s",
                            "claude.ai Notion - Initech",
                            "plugin:atlassian:atlassian",
                            "Claude.ai Globex-Gitlab",
                        ]
                    },
                    str(sub): {"disabledMcpServers": ["ru-edit"]},
                    str(tmp_path): {"disabledMcpServers": ["searxng"]},  # чужой ключ — не действует
                },
            }
        )
    )
    _cursor(
        tmp_path,
        {
            "notion": {"url": "https://n"},
            "atlassian": {"url": "https://a"},
            "globex-gitlab": {"url": "https://g"},
            "globex-pyrus": {"url": "https://p"},
        },
    )
    assert cursor_mcp.claude_disabled(tmp_path, str(proj), str(sub)) == {
        "stage-k8s",
        "claude.ai Notion - Initech",
        "plugin:atlassian:atlassian",
        "Claude.ai Globex-Gitlab",
        "ru-edit",
    }
    assert cursor_mcp.claude_disabled(tmp_path) == set()
    oc_sub = cursor_mcp.opencode_servers(tmp_path, str(proj), str(sub))
    assert sorted(oc_sub) == ["globex-pyrus", "searxng"]
    oc_root = cursor_mcp.opencode_servers(tmp_path, str(proj), str(proj))
    assert sorted(oc_root) == ["globex-pyrus", "ru-edit", "searxng"]
    assert sorted(cursor_mcp.claude_user_servers(tmp_path, str(proj), str(sub))) == ["searxng"]
    assert sorted(cursor_mcp.claude_user_servers(tmp_path)) == ["ru-edit", "searxng", "stage-k8s"]


def test_oauth_name_matching():
    assert cursor_mcp.oauth_disabled("notion", {"claude.ai Notion - Initech"})
    assert cursor_mcp.oauth_disabled("atlassian", {"plugin:atlassian:atlassian"})
    assert cursor_mcp.oauth_disabled("globex-youtrack", {"claude.ai globex-youtrack"})
    assert cursor_mcp.oauth_disabled("umbrella-oauth", {"umbrella-oauth"})
    assert not cursor_mcp.oauth_disabled("notion", {"claude.ai Slack", "notion-x"})
    assert not cursor_mcp.oauth_disabled("notion", set())


def test_global_rules_opencode_only_common_file_and_no_reserve(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("правило А")
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("правило проекта Б")
    text = cursor_mcp.global_rules(tmp_path, str(proj), str(proj / "sub"), engine="opencode")
    # `CLAUDE.md` проекта OpenCode читает сам — не дублировать
    assert "правило А" in text and "правило проекта Б" not in text
    head = text.split("\n\n", 1)[0]
    assert "OpenCode" in head and "Claude Code и Cursor" in head
    assert "резерв" not in text.lower() and "Grok" not in head and "deepseek" not in head.lower()
    cursor = cursor_mcp.global_rules(tmp_path, str(proj), str(proj / "sub"))
    assert "Ты работаешь в Cursor" in cursor and "Claude Code и OpenCode" in cursor


def test_sync_disabled_writes_cursor_project_list_from_claude_names(tmp_path):
    """Ф71-3: серверы OAuth Cursor выключаются файлом `mcp-disabled.json` корня git проекта по
    `disabledMcpServers` Claude Code; чужие записи остаются, снова включённые — убираются."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    sub = proj / "sub"
    sub.mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    str(proj): {
                        "disabledMcpServers": ["claude.ai Notion - Initech", "plugin:atlassian:atlassian"]
                    },
                    str(sub): {"disabledMcpServers": ["claude.ai globex-gitlab"]},
                }
            }
        )
    )
    _cursor(
        tmp_path,
        {
            n: {"url": f"https://{n}"}
            for n in ("notion", "atlassian", "globex-gitlab", "globex-pyrus", "hub-x")
        },
    )
    target = tmp_path / ".cursor" / "projects" / cursor_mcp.workspace_slug(str(proj)) / "mcp-disabled.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(["свой-сервер", "globex-pyrus"]))
    # из подпапки — список корня git (как у Cursor), выключенное — объединение ключей проекта и подпапки
    assert cursor_mcp.sync_disabled(tmp_path, str(proj), str(sub)) == [
        "atlassian",
        "globex-gitlab",
        "notion",
    ]
    assert json.loads(target.read_text()) == ["свой-сервер", "atlassian", "globex-gitlab", "notion"]
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    # в корне проекта gitlab снова включён — убирается из списка
    assert cursor_mcp.sync_disabled(tmp_path, str(proj), str(proj)) == ["atlassian", "notion"]
    assert json.loads(target.read_text()) == ["свой-сервер", "atlassian", "notion"]


def test_sync_disabled_without_lists_writes_nothing(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _cursor(tmp_path, {"notion": {"url": "https://n"}})
    assert cursor_mcp.sync_disabled(tmp_path, str(proj), str(proj)) == []
    assert not (tmp_path / ".cursor" / "projects").exists()
    assert cursor_mcp.project_root(str(proj)) == str(proj)


def test_auth_goes_to_git_root_of_subdir_session(tmp_path):
    """Ф71-3: Cursor читает вход из папки корня git — сессия в подпапке получает его туда."""
    home = tmp_path / "home"
    projects = home / ".cursor" / "projects"
    (projects / "src").mkdir(parents=True)
    (projects / "src" / "mcp-auth.json").write_text(json.dumps({"yt": {"tokens": {"a": 1}}}))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    assert cursor_mcp.share_auth(home, str(repo / "sub")) == ["yt"]
    root_file = projects / cursor_mcp.workspace_slug(str(repo)) / "mcp-auth.json"
    assert json.loads(root_file.read_text())["yt"]["tokens"] == {"a": 1}
    assert not (projects / cursor_mcp.workspace_slug(str(repo / "sub"))).exists()


def test_codex_servers_format_and_codex_config_names(tmp_path):
    """Ф74-5: набор Claude Code в формате `mcp_servers` Codex (stdio — command/args/env, http — url и
    `http_headers`), OAuth-серверы Cursor — если имени нет у Claude; имена из конфигов Codex — для
    выключения."""
    _claude(
        tmp_path,
        {
            "ru-edit": {"type": "stdio", "command": "/bin/ru", "args": ["-x", 1], "env": {}},
            "searxng": {"command": "/bin/sx", "env": {"SEARXNG_URL": "http://x"}},
            "hub-all": {"type": "stdio", "command": "/bin/hub", "args": ["mcp"]},
            "stage-k8s": {"type": "http", "url": "https://k", "headers": {"Authorization": "Bearer t"}},
            "umbrella-oauth": {"type": "http", "url": "https://i/mcp"},
        },
    )
    _cursor(tmp_path, {"umbrella-oauth": {"url": "https://other"}, "notion": {"url": "https://n"}})
    assert cursor_mcp.codex_servers(tmp_path) == {
        "ru-edit": {"command": "/bin/ru", "args": ["-x", "1"]},
        "searxng": {"command": "/bin/sx", "args": [], "env": {"SEARXNG_URL": "http://x"}},
        "stage-k8s": {"url": "https://k", "http_headers": {"Authorization": "Bearer t"}},
        "umbrella-oauth": {"url": "https://i/mcp"},
        "notion": {"url": "https://n"},
    }
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[mcp_servers.hub-ext]\ncommand = "hub"\n')
    project = tmp_path / "proj"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text('[mcp_servers."gitlab"]\nurl = "https://g"\n')
    broken = tmp_path / "broken"
    (broken / ".codex").mkdir(parents=True)
    (broken / ".codex" / "config.toml").write_text("[mcp_servers\n")
    names = cursor_mcp.codex_config_mcp_names(tmp_path, [str(project), str(broken), None])
    assert names == {"hub-ext", "gitlab"}


def test_codex_global_rules_not_twice_when_codex_agents_md_has_claude_md(tmp_path):
    """Ф74-5б: `~/.codex/AGENTS.md` уже несёт текст `~/.claude/CLAUDE.md` (+ вставка сисадмина) — текст правил
    в системный текст Codex-сессии не кладётся, шапка (подагенты) остаётся; нет файла или текст другой —
    блок целиком, как раньше. `AGENTS.override.md` и `$CODEX_HOME` — как у Codex."""
    (tmp_path / ".claude").mkdir()
    rules = "# Общие правила\n\nПравило владельца номер один."
    (tmp_path / ".claude" / "CLAUDE.md").write_text(rules + "\n", encoding="utf-8")
    full = cursor_mcp.global_rules(tmp_path, None, None, engine="codex")
    assert "Правило владельца номер один." in full
    assert cursor_mcp.codex_global_rules(tmp_path, None, None, {}) == full  # AGENTS.md нет
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "AGENTS.md").write_text("# своё\nдругие правила\n", encoding="utf-8")
    assert cursor_mcp.codex_global_rules(tmp_path, None, None, {}) == full  # текст другой
    (codex / "AGENTS.md").write_text(rules + "\n\n## Вставка для Codex\nещё строка\n", encoding="utf-8")
    short = cursor_mcp.codex_global_rules(tmp_path, None, None, {})
    assert "Правило владельца номер один." not in short
    assert "spawn_agent" in short and str(codex / "AGENTS.md") in short
    # Codex берёт AGENTS.override.md раньше AGENTS.md: в нём текста нет — блок целиком.
    (codex / "AGENTS.override.md").write_text("только override\n", encoding="utf-8")
    assert cursor_mcp.codex_global_rules(tmp_path, None, None, {}) == full
    other = tmp_path / "codex-home"
    other.mkdir()
    (other / "AGENTS.md").write_text(rules, encoding="utf-8")
    assert "Правило владельца номер один." not in cursor_mcp.codex_global_rules(
        tmp_path, None, None, {"CODEX_HOME": str(other)}
    )


def test_opencode_global_rules_not_twice_when_instructions_name_claude_md(tmp_path):
    """Ф74-5б: общий конфиг OpenCode владельца называет `~/.claude/CLAUDE.md` в `instructions` — OpenCode подаёт
    его сессии хаба сам (проба: свой конфиг хаба их не снимает), текст правил хаб не повторяет; другие записи,
    относительные пути и чужой файл — блок целиком. `$XDG_CONFIG_HOME` и шаблоны — как у OpenCode."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("Правило-маяк владельца.\n", encoding="utf-8")
    full = cursor_mcp.global_rules(tmp_path, None, None, engine="opencode")
    assert "Правило-маяк владельца." in full
    assert cursor_mcp.opencode_global_rules(tmp_path, None, None, {}) == full  # конфига нет
    conf = tmp_path / ".config" / "opencode"
    conf.mkdir(parents=True)
    cfg = conf / "opencode.jsonc"
    text = '{\n  // мой\n  "instructions": ["CLAUDE.md", "/elsewhere/rules.md"]\n}\n'
    cfg.write_text(text, encoding="utf-8")
    assert cursor_mcp.opencode_global_rules(tmp_path, None, None, {}) == full
    for entry in ("~/.claude/CLAUDE.md", str(tmp_path / ".claude" / "CLAUDE.md"), "~/.claude/*.md"):
        cfg.write_text(json.dumps({"instructions": ["/elsewhere/rules.md", entry]}), encoding="utf-8")
        short = cursor_mcp.opencode_global_rules(tmp_path, None, None, {})
        assert "Правило-маяк владельца." not in short, entry
        assert "OpenCode" in short and entry in short
    other = tmp_path / "xdg"
    (other / "opencode").mkdir(parents=True)
    (other / "opencode" / "opencode.json").write_text('{"instructions": []}', encoding="utf-8")
    assert cursor_mcp.opencode_global_rules(tmp_path, None, None, {"XDG_CONFIG_HOME": str(other)}) == full
