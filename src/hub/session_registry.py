"""Кто сессия Claude Code — по реестру `<claude_dir>/sessions/<pid>.json` (Ф13-1, C). Общий для MCP-сервера
(`hub_subscribe`) и CLI (`hub sub wait`).

- `find_session(start_pid)` идёт вверх по ppid (`/proc/<pid>/stat`, не больше 8 шагов) до первого процесса, у
  которого есть файл реестра с тем же `pid`; из файла — `sessionId`, `cwd`, `procStart`, `name`, `nameSource`,
  `kind`. `procStart` (целое) должен совпасть с `starttime` процесса (поле 22 `/proc/<pid>/stat`), иначе файл
  остался от прежнего процесса с тем же pid — сессии нет.
- Claude Code переписывает файл при каждой смене статуса: битый JSON или пропавший файл — повтор (3 раза по
  200 мс), ошибкой считается только устойчивая.
- Читается ТОЛЬКО `sessions/<pid>.json`: ключи `*.key`, токены и сокеты Claude Code не открываются.
- `claude_dir` = `$CLAUDE_CONFIG_DIR` или `~/.claude`; каталог `/proc`, окружение и пауза подменяются в
  тестах.

Ф16-4 (docs/architecture.md): у OpenCode реестра «pid -> сессия» нет, а один процесс
(`opencode serve` на 4096) держит МНОГО сессий. Сессию называет плагин хаба (`deploy/opencode/hub.ts`):
подставляет `oc_session` (`ses_…`) и `oc_dir` (каталог сессии) в аргументы инструмента или в команду ожидания.
`find_opencode_session` проверяет, что процесс OpenCode (`OPENCODE_PID` окружения) — среди предков, и берёт
его `starttime`; личность сессии — `(pid, proc_start, session_id)`.

Ф41-2а (docs/architecture.md + П1): сессию хаба (запись ядра `hub_session`) запускает
хост постоянных сессий и ставит её процессу `HUB_SESSION=<id>`. Её личность — постоянная условная тройка
`(-id, 0, "hub:<id>")`: ни обхода предков, ни чтения `~/.claude/sessions`, ни `oc_session`/`oc_dir` плагина
(`HUB_SESSION` главнее `OPENCODE_PID`). `HUB_SESSION` вместе с `HUB_WORK` — отказ, не тихий выбор. Подмена:
`HUB_SESSION` — обычная переменная окружения; это тот же уровень доверия, что у тройки настоящего процесса в
теле запроса (осознанно не усиливается).

Ф74-2 (docs/architecture.md, план 6.2 Б1): внешние окна Codex и Cursor. Движок — по
предку процесса (`ext_ancestor`: имена исполняемого файла из профиля движка), разговор и папку называет плагин
хаба (`ext_session`/`ext_dir` в аргументах MCP-вызова или в команде ожидания). Личность — `(pid, proc_start)`
процесса движка и `session_id` вида `<движок>:<id разговора>`. Обход предков у Claude Code останавливается на
ближайшем процессе Codex/Cursor (`find_session`): ближайший движок решает, чья это сессия.
"""

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import hostid
from .engine import registry as engine_registry

MAX_STEPS = 8
RETRIES = 3
RETRY_DELAY_S = 0.2
SESSION_ID_MAX = 128
CWD_MAX = 4096
NAME_MAX = 64
NAME_SOURCE_MAX = 32
KIND_MAX = 32
# Рабочая сессия 9a (`work/session.py`: `HUB_WORK=1`) не подписывается — отказ без запроса к ядру.
WORK_ENV = "HUB_WORK"
# Ф16-4 (F13): OpenCode ставит себе `OPENCODE_PID`, MCP-серверы и дети плагина его наследуют.
OPENCODE_ENV = "OPENCODE_PID"
OC_KIND = "opencode"
# Ф41-2а (3.5): сессия хаба — `HUB_SESSION=<id>` в окружении её процесса (ставит хост постоянных сессий).
HUB_SESSION_ENV = "HUB_SESSION"
# Ф41-4 П10: имя профиля MCP, чей сервер оформляет подписку сессии хаба при старте (ставит хост; переменной
# нет — оформляет любой сервер хаба этой сессии).
HUB_SESSION_SUB_PROFILE_ENV = "HUB_SESSION_SUB_PROFILE"
HUB_KIND = "hub"
HUB_SID_PREFIX = "hub:"
HUB_ID_MAX = 2**31 - 1
HUB_WAIT_TEXT = "сессии хаба сигналы подаёт хост"
OC_SESSION_RE = re.compile(r"^ses_[A-Za-z0-9]{8,64}$")
NO_PLUGIN_TEXT = "нужен плагин хаба для OpenCode — scripts/opencode-hub-install.sh"
# Ф74-2: id разговора внешнего Codex/Cursor (UUID нити Codex, `conversation_id` Cursor).
EXT_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,99}$")

GONE = "gone"  # сессии-предка нет (процесс умер, файл устарел)
UNREADABLE = "unreadable"  # файл есть, но устойчиво не читается


class RegistryError(Exception):
    """`kind` — `GONE` (сессии нет) или `UNREADABLE` (реестр устойчиво не читается)."""

    def __init__(self, kind: str, message: str, *, ext_absent: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        # Ф74-2: предка Codex/Cursor нет — это не внешнее окно (у вызывающего свой текст отказа).
        self.ext_absent = ext_absent


@dataclass(frozen=True)
class SessionInfo:
    pid: int
    proc_start: int
    session_id: str
    cwd: str
    name: str | None
    name_source: str | None
    kind: str
    # Ф33-1: машина сессии (`hostid.host_id`) — ядро на другой машине судит о живости по ней, а не по своему
    # `/proc`. `None` — поля в теле нет (сессия хаба, подделки тестов): ядро считает сессию своей.
    host: str | None = None
    # Ф33-1 (boot): загрузка машины сессии (`hostid.boot_id`) — первый контакт машины с новой загрузкой
    # снимает у ядра подписки прежних загрузок этой машины. `None` — поля в теле нет.
    boot: str | None = None

    def session(self) -> dict:
        """Контракт B5 `Session` — для `wait`/`end` (и всех ручек с телом `session`). Ф33-1: плюс `host`."""
        result = {"pid": self.pid, "proc_start": self.proc_start, "session_id": self.session_id}
        if self.host is not None:
            result["host"] = self.host
        if self.boot is not None:
            result["boot"] = self.boot
        return result

    def session_in(self) -> dict:
        """Контракт B5 `SessionIn` — для `POST /subs`. Имя и источник — как в реестре (имя не длиннее 64);
        имя только от `/rename` (`nameSource == 'user'`) оставляет ядро."""
        return {
            **self.session(),
            "cwd": self.cwd,
            "name": self.name,
            "name_source": self.name_source,
            "kind": self.kind,
        }


def in_work_session(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(WORK_ENV))


def in_opencode(environ: Mapping[str, str] | None = None) -> bool:
    """Процесс запущен OpenCode (MCP-сервер сессии, ожидатель плагина): в окружении есть `OPENCODE_PID`."""
    env = os.environ if environ is None else environ
    return bool(env.get(OPENCODE_ENV))


def hub_session_id(environ: Mapping[str, str] | None = None) -> int | None:
    """Ф41-2а (3.5): id сессии хаба из `HUB_SESSION` или `None` (переменной нет или она пустая). Не целое
    > 0 и `HUB_SESSION` вместе с `HUB_WORK` — `RegistryError(GONE, …)`: ошибка запуска, не тихий выбор."""
    env = os.environ if environ is None else environ
    raw = (env.get(HUB_SESSION_ENV) or "").strip()
    if not raw:
        return None
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= HUB_ID_MAX:
        raise RegistryError(GONE, f"{HUB_SESSION_ENV}: нужен номер сессии хаба (целое больше нуля)")
    if env.get(WORK_ENV):
        raise RegistryError(
            GONE, f"{HUB_SESSION_ENV} и {WORK_ENV} заданы вместе — сессия хаба не бывает рабочей задачей"
        )
    return int(raw)


def find_hub_session(
    environ: Mapping[str, str] | None = None, *, getcwd: Callable[[], str] = os.getcwd
) -> SessionInfo | None:
    """Ф41-2а (3.5 + П1): личность сессии хаба — условная тройка `(-id, 0, "hub:<id>")`, `cwd` — текущая папка
    процесса, имени нет. Не сессия хаба (`HUB_SESSION` не задан) — `None`."""
    hub_id = hub_session_id(environ)
    if hub_id is None:
        return None
    return SessionInfo(
        pid=-hub_id,
        proc_start=0,
        session_id=f"{HUB_SID_PREFIX}{hub_id}",
        cwd=getcwd(),
        name=None,
        name_source=None,
        kind=HUB_KIND,
    )


def claude_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def proc_stat(pid: int, proc_root: Path = Path("/proc")) -> tuple[int, int] | None:
    """(ppid, starttime) процесса или `None` (процесса нет, строка не разбирается). Имя процесса в скобках
    может содержать пробелы и скобки — разбор после ПОСЛЕДНЕЙ `)`."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _head, sep, rest = raw.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    # rest начинается с поля 3 (state): ppid — поле 4, starttime — поле 22.
    try:
        return int(fields[1]), int(fields[19])
    except IndexError, ValueError:
        return None


def process_alive(pid: int, proc_start: int, proc_root: Path = Path("/proc")) -> bool:
    """Ф33-1 К1: процесс `pid` жив (не зомби) и это тот же процесс (`starttime` совпал)."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _head, sep, rest = raw.rpartition(")")
    fields = rest.split()
    if not sep or len(fields) < 20 or fields[0] in ("Z", "X"):
        return False
    try:
        return int(fields[19]) == proc_start
    except ValueError:
        return False


class _Broken(Exception):
    """Файл реестра есть, но не читается — повторяемо."""


def _str_field(data: dict, key: str, limit: int, *, required: bool) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value) or len(value) > limit:
        raise _Broken(key)
    return value


def _read_entry(path: Path, pid: int) -> dict | None:
    """Содержимое файла реестра или `None` (файла нет). Битый — `_Broken`."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise _Broken("read") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _Broken("json") from exc
    if not isinstance(data, dict) or data.get("pid") != pid:
        raise _Broken("pid")
    return data


def _attempt(
    start_pid: int, *, proc_root: Path, sessions: Path, host: str | None = None, boot: str | None = None
) -> SessionInfo:
    pid = start_pid
    for _ in range(MAX_STEPS + 1):
        if pid <= 0:
            break
        stat = proc_stat(pid, proc_root)
        if stat is None:
            break
        ppid, starttime = stat
        if ext_engine(pid, proc_root) is not None:
            # Ф74-2: ближайший предок — процесс Codex/Cursor: это его сессия, реестр Claude дальше не ищется
            # (Claude Code, запустивший Codex, — не владелец его MCP-сервера).
            break
        data = _read_entry(sessions / f"{pid}.json", pid)
        if data is not None:
            proc_start = data.get("procStart")
            if isinstance(proc_start, str) and proc_start.isdigit():
                proc_start = int(proc_start)
            if not isinstance(proc_start, int) or isinstance(proc_start, bool):
                raise _Broken("procStart")
            if proc_start != starttime:
                raise RegistryError(GONE, "файл реестра остался от прежнего процесса — сессии нет")
            name_source = _str_field(data, "nameSource", NAME_SOURCE_MAX, required=False)
            # Имя и его источник — как есть (обрезка до 64: длиннее ядро отвергает 422); «только от /rename»
            # решает ядро.
            name = data.get("name")
            name = name[:NAME_MAX] if isinstance(name, str) else None
            return SessionInfo(
                pid=pid,
                proc_start=proc_start,
                session_id=_str_field(data, "sessionId", SESSION_ID_MAX, required=True),
                cwd=_str_field(data, "cwd", CWD_MAX, required=True),
                name=name,
                name_source=name_source,
                kind=_str_field(data, "kind", KIND_MAX, required=True),
                host=host,
                boot=boot,
            )
        pid = ppid
    raise RegistryError(GONE, "процесса сессии Claude Code среди предков нет")


def find_session(
    start_pid: int,
    *,
    proc_root: Path = Path("/proc"),
    claude_config: Path | None = None,
    environ: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SessionInfo:
    """Сессия Claude Code — первый предок `start_pid` (включая его самого) с файлом реестра. Ошибка —
    `RegistryError`: `GONE` (сессии нет — в т.ч. устойчиво, после повторов) или `UNREADABLE` (файл есть, но
    устойчиво битый)."""
    sessions = (claude_config if claude_config is not None else claude_dir(environ)) / "sessions"
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            sleep(RETRY_DELAY_S)
        try:
            return _attempt(
                start_pid,
                proc_root=proc_root,
                sessions=sessions,
                host=hostid.host_id(environ),
                boot=hostid.boot_id(environ),
            )
        except _Broken as exc:
            last = exc
        except RegistryError as exc:
            last = exc
    if isinstance(last, _Broken):
        raise RegistryError(UNREADABLE, f"реестр сессий Claude Code не читается ({last})")
    assert isinstance(last, RegistryError)
    raise last


def find_opencode_session(
    start_pid: int,
    oc_session: str | None,
    oc_dir: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: Path = Path("/proc"),
) -> SessionInfo:
    """Ф16-4 (3.5): сессия OpenCode, которую назвал плагин хаба. `oc_session` — `ses_…` (иначе плагина нет —
    `GONE` с подсказкой про скрипт установки); процесс `OPENCODE_PID` — сам `start_pid` или его предок (подъём
    по ppid, не больше 8 шагов), его `starttime` — `proc_start`; `oc_dir` — абсолютный существующий каталог
    (`cwd` сессии). Имени у сессии OpenCode нет. Ошибка — `RegistryError(GONE, …)`."""
    if not isinstance(oc_session, str) or not OC_SESSION_RE.fullmatch(oc_session):
        raise RegistryError(GONE, NO_PLUGIN_TEXT)
    env = os.environ if environ is None else environ
    raw = (env.get(OPENCODE_ENV) or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise RegistryError(GONE, "процесса OpenCode нет (OPENCODE_PID не задан)")
    oc_pid = int(raw)
    pid = start_pid
    proc_start: int | None = None
    for _ in range(MAX_STEPS + 1):
        if pid <= 0:
            break
        stat = proc_stat(pid, proc_root)
        if stat is None:
            break
        ppid, starttime = stat
        if pid == oc_pid:
            proc_start = starttime
            break
        pid = ppid
    if proc_start is None:
        raise RegistryError(GONE, "процесса OpenCode среди предков нет")
    if (
        not isinstance(oc_dir, str)
        or not oc_dir.startswith("/")
        or "\x00" in oc_dir
        or len(oc_dir) > CWD_MAX
        or not os.path.isdir(oc_dir)
    ):
        raise RegistryError(GONE, "каталог сессии OpenCode не найден")
    return SessionInfo(
        pid=oc_pid,
        proc_start=proc_start,
        session_id=oc_session,
        cwd=oc_dir,
        name=None,
        name_source=None,
        kind=OC_KIND,
        host=hostid.host_id(env),
        boot=hostid.boot_id(env),
    )


# --- Ф74-2: внешние окна Codex и Cursor -------------------------------------------------------------------


def _proc_names(pid: int, proc_root: Path) -> list[str]:
    """Базовые имена процесса: `argv[0]`, цель `/proc/<pid>/exe`, `comm`. Не читается — пусто."""
    names: list[str] = []
    try:
        argv0 = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0", 1)[0]
        if argv0:
            names.append(os.path.basename(argv0.decode("utf-8", "replace")))
    except OSError:
        pass
    try:
        names.append(os.path.basename(os.readlink(proc_root / str(pid) / "exe")))
    except OSError:
        pass
    try:
        names.append((proc_root / str(pid) / "comm").read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        pass
    return [n for n in names if n]


def plugin_waits(session: SessionInfo) -> bool:
    """Ожидание сигналов держит плагин хаба (OpenCode; Ф74-2 — внешние Codex и Cursor): ответ `hub_subscribe`
    несёт метку с командой, а не просьбу запустить её самой."""
    return session.kind == OC_KIND or session.kind in engine_registry.EXTERNAL


def ext_engine(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    """Движок внешнего окна, если процесс `pid` — Codex или Cursor (по профилю движка); иначе `None`."""
    return engine_registry.ext_engine_by_binary(_proc_names(pid, proc_root))


def ext_ancestor(start_pid: int, proc_root: Path = Path("/proc")) -> tuple[str, int, int] | None:
    """Ближайший предок `start_pid` (включая его самого, не больше 8 шагов) — процесс Codex/Cursor:
    `(движок, pid, starttime)`. Раньше встретился процесс с файлом реестра Claude Code — `None` не здесь:
    порядок «реестр Claude, затем внешний» держит `find_session` (останавливается на таком предке)."""
    pid = start_pid
    for _ in range(MAX_STEPS + 1):
        if pid <= 0:
            return None
        stat = proc_stat(pid, proc_root)
        if stat is None:
            return None
        ppid, starttime = stat
        engine = ext_engine(pid, proc_root)
        if engine is not None:
            return engine, pid, starttime
        pid = ppid
    return None


def _env_hub_session(pid: int, proc_root: Path) -> int | None:
    """`HUB_SESSION` из окружения процесса `pid` (`/proc/<pid>/environ`), годный номер — иначе `None`."""
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    prefix = HUB_SESSION_ENV.encode() + b"="
    for item in raw.split(b"\0"):
        if item.startswith(prefix):
            value = item[len(prefix) :].strip()
            if value.isdigit() and 1 <= int(value) <= HUB_ID_MAX:
                return int(value)
            return None
    return None


def held_ancestor(start_pid: int, proc_root: Path = Path("/proc")) -> tuple[str, int, int] | None:
    """Ф74-8 (п. 2): ближайший предок-движок (Codex/Cursor, как у `ext_ancestor`) — процесс СЕССИИ ХАБА:
    в его окружении стоит `HUB_SESSION` (хост хаба ставит его при запуске). `(движок, pid, номер сессии)`
    или `None`. Нужен MCP-серверу без своего `HUB_SESSION`: такой сервер Cursor/Codex подняли из настроек
    пользователя (`~/.cursor/mcp.json`) внутри сессии хаба — он не её сервер и личностью не служит."""
    found = ext_ancestor(start_pid, proc_root)
    if found is None:
        return None
    engine, pid, _start = found
    hub_id = _env_hub_session(pid, proc_root)
    return (engine, pid, hub_id) if hub_id is not None else None


def ext_hint(engine: str) -> str:
    """Отказ «плагина хаба нет» для внешнего окна движка."""
    profile = engine_registry.get(engine)
    installer = profile.ext_installer if profile is not None else None
    name = engine_registry.label(engine)
    return f"нужен плагин хаба для {name} — {installer}" if installer else f"нужен плагин хаба для {name}"


def find_ext_session(
    start_pid: int,
    ext_session: str | None,
    ext_dir: str | None,
    *,
    ext_pid: int | None = None,
    environ: Mapping[str, str] | None = None,
    proc_root: Path = Path("/proc"),
) -> SessionInfo:
    """Ф74-2 (Б1): сессия внешнего Codex/Cursor, которую назвал плагин хаба. Процесс движка — ближайший предок
    `start_pid` (MCP-сервер, хук) или явный `ext_pid` (ожидатель хука Codex, отвязанный от дерева процессов)
    —
    он должен быть живым процессом Codex/Cursor. `ext_session` — id разговора движка, `ext_dir` — абсолютный
    существующий каталог. Ошибка — `RegistryError(GONE, …)`."""
    env = os.environ if environ is None else environ
    if ext_pid is not None:
        stat = proc_stat(ext_pid, proc_root) if ext_pid > 0 else None
        engine = ext_engine(ext_pid, proc_root) if stat is not None else None
        if stat is None or engine is None:
            raise RegistryError(GONE, "процесса Codex/Cursor с таким pid нет")
        found = (engine, ext_pid, stat[1])
    else:
        found = ext_ancestor(start_pid, proc_root)
        if found is None:
            raise RegistryError(GONE, "процесса Codex/Cursor среди предков нет", ext_absent=True)
    engine, pid, proc_start = found
    if not isinstance(ext_session, str) or not EXT_SESSION_RE.fullmatch(ext_session):
        raise RegistryError(GONE, ext_hint(engine))
    if (
        not isinstance(ext_dir, str)
        or not ext_dir.startswith("/")
        or "\x00" in ext_dir
        or len(ext_dir) > CWD_MAX
        or not os.path.isdir(ext_dir)
    ):
        raise RegistryError(GONE, f"каталог сессии {engine_registry.label(engine)} не найден")
    return SessionInfo(
        pid=pid,
        proc_start=proc_start,
        session_id=f"{engine}{engine_registry.EXT_SEP}{ext_session}",
        cwd=ext_dir,
        name=None,
        name_source=None,
        kind=engine,
        host=hostid.host_id(env),
        boot=hostid.boot_id(env),
    )
