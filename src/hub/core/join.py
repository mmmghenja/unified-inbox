"""Заявки сессий на вступление в группы и каналы Telegram (Ф17-1, docs/architecture.md–A5 и поправки П1–П7 — они главнее
текста выше них).

Сессия просит (`create`, bearer `draft`, запреты `check_attach`), коннектор сначала смотрит, что это (шаг
`peek`), владелец жмёт «Вступить» / «Отказать» (`decide`, только UI) или вступает выключатель проекта (решение 4),
коннектор вступает (шаг `join`). Очередь «ядро -> коннектор» — по образцу `read_ack` (`lease`/`wait_state`/
`report`), но темп держит ядро (П3): между предпросмотрами аккаунта `PEEK_GAP_MS`, между вступлениями
`JOIN_GAP_MS` (автоматическими — `AUTO_GAP_MS`), потолки за скользящие сутки; FloodWait ставит паузу всем
открытым строкам аккаунта — она переживает рестарт.

Беседа, появившаяся в хабе из-за вступления (`channel.first_seen_ts` не раньше первой выдачи шага `join`), — в
проект заявки вручную и заглушённой (решение 5, `bind`); зовётся из отчёта и из `ingest._create_channel`
(`on_channel_created`), итог один при любом порядке (A3, П4). Форум (П1): сообщения идут в темы, родитель
пуст — темы глушатся явно, проект идёт за родителем сам.

Ф62-6 (docs/architecture.md и П2, П3, П12, П14): то же для групп WhatsApp — цель только
ссылка-приглашение `chat.whatsapp.com/<код>`, аккаунт WhatsApp; свои суточные потолки; выключатель проекта
«вступать сразу» на WhatsApp не действует (П2); вступившая группа WhatsApp НЕ глушится (П12).

Каждая запись в строку заявки двигает глобальный `seq` (П6): `/sync` и кадр SSE `join`. Пишет только внутри
`Db.write()`, время — параметром `now_ms`. Импорт `queries` здесь нельзя (`queries` -> `ingest` -> этот
модуль): видимость проекта токену — по `principal.project_ids`.
"""

import json
import re
import sqlite3
from datetime import datetime
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

from . import chan_subs, mapping, subs
from . import held as held_mod
from . import leave as leave_mod
from .audit import write_audit
from .db import db_of, next_seq
from .new_recipient import _USERNAME_RE  # noqa: PLC2701 — те же правила имени, что у @username Ф12-1
from .security import check_attach

# fake — для тестов и стенда (`scripts/dev-demo.py`), как у `read_ack`.
SOURCES = frozenset({"telegram", "whatsapp", "fake"})
# Ф62-6 П2: выключатель проекта «вступать сразу» — только Telegram (и fake для тестов).
AUTO_SOURCES = frozenset({"telegram", "fake"})
POLICY_KEY = "join"
MODES = ("confirm", "auto")

OPEN_STATUSES = ("checking", "pending", "approved")
FINAL_STATUSES = ("joined", "requested", "already", "failed", "declined")
STATUSES = (*OPEN_STATUSES, *FINAL_STATUSES)
JOIN_RESULTS = frozenset({"joined", "already", "requested"})
PEEK_KINDS = frozenset({"broadcast", "megagroup", "group", "wa_group"})
REPORT_OUTCOMES = frozenset({"done", "retry", "drop"})

REASON_MAX = 500
TARGET_INPUT_MAX = 300
TITLE_MAX = 256
ABOUT_MAX = 300
ERROR_MAX = 200
EXT_ID_MAX = 64

_MIN_MS = 60_000
DAY_MS = 24 * 60 * _MIN_MS
# Решение 7: от спама карточками.
OPEN_PER_PROJECT = 20
NEW_PER_PROJECT_DAY = 50
DECLINE_MEMORY_MS = 7 * DAY_MS
# Решение 4: автоматические вступления — на аккаунт.
AUTO_DAILY = 5
AUTO_GAP_MS = 10 * _MIN_MS
# П3: темп всех шагов аккаунта.
PEEK_GAP_MS = 20_000
JOIN_GAP_MS = 2 * _MIN_MS
PEEK_DAILY = 50
JOIN_DAILY = 20
# Ф62-6 (E1): WhatsApp — номер личный, бан дороже: свои потолки (Telegram не меняется).
DAILY_BY_SOURCE = {"whatsapp": (30, 10)}
# Очередь — как `read_ack`; лиз с запасом над 60 с шага коннектора (как `msg_ops`).
LEASE_MS = 120_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_MS = 30_000
MAX_LEASE_LIMIT = 10
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})

LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200

BIND_REASON = "вступление по заявке #{id}"
# Ревью Ф17-1 (находка 1): беседа, появившаяся позже этого срока после вступления, заявкой не привязывается —
# иначе группа, в которую владелец через месяц вступил сам, уехала бы в проект сессии заглушённой. Заявка у
# админов ждёт их одобрения дольше.
BIND_WINDOW_MS = DAY_MS
BIND_WINDOW_REQUESTED_MS = 14 * DAY_MS
_BIND_REASON_RE = re.compile(r"^вступление по заявке #(\d+)$")


class JoinError(Exception):
    """Базовая ошибка; текст — для человека/сессии, без названий из предпросмотра."""


class Invalid(JoinError):
    """422: цель, аккаунт или поля отчёта не годятся."""


class Denied(JoinError):
    """403: этому токену просить нельзя."""


class NotFound(JoinError):
    """404: заявки/проекта нет, не виден принципалу или чужой аккаунт."""


class Conflict(JoinError):
    """409: цель уже просит другой проект, владелец отказал недавно, решение не к этому статусу."""


class TooMany(JoinError):
    """429: лимиты заявок проекта (решение 7)."""


class StaleLease(JoinError):
    """409: `lease_gen` не совпал или шаг уже закрыт — задание перехвачено новой выдачей."""


# --- A2: разбор цели ----------------------------------------------------------------------------------------

_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_DIGITS_RE = re.compile(r"^\d+$")
_TG_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})
# Первые сегменты ссылок t.me, которые не имя канала/группы (служебные страницы Telegram).
_RESERVED = frozenset(
    {
        "joinchat",
        "addlist",
        "addstickers",
        "addemoji",
        "addtheme",
        "setlanguage",
        "share",
        "proxy",
        "socks",
        "login",
        "confirmphone",
        "boost",
        "contact",
        "invoice",
        "giftcode",
    }
)

_NOT_TARGET = (
    "target: нужно @имя, t.me/имя или ссылка-приглашение t.me/+… (t.me/joinchat/…) канала или группы Telegram"
)


def _invite(value: str) -> tuple[str, str]:
    if _DIGITS_RE.match(value):
        raise ValueError("target: t.me/+номер — ссылка на номер телефона, а не приглашение в группу")
    if not _HASH_RE.match(value):
        raise ValueError("target: ссылка-приглашение неверная (ожидается t.me/+… из 8–64 символов)")
    return "invite", value


def _username(value: str) -> tuple[str, str]:
    if not _USERNAME_RE.match(value) or value.lower() in _RESERVED:
        raise ValueError(_NOT_TARGET)
    return "username", value.lower()


_WA_HOST = "chat.whatsapp.com"
_NOT_WA_TARGET = "target: у WhatsApp вступление только по ссылке-приглашению https://chat.whatsapp.com/…"


def parse_wa_target(raw: object) -> tuple[str, str] | None:
    """Ф62-6 (E1): ссылка-приглашение WhatsApp `https://chat.whatsapp.com/<код>` (и без схемы) ->
    `("invite", код как есть)`; не ссылка WhatsApp — `None`; кривая ссылка WhatsApp — `ValueError`."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > TARGET_INPUT_MAX or any(ch.isspace() for ch in value):
        return None
    link = value if value.lower().startswith(("http://", "https://")) else "https://" + value
    parts = urlsplit(link)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host != _WA_HOST:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if parts.port is not None or parts.username or parts.password or len(segments) != 1:
        raise ValueError("target: ссылка-приглашение WhatsApp неверная (ожидается chat.whatsapp.com/<код>)")
    if not _HASH_RE.match(segments[0]):
        raise ValueError("target: ссылка-приглашение WhatsApp неверная (ожидается chat.whatsapp.com/<код>)")
    return "invite", segments[0]


def parse_any(raw: object) -> tuple[str, str, str]:
    """Ф62-6 П14: сначала разбор цели — `(сеть, вид, значение)`: ссылка WhatsApp — `whatsapp`, прочее —
    Telegram (`parse_target`)."""
    wa = parse_wa_target(raw)
    if wa is not None:
        return ("whatsapp", *wa)
    return ("telegram", *parse_target(raw))


def parse_target(raw: object) -> tuple[str, str]:
    """A2: `@имя`, `имя`, `t.me/имя`, `https://t.me/имя`, `t.me/имя/123`, `telegram.me/…`,
    `tg://resolve?domain=имя` -> `("username", имя в нижнем регистре)`; `t.me/+HASH`, `t.me/joinchat/HASH`,
    `tg://join?invite=HASH` -> `("invite", HASH как есть)`. Отказ — `ValueError` с текстом: `t.me/c/…` (ссылка
    на сообщение закрытого чата), `t.me/addlist/…` (папка), `t.me/+цифры` (номер телефона), прочее."""
    if not isinstance(raw, str):
        raise ValueError(_NOT_TARGET)
    value = raw.strip()
    if not value or len(value) > TARGET_INPUT_MAX or any(ch.isspace() for ch in value):
        raise ValueError(_NOT_TARGET)
    if value.lower().startswith("tg://"):
        parts = urlsplit(value)
        query = parse_qs(parts.query)
        action = parts.netloc.lower()
        if action == "resolve" and len(query.get("domain", [])) == 1:
            return _username(query["domain"][0])
        if action == "join" and len(query.get("invite", [])) == 1:
            return _invite(query["invite"][0])
        raise ValueError(_NOT_TARGET)
    if value.startswith("@"):
        return _username(value[1:])
    lowered = value.lower()
    if "/" not in value and "." not in value:
        return _username(value)
    link = value if lowered.startswith(("http://", "https://")) else "https://" + value
    parts = urlsplit(link)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host not in _TG_HOSTS or parts.port is not None or parts.username or parts.password:
        raise ValueError(_NOT_TARGET)
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        raise ValueError(_NOT_TARGET)
    head = segments[0]
    if head.startswith("+") and len(segments) == 1:
        return _invite(head[1:])
    if head.lower() == "joinchat" and len(segments) == 2:
        return _invite(segments[1])
    if head.lower() == "c":
        raise ValueError("target: t.me/c/… — ссылка на сообщение закрытого чата, по ней вступить нельзя")
    if head.lower() == "addlist":
        raise ValueError("target: t.me/addlist/… — это папка Telegram, а не канал или группа")
    # `t.me/имя/123` (сообщение) и `t.me/имя/тема/123` (сообщение в теме) -> имя.
    if len(segments) > 3 or not all(_DIGITS_RE.match(s) for s in segments[1:]):
        raise ValueError(_NOT_TARGET)
    return _username(head)


# --- политика проекта (решение 4) ---------------------------------------------------------------------------


def _policy_dict(policy_json: str | None) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def session_join(policy_json: str | None) -> str:
    """`agent_policy_json.join = {"auto": bool}` -> `confirm|auto`; по умолчанию и при кривом ключе —
    `confirm` (через владельца)."""
    raw = _policy_dict(policy_json).get(POLICY_KEY)
    return "auto" if isinstance(raw, dict) and raw.get("auto") is True else "confirm"


def project_auto(conn: sqlite3.Connection, project_id: int) -> bool:
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return row is not None and session_join(row[0]) == "auto"


def set_project_join(conn: sqlite3.Connection, project_id: int, mode: str) -> str | None:
    """A4/E: `session_join` (`confirm|auto`) -> ключ `join` политики проекта (прочие ключи не трогаются). Нет
    проекта — `None`; неизвестный режим — `ValueError`."""
    if mode not in MODES:
        raise ValueError("session_join: нужно confirm или auto")
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy_dict(row[0])
    policy[POLICY_KEY] = {"auto": mode == "auto"}
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    return mode


# --- строки -------------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "account_id",
    "project_id",
    "target_kind",
    "target",
    "reason",
    "status",
    "auto",
    "auto_eligible",
    "peek_json",
    "ext_id",
    "title",
    "channel_id",
    "error",
    "token_id",
    "client_name",
    "created_ts",
    "decided_ts",
    "done_ts",
    "updated_ts",
    "not_before_ms",
    "pause_until_ms",
    "attempts",
    "lease_until_ms",
    "lease_gen",
    "peek_leased_ts",
    "join_leased_ts",
    "seq",
    "autopilot_session",
)
_SELECT = ", ".join(f"j.{c}" for c in _COLUMNS)


class Req(NamedTuple):
    id: int
    account_id: int
    project_id: int
    target_kind: str
    target: str
    reason: str | None
    status: str
    auto: int
    auto_eligible: int
    peek_json: str | None
    ext_id: str | None
    title: str | None
    channel_id: int | None
    error: str | None
    token_id: int | None
    client_name: str | None
    created_ts: int
    decided_ts: int | None
    done_ts: int | None
    updated_ts: int
    not_before_ms: int
    pause_until_ms: int
    attempts: int
    lease_until_ms: int | None
    lease_gen: int
    peek_leased_ts: int | None
    join_leased_ts: int | None
    seq: int
    # Ф41-5б (5.5) — 0063_main.sql: сессия хаба, которой вступление при создании заявки разрешил
    # выключатель автопилота `tg_join`.
    autopilot_session: int | None = None


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Req]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.join_request j WHERE {where}",  # noqa: S608 — where — из кода
        tuple(params),
    ).fetchall()
    return [Req(*r) for r in rows]


def get(conn: sqlite3.Connection, request_id: int) -> Req | None:
    rows = _select(conn, "j.id = ?", (request_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, request_id: int, now_ms: int, **sets) -> None:
    """Любая запись в заявку двигает `seq` и `updated_ts` (П6)."""
    sets["updated_ts"] = now_ms
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.join_request SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), request_id],
    )


def _peek(req: Req) -> dict | None:
    if not req.peek_json:
        return None
    try:
        value = json.loads(req.peek_json)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def request_dict(req: Req) -> dict:
    """Выдача API (`/joins`, `/sync`): без опознания сессии и токена. `peek`, `title`, `reason` пишут
    посторонние/сессия — UI показывает только текстом, MCP — внутри блока данных."""
    return {
        "id": req.id,
        "account_id": req.account_id,
        "project_id": req.project_id,
        "target_kind": req.target_kind,
        "target": req.target,
        "reason": req.reason,
        "status": req.status,
        "auto": bool(req.auto),
        "peek": _peek(req),
        "ext_id": req.ext_id,
        "title": req.title,
        "channel_id": req.channel_id,
        "error": req.error,
        "client_name": req.client_name,
        "created_ts": req.created_ts,
        "decided_ts": req.decided_ts,
        "done_ts": req.done_ts,
        "updated_ts": req.updated_ts,
        # П2: «Отказать» — только пока шаг «вступить» ни разу не выдавался.
        "can_decline": req.status == "pending" or (req.status == "approved" and req.join_leased_ts is None),
        "joining": req.status == "approved" and req.join_leased_ts is not None,
        "seq": req.seq,
    }


def _audit(
    conn: sqlite3.Connection,
    req: Req,
    *,
    action: str,
    result: str,
    now_ms: int,
    actor: str = "core",
    request_id: str = "",
    api_token_id: int | None = None,
    client_name: str | None = None,
    detail: dict | None = None,
) -> None:
    """Аудит без текстов: id заявки, проект, аккаунт, вид цели (не сама цель и не название)."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or f"join-{req.id}",
        action=action,
        result=result,
        channel_id=req.channel_id,
        detail={
            "join_id": req.id,
            "project_id": req.project_id,
            "account_id": req.account_id,
            "target_kind": req.target_kind,
            **(detail or {}),
        },
    )


def _audit_done(conn: sqlite3.Connection, request_id: int, now_ms: int) -> None:
    """Аудит `join_done` на каждом конечном статусе."""
    req = get(conn, request_id)
    if req is not None and req.status in FINAL_STATUSES:
        _audit(conn, req, action="join_done", result=req.status, now_ms=now_ms, detail={"error": req.error})


# --- A2/A4: создание ----------------------------------------------------------------------------------------


def _visible(principal, project_id: int) -> bool:
    return principal.project_ids is None or project_id in principal.project_ids


_NET_RU = {"telegram": "Telegram", "whatsapp": "WhatsApp"}


def _pick_account(conn: sqlite3.Connection, account_id: int | None, network: str = "telegram") -> int:
    """Аккаунт сети `network` (`telegram` — по умолчанию: так выбирает и `tg_lookup`). Ф62-6 П14: без
    `account_id` — единственный аккаунт этой сети в хабе, иначе отказ с перечнем; `fake` (тесты) годится
    для любой сети."""
    name = _NET_RU[network]
    if account_id is not None:
        row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise Invalid("account_id: нет такого аккаунта")
        if row[0] == "fake" or row[0] == network:
            return account_id
        if row[0] == "whatsapp" and network == "telegram":
            raise Invalid(_NOT_WA_TARGET)
        if row[0] == "telegram" and network == "whatsapp":
            raise Invalid("account_id: ссылка WhatsApp, а аккаунт Telegram — укажи аккаунт WhatsApp")
        raise Invalid(f"account_id: вступать можно только аккаунтом {name}")
    rows = conn.execute("SELECT id FROM main.account WHERE source = ? ORDER BY id", (network,)).fetchall()
    if not rows:
        raise Invalid(f"account_id: в хабе нет аккаунта {name}")
    if len(rows) > 1:
        ids = ", ".join(str(r[0]) for r in rows)
        raise Invalid(f"account_id: аккаунтов {name} несколько ({ids}) — укажи account_id")
    return rows[0][0]


def _session_eligible(conn: sqlite3.Connection, token_id: int, project_id: int, session: dict | None) -> bool:
    """Решение 4: заявка живой (процесс с тем же `starttime`) сессии, у которой ЭТИМ токеном есть живая
    подписка на ЭТОТ проект (как опознание `hub_react`)."""
    if not session:
        return False
    pid, proc_start, session_id = session.get("pid"), session.get("proc_start"), session.get("session_id")
    if not isinstance(pid, int) or not isinstance(proc_start, int) or not isinstance(session_id, str):
        return False
    if not subs.is_alive(pid, proc_start, session.get("host")):
        return False
    row = conn.execute(
        "SELECT 1 FROM main.session_sub WHERE project_id = ? AND api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL LIMIT 1",
        (project_id, token_id, pid, proc_start, session_id),
    ).fetchone()
    return row is not None


def _account_pause(conn: sqlite3.Connection, account_id: int) -> int:
    """П3: пауза FloodWait аккаунта — на открытых строках; новая заявка её не обходит."""
    row = conn.execute(
        "SELECT MAX(pause_until_ms) FROM main.join_request WHERE account_id = ? "
        "AND status IN ('checking', 'pending', 'approved')",
        (account_id,),
    ).fetchone()
    return int(row[0] or 0)


def _day_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%d.%m")


def create(
    conn: sqlite3.Connection,
    *,
    principal,
    project_id: int,
    target: str,
    reason: str | None,
    session: dict | None,
    now_ms: int,
    account_id: int | None = None,
    client_name: str | None = None,
    request_id: str = "",
    autopilot_session: int | None = None,
) -> tuple[Req, bool]:
    """A2/A4: `(заявка, новая ли)`. Проверки — запреты `check_attach` к токену (403), проект виден токену
    (404), аккаунт Telegram, цель; открытая заявка на ту же цель того же проекта — она же (идемпотентно),
    другого проекта — 409; цель, которой владелец отказал за 7 дней, — 409; лимиты проекта — 429."""
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = row[0] if row and isinstance(row[0], str) else None
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise Denied("вступление — только из интерактивной сессии (не дежурная и не оркестратор)") from exc
    if project_id <= 0:
        raise Invalid("project_id: нужен настоящий проект (не «Неразобранное»)")
    if not _visible(principal, project_id):
        raise NotFound("project_id: нет такого проекта или он не виден профилю")
    if conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is None:
        raise NotFound("project_id: нет такого проекта или он не виден профилю")
    if reason is not None and len(reason) > REASON_MAX:
        raise Invalid(f"reason: не длиннее {REASON_MAX} символов")
    try:
        network, target_kind, target_value = parse_any(target)
    except ValueError as exc:
        if account_id is not None and _source(conn, account_id) == "whatsapp":
            raise Invalid(_NOT_WA_TARGET) from exc
        raise Invalid(str(exc)) from exc
    account_id = _pick_account(conn, account_id, network)

    open_rows = _select(
        conn,
        "j.account_id = ? AND j.target_kind = ? AND j.target = ? AND j.status IN ('checking', 'pending', "
        "'approved')",
        (account_id, target_kind, target_value),
    )
    if open_rows:
        existing = open_rows[0]
        if existing.project_id != project_id:
            raise Conflict(f"эту цель уже просит проект {existing.project_id} (заявка #{existing.id})")
        return existing, False
    declined = conn.execute(
        "SELECT MAX(decided_ts) FROM main.join_request WHERE account_id = ? AND target_kind = ? "
        "AND target = ? AND status = 'declined' AND decided_ts > ?",
        (account_id, target_kind, target_value, now_ms - DECLINE_MEMORY_MS),
    ).fetchone()
    if declined is not None and declined[0] is not None:
        raise Conflict(f"владелец отказал {_day_month(declined[0])} — повторять эту заявку неделю нельзя")

    open_count = conn.execute(
        "SELECT COUNT(*) FROM main.join_request WHERE project_id = ? "
        "AND status IN ('checking', 'pending', 'approved')",
        (project_id,),
    ).fetchone()[0]
    if open_count >= OPEN_PER_PROJECT:
        raise TooMany(f"у проекта уже {OPEN_PER_PROJECT} открытых заявок — дождись решения владельца")
    day_count = conn.execute(
        "SELECT COUNT(*) FROM main.join_request WHERE project_id = ? AND created_ts > ?",
        (project_id, now_ms - DAY_MS),
    ).fetchone()[0]
    if day_count >= NEW_PER_PROJECT_DAY:
        raise TooMany(f"не больше {NEW_PER_PROJECT_DAY} заявок проекта за сутки")

    eligible = _session_eligible(conn, principal.token_id, project_id, session)
    session_json = (
        json.dumps(
            {k: session[k] for k in ("pid", "proc_start", "session_id") if k in session}, ensure_ascii=False
        )
        if session
        else None
    )
    cur = conn.execute(
        "INSERT INTO main.join_request (account_id, project_id, target_kind, target, reason, status, "
        "auto_eligible, token_id, client_name, session_json, created_ts, updated_ts, not_before_ms, seq, "
        "autopilot_session, hub_session_id) VALUES (?, ?, ?, ?, ?, 'checking', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            project_id,
            target_kind,
            target_value,
            reason or None,
            1 if eligible else 0,
            principal.token_id,
            client_name,
            session_json,
            now_ms,
            now_ms,
            max(now_ms, _account_pause(conn, account_id)),
            next_seq(conn),
            autopilot_session,
            # Ф56-1а (А9): сессия хаба просителя — по условной тройке (привязка к каналу владельца, прав не даёт).
            held_mod.open_id_of(conn, session),
        ),
    )
    req = get(conn, cur.lastrowid)
    assert req is not None
    _audit(
        conn,
        req,
        action="join_create",
        result="checking",
        now_ms=now_ms,
        actor=principal.kind,
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
        detail={"auto_eligible": eligible},
    )
    return req, True


# --- выдача (A4) --------------------------------------------------------------------------------------------


def list_requests(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    project_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """`GET /joins`: видимые проекты принципала; по умолчанию — открытые и закрытые за 7 дней, новые
    первыми. `status` — один статус (`open` — все открытые)."""
    where = ["1 = 1"]
    params: list = []
    if principal.project_ids is not None:
        ids = sorted(principal.project_ids)
        if not ids:
            return []
        where.append(f"j.project_id IN ({','.join('?' for _ in ids)})")
        params += ids
    if project_id is not None:
        where.append("j.project_id = ?")
        params.append(project_id)
    if status == "open":
        where.append("j.status IN ('checking', 'pending', 'approved')")
    elif status is not None:
        where.append("j.status = ?")
        params.append(status)
    else:
        where.append("(j.status IN ('checking', 'pending', 'approved') OR j.updated_ts > ?)")
        params.append(now_ms - LIST_RECENT_MS)
    limit = max(1, min(int(limit), LIST_MAX))
    rows = _select(conn, " AND ".join(where) + " ORDER BY j.id DESC LIMIT ?", [*params, limit])
    states = chan_sub_states(conn, rows)
    out = []
    for r in rows:
        item = request_dict(r)
        item["chan_sub"] = states.get(r.id)  # 24.10: подписка сессии-заявителя на беседу вступления
        out.append(item)
    return out


def visible_request(conn: sqlite3.Connection, principal, request_id: int) -> Req:
    req = get(conn, request_id)
    if req is None or not _visible(principal, req.project_id):
        raise NotFound(f"заявка {request_id} не найдена")
    return req


def changes(conn: sqlite3.Connection, principal, *, since: int, watermark: int, limit: int) -> list[dict]:
    """П6: заявки с `since < seq <= watermark` в скоупе проекта, по возрастанию `seq` (`/sync`, SSE)."""
    where = "j.seq > ? AND j.seq <= ?"
    params: list = [since, watermark]
    if principal.project_ids is not None:
        ids = sorted(principal.project_ids)
        if not ids:
            return []
        where += f" AND j.project_id IN ({','.join('?' for _ in ids)})"
        params += ids
    rows = _select(conn, where + " ORDER BY j.seq LIMIT ?", [*params, limit])
    return [request_dict(r) for r in rows]


# --- A2: решение владельца ---------------------------------------------------------------------------------------


def decide(
    conn: sqlite3.Connection,
    request_id: int,
    action: str,
    *,
    now_ms: int,
    api_token_id: int | None = None,
    client_name: str | None = None,
    audit_request_id: str = "",
) -> Req:
    """`approve` — только из `pending`; `decline` — из `pending` или из `approved`, пока шаг «вступить» ни
    разу не выдавался (П2: иначе 409 «уже вступаю»). Аудит `join_decide` (actor `ui`)."""
    req = get(conn, request_id)
    if req is None:
        raise NotFound(f"заявка {request_id} не найдена")
    if action == "approve":
        if req.status != "pending":
            raise Conflict(f"одобрить можно только ждущую решения заявку (сейчас {req.status})")
        leave_id = _open_leave(conn, req)
        if leave_id is not None:
            raise Conflict(f"по этой беседе идёт выход (заявка #{leave_id})")
        _update(
            conn,
            request_id,
            now_ms,
            status="approved",
            decided_ts=now_ms,
            attempts=0,
            not_before_ms=max(now_ms, req.pause_until_ms),
        )
    elif action == "decline":
        if req.status == "approved" and req.join_leased_ts is not None:
            raise Conflict("уже вступаю — отказать нельзя")
        if req.status not in ("pending", "approved"):
            raise Conflict(f"заявка уже закрыта ({req.status})")
        _update(
            conn,
            request_id,
            now_ms,
            status="declined",
            decided_ts=now_ms,
            done_ts=now_ms,
            lease_until_ms=None,
        )
    else:
        raise Invalid(f"action: approve или decline, получено {action!r}")
    req = get(conn, request_id)
    assert req is not None
    _audit(
        conn,
        req,
        action="join_decide",
        result=req.status,
        now_ms=now_ms,
        actor="ui",
        request_id=audit_request_id,
        api_token_id=api_token_id,
        client_name=client_name,
    )
    if req.status == "declined":
        _audit_done(conn, request_id, now_ms)
    return req


# --- A2 + П3: очередь коннектору ----------------------------------------------------------------------------


def _account(conn: sqlite3.Connection, account_id: int) -> tuple[str, str, str] | None:
    """`(схема, статус, источник)` аккаунта или `None`."""
    row = conn.execute("SELECT db, status, source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


def _source(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def _nth_recent(conn: sqlite3.Connection, column: str, account_id: int, n: int, since_ms: int) -> int | None:
    """Время `n`-го с конца события `column` аккаунта за окно (или `None`, если их меньше `n`)."""
    row = conn.execute(
        f"SELECT {column} FROM main.join_request WHERE account_id = ? AND {column} > ? "  # noqa: S608
        f"ORDER BY {column} DESC LIMIT 1 OFFSET ?",
        (account_id, since_ms, n - 1),
    ).fetchone()
    return row[0] if row is not None else None


class _Pace(NamedTuple):
    peek_due: int
    join_due: int
    auto_join_due: int
    join_busy_until: int


def _pace(conn: sqlite3.Connection, account_id: int, now_ms: int) -> _Pace:
    """П3: ближайшее время следующего предпросмотра и вступления аккаунта по темпу и суточным потолкам."""
    last_peek, last_join, last_auto = conn.execute(
        "SELECT MAX(peek_leased_ts), MAX(join_leased_ts), MAX(CASE WHEN auto = 1 THEN join_leased_ts END) "
        "FROM main.join_request WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    peek_daily, join_daily = DAILY_BY_SOURCE.get(_source(conn, account_id) or "", (PEEK_DAILY, JOIN_DAILY))
    peek_due = (last_peek + PEEK_GAP_MS) if last_peek is not None else 0
    oldest = _nth_recent(conn, "peek_leased_ts", account_id, peek_daily, now_ms - DAY_MS)
    if oldest is not None:
        peek_due = max(peek_due, oldest + DAY_MS)
    join_due = (last_join + JOIN_GAP_MS) if last_join is not None else 0
    oldest = _nth_recent(conn, "join_leased_ts", account_id, join_daily, now_ms - DAY_MS)
    if oldest is not None:
        join_due = max(join_due, oldest + DAY_MS)
    auto_join_due = max(join_due, (last_auto + AUTO_GAP_MS) if last_auto is not None else 0)
    # Шаги «вступить» — по одному на аккаунт: пока выданный не отчитался и не истёк, следующий не выдаётся.
    busy_until = conn.execute(
        "SELECT MAX(lease_until_ms) FROM main.join_request WHERE account_id = ? AND status = 'approved' "
        "AND join_leased_ts IS NOT NULL AND lease_until_ms > ?",
        (account_id, now_ms),
    ).fetchone()[0]
    return _Pace(peek_due, join_due, auto_join_due, busy_until or 0)


def _candidates(conn: sqlite3.Connection, account_id: int, now_ms: int) -> list[tuple[int, Req]]:
    """`(срок, заявка)` открытых шагов аккаунта: `approved` (шаг `join`, старший `decided_ts` первым), затем
    `checking` (шаг `peek`, по порядку создания). Срок — max паузы строки, лиза и темпа аккаунта."""
    pace = _pace(conn, account_id, now_ms)
    out: list[tuple[int, Req]] = []
    joins = _select(
        conn, "j.account_id = ? AND j.status = 'approved' ORDER BY j.decided_ts, j.id", (account_id,)
    )
    for req in joins:
        step_due = pace.auto_join_due if req.auto else pace.join_due
        # Другой шаг «вступить» в работе — ждать его отчёта или конца его лиза.
        step_due = max(step_due, pace.join_busy_until)
        out.append((_row_due(req, step_due), req))
    peeks = _select(conn, "j.account_id = ? AND j.status = 'checking' ORDER BY j.id", (account_id,))
    out += [(_row_due(req, pace.peek_due), req) for req in peeks]
    return out


def _row_due(req: Req, step_due: int) -> int:
    return max(req.not_before_ms, req.pause_until_ms, req.lease_until_ms or 0, step_due)


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """A2 + П3: задания своего аккаунта `{id, lease_gen, step: peek|join, target_kind, target, ext_id,
    project_id}`, лиз `LEASE_MS`; `limited`/`auth_required` — пусто, строки ждут. Выдача сразу сдвигает темп:
    следующий предпросмотр — не раньше `PEEK_GAP_MS`, вступление — `JOIN_GAP_MS`."""
    info = _account(conn, account_id)
    if info is None:
        return []
    _schema, status, source = info
    if status in _BLOCKED_STATUSES or source not in SOURCES:
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    items: list[dict] = []
    while len(items) < limit:
        ready = [(due, req) for due, req in _candidates(conn, account_id, now_ms) if due <= now_ms]
        if not ready:
            break
        _due, req = ready[0]
        step = "join" if req.status == "approved" else "peek"
        sets: dict = {"lease_until_ms": now_ms + LEASE_MS, "lease_gen": req.lease_gen + 1}
        if step == "peek":
            sets["peek_leased_ts"] = now_ms
        elif req.join_leased_ts is None:
            sets["join_leased_ts"] = now_ms  # граница привязки беседы (решение 5) и темп вступлений
        _update(conn, req.id, now_ms, **sets)
        items.append(
            {
                "id": req.id,
                "lease_gen": req.lease_gen + 1,
                "step": step,
                "target_kind": req.target_kind,
                "target": req.target,
                "ext_id": req.ext_id,
                "project_id": req.project_id,
            }
        )
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait` (как `read_ack.wait_state`): `(готово, ближайший срок в мс)`. Аккаунт не может
    получать задания (`limited`, `auth_required`, не Telegram) — `(False, None)`: `wait` держит полный
    таймаут."""
    info = _account(conn, account_id)
    if info is None:
        return False, None
    _schema, status, source = info
    if status in _BLOCKED_STATUSES or source not in SOURCES:
        return False, None
    dues = [due for due, _req in _candidates(conn, account_id, now_ms)]
    if not dues:
        return False, None
    due = min(dues)
    if due <= now_ms:
        return True, None
    return False, due


# --- A2: отчёт коннектора -----------------------------------------------------------------------------------


def backoff_ms(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки (с 1): 30 с, 60 с, 120 с, …"""
    return BACKOFF_BASE_MS * 2 ** (max(attempt, 1) - 1)


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _nonneg_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**53:
        return None
    return value


def _clean_peek(peek: object) -> dict:
    """Предпросмотр от коннектора — только известные поля, строки обрезаны, числа проверены."""
    if not isinstance(peek, dict):
        raise Invalid("peek: нужен объект предпросмотра")
    kind = peek.get("kind")
    out = {
        "title": _clip(peek.get("title"), TITLE_MAX),
        "kind": kind if kind in PEEK_KINDS else None,
        "participants": _nonneg_int(peek.get("participants")),
        "about": _clip(peek.get("about"), ABOUT_MAX),
        "request_needed": peek.get("request_needed") is True,
        "member": peek.get("member") is True,
        "forum": peek.get("forum") is True,
        "ext_id": _clip(peek.get("ext_id"), EXT_ID_MAX),
    }
    linked = _clean_linked(peek.get("linked"))
    if linked is not None:
        out["linked"] = linked
    return out


def _clean_linked(linked: object) -> dict | None:
    """Ф17-3 решение 4: связанный чат предпросмотра (группа обсуждения канала / канал группы). Нет названия
    или мусор — поля нет; плохой `username` — `null`."""
    if not isinstance(linked, dict):
        return None
    title = _clip(linked.get("title"), TITLE_MAX)
    kind = linked.get("kind")
    if not title or kind not in ("discussion", "channel"):
        return None
    username = linked.get("username")
    if not isinstance(username, str) or not _USERNAME_RE.match(username):
        username = None
    return {"title": title, "username": username, "kind": kind}


def _pause_account(conn: sqlite3.Connection, account_id: int, until_ms: int, now_ms: int) -> None:
    """П3: FloodWait — пауза ВСЕМ открытым строкам аккаунта (в строках — переживает рестарт)."""
    for req in _select(
        conn,
        "j.account_id = ? AND j.status IN ('checking', 'pending', 'approved') AND "
        "(j.pause_until_ms < ? OR j.not_before_ms < ?)",
        (account_id, until_ms, until_ms),
    ):
        _update(
            conn,
            req.id,
            now_ms,
            pause_until_ms=max(req.pause_until_ms, until_ms),
            not_before_ms=max(req.not_before_ms, until_ms),
        )


def report(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    request_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    retry_after_ms: int | None = None,
    reason: str | None = None,
    result: str | None = None,
    peek: dict | None = None,
    ext_id: str | None = None,
    title: str | None = None,
) -> str:
    """Отчёт шага. `retry`: с `retry_after_ms` (FloodWait) — пауза всему аккаунту, попыткой не считается;
    без — бэкофф, 5-я неудача -> `failed`/`gave_up`. `drop` -> `failed` с причиной. `done`: шаг `peek` —
    `already` (владелец уже участник) | `approved auto=1` (выключатель проекта, решение 4) | `pending`; шаг `join`
    — `joined|already|requested`. Итог для журнала — новый статус (или `paused`)."""
    if outcome not in REPORT_OUTCOMES:
        raise Invalid(f"outcome: {outcome!r}")
    req = get(conn, request_id)
    if req is None or req.account_id != account_id:
        raise NotFound(f"join {request_id}")
    if req.lease_gen != lease_gen or req.status not in ("checking", "approved"):
        raise StaleLease(f"join {request_id}: lease_gen {lease_gen} != {req.lease_gen} или шаг закрыт")
    step = "peek" if req.status == "checking" else "join"

    if outcome == "retry":
        if retry_after_ms:
            until = now_ms + retry_after_ms
            _update(conn, request_id, now_ms, lease_until_ms=None)
            _pause_account(conn, account_id, until, now_ms)
            return "paused"
        attempt = req.attempts + 1
        if attempt >= MAX_ATTEMPTS:
            _update(
                conn,
                request_id,
                now_ms,
                status="failed",
                error="gave_up",
                done_ts=now_ms,
                lease_until_ms=None,
            )
            _audit_done(conn, request_id, now_ms)
            return "failed"
        _update(
            conn,
            request_id,
            now_ms,
            attempts=attempt,
            lease_until_ms=None,
            not_before_ms=max(req.not_before_ms, now_ms + backoff_ms(attempt)),
        )
        return "paused"

    if outcome == "drop":
        _update(
            conn,
            request_id,
            now_ms,
            status="failed",
            error=_clip(reason, ERROR_MAX) or "other",
            done_ts=now_ms,
            lease_until_ms=None,
        )
        _audit_done(conn, request_id, now_ms)
        return "failed"

    if step == "peek":
        clean = _clean_peek(peek)
        known_ext = clean["ext_id"] or req.ext_id
        common: dict = {
            "peek_json": json.dumps(clean, ensure_ascii=False),
            "ext_id": known_ext,
            "title": clean["title"],
            "lease_until_ms": None,
            "attempts": 0,
        }
        if clean["member"]:
            _update(conn, request_id, now_ms, status="already", done_ts=now_ms, **common)
            _ensure_wa_channel(conn, request_id, now_ms)
            _bind_existing(conn, request_id, now_ms)
            leave_mod.on_joined(conn, account_id, known_ext, now_ms)  # Ф17-2 П2
            _audit_done(conn, request_id, now_ms)
            return "already"
        # Ф17-2 П6: по этой беседе открыт выход — не автоодобрение, а карточка владельцу (его «Вступить» получит
        # 409, пока выход не закрыт).
        by_project = _auto_allowed(conn, req, now_ms)
        by_autopilot = not by_project and _autopilot_allowed(conn, req, now_ms)
        if (by_project or by_autopilot) and _open_leave(conn, req._replace(ext_id=known_ext)) is None:
            last_auto = conn.execute(
                "SELECT MAX(join_leased_ts) FROM main.join_request WHERE account_id = ? AND auto = 1",
                (account_id,),
            ).fetchone()[0]
            not_before = max(
                now_ms, req.pause_until_ms, (last_auto + AUTO_GAP_MS) if last_auto is not None else 0
            )
            _update(
                conn,
                request_id,
                now_ms,
                status="approved",
                auto=1,
                decided_ts=now_ms,
                not_before_ms=not_before,
                **common,
            )
            approved = get(conn, request_id)
            assert approved is not None
            if by_autopilot:
                # Ф41-5б П7: своё действие аудита — приняла не кнопка владельца и не выключатель проекта.
                _audit(
                    conn,
                    approved,
                    action="join_auto",
                    result="approved",
                    now_ms=now_ms,
                    detail={"hub_session_id": req.autopilot_session},
                )
            else:
                _audit(
                    conn,
                    approved,
                    action="join_decide",
                    result="approved",
                    now_ms=now_ms,
                    detail={"auto": True},
                )
            return "approved"
        _update(conn, request_id, now_ms, status="pending", **common)
        # Ф66-1 (П9): заявка стала карточкой владельца — «сразу да» по таблице маршрута проекта (пределы
        # `AUTO_DAILY` / `AUTO_GAP_MS` — в `card_route`).
        from . import card_route as card_route_mod  # noqa: PLC0415 — card_route -> очередь -> join

        if card_route_mod.auto_answer(conn, f"join:{request_id}", now_ms=now_ms).done:
            return "approved"
        return "pending"

    if result not in JOIN_RESULTS:
        raise Invalid("result: joined, already или requested")
    _update(
        conn,
        request_id,
        now_ms,
        status=result,
        ext_id=_clip(ext_id, EXT_ID_MAX) or req.ext_id,
        title=_clip(title, TITLE_MAX) or req.title,
        done_ts=now_ms,
        lease_until_ms=None,
    )
    if result in ("joined", "already"):
        _ensure_wa_channel(conn, request_id, now_ms)
    _bind_existing(conn, request_id, now_ms)
    if result in ("joined", "already"):
        # Ф17-2 П2: вступили снова в беседу, из которой выходили, — из архива сразу, не ждать первого поста.
        done = get(conn, request_id)
        leave_mod.on_joined(conn, account_id, done.ext_id if done is not None else None, now_ms)
    _audit_done(conn, request_id, now_ms)
    return result


def _open_leave(conn: sqlite3.Connection, req: Req) -> int | None:
    """Ф17-2 П6: открытая заявка выхода по той же беседе `(account_id, ext_id)`; ext_id неизвестен — нет."""
    if not req.ext_id:
        return None
    return leave_mod.open_leave_id(conn, req.account_id, req.ext_id)


def _auto_allowed(conn: sqlite3.Connection, req: Req, now_ms: int) -> bool:
    """Решение 4: выключатель проекта включён СЕЙЧАС, заявку создала живая подписанная сессия проекта (флаг
    при создании) и у аккаунта меньше `AUTO_DAILY` автоодобрений за скользящие сутки."""
    if not req.auto_eligible or not project_auto(conn, req.project_id):
        return False
    if _source(conn, req.account_id) not in AUTO_SOURCES:
        return False  # Ф62-6 П2: WhatsApp — без карточки только сессия хаба в авто при снятом `tg_join`
    count = conn.execute(
        "SELECT COUNT(*) FROM main.join_request WHERE account_id = ? AND auto = 1 AND decided_ts > ?",
        (req.account_id, now_ms - DAY_MS),
    ).fetchone()[0]
    return count < AUTO_DAILY


def _autopilot_allowed(conn: sqlite3.Connection, req: Req, now_ms: int) -> bool:
    """Ф41-5б (5.5): заявку создала сессия хаба, которой вступление разрешил выключатель `tg_join`, и
    СЕЙЧАС (после предпросмотра) это всё ещё так: сессия не закрыта, в авто, проект тот же, выключатель
    снят. Потолок автоодобрений аккаунта за сутки (`AUTO_DAILY`) — общий с выключателем проекта."""
    if req.autopilot_session is None:
        return False
    from . import autopilot as autopilot_mod
    from . import held as held_mod

    rec = held_mod.get_open(conn, req.autopilot_session)
    if rec is None or rec.mode != "auto" or rec.project_id != req.project_id:
        return False
    if not autopilot_mod.switch_free(conn, "tg_join", req.project_id):
        return False
    count = conn.execute(
        "SELECT COUNT(*) FROM main.join_request WHERE account_id = ? AND auto = 1 AND decided_ts > ?",
        (req.account_id, now_ms - DAY_MS),
    ).fetchone()[0]
    return count < AUTO_DAILY


# --- A3: привязка беседы ------------------------------------------------------------------------------------


class _Chan(NamedTuple):
    id: int
    ext_id: str
    kind: str
    parent_channel_id: int | None
    first_seen_ts: int
    map_source: str
    map_reason: str | None
    muted: int


def _chan(conn: sqlite3.Connection, schema: str, where: str, params: tuple) -> _Chan | None:
    row = conn.execute(
        f"SELECT id, ext_id, kind, parent_channel_id, first_seen_ts, map_source, map_reason, muted "  # noqa: S608
        f"FROM {schema}.channel WHERE {where}",
        params,
    ).fetchone()
    return _Chan(*row) if row is not None else None


def _boundary(req: Req) -> int | None:
    """Граница «беседа появилась из-за вступления»: первая выдача шага `join`; для `already` из предпросмотра
    (шага `join` не было) — время закрытия заявки."""
    if req.join_leased_ts is not None:
        return req.join_leased_ts
    if req.status == "already":
        return req.done_ts
    return None


def _mute_topic(conn: sqlite3.Connection, schema: str, topic_id: int) -> None:
    """Ревью Ф17-1 (находка 2): одна новая тема форума — заглушить; уже существующие темы не трогаются (звук,
    включённый владельцем у темы, остаётся)."""
    row = conn.execute(f"SELECT muted FROM {schema}.channel WHERE id = ?", (topic_id,)).fetchone()  # noqa: S608
    if row is not None and not row[0]:
        conn.execute(
            f"UPDATE {schema}.channel SET muted = 1, muted_by = NULL, seq = ? WHERE id = ?",  # noqa: S608
            (next_seq(conn), topic_id),
        )


def _mute_topics(conn: sqlite3.Connection, schema: str, parent_id: int, boundary: int) -> list[int]:
    """П1: темы форума, появившиеся после границы, — заглушить явно (заглушка родителя на них не
    переходит)."""
    rows = conn.execute(
        f"SELECT id, muted FROM {schema}.channel WHERE parent_channel_id = ? AND kind = 'topic' "  # noqa: S608
        "AND first_seen_ts >= ?",
        (parent_id, boundary),
    ).fetchall()
    for topic_id, muted in rows:
        if not muted:
            conn.execute(
                f"UPDATE {schema}.channel SET muted = 1, muted_by = NULL, seq = ? WHERE id = ?",  # noqa: S608
                (next_seq(conn), topic_id),
            )
    return [r[0] for r in rows]


def bind(conn: sqlite3.Connection, req: Req, channel: _Chan, now_ms: int) -> bool:
    """A3: беседа появилась после границы и не привязана вручную — в проект заявки, `manual`, причина
    «вступление по заявке #N», заглушена (свой UPDATE с `seq` беседы и аудитом `channel_update`, как
    `PATCH /channels`); темы форума — заглушены, проект идёт за родителем (`mapping`); ещё не выданные сигналы
    подписке на проект по беседе и её темам сняты (П4). В любом случае — `channel_id` в заявку. Идемпотентно.
    `True` — беседа привязана сейчас."""
    schema = db_of(channel.id)
    boundary = _boundary(req)
    bound = False
    # Ф62-6 П12: группа WhatsApp, в которую вступили, НЕ глушится (туда зовут владельца) — режим по политике
    # проекта; Telegram — как раньше.
    mute = _source(conn, req.account_id) != "whatsapp"
    if boundary is not None and channel.first_seen_ts >= boundary and channel.map_source != "manual":
        conn.execute(
            f"UPDATE {schema}.channel SET project_id = ?, map_source = 'manual', map_reason = ?, "  # noqa: S608
            f"{'muted = 1, muted_by = NULL, ' if mute else ''}seq = ? WHERE id = ?",
            (req.project_id, BIND_REASON.format(id=req.id), next_seq(conn), channel.id),
        )
        mapping.recompute_topics(conn, schema, channel.id, now_ms)
        if mute:
            topics = _mute_topics(conn, schema, channel.id, boundary)
            chan_subs.skip_project_signals(conn, [channel.id, *topics], reason="join_muted", now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="core",
            api_token_id=None,
            client_name=None,
            request_id=f"join-{req.id}",
            action="channel_update",
            result="ok",
            channel_id=channel.id,
            detail={"project_id": req.project_id, "muted": mute, "join_id": req.id},
        )
        bound = True
    if req.channel_id is None:
        _update(conn, req.id, now_ms, channel_id=channel.id)
        _subscribe_requester(conn, req, channel.id, now_ms)
    return bound


CHAN_SUB_ACTION = "chan_sub_join"


def _requester_subs(conn: sqlite3.Connection, req: Req) -> list:
    """Подписки на проект заявки той сессии, что подала заявку (тройка из `session_json`), новые первыми.
    Токен не сверяется: сессия хаба могла переподписаться новым токеном профиля."""
    row = conn.execute("SELECT session_json FROM main.join_request WHERE id = ?", (req.id,)).fetchone()
    try:
        session = json.loads(row[0]) if row is not None and row[0] else None
    except ValueError:
        session = None
    if not isinstance(session, dict):
        return []
    pid, proc_start, session_id = session.get("pid"), session.get("proc_start"), session.get("session_id")
    if not isinstance(pid, int) or not isinstance(proc_start, int) or not isinstance(session_id, str):
        return []
    ids = conn.execute(
        "SELECT id FROM main.session_sub WHERE project_id = ? AND session_pid = ? AND session_proc_start = ? "
        "AND session_id = ? ORDER BY id DESC",
        (req.project_id, pid, proc_start, session_id),
    ).fetchall()
    return [rec for rec in (subs.get_sub(conn, r[0]) for r in ids) if rec is not None]


def _role_successor(conn: sqlite3.Connection, req: Req, last) -> object | None:
    """Сессии-заявителя уже нет — живая подписка на проект той же линии роли (`role_key` и номер в роли)."""
    if last is None or not last.role_key:
        return None
    ids = conn.execute(
        "SELECT id FROM main.session_sub WHERE project_id = ? AND role_key = ? AND role_no = ? "
        "AND ended_ts IS NULL ORDER BY id DESC",
        (req.project_id, last.role_key, last.role_no),
    ).fetchall()
    for (sub_id,) in ids:
        rec = subs.get_sub(conn, sub_id)
        if rec is not None and subs.rec_session_alive(conn, rec):
            return rec
    return None


def _subscribe_requester(conn: sqlite3.Connection, req: Req, channel_id: int, now_ms: int) -> None:
    """24.10 (менеджер «Hooli»: беседы вступлений сутки лежали без разбора): беседа вступления появилась —
    сессия, подавшая заявку, сама подписывается на неё (как `hub_subscribe(channel_id=…)` без ключей), если
    её подписка на проект жива. Сессии уже нет — живая сессия той же линии роли. Итог (подписка или причина
    отказа) — строкой аудита `chan_sub_join` с `join_id`; по ней `hub_joins` пишет «подписка на беседу #N»."""
    own = _requester_subs(conn, req)
    target = next((rec for rec in own if rec.ended_ts is None), None)
    via = "own"
    if target is None or not subs.rec_session_alive(conn, target):
        target = _role_successor(conn, req, own[0] if own else None)
        via = "role"
    request_id = f"join-{req.id}"
    detail = {"join_id": req.id, "via": via}
    if target is None:
        outcome, rec = ("no_session" if own else "no_sub"), None
    else:
        rec, outcome = chan_subs.subscribe_for_sub(
            conn,
            target,
            channel_id,
            now_ms=now_ms,
            request_id=request_id,
            action=CHAN_SUB_ACTION,
            detail=detail,
        )
    if outcome == "created":
        return  # аудит — у вставки подписки (`action` = `chan_sub_join`, `result` = `ok`)
    write_audit(
        conn,
        now_ms=now_ms,
        actor="core",
        api_token_id=target.api_token_id if target is not None else None,
        client_name=None,
        request_id=request_id,
        action=CHAN_SUB_ACTION,
        result=outcome,
        channel_id=channel_id,
        detail={**detail, "sub_id": rec.id if rec is not None else None},
    )


def chan_sub_states(conn: sqlite3.Connection, reqs: list[Req]) -> dict[int, dict]:
    """Для выдачи `GET /joins`: `join_id -> {"state": "live"|"ended"|"none", "id", "reason"}` по последней
    строке аудита `chan_sub_join` заявки; у живой/снятой — номер подписки на беседу."""
    wanted = {r.id for r in reqs if r.channel_id is not None}
    if not wanted:
        return {}
    since = min(r.created_ts for r in reqs if r.id in wanted)
    rows = conn.execute(
        "SELECT result, detail_json FROM main.audit WHERE action = ? AND ts >= ? ORDER BY id",
        (CHAN_SUB_ACTION, since),
    ).fetchall()
    out: dict[int, dict] = {}
    for result, detail_json in rows:
        try:
            detail = json.loads(detail_json) if detail_json else {}
        except ValueError:
            continue
        join_id = detail.get("join_id") if isinstance(detail, dict) else None
        if join_id not in wanted:
            continue
        sub_id = detail.get("sub_id")
        if result in ("ok", "exists") and isinstance(sub_id, int):
            rec = chan_subs.get(conn, sub_id)
            live = rec is not None and rec.ended_ts is None
            out[join_id] = {"state": "live" if live else "ended", "id": sub_id, "reason": None}
        else:
            out[join_id] = {"state": "none", "id": None, "reason": result}
    return out


def _ensure_wa_channel(conn: sqlite3.Connection, request_id: int, now_ms: int) -> None:
    """Ф62-6: вступили в группу WhatsApp (`joined`/`already`) — беседа есть сразу, до первого сообщения:
    истории новой группы WhatsApp не присылает. Тот же код создания, что у приёма
    (`ingest.ensure_group_channel`); привязку делает хук `on_channel_created` / `_bind_existing`. Telegram —
    как раньше (беседа приходит с историей, решение 11.5)."""
    from . import ingest

    req = get(conn, request_id)
    if req is None or not req.ext_id or _source(conn, req.account_id) != "whatsapp":
        return
    ingest.ensure_group_channel(conn, req.account_id, req.ext_id, req.title, now_ms)


def _bind_existing(conn: sqlite3.Connection, request_id: int, now_ms: int) -> None:
    """Из отчёта: беседа `(account_id, ext_id)` уже есть в хабе — привязать (порядок «история раньше
    отчёта»)."""
    req = get(conn, request_id)
    if req is None or req.ext_id is None:
        return
    info = _account(conn, req.account_id)
    if info is None:
        return
    channel = _chan(conn, info[0], "account_id = ? AND ext_id = ?", (req.account_id, req.ext_id))
    if channel is not None:
        bind(conn, req, channel, now_ms)


def on_channel_created(
    conn: sqlite3.Connection, schema: str, account_id: int, channel_id: int, now_ms: int
) -> None:
    """A3 + П1/П2/П4: хук `ingest._create_channel` (после вставки и раскладки). Обычная беседа — заявки с тем
    же `(account_id, ext_id)`, пустым `channel_id` и заданной границей (шаг `join` выдавался — в любом
    статусе, П2; или `already` из предпросмотра). Тема форума — заявка, которой привязан её родитель: тема
    глушится явно."""
    channel = _chan(conn, schema, "id = ?", (channel_id,))
    if channel is None:
        return
    if channel.kind == "topic" and channel.parent_channel_id is not None:
        parent = _chan(conn, schema, "id = ?", (channel.parent_channel_id,))
        match = _BIND_REASON_RE.match(parent.map_reason or "") if parent is not None else None
        # Ревью Ф17-1 (находка 2): владелец включил звук у группы — новые темы тоже со звуком.
        if parent is None or match is None or parent.map_source != "manual" or not parent.muted:
            return
        req = get(conn, int(match.group(1)))
        boundary = _boundary(req) if req is not None else None
        if req is None or req.account_id != account_id or req.ext_id != parent.ext_id or boundary is None:
            return
        if channel.first_seen_ts >= boundary:
            _mute_topic(conn, schema, channel.id)
            chan_subs.skip_project_signals(conn, [channel.id], reason="join_muted", now_ms=now_ms)
        return
    # Ревью Ф17-1 (находка 1): `failed` с ответом Telegram (группа заполнена, закрыта, …) — вступления не
    # было; привязка остаётся только после `gave_up` (сдались без ответа — вступление могло пройти, П2) и в
    # пределах срока от вступления.
    reqs = _select(
        conn,
        "j.account_id = ? AND j.ext_id = ? AND j.channel_id IS NULL "
        "AND ((j.join_leased_ts IS NOT NULL AND (j.status <> 'failed' OR j.error = 'gave_up')) "
        "OR j.status = 'already') ORDER BY j.id DESC",
        (account_id, channel.ext_id),
    )
    for req in reqs:
        boundary = _boundary(req)
        if boundary is None:
            continue
        # Срок — от последнего исхода заявки (`done_ts`: отчёт, `gave_up`), в работе — от границы.
        since = max(boundary, req.done_ts or 0)
        window = BIND_WINDOW_REQUESTED_MS if req.status == "requested" else BIND_WINDOW_MS
        if channel.first_seen_ts - since > window:
            continue
        fresh = _chan(conn, schema, "id = ?", (channel_id,))
        if fresh is not None:
            bind(conn, req, fresh, now_ms)
