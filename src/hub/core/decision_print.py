"""Отпечаток, класс и дискретное решение карточки — кодом, без модели (Ф76-1,
docs/architecture.md главнее разделов 2–3а).

Здесь — ВСЕ закрытые словари фазы: классы вопросов (`CLASSES`), слова «да» / «нет» коротких ответов владельца
(`YES_WORDS`, `NO_PHRASES`, `MAYBE_WORDS`, `FILLER_WORDS`), флаги-«опасности» команд (`DANGER_FLAGS`).
Словари не расширяются из данных: что не опознано — класса нет, решение не дискретное (строка журнала идёт
только в «похожие», без счёта).

- Отпечаток разрешения (П10) — `permission|<инструмент>|<ключ правила гарда или ->|<первая значимая
  команда без значений>`; ключ правила — `held_request.rule` (у карточек сессий хаба все движки уже приведены
  к виду Claude), для внешней истории — тот же гард (`work.guard.decide`, зовёт `rule_of`).
- Вопрос сессии хаба (`AskUserQuestion`) — `question|<класс или ->|<текст вопроса словами>`; решение —
  вариант.
- Ход сессии (`turn`) и вопрос внешнего окна — `turn|<класс или ->`; решение — «да» / «нет» по словарю, только
  у короткого ответа (до `SHORT_MAX` знаков).
- Прочие виды — вид и цель (`draft|<беседа>|<вид письма>`, `join|<цель>` …), собирает `decisions`.

Модуль без БД и без импортов хаба на верхнем уровне (кроме гарда — внутри `rule_of`)."""

import os
import re
import shlex

YES, NO = "yes", "no"
SHORT_MAX = 40  # П3: короткий ответ владельца
CMD_MAX = 80
ASK_WORDS_MAX = 80

# П3: класс вопроса — закрытый словарь; порядок — приоритет («коммит и push» — push).
CLASSES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("deploy", r"deploy|депло|выкат|выклад|на прод\b|в прод\b|релиз на прод"),
        ("migration", r"migrat|миграц"),
        ("push", r"\bpush|пуш|запуш"),
        ("merge", r"\bmerge|\bмерж|\bсмерж|\bвлить|\bвлей|\bслить в master|\bслей"),
        ("release", r"\brelease|\bрелиз|\bтег\b|\bgit tag"),
        ("commit", r"\bcommit|коммит|закоммит"),
        ("delete", r"\bdelete\b|\bremove\b|\brm -|\bdrop\b|удал|снести|снеси"),
        ("restart", r"restart|перезапус|рестарт|перезагруз"),
        ("install", r"\binstall|установ|поставить пакет|поставь пакет"),
        ("server", r"\bssh\b|на сервер|на стационарник"),
        ("send", r"отправ|разослать|рассыл"),
        (
            "reply",
            r"\breply\b|ответить|ответь|написать (?:ему|ей|клиенту|человеку)|ответ (?:клиенту|человеку)",
        ),
    )
)
CLASS_NAMES = tuple(name for name, _ in CLASSES)

# П3: короткий ответ владельца — «да» / «нет». Отрицание сильнее: «не пушь» — нет.
NO_PHRASES = (
    ("нет",),
    ("не", "надо"),
    ("не", "нужно"),
    ("не", "сейчас"),
    ("не",),
    ("нельзя",),
    ("стоп",),
    ("отмена",),
    ("отмени",),
    ("отбой",),
    ("подожди",),
    ("погоди",),
    ("рано",),
    ("no",),
    ("stop",),
    ("nope",),
    ("dont",),
)
YES_WORDS = frozenset(
    {
        "да",
        "ок",
        "окей",
        "ok",
        "okay",
        "yes",
        "y",
        "угу",
        "ага",
        "го",
        "давай",
        "давайте",
        "делай",
        "делаем",
        "сделай",
        "можно",
        "можешь",
        "разрешаю",
        "конечно",
        "согласен",
        "вперед",
        "пушь",
        "пушим",
        "пуш",
        "запушь",
        "коммить",
        "закоммить",
        "коммитим",
        "выкатывай",
        "деплой",
        "деплоим",
        "влей",
        "вливай",
        "мержи",
        "ставь",
        "удаляй",
        "перезапускай",
        "отвечай",
        "отправляй",
        "продолжай",
        "+",
        "да+",
    }
)
# Слова, которые не мешают «да»: «да, пушь сам», «ок, делай всё».
FILLER_WORDS = frozenset(
    {
        "и",
        "а",
        "все",
        "всё",
        "сам",
        "сама",
        "уже",
        "тоже",
        "пожалуйста",
        "плиз",
        "please",
        "сразу",
        "теперь",
        "в",
        "на",
        "main",
        "master",
        "мастер",
        "прод",
    }
)
# Условие — не дискретно: «да, но сначала тесты».
MAYBE_WORDS = frozenset({"но", "только", "после", "сначала", "если", "когда", "потом", "кроме", "без"})

# Флаги-«опасности» команды (П10: идут в отпечаток отдельно от значений).
DANGER_FLAGS = frozenset(
    {
        "--force",
        "-f",
        "--force-with-lease",
        "--hard",
        "-rf",
        "-fr",
        "--no-verify",
        "--delete",
        "-D",
        "--prune",
    }
)
# Обёртки: команда после них — всё ещё значимая (как `_WRAPPERS` гарда).
WRAPPERS = frozenset(
    {
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
        "flock",
        "xargs",
    }
)
# Неважные команды цепочки: `cd x && git push` — значимая `git push`.
TRIVIAL = frozenset(
    {"cd", "pushd", "popd", "export", "source", ".", "set", "unset", "echo", "printf", "true", ":", "sleep",
     "pwd", "ls", "cat", "test", "[", "[[", "head", "tail", "wc", "grep", "rg", "which", "type", "date"}
)  # fmt: skip
# Команды, у которых подкоманда — часть отпечатка (`git push`, `docker push`, `npm run deploy`).
WITH_SUB = frozenset(
    {"git", "docker", "podman", "kubectl", "helm", "gh", "glab", "npm", "yarn", "pnpm", "uv", "make",
     "systemctl", "cargo", "pip", "poetry", "hub", "trello.sh", "terraform", "ansible-playbook"}
)  # fmt: skip
RUNNERS = frozenset({"npm", "yarn", "pnpm", "uv"})  # `npm run X` — и имя скрипта
SERVER = frozenset({"ssh", "scp", "rsync", "sftp"})
# П10: правило гарда -> слово, по которому из цепочки берётся «его» команда.
RULE_WORD = {
    "push": "push",
    "server": "ssh",
    "deploy": "deploy",
    "gh_write": "gh",
    "kube_write": "kubectl",
    "helm_write": "helm",
    "trello_write": "trello",
    "main_tree_git": "git",
}
_OPS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "\n", "|&", ";;", "{", "}"})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WORD_RE = re.compile(r"[a-zа-я0-9+]+", re.IGNORECASE)


# --- класс и короткий ответ ---------------------------------------------------------------------------------


def classify(text) -> str | None:
    """Класс вопроса по закрытому словарю (`CLASSES`); не опознан — `None`."""
    if not isinstance(text, str) or not text.strip():
        return None
    for name, pattern in CLASSES:
        if pattern.search(text):
            return name
    return None


def _words(text: str) -> list[str]:
    flat = text.lower().replace("ё", "е").replace("’", "").replace("'", "")
    return _WORD_RE.findall(flat)


def _bare_ne_is_no(words: list[str]) -> bool:
    """Одиночное «не» — «нет», только если согласия в ответе нет вовсе («не») или «не» стоит прямо перед
    словом согласия («не пушь», «да, не пушь»). «да не вопрос», «можешь не спрашивать, пушь» — не «нет»."""
    if "не" not in words:
        return False
    if not any(w in YES_WORDS for w in words):
        return True
    return any(w == "не" and i + 1 < len(words) and words[i + 1] in YES_WORDS for i, w in enumerate(words))


def verdict_of_text(text) -> str | None:
    """П3: короткий ответ владельца (до `SHORT_MAX` знаков) -> `yes` / `no`; длиннее, с условием или не из словаря
    — `None` (строка только в «похожие»)."""
    if not isinstance(text, str):
        return None
    flat = " ".join(text.split())
    if not flat or len(flat) > SHORT_MAX:
        return None
    words = _words(flat)
    if not words:
        return YES if flat.strip() in ("+", "👍") else None
    if any(w in MAYBE_WORDS for w in words):
        return None
    for phrase in NO_PHRASES:
        n = len(phrase)
        if phrase == ("не",):
            if _bare_ne_is_no(words):
                return NO
            continue
        if any(tuple(words[i : i + n]) == phrase for i in range(len(words) - n + 1)):
            return NO
    if any(w in YES_WORDS for w in words) and all(
        w in YES_WORDS or w in FILLER_WORDS or classify(w) is not None for w in words
    ):
        return YES
    return None


def ask_words(text) -> str:
    """Текст вопроса словами (нижний регистр, без знаков), не длиннее `ASK_WORDS_MAX` — часть отпечатка
    вопроса с вариантами."""
    if not isinstance(text, str):
        return ""
    return " ".join(_words(text))[:ASK_WORDS_MAX]


# --- первая значимая команда (П10) --------------------------------------------------------------------------


def _segments(command: str) -> list[list[str]]:
    command = command.replace("\n", " ; ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = None
    if tokens is None:
        return [part.split() for part in re.split(r"&&|\|\||[;&|\n]", command) if part.strip()]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPS or set(token) <= set(";&|()"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _strip_wrappers(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        word = words[i]
        if _ASSIGN_RE.match(word):
            i += 1
            continue
        name = os.path.basename(word)
        if name in WRAPPERS:
            i += 1
            # значения обёрток: `timeout 30`, `nice -n 10`, `flock файл`, `env -u X`
            while i < len(words) and (words[i].startswith("-") or words[i][:1].isdigit()):
                if words[i] in ("-n", "-u", "-c", "-s", "-k") and i + 1 < len(words):
                    i += 1
                i += 1
            continue
        if name == "hub-load" and i + 1 < len(words) and words[i + 1] == "run":
            try:
                i = words.index("--", i) + 1
            except ValueError:
                return []
            continue
        break
    return words[i:]


def _host(arg: str) -> str:
    host = arg.split("@", 1)[-1]
    return host.split(":", 1)[0]


def _norm(words: list[str]) -> str:
    """Команда без значений: имя, подкоманда (у `WITH_SUB`), имя скрипта (`npm run X`), хост (`ssh`),
    флаги-«опасности» по порядку."""
    words = _strip_wrappers(words)
    if not words:
        return ""
    name = os.path.basename(words[0])
    parts = [name]
    args = words[1:]
    if name in SERVER:
        hosts = [a for a in args if not a.startswith("-") and (name == "ssh" or ":" in a)]
        if hosts:
            parts.append(_host(hosts[0]))
    elif name in WITH_SUB:
        rest = list(args)
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag in ("-C", "-c", "-f", "--git-dir", "--work-tree", "--directory") and rest:
                rest.pop(0)
        if rest and re.fullmatch(r"[A-Za-z][\w.:-]*", rest[0]):
            parts.append(rest[0])
            if name in RUNNERS and rest[0] == "run" and len(rest) > 1 and re.fullmatch(r"[\w.:-]+", rest[1]):
                parts.append(rest[1])
    flags = [a for a in args if a in DANGER_FLAGS or a.startswith("--force")]
    for flag in flags:
        if flag not in parts:
            parts.append(flag)
    return " ".join(parts)[:CMD_MAX]


def first_command(command, rule: str | None = None) -> str:
    """П10: первая значимая команда цепочки без значений (`cd x && git push origin main --force` ->
    `git push --force`). Сработало правило гарда — берётся первая команда с его словом (`push`, `ssh` …)."""
    if not isinstance(command, str) or not command.strip():
        return ""
    normed = [n for n in (_norm(seg) for seg in _segments(command)) if n and n.strip()]
    if not normed:
        return ""
    word = RULE_WORD.get(rule or "")
    if word is not None:
        hit = next((n for n in normed if word in n.split() or word in n), None)
        if hit is not None:
            return hit
    significant = [n for n in normed if n.split()[:1] and n.split()[0] not in TRIVIAL]
    return (significant or normed)[0]


# --- отпечатки ----------------------------------------------------------------------------------------------


def permission_print(tool, tool_input, rule: str | None) -> tuple[str, str]:
    """(отпечаток разрешения, первая команда). Пути и значения убраны; у не-Bash — только инструмент и
    правило."""
    tool = tool if isinstance(tool, str) and tool else "?"
    cmd = ""
    if tool == "Bash" and isinstance(tool_input, dict):
        cmd = first_command(tool_input.get("command"), rule)
    return f"permission|{tool}|{rule or '-'}|{cmd}", cmd


def question_print(question_text) -> tuple[str, str | None]:
    cls = classify(question_text)
    return f"question|{cls or '-'}|{ask_words(question_text)}", cls


def turn_print(ask_text) -> tuple[str, str | None]:
    cls = classify(ask_text)
    return f"turn|{cls or '-'}", cls


def classless(fingerprint) -> bool:
    """Отпечаток без класса (`turn|-` — ход сессии и вопрос внешнего окна без опознанного класса): под ним
    собраны несвязанные вопросы, образцом он не бывает — только «похожие» (ревью Ф76)."""
    return isinstance(fingerprint, str) and fingerprint.endswith("|-")


def rule_of(tool, tool_input, *, cwd: str | None, workdir: str | None, home: str) -> str | None:
    """П10 (внешняя история): ключ правила гарда хаба для входа без правила — тот же `guard.decide` профиля
    `held`. Пропуск гарда — `None`. Гард для Bash с git может звать `git` (подпроцесс): маршрут зовёт эту
    функцию вне потока-писателя."""
    from ..work import guard

    event = {"tool_name": tool, "tool_input": tool_input if isinstance(tool_input, dict) else {}}
    if isinstance(cwd, str) and os.path.isabs(cwd):
        event["cwd"] = cwd
    try:
        guard.decide(event, workdir=workdir or cwd or home, home=home)
    except guard.Verdict as verdict:
        return verdict.key if verdict.key != guard.FAILURE else None
    except Exception:  # noqa: BLE001 — разбор чужого входа: сбой гарда — правила нет
        return None
    return None
