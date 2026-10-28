"""Разрешения рабочей сессии (Ф9-1, 9a, D; поправки П2, П3, П15, П16; сведение 9a-a).

Рабочая сессия = обычная сессия владельца в папке проекта (режим `bypassPermissions`): всё без запросов, КРОМЕ
`permissions.ask` (карточка владельцу) и `permissions.deny` (отказ) из `--settings` — они сильнее режима и любого
allow (ФЧ2 п.2). После 9a-a в `permissions.ask` — ТОЛЬКО MCP-кнопки этапа 1; все Bash-кнопки (push любой
формой, ssh/scp/rsync/sftp, деплой, trello.sh, gh…) — в хуке `hub-work-guard` (`guard.py`).

Имена MCP-инструментов сверены по `init.tools` живой сессии 2.1.281 после подключения коннекторов claude.ai
(24.09.2026, `.runs/work-9a/logs/l4-tools.json`); GitHub-плагин сейчас не подключён — правило на отсутствующее
имя безвредно. Шаблон `mcp__<сервер>__*` для целого сервера — только если все его инструменты пишущие (таких
нет): у серверов с пишущими инструментами — явный список пишущих (`ask`) и читающих (`KNOWN_READ`). П16:
итоговый `ask` = умолчание ∪ добавки проекта − «сама» проекта; хост сверяет `init.tools` каждого хода и пишет
событие-предупреждение про незнакомые инструменты серверов, у которых есть пишущие."""

import fnmatch

SLACK = "mcp__claude_ai_Slack__"
GITLAB = "mcp__claude_ai_globex-gitlab__"
YOUTRACK = "mcp__claude_ai_globex-youtrack__"
ATLASSIAN = "mcp__plugin_atlassian_atlassian__"
NOTION = "mcp__claude_ai_Notion_-_Initech__"
DOCS = "mcp__claude_ai_Claude_Docs__"
umbrella = "mcp__umbrella-oauth__"
GITHUB = "mcp__plugin_github_github__"

# Кнопки этапа 1 (таблица 11.14): сообщения в каналы в обход хаба, MR/слияние/пайплайны, правка задач
# трекеров,
# отправка документов Umbrella, правка документов.
_WRITE = {
    SLACK: (
        "slack_send_message",
        "slack_schedule_message",
        "slack_send_message_draft",
        "slack_create_canvas",
        "slack_update_canvas",
        "slack_add_reaction",
        "slack_create_conversation",
        "slack_create_list",
        "slack_update_list",
        "slack_add_list_record",
        "slack_update_list_record",
        "slack_get_file_upload_url",
        "slack_complete_file_upload",
    ),
    GITLAB: (
        "gl_merge_mr",
        "gl_create_mr",
        "gl_update_mr",
        "gl_commit",
        "gl_create_branch",
        "gl_delete_branch",
        "gl_create_tag",
        "gl_rebase_mr",
        "gl_run_pipeline",
        "gl_retry_pipeline",
        "gl_retry_job",
        "gl_play_pipeline_job",
        "gl_cancel_pipeline",
        "gl_cancel_job",
        "gl_add_mr_note",
    ),
    YOUTRACK: (
        "youtrack_create_issue",
        "youtrack_update_issue",
        "youtrack_add_comment",
        "youtrack_close_issue",
        "youtrack_execute_commands",
        "youtrack_link_issues",
        "youtrack_attach_file",
        "youtrack_create_work_item",
        "youtrack_update_work_item",
        "youtrack_delete_work_item",
    ),
    ATLASSIAN: (
        "createJiraIssue",
        "editJiraIssue",
        "addOrEditJiraIssueComment",
        "transitionJiraIssue",
        "executeWrite",
        "executeDestructive",
        "createConfluenceContent",
        "updateConfluenceContent",
        "addGraphContext",
    ),
    NOTION: (
        "notion-create-pages",
        "notion-create-comment",
        "notion-create-database",
        "notion-create-view",
        "notion-create-folder",
        "notion-create-attachment",
        "notion-create-file-upload",
        "notion-update-page",
        "notion-update-data-source",
        "notion-update-folder",
        "notion-update-view",
        "notion-move-pages",
        "notion-duplicate-page",
        "notion-spawn-session",
        "notion-send-message-to-session",
        "notion-stop-session",
        "notion-upload-skill",
        "notion-convert-page-to-skill",
    ),
    DOCS: ("create", "update", "delete", "batch"),
    umbrella: ("send_for_signature", "upload_document", "create_upload_link", "cancel_draft"),
    # Плагин GitHub сейчас не подключён (init: failed) — имена по официальному MCP-серверу GitHub.
    GITHUB: (
        "create_pull_request",
        "merge_pull_request",
        "update_pull_request",
        "update_pull_request_branch",
        "create_pull_request_review",
        "create_or_update_file",
        "push_files",
        "delete_file",
        "create_branch",
        "create_repository",
        "fork_repository",
        "create_issue",
        "update_issue",
        "add_issue_comment",
        "run_workflow",
        "rerun_workflow_run",
        "cancel_workflow_run",
        "create_release",
    ),
}

# Читающие инструменты тех же серверов (сверены по init.tools 2.1.281) — без предупреждения П16.
KNOWN_READ = frozenset(
    [
        *(
            SLACK + n
            for n in (
                "slack_get_reactions",
                "slack_list_channel_members",
                "slack_list_user_channels",
                "slack_read_canvas",
                "slack_read_channel",
                "slack_read_file",
                "slack_read_list",
                "slack_read_thread",
                "slack_read_user_profile",
                "slack_search_channels",
                "slack_search_emojis",
                "slack_search_public",
                "slack_search_public_and_private",
                "slack_search_users",
            )
        ),
        *(
            GITLAB + n
            for n in (
                "gl_compare",
                "gl_find_project",
                "gl_get_commit",
                "gl_get_file",
                "gl_get_job_trace",
                "gl_get_mr",
                "gl_get_mr_diff",
                "gl_get_pipeline",
                "gl_get_pipeline_jobs",
                "gl_list_branches",
                "gl_list_commits",
                "gl_list_dir",
                "gl_list_issues",
                "gl_list_mr_notes",
                "gl_list_mrs",
                "gl_list_pipelines",
                "gl_list_tags",
                "gl_search_code",
            )
        ),
        *(
            YOUTRACK + n
            for n in (
                "youtrack_describe_project_fields",
                "youtrack_download_attachment",
                "youtrack_get_current_user",
                "youtrack_get_issue",
                "youtrack_get_playbook",
                "youtrack_get_state_machine",
                "youtrack_get_work_item",
                "youtrack_help",
                "youtrack_list_activities",
                "youtrack_list_attachments",
                "youtrack_list_bundle_values",
                "youtrack_list_comments",
                "youtrack_list_issues",
                "youtrack_list_link_types",
                "youtrack_list_projects",
                "youtrack_list_work_item_types",
                "youtrack_list_work_items",
                "youtrack_search_users",
            )
        ),
        *(
            ATLASSIAN + n
            for n in (
                "atlassianUserInfo",
                "discover",
                "executeRead",
                "getAccessibleAtlassianResources",
                "getConfluenceContent",
                "getGraphContext",
                "getGraphObject",
                "getJiraIssue",
                "getLoomVideo",
                "search",
                "searchConfluence",
                "searchJiraIssuesUsingJql",
            )
        ),
        *(
            NOTION + n
            for n in (
                "notion-ai-search",
                "notion-check-mcp-next-steps",
                "notion-download-attachment",
                "notion-download-skill",
                "notion-fetch",
                "notion-get-async-task",
                "notion-get-comments",
                "notion-get-session-status",
                "notion-get-teams",
                "notion-get-tool-access",
                "notion-get-users",
                "notion-list-favorite-pages",
                "notion-list-private-pages",
                "notion-list-recent-pages",
                "notion-list-session-events",
                "notion-list-shared-pages",
                "notion-query-data-sources",
                "notion-query-meeting-notes",
                "notion-query-multiple-data-sources",
                "notion-query-sessions",
                "notion-read-session-event",
                "notion-search",
                "notion-search-agents",
                "notion-search-sessions",
                "notion-search-skills",
                "notion-show-advanced-analysis-next-steps",
                "notion-wait-session",
            )
        ),
        *(DOCS + n for n in ("export", "guide", "query", "read")),
        *(
            umbrella + n
            for n in ("check_inn", "list_documents", "package_status", "scan_contract", "scan_status")
        ),
    ]
)

DEFAULT_ASK = tuple(prefix + name for prefix, names in _WRITE.items() for name in names)
# Живая приёмка 9a (24.09): пишущие инструменты БОЕВОГО хаба у сессии (профиль `hub-all` владельца: перенос беседы,
# снятие черновика) и встроенные действия наружу (облачные запуски, расписание) — тоже под кнопку.
_ASK_EXTRA = (
    "mcp__hub-all__hub_move_channel",
    "mcp__hub-all__hub_cancel_draft",
    "RemoteTrigger",
    "CronCreate",
    "CronDelete",
)
DEFAULT_ASK = DEFAULT_ASK + _ASK_EXTRA
WRITE_SERVERS = tuple(_WRITE)

# D + П3 + П15 + «Мелочи»: окна на экране владельца, CLI хаба (решить свою же карточку), входящие
# межсессионные
# сообщения мимо очереди хаба, смена режима разрешений.
DEFAULT_DENY = (
    "mcp__playwright-show__*",
    "Bash(hub *)",
    "Bash(*/.venv/bin/hub *)",
    "SendMessage",
    "EnterPlanMode",
    "ExitPlanMode",
)

# Ф41-2б (4.2 + П7): правила `permissions` настроек сессии — именованными пунктами, как `guard.RULES`:
# ключ -> (шаблоны, вид); вид — `deny` | `ask`. Шаблоны `mcp_write` — итог `effective_ask` (умолчание ∪
# добавки проекта − «сама» проекта), подставляются при сборке. Каждый пункт — выключатель автопилота
# (Ф41-5). Профиль `task` (задачи по «Го») убран в Ф78-1.
MCP_WRITE = "mcp_write"
PERMISSION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    MCP_WRITE: ((), "ask"),  # пишущие MCP и встроенные действия наружу (`DEFAULT_ASK`)
    "screen_windows": (("mcp__playwright-show__*",), "deny"),  # окна на экране владельца
    "hub_cli": (("Bash(hub *)", "Bash(*/.venv/bin/hub *)"), "deny"),  # CLI хаба
    "cross_session": (("SendMessage",), "deny"),  # сообщения в другие сессии мимо хаба
    "plan_mode": (("EnterPlanMode", "ExitPlanMode"), "deny"),  # смена режима разрешений
}


def session_permissions(ask: list[str], free=()) -> dict:
    """`permissions` настроек сессии: `{"ask": […], "deny": […]}` по таблице `PERMISSION_RULES`.
    Ф41-5 (4.2): правило из `free` (снятый выключатель автопилота) в настройки не попадает."""
    result: dict[str, list[str]] = {"ask": [], "deny": []}
    for key, rule in PERMISSION_RULES.items():
        kind = rule[1]
        if key in free:
            continue
        for pattern in ask if key == MCP_WRITE else rule[0]:
            if pattern not in result[kind]:
                result[kind].append(pattern)
    return result


def effective_ask(policy: dict | None) -> list[str]:
    """П16: умолчание ∪ добавки проекта (`work.ask`) − «сама» проекта (`work.self`), порядок стабильный."""
    policy = policy or {}
    extra = [x for x in policy.get("ask") or [] if isinstance(x, str) and x]
    own = {x for x in policy.get("self") or [] if isinstance(x, str)}
    result: list[str] = []
    for name in (*DEFAULT_ASK, *extra):
        if name not in own and name not in result:
            result.append(name)
    return result


def settings_config(*, ask: list[str], guard_command: str, free=()) -> dict:
    """F3 (+ П15, 9a-a): `permissions.ask` (только MCP-кнопки) и `permissions.deny`, входы мимо хоста закрыты
    (`remoteControlAtStartup: false`, `crossSessionInbound: "refuse"` — ключи сверены по схеме настроек
    2.1.281), ровно один PreToolUse-хук хаба (`hub-work-guard --workdir …`). Больше ничего: автопамять, хуки и
    MCP владельца и проекта действуют как у обычной сессии. Набор — по таблице `PERMISSION_RULES`."""
    return {
        "permissions": session_permissions(list(ask), free),
        "remoteControlAtStartup": False,
        "crossSessionInbound": "refuse",
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": guard_command, "timeout": 30}]}
            ]
        },
    }


def unknown_tools(tools, ask: list[str]) -> list[str]:
    """П16: инструменты серверов с пишущими инструментами, которых нет ни в `ask`, ни среди известных
    читающих."""
    asked = set(ask)
    result = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, str) or tool in asked or tool in KNOWN_READ:
            continue
        if tool.startswith(WRITE_SERVERS):
            result.append(tool)
    return result


# --- Ф16 П4: решение по правилам для рабочих сессий OpenCode ------------------------------------------------
# У Claude правила `permissions.ask/deny` применяет сам CLI. У OpenCode их применяет хост (`host._oc_decide`)
# этой функцией, а имена серверов у движков разные: у Claude `claude_ai_globex-gitlab`,
# `plugin_atlassian_atlassian`, в OpenCode владельца — `globex-gitlab`, `atlassian`. Поэтому сравнение — по
# каноническому имени сервера (без префиксов `claude_ai_` и `plugin_<имя>_`, в нижнем регистре).

_MCP = "mcp__"


def canonical_server(server: str) -> str:
    """`claude_ai_globex-gitlab` -> `globex-gitlab`, `plugin_atlassian_atlassian` -> `atlassian`,
    `claude_ai_Slack` -> `slack`; имя OpenCode — в нижнем регистре. Ф71-1 (П2): сервер плагина Cursor —
    `plugin-<плагин>-<сервер>` (`plugin-s12-hub-all` -> `hub-all`)."""
    name = server
    if name.startswith("claude_ai_"):
        name = name[len("claude_ai_") :]
    elif name.startswith("plugin-"):
        head, sep, tail = name[len("plugin-") :].partition("-")
        if sep and tail:
            name = tail
        elif head:
            name = head
    elif name.startswith("plugin_"):
        rest = name[len("plugin_") :]
        head, sep, tail = rest.partition("_")
        if sep and tail:
            name = tail
        elif head:
            name = head
    return name.lower()


def split_mcp(tool_name: str) -> tuple[str, str] | None:
    """`mcp__<сервер>__<инструмент>` -> (канонический сервер, инструмент); не MCP — `None`."""
    if not isinstance(tool_name, str) or not tool_name.startswith(_MCP):
        return None
    server, sep, tool = tool_name[len(_MCP) :].partition("__")
    if not sep or not server or not tool:
        return None
    return canonical_server(server), tool


def _bash_pattern(rule: str) -> str | None:
    if rule.startswith("Bash(") and rule.endswith(")"):
        return rule[len("Bash(") : -1]
    return None


def _rule_matches(rule: str, tool_name: str, tool_input: dict) -> bool:
    """Одно правило в синтаксисе Claude: `Bash(<шаблон>)` — `fnmatch` по всей команде; `mcp__<сервер>__*` и
    `mcp__<сервер>__<инструмент>` — по каноническому серверу; прочее — имя целиком."""
    pattern = _bash_pattern(rule)
    if pattern is not None:
        command = tool_input.get("command") if tool_name == "Bash" else None
        return isinstance(command, str) and fnmatch.fnmatchcase(command.strip(), pattern)
    rule_mcp = split_mcp(rule)
    call_mcp = split_mcp(tool_name)
    if rule_mcp is not None:
        if call_mcp is None or rule_mcp[0] != call_mcp[0]:
            return False
        return rule_mcp[1] == "*" or rule_mcp[1] == call_mcp[1]
    return rule == tool_name


MOVE_TOOL = "hub_move_channel"


def without_move(ask: list[str]) -> list[str]:
    """Ф41-5 П5: у сессии хаба в авто перенос беседы регулирует один выключатель `move_channel` (ядро), а
    не карточка разрешения инструмента — `hub_move_channel` из шаблонов `mcp_write` убирается."""
    return [p for p in ask if (split_mcp(p) or ("", ""))[1] != MOVE_TOOL]


def rule_key(tool_name: str, tool_input: dict | None = None, free=()) -> str:
    """Ф41-5: ключ правила `PERMISSION_RULES`, под которое попал вызов, — по шаблонам именованных правил;
    не нашлось — `mcp_write` (пишущий MCP: точный шаблон проекта или эвристика серверов). Ревью 41-5:
    вызов спрошен (`decide_tool`), а совпавшее именованное правило снято (`free`) — значит, спросило
    другое: ключ берётся у СТОЯЩЕГО правила (разрешить можно, только когда сняты все совпавшие)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    for key, rule in PERMISSION_RULES.items():
        if key == MCP_WRITE or key in free:
            continue
        if any(_rule_matches(p, tool_name, tool_input) for p in rule[0]):
            return key
    return MCP_WRITE


def decide_tool(tool_name: str, settings: dict, tool_input: dict | None = None, free=()) -> str | None:
    """П4: `"deny"` | `"ask"` | `None` (ничего не сказано — как `bypassPermissions` у Claude) по
    `settings["permissions"]` (как `settings_config`). Порядок: `deny` сильнее `ask`. Для MCP сверх точного
    совпадения по каноническим именам: сервер не совпал, но короткое имя инструмента есть среди `ask` —
    `ask`; незнакомый инструмент (не в `ask` и не среди известных читающих) сервера с пишущими инструментами
    этапа 1 (`_WRITE`) — `ask`."""
    permissions = (settings or {}).get("permissions") or {}
    ask = [r for r in permissions.get("ask") or [] if isinstance(r, str)]
    deny = [r for r in permissions.get("deny") or [] if isinstance(r, str)]
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if any(_rule_matches(rule, tool_name, tool_input) for rule in deny):
        return "deny"
    if any(_rule_matches(rule, tool_name, tool_input) for rule in ask):
        return "ask"
    call = split_mcp(tool_name)
    if call is None or MCP_WRITE in free:
        # Ф41-5 П3г: `mcp_write` снят выключателем автопилота — эвристика серверов не спрашивает.
        return None
    server, tool = call
    ask_mcp = [m for m in (split_mcp(r) for r in ask) if m is not None]
    if any(t == tool for _s, t in ask_mcp):
        return "ask"
    known = {m for m in (split_mcp(r) for r in KNOWN_READ) if m is not None}
    if (server, tool) in known:
        return None
    # Как предупреждение П16 у Claude (`unknown_tools`): только серверы с пишущими инструментами этапа 1
    # (`_WRITE`), а не любой сервер с `ask` — у `hub-all` под кнопкой лишь перенос беседы и снятие черновика,
    # его чтение и черновики карточек не требуют.
    if server in _WRITE_CANONICAL:
        return "ask"
    return None


_WRITE_CANONICAL = frozenset(m[0] for m in (split_mcp(p + "x") for p in WRITE_SERVERS) if m is not None)
