"""Роли сессий-агентов в коде хаба (Ф41-8, docs/architecture.md).

Каталог — файлы `*.md` рядом с модулем (данные пакета): `_common.md` — общие правила взаимодействия, по
файлу на роль (порядок в форме — по имени файла, отсюда цифра в начале) с шапкой-комментарием первой строкой:
`<!-- key: programmer; names: программист, разработчик; title: Программист; sub: direct -->`.
Необязательные поля шапки (Ф48-2, раздел D) — умолчания сжатия для НОВЫХ сессий роли: `compact_tokens`
(порог авто-сжатия, целое 50000–900000) и `compact_auto` (`on` | `off`); нет поля — умолчание хоста.

Ф48-3 (docs/architecture.md): встроенных текстов ролей нет — в файлах ролей осталась только
шапка (каталог для формы, режим подписки, умолчания сжатия). Инструкцию роли задаёт владелец своим MD-файлом в
рабочей папке проекта (поле сессии `role_file`): `file_form` — проверка пути при записи, `read_file` — чтение
с границей «внутри рабочей папки после разрешения ссылок, до 64 КиБ» — ОДНО место и для хоста (системный
текст сессии), и для ручки ядра (окно «Инструкция роли»).
Только stdlib.
"""

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import NamedTuple

COMMON_FILE = "_common.md"
MAX_CHARS = 6000  # предел общего текста и текста роли; длиннее — тест падает, обрезки на проде нет
_HEADER_RE = re.compile(r"\A<!--(.*?)-->[ \t]*\n?", re.DOTALL)
_TRAILING_NO_RE = re.compile(r"[\s\-_#№]*\d+\s*$")


@dataclass(frozen=True)
class Role:
    key: str
    names: tuple[str, ...]
    title: str
    sub: str
    text: str
    # Ф48-2 (D): умолчания сжатия для новых сессий роли; `None` — не заданы (умолчание хоста).
    compact_tokens: int | None = None
    compact_auto: bool | None = None


def _norm(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _read(name: str) -> str:
    return (resources.files(__package__) / name).read_text(encoding="utf-8")


COMPACT_TOKENS_RANGE = (50_000, 900_000)  # те же границы, что у поля сессии (`core.held`)
_SWITCH = {"on": True, "off": False}


def _compact(fields: dict[str, str]) -> tuple[int | None, bool | None]:
    tokens: int | None = None
    raw = fields.get("compact_tokens")
    if raw:
        low, high = COMPACT_TOKENS_RANGE
        if not raw.isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"compact_tokens роли: целое {low}–{high}")
        tokens = int(raw)
    auto: bool | None = None
    raw = fields.get("compact_auto")
    if raw:
        if raw not in _SWITCH:
            raise ValueError("compact_auto роли: on или off")
        auto = _SWITCH[raw]
    return tokens, auto


def _parse(raw: str) -> Role:
    match = _HEADER_RE.match(raw)
    if match is None:
        raise ValueError("у файла роли нет шапки-комментария")
    fields: dict[str, str] = {}
    for part in match.group(1).split(";"):
        key, sep, value = part.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    names = tuple(n.strip() for n in fields.get("names", "").split(",") if n.strip())
    if not fields.get("key") or not names:
        raise ValueError("в шапке роли нужны key и names")
    compact_tokens, compact_auto = _compact(fields)
    return Role(
        key=fields["key"],
        names=names,
        title=fields.get("title") or fields["key"],
        sub=fields.get("sub") or "direct",
        text=raw[match.end() :].strip(),
        compact_tokens=compact_tokens,
        compact_auto=compact_auto,
    )


@lru_cache(maxsize=1)
def _load() -> tuple[str, tuple[Role, ...]]:
    common = _read(COMMON_FILE).strip()
    files = sorted(
        e.name
        for e in resources.files(__package__).iterdir()
        if e.name.endswith(".md") and e.name != COMMON_FILE
    )
    return common, tuple(_parse(_read(name)) for name in files)


def common_text() -> str:
    return _load()[0]


def catalog() -> list[Role]:
    return list(_load()[1])


def find(role: str | None) -> Role | None:
    """Роль каталога по роли подписки: без номера в конце, регистра и разницы «ё»/«е», по `names`."""
    if not isinstance(role, str):
        return None
    wanted = _norm(_TRAILING_NO_RE.sub("", role))
    if not wanted:
        return None
    for item in _load()[1]:
        if wanted in {_norm(n) for n in item.names}:
            return item
    return None


def prompt(role: str | None) -> str:
    """Общий текст плюс текст найденной роли (не найдена, `None` или текст роли пуст — только общий)."""
    found = find(role)
    common = common_text()
    return f"{common}\n\n{found.text}" if found and found.text else common


# --- файл роли из проекта (Ф48-3) ---------------------------------------------------------------------------

FILE_PATH_MAX = 300  # символов пути
FILE_MAX_BYTES = 64 * 1024
FILE_SUFFIX = ".md"
FILE_HEADER = "Инструкция роли (файл проекта {path}):"
# Ф50-7 (П6): сессия в подпапке — путь файла роли считается от папки проекта, а не от папки сессии.
FILE_HEADER_SUBDIR = "Инструкция роли (файл проекта {path}; путь — от папки проекта {project_dir}):"


def file_form(value) -> str | None:
    """Путь файла роли для записи: относительный путь внутри рабочей папки проекта. `None`, пустая строка и
    одни пробелы — `None` (файла нет). Иначе `ValueError` с готовым русским текстом: абсолютный путь, `~`,
    `..`, длиннее `FILE_PATH_MAX`, не `.md`, управляющие символы или обратная косая черта."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("role_file: строка — путь к MD-файлу внутри рабочей папки проекта")
    path = value.strip()
    if not path:
        return None
    if len(path) > FILE_PATH_MAX:
        raise ValueError(f"role_file: путь до {FILE_PATH_MAX} символов")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or "\\" in path:
        raise ValueError("role_file: без управляющих символов и обратной косой черты")
    if path.startswith(("/", "~")):
        raise ValueError("role_file: путь относительно рабочей папки проекта — без начального «/» и «~»")
    parts = path.split("/")
    if ".." in parts:
        raise ValueError("role_file: без «..» — файл лежит внутри рабочей папки проекта")
    if not path.lower().endswith(FILE_SUFFIX) or not parts[-1][: -len(FILE_SUFFIX)]:
        raise ValueError("role_file: нужен MD-файл (расширение .md)")
    return path


class RoleFile(NamedTuple):
    text: str | None  # содержимое дословно; `None` — не прочитан
    reason: str | None  # почему не прочитан (готовый русский текст без содержимого); `None` — прочитан


def read_file(workdir: str | os.PathLike | None, path: str | None) -> RoleFile:
    """Файл роли `path` (относительно `workdir`) целиком и дословно, UTF-8 с заменой. Не читается — причина:
    негодный путь, нет рабочей папки, путь после разрешения символических ссылок вышел за рабочую папку,
    нет файла, не обычный файл, больше `FILE_MAX_BYTES`, пуст, ошибка чтения. Исключений не бросает."""
    try:
        rel = file_form(path)
    except ValueError as exc:
        return RoleFile(None, str(exc))
    if rel is None:
        return RoleFile(None, "файл роли не задан")
    if not workdir:
        return RoleFile(None, "у проекта нет рабочей папки")
    try:
        root = Path(workdir).resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            return RoleFile(None, "путь выходит за рабочую папку проекта")
        if not target.exists():
            return RoleFile(None, "нет файла")
        if not target.is_file():
            return RoleFile(None, "это не обычный файл")
        if target.stat().st_size > FILE_MAX_BYTES:
            return RoleFile(None, f"файл больше {FILE_MAX_BYTES // 1024} КиБ")
        with open(target, "rb") as f:
            raw = f.read(FILE_MAX_BYTES + 1)  # файл мог вырасти между проверкой размера и чтением
    except (OSError, RuntimeError, ValueError) as exc:
        return RoleFile(None, f"не читается ({exc.__class__.__name__})")
    if len(raw) > FILE_MAX_BYTES:
        return RoleFile(None, f"файл больше {FILE_MAX_BYTES // 1024} КиБ")
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return RoleFile(None, "файл пуст")
    return RoleFile(text, None)


def file_block(path: str, text: str, project_dir: str | None = None) -> str:
    """Блок системного текста сессии: заголовок с путём и содержимое файла дословно. Ф50-7 (П6):
    `project_dir` — папка проекта, когда сессия работает в подпапке (путь файла — от неё, а не от папки
    сессии); без неё заголовок прежний байт в байт."""
    header = FILE_HEADER_SUBDIR if project_dir else FILE_HEADER
    return f"{header.format(path=path, project_dir=project_dir)}\n{text}"


# --- подпапка сессии (Ф50-7) --------------------------------------------------------------------------------

SUBDIR_SERVICE = (".git", ".claude")  # сегменты, внутри которых сессия не стартует
# П4: свои настройки движка в папке запуска и выше неё (до корня проекта, корень не в счёт).
SUBDIR_SETTINGS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".mcp.json",
    "opencode.json",
    "opencode.jsonc",
    ".opencode",
)


def subdir_form(value) -> str | None:
    """Подпапка сессии для записи: относительный путь каталога внутри рабочей папки проекта, сегменты через
    «/». `None`, пустая строка, одни пробелы и «.» — `None` (корень проекта); хвостовой «/» срезается. Иначе
    `ValueError` с готовым русским текстом: абсолютный путь, `~`, `..`, пустой сегмент, длиннее
    `FILE_PATH_MAX`, управляющие символы, обратная косая черта, сегмент `.git` или `.claude`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("subdir: строка — путь папки внутри рабочей папки проекта")
    path = value.strip()
    if not path:
        return None
    if len(path) > FILE_PATH_MAX:
        raise ValueError(f"subdir: путь до {FILE_PATH_MAX} символов")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or "\\" in path:
        raise ValueError("subdir: без управляющих символов и обратной косой черты")
    if path.startswith(("/", "~")):
        raise ValueError("subdir: путь относительно рабочей папки проекта — без начального «/» и «~»")
    path = path.rstrip("/")
    parts = [part for part in path.split("/") if part != "."]
    if not parts:
        return None
    if ".." in parts:
        raise ValueError("subdir: без «..» — папка лежит внутри рабочей папки проекта")
    if "" in parts:
        raise ValueError("subdir: без пустых сегментов («//»)")
    for name in SUBDIR_SERVICE:
        if name in parts:
            raise ValueError(f"subdir: не внутри служебного каталога {name}")
    return "/".join(parts)


def subdir_path(workdir: str | os.PathLike | None, subdir: str | None) -> str:
    """Настоящий путь папки процесса сессии: `realpath(<workdir>/<subdir>)` — внутри `realpath(workdir)` и
    каталог. Иначе `ValueError` с готовым русским текстом (негодная форма, нет рабочей папки, папки нет,
    это не папка, ссылка наружу). Подпапка не задана — настоящий путь рабочей папки проекта."""
    rel = subdir_form(subdir)
    if not workdir:
        raise ValueError("у проекта нет рабочей папки")
    root = os.path.realpath(workdir)
    if rel is None:
        return root
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        raise ValueError(f"папка {rel} выходит за рабочую папку проекта")
    if not os.path.exists(target):
        raise ValueError(f"папки {rel} нет в проекте")
    if not os.path.isdir(target):
        raise ValueError(f"{rel} в проекте — не папка")
    return target


def subdir_clean(workdir: str | os.PathLike | None, cwd: str | os.PathLike | None) -> str | None:
    """П4: папка запуска не приносит своих настроек Claude Code. На пути от папки процесса `cwd` вверх до
    корня проекта (корень НЕ включая) ищет `SUBDIR_SETTINGS`; найден — его путь от корня проекта, нет —
    `None`. `cwd` вне проекта или равна корню — `None` (нечего проверять)."""
    if not workdir or not cwd:
        return None
    root = os.path.realpath(workdir)
    current = os.path.realpath(cwd)
    while current != root and current.startswith(root.rstrip("/") + "/"):
        for name in SUBDIR_SETTINGS:
            if os.path.lexists(os.path.join(current, name)):
                return os.path.relpath(os.path.join(current, name), root)
        current = os.path.dirname(current)
    return None


def subdir_check(workdir: str | os.PathLike | None, subdir: str | None) -> str:
    """Р3 + П4 одним вызовом (создание сессии, «Завести», хост перед каждым запуском): настоящий путь папки
    процесса или `ValueError` с готовым русским текстом."""
    target = subdir_path(workdir, subdir)
    found = subdir_clean(workdir, target)
    if found is not None:
        raise ValueError(f"в папке свои настройки движка: {found}")
    return target
