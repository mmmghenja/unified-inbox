"""Грубый гард рабочей сессии хаба — хук PreToolUse `hub-work-guard` (Ф9-1a, раздел 2; спека
`docs/architecture.md` спеки 9a). Подключается ТОЛЬКО через `--settings` рабочей
сессии (матчер `*`); хуки владельца и проекта работают как есть, этот складывается с ними. В `~/.claude/hooks`
ничего не ставится.

Запуск: `hub-work-guard --workdir <project.workdir> [--log <data_dir>/work/guard.log]
[--free-sock <имя> --free-pid <pid хоста> --free-session <id>]` (Ф41-5, Ф50-3: набор ключей правил, снятых
выключателями автопилота, хук на каждый вызов спрашивает у хоста по unix-сокету в абстрактном пространстве
имён — `hub.work.free_sock`; все три вместе. Файла с набором нет: процесс сессии
до него не дотянется. Нет соединения, собеседник не тот процесс (`SO_PEERCRED`), таймаут, битый ответ —
набор пустой, в причине вердикта — пометка).
Контракт хуков: JSON
на stdin (`tool_name`, `tool_input`, `cwd`); `ask` — JSON `permissionDecision: "ask"` на stdout (карточка
владельцу, ход стоит), `deny` — exit 2 и причина в stderr (модель получает причину и работает дальше), пропуск —
exit 0 без вывода. Любой сбой самого гарда — `ask` (безопасная сторона). Только stdlib: хук зовётся на каждый
инструмент.

Грубость принята (владелец): гард не угадывает намерения, а ловит формы.
1. Кнопки этапа 1 (Bash) — `ask` (единственный источник; в `permissions.ask` Bash-шаблонов нет): слово `push`
   в любом месте команды (`git -C x push`, `env … git push`, `bash -c "…push…"`, `docker push`; `grep push
   file` — тоже, это цена грубости; метод `.push(` в коде без git и docker в команде — нет);
   `ssh`/`scp`/`rsync`/`sftp` в позиции команды (и после `timeout`, `env`,
   `sudo`…); `kubectl` и `helm` на запись (кроме чтения: get/describe/logs…); пишущие подкоманды `trello.sh`;
   `gh pr create|merge`, `gh workflow run`, `gh release create`, `gh api -X|--method`; деплой-скрипты
   (`deploy*`, `make deploy…`, `npm|yarn|pnpm run deploy…`).
2. Рабочая копия владельца (основное рабочее дерево git-репо внутри `workdir`: `--git-dir` == `--git-common-dir`;
   связанные worktree свободны): `ask` на `git checkout|switch|reset --hard|stash|clean|restore`. Ошибка git
   при проверке — `ask`.
3. Правила и настройки — `ask` на правку и запись из Bash (редирект, `tee`, `sed -i`, `cp`/`mv`/`rm`…):
   `CLAUDE.md`, `AGENTS.md`, `.claude/**` (кроме файлов в `.claude/worktrees/<wt>/`, не считая их собственного
   `.claude/`), `.agents/`, `.clinerules/`, `.opencode/`; `deny` — `~/.claude/**` (кроме
   `~/.claude/projects/*/memory/**`).
4. Сам хаб — `deny`: `systemctl stop|restart|kill|disable|mask|edit` юнитов `hub-*`, `hub-deploy.sh`,
   `hub-units-install.sh`, запись в `~/projects/hub-prod`, `pkill|killall|kill` по `claude`/`hub`.
5. Каталоги хаба в домашней папке (конфиг и данные: файлы входа хоста, настройки сессий) — `deny` на чтение и
   запись, инструментами файлов и из Bash (Ф41-2б П7). Ревью 2б: путь через `$HOME`, `${HOME}`, `~user`;
   шаблон (`*`, `?`, `[`, `{…}`, переменная в пути), покрывающий каталог хаба; каталог-ПРЕДОК (`~`,
   родитель каталога хаба, `/home`, `/`) у «широких» команд (`grep`, `rg`, `cp`, `tar`, `find`, `du`,
   `rsync`, `zip`, `ls -R`) и у инструментов `Grep` и `Glob` — тоже `deny`.

Вид каждого правила — ТОЛЬКО таблица `RULES` (сессия хаба работает в основном дереве проекта, как обычная
сессия владельца; профиль `task` задач по «Го» убран в Ф78-1): места проверок в коде называют ключ, а не решают
сами. Каждая строка таблицы — выключатель автопилота (Ф41-5)."""

import fnmatch
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import time

# Ф74-1: профили движков — модуль без зависимостей (только stdlib), хук остаётся лёгким.
from hub.engine import oc_tools  # разбор патча (stdlib)
from hub.engine import registry as engine_registry

EXIT_OK = 0
EXIT_DENY = 2
GIT_TIMEOUT_S = 10
LOG_MAX_BYTES = 1_000_000
_MAX_STDIN = 4_000_000

EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")
SERVER_COMMANDS = ("ssh", "scp", "rsync", "sftp")
# Обёртки: команда после них — всё ещё «в позиции команды».
_WRAPPERS = (
    "env",
    "timeout",
    "sudo",
    "nice",
    "nohup",
    "time",
    "command",
    "exec",
    "stdbuf",
    "ionice",
    "xargs",
    "watch",
    "setsid",
    "doas",
    "flock",
    "unbuffer",
    "builtin",
)
# Обёртки, у которых первый не-флаговый аргумент — не команда (длительность, файл замка).
_WRAPPER_VALUE = {"timeout": 1, "flock": 1}
# Флаги обёрток с отдельным значением (`sudo -u x`, `xargs -I {}`, `timeout -s KILL`…).
_WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-C", "-p", "-U", "-h", "-D", "-r", "-t"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "env": {"-u", "-C", "-S"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k"},
    "watch": {"-n", "-d"},
    "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-a", "-E"},
    "flock": {"-w", "-E", "-c"},
}
# Ключевые слова оболочки перед командой (`if true; then ssh …; fi`, `do scp …`, `! cmd`).
_SHELL_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}"}
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
# `bash -c`, `bash -lc`, `sh -xc`, `bash -ec`… — кластер коротких флагов, где есть `c`.
_SHELL_C_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
# Страховка кнопок (только `ask`): ssh-семейство в позиции команды или после `;&|(`$` — по всему тексту.
_SERVER_RE = re.compile(r"(?:^|[;&|(`$\n])\s*(?:[\w./-]*/)?(ssh|scp|rsync|sftp)(?=\s|$|[;&|)`])")
KUBE_READ = (
    "get",
    "describe",
    "logs",
    "top",
    "explain",
    "version",
    "api-resources",
    "api-versions",
    "cluster-info",
)
HELM_READ = ("get", "list", "ls", "status", "history", "show", "template", "version", "search", "lint", "env")
TRELLO_WRITE = (
    "comment",
    "add-card",
    "move",
    "top",
    "move-list",
    "close-list",
    "member-add",
    "desc",
    "rename",
    "archive",
    "archive-stale",
    "check-create",
    "check-add",
    "check-set",
    "label-add",
)
GIT_MAIN_DENY = ("checkout", "switch", "stash", "clean", "restore")
GIT_STASH_READ = ("list", "show")
GIT_MUTATING = (
    "checkout",
    "switch",
    "reset",
    "merge",
    "pull",
    "commit",
    "rebase",
    "stash",
    "clean",
    "restore",
    "cherry-pick",
    "am",
    "apply",
    "worktree",
    "push",
    "tag",
    "branch",
    "fetch",
    "rm",
    "mv",
    "add",
)
SYSTEMCTL_DENY = ("stop", "restart", "kill", "disable", "mask", "edit", "try-restart", "reload-or-restart")
HUB_SCRIPTS = ("hub-deploy.sh", "hub-units-install.sh")
# Запись из Bash: у этих команд цель — последний аргумент (источник только читается)…
WRITE_LAST_ARG = ("cp", "ln", "install", "rsync")
# …у этих — все аргументы (`mv` уносит и источник).
WRITE_ALL_ARGS = (
    "tee",
    "mv",
    "rm",
    "touch",
    "truncate",
    "chmod",
    "chown",
    "mkdir",
    "rmdir",
    "unlink",
    "shred",
)
RULE_NAMES = ("CLAUDE.md", "AGENTS.md")
# Ф71-1 П7: и `.cursor/`; Ф74-5: и `.codex/` (настройки, хуки и правила Codex проекта).
RULE_DIRS = (".claude", ".agents", ".clinerules", ".opencode", ".cursor", ".codex")
_OPS = {"&&", "||", ";", "|", "&", "(", ")", "\n", "|&", ";;", "{", "}"}
_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_PUSH_RE = re.compile(r"\bpush\b")
# Метод `.push(` в коде (JS `arr.push(x)`, шаблон `grep "sources\.push"`) — не push: считается, только
# когда в той же команде названы git или docker (`repo.remotes.origin.push()` у GitPython — с `import git`).
_PUSH_BARE_RE = re.compile(r"(?<![.\w])push\b")
_PUSH_TOOL_RE = re.compile(r"\b(?:git|docker|podman|gh|glab)\b", re.IGNORECASE)


def _is_push(command: str) -> bool:
    if not _PUSH_RE.search(command):
        return False
    return bool(_PUSH_BARE_RE.search(command) or _PUSH_TOOL_RE.search(command))

# `kill $(pgrep -f claude)`, `kill `pidof hub``: подстановка разрезает команду на сегменты — ловим по тексту.
_KILL_SUBST_RE = re.compile(r"\bkill\b[^;&|\n]*(\$\(|`)\s*(pgrep|pidof)\b[^)`]*(claude|\bhub\b|hub-)")
# Ф41-2б П7: настройки агентов проекта (проектный `opencode.json` сливается в конфиг приватного сервера).
AGENT_CONFIG_NAMES = (".mcp.json", "opencode.json", "opencode.jsonc", "OPENCODE.md")
# Ф71-1: и Cursor (`agent` — его же имя). Ф74-1: исполняемые файлы профилей включённых движков.
NESTED_AGENTS = engine_registry.BINARIES
# Ревью Ф74 (2): arg0-помощники Codex в PATH команд (`~/.codex/tmp/arg0-linux/*/apply_patch`).
PATCH_HELPERS = frozenset({"apply_patch", "applypatch"})
# Ревью 2б: запускалки, после которых агент — не в позиции команды (`npx claude`, `uv run claude`,
# `script -c "claude …"`, `node …/claude-code/cli.js`), и страховка по тексту: слово `claude` / `opencode`
# отдельным словом в любом месте команды (не `.claude/…`, не `opencode.json`). Грубость принята.
AGENT_RUNNERS = ("npx", "bunx", "uvx", "uv", "pnpm", "yarn", "script", "systemd-run", "parallel", "node")
# Ревью Ф41-3: `claude-<цифры>/` — временный каталог самого Claude Code (`/tmp/claude-1000/…/scratchpad`,
# туда сессия пишет черновики и оттуда читает приложенные картинки), а не запуск агента.
# 09.10 (разбор карточек проекта): не запуск и часть пути — ветка `origin/claude/ABC-101`, каталог
# `/tmp/claude-1000` (после слова косая; `claude-<цифры>` последней частью пути).
# Ф74-1 (6.2 В5): слова страховки по тексту — из `NESTED_AGENTS`; у `claude` своя форма (исключения выше),
# общее слово `agent` по тексту не ловится (только в позиции команды) — эти две поправки ведутся здесь руками.
_NESTED_WORD = {"claude": r"claude(?!-\d+/)(?!(?<=/claude)-\d+(?![\w-]))"}
_NESTED_TEXT_SKIP = frozenset({"agent"})
_NESTED_RE = re.compile(
    r"(?<![\w.])(?:"
    + "|".join(
        _NESTED_WORD.get(name, name if re.fullmatch(r"[\w-]+", name) else re.escape(name))
        for name in NESTED_AGENTS
        if name not in _NESTED_TEXT_SKIP
    )
    + r")(?![\w./])"
)
# Ревью 2б: «широкие» команды — читают каталог целиком; слово-предок каталога хаба у них — чтение хаба.
WIDE_COMMANDS = ("grep", "egrep", "fgrep", "zgrep", "rg", "ag", "cp", "tar", "find", "du", "rsync", "zip")
_LS_RECURSIVE_RE = re.compile(r"-[A-Za-z]*R[A-Za-z]*|--recursive")
_HOME_VAR_RE = re.compile(r"\$HOME(?![A-Za-z0-9_])|\$\{HOME\}")
# Переменная или фигурные скобки в пути — как шаблон «что угодно» (значения гард не знает).
_UNKNOWN_PART_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?!$-]|\{[^{}]*\}")
_GLOB_CHARS = frozenset("*?[")
# Формы записи, которые разбирает только профиль с ключом `write_forms` (ревью 2б).
GIT_TREE_WRITERS = ("apply", "checkout-index", "read-tree")
GIT_APPLY_READ = ("--check", "--stat", "--numstat", "--summary")
GIT_PATH_WRITERS = ("mv", "rm")
TARGET_DIR_COMMANDS = ("cp", "mv", "install", "ln")
_PERL_IN_PLACE_RE = re.compile(r"-[A-Za-z]*i.*")
# Каталоги хаба в домашней папке (конфиг и данные) и они же по тексту команды (`$HOME/.config/hub/…`, но не
# `.config/hub-other`).
HUB_DIRS = ((".config", "hub"), (".local", "share", "hub"))
_HUB_DIR_RE = re.compile(r"(?:^|[^\w.-])(?:\.config|\.local/share)/hub(?![\w.-])")

DENY, ASK = "deny", "ask"
# Именованные правила сессии хаба (работает в основном дереве проекта, как обычная сессия владельца): ключ -> вид
# (`deny` | `ask`; `None` — правило не действует). Профиль `task` (задачи по «Го» в своём worktree) убран в
# Ф78-1. Правила `permissions.ask|deny` настроек сессии — такая же таблица в `policy.py`.
RULES: dict[str, str | None] = {
    # рабочая копия владельца
    "main_tree_git": ASK,  # git checkout/switch/restore/stash/clean/reset --hard в основном дереве
    # правила и настройки
    "project_rules": ASK,  # CLAUDE.md, AGENTS.md, .claude/**, .agents/, .clinerules/, .opencode/
    "agent_config": ASK,  # .mcp.json, opencode.json, OPENCODE.md проекта
    # ~/.claude/** (кроме памяти проектов), ~/.cursor/** (Ф71-1), ~/.codex/** и чтение
    # ~/.codex/auth.json (Ф74-5)
    "home_claude": DENY,
    "foreign_memory": ASK,  # ~/.claude/projects/<другой слаг>/memory/**
    # Владелец 08.10.2026: запись вне рабочей папки сессии (кроме временных каталогов и `/dev`) — карточкой.
    "outside_workdir": ASK,
    "nested_agent": ASK,  # claude/opencode в Bash: позиция команды, запускалки, слово в тексте
    # Не правило, а переключатель разбора: формы записи сверх базовых (`sed --in-place`, `perl -pi`,
    # `cp -t`, `pushd`, `git mv|rm`, `git apply|checkout-index|read-tree`, `$HOME` в цели записи). Вид
    # решения — у правила цели (`project_rules`, `home_claude`, `main_tree_git`…).
    "write_forms": ASK,
    # сам хаб
    "hub_dirs": DENY,  # чтение и запись каталогов конфига и данных хаба
    "hub_prod": DENY,  # ~/projects/hub-prod
    "hub_scripts": DENY,  # hub-deploy.sh, hub-units-install.sh
    "hub_units": DENY,  # systemctl stop|restart|… юнитов hub-*
    "kill_agents": DENY,  # kill/pkill по процессам claude и хаба
    "oc_private_server": DENY,  # пароль, окружение и порт приватного сервера OpenCode
    # кнопки этапа 1
    "push": ASK,
    "server": ASK,  # ssh/scp/rsync/sftp
    "kube_write": ASK,
    "helm_write": ASK,
    "gh_write": ASK,
    "trello_write": ASK,
    "deploy": ASK,
    "unparsed_command": ASK,  # команду не разобрать
    # Ф41-5 П1: окружение процессов — там ключ запуска сессии хаба (`HUB_SESSION_KEY`).
    "proc_environ": DENY,
    # Ф41-5 П3: сбой самой проверки (вызов не разобран, git не ответил, аргументы хука) — не правило, а ключ
    # карточки; в `free` гард его не снимает (решает хост), места сбоя зовут `_fail`.
    "guard_failure": ASK,
}
FAILURE = "guard_failure"
# Ф99-7.2 (П11): «Всегда в этой сессии» для записи вне папки проекта — точный каталог записи. Едет в том же
# наборе, что снятые выключатели (сокет хоста, `free` у `decide`): строка `dir:<абсолютный путь>`. Действует
# ровно на файлы, лежащие прямо в этом каталоге (не в подкаталогах), и только на правило `outside_workdir`.
ALWAYS_DIR = "dir:"


def always_dir_entry(value) -> bool:
    """Строка набора — каталог «всегда» (`dir:` и абсолютный путь без мусора)."""
    if not isinstance(value, str) or not value.startswith(ALWAYS_DIR):
        return False
    path = value[len(ALWAYS_DIR) :]
    return os.path.isabs(path) and len(path) <= 4096 and "\x00" not in path and "\n" not in path


class Verdict(Exception):
    """Решение гарда: `kind` — `ask` или `deny`, `reason` — для владельца/модели (без входа инструмента)."""

    def __init__(self, kind: str, reason: str, key: str | None = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.key = key  # Ф41-5: ключ сработавшего правила (`RULES`); у составной команды — первый по порядку


def _ask(reason: str, key: str | None = None) -> Verdict:
    return Verdict("ask", "гард хаба: " + reason, key)


def _deny(reason: str, key: str | None = None) -> Verdict:
    return Verdict("deny", "гард хаба: " + reason, key)


def _fail(reason: str) -> Verdict:
    """Сбой самой проверки — карточка под ключом `guard_failure` (Ф41-5 П3)."""
    return _ask(reason, FAILURE)


class _Rules:
    """Правила сессии: место проверки называет ключ (`hit`), вид берётся из таблицы `RULES`."""

    def __init__(self, free=frozenset()) -> None:
        free = frozenset(free or ())
        # Ф99-7.2: каталоги «всегда» сессии — отдельно от ключей правил (сверяются по настоящему пути).
        dirs = {k for k in free if always_dir_entry(k)}
        free -= dirs
        self.always_dirs = frozenset(os.path.realpath(k[len(ALWAYS_DIR) :]) for k in dirs)
        # Ф41-5 (4.1): снятые выключатели автопилота; ключи — из таблицы.
        if not free <= RULES.keys():
            raise _fail("неверные аргументы хука хаба")
        self.free = free - {FAILURE}
        # Ф50-7: папка процесса сессии внутри проекта (`--home-dir`). `own_slugs` — добавочные слаги своей
        # памяти (папка процесса и верх её git-репозитория); `home_chain` — каталоги от папки процесса вверх
        # до корня проекта (корень не в счёт): `.mcp.json` в них — настройки самой сессии (П4). Ставит
        # `decide` (`_home_place`); без `--home-dir` — пусто, поведение прежнее.
        self.own_slugs: frozenset[str] = frozenset()
        self.home_chain: tuple[str, ...] = ()

    def kind(self, key: str) -> str | None:
        """Вид правила; снятое выключателем (`free`) не действует."""
        return None if key in self.free else RULES[key]

    def hit(self, key: str, reason: str, *, ask_reason: str | None = None, asks: list | None = None) -> None:
        """Правило `key` сработало: `deny` — отказ сразу; `ask` — карточка (в Bash копится в `asks`: отказ по
        другой части команды сильнее); правило не действует (снято) — ничего."""
        kind = self.kind(key)
        if kind == DENY:
            raise _deny(reason, key)
        if kind == ASK:
            text = ask_reason or reason
            if asks is None:
                raise _ask(text, key)
            asks.append((key, text))


# --- пути ---------------------------------------------------------------------------------------------------


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _abs(raw: str, cwd: str, home: str) -> str:
    if raw == "~" or raw.startswith("~/"):
        raw = home + raw[1:]
    return os.path.normpath(raw if os.path.isabs(raw) else os.path.join(cwd, raw))


def _variants(path: str) -> list[str]:
    """Путь как написан и после realpath (симлинк в `.claude` — тоже правка правил)."""
    real = os.path.realpath(path)
    return [path] if real == path else [path, real]


def memory_slug(workdir: str) -> str:
    """Слаг папки проекта в `~/.claude/projects/` (Claude Code: всё, кроме букв и цифр ASCII, — `-`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", workdir)


def _home_place(home_dir, workdir: str) -> tuple[str | None, frozenset[str], tuple[str, ...]]:
    """Ф50-7 (Р6, П7, П8): папка процесса сессии `home_dir` относительно папки проекта `workdir`. Принимается,
    только если по НАСТОЯЩИМ путям обеих сторон лежит строго внутри проекта, — иначе `(None, пусто, пусто)`:
    гард чужой папкой не расширяется. Иначе `(настоящий путь, слаги своей памяти, цепочка каталогов)`.
    Слаги (проба 2 спеки): Claude Code держит память в слаге верхнего каталога git-репозитория папки
    процесса, а без репозитория — в слаге самой папки; свои — оба, и только внутри проекта (слаг проекта
    свой и без этого). Цепочка — от папки процесса вверх до корня проекта, корень не включая."""
    if not isinstance(home_dir, str) or not os.path.isabs(home_dir) or not workdir:
        return None, frozenset(), ()
    root = os.path.realpath(workdir)
    real = os.path.realpath(home_dir)
    if real == root or not _within(real, root):
        return None, frozenset(), ()
    slugs = {memory_slug(real)}
    written = os.path.normpath(home_dir)
    if _within(written, os.path.normpath(workdir)) or _within(written, root):
        slugs.add(memory_slug(written))
    chain: list[str] = []
    current, top = real, None
    while current != root and _within(current, root):
        chain.append(current)
        if top is None and os.path.lexists(os.path.join(current, ".git")):
            top = current
        current = os.path.dirname(current)
    if top is not None:
        slugs.add(memory_slug(top))
    return real, frozenset(slugs), tuple(chain)


def _home_claude_zone(path: str, home: str) -> tuple[str, str | None] | None:
    """`~/.claude/**`: `("rules", None)` — правила и настройки владельца; `("memory", слаг)` — автопамять проекта
    `~/.claude/projects/<слаг>/memory/**` (кроме файлов правил в ней); `None` — путь вне `~/.claude`."""
    home_claude = os.path.join(home, ".claude")
    if not _within(path, home_claude):
        return None
    rel = os.path.relpath(path, home_claude).split("/")
    memory = len(rel) >= 4 and rel[0] == "projects" and rel[2] == "memory"
    if memory and os.path.basename(path) not in RULE_NAMES:
        return "memory", rel[1]
    return "rules", None


def _project_rules_path(path: str) -> bool:
    """П.3: файл правил или настроек проекта или соседних агентов (путь вне `~/.claude`)."""
    parts = path.split("/")
    if os.path.basename(path) in RULE_NAMES:
        return True
    i = 0
    while i < len(parts):
        name = parts[i]
        if name == ".claude":
            # .claude/worktrees/<wt>/… — связанный worktree: свободен, кроме его собственного .claude/.
            if i + 2 < len(parts) and parts[i + 1] == "worktrees" and parts[i + 2]:
                i += 3
                continue
            return True
        if name in RULE_DIRS:
            return True
        i += 1
    return False


def _hub_prod(path: str, home: str) -> bool:
    return _within(path, os.path.join(home, "projects", "hub-prod"))


def _hub_dir(path: str, home: str) -> bool:
    return any(_within(path, os.path.join(home, *parts)) for parts in HUB_DIRS)


def _expand_home(raw: str, home: str) -> str:
    """`$HOME`, `${HOME}` и `~user` (свой пользователь) — в домашнюю папку; прочее как есть."""
    raw = _HOME_VAR_RE.sub(lambda _m: home, raw)
    user = os.path.basename(home.rstrip("/"))
    if user and (raw == "~" + user or raw.startswith("~" + user + "/")):
        raw = home + raw[len(user) + 1 :]
    return raw


def _relation(parts: list[str], target: list[str], deep: bool) -> str | None:
    for i, name in enumerate(target):
        if i >= len(parts):
            return "ancestor"
        part = parts[i]
        if deep and part == "**":
            return "inside"
        if part != name and not (_GLOB_CHARS & set(part) and fnmatch.fnmatchcase(name, part)):
            return None
    return "inside"


def _hub_relation(path: str, home: str, *, deep: bool = False) -> str | None:
    """Как абсолютный путь или шаблон относится к каталогам хаба: `inside` — указывает в каталог хаба (шаблон
    сверяется по частям пути через `fnmatch`; `deep` — часть `**` покрывает любую глубину), `ancestor` —
    каталог выше каталога хаба, `None` — мимо."""
    parts = [x for x in path.split("/") if x]
    found = None
    for hub in HUB_DIRS:
        relation = _relation(parts, [x for x in os.path.join(home, *hub).split("/") if x], deep)
        if relation == "inside":
            return relation
        found = found or relation
    return found


def _check_hub_word(
    raw: str, cwd: str, home: str, rules: _Rules, *, wide: bool, deep: bool = False
) -> None:
    """Ревью 2б: слово команды или путь инструмента против каталогов хаба — с раскрытием `$HOME` / `~user`,
    шаблонами и неизвестными частями пути (переменная, `{…}` — «что угодно»); `wide` — каталог-предок тоже
    запрещён (широкая команда, `Grep`, `Glob`)."""
    pattern = _UNKNOWN_PART_RE.sub("*", _expand_home(raw, home))
    relation = _hub_relation(_abs(pattern, cwd, home), home, deep=deep)
    if relation == "inside":
        rules.hit("hub_dirs", _HUB_DIRS_REASON)
    elif relation == "ancestor" and wide:
        # Слово-предок бывает и шаблоном поиска (`grep "/" файл`) — причина говорит сессии, что делать.
        rules.hit("hub_dirs", _HUB_ABOVE_REASON)


_HUB_DIRS_REASON = "каталоги конфига и данных хаба — не для сессии"
# Ф74-5: настройки агентов владельца в домашней папке — запись под `home_claude`, как `~/.claude`.
HOME_AGENT_DIRS = (".cursor", ".codex")
# Ф74-5: секрет входа Codex — закрыт и на чтение (любой инструмент с путём, любое слово команды).
CODEX_AUTH = (".codex", "auth.json")
_CODEX_AUTH_RE = re.compile(r"\.codex/+auth\.json")
_CODEX_AUTH_REASON = "вход Codex (~/.codex/auth.json) — секрет, не для сессии"


def _check_codex_auth(
    raw: str, cwd: str, home: str, rules: _Rules, *, wide: bool, deep: bool = False
) -> None:
    """Слово команды или путь инструмента против `~/.codex/auth.json` (с раскрытием `$HOME`, шаблонами,
    симлинками); `wide` — каталог-предок тоже (широкая команда, поиск): `grep -r … ~/.codex` прочёл бы
    секрет."""
    pattern = _UNKNOWN_PART_RE.sub("*", _expand_home(raw, home))
    path = _abs(pattern, cwd, home)
    target = [x for x in os.path.join(home, *CODEX_AUTH).split("/") if x]
    relations = {_relation([x for x in v.split("/") if x], target, deep) for v in _variants(path)}
    if "inside" in relations:
        rules.hit("home_claude", _CODEX_AUTH_REASON)
    elif "ancestor" in relations and wide and _within(path, os.path.join(home, CODEX_AUTH[0])):
        rules.hit("home_claude", _CODEX_AUTH_REASON)
_HUB_ABOVE_REASON = (
    "команда с каталогом выше каталогов хаба (домашняя папка, её родители, ~/.config, ~/.local) захватила бы "
    "конфиг и данные хаба — назови папку уже; если это не путь, а шаблон поиска, ищи инструментом Grep"
)
_RULES_REASON = "правила и настройки меняет владелец — предложи правку в итоге"


def _check_hub_dir(path: str, home: str, rules: _Rules) -> None:
    """Ф41-2б П7: каталоги хаба закрыты и на чтение."""
    for variant in _variants(path):
        if _hub_dir(variant, home):
            rules.hit("hub_dirs", _HUB_DIRS_REASON)


def _check_write_target(
    path: str,
    home: str,
    rules: _Rules,
    *,
    workdir: str = "",
    asks: list | None = None,
    stream: bool = False,
    file_tool: bool = False,
) -> None:
    """Цель записи. Порядок (П7): `~/.claude/**` раньше правил проекта — `~/.claude/CLAUDE.md` не уходит в
    карточку. `stream` — цель перенаправления или `tee` (туда пишется поток, а не кладётся файл): только у
    неё дескриптор `/dev/fd/N` считается устройством. `file_tool` — запись инструментом файлов (`EDIT_TOOLS`):
    только ей действует «Всегда в этой сессии» (Ф99-7.2, ревью: не целям Bash)."""
    own_slugs = {memory_slug(workdir), memory_slug(os.path.realpath(workdir))} if workdir else set()
    own_slugs |= rules.own_slugs  # Ф50-7: память папки процесса сессии (подпапка проекта) — тоже своя
    for variant in _variants(path):
        if _hub_dir(variant, home):
            rules.hit("hub_dirs", _HUB_DIRS_REASON)
        zone = _home_claude_zone(variant, home)
        if zone is None and any(_within(variant, os.path.join(home, d)) for d in HOME_AGENT_DIRS):
            # Ф71-1 П7, Ф74-5: настройки Cursor и Codex владельца (`~/.cursor/**`, `~/.codex/**`) — под той же
            # защитой, что `~/.claude`.
            rules.hit("home_claude", _RULES_REASON)
        elif zone is not None:
            if zone[0] == "rules":
                rules.hit("home_claude", _RULES_REASON)
            elif zone[1] not in own_slugs:
                rules.hit("foreign_memory", "память другого проекта — решает владелец", asks=asks)
        elif _project_rules_path(variant):
            rules.hit(
                "project_rules",
                _RULES_REASON,
                ask_reason="правка правил и настроек проекта — решает владелец",
                asks=asks,
            )
        elif os.path.basename(variant) == ".mcp.json" and os.path.dirname(variant) in rules.home_chain:
            # Ф50-7 (П4): `.mcp.json` в папке запуска сессии (и выше неё внутри проекта) — её собственные
            # настройки Claude Code, как `.claude/` там же: правило правил проекта, а не настроек агентов.
            rules.hit(
                "project_rules",
                _RULES_REASON,
                ask_reason="правка правил и настроек проекта — решает владелец",
                asks=asks,
            )
        elif os.path.basename(variant) in AGENT_CONFIG_NAMES:
            rules.hit("agent_config", "настройки агентов проекта — решает владелец", asks=asks)
        if _hub_prod(variant, home):
            rules.hit("hub_prod", "боевой хаб (~/projects/hub-prod) правит только деплой владельца")
    # Последним: запреты выше сильнее. Устройство по написанному пути (`> /dev/null`, `tee /dev/stderr`) —
    # не файл: его realpath ведёт в конвейер или терминал гарда и ничего не значит.
    if workdir and rules.kind("outside_workdir") is not None and not _device_path(path, stream=stream):
        # Ревью Ф41-3: сверяется только настоящий путь — рабочая папка и путь к ней бывают симлинками, а
        # симлинк наружу ловит именно realpath. `~/.claude/**` сюда не попадает — у него свои правила (своя
        # память проекта разрешена).
        real = os.path.realpath(path)
        if (
            _home_claude_zone(real, home) is None
            and not _open_write_root(real, workdir)
            and not _always_dir_ok(real, home, rules, file_tool=file_tool)
        ):
            rules.hit("outside_workdir", _OUTSIDE_REASON, asks=asks)


def _always_dir_ok(real: str, home: str, rules: _Rules, *, file_tool: bool) -> bool:
    """Ф99-7.2 «Всегда в этой сессии»: файл прямо в каталоге из набора, записанный инструментом файлов. Ревью
    Ф99: не цели Bash, не файлы с именем на «.» (настройки оболочки и git в домашнем каталоге), не домашний
    каталог и его предки (ключ на них ядро и так не даёт — страховка на старый набор)."""
    if not file_tool or not rules.always_dirs:
        return False
    folder = os.path.dirname(real)
    if folder not in rules.always_dirs or os.path.basename(real).startswith("."):
        return False
    home_real = os.path.realpath(home) if home else ""
    return not home_real or not (home_real == folder or home_real.startswith(folder.rstrip("/") + "/"))


# Где сессия пишет без карточки `outside_workdir`, кроме своей рабочей папки: временные каталоги.
OPEN_WRITE_ROOTS = ("/tmp", "/var/tmp")  # noqa: S108 — список путей для сверки, файлы здесь не создаются
_OUTSIDE_REASON = "запись вне папки проекта — решает владелец"
# Ревью Ф41-3: устройство — только точный путь. `/dev/fd/3/a.txt` — файл в каталоге, открытом дескриптором
# (`exec 3</чужой/каталог`), а не устройство; прочее под `/dev` и `/proc` идёт на общих основаниях.
_DEVICE_RE = re.compile(r"/dev/(?:null|zero|full|stdin|stdout|stderr|tty)")
_FD_RE = re.compile(r"/(?:dev|proc/self)/fd/\d+")


def _device_path(path: str, *, stream: bool = False) -> bool:
    """Именованное устройство — всегда; дескриптор `/dev/fd/N` — только как цель потока (`> /dev/fd/2`,
    `tee /dev/fd/2`): у `cp`, `touch`, `ln` дескриптор может оказаться каталогом снаружи."""
    return _DEVICE_RE.fullmatch(path) is not None or (stream and _FD_RE.fullmatch(path) is not None)


def _open_write_root(real: str, workdir: str) -> bool:
    """`real` — настоящий путь цели; корни — тоже настоящие."""
    roots = (os.path.realpath(workdir), *(os.path.realpath(root) for root in OPEN_WRITE_ROOTS))
    return any(_within(real, root.rstrip("/") or "/") for root in roots)


# --- git ----------------------------------------------------------------------------------------------------


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — git без shell, аргументы — пути
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
    )


def _existing_dir(path: str) -> str:
    current = path
    while current and not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current or "/"


def _dot_git_above(directory: str) -> bool:
    current = os.path.realpath(directory)
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def main_worktree(directory: str) -> str | None:
    """Верхний каталог репо, если `directory` — в ОСНОВНОМ рабочем дереве; `None` — не в репо или в связанном
    worktree. Ошибка git — `Verdict ask`."""
    directory = _existing_dir(directory)
    try:
        proc = _git(
            ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel"],
            directory,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail(f"git не ответил ({exc.__class__.__name__}) — проверь вручную") from None
    if proc.returncode != 0:
        # «Не репо» — свободно, но только если `.git` нет и выше: сломанный репо git тоже называет «не репо».
        if "not a git repository" in proc.stderr and not _dot_git_above(directory):
            return None
        raise _fail("git не смог проверить рабочее дерево — проверь вручную")
    lines = proc.stdout.splitlines()
    if len(lines) < 3:
        # Внутри .git или голый репо — не рабочее дерево владельца.
        return None
    git_dir, common_dir, toplevel = lines[0], lines[1], lines[2]
    if os.path.realpath(git_dir) != os.path.realpath(common_dir):
        return None
    return toplevel


# --- Bash ---------------------------------------------------------------------------------------------------


# 09.10 (разбор карточек проекта): `cat > файл <<'EOF' … EOF` — тело с кавычками ломало разбор, и запись
# черновика скрипта уходила карточкой «команду не разобрать». Тело heredoc с ограничителем В КАВЫЧКАХ —
# буквальные данные (оболочка в нём ничего не раскрывает), а `cat` и `tee` его не исполняют: для разбора на
# слова оно выбрасывается. Ограничитель без кавычек или другой получатель (`bash <<EOF`) — как раньше.
_HEREDOC_RE = re.compile(r"<<(-?)\s*(?:'([\w.-]+)'|\"([\w.-]+)\")")
_HEREDOC_SPLIT_RE = re.compile(r"&&|\|\||[;|&(]")
_HEREDOC_DATA_COMMANDS = ("cat", "tee")


def _drop_heredoc_bodies(command: str) -> str:
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        found = _HEREDOC_RE.search(line)
        if found is None or len(_HEREDOC_RE.findall(line)) != 1:
            continue
        head = _HEREDOC_SPLIT_RE.split(line[: found.start()])[-1].split()
        if not head or head[0] not in _HEREDOC_DATA_COMMANDS:
            continue
        mark = found.group(2) or found.group(3)
        tabs = bool(found.group(1))
        for j in range(i, len(lines)):
            if (lines[j].lstrip("\t") if tabs else lines[j]) == mark:
                i = j + 1
                break
    return "\n".join(out)


# 10.10 (карточка продуктовика): заметка в файл через `cat >> файл <<EOF` со словом «push» в тексте. Для
# правила push тело heredoc у `cat`/`tee` — данные: с ограничителем в кавычках выбрасывается целиком, без
# кавычек — остаются только строки с подстановкой (`$(…)`, обратные кавычки), их оболочка исполняет.
# Подстановка, не закрытая на своей строке, — тело остаётся как есть.
_HEREDOC_ANY_RE = re.compile(r"<<(-?)\s*(?:'([\w.-]+)'|\"([\w.-]+)\"|([\w.-]+))")


def _push_text(command: str) -> str:
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        found = _HEREDOC_ANY_RE.search(line)
        if found is None or len(_HEREDOC_ANY_RE.findall(line)) != 1:
            continue
        head = _HEREDOC_SPLIT_RE.split(line[: found.start()])[-1].split()
        if not head or head[0] not in _HEREDOC_DATA_COMMANDS:
            continue
        mark = found.group(2) or found.group(3) or found.group(4)
        tabs = bool(found.group(1))
        rest = range(i, len(lines))
        end = next((j for j in rest if (lines[j].lstrip("\t") if tabs else lines[j]) == mark), None)
        if end is None:
            continue
        body = lines[i:end]
        if found.group(4):
            live = [x for x in body if "$(" in x or "`" in x]
            if any(x.count("(") != x.count(")") or x.count("`") % 2 for x in live):
                continue
            out.extend(live)
        i = end + 1
    return "\n".join(out)


def _tokens(command: str) -> list[str] | None:
    lex = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPS or token == "$":  # noqa: S105 — оператор оболочки, не пароль
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [s for s in segments if s]


def _strip_redirects(segment: list[str]) -> tuple[list[str], list[str]]:
    """(слова без перенаправлений, цели записи `>`/`>>`)."""
    words: list[str] = []
    targets: list[str] = []
    i = 0
    while i < len(segment):
        token = segment[i]
        if token in _REDIRECTS:
            if i + 1 < len(segment):
                targets.append(segment[i + 1])
            i += 2
            continue
        if token == ">&":  # noqa: S105 — оператор оболочки, не пароль
            # Ревью Ф41-3: `>& файл` — то же, что `&> файл`; `>&2`, `>&-` — дескриптор.
            if i + 1 < len(segment) and not segment[i + 1].isdigit() and segment[i + 1] != "-":
                targets.append(segment[i + 1])
            i += 2
            continue
        if token in ("<", "<<", "<<<", "<&"):
            i += 2
            continue
        words.append(token)
        i += 1
    # `2>` даёт отдельный токен «2» перед `>` — он уже в словах; это безвредно.
    return words, targets


def _command_words(words: list[str]) -> list[str]:
    """Снять присваивания `X=1` и обёртки (`timeout 5`, `env -i`, `sudo -u x`…): первое слово — команда."""
    i = 0
    while i < len(words):
        word = words[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
            i += 1
            continue
        if word in _SHELL_KEYWORDS:
            i += 1
            continue
        base = os.path.basename(word)
        if base in _WRAPPERS:
            i += 1
            skip_values = _WRAPPER_VALUE.get(base, 0)
            value_flags = _WRAPPER_VALUE_FLAGS.get(base, set())
            while i < len(words):
                w = words[i]
                if w == "--":
                    i += 1
                    break
                if w.startswith("-"):
                    i += 2 if w in value_flags else 1
                    continue
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
                    i += 1
                    continue
                if skip_values:
                    skip_values -= 1
                    i += 1
                    continue
                break
            continue
        return words[i:]
    return []


# Ф52-1 D: больше стольких кандидатов в команду под `hub load run` не бывает у честной команды.
_HUB_LOAD_MAX = 16


_HUB_CLI_NAMES = ("hub", "hub-load")
_HUB_UNDER_LOAD_REASON = "под `hub-load run` запускай hub напрямую, не через обёртку"
_PYTHON_RE = re.compile(r"python[0-9.]*")
# Ключи `uv run` со значением следующим словом (форма `--ключ=значение` следующего слова не берёт); все
# прочие ключи — без значения (`-q`, `--no-sync`, `--frozen`, `--locked`, `--isolated`…).
_UV_RUN_VALUE_FLAGS = frozenset(
    {
        "--project", "--directory", "--with", "-w", "--with-requirements", "--with-editable", "--python",
        "-p", "--env-file", "--package", "--group", "--only-group", "--no-group", "--extra", "--no-extra",
        "--index", "--default-index", "--index-url", "-i", "--extra-index-url", "--find-links", "-f",
        "--config-file",
        "--cache-dir", "--color", "--python-preference", "--upgrade-package", "-P", "--reinstall-package",
        "--refresh-package", "--no-binary-package", "--no-build-package", "--exclude-newer", "--resolution",
        "--prerelease", "--index-strategy", "--keyring-provider", "--link-mode", "--config-setting", "-C",
        "--allow-insecure-host", "--python-platform", "--no-editable-package", "--fork-strategy",
    }
)  # fmt: skip
_UV_RUN_MODULE_FLAGS = ("-m", "--module")
# Ключи `hub load run` (все со значением); argparse принимает и однозначное начало (`--wh`, `--co`).
_HUB_LOAD_RUN_FLAGS = ("--what", "--cores", "--mem-gb", "--minutes", "--who")


def _hub_module(name: str) -> bool:
    return name == "hub" or name.startswith("hub.")


def _python_runs_hub(args: list[str]) -> bool:
    """`python -m hub …`, `-m hub.cli`, слитно `-mhub.cli`, в связке ключей `-Bm hub`. Скрипт или `-c` —
    не модуль (известный предел: `python /путь/bin/hub`)."""
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-") or arg == "-":
            return False
        if arg == "--":
            return False
        if arg.startswith("--"):
            i += 1
            continue
        for at, char in enumerate(arg[1:], start=1):
            if char == "c":
                return False
            if char in "mWX":
                value = arg[at + 1 :] or (args[i + 1] if i + 1 < len(args) else "")
                if char == "m":
                    return _hub_module(value)
                if not arg[at + 1 :]:
                    i += 1
                break
        i += 1
    return False


def _is_hub_cli(cmd_words: list[str], *, runner: bool = True) -> bool:
    """Ф52-1 D: команда — сам CLI хаба: `hub …`, `…/bin/hub …`, `hub-load …`, `python -m hub …`, то же под
    `uv run`. Под обёрткой учёта нагрузки такая команда запрещена: `hub-load` доступен сессии при любом
    положении выключателя `hub_cli`, и обёртка не должна его обходить."""
    if not cmd_words:
        return False
    base = os.path.basename(cmd_words[0])
    if base in _HUB_CLI_NAMES:
        return True
    if _PYTHON_RE.fullmatch(base):
        return _python_runs_hub(cmd_words[1:])
    if runner and base == "uv" and cmd_words[1:2] == ["run"]:
        # Ревью Ф52-1: значение ключа (`--project /…/hub`) — не команда; команда — первое слово после ключей.
        rest = cmd_words[2:]
        module = False
        i = 0
        while i < len(rest) and rest[i].startswith("-") and rest[i] != "-":
            flag = rest[i]
            i += 1
            if flag == "--":
                break
            module = module or flag in _UV_RUN_MODULE_FLAGS
            if flag in _UV_RUN_VALUE_FLAGS:
                i += 1
        if module:
            return _hub_module(rest[i]) if i < len(rest) else False
        return _is_hub_cli(_command_words(rest[i:]), runner=False)
    return False


def _hub_load_run_flag(word: str) -> bool:
    name = word.split("=", 1)[0]
    return len(name) > 2 and any(flag.startswith(name) for flag in _HUB_LOAD_RUN_FLAGS)


def _hub_load_scan(words: list[str]) -> tuple[list[list[str]], bool]:
    """Ф52-1 D: `hub load run … -- <команда>` (и то же через `hub-load run`) — обёртка CLI хаба: записывает
    нагрузку и запускает `<команда>`. Возвращает всё, что может оказаться этой командой, — её проверяют
    обычными правилами, иначе обёртка стала бы способом обойти гард, — и признак «разобрать не удалось».
    Грубо и с запасом (лишняя проверка дешевле пропуска):
    - хвост после КАЖДОГО отдельного `--` за словом `hub-load` / `hub … load` — слово `run` перед ним не
      требуется (ревью: `R=run; hub-load $R … -- ssh host` — `run` подставит оболочка);
    - при буквальном `run` — хвост с первого слова, которое не ключ и не значение ключа (у `run` все ключи
      со значением; `--ключ=значение` следующего слова не берёт) — так команду находит и сам разбор CLI.
    Не разобрать: команда начинается с дефиса (первый `--` и сразу ключ) либо до команды стоит незнакомый
    ключ — что именно запустится, решает разбор CLI, а не гард.
    Смотрит каждое слово `hub` сегмента, поэтому обёртка в обёртке раскрывается здесь же, без рекурсии."""
    starts: set[int] = set()
    odd = False
    for i, word in enumerate(words):
        base = os.path.basename(word)
        if base not in _HUB_CLI_NAMES:
            continue
        try:
            # `hub-load` — второй вход того же CLI: слово `load` у него уже в имени.
            after = i + 1 if base == "hub-load" else words.index("load", i + 1) + 1
        except ValueError:
            continue
        dashes = [j for j in range(after, len(words) - 1) if words[j] == "--"]
        starts.update(j + 1 for j in dashes)
        if dashes and words[dashes[0] + 1].startswith("-"):
            odd = True
        try:
            run = words.index("run", after)
        except ValueError:
            continue
        j = run + 1
        while j < len(words) and words[j] != "--":
            if _hub_load_run_flag(words[j]):
                j += 1 if "=" in words[j] else 2
                continue
            if words[j].startswith("-"):
                odd = True
                break
            starts.add(j)
            break
    return [words[j:] for j in sorted(starts)], odd


def _hub_load_commands(words: list[str]) -> list[list[str]]:
    return _hub_load_scan(words)[0]


def _nonflag(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


_SED_SCRIPT_FLAGS = ("-e", "-f", "--expression", "--file")
# `-ne`, `-Ee` — значение следом; не `-i…`: там хвост — суффикс копии.
_SED_SCRIPT_CLUSTER_RE = re.compile(r"-[A-Za-hj-z0-9]*[ef]")


def _in_place_files(cmd: str, args: list[str]) -> list[str]:
    """Ревью Ф41-3: у `sed -i` и `perl -pi` цель записи — файлы, а не выражение. Значение `-e`/`-f`
    (`--expression`/`--file`) — никогда не цель; без них выражение — первый аргумент без дефиса."""
    files: list[str] = []
    scripted = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in _SED_SCRIPT_FLAGS or (not a.startswith("--") and _SED_SCRIPT_CLUSTER_RE.fullmatch(a)):
            scripted = True
            i += 2
            continue
        if a.startswith(("--expression=", "--file=")):
            scripted = True
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if cmd == "perl" and not scripted:
        return files  # `perl -pi сценарий.pl файл`: грубо — целью считается и файл сценария
    return files if scripted else files[1:]


def _touch_files(args: list[str]) -> list[str]:
    """Ревью Ф41-3: `touch -r ОБРАЗЕЦ файл` — образец только читается."""
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "--reference"):
            i += 2
            continue
        if not a.startswith("-"):
            files.append(a)
        i += 1
    return files


def _git_dir_and_sub(args: list[str], cwd: str, home: str) -> tuple[str, str | None, list[str]]:
    """git [-C dir] [-c k=v] … <подкоманда> … -> (каталог, подкоманда, её аргументы)."""
    directory = cwd
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            directory = _abs(args[i + 1], directory, home)
            i += 2
            continue
        if a in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return directory, a, args[i + 1 :]
    return directory, None, []


class _Ctx:
    def __init__(self, workdir: str, home: str, cwd: str, rules: _Rules | None = None) -> None:
        self.workdir = workdir
        self.home = home
        self.cwd = cwd
        self.rules = rules or _Rules()
        self.asks: list[tuple[str, str]] = []  # (ключ, текст)
        # Ревью Ф41-3: после `cd $ПЕРЕМЕННАЯ` гард не знает, где команда; относительная цель — карточка.
        self.cwd_unknown = False
        # Ревью Ф41-5: собственная проверка хоста — текущей папки сессии он не знает вовсе (она держится
        # между вызовами Bash). Пока команда сама не перешла в известную папку, относительная цель записи
        # — карточка под первым стоящим правилом путей.
        self.blind = False
        # Ф52-1 D: идёт проверка команды под обёрткой учёта нагрузки (и её вложенных оболочек).
        self.under_load = False

    def write_target(self, raw: str, *, stream: bool = False) -> None:
        if self.rules.kind("write_forms") is not None:
            raw = _expand_home(raw, self.home)  # `$HOME/.claude/…` — запрет, а не карточка «правила проекта»
        if self.cwd_unknown and not raw.startswith(("/", "~")):
            self.rules.hit("outside_workdir", _OUTSIDE_REASON, asks=self.asks)
        if self.blind and not raw.startswith(("/", "~")):
            blind_target(self.rules, self.asks)
        # Косая в конце — каталог: `cp x /dev/fd/3/` кладёт файл в каталог за дескриптором.
        _check_write_target(
            _abs(raw, self.cwd, self.home),
            self.home,
            self.rules,
            workdir=self.workdir,
            asks=self.asks,
            stream=stream and not raw.endswith("/"),
        )


def _check_segment(
    words: list[str], targets: list[str], ctx: _Ctx, depth: int, *, unwrap: bool = True
) -> None:
    for target in targets:
        if target in ("/dev/null", "/dev/stdout", "/dev/stderr") or target.isdigit():
            continue
        ctx.write_target(target, stream=True)
    rules = ctx.rules
    asks = ctx.asks
    strict = rules.kind("write_forms") is not None
    cmd_words = _command_words(words)
    cmd = os.path.basename(cmd_words[0]) if cmd_words else ""
    args = cmd_words[1:]
    wide = cmd in WIDE_COMMANDS or (cmd == "ls" and any(_LS_RECURSIVE_RE.fullmatch(a) for a in args))
    # Ф41-2б П7: каталоги хаба — любое слово команды, которое в них указывает (чтение тоже).
    for word in [*words, *targets]:
        if not word.startswith("-") and len(word) <= 4096:
            _check_hub_dir(_abs(word, ctx.cwd, ctx.home), ctx.home, rules)
            _check_hub_word(word, ctx.cwd, ctx.home, rules, wide=wide)
            _check_codex_auth(word, ctx.cwd, ctx.home, rules, wide=wide)
    if not cmd_words:
        return

    # Ф52-1 D: CLI хаба под обёрткой учёта нагрузки — отказ всегда: не правило, выключателями не снимается.
    if ctx.under_load and _is_hub_cli(cmd_words):
        raise _deny(_HUB_UNDER_LOAD_REASON)

    # Ф52-1 D: команда под обёрткой `hub load run … -- <команда>` — теми же правилами, что без обёртки.
    if unwrap:
        inner_commands, odd = _hub_load_scan(words)
        if odd:
            rules.hit(FAILURE, "команда под `hub-load run` начинается с ключа — пусть решит владелец", asks=asks)
        if len(inner_commands) > _HUB_LOAD_MAX:
            # Остальные кандидаты не проверены — карточка под ключом сбоя: выключателем он не снимается.
            rules.hit(FAILURE, "слишком много вложенных `hub load run` — пусть решит владелец", asks=asks)
        was_under = ctx.under_load
        ctx.under_load = True
        try:
            for inner in inner_commands[:_HUB_LOAD_MAX]:
                _check_segment(inner, [], ctx, depth, unwrap=False)
        finally:
            ctx.under_load = was_under

    # вложенная оболочка: bash -c "…" (и -lc, -xc, -ec…), eval "…"
    c_flag = next((i for i, a in enumerate(args) if _SHELL_C_FLAG.fullmatch(a)), None)
    if (cmd in _SHELLS and c_flag is not None) or cmd == "eval":
        if cmd == "eval":
            inner = " ".join(args)
        else:
            rest = [a for a in args[c_flag + 1 :] if not a.startswith("-")]
            inner = rest[0] if rest else ""
        if depth < 4:
            _check_command(inner, ctx, depth + 1)
        return
    if cmd == "cd" or (strict and cmd == "pushd"):
        target = _nonflag(args)
        if strict and target:
            # Ревью Ф41-3: `cd $HOME && echo x > notes.txt`. Осталась переменная или подстановка — где
            # команда дальше, неизвестно.
            where = _expand_home(target[0], ctx.home)
            if _UNKNOWN_PART_RE.search(where) or "`" in where or "$" in where:
                ctx.cwd_unknown = True
            elif where.startswith(("/", "~")):
                ctx.blind = False  # дальше команда стоит в известной папке
            ctx.cwd = _abs(where, ctx.cwd, ctx.home)
            return
        ctx.cwd = _abs(target[0], ctx.cwd, ctx.home) if target else ctx.home
        return

    if cmd == "ps" and _ps_environ(args):
        rules.hit("proc_environ", "окружение процессов (ps e) — не для сессии")

    # п.4: сам хаб
    for word in cmd_words:
        if os.path.basename(word) in HUB_SCRIPTS:
            rules.hit("hub_scripts", "деплой и юниты хаба запускает только владелец")
    if cmd == "systemctl":
        sub = [a for a in args if not a.startswith("-")]
        if (
            sub
            and sub[0] in SYSTEMCTL_DENY
            and any(u.startswith("hub-") or u.startswith("hub@") for u in sub[1:])
        ):
            rules.hit("hub_units", "службы хаба останавливает и перезапускает только владелец")
    if cmd in ("pkill", "killall", "kill") and any(re.search(r"(claude|\bhub\b|hub-)", a) for a in args):
        rules.hit("kill_agents", "процессы claude и хаба гасит только хаб или владелец")
    if cmd in NESTED_AGENTS:
        # Вложенный процесс идёт без гарда и наследует личность сессии хаба.
        rules.hit("nested_agent", f"запуск {cmd} из сессии — решает владелец", asks=asks)
    elif cmd in AGENT_RUNNERS:
        for word in args:
            base = os.path.basename(word)
            if base in NESTED_AGENTS or "claude-code" in word:
                rules.hit("nested_agent", f"запуск агента через {cmd} из сессии — решает владелец", asks=asks)
                break
    if cmd in PATCH_HELPERS:
        # Ревью Ф74 (2): arg0-помощник Codex `apply_patch` лежит в PATH команд — патч командой судится по
        # файлам, как инструмент `apply_patch`; патч со stdin (heredoc) или неясный — карточка.
        text = next((a for a in args if not a.startswith("-")), None)
        files = oc_tools.patch_files(text)
        if not files or oc_tools.patch_suspect(text):
            rules.hit("unparsed_command", f"патч командой {cmd} не разобрать — пусть решит владелец", asks=asks)
        for path in files:
            ctx.write_target(path)
    write_args: list[str] = []
    in_place = cmd in ("sed", "perl") and any(a.startswith("-i") for a in args)
    if strict and not in_place:
        in_place = (cmd == "sed" and any(a.startswith("--in-place") for a in args)) or (
            cmd == "perl" and any(_PERL_IN_PLACE_RE.fullmatch(a) for a in args if not a.startswith("--"))
        )
    if strict and in_place:
        write_args = _in_place_files(cmd, args)
    elif strict and cmd == "touch":
        write_args = _touch_files(args)
    elif strict and cmd == "ln":
        # Ревью Ф41-3: симлинк, созданный в этой же команде, уводит следующую запись наружу — целью считается
        # и имя ссылки, и то, на что она указывает.
        write_args = _nonflag(args)
    elif cmd in WRITE_ALL_ARGS or in_place:
        write_args = _nonflag(args)
    elif cmd in WRITE_LAST_ARG:
        write_args = _nonflag(args)[-1:]
    elif cmd == "dd":
        write_args = [a[3:] for a in args if a.startswith("of=")]
    if strict and cmd in TARGET_DIR_COMMANDS:
        # `cp -t КАТАЛОГ файл…`, `--target-directory[=]КАТАЛОГ`: цель — не последний аргумент.
        for i, a in enumerate(args):
            if a.startswith("--target-directory="):
                write_args.append(a.partition("=")[2])
            elif (a == "--target-directory" or re.fullmatch(r"-[A-Za-z]*t", a)) and i + 1 < len(args):
                write_args.append(args[i + 1])
    for arg in write_args:
        ctx.write_target(arg, stream=cmd == "tee")

    # п.2: рабочая копия владельца; п.4: git в боевом хабе
    if cmd == "git":
        directory, sub, sub_args = _git_dir_and_sub(args, ctx.cwd, ctx.home)
        if sub in GIT_MUTATING and _hub_prod(os.path.realpath(directory), ctx.home):
            rules.hit("hub_prod", "боевой хаб (~/projects/hub-prod) правит только деплой владельца")
        main_deny = sub in GIT_MAIN_DENY or (sub == "reset" and "--hard" in sub_args)
        if sub == "stash" and sub_args and sub_args[0] in GIT_STASH_READ:
            main_deny = False
        if main_deny and _within(os.path.realpath(directory), ctx.workdir):
            if main_worktree(directory) is not None:
                rules.hit(
                    "main_tree_git",
                    f"git {sub} в рабочей копии владельца — переключения и откаты только в своём worktree "
                    "(EnterWorktree или `git worktree add`)",
                    ask_reason=f"git {sub} в основном дереве проекта — решает владелец",
                    asks=asks,
                )
        if strict and sub in GIT_PATH_WRITERS:
            for arg in _nonflag(sub_args):
                _check_write_target(
                    _abs(_expand_home(arg, ctx.home), directory, ctx.home),
                    ctx.home,
                    rules,
                    workdir=ctx.workdir,
                    asks=asks,
                )
        tree_writer = sub in GIT_TREE_WRITERS and not (sub == "apply" and set(sub_args) & set(GIT_APPLY_READ))
        if strict and tree_writer and _within(os.path.realpath(directory), ctx.workdir):
            if main_worktree(directory) is not None:
                # Пишет файлы рабочей копии мимо инструментов правки (в том числе файлы правил).
                rules.hit(
                    "main_tree_git",
                    f"git {sub} в основном дереве проекта — решает владелец",
                    ask_reason=f"git {sub} в основном дереве проекта — решает владелец",
                    asks=asks,
                )

    # п.1: кнопки этапа 1 — ask (запоминаем, deny по остальным сегментам сильнее)
    if cmd in SERVER_COMMANDS:
        rules.hit("server", f"{cmd} — связь с сервером, решает владелец", asks=asks)
    elif cmd == "kubectl":
        sub = _nonflag(args)
        if not sub or sub[0] not in KUBE_READ:
            rules.hit("kube_write", "kubectl на запись — решает владелец", asks=asks)
    elif cmd == "helm":
        sub = _nonflag(args)
        if not sub or sub[0] not in HELM_READ:
            rules.hit("helm_write", "helm на запись — решает владелец", asks=asks)
    elif cmd == "gh":
        sub = _nonflag(args)
        pair = tuple(sub[:2])
        if pair in (("pr", "create"), ("pr", "merge"), ("workflow", "run"), ("release", "create")):
            rules.hit("gh_write", f"gh {' '.join(pair)} — решает владелец", asks=asks)
        elif sub[:1] == ["api"] and any(
            a in ("-X", "--method") or a.startswith(("-X", "--method=")) for a in args
        ):
            rules.hit("gh_write", "gh api с методом — решает владелец", asks=asks)
    if any(os.path.basename(w) == "trello.sh" for w in cmd_words):
        idx = next(i for i, w in enumerate(cmd_words) if os.path.basename(w) == "trello.sh")
        sub = _nonflag(cmd_words[idx + 1 :])
        if sub and sub[0] in TRELLO_WRITE:
            rules.hit("trello_write", f"trello.sh {sub[0]} — запись в Trello, решает владелец", asks=asks)
    deploy_word = any(a.startswith("deploy") for a in _nonflag(args))
    if (
        cmd.startswith("deploy")
        or (cmd == "make" and deploy_word)
        or (cmd in ("npm", "yarn", "pnpm", "npx") and deploy_word)
    ):
        rules.hit("deploy", "деплой — решает владелец", asks=asks)


def _substitutions(command: str) -> list[str]:
    """Тексты подстановок `` `…` `` и `$(…)` вне одинарных кавычек (там оболочка их не выполняет)."""
    found: list[str] = []
    i, n = 0, len(command)
    quote = None
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and quote is None:
            quote = "'"
        elif ch == '"':
            quote = None if quote == '"' else '"'
        elif ch == "`":
            end = command.find("`", i + 1)
            found.append(command[i + 1 :] if end == -1 else command[i + 1 : end])
            if end == -1:
                break
            i = end
        elif ch == "$" and command.startswith("(", i + 1) and not command.startswith("((", i + 1):
            level, j = 1, i + 2
            while j < n and level:
                level += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            found.append(command[i + 2 : j - 1] if level == 0 else command[i + 2 :])
            i = j - 1
        i += 1
    return found


# Ревью Ф41-5: `ps e`, `ps eww`, `ps auxe` (BSD-ключ `e` — окружение после команды), `ps -E`.
_PS_VALUE_FLAG_RE = re.compile(r"-[A-Za-z]*[CGUgopqstuOkN]|--[a-z-]+")
_PS_BSD_ENV_RE = re.compile(r"[A-Za-z]*[eE][A-Za-z]*")


def _ps_environ(args: list[str]) -> bool:
    """`ps` печатает окружение процессов: BSD-слово с буквой `e` (не значение ключа: `-C node`,
    `-u user`, `--sort etime`) или ключ с `E`."""
    previous = ""
    for arg in args:
        if arg.startswith("-"):
            if not arg.startswith("--") and "E" in arg:
                return True
        elif _PS_BSD_ENV_RE.fullmatch(arg) and not _PS_VALUE_FLAG_RE.fullmatch(previous):
            return True
        previous = arg
    return False


# Правила путей вида «карточка»: какое из них сработало бы у относительной цели, хост не знает.
BLIND_KEYS = ("project_rules", "agent_config", "foreign_memory", "outside_workdir")
_BLIND_REASON = "текущая папка сессии хосту неизвестна, цель записи — относительный путь; решает владелец"


def blind_target(rules: _Rules, asks: list | None = None) -> None:
    for key in BLIND_KEYS:
        if rules.kind(key) == ASK:
            rules.hit(key, _BLIND_REASON, asks=asks)
            return


def _check_command(command: str, ctx: _Ctx, depth: int = 0) -> None:
    rules = ctx.rules
    if _KILL_SUBST_RE.search(command):
        rules.hit("kill_agents", "процессы claude и хаба гасит только хаб или владелец")
    if _HUB_DIR_RE.search(command):
        rules.hit("hub_dirs", _HUB_DIRS_REASON)
    if _CODEX_AUTH_RE.search(command):
        rules.hit("home_claude", _CODEX_AUTH_REASON)  # Ф74-5: имя секрета в любом месте текста
    if _is_push(_push_text(command)):
        rules.hit("push", "push — решает владелец", asks=ctx.asks)
    if _NESTED_RE.search(command):
        # Страховка: агент не в позиции команды (`$(which claude)`, `C=claude; $C`, `echo "claude …" | bash`).
        rules.hit("nested_agent", "claude или opencode в команде — решает владелец", asks=ctx.asks)
    # Страховка кнопок по всему тексту (только ask: ложная карточка дешевле ложного запрета).
    if _SERVER_RE.search(command):
        rules.hit("server", "связь с сервером — решает владелец", asks=ctx.asks)
    if depth < 4:
        for inner in _substitutions(command):
            _check_command(inner, ctx, depth + 1)
    tokens = _tokens(_drop_heredoc_bodies(command))
    if tokens is None:
        # Кавычки не сходятся — грубо по тексту.
        if any(name in command for name in HUB_SCRIPTS):
            rules.hit("hub_scripts", "деплой и юниты хаба запускает только владелец")
        rules.hit("unparsed_command", "команду не разобрать — пусть решит владелец", asks=ctx.asks)
        return
    for segment in _segments(tokens):
        words, targets = _strip_redirects(segment)
        _check_segment(words, targets, ctx, depth)


# --- решение ------------------------------------------------------------------------------------------------


_PROC_ENVIRON_RE = re.compile(r"/proc/[^\s'\"]*environ")
# Ревью Ф16: процесс по номеру, `self`, переменная или шаблон — `/proc/$PPID/env*`, `/proc/self/cmdline`,
# `/proc/[0-9]*/…`; обычные файлы `/proc` (`cpuinfo`, `meminfo`) не трогаются.
_PROC_PID_RE = re.compile(r"/proc/+(?:self|thread-self|\d+|[^/\s'\"]*[$`*?\[{][^/\s'\"]*)(?:/|\b|$)")
_PROC_WORDS_RE = re.compile(r"\b(?:environ|cmdline)\b|\benv\*|\benv\?")
_PROC_ENV_WORDS_RE = re.compile(r"\benviron\b|\benv[\w]*[*?\[]|\be[*?\[]")
_LOOPBACK_RE = re.compile(r"(?:127\.\d+\.\d+\.\d+|localhost|0\.0\.0\.0|\[?::1\]?)")
_DYNAMIC_PORT_RE = re.compile(r"(?:127\.\d+\.\d+\.\d+|localhost|0\.0\.0\.0|\]):[$`(]")
OC_PASSWORD_VAR = "OPENCODE_SERVER_PASSWORD"  # noqa: S105 — имя переменной, не пароль


def _strings(value, depth: int = 0, skip=frozenset()):
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in skip:
                yield from _strings(item, depth + 1, skip)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, depth + 1, skip)


def _check_private_server(tool, tool_input: dict, oc_port: int, rules: _Rules | None = None) -> None:
    """Ф16 П9: рабочая сессия OpenCode не трогает свой приватный сервер — пароль (`OPENCODE_SERVER_PASSWORD`),
    окружение и командную строку процессов (`/proc/<pid|self|$VAR|шаблон>/…`: там исходное окружение и порт
    сервера) и его порт (API сервера отвечает на разрешения; порт, собранный подстановкой или арифметикой
    рядом с адресом loopback, — тоже). Грубо, по тексту вызова: защита от случайности, а не от намеренного
    обхода (от него — выключенные агенты без запросов, ревью Ф16)."""
    rules = rules or _Rules()
    key = "oc_private_server"
    port = re.compile(rf"(?<![\w.]){int(oc_port)}(?![\w])")
    for text in _strings(tool_input):
        if OC_PASSWORD_VAR in text or _PROC_ENVIRON_RE.search(text):
            rules.hit(key, "пароль и окружение приватного сервера OpenCode — не для сессии")
        if _PROC_PID_RE.search(text) or ("/proc" in text and _PROC_WORDS_RE.search(text)):
            rules.hit(key, "окружение и командная строка процессов (/proc/<pid>/…) — не для рабочей сессии")
        if tool == "Bash" and (
            port.search(text)
            or _DYNAMIC_PORT_RE.search(text)
            or (_LOOPBACK_RE.search(text) and ("$((" in text or "$[" in text))
        ):
            rules.hit(key, "API своего сервера OpenCode сессии недоступен")


PATH_KEYS = ("file_path", "notebook_path", "path")
# Ревью Ф41-5: содержимое, которое инструмент пишет в файл, — текст, а не обращение к `/proc`.
CONTENT_KEYS = frozenset({"content", "new_string", "old_string", "new_source"})
SEARCH_TOOLS = ("Grep", "Glob")


def _check_proc_environ(tool_input: dict, rules: _Rules) -> None:
    """Ф41-5 П1: окружение процессов (`/proc/<pid>/environ`, и шаблоном `env*`) — в нём ключ запуска сессии
    хаба. Грубо, по тексту вызова любого инструмента — кроме содержимого, которое инструмент пишет в файл
    (`CONTENT_KEYS`: упоминание пути в тексте — не чтение)."""
    if rules.kind("proc_environ") is None:
        return
    for text in _strings(tool_input, skip=CONTENT_KEYS):
        if _PROC_ENVIRON_RE.search(text) or ("/proc" in text and _PROC_ENV_WORDS_RE.search(text)):
            rules.hit("proc_environ", "окружение процессов (/proc/<pid>/environ) — не для сессии")


def decide(
    event,
    *,
    workdir: str,
    home: str,
    oc_port: int | None = None,
    free=frozenset(),
    cwd_unknown: bool = False,
    home_dir: str | None = None,
) -> None:
    """Пропуск (ничего) или `Verdict`. Ф50-7: `home_dir` — папка процесса сессии, когда она не папка
    проекта (подпапка): её память — своя, событие без `cwd` считается от неё; граница проекта, правила
    дерева и git — по-прежнему от `workdir`. Вне `workdir` — не действует (`_home_place`).
    `cwd_unknown` (ревью Ф41-5) — проверяет не хук, а хост: текущей
    папки сессии он не знает, относительная цель записи — карточка (`blind_target`).
    `oc_port` — порт приватного сервера рабочей сессии OpenCode (Ф16 П9:
    сверх правил ниже режутся пароль, `/proc/*/environ` и обращения к этому порту); у хука Claude его нет.
    Ф41-5: `free` — ключи правил (таблица `RULES`), снятых выключателями автопилота: такое правило не
    срабатывает. Ф99-7.2: в `free` же — каталоги «всегда» сессии (`dir:<путь>`, `always_dir_entry`)."""
    if not isinstance(event, dict):
        raise _fail("неразборчивый вызов инструмента")
    rules = _Rules(free)
    home_real, rules.own_slugs, rules.home_chain = _home_place(home_dir, workdir)
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if oc_port is not None:
        _check_private_server(tool, tool_input, oc_port, rules)
    _check_proc_environ(tool_input, rules)
    cwd = (
        event.get("cwd")
        if isinstance(event.get("cwd"), str) and os.path.isabs(event.get("cwd"))
        else (home_real or workdir)  # Ф50-7 (П9): запасная папка события — папка процесса
    )
    workdir = os.path.realpath(workdir)
    # Ф41-2б П7: каталоги хаба — любой инструмент с путём (Read, Grep, Glob, Edit, Write…).
    for key in PATH_KEYS:
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw:
            _check_hub_dir(_abs(raw, cwd, home), home, rules)
            _check_hub_word(raw, cwd, home, rules, wide=tool in SEARCH_TOOLS)
            _check_codex_auth(raw, cwd, home, rules, wide=tool in SEARCH_TOOLS)
    if tool in SEARCH_TOOLS:
        # Ревью 2б: поиск от каталога-предка отдаёт содержимое каталогов хаба; шаблон — тоже путь.
        base = tool_input.get("path")
        base = _abs(_expand_home(base, home), cwd, home) if isinstance(base, str) and base else cwd
        pattern = tool_input.get("pattern" if tool == "Glob" else "glob")
        if isinstance(pattern, str) and pattern and len(pattern) <= 4096:
            absolute = _expand_home(pattern, home).startswith(("/", "~"))
            _check_hub_word(pattern, base, home, rules, wide=absolute, deep=absolute)
            _check_codex_auth(pattern, base, home, rules, wide=absolute, deep=absolute)
    if tool in EDIT_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return
        if cwd_unknown and not _expand_home(raw, home).startswith(("/", "~")):
            blind_target(rules)
        path = _abs(raw, cwd, home)
        _check_write_target(path, home, rules, workdir=workdir, file_tool=True)
        return
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return
        ctx = _Ctx(workdir, home, cwd, rules)
        ctx.blind = cwd_unknown
        _check_command(command, ctx)
        if ctx.asks:
            key, text = ctx.asks[0]
            raise _ask(text, key)


def _log(path, tool, verdict: str, reason: str) -> None:
    if not isinstance(path, str) or not path:
        return
    try:
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        safe_tool = "".join(ch if ch.isalnum() or ch in "_-.:" else "?" for ch in str(tool)[:100])
        safe_reason = reason.replace("\n", " ").replace("\t", " ")[:200]
        line = "\t".join([time.strftime("%Y-%m-%dT%H:%M:%S"), f"tool={safe_tool}", verdict, safe_reason])
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001, S110 — журнал не влияет на решение
        pass


FREE_REPLY_MAX = 16_384
FREE_NOTE = "набор автопилота у хаба не получен — действуют все правила"
FREE_TIMEOUT_S = 2.0  # на всё: соединение, запрос, ответ
_FREE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_UCRED = struct.Struct("3i")  # pid, uid, gid


def read_free_sock(name: str, pid: int, session: int, *, timeout: float = FREE_TIMEOUT_S):
    """Ф50-3 (поправка по ревью): `(набор, причина)` — набор снятых выключателей сессии `session` от хоста
    по абстрактному unix-сокету `name`. Собеседник — ровно процесс хоста: `SO_PEERCRED` называет pid
    `pid` и uid этого процесса. Причина не `None` — набор не получен и он ПУСТОЙ (всё, что было карточкой
    или запретом, ими остаётся): нет соединения, не тот собеседник, таймаут, битый ответ. Не бросает."""
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as sock:
            sock.settimeout(max(deadline - time.monotonic(), 0.001))
            try:
                sock.connect("\0" + name)
            except TimeoutError:
                return frozenset(), "хост не ответил вовремя"
            except OSError:
                return frozenset(), "нет соединения с хостом"
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
            peer_pid, peer_uid, _gid = _UCRED.unpack(raw)
            if peer_pid != pid or peer_uid != os.getuid():
                return frozenset(), "на сокете не хост хаба"
            data = b""
            try:
                sock.sendall(f"{int(session)}\n".encode("ascii"))
                while len(data) <= FREE_REPLY_MAX:
                    sock.settimeout(max(deadline - time.monotonic(), 0.001))
                    chunk = sock.recv(FREE_REPLY_MAX + 1)
                    if not chunk:
                        break
                    data += chunk
            except TimeoutError:
                return frozenset(), "хост не ответил вовремя"
        if len(data) > FREE_REPLY_MAX or not data.endswith(b"\n"):
            return frozenset(), "ответ хоста неразборчив"
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, list) or not all(
            isinstance(k, str) and (k in RULES or always_dir_entry(k)) for k in value
        ):
            return frozenset(), "ответ хоста неразборчив"
        return frozenset(value) - {FAILURE}, None
    except Exception:  # noqa: BLE001 — любая ошибка обмена и разбора: строгая сторона
        return frozenset(), "ответ хоста неразборчив"


def _positive(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > 12 or int(raw) <= 0:
        raise _fail("неверные аргументы хука хаба")
    return int(raw)


def _parse_args(argv: list[str]):
    """`(workdir, log, free, home_dir)`; `free` — `(имя сокета, pid хоста, id сессии)` или `None`."""
    workdir = log = home_dir = None
    sock: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--workdir" and i + 1 < len(argv):
            workdir = argv[i + 1]
            i += 2
        elif argv[i] == "--log" and i + 1 < len(argv):
            log = argv[i + 1]
            i += 2
        elif argv[i] == "--profile" and i + 1 < len(argv) and argv[i + 1] == "held":
            i += 2  # Ф78-1: профиль один; флаг прежних настроек сессии принимается и ничего не меняет
        elif argv[i] in ("--free-sock", "--free-pid", "--free-session") and i + 1 < len(argv):
            # Ф50-3: сокет хоста с набором снятых выключателей автопилота (спрашивается на каждый вызов).
            sock[argv[i]] = argv[i + 1]
            i += 2
        elif argv[i] == "--home-dir" and i + 1 < len(argv):
            # Ф50-7: папка процесса сессии внутри проекта; лежит ли она в `--workdir`, сверяет `decide` по
            # настоящим путям на каждый вызов (вне проекта — не действует). Аргумента нет — как раньше.
            home_dir = argv[i + 1]
            if not os.path.isabs(home_dir):
                raise _fail("неверные аргументы хука хаба")
            i += 2
        else:
            raise _fail("неверные аргументы хука хаба")
    if not workdir or not os.path.isabs(workdir):
        raise _fail("хуку хаба не передана рабочая папка")
    free = None
    if sock:
        if len(sock) != 3 or not _FREE_NAME_RE.fullmatch(sock["--free-sock"]):
            raise _fail("неверные аргументы хука хаба")
        free = (sock["--free-sock"], _positive(sock["--free-pid"]), _positive(sock["--free-session"]))
    return workdir, log, free, home_dir


RULE_MARK = "[hub-rule:"  # то же, что `hub.autopilot.RULE_MARK` (гард — только stdlib и без импортов хаба)


def ask_output(reason: str, key: str | None = None) -> str:
    """Ответ хука «спросить». Ф41-5 (4.1): `key` — метка правила последней строкой причины
    (`[hub-rule:<ключ>]`): единственный канал до хоста у Claude Code; подсказка, не основание решения."""
    if key:
        reason = f"{reason}\n{RULE_MARK}{key}]"
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        },
        ensure_ascii=False,
    )


def run(argv: list[str], stdin, *, home: str | None = None) -> tuple[int, str, str]:
    """(код выхода, stdout, stderr). Не бросает."""
    tool = None
    log = None
    marked = False  # метка правила — после разбора аргументов (сбой разбора — без метки): её читает хост
    note = ""  # Ф50-3: набор автопилота у хоста не получен — пометка в причине вердикта
    try:
        workdir, log, free_sock, home_dir = _parse_args(argv)
        marked = True
        raw = stdin.read(_MAX_STDIN)
        try:
            event = json.loads(raw)
        except ValueError:
            raise _fail("неразборчивый вызов инструмента") from None
        tool = event.get("tool_name") if isinstance(event, dict) else None
        free: frozenset = frozenset()
        if free_sock is not None:
            free, why = read_free_sock(*free_sock)
            if why is not None:
                note = f" ({FREE_NOTE}: {why})"
        decide(
            event,
            workdir=workdir,
            home=home or os.path.expanduser("~"),
            free=free,
            home_dir=home_dir,
        )
    except Verdict as verdict:
        reason = verdict.reason + note
        _log(log, tool, verdict.kind, reason)
        if verdict.kind == "deny":
            return EXIT_DENY, "", reason
        return EXIT_OK, ask_output(reason, verdict.key if marked else None), ""
    except Exception as exc:  # noqa: BLE001 — сбой гарда: безопасная сторона — спросить владельца
        reason = f"гард хаба: сбой проверки ({exc.__class__.__name__}) — пусть решит владелец"
        _log(log, tool, "ask", reason)
        return EXIT_OK, ask_output(reason, FAILURE if marked else None), ""
    return EXIT_OK, "", ""


def main(argv: list[str] | None = None) -> None:
    """Точка входа `hub-work-guard`."""
    try:
        code, out, err = run(list(sys.argv[1:] if argv is None else argv), sys.stdin)
        if out:
            sys.stdout.write(out + "\n")
        if err:
            sys.stderr.write(err + "\n")
    except BaseException:  # noqa: BLE001 — последний рубеж: не пропустить молча
        code = EXIT_DENY
        try:
            sys.stderr.write("гард хаба: сбой гарда — запрет\n")
        except BaseException:  # noqa: BLE001, S110
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
