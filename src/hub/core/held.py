"""Сессия хаба — запись ядра `main.hub_session` (Ф41-2а, docs/architecture.md и
«Поправки после plan-critic» 3а — они приоритетнее). Здесь — личность и живость; создание, закрытие и ручки
хоста — `hub_sessions.py`.

Личность сессии хаба в подписках — постоянная УСЛОВНАЯ тройка `(-id, 0, "hub:<id>")` (П1): настоящий pid
процесса и id разговора движка в неё не входят (они меняются при каждом подъёме), поэтому все сравнения троек,
уникальные индексы живых подписок, `skey` и лимиты работают без правок.

Живость условной тройки — запись, а не `/proc`:

- `alive_db(conn, pid, proc_start)` — по строке `main.hub_session` в СВОЕЙ транзакции. Только так решают
  места, которые по «сессия мертва» ЗАВЕРШАЮТ подписку (П2): `session_gone` у спящей сессии невосстановим;
- реестр в памяти (`Held`) — для мест без соединения (`subs.is_alive`: показ `live`, быстрые отказы). Пустой
  или устаревший реестр даёт отказ, но ничего не завершает. Меняется маршрутом ПОСЛЕ `await db.write(...)`
  (откат транзакции реестр не трогает), на сборке приложения перечитывается из базы.

Модуль не импортирует `subs` (тот импортирует этот)."""

import weakref
from typing import NamedTuple

from hub.engine import registry as engine_registry

KIND = "hub"
HELD_PROC_START = 0
SID_PREFIX = "hub:"
# Ф71-1: `cursor` — Cursor CLI. Ф74-1: движки сессий хаба — профили с `available` (`engine/registry.py`).
ENGINES = engine_registry.HELD_ENGINES
STATES = ("running", "idle", "asleep")
MODES = ("manual", "auto")
# Ф41-6: `adopt` — «Забрать в хаб»; `request`, `agent`, `handoff` — заделы 41-5 и 41-7 (в базе ограничения
# на столбец нет).
CREATED_BY = ("ui", "host", "adopt", "request", "agent", "handoff")
# 3.1: незакрытых сессий хаба на проект (отдельно от `subs.MAX_PROJECT_SUBS` внешних подписок, П6).
MAX_HUB_SESSIONS_PER_PROJECT = 8
# 2.3: хост не подтверждал состояние дольше — ядро само переводит `running`/`idle` в `asleep`.
HOST_STALE_MS = 120_000
TITLE_MAX = 64
# Ф41-2б (2.2, П2, П13): файл базы строк сессии и причины, по которым реплика ждёт запуска.
STORES = ("main", "work")
# Ф41-4 П3д: `heavy` — сигналы ждут, но контекст большой и кэш остыл (поднимет реплика или «Поднять»).
# Ф48-2 (раздел B): `compact` — срочная реплика ждёт конца идущего сжатия (не прерывает его).
WAIT_REASONS = ("slot", "elsewhere", "paused", "heavy", "compact")
# Ф48-2 (раздел D): границы своего порога сжатия сессии, токены.
COMPACT_TOKENS_MIN = 50_000
COMPACT_TOKENS_MAX = 900_000
ID_MAX = 2**31 - 1  # условный pid — в пределах поля `pid` моделей запросов
STATE_RU = {"running": "в работе", "idle": "ждёт входа", "asleep": "спит"}
# Ф50-4: источник идущего хода, как его называет хост (`turn` с `source`).
TURN_SOURCES = ("say", "signal", "self", "compact")

_COLUMNS = (
    "id, project_id, role, role_key, role_no, engine, model, effort, mode, engine_session_id, workdir, "
    "title, state, proc_pid, proc_start, host_seen_ts, created_ts, updated_ts, closed_ts, close_reason, "
    "created_by, seq, last_event_id, interrupt_requested_ts, sleep_requested_ts, last_turn_ts, launch_fails, "
    "failure, store, wait_reason, usage_json, want_sub_json, wake_requested_ts, compact_requested_ts, "
    "signal_turn_ts, signal_lost_ts, autosend_ok, launch_key_hash, bg_json, compact_tokens, compact_auto, "
    "compact_at, role_file, sys_pin, turn_ts, turn_origin, mid_queued_ts, subdir, via_agent, turns, "
    "mcp_reload_ts, fallback_json, on_fallback_json, always_json"
)


class HubSession(NamedTuple):
    id: int
    project_id: int
    role: str | None
    role_key: str | None
    role_no: int | None  # номер линии роли; у сессии без роли — `None` (номер безрольной — только адрес)
    engine: str
    model: str | None
    effort: str | None
    mode: str
    engine_session_id: str | None
    workdir: str
    title: str | None
    state: str
    proc_pid: int | None
    proc_start: int | None
    host_seen_ts: int | None
    created_ts: int
    updated_ts: int
    closed_ts: int | None
    close_reason: str | None
    created_by: str
    # Ф41-2б (2.2, П1, П2, П13) — 0057_main.sql.
    seq: int
    last_event_id: int | None
    interrupt_requested_ts: int | None
    sleep_requested_ts: int | None
    last_turn_ts: int | None
    launch_fails: int
    failure: str | None
    store: str | None  # файл событий, запросов и реплик; `None` — только у записи 41-2а до первой записи
    wait_reason: str | None
    # Ф41-3 (2.3) — 0058_main.sql: расход `{"context_tokens", "context_limit", "cost_usd", "updated_ts"}`.
    usage_json: str | None
    # Ф41-4 (2.1, 2.5, П5) — 0060_main.sql: желаемый фильтр подписки, просьбы «поднять» и «сжать», ход по
    # сигналу (идёт / оборван).
    want_sub_json: str | None = None
    wake_requested_ts: int | None = None
    compact_requested_ts: int | None = None
    signal_turn_ts: int | None = None
    signal_lost_ts: int | None = None
    # Ф41-5 (3.3, П1) — 0061_main.sql: может ли отвечать по списку «без подтверждения»; хеш ключа запуска
    # идущего процесса (стирается при сне и закрытии).
    autosend_ok: int = 1
    launch_key_hash: str | None = None
    # Ф48-1 — 0066_main.sql: фоновые задачи процесса сессии `{"items": [...]}`; `None` — нет и не было.
    bg_json: str | None = None
    # Ф48-2 (раздел D) — 0067_main.sql: свой порог авто-сжатия (`None` — умолчание хоста), выключатель
    # авто-сжатия и действующий порог, как его посчитал хост (`None` — ещё не сообщал).
    compact_tokens: int | None = None
    compact_auto: int = 1
    compact_at: int | None = None
    # Ф48-3 — 0069_main.sql: путь к MD-файлу роли относительно рабочей папки проекта (`None` — не задан).
    role_file: str | None = None
    # Ф48-5 — 0071_main.sql: снимок закреплённого системного текста Claude Code (JSON `{mode, role, role_no,
    # role_file, file_sha}`; `None` — снимка нет). Пишет только хост вместе с привязкой разговора.
    sys_pin: str | None = None
    # Ф50-4 (П9) — 0073_main.sql: начало и источник идущего хода (`None` — хода нет); сигнал, поданный
    # посреди хода, ещё стоит в очереди движка.
    turn_ts: int | None = None
    turn_origin: str | None = None
    mid_queued_ts: int | None = None
    # Ф50-7 — 0074_main.sql: подпапка процесса сессии относительно `workdir` (папки проекта); `None` —
    # корень проекта. Задаётся при создании и не меняется.
    subdir: str | None = None
    # Ф56-1а — 0079_main.sql: переключатель «канал владельца» (1 — ожидания сессии идут агенту владельца, пока у проекта
    # снят выключатель `human_agent`). Ставит только владелец.
    via_agent: int = 0
    # Ф56-1б — 0082_main.sql: счётчик законченных ходов (ключ карточки `turn:<id>:<turns>`); ведёт ядро.
    turns: int = 0
    # Ф73-1 — 0103_main.sql: «Перечитать MCP» — мс нажатия владельца (`None` — просьбы нет); снимает хост.
    mcp_reload_ts: int | None = None
    # Ф83-1 — 0108_main.sql: свой запасной движок (`None` — как у проекта; `{"engine": null}` — нет) и пометка
    # «на запасном» (`None` — на основном). Форма — `core/engine_fallback.py`.
    fallback_json: str | None = None
    on_fallback_json: str | None = None
    # Ф99-7.2 — 0119_main.sql: «Всегда в этой сессии» (`[{"key", "ts"}]`; `None` — пусто). Форма —
    # `core/session_always.py`.
    always_json: str | None = None


# --- условная тройка ----------------------------------------------------------------------------------------


def held_pid(hub_session_id: int) -> int:
    """Условный pid сессии хаба — минус её id."""
    return -int(hub_session_id)


def held_id(pid) -> int | None:
    """id сессии хаба для условного (отрицательного) pid; иначе `None`."""
    if isinstance(pid, int) and not isinstance(pid, bool) and pid < 0:
        return -pid
    return None


def is_held(pid) -> bool:
    return held_id(pid) is not None


def held_sid(hub_session_id: int) -> str:
    """Третий элемент тройки (П1): постоянная метка, не id разговора движка. Не начинается с `ses_` — правило
    брошенной подписки OpenCode (`subs.oc_stale`) к ней не относится."""
    return f"{SID_PREFIX}{int(hub_session_id)}"


def triple(hub_session_id: int) -> tuple[int, int, str]:
    return held_pid(hub_session_id), HELD_PROC_START, held_sid(hub_session_id)


_NO_KIND = object()


def triple_error(pid, proc_start, session_id, kind=_NO_KIND) -> str | None:
    """П4: общий валидатор тройки для всех ручек, принимающих `session`. Годится либо тройка настоящего
    процесса (`pid >= 1` и `proc_start >= 1` — `starttime` живого процесса нулём не бывает; ноль занят
    условной тройкой), либо условная тройка сессии хаба: `pid <= -1`, `proc_start == 0`, `session_id ==
    "hub:<-pid>"`. `kind` (где он передаётся): `"hub"` — только с условной тройкой и наоборот. Текст отказа
    или `None`."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid == 0:
        return "pid: не ноль"
    if pid > 0:
        if not isinstance(proc_start, int) or isinstance(proc_start, bool) or proc_start < 1:
            return "proc_start: starttime процесса, целое больше нуля"
        if kind == KIND:
            return "kind=hub — только с условной тройкой сессии хаба"
        return None
    if proc_start != HELD_PROC_START or session_id != held_sid(-pid):
        return "тройка сессии хаба: pid = -id, proc_start = 0, session_id = hub:<id>"
    if kind is not _NO_KIND and kind != KIND:
        return "условная тройка — только у сессии хаба (kind=hub)"
    return None


# --- реестр в памяти ядра -----------------------------------------------------------------------------------


class Held:
    """Множество id незакрытых сессий хаба. Общий объект публичного и внутреннего приложений одной базы."""

    def __init__(self) -> None:
        self._ids: set[int] = set()

    def load(self, conn) -> None:
        self._ids = {
            row[0] for row in conn.execute("SELECT id FROM main.hub_session WHERE closed_ts IS NULL")
        }

    def add(self, hub_session_id: int) -> None:
        self._ids.add(int(hub_session_id))

    def discard(self, hub_session_id: int) -> None:
        self._ids.discard(int(hub_session_id))

    def __contains__(self, hub_session_id) -> bool:
        return hub_session_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


# Модульная ссылка для `subs.is_alive` (у неё нет ни соединения, ни приложения). Без приложения реестр пуст —
# отрицательный pid мёртв. Реестры по базам — слабые ссылки: приложение, собранное над той же `Db` (публичное
# и внутреннее в тестах — по отдельности), получает ТОТ ЖЕ объект.
_current = Held()
_by_db: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# Ф41-4 П11: когда хост постоянных сессий последний раз звал `/work/host/wait` (мс часов ядра) — в памяти
# процесса, рядом с реестром: по нему `listening` подписки сессии хаба (спящих хост не подтверждает, поэтому
# `host_seen_ts` записи не годится).
_host_wait_ms: int | None = None


def install(db) -> Held:
    """Сборка приложения (П2б): реестр базы `db` перечитывается из неё и становится текущим — ДО запуска
    фоновых задач. Приложение над другой базой заменяет текущий реестр целиком (в тестах он не течёт между
    базами)."""
    global _current, _host_wait_ms  # noqa: PLW0603 — единственная точка замены текущего реестра
    registry = _by_db.get(db)
    if registry is None:
        registry = _by_db[db] = Held()
    with db.reader() as conn:
        registry.load(conn)
    _current = registry
    _host_wait_ms = None  # сборка приложения: хост ещё не звал ожидание (и в тестах значение не течёт)
    return registry


def reset() -> None:
    """Тесты: как процесс без приложения — реестр пуст."""
    global _current, _host_wait_ms  # noqa: PLW0603
    _current = Held()
    _host_wait_ms = None


def host_waited(now_ms: int) -> None:
    """Ф41-4 П11: хост зовёт (или держит) своё длинное ожидание."""
    global _host_wait_ms  # noqa: PLW0603
    _host_wait_ms = now_ms


def host_listening(now_ms: int) -> bool:
    """Ф41-4 П11: `listening` подписки сессии хаба — хост ждал не позже `HOST_STALE_MS` назад (и у спящей:
    её поднимет сигнал)."""
    return _host_wait_ms is not None and 0 <= now_ms - _host_wait_ms <= HOST_STALE_MS


def current() -> Held:
    return _current


def registry_alive(pid: int, proc_start: int) -> bool:
    """`subs.is_alive` для условного pid: `proc_start == 0` и сессия хаба — в текущем реестре."""
    return proc_start == HELD_PROC_START and held_id(pid) in _current


# --- чтение записи ------------------------------------------------------------------------------------------


def get(conn, hub_session_id: int) -> HubSession | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM main.hub_session WHERE id = ?",  # noqa: S608 — список колонок константа
        (hub_session_id,),
    ).fetchone()
    return HubSession(*row) if row is not None else None


def get_open(conn, hub_session_id: int | None) -> HubSession | None:
    """Незакрытая сессия хаба или `None` (нет строки, закрыта)."""
    if hub_session_id is None:
        return None
    rec = get(conn, hub_session_id)
    return rec if rec is not None and rec.closed_ts is None else None


def open_id_of(conn, session) -> int | None:
    """Ф56-1а (А9): номер незакрытой сессии хаба по условной тройке запроса (словарь с `pid`, `proc_start`,
    `session_id`); у внешней сессии, чужой тройки или закрытой записи — `None`. Только подпись и привязка
    ожидания к сессии (как `agent_requests._hub_session_id`); прав не даёт."""
    if not isinstance(session, dict):
        return None
    hub_id = held_id(session.get("pid"))
    if hub_id is None:
        return None
    if (session.get("pid"), session.get("proc_start"), session.get("session_id")) != triple(hub_id):
        return None
    return hub_id if get_open(conn, hub_id) is not None else None


def alive_db(conn, pid: int, proc_start: int) -> bool:
    """П2а: живость условной тройки по базе — строка `main.hub_session` есть и не закрыта. Для разрушающих
    решений (завершить подписку) — только она, не реестр."""
    return proc_start == HELD_PROC_START and get_open(conn, held_id(pid)) is not None


def of_rec(conn, rec) -> HubSession | None:
    """Запись сессии хаба подписки `rec` (на проект или на беседу) — и закрытая; у внешней сессии — `None`."""
    hub_id = held_id(rec.session_pid)
    return get(conn, hub_id) if hub_id is not None else None


def list_sessions(
    conn, *, project_id: int | None = None, closed: bool = False, project_ids=None
) -> list[HubSession]:
    """Сессии хаба: незакрытые (`closed=False`) или закрытые, по `id`. `project_ids` — скоуп токена (`None` —
    все проекты)."""
    where = ["closed_ts IS NOT NULL" if closed else "closed_ts IS NULL"]
    params: list = []
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if project_ids is not None:
        ids = sorted(project_ids)
        if not ids:
            return []
        where.append(f"project_id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.hub_session WHERE {' AND '.join(where)} ORDER BY id",  # noqa: S608
        params,
    ).fetchall()
    return [HubSession(*row) for row in rows]


def role_lines(conn, project_id: int, *, except_id: int | None = None) -> dict[tuple[str, int], HubSession]:
    """П3: линии роли `(role_key, role_no)`, которые держат незакрытые сессии хаба проекта САМОЙ ЗАПИСЬЮ —
    есть у них живая подписка или нет. `except_id` — кроме этой сессии (своя линия ей не чужая)."""
    return {
        (rec.role_key, rec.role_no): rec
        for rec in list_sessions(conn, project_id=project_id)
        if rec.role_key is not None and rec.id != except_id
    }


def shown_name(rec: HubSession) -> str:
    """Имя сессии хаба для текстов отказов: название, иначе номер. Название задаёт владелец."""
    return rec.title if rec.title else f"#{rec.id}"


def sub_fields(rec: HubSession) -> dict:
    """3.6: поля строки подписки сессии хаба (`GET /subs`): запись, а не процесс."""
    return {"held": True, "hub_session_id": rec.id, "state": "asleep" if rec.closed_ts else rec.state}


def select(
    conn, where: str, params: list, *, order: str = "id", limit: int | None = None
) -> list[HubSession]:
    """Ф41-2б: записи по доверенному условию `where` (имена столбцов без псевдонима) — и закрытые тоже; для
    лент изменений по `seq` (`held_work`)."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.hub_session WHERE {where} ORDER BY {order}"  # noqa: S608
        + (" LIMIT ?" if limit is not None else ""),
        [*params, *([limit] if limit is not None else [])],
    ).fetchall()
    return [HubSession(*row) for row in rows]
