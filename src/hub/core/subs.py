"""Подписка сессии Claude Code на проект хаба и сигналы о карточках. Спецификация:
docs/architecture.md (B0–B5) и «Поправки после plan-critic».

Модуль пишет только внутри `Db.write()` (как `agent_items.py`), время — параметром `now_ms`. Текстов нет:
сигнал — только числа и метка из перечня. Живость сессии — pid + `starttime` процесса (поле 22
`/proc/<pid>/stat`); файлы Claude Code ядро не читает. `read_proc_start` — единственное место, которое смотрит
в `/proc`; тесты подменяют её (`monkeypatch.setattr(subs, "read_proc_start", …)`).

Кто ждёт сигнала сейчас (флаг «ожидание идёт») — только в памяти ядра (`SubWaiters`): рестарт ядра его
обнуляет, `last_wait_ts` пишется только в конце ожидания.
"""

import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import NamedTuple

from ..engine import registry as engine_registry
from . import agent_items as agent_items_mod
from . import engine as engine_mod
from . import held as held_mod
from . import keywords as keywords_mod
from . import machines as machines_mod
from . import session_host as session_host_mod
from . import sub_accounts
from . import threads as threads_mod
from . import transcript as transcript_mod
from . import unsorted as unsorted_mod
from .audit import write_audit
from .db import db_of, next_seq
from .presence import is_visible

SUB_LISTEN_GRACE_S = 600
SUB_DEBOUNCE_S = 60
SUB_DEBOUNCE_MS = SUB_DEBOUNCE_S * 1000
# Ф14-5: дебаунс подписок на беседы и режима `messages` подписки на проект (у `cards` — 60 с). Здесь, а не
# в `chan_subs`: тот импортирует этот модуль.
CHAN_DEBOUNCE_MS = 5_000
MAX_SIGNALS_PER_DELIVERY = 20
BACKFILL_MAX_AGE_MS = 6 * 60 * 60 * 1000  # окно свежести hub-wake (`max_card_age_s`, 6 ч)
ENDED_RECENT_MS = 24 * 60 * 60 * 1000  # `GET /subs?live=0`, `hub sub list`: завершённые за сутки
ENDED_DAYS_MAX = 30  # Ф92-1 (B5): `hub sub list --days N` — не глубже месяца
# Ф38-3 C: подсказка о других линиях роли (`Subscribed.other_lines`) — свободная линия помнится 7 суток,
# в ответе не больше 5 линий.
LINE_HINT_MS = 7 * 24 * 60 * 60 * 1000
LINE_HINT_MAX = 5
SESSION_NAME_MAX = 64
SESSION_ID8 = 8
# Ф26-1: живых подписок на проект — не больше пяти (по одной на сессию); роль — метка подписки, 1–32 символа.
MAX_PROJECT_SUBS = 5
ROLE_MAX = 32
# Ф35-1 (решение 2): номер в роли; адрес агента — `(проект, роль, номер)`, у безрольных — без роли.
ROLE_NO_MAX = 99
# Ф35-1 П6: занятые имена ролей (по ключу сравнения) — так в выдаче зовут владельца и сам хаб.
RESERVED_ROLE_KEYS = frozenset(
    {
        "я",
        "пользователь",
        "хозяин",
        "владелец",
        "user",
        "owner",
        "система",
        "system",
        "админ",
        "admin",
    }
)
# Ф35-1 (решение 14): агент вне проектов — подписка с `project_id = 0` (`-1` и `-2` заняты псевдопроектами
# `queries`), только режим `direct`, только токен без списка проектов; живых — до `MAX_FREE_SUBS`.
NO_PROJECT_ID = 0
MAX_FREE_SUBS = 20
# Н3: общий выключатель агентов вне проектов — ключ `main.meta` (нет ключа — включено).
META_FREE_AGENTS = "free_agents"
NOT_IN_PROJECT = "сессия не в рабочей папке проекта"
FREE_HINT = NOT_IN_PROJECT + ' — агентом вне проектов: hub_subscribe(mode="direct", role="…")'
ROLE_RE = re.compile(rf"[\w \-]{{1,{ROLE_MAX}}}")
# Ф26-1 П4: ключ сессии в `GET /subs` (`skey`) — первые 16 hex sha256 «токен:pid:starttime:session_id».
SKEY_LEN = 16
# Ф26-1 A2: откуда новая подписка взяла режим, ключи и роль (`inherited_why`).
INHERIT_WHY = ("role", "process", "session", "name", "only")

SIGNAL_LABELS = ("urgent", "reply")
# 21.10: «Новая с передачей дел» — подписки прежней сессии переходят новой той же линии роли (Ф32-1, Ф36-1).
HANDOFF_END_REASON = "handoff"
# Ф92-1 (B3): токен профиля перевыпущен (`hub mcp setup`; прежний отозван с `replaced_by`) — подписки прежнего
# токена кончаются «сами» (беседы возвращаются сессии под новым токеном); явный отзыв — `token_revoked`.
TOKEN_REISSUED_END_REASON = "token_reissued"  # noqa: S105 — причина конца подписки, не секрет
END_REASONS = (
    "session_gone",
    "conversation_changed",
    "unsubscribed",
    "ended_by_owner",
    "subs_off",
    "registry_unreadable",
    "token_revoked",
    "replaced",
    # Ф16-1 (3.6): владелец переключил движок сессий — подписки чужого движка сняты ядром.
    engine_mod.END_REASON,
    HANDOFF_END_REASON,
    TOKEN_REISSUED_END_REASON,
)
CLIENT_END_REASONS = ("unsubscribed", "conversation_changed", "registry_unreadable")
# Ф16-1 (П15): подписываться может интерактивная сессия Claude Code (`interactive`) или сессия OpenCode
# (`opencode`, её приводит плагин хаба). Порядок проверок в `subscribe`: живость -> вид -> движок.
# Ф41-2а (3.2): `hub` — сессия хаба (запись `main.hub_session`), только с условной тройкой (`held`).
# Ф74-2 (Б2): внешние окна Codex/Cursor — `kind` = имя движка (их приводит плагин хаба), из профиля движков.
SESSION_KINDS = ("interactive", "opencode", held_mod.KIND, *engine_registry.EXTERNAL)
# Ревью Ф13-1 (важное 1): `draft_exists` снят — черновик в беседе не доказывает, что сессия видела новое
# сообщение (пришло, пока она писала; другой тред Slack); от двойного черновика — подсказка `hub_drafts` в
# выводе сигнала и подтверждение владельца.
SKIP_REASONS = ("closed", "answered", "read", "present", "moved", "chan_sub")

_SUB_COLUMNS = (
    "id, project_id, api_token_id, session_pid, session_proc_start, session_id, session_cwd, session_name, "
    "created_ts, last_wait_ts, last_delivery_ts, ended_ts, end_reason, mode, msg_filter, keywords_json, "
    "role, role_key, role_no, direct, accounts_json, session_host, session_engine"
)
# Ф14-5 П20: режим подписки на проект — карточки дежурного (13-1) или каждое чужое входящее беседы проекта.
# Ф35-1 (решение 4, П11): `direct` — «только адресованное агенту»; хранится флагом `direct` (в столбце `mode`
# при нём `cards`), наружу — `mode: "direct"`.
DIRECT = "direct"
MODES = ("cards", "messages", DIRECT)
# Ф20-1 П1: фильтр сигналов режима `messages` — `all` (каждое чужое входящее, кроме ботов) или `personal`
# («лично мне»: лички, личные упоминания, ответы в тредах владельца и на его сообщения). При `cards` — только
# `all`.
MSG_FILTERS = ("all", "personal")


class SubRec(NamedTuple):
    id: int
    project_id: int
    api_token_id: int
    session_pid: int
    session_proc_start: int
    session_id: str
    session_cwd: str
    session_name: str | None
    created_ts: int
    last_wait_ts: int | None
    last_delivery_ts: int | None
    ended_ts: int | None
    end_reason: str | None
    mode: str = "cards"
    msg_filter: str = "all"
    # Ф25-1 A1: ключи «лично мне» — `{"keys": [...], "exclude": [...]}` (нормализованы), `None` — без ключей.
    keywords_json: str | None = None
    # Ф26-1: роль подписки (как задала сессия) и её ключ сравнения (`role_key`); `None` — без роли.
    role: str | None = None
    role_key: str | None = None
    # Ф35-1: номер в роли (линия агента) и флаг режима `direct`.
    role_no: int = 1
    direct: int = 0
    # Ф38-1 (п.5, П1): фильтр по аккаунтам — `{"only": [..]}` или `{"except": [..]}`; `None` — все аккаунты.
    accounts_json: str | None = None
    # Ф33-1: машина сессии; пусто или машина ядра — живость по `/proc` ядра (реестр удалённых не судит).
    session_host: str | None = None
    # Ф74-2 (Б2): движок сессии подписки (`claude`, `opencode`, `codex`, `cursor`); у сессии хаба — движок её
    # записи. NULL — строка до 0105 без разметки (движок — по `session_id`, `rec_engine`).
    session_engine: str | None = None


class Subscribed(NamedTuple):
    """Итог `subscribe`: подписка, создана ли, добрано сигналов, из какой подписки взято унаследованное и
    почему (`INHERIT_WHY`)."""

    rec: SubRec
    created: bool
    backfilled: int
    inherited_from: int | None
    inherited_why: str | None
    # Ф26-1 Р11: шаг «единственная в проекте» не сработал — за сутки в проекте жили одновременно несколько
    # сессий без роли (чьё — не понять).
    inherit_blocked: bool = False
    # Ф32-1 П3: источник роли — последняя завершённая подписка проекта с тем же `role_key` (без фильтра режима
    # и токена; только у НОВОЙ подписки с ролью): по нему возвращаются подписки на беседы
    # (`chan_subs.restore_for_role`). `role_source_reason` — почему источник кончился; источник, снятый этим
    # же вызовом как мёртвый (`replaced`, сессии уже нет), — `session_gone` (П7: cleanup ещё не прошёл).
    role_source_id: int | None = None
    role_source_reason: str | None = None
    # Ф38-1 П6: действующий фильтр по аккаунтам взят у источника наследования, а не из запроса.
    accounts_inherited: bool = False
    # Ф38-3 C: другие линии той же роли с ДРУГИМИ настройками (`_other_lines`) — только у новой подписки с
    # ролью в проекте, когда номер не задан и взят наименьший свободный (прежней подписки этой сессии нет:
    # чья линия — ядру узнать не по чему, сессии говорится об этом в ответе).
    other_lines: tuple[dict, ...] = ()
    # Ф92-1 (B2): источник роли взят с ДРУГОЙ линии той же роли (своей прежней линии нет) — при возврате бесед
    # не трогать те, что держит живая сессия роли.
    role_source_wide: bool = False


class SubError(Exception):
    """Отказ ручки подписки: `status` — HTTP-код, `detail` — текст ответа (без текстов сообщений)."""

    def __init__(self, status: int, detail) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def gone(end_reason: str | None) -> SubError:
    """410 по контракту B5: `{"detail": {"end_reason": …}}`."""
    return SubError(410, {"end_reason": end_reason})


# Ф16-1 (3.5): сессия чужого движка — 409 с машинной меткой, по ней MCP пишет человеку, какой движок сейчас
# в хабе и где переключатель (текст — в `mcp_server`, ядро текстов не знает).
# Ф16-4 (П1): живость подписки сессии OpenCode. Процесс OpenCode (сервер 4096) живёт долго и держит много
# сессий, поэтому «процесс жив» не значит «сессия жива»: подписка OpenCode жива, пока процесс жив И (идёт
# ожидание ИЛИ последнее ожидание моложе 10 мин ИЛИ подписке меньше 10 мин). Иначе — `session_gone`.
OC_LIVE_GRACE_MS = 10 * 60 * 1000


def is_oc(session_id: str | None) -> bool:
    """Подписка (или запрос) сессии OpenCode — по идентификатору разговора (`ses_…`, 3.6)."""
    return engine_mod.engine_of_session(None, session_id) == "opencode"


def rec_engine(rec) -> str:
    """Ф74-2 (Б2): движок сессии строки подписки (на проект или на беседу) — столбец `session_engine`, у
    строки без разметки — по `session_id`."""
    stored = getattr(rec, "session_engine", None)
    return stored if stored else engine_mod.engine_of_session(None, rec.session_id)


def many_convs(session_id: str | None, kind: str | None = None) -> bool:
    """Ф74-2 (Б2): разговор запроса — внешней сессии движка, у которого один процесс держит много разговоров
    (OpenCode, Codex, Cursor; профиль `many_convs_per_proc`): другой разговор того же процесса — другая живая
    сессия, а не «сменившийся разговор». Условная тройка сессии хаба — нет (её pid ни с кем не делится)."""
    return engine_mod.many_convs(engine_mod.engine_of_session(kind, session_id))


def engine_for_insert(conn, session: dict) -> str:
    """Ф74-2 (Б2): значение `session_engine` новой строки подписки: у сессии хаба — движок её записи, у
    внешней — по `kind` и `session_id` тела."""
    hub_id = held_mod.held_id(session.get("pid"))
    if hub_id is not None:
        hub = held_mod.get(conn, hub_id)
        if hub is not None and hub.engine:
            return hub.engine
    return engine_mod.engine_of_session(session.get("kind"), session.get("session_id"))


def rec_many_convs(rec) -> bool:
    """`many_convs` для строки подписки — по её движку; у сессии хаба — нет."""
    return not held_mod.is_held(rec.session_pid) and engine_mod.many_convs(rec_engine(rec))


def oc_stale(rec, now_ms: int, *, waiting: bool) -> bool:
    """П1: подписка OpenCode брошена — ожидания нет, последнее старше 10 мин и сама подписка старше 10 мин.
    У подписок Claude Code — всегда `False` (их живость — только процесс). Ф74-2 (В2): то же у внешних Codex и
    Cursor со сроком их профиля (`idle_grace_ms`); у сессии хаба — `False` (её живость держит хост)."""
    if waiting or held_mod.is_held(rec.session_pid):
        return False
    grace = engine_mod.idle_grace_ms(rec_engine(rec))
    if grace is None:
        return False
    if rec.last_wait_ts is not None and now_ms - rec.last_wait_ts <= grace:
        return False
    return now_ms - rec.created_ts > grace


def same_session(rec, pid: int, proc_start: int, session_id: str) -> bool:
    """П17: подписка этой сессии — полная тройка `(pid, proc_start, session_id)`."""
    return rec.session_pid == pid and rec.session_proc_start == proc_start and rec.session_id == session_id


def skey(rec) -> str:
    """Ф26-1 П4: ключ сессии подписки (на проект или на беседу) — по нему hub-wake узнаёт подписку проекта той
    же сессии, что держит подписку на беседу (pid не годится: у OpenCode он общий на все сессии сервера)."""
    raw = f"{rec.api_token_id}:{rec.session_pid}:{rec.session_proc_start}:{rec.session_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:SKEY_LEN]


def role_form(value: str) -> tuple[str | None, str | None]:
    """Ф26-1 A2: роль запроса -> `(role, role_key)`; `""` (и одни пробелы) — `(None, None)`, роль снять.
    Неверная форма — 422. Хранится как задано (без крайних пробелов), сравнивается по ключу: без регистра,
    `ё` = `е`, пробелы схлопнуты."""
    text = value.strip()
    if not text:
        return None, None
    if not ROLE_RE.fullmatch(text) or "--" in text:
        # Ф35-2 (приёмка): два дефиса подряд — нет: роль печатается в служебных строках MCP, а рамки блоков
        # там начинаются с «-----».
        raise SubError(422, f"role: 1–{ROLE_MAX} символа — буквы, цифры, пробел, «-», «_» (без «--»)")
    key = " ".join(text.casefold().replace("ё", "е").split())
    if key in RESERVED_ROLE_KEYS:
        raise SubError(422, f"role: «{text}» занято — так в хабе зовут владельца и сам хаб; назови роль по делу")
    return text, key


def mode_of(rec) -> str:
    """Режим подписки наружу (`GET /subs`, соседи, аудит): флаг `direct` главнее столбца `mode` (П11)."""
    return DIRECT if rec.direct else rec.mode


def line_name(role: str | None, role_no: int) -> str:
    """Линия агента словами — для текстов отказов."""
    return f"роль «{role}» №{role_no}" if role is not None else f"без роли №{role_no}"


def _sub_dead(rec, now_ms: int, waiting) -> bool:
    """Подписка держит место и роль, только пока жива: процесс мёртв или подменён, или (П6) брошенная подписка
    OpenCode (`oc_stale`, как в `cleanup`). Без соединения — для отказов; завершать подписку по этому ответу
    нельзя (Ф41-2а П2: у сессии хаба он — по реестру в памяти), для этого — `rec_dead`."""
    if not rec_alive(rec):
        return True
    return oc_stale(rec, now_ms, waiting=bool(waiting and waiting(rec.id)))


def session_alive(
    conn, pid: int, proc_start: int, host: str | None = None, *, session_id: str | None = None
) -> bool:
    """Ф41-2а П2: живость сессии для РАЗРУШАЮЩИХ решений (завершить подписку, отдать её линию). Условная
    тройка сессии хаба — по строке `main.hub_session` в этой транзакции (`held.alive_db`), а не по реестру в
    памяти; настоящий процесс — как `is_alive`. Ф33-1 (К2): сессия другой машины «не в сети» (`STALE`) для
    разрушающих решений жива — снимают её только `gone`, смена разговора и 12 ч без связи (`DEAD`). `host` —
    машина строки подписки (`session_host`) или тела запроса; пусто или машина ядра — `/proc`, как раньше.
    Ф99-0: `session_id` внешнего Codex/Cursor — процесс не проверяется (`ext_idle`)."""
    if held_mod.is_held(pid):
        return held_mod.alive_db(conn, pid, proc_start)
    far = session_host_mod.state(host, pid, proc_start)
    if far is not None:
        return far != session_host_mod.DEAD
    return is_alive(pid, proc_start, session_id=session_id)


def ext_idle(pid: int, session_id: str | None) -> bool:
    """Ф99-0 (итоги проб): личность внешнего Codex/Cursor — разговор (`<движок>:<id>`), а не процесс. Процесс
    MCP-сервера окна Codex — общий демон `codex app-server`: один на все окна и разговоры, перезапускается сам
    (разговор жив, pid и `starttime` новые) — по `/proc` такую подписку не судят. Живость — срок без
    ожидания профиля движка (`oc_stale`, `idle_grace_ms`); смену процесса при том же разговоре переносит
    `ext_rebind`."""
    return isinstance(pid, int) and pid > 0 and engine_registry.ext_engine_of(session_id) is not None


def rec_alive(rec) -> bool:
    """`is_alive` для строки подписки (на проект или на беседу): у внешнего Codex/Cursor — без `/proc`."""
    return is_alive(rec.session_pid, rec.session_proc_start, rec.session_host, session_id=rec.session_id)


def rec_session_alive(conn, rec) -> bool:
    """`session_alive` для строки подписки: у внешнего Codex/Cursor — без `/proc` (`ext_idle`)."""
    return session_alive(
        conn, rec.session_pid, rec.session_proc_start, rec.session_host, session_id=rec.session_id
    )


def rec_dead(conn, rec, now_ms: int, waiting) -> bool:
    """`_sub_dead` для мест, которые по ответу ЗАВЕРШАЮТ подписку (П2): живость — `session_alive` (у хаба — по
    базе). `waiting(rec.id)` — идёт ли ожидание (брошенная подписка OpenCode)."""
    if not rec_session_alive(conn, rec):
        return True
    return oc_stale(rec, now_ms, waiting=bool(waiting and waiting(rec.id)))


# --- живость процесса -----------------------------------------------------------------------------------


def parse_proc_stat(raw: bytes) -> int | None:
    """`starttime` (поле 22) из содержимого `/proc/<pid>/stat`. Имя процесса (поле 2, в скобках) может
    содержать пробелы и скобки — разбор после ПОСЛЕДНЕЙ `)`. Зомби (`Z`) и мёртвый (`X`) — `None`."""
    _head, sep, rest = raw.rpartition(b")")
    if not sep:
        return None
    fields = rest.split()
    # После `)`: fields[0] — поле 3 (state), поле 22 — fields[19].
    if len(fields) < 20 or fields[0] in (b"Z", b"X"):
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def read_proc_start(pid: int) -> int | None:
    """`starttime` живого процесса `pid` или `None` (процесса нет, зомби, не прочитать)."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    return parse_proc_stat(raw)


def is_alive(pid: int, proc_start: int, host: str | None = None, *, session_id: str | None = None) -> bool:
    """B2: процесс `pid` существует с тем же `starttime` (сравнение целыми). Ф41-2а (2.1): условный
    (отрицательный) pid сессии хаба — `proc_start == 0` и сессия в реестре незакрытых (`held`); её живость —
    запись, а не `/proc`. Ф33-1: сессия другой машины (`host` — машина строки подписки или тела запроса) —
    `session_host.state(...) == LIVE`; пусто или машина ядра — `/proc`, как до 33-1."""
    if held_mod.is_held(pid):
        return held_mod.registry_alive(pid, proc_start)
    # Ф33-1 (К9): сессия другой машины — по реестру в памяти ядра (контакт, ожидание, `gone`), не по `/proc`
    # машины ядра; «не в сети» здесь — не жива (отказы, `live`). Одна точка для всех вызовов `is_alive`.
    far = session_host_mod.state(host, pid, proc_start)
    if far is not None:
        return far == session_host_mod.LIVE
    if ext_idle(pid, session_id):
        # Ф99-0: строка внешнего Codex/Cursor — процесс (общий демон) не проверяется; брошенную снимает
        # `oc_stale`. Тело запроса (`session_id` не передаётся) по-прежнему сверяется с `/proc`.
        return True
    start = read_proc_start(pid)
    return start is not None and start == proc_start


# --- кто ждёт сейчас (в памяти ядра) --------------------------------------------------------------------


class SubWaiters:
    """Одно ожидание на подписку: новое вытесняет прежнее (прежнее видит `is_current() == False` и отвечает
    409 `superseded`). Общий объект публичного и внутреннего приложений (`waiting` в `/admin/subs`)."""

    def __init__(self) -> None:
        self._current: dict[int, object] = {}
        # Ф35-2 П12: версия протокола клиента идущего ожидания (сигналы `peer` — с 4-й).
        self._protocol: dict[int, int] = {}
        # Ф49-1: протокол последнего ожидания подписки — помнится и между ожиданиями (сигнал `card` ждёт в
        # базе, пока клиент перезапускает `hub sub wait` после выдачи).
        self._last_protocol: dict[int, int] = {}

    def enter(self, sub_id: int, protocol: int = 0) -> tuple[object, bool]:
        """Занять подписку; `(метка ожидания, было ли прежнее)`."""
        mark = object()
        had = sub_id in self._current
        self._current[sub_id] = mark
        self._protocol[sub_id] = protocol
        self._last_protocol[sub_id] = protocol
        return mark, had

    def is_current(self, sub_id: int, mark: object) -> bool:
        return self._current.get(sub_id) is mark

    def leave(self, sub_id: int, mark: object) -> None:
        if self._current.get(sub_id) is mark:
            del self._current[sub_id]
            self._protocol.pop(sub_id, None)

    def waiting(self, sub_id: int) -> bool:
        return sub_id in self._current

    def protocol(self, sub_id: int) -> int:
        """Версия протокола идущего ожидания подписки; ожидания нет — 0."""
        return self._protocol.get(sub_id, 0) if sub_id in self._current else 0

    def last_protocol(self, sub_id: int) -> int:
        """Версия протокола последнего ожидания подписки, идёт оно сейчас или нет; ожиданий с запуска ядра
        не было — 0."""
        return self._last_protocol.get(sub_id, 0)


# --- включение по проекту (B0) --------------------------------------------------------------------------


def _policy_dict(policy_json: str | None) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def subs_enabled(policy_json: str | None) -> bool:
    """B0: `subs: true` в `project.agent_policy_json`."""
    return _policy_dict(policy_json).get("subs") is True


def set_project_subs(conn, project_id: int, enabled: bool, *, now_ms: int) -> list[int] | None:
    """`PATCH /admin/projects/{id}` с `subs`: ключ `subs` политики проекта (прочие ключи не трогаются).
    Выключение завершает живые подписки проекта (`subs_off`). Возвращает id завершённых или `None`, если
    проекта нет (404)."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy_dict(row[0])
    policy["subs"] = bool(enabled)
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    ended: list[int] = []
    if not enabled:
        for (sub_id,) in conn.execute(
            "SELECT id FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL", (project_id,)
        ).fetchall():
            if end_sub(conn, sub_id, "subs_off", now_ms=now_ms, actor="admin"):
                ended.append(sub_id)
    return ended


def free_agents_enabled(conn) -> bool:
    """Н3: общий выключатель агентов вне проектов (нет ключа в `main.meta` — включено)."""
    return engine_mod._meta_get(conn, META_FREE_AGENTS) != "off"


def set_free_agents(conn, enabled: bool, *, now_ms: int) -> list[int]:
    """`hub sub free on|off`: выключение завершает живые подписки проекта 0 (`subs_off`) и отказывает новым.
    Возвращает id завершённых."""
    engine_mod._meta_set(conn, META_FREE_AGENTS, "on" if enabled else "off")
    ended: list[int] = []
    if not enabled:
        for (sub_id,) in conn.execute(
            "SELECT id FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL", (NO_PROJECT_ID,)
        ).fetchall():
            if end_sub(conn, sub_id, "subs_off", now_ms=now_ms, actor="admin"):
                ended.append(sub_id)
    return ended


# --- Ф48-4: пачка сообщений агентов ---------------------------------------------------------------------

AGENT_BATCH_KEY = "agent_batch_min"  # ключ политики проекта: умолчание интервала пачки, минуты
AGENT_BATCH_MAX = 60


def agent_batch_valid(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= AGENT_BATCH_MAX


def check_agent_batch(value) -> None:
    """`agent_batch_min` запроса — целое 0..`AGENT_BATCH_MAX` (минуты; 0 — сразу) или `None`."""
    if value is not None and not agent_batch_valid(value):
        raise SubError(422, f"agent_batch_min: 0–{AGENT_BATCH_MAX} минут")


def project_agent_batch(policy_json: str | None) -> int:
    """Умолчание проекта: ключ `agent_batch_min` политики; нет ключа или кривой — 0 (сразу)."""
    value = _policy_dict(policy_json).get(AGENT_BATCH_KEY)
    return value if agent_batch_valid(value) else 0


def agent_batch_row(conn, sub_id: int) -> tuple[int | None, int | None]:
    """Столбцы пачки сообщений агентов подписки: (свой интервал `agent_batch_min`, срок `agent_batch_ts`).
    Читаются отдельным запросом, а не в `SubRec`: значение всегда свежее (срок двигают отправка и выдача).
    `_SUB_COLUMNS` читается только на мигрированной базе (с 0100 в нём `session_host`)."""
    row = conn.execute(
        "SELECT agent_batch_min, agent_batch_ts FROM main.session_sub WHERE id = ?", (sub_id,)
    ).fetchone()
    return (row[0], row[1]) if row is not None else (None, None)


def agent_batch_of(conn, rec) -> int:
    """Действующий интервал пачки сообщений агентов подписки `rec`, минуты: свой (`agent_batch_min`), иначе
    умолчание проекта; у агента вне проектов — 0. 0 — сообщения агентов сигналят сразу, как до Ф48-4."""
    own = agent_batch_row(conn, rec.id)[0]
    if agent_batch_valid(own):
        return own
    if rec.project_id == NO_PROJECT_ID:
        return 0
    row = conn.execute(
        "SELECT agent_policy_json FROM main.project WHERE id = ?", (rec.project_id,)
    ).fetchone()
    return project_agent_batch(row[0]) if row is not None else 0


def set_project_agent_batch(conn, project_id: int, minutes: int) -> int | None:
    """`hub project set P --agent-batch-min M` и поле интерфейса — ключ `agent_batch_min` политики проекта
    (прочие ключи не трогаются; 0 — ключ снят). Сроки пачки живых подписок проекта без своего интервала
    обнуляются: со старым умолчанием срок не имеет смысла, ждущее уйдёт ближайшей выдачей. Нет проекта —
    `None`."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy_dict(row[0])
    if minutes:
        policy[AGENT_BATCH_KEY] = minutes
    else:
        policy.pop(AGENT_BATCH_KEY, None)
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    conn.execute(
        "UPDATE main.session_sub SET agent_batch_ts = NULL WHERE project_id = ? AND ended_ts IS NULL "
        "AND agent_batch_min IS NULL AND agent_batch_ts IS NOT NULL",
        (project_id,),
    )
    return minutes


def _set_agent_batch(conn, sub: SubRec, minutes: int, *, now_ms: int, audit: dict) -> None:
    """Свой интервал пачки сообщений агентов живой подписки на месте (зовётся, только если значение
    изменилось): срок пачки обнуляется — ждущие сообщения уйдут ближайшей выдачей. Аудит `sub_mode`."""
    conn.execute(
        "UPDATE main.session_sub SET agent_batch_min = ?, agent_batch_ts = NULL WHERE id = ?",
        (minutes, sub.id),
    )
    write_audit(
        conn,
        now_ms=now_ms,
        action="sub_mode",
        result="ok",
        detail={"sub_id": sub.id, "agent_batch_min": minutes},
        **audit,
    )


def _agent_batch_prior(
    conn, hub, sub_id: int, project_id: int, role_key: str | None, role_no: int, own: dict
) -> SubRec | None:
    """Откуда новая подписка берёт свой интервал и срок пачки сообщений агентов (переезд с подпиской линии:
    resume, вторая подписка роли, «Забрать в хаб», передача дел): прежняя подписка той же сессии хаба, снятая
    сменой токена; иначе самая свежая завершённая подписка ЛИНИИ роли; у подписки без роли — прежняя
    безрольная этого же процесса или разговора."""
    if hub is not None:
        prior = _same_held_prior(conn, hub, sub_id)
        if prior is not None:
            return prior
    if role_key is not None:
        # Как `_role_source_of`: у агента вне проектов линия роли — только из той же папки сессии (Н2).
        cwd = own["cwd"] if project_id == NO_PROJECT_ID else None
        return _role_source(conn, project_id, role_key, role_no, cwd=cwd)
    return _own_prior(conn, project_id, role_key=None, **own)[0]


# --- строки подписок ------------------------------------------------------------------------------------


def get_sub(conn, sub_id: int) -> SubRec | None:
    row = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE id = ?",  # noqa: S608 — список колонок константа
        (sub_id,),
    ).fetchone()
    return SubRec(*row) if row is not None else None


def _pending_counts(conn, sub_ids: list[int]) -> dict[int, int]:
    if not sub_ids:
        return {}
    placeholders = ",".join("?" for _ in sub_ids)
    rows = conn.execute(
        f"SELECT sub_id, COUNT(*) FROM main.session_signal WHERE status = 'pending' "  # noqa: S608
        f"AND sub_id IN ({placeholders}) GROUP BY sub_id",
        sub_ids,
    ).fetchall()
    counts = dict(rows)
    # Ф14-5 П20: сигналы сообщений подписки в режиме `messages` — в `session_chan_signal`.
    for sub_id, count in conn.execute(
        f"SELECT project_sub_id, COUNT(*) FROM main.session_chan_signal WHERE status = 'pending' "  # noqa: S608
        f"AND project_sub_id IN ({placeholders}) GROUP BY project_sub_id",
        sub_ids,
    ).fetchall():
        counts[sub_id] = counts.get(sub_id, 0) + count
    return counts


def sub_json(
    rec: SubRec,
    *,
    now_ms: int,
    waiting: bool,
    pending: int,
    admin: bool = False,
    me: tuple[int, int, str] | None = None,
    with_cache: bool = False,
    hub: held_mod.HubSession | None = None,
    agent_batch: int = 0,
    machine: dict | None = None,
) -> dict:
    """SubRow контракта B5 (`admin` — плюс `session_id8`, `session_cwd`) и режим (Ф14-5 П20). Ф41-2а (3.6,
    П1): `hub` — запись сессии хаба этой подписки: `held`, `hub_session_id`, `state`; движок и срок кэша — из
    записи (условная метка `hub:<id>` движка не называет). Ф77-1 (A5): `machine` — метка машины сессии
    (`machines.Labels.of_rec`)."""
    result = session_json(
        rec, now_ms=now_ms, waiting=waiting, pending=pending, admin=admin, me=me, machine=machine
    )
    result["project_id"] = rec.project_id
    result["mode"] = mode_of(rec)  # Ф35-1: `direct` — по флагу
    result["msg_filter"] = rec.msg_filter  # Ф20-1 П1
    # Ф25-1 A3: сессии видны свои списки; Ф36-1 A4: `batch_min` — действующий интервал пачки.
    result["keywords"] = keywords_mod.public_spec(rec.keywords_json)
    # Ф67-1: действующий интервал пачки сигналов подписки (с ключами и без; 0 — сразу; у `direct` — 0).
    result["batch_min"] = 0 if rec.direct else keywords_mod.batch_minutes(rec.keywords_json)
    result["role"] = rec.role  # Ф26-1
    result["role_no"] = rec.role_no  # Ф35-1
    result["accounts"] = accounts_of(rec)  # Ф38-1 п.10, П1
    if agent_batch > 0:
        # Ф48-4 п.5: действующий интервал пачки сообщений агентов (свой или умолчание проекта; считает
        # вызывающий). Без пачки ключа нет — строка подписки прежняя.
        result["agent_batch_min"] = agent_batch
    result["engine"] = rec_engine(rec)
    engine_sid: str | None = rec.session_id
    if hub is not None:
        result.update(held_mod.sub_fields(hub))
        # Ф41-4 П11: ожидание подписки сессии хаба держит хост своим длинным ожиданием, а не `hub sub wait`.
        result["listening"] = bool(result["live"]) and held_mod.host_listening(now_ms)
        result["engine"] = hub.engine
        engine_sid = hub.engine_session_id
    if with_cache and result["engine"] == "claude":
        # Ф41-1: срок кэша и контекст — только в публичной выдаче UI, у сессий Claude Code. Ф33-1: у сессии
        # другой машины стенограммы на машине ядра нет — числа из её пульса (`POST /subs/beat`).
        far, cache = session_host_mod.cache_of(rec.session_host, rec.session_pid, rec.session_proc_start)
        if far:
            result["cache"] = cache
        else:
            result["cache"] = transcript_mod.state_for(engine_sid) if engine_sid else None
    if rec.project_id == NO_PROJECT_ID:
        result["cwd"] = rec.session_cwd  # решение 14: у агента вне проектов в реестре — папка сессии
    return result


def session_json(
    rec,
    *,
    now_ms: int,
    waiting: bool,
    pending: int,
    admin: bool = False,
    me: tuple[int, int, str] | None = None,
    machine: dict | None = None,
) -> dict:
    """Общая часть строки подписки (на проект — `SubRec`, на беседу — `chan_subs.ChanSubRec`): `live`/
    `listening` вычисляются при чтении (`GET` ничего не пишет). Ф16-4 (П17): `me` — своя сессия запроса
    `(pid, proc_start, session_id)`: в строке поле `mine` (без `me` поля нет, как раньше); П1 — брошенная
    подписка OpenCode не `live`."""
    live = rec.ended_ts is None and rec_alive(rec) and not oc_stale(rec, now_ms, waiting=bool(waiting))
    waiting = bool(waiting) and rec.ended_ts is None
    recent_wait = rec.last_wait_ts is not None and 0 <= now_ms - rec.last_wait_ts <= SUB_LISTEN_GRACE_S * 1000
    result = {
        "id": rec.id,
        "session_name": rec.session_name,
        "session_pid": rec.session_pid,
        "created_ts": rec.created_ts,
        "last_wait_ts": rec.last_wait_ts,
        "last_delivery_ts": rec.last_delivery_ts,
        "live": live,
        "listening": live and (waiting or recent_wait),
        "waiting": waiting,
        "ended_ts": rec.ended_ts,
        "end_reason": rec.end_reason,
        "pending": pending,
        "skey": skey(rec),  # Ф26-1 П4
    }
    if admin:
        result["session_id8"] = rec.session_id[:SESSION_ID8]
        result["session_cwd"] = rec.session_cwd
    if me is not None:
        result["mine"] = same_session(rec, *me)
    if machine is not None:
        # Ф77-1 (A5): машина сессии — `session_host`, а пустой и сессия хаба — машина ядра.
        result["machine"] = machine
    return result


def one_json(conn, rec: SubRec, *, now_ms: int, waiters: SubWaiters | None, admin: bool = False) -> dict:
    pending = _pending_counts(conn, [rec.id]).get(rec.id, 0)
    waiting = waiters.waiting(rec.id) if waiters is not None else False
    return sub_json(
        rec,
        now_ms=now_ms,
        waiting=waiting,
        pending=pending,
        admin=admin,
        hub=held_mod.of_rec(conn, rec),
        agent_batch=agent_batch_of(conn, rec),
        machine=machines_mod.of_rec(conn, rec, now_ms=now_ms),
    )


def list_subs(
    conn,
    *,
    project_ids: frozenset[int] | None,
    live_only: bool,
    now_ms: int,
    waiters: SubWaiters | None,
    admin: bool = False,
    me: tuple[int, int, str] | None = None,
    with_cache: bool = False,
    ended_ms: int = ENDED_RECENT_MS,
) -> list[dict]:
    """`GET /subs` (видимость по проекту подписки) и `GET /admin/subs`. `live_only` — незавершённые
    (`ended_ts IS NULL`; живость процесса — поле `live`), иначе ещё и завершённые за `ended_ms` (сутки). `me`
    — поле `mine` (П17)."""
    where = ["1=1"]
    params: list = []
    if live_only:
        where.append("ended_ts IS NULL")
    else:
        where.append("(ended_ts IS NULL OR ended_ts >= ?)")
        params.append(now_ms - ended_ms)
    if project_ids is not None:
        ids = sorted(project_ids)
        if not ids:
            return []
        where.append(f"project_id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE {' AND '.join(where)} "  # noqa: S608 — фрагменты выше
        "ORDER BY id DESC",
        params,
    ).fetchall()
    recs = [SubRec(*row) for row in rows]
    pending = _pending_counts(conn, [r.id for r in recs])
    machines = machines_mod.Labels(conn, now_ms=now_ms)
    return [
        sub_json(
            rec,
            now_ms=now_ms,
            waiting=waiters.waiting(rec.id) if waiters is not None else False,
            pending=pending.get(rec.id, 0),
            admin=admin,
            me=me,
            with_cache=with_cache,
            hub=held_mod.of_rec(conn, rec),
            agent_batch=agent_batch_of(conn, rec),
            machine=machines.of_rec(rec),
        )
        for rec in recs
    ]


# --- завершение ----------------------------------------------------------------------------------------


def end_sub(
    conn,
    sub_id: int,
    reason: str,
    *,
    now_ms: int,
    actor: str,
    api_token_id: int | None = None,
    client_name: str | None = None,
    request_id: str | None = None,
) -> bool:
    """B2: завершить живую подписку (`ended_ts`, `end_reason`), её `pending` -> `ended`, аудит `sub_end`.
    Уже завершённая — `False`, ничего не пишется."""
    if reason not in END_REASONS:
        raise ValueError(f"неизвестная причина завершения {reason!r}")
    cur = conn.execute(
        "UPDATE main.session_sub SET ended_ts = ?, end_reason = ? WHERE id = ? AND ended_ts IS NULL",
        (now_ms, reason, sub_id),
    )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "UPDATE main.session_signal SET status = 'ended', updated_ts = ? "
        "WHERE sub_id = ? AND status = 'pending'",
        (now_ms, sub_id),
    )
    conn.execute(
        "UPDATE main.session_chan_signal SET status = 'ended', updated_ts = ? "
        "WHERE project_sub_id = ? AND status = 'pending'",
        (now_ms, sub_id),
    )
    # Ф35-2 (решение 5, Н4): ждущие сообщения агентов, привязанные к этой подписке, — `lost` (импорт здесь:
    # `peer_msgs` сам импортирует этот модуль).
    from . import peer_msgs as peer_msgs_mod

    peer_msgs_mod.on_sub_end(conn, sub_id, now_ms=now_ms)
    project_id, session_pid = conn.execute(
        "SELECT project_id, session_pid FROM main.session_sub WHERE id = ?", (sub_id,)
    ).fetchone()
    touch_held(conn, session_pid, now_ms)
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or uuid.uuid4().hex,
        action="sub_end",
        result=reason,
        detail={"sub_id": sub_id, "project_id": project_id},
    )
    return True


def touch_held(conn, session_pid: int, now_ms: int) -> None:
    """Ф41-4 (2.1, П1): подписка сессии ХАБА появилась, изменилась или кончилась — её запись получает новый
    `seq`: окно видит `sub` и `sub_stale`, хост — что подписку пора оформить заново. У внешней сессии
    (настоящий pid) ничего не пишется."""
    hub_id = held_mod.held_id(session_pid)
    if hub_id is not None:
        conn.execute(
            "UPDATE main.hub_session SET seq = ?, updated_ts = ? WHERE id = ? AND closed_ts IS NULL",
            (next_seq(conn), now_ms, hub_id),
        )


def end_reissued_token(conn, token_id: int, *, now_ms: int) -> int:
    """Ф92-1 (B3): токен `token_id` перевыпущен (отозван с `replaced_by`) — его живые подписки на проект и на
    беседы завершаются `token_reissued` (в той же транзакции, что отзыв; чистка иначе сняла бы их
    `token_revoked` — насовсем). Возвращает, сколько снято."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    ended = 0
    for (sub_id,) in conn.execute(
        "SELECT id FROM main.session_sub WHERE api_token_id = ? AND ended_ts IS NULL ORDER BY id", (token_id,)
    ).fetchall():
        ended += end_sub(conn, sub_id, TOKEN_REISSUED_END_REASON, now_ms=now_ms, actor="admin")
    for (chan_id,) in conn.execute(
        "SELECT id FROM main.session_chan_sub WHERE api_token_id = ? AND ended_ts IS NULL ORDER BY id",
        (token_id,),
    ).fetchall():
        ended += chan_subs.end(conn, chan_id, TOKEN_REISSUED_END_REASON, now_ms=now_ms, actor="admin")
    return ended


def cleanup(conn, *, now_ms: int, waiting=None) -> list[tuple[int, str]]:
    """Фоновая задача ядра (B2): живые подписки, чей токен отозван или истёк, — `token_revoked`; чей процесс
    умер или подменён — `session_gone`. Возвращает `(sub_id, причина)` завершённых. Ф16-4 (П1): брошенная
    подписка OpenCode (`oc_stale`; `waiting(sub_id)` — идёт ли ожидание сейчас) — тоже `session_gone`."""
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE ended_ts IS NULL ORDER BY id"  # noqa: S608
    ).fetchall()
    ended: list[tuple[int, str]] = []
    for row in rows:
        rec = SubRec(*row)
        sub_id = rec.id
        token = conn.execute(
            "SELECT id, revoked, expires_ts FROM main.api_token WHERE id = ?", (rec.api_token_id,)
        ).fetchone()
        if token is None or token[1] or (token[2] is not None and token[2] <= now_ms):
            reason = "token_revoked"
        elif not rec_session_alive(conn, rec):
            reason = "session_gone"  # Ф41-2а П2: сессия хаба — по базе (пустой реестр ничего не завершает)
        elif oc_stale(rec, now_ms, waiting=bool(waiting and waiting(sub_id))):
            reason = "session_gone"
        else:
            refresh_local_seen(conn, "session_sub", rec, now_ms=now_ms)
            continue
        if end_sub(conn, sub_id, reason, now_ms=now_ms, actor="core"):
            ended.append((sub_id, reason))
    return ended


# --- Ф33-1: пульс сессии другой машины ------------------------------------------------------------------


def _live_keys(
    conn, host: str, token_id: int, where: str = "", args: tuple = ()
) -> list[tuple[int, int, str]]:
    """Живые личности `(pid, proc_start, session_id)` машины `host` под токеном `token_id` — по строкам
    подписок на проект и на беседы (с доп. условием `where`)."""
    rows: set[tuple[int, int, str]] = set()
    for table in ("session_sub", "session_chan_sub"):
        rows.update(
            conn.execute(
                "SELECT session_pid, session_proc_start, session_id FROM main."  # noqa: S608 — имя таблицы константа
                f"{table} WHERE ended_ts IS NULL AND session_host = ? AND api_token_id = ?{where}",
                (host, token_id, *args),
            ).fetchall()
        )
    return sorted(rows)


def end_process(
    conn, host: str, pid: int, proc_start: int, reason: str, *, token_id: int, now_ms: int, audit: dict
) -> int:
    """Ф33-1 (`gone`): снять живые подписки процесса машины `host` — на проект и на беседы, все разговоры,
    ТОЛЬКО строки этой машины под токеном `token_id` (ревью Б1: чужой токен и строки без машины не трогаются).
    Если у личности не осталось живых строк ни под каким токеном — она `gone` и в реестре. Возвращает, сколько
    снято."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    ended = 0
    for (sub_id,) in conn.execute(
        "SELECT id FROM main.session_sub WHERE ended_ts IS NULL AND session_host = ? AND api_token_id = ? "
        "AND session_pid = ? AND session_proc_start = ?",
        (host, token_id, pid, proc_start),
    ).fetchall():
        ended += end_sub(conn, sub_id, reason, now_ms=now_ms, **audit)
    for rec in chan_subs.live_of_process(conn, pid, proc_start, token_id):
        if rec.session_host == host:
            ended += chan_subs.end(conn, rec.id, reason, now_ms=now_ms, **audit)
    left = any(
        conn.execute(
            f"SELECT 1 FROM main.{table} WHERE ended_ts IS NULL AND session_host = ? "  # noqa: S608 — константа
            "AND session_pid = ? AND session_proc_start = ? LIMIT 1",
            (host, pid, proc_start),
        ).fetchone()
        for table in ("session_sub", "session_chan_sub")
    )
    if not left:
        session_host_mod.current().mark_gone(host, pid, proc_start)
    return ended


def end_rebooted(conn, *, host: str, boot: str, token_id: int, now_ms: int, audit: dict) -> int:
    """Ф33-1 (boot): машина `host` прислала загрузку `boot` — подписки её прежних загрузок (другой НЕПУСТОЙ
    `session_boot`) под ЭТИМ токеном снимаются `session_gone`, как по обычному `gone` (ревью Б1: только строки
    этой машины и токена; остальные — по окну 12 ч). Возвращает, сколько снято."""
    ended = 0
    keys = _live_keys(conn, host, token_id, " AND session_boot IS NOT NULL AND session_boot != ?", (boot,))
    for pid, proc_start in sorted({(pid, proc_start) for pid, proc_start, _sid in keys}):
        ended += end_process(
            conn, host, pid, proc_start, "session_gone", token_id=token_id, now_ms=now_ms, audit=audit
        )
    return ended


def refresh_local_seen(conn, table: str, rec, *, now_ms: int) -> None:
    """Правка финального ревью Ф33 (17.10): чистка увидела живой процесс сессии СВОЕЙ машины (строка с
    `session_host` == машина ядра) — это её контакт; пишется в `session_seen_ts` не чаще раза в
    `session_host.SAVE_MS`. После переезда ядра эта машина становится чужой, и засев реестра считает окно 12 ч
    от этой отметки, а не от `created_ts` (иначе первая же чистка на новой главной сняла бы подписки
    ноутбука).
    Сессии другой машины и строки без машины не трогаются (их контакт — `mark_seen`)."""
    if not session_host_mod.current().local(rec.session_host, rec.session_pid):
        return
    conn.execute(
        f"UPDATE main.{table} SET session_seen_ts = ? "  # noqa: S608 — имя таблицы из двух констант
        "WHERE id = ? AND ended_ts IS NULL AND (session_seen_ts IS NULL OR session_seen_ts <= ?)",
        (now_ms, rec.id, now_ms - session_host_mod.SAVE_MS),
    )


def mark_seen(conn, host: str, pid: int, proc_start: int, *, token_id: int, seen_ms: int) -> None:
    """Ф33-1 (ревью В1): последний контакт личности другой машины — в её живые строки под этим токеном; после
    рестарта ядра окно 12 ч считается от него (`session_host.Registry.seed`)."""
    for table in ("session_sub", "session_chan_sub"):
        conn.execute(
            f"UPDATE main.{table} SET session_seen_ts = MAX(COALESCE(session_seen_ts, 0), ?) "  # noqa: S608
            "WHERE ended_ts IS NULL AND session_host = ? AND api_token_id = ? AND session_pid = ? "
            "AND session_proc_start = ?",
            (seen_ms, host, token_id, pid, proc_start),
        )


def beat(conn, *, token_id: int, session: dict, now_ms: int, audit: dict) -> tuple[bool, list[dict]]:
    """Ф33-1 (`POST /subs/beat` удалённой сессии): у Claude Code подписки прежнего разговора того же процесса,
    машины и токена — `conversation_changed` (`/clear`, как у `hub sub wait`); у OpenCode на одном процессе
    много живых сессий — не трогаются. Итог — есть ли у ЭТОГО разговора под этим токеном живые подписки на
    проект или на беседы (слать ли пульс дальше), и соседи: другие живые личности той же машины под тем же
    токеном `{pid, proc_start, session_id}` — клиент сверит их со своим `/proc` (ревью П1)."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    host = session["host"]
    if not many_convs(session_id) and not held_mod.is_held(pid):
        for (sub_id,) in conn.execute(
            "SELECT id FROM main.session_sub WHERE ended_ts IS NULL AND api_token_id = ? "
            "AND session_host = ? AND session_pid = ? AND session_proc_start = ? AND session_id != ?",
            (token_id, host, pid, proc_start, session_id),
        ).fetchall():
            end_sub(conn, sub_id, "conversation_changed", now_ms=now_ms, **audit)
        for rec in chan_subs.live_of_process(conn, pid, proc_start, token_id):
            if rec.session_host == host and rec.session_id != session_id:
                chan_subs.end(conn, rec.id, "conversation_changed", now_ms=now_ms, **audit)
    project = conn.execute(
        "SELECT 1 FROM main.session_sub WHERE ended_ts IS NULL AND api_token_id = ? AND session_host = ? "
        "AND session_pid = ? AND session_proc_start = ? AND session_id = ? LIMIT 1",
        (token_id, host, pid, proc_start, session_id),
    ).fetchone()
    mine = [
        r
        for r in chan_subs.session_subs(conn, token_id, pid, proc_start, session_id)
        if r.session_host == host
    ]
    neighbours: dict[tuple[int, int], dict] = {}
    for n_pid, n_start, n_sid in _live_keys(conn, host, token_id):
        if (n_pid, n_start) != (pid, proc_start):
            neighbours.setdefault(
                (n_pid, n_start), {"pid": n_pid, "proc_start": n_start, "session_id": n_sid}
            )
    return project is not None or bool(mine), list(neighbours.values())


def ext_rebind(conn, *, token_id: int, session: dict, now_ms: int, audit: dict) -> int:
    """Ф99-0 (итоги проб, долг 23.10 «Codex теряет подписку между ходами»): разговор внешнего Codex/Cursor
    пришёл с другим процессом (`codex app-server` перезапустился — pid и `starttime` новые) — его живые
    подписки на проект и на беседы под ЭТИМ токеном и на ЭТОЙ машине переезжают на новый процесс на месте:
    номер строки, роль, номер в роли, ключи и ждущие сигналы остаются, «роль 2» не заводится. Личность внешней
    сессии — разговор (`<движок>:<id>`), процесс — только адрес. Строка, на чьё место (проект или беседа) у
    нового процесса уже есть своя живая, не трогается (её снимет срок без ожидания). Не внешняя сессия —
    0. Аудит `sub_rebind` (без текстов). Возвращает, сколько строк переехало."""
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    if not ext_idle(pid, session_id) or not isinstance(proc_start, int):
        return 0
    host, boot = session.get("host"), session.get("boot")
    moved: list[tuple[str, int]] = []
    for table, place in (("session_sub", "project_id"), ("session_chan_sub", "channel_id")):
        rows = conn.execute(
            f"SELECT id, {place} FROM main.{table} WHERE ended_ts IS NULL AND api_token_id = ? "  # noqa: S608
            "AND session_id = ? AND session_host IS ? AND NOT (session_pid = ? AND session_proc_start = ?) "
            "ORDER BY id",
            (token_id, session_id, host, pid, proc_start),
        ).fetchall()
        for row_id, where in rows:
            clash = conn.execute(
                f"SELECT 1 FROM main.{table} WHERE ended_ts IS NULL AND {place} = ? AND api_token_id = ? "  # noqa: S608
                "AND session_pid = ? AND session_proc_start = ? AND session_id = ? LIMIT 1",
                (where, token_id, pid, proc_start, session_id),
            ).fetchone()
            if clash is not None:
                continue
            conn.execute(
                f"UPDATE main.{table} SET session_pid = ?, session_proc_start = ?, "  # noqa: S608
                "session_boot = COALESCE(?, session_boot) WHERE id = ?",
                (pid, proc_start, boot, row_id),
            )
            moved.append((table, row_id))
    if moved:
        write_audit(
            conn,
            now_ms=now_ms,
            action="sub_rebind",
            result="ok",
            detail={
                "engine": engine_registry.ext_engine_of(session_id),
                "subs": [i for t, i in moved if t == "session_sub"],
                "chan_subs": [i for t, i in moved if t == "session_chan_sub"],
            },
            **audit,
        )
    return len(moved)


# --- подписка -------------------------------------------------------------------------------------------


def _realpath(path: str) -> str:
    return os.path.realpath(path)


def subscribe(
    conn,
    *,
    principal,
    session: dict,
    project_id: int | None,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    mode: str | None = None,
    msg_filter: str | None = None,
    keywords: list | None = None,
    exclude: list | None = None,
    role: str | None = None,
    number: int | None = None,
    waiting=None,
    batch_min: int | None = None,
    accounts: list | None = None,
    accounts_except: list | None = None,
    agent_batch_min: int | None = None,
) -> Subscribed:
    """`POST /subs` (B4) одной транзакцией: проверки сессии и проекта, разбор живых подписок проекта, вставка,
    добор (B3), аудит `sub_create`. Права токена (скоуп, вид, имя) проверяет маршрут. Отказ — `SubError`.

    Ф14-5 П20: `mode` — `cards`|`messages`; `None` — новая подписка `cards`, своя живая — режим не меняется.
    Своя живая с другим `mode` — режим меняется на месте (аудит `sub_mode`), `pending` прежнего режима —
    `ended`.

    Ф20-1 П1: `msg_filter` — `all`|`personal`, только вместе с `mode`; при `cards` — только `all` (иначе 422).
    `mode` без фильтра — `all`. Смена одного фильтра своей живой — на месте, `pending` выдаётся как есть.

    Ф25-1 A3 + П2, П5: `keywords`/`exclude` — только с `mode="messages"` + `msg_filter="personal"` (иначе
    422); `keywords=[]` — ключи сняты. Смена одних ключей своей живой — на месте (аудит `sub_mode`).

    Ф26-1 (A2, П2, П3, П6, П8): живая подписка на проект — одна на СЕССИЮ (токен, pid, starttime,
    `session_id`); чужая живая сессия — рядом, до `MAX_PROJECT_SUBS`. `role` — метка подписки (`""` — снять).
    НОВАЯ подписка без `mode` (и «лично мне» без `keywords`) наследует из своей «линии» (`_inherit_source`).
    `waiting(sub_id)` — идёт ли ожидание подписки (брошенная подписка OpenCode — мёртвая, П6).

    Ф35-1 (решения 2–4, 14; П1, П3, П11; Н2, Н3): адрес агента — линия `(проект, роль, номер)`, живая — одна.
    `number` — номер в роли: задан — он (линию держит живая чужая — 409); нет — номер прежней подписки этой
    сессии с той же ролью (её линия занята живой чужой — 409, не переезд), иначе наименьший свободный. Номер
    своей живой на месте не меняется (409); смена роли на месте — номер заданный или наименьший свободный.
    У подписки с ролью режим и ключи наследуются только из своей линии. `mode="direct"` — «только
    адресованное агенту» (флаг `direct`; карточек, сообщений проекта и добора нет). Сессия не в рабочей
    папке проекта (и не внутри неё) — агент вне проектов (`NO_PROJECT_ID`): только `direct`, только токен
    без списка проектов, пока включён `free_agents`.

    Ф36-1 A3 + П4: `batch_min` — интервал пачки сигналов «по ключу» (минуты, 0 — сразу), хранится внутри
    `keywords_json`. Задан с `keywords` — пишется; `keywords` без него — остаётся интервал прежнего
    `keywords_json` (своей живой или источника наследования); без `keywords` при ключах (своих или
    унаследованных) — меняется только интервал, в том числе у своей живой без `mode`; ключей в итоге нет —
    422. П1: новая подписка `messages` забирает ждущие сигналы завершённого источника наследования
    (`chan_subs.carry_project_signals`).

    Ф38-1 (п.1, 7, 8; П1–П6): `accounts` («только эти аккаунты») или `accounts_except` («все, кроме этих») —
    фильтр подписки на проект по аккаунтам бесед (вместе — 422; пустой список в любом — снять). Итоговый режим
    `direct` с непустым фильтром в запросе — 422. Новая подписка без них наследует фильтр как режим: у
    источника режима, иначе — у прежней подписки своей линии (роль, процесс, разговор). У своей живой фильтр
    меняется на месте ДО смены режима (аудит `sub_mode`): ждущие сигналы бесед вне нового фильтра — `ended`,
    у `cards` — добор карточек вновь слышимых аккаунтов.

    Ф48-4: `agent_batch_min` — свой интервал пачки сообщений агентов (минуты 0–60; 0 — сразу), в любом
    режиме. У своей живой меняется на месте (срок пачки обнуляется); новая подписка без него берёт интервал
    и срок прежней подписки своей линии (`_agent_batch_prior`), иначе действует умолчание проекта."""
    try:
        return _subscribe(
            conn,
            principal=principal,
            session=session,
            project_id=project_id,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            mode=mode,
            msg_filter=msg_filter,
            keywords=keywords,
            exclude=exclude,
            role=role,
            number=number,
            waiting=waiting,
            batch_min=batch_min,
            accounts=accounts,
            accounts_except=accounts_except,
            agent_batch_min=agent_batch_min,
        )
    except sqlite3.IntegrityError as exc:
        # П8: гонка с другой подпиской той же сессии или роли (уникальные индексы 0040) — отказ, не 500.
        raise SubError(409, "подписка этой сессии или роль в проекте уже заняты — повтори подписку") from exc


def _subscribe(
    conn,
    *,
    principal,
    session: dict,
    project_id: int | None,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    mode: str | None,
    msg_filter: str | None,
    keywords: list | None,
    exclude: list | None,
    role: str | None,
    number: int | None,
    waiting,
    batch_min: int | None,
    accounts: list | None,
    accounts_except: list | None,
    agent_batch_min: int | None = None,
) -> Subscribed:
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    new_keywords = _new_keywords(mode, msg_filter, keywords, exclude)
    # Ф38-1: фильтр запроса — `acc_given` (задан ли, в том числе пустым списком), `acc_json` (`None` — снять).
    acc_given, acc_json = _new_accounts(conn, accounts, accounts_except)
    check_batch_min(batch_min)
    check_agent_batch(agent_batch_min)
    if mode == DIRECT and batch_min is not None:
        raise SubError(422, BATCH_MIN_DIRECT)
    if mode is not None and mode not in MODES:
        raise SubError(422, "mode: cards, messages или direct")
    if msg_filter is not None and msg_filter not in MSG_FILTERS:
        raise SubError(422, "msg_filter: all или personal")
    if msg_filter is not None and mode is None:
        raise SubError(422, "msg_filter — только вместе с mode")
    if mode == DIRECT and (msg_filter is not None or exclude is not None):
        raise SubError(422, "mode=direct — без msg_filter, keywords и exclude")
    if mode == "cards" and msg_filter not in (None, "all"):
        raise SubError(422, "msg_filter personal — только с mode=messages")
    if number is not None and not (isinstance(number, int) and 1 <= number <= ROLE_NO_MAX):
        raise SubError(422, f"number: 1–{ROLE_NO_MAX}")
    role_given = role is not None
    new_role, new_role_key = role_form(role) if role_given else (None, None)
    new_filter = (msg_filter or "all") if mode is not None else None
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    kind = session.get("kind")
    # Ф41-2а (3.2, П2в, П4): сессия хаба — условная тройка согласована, запись есть и не закрыта (по базе, не
    # по реестру) — до любых записей.
    hub: held_mod.HubSession | None = None
    if kind == held_mod.KIND or held_mod.is_held(pid):
        hub = hub_session_of(conn, session)
    elif not is_alive(pid, proc_start, session.get("host")):
        raise SubError(409, "сессия не найдена")
    if kind not in SESSION_KINDS:
        raise SubError(403, "подписываться может только интерактивная сессия")
    cwd = _realpath(session["cwd"])
    projects = [
        (pid_, _realpath(workdir), policy_json)
        for pid_, workdir, policy_json in conn.execute(
            "SELECT id, workdir, agent_policy_json FROM main.project WHERE workdir IS NOT NULL ORDER BY id"
        ).fetchall()
    ]
    matching = [(pid_, policy_json) for pid_, workdir, policy_json in projects if workdir == cwd]
    free = False
    if hub is not None:
        # П5: проект, роль и номер — из записи сессии хаба (не поиск по `cwd`: две папки одного проекта и
        # смена `workdir` проекта сессию не ломают). Роль задаёт владелец при создании: сессия не может
        # переназваться и получить чужое «без подтверждения» (Ф37-1); наследование роли выключено.
        target = _hub_target(conn, hub, project_id, principal)
        if (role_given and new_role_key != hub.role_key) or (number is not None and number != hub.role_no):
            raise SubError(422, "роль и номер сессии хаба задаёт владелец при создании — в запросе их не менять")
        role_given, new_role, new_role_key, number = True, hub.role, hub.role_key, hub.role_no
    elif not matching:
        # Ф35-1 (решение 14, Н3): папка не проекта — агент вне проектов; папка ВНУТРИ рабочей папки проекта
        # (подпапка, worktree) им не становится (иначе обход `subs off` проекта), явный `project_id` — тоже.
        inside = any(cwd.startswith(workdir.rstrip(os.sep) + os.sep) for _id, workdir, _policy in projects)
        if inside or project_id is not None or principal.project_ids is not None:
            raise SubError(403, NOT_IN_PROJECT)
        if not free_agents_enabled(conn):
            raise SubError(403, "агенты вне проектов выключены — hub sub free on")
        free = True
        target = NO_PROJECT_ID
    else:
        if project_id is not None:
            matching = [m for m in matching if m[0] == project_id]
            if not matching:
                raise SubError(403, "проект не совпадает с рабочей папкой сессии")
        if principal.project_ids is not None:
            in_scope = [m for m in matching if m[0] in principal.project_ids]
            if not in_scope:
                raise SubError(403, "проект вне скоупа токена")
            matching = in_scope
        if len(matching) > 1:
            raise SubError(403, "у нескольких проектов эта рабочая папка — укажи project_id")
        target, policy_json = matching[0]
        if not subs_enabled(policy_json):
            raise SubError(403, "подписки в проекте выключены")
    if free and mode not in (None, DIRECT):
        raise SubError(403, FREE_HINT)  # вне проектов — только `direct` (и смена режима живой — тоже)
    # Н2: вне проектов «своё прежнее» — только из той же папки сессии.
    own_cwd = session["cwd"] if free else None
    audit = {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": client_name,
        "request_id": request_id,
    }
    # Ф99-0: разговор Codex/Cursor с новым процессом — его живые подписки сначала переезжают на этот процесс:
    # повторный `hub_subscribe` находит свою живую (та же роль и номер), а не заводит «роль 2».
    ext_rebind(conn, token_id=principal.token_id, session=session, now_ms=now_ms, audit=audit)

    # Ф41-2а П3: линии роли держат и сами записи незакрытых сессий хаба проекта (своя — не в счёт).
    held_lines = (
        {} if free else held_mod.role_lines(conn, target, except_id=hub.id if hub is not None else None)
    )
    lives = live_subs(conn, target)
    prev = next(
        (
            r
            for r in lives
            if r.api_token_id == principal.token_id and same_session(r, pid, proc_start, session_id)
        ),
        None,
    )
    if prev is not None:
        # Своя живая подписка — меняется на месте: роль (П8: занятая мёртвой — та `replaced`), режим, фильтр,
        # ключи. Ф35-1 П3: номер на месте не меняется; при смене роли — заданный или наименьший свободный.
        # Ф38-1 П3: `direct` — по ИТОГОВОМУ режиму (выход из `direct` тем же вызовом — законно).
        if acc_json is not None and (mode == DIRECT if mode is not None else bool(prev.direct)):
            raise SubError(422, ACCOUNTS_NO_DIRECT)
        taken: set[int] = set()
        if role_given and new_role_key != prev.role_key:
            alive = [r for r in lives if r.id != prev.id and not rec_dead(conn, r, now_ms, waiting)]
            if number is not None:
                new_no = number
                holder = _line_holder(alive, new_role_key, new_no)
                if holder is not None:
                    raise SubError(409, f"{line_name(holder.role, new_no)} уже слушает сессия #{holder.id}")
                _check_held_line(held_lines, new_role, new_role_key, new_no)
            else:
                new_no = _free_no([*alive, *held_lines.values()], new_role_key)
            if new_role_key is not None:
                taken = _take_role(
                    conn, lives, prev, new_role_key, new_no, now_ms=now_ms, waiting=waiting, audit=audit
                )
            conn.execute(
                "UPDATE main.session_sub SET role = ?, role_key = ?, role_no = ? WHERE id = ?",
                (new_role, new_role_key, new_no, prev.id),
            )
        elif number is not None and number != prev.role_no:
            raise SubError(
                409,
                f"номер живой подписки на месте не меняется (сейчас {line_name(prev.role, prev.role_no)}) — "
                "отпишись и подпишись заново с number",
            )
        elif role_given and new_role != prev.role:
            conn.execute("UPDATE main.session_sub SET role = ? WHERE id = ?", (new_role, prev.id))
        if role_given and new_role != prev.role:
            write_audit(
                conn,
                now_ms=now_ms,
                action="sub_mode",
                result="ok",
                detail={"sub_id": prev.id, "role": new_role is not None},
                **audit,
            )
            prev = get_sub(conn, prev.id)
        acc_before = prev.accounts_json
        acc_changed = acc_given and acc_json != acc_before
        if acc_changed:
            # Ф38-1 П2: фильтр — ДО `_set_mode`: тот при переходе в `cards` сам зовёт `backfill`, а `backfill`
            # перечитывает подписку.
            _set_accounts(conn, prev, acc_json, now_ms=now_ms, audit=audit)
            prev = get_sub(conn, prev.id)
        if mode is not None:
            to_direct = mode == DIRECT
            col_mode = "cards" if to_direct else mode
            if keywords is not None:
                # Ф36-1 П4(а): прежний интервал подмешивается ДО сравнения — повтор с теми же ключами
                # ничего не меняет и срок ждущих сигналов не сбрасывает.
                kw_json = keywords_mod.with_batch(
                    new_keywords,
                    batch_min if batch_min is not None else keywords_mod.stored_batch(prev.keywords_json),
                )
            elif mode == "messages" and new_filter == "personal" and prev.msg_filter == "personal":
                kw_json = prev.keywords_json  # «лично мне» без ключей в запросе — ключи остаются
                if batch_min is not None:
                    kw_json = keywords_mod.with_batch(kw_json, batch_min)  # меняется только интервал
            elif to_direct:
                kw_json = None  # у `direct` нет ни ключей, ни пачки
            else:
                # Смена фильтра на `all`/`cards` ключи снимает; Ф67-1: интервал пачки — заданный или прежний.
                kept = batch_min if batch_min is not None else keywords_mod.stored_batch(prev.keywords_json)
                kw_json = keywords_mod.with_batch(None, kept)
            if (
                col_mode != prev.mode
                or new_filter != prev.msg_filter
                or kw_json != prev.keywords_json
                or to_direct != bool(prev.direct)
            ):
                _set_mode(
                    conn,
                    prev,
                    col_mode,
                    msg_filter=new_filter or "all",
                    keywords_json=kw_json,
                    direct=to_direct,
                    now_ms=now_ms,
                    principal=principal,
                    client_name=client_name,
                    request_id=request_id,
                )
                prev = get_sub(conn, prev.id)
        elif batch_min is not None:
            # Ф36-1 П4(б) + Ф67-1: `batch_min` без `mode` и `keywords` у своей живой подписки — меняется
            # только интервал (с ключами и без); у `direct` пачки нет — 422.
            if prev.direct:
                raise SubError(422, BATCH_MIN_DIRECT)
            kw_json = keywords_mod.with_batch(prev.keywords_json, batch_min)
            if kw_json != prev.keywords_json:
                _set_mode(
                    conn,
                    prev,
                    prev.mode,
                    msg_filter=prev.msg_filter,
                    keywords_json=kw_json,
                    direct=bool(prev.direct),
                    now_ms=now_ms,
                    principal=principal,
                    client_name=client_name,
                    request_id=request_id,
                )
                prev = get_sub(conn, prev.id)
        if agent_batch_min is not None and agent_batch_min != agent_batch_row(conn, prev.id)[0]:
            _set_agent_batch(conn, prev, agent_batch_min, now_ms=now_ms, audit=audit)  # Ф48-4
            prev = get_sub(conn, prev.id)
        if acc_changed:
            # Ф38-1 П5: у `cards` — открытые карточки вновь слышимых аккаунтов приходят сразу (карточки
            # аккаунтов, слышимых и раньше, не трогаются: их сигналы уже были). У `messages` добора нет.
            backfill(conn, prev.id, prev.project_id, now_ms=now_ms, heard_before=(acc_before,))
        # Ф32-1 (ревью): беседы роли возвращаются по ИТОГОВОЙ роли — и когда роль поставлена вторым вызовом,
        # и повторным вызовом (добрать пропущенное по `limit`/`busy`).
        src_id, src_reason, src_wide = _restore_source(conn, hub, prev, taken)
        return Subscribed(
            prev,
            False,
            0,
            None,
            None,
            False,
            role_source_id=src_id,
            role_source_reason=src_reason,
            role_source_wide=src_wide,
        )

    # Ф26-1 A2: разбор живых подписок проекта — что завершить, кто остаётся рядом.
    to_end: list[tuple[SubRec, str]] = []
    dead_other: list[SubRec] = []  # мёртвые чужого разговора: причина — по линии новой подписки (ниже)
    others: list[SubRec] = []
    dead: set[int] = set()  # Ф32-1: сняты как мёртвые (процесса нет или брошенная подписка OpenCode)
    for rec in lives:
        same_proc = rec.session_pid == pid and rec.session_proc_start == proc_start
        if same_proc and rec.session_id == session_id:
            to_end.append((rec, "replaced"))  # П3: та же сессия под другим токеном
        elif same_proc and not many_convs(session_id, kind):
            # Новый разговор того же процесса Claude Code (/clear). У OpenCode на одном pid — много живых
            # сессий: другая — рядом (П1 Ф16-1 больше не 409).
            to_end.append((rec, "conversation_changed"))
        elif (
            rec.session_id == session_id
            and not many_convs(session_id, kind)
            and session_host_mod.state(rec.session_host, rec.session_pid, rec.session_proc_start)
            == session_host_mod.STALE
        ):
            # Ф33-1: тот же разговор Claude Code пришёл из другого процесса (`--resume` после падения машины
            # сессии), а прежний процесс другой машины «не в сети» — разговор переехал, прежняя подписка
            # `replaced` (по К2 «не в сети» иначе держала бы линию до 12 ч).
            dead.add(rec.id)
            to_end.append((rec, "replaced"))
        elif rec_dead(conn, rec, now_ms, waiting):
            dead.add(rec.id)
            if rec.session_id == session_id:
                to_end.append((rec, "replaced"))  # тот же разговор в умершем процессе (resume)
            else:
                dead_other.append(rec)
        else:
            others.append(rec)
    # Ф41-2а П6: места внешних подписок и сессий хаба считаются раздельно (потолок сессий хаба — на их
    # создании, `held.MAX_HUB_SESSIONS_PER_PROJECT`); линии и номера — по полному списку `others`.
    # Только потолок: шаг наследования «единственная в проекте» видит ВСЕХ живых соседей (ревью Н2).
    same_side = [r for r in others if held_mod.is_held(r.session_pid) == (hub is not None)]
    limit = MAX_FREE_SUBS if free else MAX_PROJECT_SUBS
    if hub is None and len(same_side) >= limit:
        ids = ", ".join(f"#{r.id}" for r in same_side)
        where = "вне проектов" if free else "в проекте"
        raise SubError(409, f"{where} уже {limit} подписок: {ids}")
    # Дальше — записи; отказ после них откатывает транзакцию целиком (`Db.write`).
    for rec, reason in to_end:
        end_sub(conn, rec.id, reason, now_ms=now_ms, **audit)

    name = session.get("name")
    session_name = name[:SESSION_NAME_MAX] if name and session.get("name_source") == "user" else None
    new_direct = mode == DIRECT
    new_mode = "cards" if mode in (None, DIRECT) else mode
    new_filter = new_filter or "all"
    kw_json = new_keywords
    want_inherit = mode is None or (new_mode == "messages" and new_filter == "personal" and keywords is None)

    # Ф35-1 (решение 2, П1): СНАЧАЛА линия. Роль — заданная или унаследованная по процессу или разговору
    # (Ф26-1 П2).
    own = {"pid": pid, "proc_start": proc_start, "session_id": session_id, "kind": kind, "cwd": own_cwd}
    role_from: SubRec | None = None
    role_why: str | None = None
    if not role_given and want_inherit:
        found, found_why = _own_prior(conn, target, personal_only=mode is not None, **own)
        if found is not None and found.role_key is not None:
            role_from, role_why = found, found_why
            new_role, new_role_key = found.role, found.role_key
    free_pick = False  # Ф38-3 C: номер не задан и прежней подписки сессии нет — взят наименьший свободный
    if number is not None:
        new_no = number
        holder = _line_holder(others, new_role_key, new_no)
        if holder is not None:
            raise SubError(409, f"{line_name(holder.role, new_no)} уже слушает сессия #{holder.id}")
        _check_held_line(held_lines, new_role, new_role_key, new_no)
    else:
        prior = role_from or _own_prior(conn, target, role_key=new_role_key, **own)[0]
        if prior is not None:
            # П1: прежняя линия сессии С РОЛЬЮ занята живой чужой — не переезд на свободный номер, а отказ.
            # Безрольный номер — только адрес (П2): у такой линии нет ни ключей, ни бесед, ни напоминаний,
            # смешать нечего — берётся наименьший свободный (сессии без роли зовут `hub_subscribe()` без
            # аргументов, отказ им ничего бы не объяснил).
            new_no = prior.role_no
            holder = _line_holder(others, new_role_key, new_no)
            if holder is not None and new_role_key is None:
                new_no = _free_no(others, None)
            elif holder is not None:
                raise SubError(
                    409, f"твою линию «{new_role}» №{new_no} держит сессия #{holder.id} — задай number явно"
                )
            else:
                _check_held_line(held_lines, new_role, new_role_key, new_no)
        else:
            new_no = _free_no([*others, *held_lines.values()], new_role_key)
            free_pick = True
    for rec in dead_other:
        # Мёртвая подписка той же линии — `replaced`, прочие мёртвые — `session_gone`.
        same_line = new_role_key is not None and rec.role_key == new_role_key and rec.role_no == new_no
        end_sub(conn, rec.id, "replaced" if same_line else "session_gone", now_ms=now_ms, **audit)

    # ПОТОМ — один источник режима и ключей: у подписки с ролью — только своя линия (П1).
    inherited_from: int | None = None
    inherited_why: str | None = None
    source: SubRec | None = None
    why: str | None = None
    line = {
        "pid": pid,
        "proc_start": proc_start,
        "session_id": session_id,
        "session_name": session_name,
        "kind": kind,
        "role_key": new_role_key,
        "role_no": new_no,
        "live_count": len(others),
        "now_ms": now_ms,
        "own_cwd": own_cwd,
    }
    if want_inherit:
        source, why = _inherit_source(conn, target, personal_only=mode is not None, **line)
    if source is not None and mode is None:
        # Ф25-1 П2: подсказки хаба после /clear и выхода ожидания велят звать `hub_subscribe()` без
        # аргументов.
        new_direct = bool(source.direct)
        if not new_direct:
            new_mode, new_filter = source.mode, source.msg_filter
            # Ф67-1: у источника без «лично мне» — только его интервал пачки (ключей там нет).
            raw = source.keywords_json
            kw_json = raw if _is_personal(source) else keywords_mod.batch_only(raw)
        if source.mode != "cards" or new_direct:
            inherited_from = source.id  # П11: и для унаследованного `direct`
    elif source is not None and source.keywords_json is not None:
        kw_json = source.keywords_json
        inherited_from = source.id
    if role_from is not None:
        if inherited_from is None:
            inherited_from = role_from.id
        why = role_why
    if inherited_from is not None:
        inherited_why = why
    inherit_blocked = source is None and why == "several"
    if free and not new_direct:
        raise SubError(403, FREE_HINT)  # решение 14: вне проектов — только `direct`, явный или унаследованный
    # Ф38-1 П3: `direct` — по итоговому режиму (явному или унаследованному).
    if new_direct and acc_json is not None:
        raise SubError(422, ACCOUNTS_NO_DIRECT)
    # Ревью Ф36-1 + Ф38-1 п.7: режим не наследовался — СВОЁ прежнее берётся только по линии роли, тому же
    # процессу или тому же разговору; шаги «то же имя» и «единственная в проекте» могли бы отдать чужое
    # (ждущие сигналы, фильтр по аккаунтам).
    own_line: SubRec | None = None
    if source is None and not inherit_blocked:
        found, found_why = _inherit_source(conn, target, personal_only=False, **line)
        if found_why in ("role", "process", "session"):
            own_line = found
    # Ф38-1 п.7: фильтр по аккаунтам наследуется как режим — у источника режима, иначе из своей линии;
    # источник в `direct` и итог `direct` — без фильтра; задан в запросе (и пустым списком) — он.
    new_acc = acc_json
    if not acc_given and not new_direct:
        acc_from = source if source is not None else own_line
        if acc_from is not None and not acc_from.direct and accounts_of(acc_from) is not None:
            new_acc = acc_from.accounts_json
    acc_inherited = new_acc is not None and not acc_given
    # Ф36-1 A3: интервал пачки. Задан — пишется (и поверх унаследованных ключей: меняется только интервал);
    # ключи заданы без него — интервал прежнего `keywords_json` линии (тот же поиск источника, что у
    # наследования: с явными ключами он иначе не зовётся); унаследованные ключи несут свой интервал сами.
    if batch_min is not None:
        if new_direct:
            raise SubError(422, BATCH_MIN_DIRECT)  # унаследованный `direct`
        kw_json = keywords_mod.with_batch(kw_json, batch_min)  # Ф67-1: и без ключей
    elif keywords is not None and kw_json is not None:
        before, _why = _inherit_source(conn, target, personal_only=True, **line)
        if before is not None:
            kw_json = keywords_mod.with_batch(kw_json, keywords_mod.stored_batch(before.keywords_json))
    cur = conn.execute(
        "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
        "session_id, session_cwd, session_name, created_ts, mode, msg_filter, keywords_json, role, role_key, "
        "role_no, direct, accounts_json, session_host, session_boot, session_seen_ts, session_engine) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target,
            principal.token_id,
            pid,
            proc_start,
            session_id,
            session["cwd"],
            session_name,
            now_ms,
            new_mode,
            new_filter,
            kw_json,
            new_role,
            new_role_key,
            new_no,
            int(new_direct),
            new_acc,
            session.get("host"),  # Ф33-1: машина сессии (NULL — своя)
            session.get("boot"),  # Ф33-1: её загрузка (NULL — клиент не прислал)
            session_host_mod.seen_at_insert(session, now_ms),  # Ф33-1: контакт
            engine_for_insert(conn, session),  # Ф74-2: движок сессии
        ),
    )
    sub_id = cur.lastrowid
    # Ф48-4: интервал и срок пачки сообщений агентов переезжают с подпиской линии; заданный в запросе
    # интервал главнее (срок тогда не переносится — у новых правил его ещё нет).
    batch_prior = _agent_batch_prior(conn, hub, sub_id, target, new_role_key, new_no, own)
    if agent_batch_min is not None:
        conn.execute(
            "UPDATE main.session_sub SET agent_batch_min = ? WHERE id = ?", (agent_batch_min, sub_id)
        )
    elif batch_prior is not None:
        prior_min, prior_ts = agent_batch_row(conn, batch_prior.id)
        carry_ts = prior_ts if (prior_ts or 0) > now_ms else None
        if prior_min is not None or carry_ts is not None:
            conn.execute(
                "UPDATE main.session_sub SET agent_batch_min = ?, agent_batch_ts = ? WHERE id = ?",
                (prior_min, carry_ts, sub_id),
            )
    if new_mode == "messages" and not new_direct:
        # Ф36-1 П1: ждущие сигналы завершённого источника наследования не пропадают — переезжают новой.
        # Режим и ключи заданы явно (источник не искали или искали только среди «лично мне») — тот же поиск
        # по линии: `hub_subscribe(mode="personal", keywords=[…])` после /clear накопленное тоже не теряет.
        # Без наследования режима — только СВОЁ прежнее (`own_line`, выше).
        carry_from = source if source is not None else own_line
        if carry_from is not None:
            chan_subs.carry_project_signals(
                conn,
                carry_from,
                sub_id,
                batch_ms=keywords_mod.batch_ms(kw_json),
                now_ms=now_ms,
                accounts_json=new_acc,  # Ф38-1 п.9: сигналы бесед вне фильтра новой не переезжают
                # Ф41-2а П7: прежняя подписка — той же сессии хаба (сменился только токен).
                same_held=hub is not None and same_session(carry_from, pid, proc_start, session_id),
            )
    if hub is not None:
        # Ф41-2а П7 (ревью Н1): прежняя подписка ТОЙ ЖЕ сессии хаба снята сменой токена — сообщения агентов,
        # привязанные к ней (`to_sub_id`: безрольная сессия, диалог владельца), не теряются, а переходят новой.
        prior_same = _same_held_prior(conn, hub, sub_id)
        if prior_same is not None:
            from . import peer_msgs as peer_msgs_mod  # здесь: `peer_msgs` сам импортирует этот модуль

            peer_msgs_mod.rebind_lost(conn, prior_same, sub_id, now_ms=now_ms)
    backfilled = backfill(conn, sub_id, target, now_ms=now_ms)
    shown_mode = DIRECT if new_direct else new_mode
    write_audit(
        conn,
        now_ms=now_ms,
        action="sub_create",
        result="ok",
        detail={
            "sub_id": sub_id,
            "project_id": target,
            "pid": pid,
            "session_id8": session_id[:SESSION_ID8],
            "backfilled": backfilled,
            **({"mode": shown_mode} if shown_mode != "cards" else {}),
            **({"msg_filter": new_filter} if new_filter != "all" else {}),
            **_keys_audit(kw_json),
            **({"role": True} if new_role is not None else {}),
            **({"role_no": new_no} if new_no != 1 else {}),
            **({"accounts": sub_accounts.parse(new_acc)} if new_acc is not None else {}),  # Ф38-1
            **({"agent_batch_min": agent_batch_min} if agent_batch_min is not None else {}),  # Ф48-4
            **({"inherited_from": inherited_from} if inherited_from is not None else {}),
            **({"inherited_why": inherited_why} if inherited_why is not None else {}),
        },
        **audit,
    )
    rec = get_sub(conn, sub_id)
    assert rec is not None  # noqa: S101 — только что вставлена в этой транзакции
    # Ф38-3 C: у агента вне проектов и у подписки без роли линий с настройками нет.
    other_lines: tuple[dict, ...] = ()
    if free_pick and target != NO_PROJECT_ID and rec.role_key is not None:
        other_lines = _other_lines(conn, rec, others, now_ms=now_ms)
    # Ф32-1 (ревью): источник роли — по итоговой роли подписки (заданной или унаследованной по разговору).
    src_id, src_reason, src_wide = _restore_source(conn, hub, rec, dead)
    return Subscribed(
        rec,
        True,
        backfilled,
        inherited_from,
        inherited_why,
        inherit_blocked,
        role_source_id=src_id,
        role_source_reason=src_reason,
        accounts_inherited=acc_inherited,
        other_lines=other_lines,
        role_source_wide=src_wide,
    )


def _line_holder(others: list[SubRec], role_key: str | None, role_no: int) -> SubRec | None:
    """Ф35-1: кто из живых чужих держит линию `(role_key, role_no)` (без роли — среди безрольных)."""
    return next((r for r in others if r.role_key == role_key and r.role_no == role_no), None)


def _free_no(others: list[SubRec], role_key: str | None) -> int:
    """Решение 2, правило 3: наименьший свободный номер среди живых чужих той же роли (безрольных). Ф41-2а
    П3: в `others` вызывающий добавляет и записи сессий хаба, держащих линии роли (`held.role_lines` — у них
    те же `role_key` и `role_no`)."""
    used = {r.role_no for r in others if r.role_key == role_key}
    return next(n for n in range(1, ROLE_NO_MAX + 1) if n not in used)


def _check_held_line(held_lines: dict, role: str | None, role_key: str | None, role_no: int) -> None:
    """Ф41-2а П3: линию `(role_key, role_no)` держит запись ДРУГОЙ незакрытой сессии хаба — 409, есть у той
    живая подписка или нет (иначе в окне без подписки — до первого запуска, после отписки, отзыва токена —
    внешняя сессия заняла бы линию, и сессия хаба получала бы 409 навсегда)."""
    holder = held_lines.get((role_key, role_no)) if role_key is not None else None
    if holder is not None:
        raise SubError(
            409, f"{line_name(role, role_no)}: линию держит сессия хаба «{held_mod.shown_name(holder)}»"
        )


def hub_session_of(conn, session) -> held_mod.HubSession:
    """Ф41-2а (3.2, П2в, П4): незакрытая сессия хаба запроса с `kind == "hub"` или условной тройкой — по базе.
    Несогласованная тройка — 422, нет записи или закрыта — 409. `session` — словарь запроса."""
    error = held_mod.triple_error(
        session["pid"], session["proc_start"], session["session_id"], session.get("kind")
    )
    if error is not None:
        raise SubError(422, error)
    hub = held_mod.get_open(conn, held_mod.held_id(session["pid"]))
    if hub is None:
        raise SubError(409, "сессия хаба не найдена или закрыта")
    return hub


def _hub_target(conn, hub: held_mod.HubSession, project_id: int | None, principal) -> int:
    """П5: проект подписки сессии хаба — из записи; другой `project_id` запроса, проект вне скоупа токена и
    выключенные подписки проекта — 403 (как у внешней сессии)."""
    if project_id is not None and project_id != hub.project_id:
        raise SubError(403, "проект не совпадает с проектом сессии хаба")
    if principal.project_ids is not None and hub.project_id not in principal.project_ids:
        raise SubError(403, "проект вне скоупа токена")
    row = conn.execute(
        "SELECT agent_policy_json FROM main.project WHERE id = ?", (hub.project_id,)
    ).fetchone()
    if row is None or not subs_enabled(row[0]):
        raise SubError(403, "подписки в проекте выключены")
    return hub.project_id


_ANY_ROLE = object()


def _own_prior(
    conn,
    project_id: int,
    *,
    pid: int,
    proc_start: int,
    session_id: str,
    kind: str,
    cwd: str | None,
    role_key=_ANY_ROLE,
    personal_only: bool = False,
) -> tuple[SubRec | None, str | None]:
    """Ф35-1 (решение 2, правило 2): самая свежая ЗАВЕРШЁННАЯ подписка проекта этой же сессии — того же
    процесса (только Claude Code: у OpenCode pid общий) или того же `session_id`; `(подписка, "process" |
    "session")`. `role_key` — только с этой ролью (`None` — только безрольные); `cwd` — только из той же папки
    сессии (Н2, проект 0)."""
    extra = "AND mode = 'messages' AND msg_filter = 'personal' " if personal_only else ""
    params: list = [project_id]
    if role_key is not _ANY_ROLE:
        extra += "AND role_key IS ? "
        params.append(role_key)
    if cwd is not None:
        extra += "AND session_cwd = ? "
        params.append(cwd)
    steps: list[tuple[str, str, tuple]] = []
    if not many_convs(session_id, kind):
        steps.append(("process", "session_pid = ? AND session_proc_start = ?", (pid, proc_start)))
    steps.append(("session", "session_id = ?", (session_id,)))
    for why, where, step_params in steps:
        row = conn.execute(
            f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NOT NULL "  # noqa: S608
            f"{extra}AND {where} ORDER BY ended_ts DESC, id DESC LIMIT 1",
            (*params, *step_params),
        ).fetchone()
        if row is not None:
            return SubRec(*row), why
    return None, None


def live_subs(conn, project_id: int) -> list[SubRec]:
    """Незавершённые подписки проекта (живость процесса не проверяется), старшие первыми."""
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL "  # noqa: S608
        "ORDER BY id",
        (project_id,),
    ).fetchall()
    return [SubRec(*row) for row in rows]


def _take_role(
    conn,
    lives: list[SubRec],
    mine: SubRec,
    role_key: str,
    role_no: int,
    *,
    now_ms: int,
    waiting,
    audit: dict,
) -> set[int]:
    """П8 + Ф35-1 П3: смена роли своей подписки на месте — ЛИНИЯ `(role_key, role_no)` у живой чужой сессии —
    409; у мёртвой или брошенной — та завершается `replaced` (только она, не вся роль). Возвращает id
    завершённых (Ф32-1: мёртвый источник роли)."""
    taken: set[int] = set()
    for rec in lives:
        if rec.id == mine.id or rec.role_key != role_key or rec.role_no != role_no:
            continue
        if not rec_dead(conn, rec, now_ms, waiting):
            raise SubError(409, f"{line_name(rec.role, role_no)} уже слушает сессия #{rec.id}")
        end_sub(conn, rec.id, "replaced", now_ms=now_ms, **audit)
        taken.add(rec.id)
    return taken


def neighbours(conn, rec: SubRec) -> list[dict]:
    """Ф26-1 A2: живые подписки проекта, кроме `rec` (ответ `POST /subs`): номер, роль, имя сессии, режим,
    фильтр, ЧИСЛА ключей (тексты ключей — дело самой сессии)."""
    result: list[dict] = []
    if rec.project_id == NO_PROJECT_ID:
        return result  # Н2: у агента вне проектов «соседей» нет (реестр — `hub_agents`)
    for other in live_subs(conn, rec.project_id):
        if other.id == rec.id or not rec_session_alive(conn, other):
            continue
        result.append(_neighbour_row(other))
    return result


def session_role(conn, key: tuple[int, int, int, str], project_id: int | None) -> tuple[str | None, int]:
    """Ф39 A2 п.9: роль сессии `key` (токен, pid, starttime, `session_id`) — её живая подписка на проект с
    ролью; из нескольких — та, что на проект `project_id` (проект беседы), иначе с наименьшим `id`. Роли нет —
    `(None, 1)`. Роль сессия назначает себе сама: это подпись, прав не даёт."""
    rows = conn.execute(
        "SELECT project_id, role, role_no FROM main.session_sub WHERE ended_ts IS NULL AND role IS NOT NULL "
        "AND api_token_id = ? AND session_pid = ? AND session_proc_start = ? AND session_id = ? ORDER BY id",
        key,
    ).fetchall()
    if not rows:
        return None, 1
    for row_project, role, role_no in rows:
        if project_id is not None and row_project == project_id:
            return role, role_no
    return rows[0][1], rows[0][2]


def author_sub(conn, *, token_id: int, session, project_id: int | None) -> SubRec | None:
    """Ф39 B2: подписка АВТОРА черновика — живая подписка на проект сессии `session` (pid, `proc_start`,
    `session_id`) под токеном `token_id`, пока её процесс жив (`is_alive`). Из нескольких — подписка на проект
    беседы `project_id`, иначе на проект 0 (агент вне проектов), иначе с наименьшим `id`. Режим любой (и
    `direct`), роль не обязательна. Нет — `None`."""
    if not is_alive(session.pid, session.proc_start, session_host_mod.host_of(session)):
        return None
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE ended_ts IS NULL AND api_token_id = ? "  # noqa: S608
        "AND session_pid = ? AND session_proc_start = ? AND session_id = ? ORDER BY id",
        (token_id, session.pid, session.proc_start, session.session_id),
    ).fetchall()
    recs = [SubRec(*row) for row in rows]
    if not recs:
        return None
    for wanted in (project_id, NO_PROJECT_ID):
        for rec in recs:
            if wanted is not None and rec.project_id == wanted:
                return rec
    return recs[0]


def author_role(rec: SubRec) -> str | None:
    """Ф39 B2: роль автора словами — `role` и, у линии с номером больше 1, номер через пробел («маркетолог
    2»); без роли — `None`. Роль сессия назначает себе сама (Ф37-1 П2): подпись — сведение, прав не даёт."""
    if rec.role is None:
        return None
    return rec.role if rec.role_no <= 1 else f"{rec.role} {rec.role_no}"


def _neighbour_row(other: SubRec) -> dict:
    """Строка чужой подписки проекта в ответе `POST /subs` — у соседей (`neighbours`) и у других линий роли
    (`_other_lines`): только ЧИСЛА ключей."""
    keys = None
    if keywords_mod.has_keys(other.keywords_json):
        n_keys, n_minus = keywords_mod.spec_counts(other.keywords_json)
        keys = {"keys": n_keys, "exclude": n_minus}
    return {
        "id": other.id,
        "role": other.role,
        "role_no": other.role_no,
        "session_name": other.session_name,
        "mode": mode_of(other),
        "msg_filter": other.msg_filter,
        "keys": keys,
        "accounts": accounts_of(other),  # Ф38-1 п.10
    }


def _line_settings(rec: SubRec) -> tuple:
    """Ф38-3 C: настройки линии для сравнения — режим, фильтр сообщений, ключи (с исключениями и действующим
    интервалом пачки), фильтр по аккаунтам; разобранные значения, не строки JSON. То, чего у режима нет
    (фильтр и ключи вне `messages`, аккаунты у `direct`), в сравнение не идёт."""
    mode = mode_of(rec)
    msg_filter = rec.msg_filter if mode == "messages" else "all"
    spec = keywords_mod.parse_spec(rec.keywords_json) if _is_personal(rec) and not rec.direct else None
    keys = None
    if spec is not None:
        keys = (sorted(spec["keys"]), sorted(spec["exclude"]), keywords_mod.batch_ms(spec))
    elif not rec.direct:
        keys = keywords_mod.batch_ms(rec.keywords_json) or None  # Ф67-1: интервал пачки без ключей
    return mode, msg_filter, keys, None if rec.direct else accounts_of(rec)


def _other_lines(conn, rec: SubRec, others: list[SubRec], *, now_ms: int) -> tuple[dict, ...]:
    """Ф38-3 C: другие линии роли подписки `rec` (тот же `role_key`, другой `role_no`), чьи настройки
    ОТЛИЧАЮТСЯ от её итоговых. Линию представляет живая чужая подписка (`others`), иначе — самая свежая
    завершённая подписка линии, если она завершена не раньше `LINE_HINT_MS` назад. Строка — как у соседа
    (`_neighbour_row`) плюс `live` и `ended_ts` (у живой — `None`); не больше `LINE_HINT_MAX`, по номеру."""
    lines: dict[int, SubRec] = {
        other.role_no: other
        for other in others
        if other.role_key == rec.role_key and other.role_no != rec.role_no
    }
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NOT NULL "  # noqa: S608
        "AND ended_ts >= ? AND role_key = ? AND role_no != ? ORDER BY ended_ts DESC, id DESC",
        (rec.project_id, now_ms - LINE_HINT_MS, rec.role_key, rec.role_no),
    ).fetchall()
    for row in rows:
        ended = SubRec(*row)
        lines.setdefault(ended.role_no, ended)  # живая главнее; среди завершённых — самая свежая
    mine = _line_settings(rec)
    result: list[dict] = []
    for _no, other in sorted(lines.items()):
        if _line_settings(other) == mine:
            continue
        live = other.ended_ts is None
        result.append({**_neighbour_row(other), "live": live, "ended_ts": other.ended_ts})
        if len(result) == LINE_HINT_MAX:
            break
    return tuple(result)


def _is_personal(rec: SubRec) -> bool:
    return rec.mode == "messages" and rec.msg_filter == "personal"


def _role_source(
    conn, project_id: int, role_key: str, role_no: int, *, cwd: str | None = None
) -> SubRec | None:
    """Ф32-1 П3: самая свежая ЗАВЕРШЁННАЯ подписка ЛИНИИ `(role_key, role_no)` проекта (Ф35-1, решение 3) —
    без фильтра режима и токена (в отличие от `_inherit_source`, который зовётся не всегда: роль в `cards`,
    явные `mode`/`keywords`). `cwd` — только из той же папки сессии (Н2: проект 0)."""
    only = "" if cwd is None else "AND session_cwd = ? "
    row = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NOT NULL "  # noqa: S608
        f"AND role_key = ? AND role_no = ? {only}ORDER BY ended_ts DESC, id DESC LIMIT 1",
        (project_id, role_key, role_no) + (() if cwd is None else (cwd,)),
    ).fetchone()
    return SubRec(*row) if row is not None else None


def _same_held_prior(conn, hub: held_mod.HubSession, sub_id: int) -> SubRec | None:
    """Ф41-2а П7 (ревью Н1): самая свежая ЗАВЕРШЁННАЯ подписка проекта с условной тройкой сессии хаба `hub`
    (любого токена), кроме `sub_id`, — если она снята сменой токена (`chan_subs.SAME_HELD_REASONS`:
    `token_revoked`, `replaced`). Владелец тот же: по ней новой подписке возвращаются беседы, ждущие сигналы
    и сообщения агентов — и у сессии без роли (у неё источника роли нет). Явная отписка, снятие владельцем,
    `subs off` — `None` (насовсем). Чужой тройке (другая сессия хаба, внешняя сессия) не достаётся ничего."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    pid, proc_start, session_id = held_mod.triple(hub.id)
    row = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND session_pid = ? "  # noqa: S608
        "AND session_proc_start = ? AND session_id = ? AND id != ? AND ended_ts IS NOT NULL "
        "ORDER BY ended_ts DESC, id DESC LIMIT 1",
        (hub.project_id, pid, proc_start, session_id, sub_id),
    ).fetchone()
    prior = SubRec(*row) if row is not None else None
    if prior is None or prior.end_reason not in chan_subs.SAME_HELD_REASONS:
        return None
    return prior


def _same_ext_prior(conn, rec: SubRec) -> SubRec | None:
    """Ф92-1 (B3): у ВНЕШНЕЙ сессии — самая свежая завершённая подписка проекта той же тройки (pid, starttime,
    session_id) под ДРУГИМ токеном, если она снята сменой токена той же сессией (`replaced`) или перевыпуском
    токена профиля (`token_reissued`) — `chan_subs.SAME_EXT_REASONS`. Владелец тот же: её беседы переходят
    `rec` и без роли. Тот же токен — не сюда (`replaced` тогда — линию мёртвой сессии заняла другая; её
    беседы — по источнику роли). Ревью Ф92-1: и `session_gone` любого токена — ядро сняло живую сессию как
    умершую (окно «не в сети»), а она подписывается заново (сама тройка жива — иначе подписка не прошла бы).
    Явный отзыв токена владельцем, отписка, снятие владельцем — `None`."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    row = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND session_pid = ? "  # noqa: S608
        "AND session_proc_start = ? AND session_id = ? AND id != ? "
        "AND ended_ts IS NOT NULL ORDER BY ended_ts DESC, id DESC LIMIT 1",
        (rec.project_id, rec.session_pid, rec.session_proc_start, rec.session_id, rec.id),
    ).fetchone()
    prior = SubRec(*row) if row is not None else None
    if prior is None:
        return None
    if prior.end_reason == "session_gone":
        return prior
    if prior.api_token_id != rec.api_token_id and prior.end_reason in chan_subs.SAME_EXT_REASONS:
        return prior
    return None


def _restore_source(
    conn, hub: held_mod.HubSession | None, rec: SubRec, dead: set[int]
) -> tuple[int | None, str | None, bool]:
    """Источник возврата подписок на беседы для `rec` — `(id, причина конца, с другой линии ли)`: у сессии
    хаба, чья прежняя подписка снята сменой токена, — эта прежняя подписка той же тройки
    (`_same_held_prior`; и без роли); у внешней — так же (`_same_ext_prior`, Ф92-1 B3); иначе — источник
    роли (`_role_source_of`, Ф32-1; Ф92-1 B2 — и с другой линии роли)."""
    prior = _same_held_prior(conn, hub, rec.id) if hub is not None else _same_ext_prior(conn, rec)
    if prior is not None:
        return prior.id, prior.end_reason, False
    return _role_source_of(conn, hub, rec, dead)


def _role_source_wide(conn, hub: held_mod.HubSession | None, rec: SubRec) -> SubRec | None:
    """Ф92-1 (B2): своей прежней линии у `rec` нет — самая свежая ЗАВЕРШЁННАЯ подписка той же роли проекта с
    ДРУГОЙ линии, которую сейчас никто не держит: ни живая подписка (кроме `rec`), ни запись незакрытой сессии
    хаба (кроме своей). Беседы живой линии — у неё; беседу, которую держит живая сессия роли, возврат не
    трогает (`chan_subs.restore_for_role(role_key=…)`). Ревью: среди свободных линий — самая свежая, которой
    есть что вернуть (`_wide_returnable`); свежая, снятая насовсем (владельцем, отпиской), не заслоняет."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    held = held_mod.role_lines(conn, rec.project_id, except_id=hub.id if hub is not None else None)
    busy = {no for (key, no) in held if key == rec.role_key}
    busy.update(
        no
        for (no,) in conn.execute(
            "SELECT role_no FROM main.session_sub WHERE project_id = ? AND role_key = ? AND ended_ts IS NULL "
            "AND id != ?",
            (rec.project_id, rec.role_key, rec.id),
        ).fetchall()
    )
    busy.add(rec.role_no)
    only = "AND session_cwd = ? " if rec.project_id == NO_PROJECT_ID else ""
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NOT NULL "  # noqa: S608
        f"AND role_key = ? {only}ORDER BY ended_ts DESC, id DESC",
        (rec.project_id, rec.role_key) + ((rec.session_cwd,) if only else ()),
    )
    for row in rows.fetchall():
        cand = SubRec(*row)
        if cand.role_no not in busy and _wide_returnable(conn, cand, chan_subs.RESTORE_REASONS):
            return cand
    return None


def _wide_returnable(conn, cand: SubRec, reasons: tuple[str, ...]) -> bool:
    """Ревью Ф92-1 (B2): источник с другой линии годится, только если сам кончился «сам» (`reasons` или
    `replaced`) и у его сессии (токен + тройка) есть подписка на беседу — живая или снятая «сама»."""
    if cand.end_reason not in (*reasons, "replaced"):
        return False
    marks = ", ".join("?" for _ in reasons)
    row = conn.execute(
        "SELECT 1 FROM main.session_chan_sub WHERE api_token_id = ? AND session_pid = ? "  # noqa: S608
        "AND session_proc_start = ? AND session_id = ? "
        f"AND (ended_ts IS NULL OR end_reason IN ({marks})) LIMIT 1",
        (cand.api_token_id, cand.session_pid, cand.session_proc_start, cand.session_id, *reasons),
    ).fetchone()
    return row is not None


def _role_source_of(
    conn, hub: held_mod.HubSession | None, rec: SubRec, dead: set[int]
) -> tuple[int | None, str | None, bool]:
    """Ф32-1 П3 + ревью: `(role_source_id, role_source_reason, с другой линии)` итоговой подписки `rec` —
    `(None, None, False)` без роли или без прежней подписки роли. Источник `replaced`, которого заменили как
    мёртвого (`dead` — сняты этим вызовом; при повторном вызове — процесса источника нет, у OpenCode —
    брошенная подписка), считается `session_gone`: его сессии нет, cleanup просто не успел. Живые строки бесед
    источника всё равно проверяет `chan_subs.restore_for_role` (`busy`). Ф92-1 (B2): своей линии нет —
    источник с другой свободной линии роли (`_role_source_wide`)."""
    if rec.role_key is None:
        return None, None, False
    wide = False
    source = _role_source(
        conn,
        rec.project_id,
        rec.role_key,
        rec.role_no,
        cwd=rec.session_cwd if rec.project_id == NO_PROJECT_ID else None,
    )
    if source is None:
        source = _role_source_wide(conn, hub, rec)
        wide = source is not None
    if source is None:
        return None, None, False
    reason = source.end_reason
    if reason == "replaced" and (
        source.id in dead
        or rec_many_convs(source)
        or not session_alive(conn, source.session_pid, source.session_proc_start, source.session_host)
    ):
        reason = "session_gone"
    return source.id, reason, wide


def _inherit_source(
    conn,
    project_id: int,
    *,
    pid: int,
    proc_start: int,
    session_id: str,
    session_name: str | None,
    kind: str,
    role_key: str | None,
    role_no: int,
    live_count: int,
    personal_only: bool,
    now_ms: int,
    own_cwd: str | None = None,
) -> tuple[SubRec | None, str | None]:
    """Ф26-1 П2 (заменяет `_last_ended` Ф25-1): из чьей ЗАВЕРШЁННОЙ подписки проекта новая берёт режим и ключи
    — первое найденное по порядку: (1) задана роль — только самая свежая с той же ролью; (2) тот же процесс —
    только Claude Code (у OpenCode pid общий на все сессии сервера); (3) тот же `session_id`; (4) то же
    `session_name`; (5) в проекте нет живых подписок, у самой свежей (по `ended_ts`) нет роли, за сутки не
    было подписок с ролью и (Р11) среди завершённых за сутки безымянных нет двух, живших одновременно, — самая
    свежая. `personal_only` — среди «лично мне». Возвращает `(подписка, почему)`, `(None, "several")` — шаг 5
    закрыт пересечением безымянных, или `(None, None)`.

    Ф35-1 (решение 3, П1): шаг роли — по ЛИНИИ `(role_key, role_no)`. Н2: вне проектов (`own_cwd` задан) —
    только своё прежнее: шаги «процесс» и «разговор» и только из той же папки сессии (и у линии с ролью);
    шагов «то же имя» и «единственная в проекте» нет."""
    extra = "AND mode = 'messages' AND msg_filter = 'personal' " if personal_only else ""
    extra_params: tuple = ()
    if own_cwd is not None:
        extra += "AND session_cwd = ? "
        extra_params = (own_cwd,)

    def latest(where: str, params: tuple) -> SubRec | None:
        # Р11: самая свежая — по моменту завершения (при равенстве — по номеру), не по порядку создания.
        row = conn.execute(
            f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NOT NULL "  # noqa: S608
            f"{extra}AND {where} ORDER BY ended_ts DESC, id DESC LIMIT 1",
            (project_id, *extra_params, *params),
        ).fetchone()
        return SubRec(*row) if row is not None else None

    steps: list[tuple[str, str, tuple]] = []
    if not many_convs(session_id, kind):
        steps.append(("process", "session_pid = ? AND session_proc_start = ?", (pid, proc_start)))
    steps.append(("session", "session_id = ?", (session_id,)))
    if role_key is not None:
        line = ("role_key = ? AND role_no = ?", (role_key, role_no))
        if own_cwd is None:
            found = latest(*line)
            return (found, "role") if found is not None else (None, None)
        for _why, where, params in steps:
            found = latest(f"{line[0]} AND {where}", (*line[1], *params))
            if found is not None:
                return found, "role"
        return None, None
    if session_name is not None and own_cwd is None:
        steps.append(("name", "session_name = ?", (session_name,)))
    for why, where, params in steps:
        found = latest(where, params)
        if found is not None:
            return found, why
    if live_count == 0 and own_cwd is None:
        found = latest("1=1", ())
        roles_recently = conn.execute(
            "SELECT 1 FROM main.session_sub WHERE project_id = ? AND ended_ts IS NOT NULL AND ended_ts >= ? "
            "AND role_key IS NOT NULL LIMIT 1",
            (project_id, now_ms - ENDED_RECENT_MS),
        ).fetchone()
        if found is None or found.role_key is not None or roles_recently is not None:
            return None, None
        # Р11: за сутки в проекте жили одновременно две безымянные сессии (интервалы `[created_ts, ended_ts)`
        # пересекаются строго; цепочка /clear и `replaced` — старая завершена в момент создания новой — не
        # пересечение) — чья линия у новой, не понять: наследования нет.
        overlap = conn.execute(
            "SELECT 1 FROM main.session_sub a JOIN main.session_sub b ON b.project_id = a.project_id "
            "AND b.id > a.id WHERE a.project_id = ? AND a.ended_ts IS NOT NULL AND b.ended_ts IS NOT NULL "
            "AND a.ended_ts >= ? AND b.ended_ts >= ? AND a.role_key IS NULL AND b.role_key IS NULL "
            "AND a.created_ts < b.ended_ts AND b.created_ts < a.ended_ts LIMIT 1",
            (project_id, now_ms - ENDED_RECENT_MS, now_ms - ENDED_RECENT_MS),
        ).fetchone()
        if overlap is not None:
            return None, "several"
        return found, "only"
    return None, None


# Ф38-1 П3: отказ фильтра по аккаунтам при итоговом режиме `direct`.
ACCOUNTS_NO_DIRECT = "accounts — не для режима direct (задай mode)"


def accounts_of(rec) -> dict | None:
    """Ф38-1 (П1): фильтр подписки по аккаунтам — `{"only": [..]}` или `{"except": [..]}`; `None` — фильтра
    нет (и при кривом значении столбца — без исключения)."""
    return sub_accounts.parse(rec.accounts_json)


def listens_account(rec, account_id) -> bool:
    """Ф38-1 (П1): слышит ли подписка на проект `rec` беседы аккаунта `account_id` (правило —
    `sub_accounts.listens`)."""
    return sub_accounts.listens(accounts_of(rec), account_id)


def check_accounts(conn, accounts, *, what: str = "accounts") -> None:
    """Ф38-1 п.6: список номеров аккаунтов запроса — до 20 целых ≥ 1 (не `bool`), каждый — существующий
    `main.account.id`; иначе `SubError` 422 с перечнем неизвестных. Пустой список годен (фильтр снять)."""
    if not isinstance(accounts, list) or len(accounts) > sub_accounts.ACCOUNTS_MAX:
        raise SubError(422, f"{what}: список номеров аккаунтов, не больше {sub_accounts.ACCOUNTS_MAX}")
    if not all(sub_accounts.is_account_id(x) for x in accounts):
        raise SubError(422, f"{what}: номера аккаунтов — целые от 1")
    known = {row[0] for row in conn.execute("SELECT id FROM main.account")}
    unknown = sorted(set(accounts) - known)
    if unknown:
        raise SubError(422, f"{what}: нет аккаунтов {', '.join(str(x) for x in unknown)}")


def _new_accounts(conn, accounts, accounts_except) -> tuple[bool, str | None]:
    """Ф38-1 П1: фильтр запроса -> `(задан ли, accounts_json)`; пустой список — задан, фильтр снять (`None`).
    `accounts` и `accounts_except` вместе — 422."""
    if accounts is not None and accounts_except is not None:
        raise SubError(422, "accounts и accounts_except — что-то одно")
    if accounts is None and accounts_except is None:
        return False, None
    kind, ids, what = (
        (sub_accounts.ONLY, accounts, "accounts")
        if accounts is not None
        else (sub_accounts.EXCEPT, accounts_except, "accounts_except")
    )
    check_accounts(conn, ids, what=what)
    return True, (sub_accounts.dump(kind, ids) if ids else None)


def _set_accounts(conn, sub: SubRec, accounts_json: str | None, *, now_ms: int, audit: dict) -> None:
    """Ф38-1 п.8 + П4: фильтр по аккаунтам своей живой подписки на месте (зовётся, только если значение
    изменилось). Ждущие сигналы бесед, которых подписка больше не слышит, — `ended` (и карточек, и сообщений),
    как `_set_mode` делает с `pending` прежнего режима: иначе сессия получила бы сигнал беседы, которую уже не
    слушает, а дежурная эту подписку для той беседы пропускает. Аудит `sub_mode` (при снятии — `[]`)."""
    conn.execute("UPDATE main.session_sub SET accounts_json = ? WHERE id = ?", (accounts_json, sub.id))
    spec = sub_accounts.parse(accounts_json)
    for signal_id, account_id in conn.execute(
        "SELECT id, account_id FROM main.session_signal WHERE sub_id = ? AND status = 'pending'", (sub.id,)
    ).fetchall():
        if not sub_accounts.listens(spec, account_id):
            conn.execute(
                "UPDATE main.session_signal SET status = 'ended', updated_ts = ? WHERE id = ?",
                (now_ms, signal_id),
            )
    for signal_id, channel_id in conn.execute(
        "SELECT id, channel_id FROM main.session_chan_signal WHERE project_sub_id = ? AND status = 'pending'",
        (sub.id,),
    ).fetchall():
        if not sub_accounts.listens(spec, channel_account(conn, channel_id)):
            conn.execute(
                "UPDATE main.session_chan_signal SET status = 'ended', updated_ts = ? WHERE id = ?",
                (now_ms, signal_id),
            )
    write_audit(
        conn,
        now_ms=now_ms,
        action="sub_mode",
        result="ok",
        detail={"sub_id": sub.id, "accounts": spec if spec is not None else []},
        **audit,
    )


def channel_account(conn, channel_id: int) -> int | None:
    """Ф38-1: аккаунт беседы (`None` — беседы нет)."""
    row = conn.execute(
        f"SELECT account_id FROM {db_of(channel_id)}.channel WHERE id = ?",  # noqa: S608 — схема из `db_of`
        (channel_id,),
    ).fetchone()
    return row[0] if row is not None else None


# Ф36-1 A3: отказ `batch_min` без ключей — один текст у подписки на проект и на беседу. Ф67-1: интервал есть у
# любой подписки, отказа без ключей больше нет; константа — для старых тестов и клиентов.
BATCH_MIN_NO_KEYS = "batch_min — только вместе с keywords"
# Ф67-1: у `direct` (только сообщения агентов) пачки сигналов сообщений нет — у сообщений агентов своя.
BATCH_MIN_DIRECT = "batch_min — не для mode=direct (у сообщений агентов своя пачка: agent_batch_min)"


def check_batch_min(batch_min) -> None:
    """Ф36-1 A3: `batch_min` — целое 0..`keywords.BATCH_MIN_MAX` (минуты; 0 — сразу) или `None`."""
    if batch_min is None:
        return
    if (
        not isinstance(batch_min, int)
        or isinstance(batch_min, bool)
        or not 0 <= batch_min <= keywords_mod.BATCH_MIN_MAX
    ):
        raise SubError(422, f"batch_min: 0–{keywords_mod.BATCH_MIN_MAX} минут")


def _new_keywords(mode, msg_filter, keywords, exclude) -> str | None:
    """A3: проверка и нормализация ключей запроса -> `keywords_json` (`None` — ключей нет или сняты)."""
    if keywords is not None and (mode != "messages" or msg_filter != "personal"):
        raise SubError(422, 'ключи — только с mode="personal"')
    return keywords_spec(keywords, exclude)


def keywords_spec(keywords, exclude) -> str | None:
    """Ф25-1 A3 (и Ф31-1 решение 4 — подписка на беседу): нормализация ключей запроса -> `keywords_json`
    (`None` — ключей нет или сняты `keywords=[]`). `exclude` без `keywords` — 422."""
    if keywords is None:
        if exclude is not None:
            raise SubError(422, "exclude — только вместе с keywords")
        return None
    try:
        keys = keywords_mod.normalize(keywords, keywords_mod.KEYS_MAX, what="ключ")
        minus = keywords_mod.normalize(
            exclude if exclude is not None else [], keywords_mod.EXCLUDE_MAX, what="минус-слово"
        )
    except keywords_mod.Invalid as exc:
        raise SubError(422, str(exc)) from exc
    if not keys and minus:
        raise SubError(422, "keywords=[] снимает ключи — exclude при этом должен быть пуст")
    return keywords_mod.dump_spec(keys, minus)


def want_form(conn, value) -> dict:
    """Ф41-4 (2.1, П9): желаемый фильтр подписки сессии хаба (`hub_session.want_sub_json`) — проверка и
    нормализация ТЕМ ЖЕ кодом, что у `POST /subs` (режим, фильтр, ключи, интервал пачки, аккаунты); роль и
    номер — из записи сессии, здесь их нет. Возвращает полную форму `{"mode", "msg_filter", "keywords",
    "exclude", "batch_min", "accounts"}`; негодное — `SubError` 422 с готовым текстом. Пустые списки ключей —
    «ключей нет» (форма шлёт их всегда)."""
    if not isinstance(value, dict):
        raise SubError(422, "want_sub: объект или null")
    known = {"mode", "msg_filter", "keywords", "exclude", "batch_min", "accounts"}
    extra = sorted(str(key)[:32] for key in set(value) - known)
    if extra:
        raise SubError(422, f"want_sub: лишние поля {', '.join(extra[:5])}")
    mode = value.get("mode")
    if mode not in MODES:
        raise SubError(422, "mode: cards, messages или direct")
    msg_filter = value.get("msg_filter")
    if msg_filter is not None and msg_filter not in MSG_FILTERS:
        raise SubError(422, "msg_filter: all или personal")
    for name in ("keywords", "exclude"):
        items = value.get(name)
        if items is not None and not (isinstance(items, list) and all(isinstance(x, str) for x in items)):
            raise SubError(422, f"{name}: список строк")
    keywords = value.get("keywords") or None
    exclude = value.get("exclude") or None
    batch_min = value.get("batch_min")
    accounts = value.get("accounts")
    if mode == DIRECT:
        if msg_filter not in (None, "all") or keywords or exclude or batch_min is not None or accounts:
            raise SubError(422, "mode=direct — без msg_filter, keywords, exclude, batch_min и accounts")
        msg_filter = "all"
    elif mode == "cards":
        if msg_filter not in (None, "all"):
            raise SubError(422, "msg_filter personal — только с mode=messages")
        msg_filter = "all"
    else:
        msg_filter = msg_filter or "all"
    spec = keywords_mod.parse_spec(_new_keywords(mode, msg_filter, keywords, exclude))
    check_batch_min(batch_min)  # Ф67-1: интервал пачки — и без ключей, и у карточек
    only = except_ = None
    if accounts is not None:
        if not isinstance(accounts, dict) or len(accounts) != 1:
            raise SubError(422, 'accounts: {"only": [номера]} или {"except": [номера]}')
        if next(iter(accounts)) not in sub_accounts.KINDS:
            raise SubError(422, 'accounts: {"only": [номера]} или {"except": [номера]}')
        ((kind, ids),) = accounts.items()
        only, except_ = (ids, None) if kind == sub_accounts.ONLY else (None, ids)
    _given, acc_json = _new_accounts(conn, only, except_)
    return {
        "mode": mode,
        "msg_filter": msg_filter,
        "keywords": spec["keys"] if spec is not None else [],
        "exclude": spec["exclude"] if spec is not None else [],
        "batch_min": batch_min,
        "accounts": sub_accounts.parse(acc_json),
    }


def want_differs(want: dict, rec: SubRec) -> bool:
    """Ф41-4 П9: расходится ли живая подписка `rec` с желаемым фильтром `want` (нормализованным
    `want_form`). Интервал пачки сравнивается, только когда задан в `want` (иначе действует прежний или
    умолчание — как у `POST /subs` с ключами без `batch_min`)."""
    if mode_of(rec) != want.get("mode"):
        return True
    if rec.direct:
        return False
    if rec.msg_filter != want.get("msg_filter"):
        return True
    spec = keywords_mod.parse_spec(rec.keywords_json) or {"keys": [], "exclude": []}
    if spec["keys"] != (want.get("keywords") or []) or spec["exclude"] != (want.get("exclude") or []):
        return True
    batch_min = want.get("batch_min")
    if batch_min is not None and keywords_mod.batch_ms(rec.keywords_json) != batch_min * 60_000:
        return True  # Ф67-1: интервал — и у подписки без ключей
    return accounts_of(rec) != sub_accounts.clean(want.get("accounts"))


def _keys_audit(keywords_json: str | None) -> dict:
    """Аудит — только числа (тексты ключей в аудит не пишутся)."""
    if keywords_json is None:
        return {}
    keys, minus = keywords_mod.spec_counts(keywords_json)
    batch_min = keywords_mod.stored_batch(keywords_json)  # Ф36-1 A3: число интервала — рядом с числами ключей
    return {"keys": keys, "exclude": minus, **({"batch_min": batch_min} if batch_min is not None else {})}


def _set_mode(
    conn,
    sub: SubRec,
    mode: str,
    *,
    msg_filter: str = "all",
    keywords_json: str | None = None,
    direct: bool = False,
    now_ms: int,
    principal,
    client_name,
    request_id,
) -> None:
    """П20: смена режима своей живой подписки на месте. `pending` прежнего режима — `ended` (сигналы
    сообщений при `messages -> cards`, сигналы карточек при `cards -> messages`); в `cards` — добор
    карточек. Ф20-1 П1: смена одного фильтра (`all` <-> `personal`) — только строка подписки: ожидающий
    сигнал выдаётся как есть. Ф25-1 П5: смена одних ключей — так же, на месте. Ф36-1 B4: любое изменение
    `keywords_json` (ключи, минус-слова, интервал, снятие) обнуляет срок пачки ждущих строк — сигнал уходит
    ближайшей выдачей."""
    # Ф35-1 П11: `mode` здесь — значение СТОЛБЦА (`cards`|`messages`); `direct` — отдельный флаг, и переходы
    # `cards <-> direct` (столбец не меняется) ветвятся по нему.
    conn.execute(
        "UPDATE main.session_sub SET mode = ?, msg_filter = ?, keywords_json = ?, direct = ? WHERE id = ?",
        (mode, msg_filter, keywords_json, int(direct), sub.id),
    )
    if direct:
        # Ф38-1 п.8: в `direct` фильтра по аккаунтам нет — снимается молча.
        conn.execute("UPDATE main.session_sub SET accounts_json = NULL WHERE id = ?", (sub.id,))
    if keywords_json != sub.keywords_json:
        conn.execute(
            "UPDATE main.session_chan_signal SET ready_ts = NULL "
            "WHERE project_sub_id = ? AND status = 'pending' AND ready_ts IS NOT NULL",
            (sub.id,),
        )
    if direct and not sub.direct:
        # В `direct`: ждущие сигналы карточек и сообщений — `ended`.
        conn.execute(
            "UPDATE main.session_signal SET status = 'ended', updated_ts = ? "
            "WHERE sub_id = ? AND status = 'pending'",
            (now_ms, sub.id),
        )
        conn.execute(
            "UPDATE main.session_chan_signal SET status = 'ended', updated_ts = ? "
            "WHERE project_sub_id = ? AND status = 'pending'",
            (now_ms, sub.id),
        )
    elif direct:
        pass  # `direct` и был: сигналов проекта нет
    elif sub.direct:
        if mode == "cards":
            backfill(conn, sub.id, sub.project_id, now_ms=now_ms)  # из `direct` в `cards` — добор
    elif mode != sub.mode and mode == "cards":
        conn.execute(
            "UPDATE main.session_chan_signal SET status = 'ended', updated_ts = ? "
            "WHERE project_sub_id = ? AND status = 'pending'",
            (now_ms, sub.id),
        )
        backfill(conn, sub.id, sub.project_id, now_ms=now_ms)
    elif mode != sub.mode:
        conn.execute(
            "UPDATE main.session_signal SET status = 'ended', updated_ts = ? "
            "WHERE sub_id = ? AND status = 'pending'",
            (now_ms, sub.id),
        )
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=client_name,
        request_id=request_id,
        action="sub_mode",
        result="ok",
        detail={
            "sub_id": sub.id,
            "mode": DIRECT if direct else mode,
            **({"msg_filter": msg_filter} if msg_filter != "all" or sub.msg_filter != "all" else {}),
            **(
                dict(zip(("keys", "exclude"), keywords_mod.spec_counts(keywords_json), strict=True))
                if keywords_json is not None or sub.keywords_json is not None
                else {}
            ),
            **(
                {"batch_min": keywords_mod.stored_batch(keywords_json)}
                if keywords_mod.stored_batch(keywords_json) is not None
                else {}
            ),
        },
    )


# --- сигналы (B3) ---------------------------------------------------------------------------------------

_CARD_FROM = (
    "FROM {schema}.agent_item a "
    "JOIN {schema}.channel c ON c.id = a.channel_id "
    "JOIN {schema}.message lm ON lm.id = a.last_message_id "
    "LEFT JOIN {schema}.thread t ON t.channel_id = lm.channel_id AND t.ext_id = lm.thread_ext_id "
)
_REPLY_PRED = threads_mod.is_reply_sql("lm.thread_ext_id", "lm.ext_id")


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в agent_items.py/queries.py: схема и доверенные фрагменты подставляются здесь."""
    return template.format(schema=schema, **parts)


def _msg_count(message_ids_json: str | None) -> int:
    try:
        ids = json.loads(message_ids_json or "[]")
    except ValueError, TypeError:
        return 0
    return len(ids) if isinstance(ids, list) else 0


def _live_card_subs(conn, project_id: int) -> list[SubRec]:
    """Ф26-1 A3: живые подписки проекта в режиме `cards` (Ф14-5 П20: режиму `messages` сигналы карточек не
    идут) — каждой своя копия сигнала."""
    rows = conn.execute(
        f"SELECT {_SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL "  # noqa: S608
        "AND mode = 'cards' AND direct = 0 ORDER BY id",
        (project_id,),
    ).fetchall()
    return [SubRec(*row) for row in rows]


def chan_sub_of_session(conn, channel_id: int, rec) -> bool:
    """Ф14-5 П1 + Ф26-1 A3: у беседы живая подписка на беседу ТОЙ ЖЕ сессии, что держит подписку `rec` на
    проект (ключ сессии — токен, pid, starttime, `session_id`): она главнее подписки этой сессии на проект.
    Подписки других сессий беседу слушают как обычно."""
    row = conn.execute(
        "SELECT 1 FROM main.session_chan_sub WHERE channel_id = ? AND ended_ts IS NULL AND api_token_id = ? "
        "AND session_pid = ? AND session_proc_start = ? AND session_id = ?",
        (channel_id, rec.api_token_id, rec.session_pid, rec.session_proc_start, rec.session_id),
    ).fetchone()
    return row is not None


def _put_signal(
    conn,
    *,
    sub_id: int,
    item_id: int,
    channel_id: int,
    project_id: int,
    account_id: int,
    label: str,
    covered_seq: int,
    msg_count: int,
    now_ms: int,
) -> bool:
    """Есть `pending` на (подписка, карточка) — обновить метку, покрытие, число сообщений; нет — вставить.
    `True` — вставлен новый."""
    row = conn.execute(
        "SELECT id FROM main.session_signal WHERE sub_id = ? AND item_id = ? AND status = 'pending'",
        (sub_id, item_id),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE main.session_signal SET label = ?, covered_seq = ?, msg_count = ?, channel_id = ?, "
            "account_id = ?, updated_ts = ? WHERE id = ?",
            (label, covered_seq, msg_count, channel_id, account_id, now_ms, row[0]),
        )
        return False
    conn.execute(
        "INSERT INTO main.session_signal (sub_id, item_id, channel_id, project_id, account_id, label, "
        "covered_seq, msg_count, status, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (sub_id, item_id, channel_id, project_id, account_id, label, covered_seq, msg_count, now_ms, now_ms),
    )
    return True


def signal_for_item(conn, item_id: int, *, now_ms: int) -> bool:
    """B3: после `agent_items.upsert` в той же транзакции — по ИТОГОВОЙ строке карточки (метка, покрытие,
    `message_ids` дочитываются здесь). Карточка `new`, метка `urgent|reply`, беседа в проекте живой
    подписки -> сигнал. `True` — сигнал вставлен или обновлён."""
    schema = db_of(item_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT a.status, a.label, a.covered_seq, a.message_ids_json, a.channel_id, c.project_id, "
            "c.account_id FROM {schema}.agent_item a JOIN {schema}.channel c ON c.id = a.channel_id "
            "WHERE a.id = ?",
        ),
        (item_id,),
    ).fetchone()
    if row is None:
        return False
    status, label, covered_seq, ids_json, channel_id, project_id, account_id = row
    if status != "new" or label not in SIGNAL_LABELS:
        return False
    from . import service_bot as service_bot_mod

    if channel_id in service_bot_mod.channel_ids(conn):
        return False  # Ф54-2 (П2): по служебной личке с ботом хаба сессиям сигналов нет
    if project_id is None:
        # Ф52-2 П2: беседа без проекта — сигнал подпискам КАЖДОГО проекта, слышащего «Неразобранное» её
        # аккаунта (`_live_card_subs` режим `direct` не берёт); в строке сигнала — проект подписки.
        targets = [pid for pid, _muted in unsorted_mod.listening_projects(conn, account_id)]
    else:
        targets = [project_id]
    put = False
    for sub in (s for pid in targets for s in _live_card_subs(conn, pid)):
        if project_id is None and not unsorted_mod.token_sees(conn, sub.api_token_id):
            continue  # ревью Ф52-2: токен подписки сам «Неразобранное» не видит — сигнала нет
        if not listens_account(sub, account_id):
            continue  # Ф38-1 п.2: аккаунт беседы вне фильтра подписки
        if chan_sub_of_session(conn, channel_id, sub):
            continue  # Ф26-1 A3: беседу эта сессия слушает подпиской на беседу
        _put_signal(
            conn,
            sub_id=sub.id,
            item_id=item_id,
            channel_id=channel_id,
            project_id=sub.project_id,
            account_id=account_id,
            label=label,
            covered_seq=covered_seq,
            msg_count=_msg_count(ids_json),
            now_ms=now_ms,
        )
        put = True
    return put


def backfill(
    conn,
    sub_id: int,
    project_id: int,
    *,
    now_ms: int,
    channel_id: int | None = None,
    heard_before: tuple[str | None] | None = None,
) -> int:
    """B3, блокер 1: при подписке — `pending` на открытые карточки проекта: `new`, `urgent|reply`, не
    отвечены, `updated_ts` не старше 6 ч. Возвращает число вставленных.

    Ф14-5: подписке в режиме `messages` — ничего (П20); беседы с живой подпиской на беседу ТОЙ ЖЕ сессии
    пропускаются (П1, Ф26-1 A3); `channel_id` — добор только по этой беседе (П19: подписку на беседу
    сняли).

    Ф38-1 (п.2, П5): у подписки с фильтром по аккаунтам — только карточки бесед, которые она слышит;
    `heard_before` — `(прежний accounts_json,)` при смене фильтра на месте: добираются только карточки
    аккаунтов, которых подписка раньше НЕ слышала."""
    sub = get_sub(conn, sub_id)
    if sub is None or sub.mode != "cards" or sub.direct:
        return 0  # Ф35-1: `direct` карточек не получает — и добора нет
    count = 0
    only = "" if channel_id is None else "AND a.channel_id = ? "
    params: tuple = (
        (project_id, now_ms - BACKFILL_MAX_AGE_MS)
        + (() if channel_id is None else (channel_id,))
        + (sub.api_token_id, sub.session_pid, sub.session_proc_start, sub.session_id)
    )
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT a.id, a.channel_id, c.account_id, a.label, a.covered_seq, a.message_ids_json "  # noqa: S608
                + _CARD_FROM
                + "WHERE c.project_id = ? AND a.status = 'new' AND a.label IN ('urgent', 'reply') "
                "AND a.updated_ts >= ? AND lm.hidden = 0 AND NOT " + agent_items_mod.ANSWERED_SQL + " "
                "{only}AND NOT EXISTS (SELECT 1 FROM main.session_chan_sub cs "
                "WHERE cs.channel_id = a.channel_id AND cs.ended_ts IS NULL AND cs.api_token_id = ? "
                "AND cs.session_pid = ? AND cs.session_proc_start = ? AND cs.session_id = ?) "
                "ORDER BY a.updated_ts, a.id",
                only=only,
            ),
            params,
        ).fetchall()
        for item_id, channel_id_, account_id, label, covered_seq, ids_json in rows:
            if not listens_account(sub, account_id):
                continue
            if heard_before is not None and sub_accounts.listens(
                sub_accounts.parse(heard_before[0]), account_id
            ):
                continue
            if _put_signal(
                conn,
                sub_id=sub_id,
                item_id=item_id,
                channel_id=channel_id_,
                project_id=project_id,
                account_id=account_id,
                label=label,
                covered_seq=covered_seq,
                msg_count=_msg_count(ids_json),
                now_ms=now_ms,
            ):
                count += 1
    return count


def pending_count(conn, sub_id: int) -> int:
    return _pending_counts(conn, [sub_id]).get(sub_id, 0)


def wait_state(
    conn,
    sub_id: int,
    *,
    now_ms: int,
    peers: bool = False,
    drafts: bool = False,
    presses: bool = False,
    cards: bool = False,
    humans: bool = False,
    group_ops: bool = False,
) -> tuple[int, int]:
    """Читатель, для ожидания (Ф14-6b П1): `(готовых к выдаче pending, мс до конца удержания ближайшего
    сигнала сообщений, ждущего расшифровку — 0, если таких нет)`. Сигналы карточек (`cards`) не держатся:
    карточку уже задержал разборщик. Ф35-2 (решение 7): `peers` — клиент понимает сигналы сообщений агентов
    (протокол ≥ 4); иначе они не считаются (и `deliver` их не выдаёт) — ожидание спит, а не крутится.
    Ф39 B5: `drafts` — так же сигналы исходов черновиков (клиент протокола ≥ 5). Ф44-1c: `presses` — так же
    сигналы исходов нажатий кнопок ботов (клиент протокола ≥ 6). `cards` — клиент протокола ≥ 7 (Ф49-1); с
    Ф66-1 сигналов `card` (делегату) нет — флаг принимается и ничего не добавляет. Ф54-1 (П11): `humans` — так
    же сигналы исходов заявок «нужен человек» (клиент протокола ≥ 8). Ф62-6: `group_ops` — так же сигналы
    исходов операций над группами WhatsApp (клиент протокола ≥ 9)."""
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль
    from . import draft_outcomes as draft_outcomes_mod
    from . import group_ops as group_ops_mod
    from . import human_requests as human_requests_mod
    from . import peer_msgs as peer_msgs_mod
    from . import presses as presses_mod
    from . import reminders as reminders_mod  # и `reminders` — тоже

    sub = get_sub(conn, sub_id)
    # Ф67-1: сигналы карточек подписки с пачкой ждут срока пачки (срочные и личные — нет).
    batch = card_batch_ms(sub) if sub is not None else 0
    triage = 0
    card_held = 0
    for item_id, created_ts, label in conn.execute(
        "SELECT item_id, created_ts, label FROM main.session_signal WHERE sub_id = ? AND status = 'pending'",
        (sub_id,),
    ).fetchall():
        left = card_hold_ms(conn, item_id, created_ts, label, batch, now_ms=now_ms)
        if left <= 0:
            triage += 1
        elif card_held == 0 or left < card_held:
            card_held = left
    ready, held = chan_subs.ready_state(conn, project_sub_id=sub_id, now_ms=now_ms)
    if card_held and (not held or card_held < held):
        held = card_held
    # Ф34-2 П1: сработавшие напоминания роли подписки, ещё не отданные сигналом, — сигнал — сама строка.
    live = sub is not None and sub.ended_ts is None
    reminders = reminders_mod.signal_count(conn, sub) if live else 0
    peer = peer_msgs_mod.signal_count(conn, sub, now_ms=now_ms) if live and peers else 0
    if live and peers:
        # Ф48-4: сообщения агентов, ждущие срока пачки, в готовые не входят — ожидание спит до срока.
        _later, peer_ms = peer_msgs_mod.batch_state(conn, sub, now_ms=now_ms)
        if peer_ms and (not held or peer_ms < held):
            held = peer_ms
    outcomes = draft_outcomes_mod.signal_count(conn, sub, now_ms=now_ms) if live and drafts else 0
    pressed = presses_mod.signal_count(conn, sub, now_ms=now_ms) if live and presses else 0
    called = human_requests_mod.signal_count(conn, sub, now_ms=now_ms) if live and humans else 0
    grouped = group_ops_mod.signal_count(conn, sub, now_ms=now_ms) if live and group_ops else 0
    return triage + ready + reminders + peer + outcomes + pressed + called + grouped, held


def card_batch_ms(sub: SubRec) -> int:
    """Ф67-1: интервал пачки сигналов карточек подписки (мс; 0 — сразу): только у режима `cards`."""
    if sub.mode != "cards" or sub.direct:
        return 0
    return keywords_mod.batch_ms(sub.keywords_json)


def card_hold_ms(
    conn, item_id: int, created_ts: int, label: str | None, batch_ms: int, *, now_ms: int
) -> int:
    """Ф67-1: сколько мс сигнал карточки ещё ждёт срока пачки подписки (0 — готов). Срок — тот же узел сетки,
    что у сигналов сообщений (`chan_subs.batch_ready_ts` от `created_ts`); срочная карточка (`urgent`) —
    сразу и, как личное, забирает накопленное (выдача отдаёт все ждущие). 21.10: личная карточка
    (`personal_card`) — тоже сразу, как обещает `hub_subscribe` («личное и срочное — всегда сразу»)."""
    if batch_ms <= 0 or label == "urgent":
        return 0
    from . import chan_subs  # здесь: `chan_subs` сам импортирует этот модуль

    left = max(0, chan_subs.batch_ready_ts(created_ts, batch_ms) - now_ms)
    if left and personal_card(conn, item_id):
        return 0
    return left


def personal_card(conn, item_id: int) -> bool:
    """21.10 (личка коллеги ждала дежурную 5 минут): карточка ЛИЧНАЯ — среди покрытых ею сообщений есть чужое
    не от бота в личке (или групповой личке Slack), с прямым упоминанием владельца или настоящий ответ в его треде
    (подписанном, не broadcast) или на его сообщение — те же категории `dm`/`mention`/`thread`, что
    `chan_subs.message_cat` у сигналов сообщений, и те же «личка или упоминание не от бота», что приоритет
    разборщика (`agent.dispatcher.is_priority`). По сообщениям, а не по `action`: при слиянии карточки
    `action` переписывается последним разбором. Только срочность подачи сессии хаба (`held_signals`); метку
    карточки `urgent|reply` для интерфейса и уведомлений не трогает."""
    schema = db_of(item_id)
    real_reply = threads_mod.real_reply_sql("m.thread_ext_id", "m.ext_id")
    row = conn.execute(
        _sql(
            schema,
            "SELECT EXISTS (SELECT 1 FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id "
            "LEFT JOIN {schema}.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
            "WHERE m.id IN (SELECT value FROM json_each(a.message_ids_json)) AND m.channel_id = a.channel_id "
            "AND m.is_me = 0 AND m.is_bot = 0 AND (c.kind = 'dm' "
            "OR json_extract(c.meta_json, '$.attrs.slack_type') = 'mpim' OR {mention} "
            "OR ({real_reply} AND m.thread_broadcast = 0 AND COALESCE(t.subscribed, 0) = 1) "
            "OR (m.reply_to_ext_id IS NOT NULL AND EXISTS (SELECT 1 FROM {schema}.message r "
            "WHERE r.channel_id = m.channel_id AND r.ext_id = m.reply_to_ext_id AND r.is_me = 1)))) "
            "FROM {schema}.agent_item a WHERE a.id = ?",
            mention=threads_mod.direct_mention_sql("m"),
            real_reply=real_reply,
        ),
        (item_id,),
    ).fetchone()
    return bool(row and row[0])


def cards_state(
    conn,
    sub: SubRec,
    *,
    visible_projects: frozenset[int] | None,
    presence: list[dict],
    now_ms: int,
) -> tuple[int, int, int, int]:
    """Ф67-1, читатель: как `valid_cards`, но с пачкой — `(готовых сейчас, ждущих срока пачки, мс до
    ближайшего срока — 0, если ждущих нет, сколько из готовых — личные карточки (`personal_card`))`."""
    batch = card_batch_ms(sub)
    rows = conn.execute(
        "SELECT item_id, created_ts, label FROM main.session_signal WHERE sub_id = ? AND status = 'pending'",
        (sub.id,),
    ).fetchall()
    ready = waiting = nearest = personal = 0
    for item_id, created_ts, label in rows:
        if (
            _skip_reason(conn, item_id=item_id, sub=sub, visible_projects=visible_projects, presence=presence)
            is not None
        ):
            continue
        left = card_hold_ms(conn, item_id, created_ts, label, batch, now_ms=now_ms)
        if left <= 0:
            ready += 1
            if personal_card(conn, item_id):
                personal += 1
        else:
            waiting += 1
            if nearest == 0 or left < nearest:
                nearest = left
    return ready, waiting, nearest, personal


def valid_cards(conn, sub: SubRec, *, visible_projects: frozenset[int] | None, presence: list[dict]) -> int:
    """Ф41-4 П2, читатель: ждущие сигналы карточек подписки, ГОДНЫЕ к выдаче сейчас (тем же `_skip_reason`,
    что выдача: закрытая, отвеченная, прочитанная, перенесённая карточка и беседа перед глазами владельца не
    считаются)."""
    rows = conn.execute(
        "SELECT item_id FROM main.session_signal WHERE sub_id = ? AND status = 'pending'", (sub.id,)
    ).fetchall()
    return sum(
        1
        for (item_id,) in rows
        if _skip_reason(conn, item_id=item_id, sub=sub, visible_projects=visible_projects, presence=presence)
        is None
    )


def unsorted_hears(
    conn, sub, account_id, visible_projects: frozenset[int] | None, api_token_id: int | None = None
) -> bool:
    """Ф52-2 П2: сигнал подписки `sub` по беседе БЕЗ проекта годен — проект подписки слышит «Неразобранное»
    аккаунта беседы и сам виден токену. Одно определение для карточек (`_skip_reason`) и сообщений
    (`chan_subs._project_skip_reason`).

    Правка по ревью: и токен подписки, и токен запроса выдачи (`api_token_id`; у читателей-счётчиков его нет)
    сами видят «Неразобранное» (`unsorted.token_sees`) — проектному токену такой сигнал не выдаётся."""
    if sub.direct or (visible_projects is not None and sub.project_id not in visible_projects):
        return False
    if not unsorted_mod.token_sees(conn, sub.api_token_id):
        return False
    if api_token_id is not None and not unsorted_mod.token_sees(conn, api_token_id):
        return False
    return unsorted_mod.hears(conn, sub.project_id, account_id)


def channel_unsorted(conn, channel_id: int) -> bool:
    """Ф52-2: беседа сейчас без проекта (пометка `unsorted` выданного сигнала)."""
    row = conn.execute(
        f"SELECT project_id FROM {db_of(channel_id)}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    return row is not None and row[0] is None


def _skip_reason(
    conn,
    *,
    item_id: int,
    sub: SubRec,
    visible_projects: frozenset[int] | None,
    presence: list[dict],
    api_token_id: int | None = None,
) -> str | None:
    """B3 «годность при выдаче»: причина из `SKIP_REASONS` или `None` (годен)."""
    schema = db_of(item_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT a.status, a.channel_id, c.project_id, c.account_id, lm.hidden, "
            + agent_items_mod.ANSWERED_SQL
            + ", "
            + agent_items_mod.READ_SQL
            + ", CASE WHEN {reply_pred} AND lm.thread_broadcast = 0 THEN lm.thread_ext_id END "
            + _CARD_FROM
            + "WHERE a.id = ?",
            reply_pred=_REPLY_PRED,
        ),
        (item_id,),
    ).fetchone()
    if row is None:
        return "closed"
    status, channel_id, project_id, account_id, hidden, answered, read, thread = row
    if status != "new" or hidden:
        return "closed"
    if chan_sub_of_session(conn, channel_id, sub):
        # Ф14-5 П1: беседу слушает подписка на беседу этой же сессии (Ф26-1 A3) — она главнее (добор П19 при
        # повторной подписке, `pending`, лёгший до подписки на беседу).
        return "chan_sub"
    if answered:
        return "answered"
    if read:
        return "read"
    if project_id is None:
        # Ф52-2 П2: беседа без проекта годна, пока проект подписки слышит «Неразобранное» её аккаунта (право
        # проверяется и здесь: владелец снял его — ждущие не выдаются; беседу разложили — ветка ниже).
        if not unsorted_hears(conn, sub, account_id, visible_projects, api_token_id):
            return "moved"
    elif project_id != sub.project_id or (
        visible_projects is not None and project_id not in visible_projects
    ):
        return "moved"
    if is_visible(presence, channel_id, thread):
        return "present"
    return None


def deliver(
    conn,
    sub_id: int,
    *,
    now_ms: int,
    visible_projects: frozenset[int] | None,
    presence: list[dict],
    api_token_id: int,
    client_name: str | None,
    request_id: str,
    peers: bool = False,
    drafts: bool = False,
    presses: bool = False,
    cards: bool = False,
    humans: bool = False,
    group_ops: bool = False,
) -> list[dict] | None:
    """B4, выдача одной транзакцией писателя: годные (не больше 20, старшие первыми) -> `delivered`,
    негодные -> `skipped` с причиной; при выдаче — `last_delivery_ts`, `last_wait_ts` и аудит `sub_signal` на
    каждый выданный. Подписка уже завершена — `None`. Возвращает выданные сигналы (только числа и метка).

    Ф34-2 П1: в ОБОИХ режимах следом — сигналы сработавших напоминаний роли подписки (`{"kind":
    "reminder", …}`, `reminders.take_signals`), в общий предел выдачи.

    Ф35-2 (решения 6–7): при `peers` (клиент протокола ≥ 4) после напоминаний — сигналы сообщений агентов
    (`{"kind": "peer", …}`, `peer_msgs.take_signals`), в тот же предел.

    Ф39 B5: при `drafts` (клиент протокола ≥ 5) после сигналов агентов — исходы черновиков подписки
    (`{"kind": "draft", …}`, `draft_outcomes.take_signals`), в тот же предел.

    Ф44-1c: при `presses` (клиент протокола ≥ 6) следом — исходы нажатий кнопок ботов, шедших карточкой
    (`{"kind": "press", …}`, `presses.take_signals`), в тот же предел.

    `cards` (клиент протокола ≥ 7, Ф49-1): с Ф66-1 сигналов `card` (карточки делегату) нет — флаг
    принимается и ничего не выдаёт.

    Ф54-1 (П11): при `humans` (клиент протокола ≥ 8) следом — исходы заявок «нужен человек» линии подписки
    (`{"kind": "human", …}`, `human_requests.take_signals`), в тот же предел.

    Ф62-6: при `group_ops` (клиент протокола ≥ 9) следом — исходы операций над группами WhatsApp, заявленных
    подпиской (`{"kind": "group_op", …}`, `group_ops.take_signals`), в тот же предел."""
    from . import draft_outcomes as draft_outcomes_mod
    from . import group_ops as group_ops_mod
    from . import human_requests as human_requests_mod
    from . import peer_msgs as peer_msgs_mod
    from . import presses as presses_mod
    from . import reminders as reminders_mod  # здесь: `reminders` сам импортирует этот модуль

    sub = get_sub(conn, sub_id)
    if sub is None or sub.ended_ts is not None:
        return None
    delivered = _deliver_signals(
        conn,
        sub,
        now_ms=now_ms,
        visible_projects=visible_projects,
        presence=presence,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id,
    )
    extra = reminders_mod.take_signals(
        conn,
        sub,
        now_ms=now_ms,
        limit=MAX_SIGNALS_PER_DELIVERY - len(delivered),
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id,
    )
    if peers:
        # Ф48-4 п.2: накопленное в пачке уходит, только когда сессия этой выдачей всё равно просыпается.
        woken = bool(
            delivered
            or extra
            or (drafts and draft_outcomes_mod.signal_count(conn, sub, now_ms=now_ms))
            or (presses and presses_mod.signal_count(conn, sub, now_ms=now_ms))
            or (humans and human_requests_mod.signal_count(conn, sub, now_ms=now_ms))
            or (group_ops and group_ops_mod.signal_count(conn, sub, now_ms=now_ms))
        )
        extra += peer_msgs_mod.take_signals(
            conn,
            sub,
            now_ms=now_ms,
            limit=MAX_SIGNALS_PER_DELIVERY - len(delivered) - len(extra),
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            woken=woken,
        )
    if drafts:
        extra += draft_outcomes_mod.take_signals(
            conn,
            sub,
            now_ms=now_ms,
            limit=MAX_SIGNALS_PER_DELIVERY - len(delivered) - len(extra),
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
        )
    if presses:
        extra += presses_mod.take_signals(
            conn,
            sub,
            now_ms=now_ms,
            limit=MAX_SIGNALS_PER_DELIVERY - len(delivered) - len(extra),
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
        )
    if humans:
        extra += human_requests_mod.take_signals(
            conn,
            sub,
            now_ms=now_ms,
            limit=MAX_SIGNALS_PER_DELIVERY - len(delivered) - len(extra),
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
        )
    if group_ops:
        extra += group_ops_mod.take_signals(
            conn,
            sub,
            now_ms=now_ms,
            limit=MAX_SIGNALS_PER_DELIVERY - len(delivered) - len(extra),
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
        )
    if extra:
        conn.execute(
            "UPDATE main.session_sub SET last_delivery_ts = ?, last_wait_ts = ? WHERE id = ?",
            (now_ms, now_ms, sub_id),
        )
    return delivered + extra


def _deliver_signals(
    conn,
    sub: SubRec,
    *,
    now_ms: int,
    visible_projects: frozenset[int] | None,
    presence: list[dict],
    api_token_id: int,
    client_name: str | None,
    request_id: str,
) -> list[dict]:
    """Выдача сигналов карточек (`cards`) или сообщений (`messages`) живой подписке `sub`."""
    sub_id = sub.id
    if sub.mode == "messages":
        # Ф14-5 П20: сигналы сообщений (импорт здесь: `chan_subs` сам импортирует этот модуль).
        from . import chan_subs

        return chan_subs.deliver_project_messages(
            conn,
            sub,
            now_ms=now_ms,
            visible_projects=visible_projects,
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
        )
    rows = conn.execute(
        "SELECT id, item_id, channel_id, account_id, label, covered_seq, msg_count, created_ts "
        "FROM main.session_signal WHERE sub_id = ? AND status = 'pending' ORDER BY created_ts, id",
        (sub_id,),
    ).fetchall()
    # Ф67-1 (как Ф36-1 П5): выдача отдаёт все ждущие, не глядя на срок пачки, — готовые первыми.
    batch = card_batch_ms(sub)
    pending = [
        row[:7]
        for row in sorted(
            rows, key=lambda r: card_hold_ms(conn, r[1], r[7], r[4], batch, now_ms=now_ms) > 0
        )
    ]
    delivered: list[dict] = []
    for signal_id, item_id, channel_id, account_id, label, covered_seq, msg_count in pending:
        reason = _skip_reason(
            conn,
            item_id=item_id,
            sub=sub,
            visible_projects=visible_projects,
            presence=presence,
            api_token_id=api_token_id,
        )
        if reason is not None:
            conn.execute(
                "UPDATE main.session_signal SET status = 'skipped', reason = ?, updated_ts = ? WHERE id = ?",
                (reason, now_ms, signal_id),
            )
            continue
        if len(delivered) >= MAX_SIGNALS_PER_DELIVERY:
            continue  # остаются `pending` — следующей выдачей
        conn.execute(
            "UPDATE main.session_signal SET status = 'delivered', delivered_ts = ?, updated_ts = ? "
            "WHERE id = ?",
            (now_ms, now_ms, signal_id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            action="sub_signal",
            result="delivered",
            channel_id=channel_id,
            detail={
                "sub_id": sub_id,
                "item_id": item_id,
                "covered_seq": covered_seq,
                "label": label,
                "account_id": account_id,
                "msg_count": msg_count,
            },
        )
        delivered.append(
            {
                "item_id": item_id,
                "channel_id": channel_id,
                "account_id": account_id,
                "label": label,
                "msg_count": msg_count,
                # Ф52-2 П2: беседа без проекта — клиент пишет строку «неразобранное (аккаунт N): …».
                **({"unsorted": True} if channel_unsorted(conn, channel_id) else {}),
            }
        )
    if delivered:
        conn.execute(
            "UPDATE main.session_sub SET last_delivery_ts = ?, last_wait_ts = ? WHERE id = ?",
            (now_ms, now_ms, sub_id),
        )
    return delivered


def touch_wait(conn, sub_id: int, *, now_ms: int) -> None:
    """Конец ожидания без выдачи (таймаут, клиент ушёл) — `last_wait_ts`. Пишется только в конце ожидания:
    запись будит всех ожидающих (`on_commit`)."""
    conn.execute(
        "UPDATE main.session_sub SET last_wait_ts = ? WHERE id = ? AND ended_ts IS NULL", (now_ms, sub_id)
    )


def debounce_left_ms(sub: SubRec, now_ms: int) -> int:
    """Сколько ещё ждать до следующей выдачи (0 — можно сейчас). Первая выдача — сразу; часы назад —
    считаем, что можно."""
    last = sub.last_delivery_ts
    if last is None:
        return 0
    # Ф14-5: режим «каждое сообщение» — окно подписок на беседы (5 с), `cards` — 60 с (13-1).
    # Ф35-1: `direct` — тоже 5 с (сообщения агентов и напоминания, не карточки).
    window = CHAN_DEBOUNCE_MS if sub.mode == "messages" or sub.direct else SUB_DEBOUNCE_MS
    elapsed = now_ms - last
    if elapsed < 0 or elapsed >= window:
        return 0
    return window - elapsed


# --- поле `signal` в `GET /agent/items` -----------------------------------------------------------------


def attach_signals(conn, items: list[dict]) -> None:
    """B4: у каждой карточки — последний сигнал по ЖИВОЙ (незавершённой) подписке её проекта или `None`
    (`signal`). Ф26-1 A5: `signals` — последний сигнал КАЖДОЙ живой подписки проекта карточки (по номеру
    подписки); по нему hub-wake решает, покрыта ли карточка хоть одной сессией."""
    for item in items:
        item["signal"] = None
        item["signals"] = []
    if not items:
        return
    by_id = {item["id"]: item for item in items}
    ids = list(by_id)
    per_sub: dict[int, dict[int, dict]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT s.id, s.item_id, s.sub_id, s.status, s.reason, s.created_ts, s.delivered_ts, "  # noqa: S608
            "s.covered_seq, u.project_id FROM main.session_signal s "
            "JOIN main.session_sub u ON u.id = s.sub_id "
            f"WHERE u.ended_ts IS NULL AND s.item_id IN ({placeholders}) ORDER BY s.id",
            chunk,
        ).fetchall()
        for _sid, item_id, sub_id, status, reason, created_ts, delivered_ts, covered_seq, project_id in rows:
            item = by_id[item_id]
            if item.get("project_id") != project_id:
                continue
            signal = {
                "sub_id": sub_id,
                "status": status,
                "reason": reason,
                "created_ts": created_ts,
                "delivered_ts": delivered_ts,
                "covered_seq": covered_seq,
            }
            item["signal"] = signal
            per_sub.setdefault(item_id, {})[sub_id] = signal
    for item_id, signals in per_sub.items():
        by_id[item_id]["signals"] = [signals[k] for k in sorted(signals)]
