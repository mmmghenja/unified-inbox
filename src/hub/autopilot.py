"""Каталог выключателей автопилота (Ф41-5, docs/architecture.md). Без зависимостей от ядра и хоста: его читают и ядро
(`core/autopilot.py` — значения и ручки), и гард с
хостом (ключи правил), и интерфейс (через `GET /autopilot`).

Выключатель СТОИТ — действие идёт как в ручном режиме (`manual`: `"ask"` — карточка, `"deny"` — запрет);
СНЯТ — у сессии хаба в авто-режиме проходит само. Умолчание — стоит; неизвестный ключ считается стоящим.
`meta` (П10) — правило, сняв которое сессия может снять и остальные ограничения."""

from typing import NamedTuple

G_FILES = "Файлы и git проекта"
G_OUT = "Наружу: push, серверы, выкладка"
G_MCP = "Пишущие инструменты (MCP)"
G_HUB = "Хаб и его службы"
G_PEOPLE = "Сообщения людям"
G_AGENTS = "Агенты и сессии"
G_ASK = "Вопросы и заявки"
G_HUMAN = "Нужен человек"  # Ф54-1 (П6)
GROUPS = (G_FILES, G_OUT, G_MCP, G_HUB, G_PEOPLE, G_AGENTS, G_ASK)  # Ф66-1: группа «Нужен человек» пуста
# Ф54-1 (П6): область ключа. `session` — правило сессии хаба в авто (входит в её `auto_free`); `project` —
# настройка проекта, которую читает само ядро (`core.autopilot.switch_free`): действует на заявки любых сессий
# проекта, и ручных, в `auto_free` и в счёт `auto_held` сессий не входит.
SCOPE_SESSION = "session"
SCOPE_PROJECT = "project"


class Switch(NamedTuple):
    key: str
    group: str
    title: str
    manual: str  # "ask" — карточка, "deny" — запрет
    meta: bool = False
    note: str | None = None
    scope: str = SCOPE_SESSION


def _s(key, group, title, manual, *, meta=False, note=None, scope=SCOPE_SESSION) -> Switch:
    return Switch(key, group, title, manual, meta, note, scope)


SWITCHES: tuple[Switch, ...] = (
    # --- файлы и git проекта (гард) ---
    _s(
        "main_tree_git",
        G_FILES,
        "git checkout, switch, restore, stash, clean, reset --hard в папке проекта",
        "ask",
    ),
    _s(
        "project_rules",
        G_FILES,
        "правка правил проекта: CLAUDE.md, AGENTS.md, .claude, .agents, .opencode",
        "ask",
        meta=True,
    ),
    _s(
        "agent_config",
        G_FILES,
        "правка настроек агентов проекта: .mcp.json, opencode.json, OPENCODE.md",
        "ask",
        meta=True,
    ),
    _s("home_claude", G_FILES, "правка ~/.claude (кроме памяти проектов)", "deny", meta=True),
    _s("foreign_memory", G_FILES, "запись в память другого проекта", "ask"),
    _s("outside_workdir", G_FILES, "запись вне папки проекта (кроме /tmp)", "ask"),
    _s("unparsed_command", G_FILES, "команда Bash, которую не разобрать", "ask"),
    # --- наружу (гард) ---
    _s("push", G_OUT, "push (git push, docker push — слово push в команде)", "ask"),
    _s("server", G_OUT, "связь с сервером: ssh, scp, rsync, sftp", "ask"),
    _s("kube_write", G_OUT, "kubectl на запись", "ask"),
    _s("helm_write", G_OUT, "helm на запись", "ask"),
    _s("gh_write", G_OUT, "gh: создать или слить PR, запустить workflow, релиз, api с методом", "ask"),
    _s("trello_write", G_OUT, "пишущие команды trello.sh", "ask"),
    _s("deploy", G_OUT, "деплой-скрипты, make deploy, npm run deploy", "ask"),
    # --- пишущие инструменты (настройки процесса) ---
    _s(
        "mcp_write",
        G_MCP,
        "пишущие инструменты MCP: Slack, GitLab, YouTrack, Jira, Notion, GitHub и другие",
        "ask",
    ),
    _s("screen_windows", G_MCP, "окна на твоём экране (playwright-show)", "deny"),
    # --- хаб и его службы ---
    _s("hub_dirs", G_HUB, "чтение и запись каталогов конфига и данных хаба", "deny", meta=True),
    _s("hub_cli", G_HUB, "команды hub в терминале", "deny", meta=True),
    _s("hub_prod", G_HUB, "запись в боевой хаб (~/projects/hub-prod)", "deny", meta=True),
    _s("hub_scripts", G_HUB, "деплой и установка юнитов хаба", "deny", meta=True),
    _s("hub_units", G_HUB, "остановка и перезапуск служб хаба (systemctl)", "deny", meta=True),
    _s("kill_agents", G_HUB, "kill процессов claude и хаба", "deny", meta=True),
    _s("oc_private_server", G_HUB, "пароль, окружение и порт своего сервера OpenCode", "deny", meta=True),
    _s("proc_environ", G_HUB, "чтение окружения процессов (/proc/<pid>/environ)", "deny", meta=True),
    _s("guard_failure", G_HUB, "сбой проверки гарда (вызов не разобран, git не ответил)", "ask"),
    # --- сообщения людям (5.4, 5.5) ---
    _s("hub_send", G_PEOPLE, "ответ в беседу своего проекта без черновика", "ask"),
    _s("hub_send_new", G_PEOPLE, "первое сообщение новому адресату без черновика", "ask"),
    # Ф44-1c (4.3): нажатие кнопки под сообщением Telegram-бота — действие от имени владельца, как отправка.
    _s("hub_press", G_PEOPLE, "нажатие кнопок ботов", "ask", note="бот выполнит действие от твоего имени"),
    # Ф63-2 (1.2, П10): только беседы с Telegram-ботом своего проекта; оплата, коды, удаление, личные
    # данные и файлы — всё равно карточкой (страховка `core/bot_guard.py`).
    _s(
        "bot_chat",
        G_PEOPLE,
        "переписка с ботами без черновика: начать беседу, писать, нажимать кнопки",
        "ask",
        note="бот может пересылать твои сообщения людям (поддержка, обратная связь)",
    ),
    # Ф62-6 (B): ключи `tg_join`/`tg_leave` хранятся у проектов — не переименовываются; действуют и
    # на WhatsApp.
    _s(
        "tg_join",
        G_PEOPLE,
        "вступить в группу или канал (Telegram, WhatsApp)",
        "ask",
        note="от твоего личного аккаунта",
    ),
    _s(
        "tg_leave",
        G_PEOPLE,
        "выйти из группы или канала (Telegram, WhatsApp)",
        "ask",
        note="необратимо для закрытых групп",
    ),
    # Ф62-6 (B): создать группу, добавить или убрать участников, сбросить ссылку — от номера владельца.
    _s(
        "wa_group",
        G_PEOPLE,
        "создать группу WhatsApp, добавить или убрать участников, сбросить ссылку",
        "ask",
        note="люди увидят, что их добавили; от твоего номера",
    ),
    # --- агенты и сессии ---
    _s("nested_agent", G_AGENTS, "запуск claude или opencode из команды", "ask", meta=True),
    _s("oc_subagents", G_AGENTS, "подагенты OpenCode, кроме general и explore", "deny", meta=True),
    _s("cross_session", G_AGENTS, "сообщения в другие сессии мимо хаба (SendMessage)", "deny"),
    _s("plan_mode", G_AGENTS, "смена режима разрешений (режим плана)", "deny"),
    _s("new_session", G_AGENTS, "завести напарника — новую сессию проекта", "ask"),
    # --- вопросы и заявки ---
    _s("question", G_ASK, "вопрос сессии тебе (в авто решает сама)", "ask"),
    _s(
        "project_permissions",
        G_ASK,
        "запросы разрешений из настроек самого проекта",
        "ask",
        note="встроенные проверки безопасности движка — всегда карточкой",
    ),
    _s(
        "foreign_hook",
        G_ASK,
        "вопросы твоего общего гарда (~/.claude/hooks): секреты, персональные данные",
        "ask",
    ),
    _s("move_channel", G_ASK, "перенос беседы в другой проект", "ask"),
    _s(
        "map_rule",
        G_ASK,
        "завести правило раскладки",
        "ask",
        note="стойкое: правило остаётся после выключения авто",
    ),
    # Ф66-1: ключ `human_agent` (Ф54-1, «нужен человек → агенту») убран — кому заявки, решает таблица маршрута
    # карточек проекта (`core/card_route.py`, группа `human`); старые значения остаются мёртвыми в хранилище
    # и читаются один раз переносом (`card_route.migrate`).
)

KEYS: tuple[str, ...] = tuple(s.key for s in SWITCHES)
# Ф54-1 (П6): ключи правил сессии — только они входят в `auto_free` и в счёт `auto_held`.
SESSION_KEYS: tuple[str, ...] = tuple(s.key for s in SWITCHES if s.scope == SCOPE_SESSION)
BY_KEY: dict[str, Switch] = {s.key: s for s in SWITCHES}
META: frozenset[str] = frozenset(s.key for s in SWITCHES if s.meta)
RULE_MARK = "[hub-rule:"


def title(key: str | None) -> str | None:
    switch = BY_KEY.get(key) if isinstance(key, str) else None
    return switch.title if switch is not None else None


def public() -> list[dict]:
    """Каталог для `GET /autopilot` — в порядке групп."""
    order = {group: i for i, group in enumerate(GROUPS)}
    rows = sorted(SWITCHES, key=lambda s: order[s.group])
    return [s._asdict() for s in rows]


def effective(key: str, global_hold: dict | None, project_hold: dict | None) -> bool:
    """Действующее значение для проекта: `project.get(key, global.get(key, True))`; `True` — стоит. Не `bool`
    в хранилище — как отсутствие (стоит)."""
    for source in (project_hold, global_hold):
        value = source.get(key) if isinstance(source, dict) else None
        if isinstance(value, bool):
            return value
    return True


def free_keys(global_hold: dict | None, project_hold: dict | None) -> list[str]:
    """Отсортированные ключи правил сессии, снятые для проекта (ключи области `project` сюда не входят:
    это не правила сессии — Ф54-1, П6)."""
    return sorted(key for key in SESSION_KEYS if not effective(key, global_hold, project_hold))


def mark(reason: str, key: str | None) -> str:
    """Причина карточки гарда с меткой правила последней строкой (канал до хоста у Claude Code)."""
    return f"{reason}\n{RULE_MARK}{key}]" if key else reason


def split_mark(reason) -> tuple[str | None, str | None]:
    """`(причина без метки, ключ)`; метки нет или ключ не из каталога — `(причина, None)`. Метка — подсказка
    для заголовка карточки, не основание решения (П2)."""
    if not isinstance(reason, str):
        return None, None
    head, sep, tail = reason.rpartition("\n" + RULE_MARK)
    if not sep or not tail.endswith("]") or tail[:-1] not in BY_KEY:
        return reason, None
    return head, tail[:-1]
