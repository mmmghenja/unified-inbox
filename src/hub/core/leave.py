"""Выход из групп и каналов Telegram (Ф17-2, docs/architecture.md–A7 и поправки
П1–П12 — они главнее текста разделов).

Владелец выходит кнопкой в шапке беседы (`create_ui`: заявка сразу `approved`); сессия проекта просит
(`create_session`, bearer `draft`, запреты `check_attach`) — заявка `pending`, выходит хаб только после
«Выйти» владельца (`decide`). Очередь «ядро -> коннектор» — по форме вступления (`lease`/`wait_state`/`report`):
между выдачами аккаунта `LEAVE_GAP_MS`, FloodWait — пауза всем открытым заявкам выхода аккаунта (переживает
рестарт).

После `done` (A5, порядок П9) — в той же транзакции: архив беседы и её тем (`channels.update_channel`,
закрывает карточки), снятие ещё не выданных сигналов подписке на проект, завершение подписок сессий на эти
беседы (`channel_gone`, П10). Возврат из архива (A6/П2/П3): новое сообщение позже выхода
(`on_message_inserted` из `ingest`) или повторное вступление (`on_joined` из `join.report`).

Тема форума — заявка на РОДИТЕЛЯ (выход из форума целиком). Каждая запись в строку заявки двигает глобальный
`seq`: `/sync` (`leaves`) и кадр SSE `leave`. Пишет только внутри `Db.write()`, время — параметром `now_ms`.
Импортов `queries`/`join` здесь нет (циклы: `queries` -> `ingest` -> этот модуль, `join` -> этот модуль):
заявки вступления (П6) читаются прямым запросом к `main.join_request`.
"""

import json
import sqlite3
from datetime import datetime
from typing import NamedTuple

from . import chan_subs, draft_channels
from . import channels as channels_mod
from . import held as held_mod
from .audit import write_audit
from .db import db_of, next_seq
from .security import check_attach

# fake — для тестов и стенда (`scripts/dev-demo.py`), как у вступления. Ф62-6 (E1): и группы WhatsApp
# (каналов WhatsApp в хабе нет — у них только `group`).
SOURCES = frozenset({"telegram", "whatsapp", "fake"})
CHANNEL_KINDS = frozenset({"group", "channel", "topic"})

OPEN_STATUSES = ("pending", "approved")
FINAL_STATUSES = ("left", "failed", "declined")
STATUSES = (*OPEN_STATUSES, *FINAL_STATUSES)
RESULTS = frozenset({"left", "not_member"})
REPORT_OUTCOMES = frozenset({"done", "retry", "drop"})

REASON_MAX = 500
ERROR_MAX = 200

_MIN_MS = 60_000
DAY_MS = 24 * 60 * _MIN_MS
# A2: лимиты просьб сессий (от спама карточками); у владельца лимитов нет (решение 5).
OPEN_PER_PROJECT = 20
NEW_PER_PROJECT_DAY = 30
DECLINE_MEMORY_MS = 7 * DAY_MS
# A4: темп — только защита от FloodWait; лиз с запасом над 60 с шага коннектора (как у вступления).
LEAVE_GAP_MS = 10_000
LEASE_MS = 120_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_MS = 30_000
MAX_LEASE_LIMIT = 10
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})
# A6: служебное «вышел» и хвост живых апдейтов у выхода — не возврат; возврат — сообщение позже этого запаса.
RETURN_GRACE_MS = _MIN_MS

LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200

_NOT_GROUP = "выйти можно только из группы или канала Telegram или группы WhatsApp"
_NO_PROJECT = "беседа без проекта — выйти может только владелец из шапки"
_FORUM_ELSEWHERE = "форум в другом проекте — выйти может только владелец из шапки"


class LeaveError(Exception):
    """Базовая ошибка; текст — для человека/сессии, без названий бесед."""


class Invalid(LeaveError):
    """422: беседа не годится (личка, Slack, почта, заготовка, без проекта) или кривые поля отчёта."""


class Denied(LeaveError):
    """403: этому токену просить нельзя."""


class NotFound(LeaveError):
    """404: заявки/беседы нет, не видна принципалу или чужой аккаунт."""


class Conflict(LeaveError):
    """409: уже вышли, выход просит другой проект, владелец оставил беседу недавно, идёт вступление, решение не к
    этому статусу."""


class TooMany(LeaveError):
    """429: лимиты просьб проекта."""


class StaleLease(LeaveError):
    """409: `lease_gen` не совпал или шаг уже закрыт — задание перехвачено новой выдачей."""


# --- строки -------------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "account_id",
    "project_id",
    "channel_id",
    "ext_id",
    "chat_kind",
    "title",
    "reason",
    "origin",
    "status",
    "result",
    "error",
    "token_id",
    "client_name",
    "created_ts",
    "decided_ts",
    "done_ts",
    "archived_ids_json",
    "returned_ts",
    "updated_ts",
    "not_before_ms",
    "pause_until_ms",
    "attempts",
    "lease_until_ms",
    "lease_gen",
    "first_leased_ts",
    "last_leased_ts",
    "seq",
    "auto",
)
_SELECT = ", ".join(f"l.{c}" for c in _COLUMNS)


class Req(NamedTuple):
    id: int
    account_id: int
    project_id: int | None
    channel_id: int
    ext_id: str
    chat_kind: str
    title: str | None
    reason: str | None
    origin: str
    status: str
    result: str | None
    error: str | None
    token_id: int | None
    client_name: str | None
    created_ts: int
    decided_ts: int | None
    done_ts: int | None
    archived_ids_json: str | None
    returned_ts: int | None
    updated_ts: int
    not_before_ms: int
    pause_until_ms: int
    attempts: int
    lease_until_ms: int | None
    lease_gen: int
    first_leased_ts: int | None
    last_leased_ts: int | None
    seq: int
    auto: int = 0  # Ф41-5б (5.5): принята автопилотом


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Req]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.leave_request l WHERE {where}",  # noqa: S608 — where — из кода
        tuple(params),
    ).fetchall()
    return [Req(*r) for r in rows]


def get(conn: sqlite3.Connection, request_id: int) -> Req | None:
    rows = _select(conn, "l.id = ?", (request_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, request_id: int, now_ms: int, **sets) -> None:
    """Любая запись в заявку двигает `seq` и `updated_ts`."""
    sets["updated_ts"] = now_ms
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.leave_request SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), request_id],
    )


def request_dict(req: Req) -> dict:
    """Выдача API (`/leaves`, `/sync`): без токена. `title` (название беседы) и `reason` пишут посторонние/
    сессия — UI показывает только текстом, MCP — внутри блока данных."""
    return {
        "id": req.id,
        "account_id": req.account_id,
        "project_id": req.project_id,
        "channel_id": req.channel_id,
        "title": req.title,
        "chat_kind": req.chat_kind,
        "reason": req.reason,
        "origin": req.origin,
        "status": req.status,
        "result": req.result,
        "error": req.error,
        "client_name": req.client_name,
        "created_ts": req.created_ts,
        "decided_ts": req.decided_ts,
        "done_ts": req.done_ts,
        # Хаб вернул беседу после выхода (сообщение или вступление): выход больше не в силе.
        "returned_ts": req.returned_ts,
        "updated_ts": req.updated_ts,
        # A3: «Оставить» — только пока шаг ни разу не выдавался.
        "can_decline": req.status in OPEN_STATUSES and req.first_leased_ts is None,
        # Для «Выхожу…» в UI: одобрена (шаг ждёт очереди или в работе).
        "leaving": req.status == "approved",
        "seq": req.seq,
        # Ф41-5б: принята автопилотом (без владельца) — поле есть только у таких заявок.
        **({"auto": True} if req.auto else {}),
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
    """Аудит без текстов: id заявки, беседы, проекта, аккаунта (не название и не причина)."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or f"leave-{req.id}",
        action=action,
        result=result,
        channel_id=req.channel_id,
        detail={
            "leave_id": req.id,
            "project_id": req.project_id,
            "account_id": req.account_id,
            "channel_id": req.channel_id,
            **(detail or {}),
        },
    )


# --- беседа -------------------------------------------------------------------------------------------------


class _Chan(NamedTuple):
    id: int
    account_id: int
    ext_id: str
    kind: str
    parent_channel_id: int | None
    title: str | None
    project_id: int | None
    archived: int


def _chan(conn: sqlite3.Connection, channel_id: int) -> _Chan | None:
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT id, account_id, ext_id, kind, parent_channel_id, title, project_id, archived "  # noqa: S608
        f"FROM {schema}.channel WHERE id = ?",
        (channel_id,),
    ).fetchone()
    return _Chan(*row) if row is not None else None


def _source(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def _target(conn: sqlite3.Connection, src: _Chan) -> _Chan:
    """A2: беседа, из которой выходить: группа/канал Telegram — она сама, тема форума — родитель. Личка, бот,
    Slack, почта, заготовка нового адресата — 422."""
    if _source(conn, src.account_id) not in SOURCES or src.kind not in CHANNEL_KINDS:
        raise Invalid(_NOT_GROUP)
    if draft_channels.is_temp_ext_id(src.ext_id):
        raise Invalid("это заготовка нового адресата — выходить не из чего")
    if src.kind != "topic":
        return src
    parent = _chan(conn, src.parent_channel_id) if src.parent_channel_id is not None else None
    if parent is None or parent.kind != "group":
        raise Invalid(_NOT_GROUP)
    return parent


def _last_request(conn: sqlite3.Connection, channel_id: int) -> Req | None:
    rows = _select(conn, "l.channel_id = ? ORDER BY l.id DESC LIMIT 1", (channel_id,))
    return rows[0] if rows else None


def _in_force(req: Req | None) -> bool:
    """Выход в силе: заявка `left`, и хаб беседу ещё не возвращал (`returned_ts IS NULL`)."""
    return req is not None and req.status == "left" and req.returned_ts is None


def _left_archived(conn: sqlite3.Connection, target: _Chan) -> Req | None:
    """Беседа в архиве после выхода: в архиве и последняя заявка выхода в силе."""
    if not target.archived:
        return None
    last = _last_request(conn, target.id)
    return last if _in_force(last) else None


def _check_not_left(conn: sqlite3.Connection, target: _Chan) -> None:
    left = _left_archived(conn, target)
    if left is not None:
        raise Conflict(f"уже вышли из этой беседы (заявка #{left.id})")


def open_join_id(conn: sqlite3.Connection, account_id: int, ext_id: str) -> int | None:
    """П6: открытая заявка вступления (`checking|pending|approved`) с тем же `(account_id, ext_id)`."""
    row = conn.execute(
        "SELECT id FROM main.join_request WHERE account_id = ? AND ext_id = ? "
        "AND status IN ('checking', 'pending', 'approved') ORDER BY id LIMIT 1",
        (account_id, ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def _check_no_join(conn: sqlite3.Connection, target: _Chan) -> None:
    join_id = open_join_id(conn, target.account_id, target.ext_id)
    if join_id is not None:
        raise Conflict(f"по этой беседе идёт вступление (заявка #{join_id})")


def open_leave_id(conn: sqlite3.Connection, account_id: int, ext_id: str) -> int | None:
    """П6 (для `join`): открытая заявка выхода с тем же `(account_id, ext_id)`."""
    row = conn.execute(
        "SELECT id FROM main.leave_request WHERE account_id = ? AND ext_id = ? "
        "AND status IN ('pending', 'approved') ORDER BY id LIMIT 1",
        (account_id, ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def _open_for(conn: sqlite3.Connection, channel_id: int) -> Req | None:
    rows = _select(conn, "l.channel_id = ? AND l.status IN ('pending', 'approved')", (channel_id,))
    return rows[0] if rows else None


def _account_pause(conn: sqlite3.Connection, account_id: int) -> int:
    """Пауза FloodWait аккаунта — на открытых строках; новая заявка и «Выйти» её не обходят."""
    row = conn.execute(
        "SELECT MAX(pause_until_ms) FROM main.leave_request WHERE account_id = ? "
        "AND status IN ('pending', 'approved')",
        (account_id,),
    ).fetchone()
    return int(row[0] or 0)


def _day_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%d.%m")


def _visible(project_ids: frozenset[int] | None, project_id: int | None) -> bool:
    """П12: заявки и беседы без проекта видит только принципал без списка проектов (UI-сессия и профиль с
    «Неразобранным»)."""
    return project_ids is None or (project_id is not None and project_id in project_ids)


def _insert(
    conn: sqlite3.Connection,
    target: _Chan,
    *,
    project_id: int | None,
    reason: str | None,
    origin: str,
    status: str,
    token_id: int | None,
    client_name: str | None,
    now_ms: int,
    hub_session_id: int | None = None,
) -> Req:
    cur = conn.execute(
        "INSERT INTO main.leave_request (account_id, project_id, channel_id, ext_id, chat_kind, title, "
        "reason, origin, status, token_id, client_name, created_ts, decided_ts, updated_ts, not_before_ms, "
        "seq, hub_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target.account_id,
            project_id,
            target.id,
            target.ext_id,
            "channel" if target.kind == "channel" else "group",
            target.title,
            reason or None,
            origin,
            status,
            token_id,
            client_name,
            now_ms,
            now_ms if status == "approved" else None,
            now_ms,
            max(now_ms, _account_pause(conn, target.account_id)),
            next_seq(conn),
            hub_session_id,
        ),
    )
    req = get(conn, cur.lastrowid)
    assert req is not None
    return req


# --- A2: создать заявку -------------------------------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    *,
    principal,
    channel_id: int,
    reason: str | None,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
    session: dict | None = None,
) -> tuple[Req, bool]:
    """A2 (сессия): `(заявка, новая ли)`. Запреты `check_attach` (403); беседа видна токену (404) и лежит в
    проекте (422); тема — заявка на родителя, родитель тоже в проекте токена (П7, 422); уже вышли, выход
    просит другой проект (П8), идёт вступление (П6), владелец оставил беседу за 7 дней — 409; лимиты — 429.
    Открытая заявка беседы этого же проекта — она же, без смены статуса.

    Ф56-1а (А9): `session` — тройка запроса (словарь); сессия хаба по ней пишется в `hub_session_id` новой
    заявки (привязка к каналу владельца, прав не даёт)."""
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = row[0] if row and isinstance(row[0], str) else None
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise Denied("выход — только из интерактивной сессии (не дежурная и не оркестратор)") from exc
    if reason is not None and len(reason) > REASON_MAX:
        raise Invalid(f"reason: не длиннее {REASON_MAX} символов")
    src = _chan(conn, channel_id)
    if src is None or not _visible(principal.project_ids, src.project_id):
        raise NotFound(f"беседа #{channel_id} не найдена или не видна профилю")
    target = _target(conn, src)
    if src.project_id is None:
        raise Invalid(_NO_PROJECT)
    # П7: тема с проектом, заданным вручную, — проект и видимость проверяются и у родителя.
    if target.project_id is None or not _visible(principal.project_ids, target.project_id):
        raise Invalid(_FORUM_ELSEWHERE)
    project_id = target.project_id
    _check_not_left(conn, target)

    existing = _open_for(conn, target.id)
    if existing is not None:
        if existing.project_id == project_id:
            return existing, False
        if existing.origin == "ui":
            raise Conflict(f"владелец уже выходит из этой беседы (заявка #{existing.id})")
        raise Conflict(f"выход уже просит проект {existing.project_id} (заявка #{existing.id})")
    _check_no_join(conn, target)
    declined = conn.execute(
        "SELECT MAX(decided_ts) FROM main.leave_request WHERE channel_id = ? AND status = 'declined' "
        "AND decided_ts > ?",
        (target.id, now_ms - DECLINE_MEMORY_MS),
    ).fetchone()
    if declined is not None and declined[0] is not None:
        raise Conflict(f"владелец оставил беседу {_day_month(declined[0])} — повторять просьбу неделю нельзя")

    open_count = conn.execute(
        "SELECT COUNT(*) FROM main.leave_request WHERE project_id = ? AND origin = 'session' "
        "AND status IN ('pending', 'approved')",
        (project_id,),
    ).fetchone()[0]
    if open_count >= OPEN_PER_PROJECT:
        raise TooMany(f"у проекта уже {OPEN_PER_PROJECT} открытых просьб о выходе — дождись решения владельца")
    day_count = conn.execute(
        "SELECT COUNT(*) FROM main.leave_request WHERE project_id = ? AND origin = 'session' "
        "AND created_ts > ?",
        (project_id, now_ms - DAY_MS),
    ).fetchone()[0]
    if day_count >= NEW_PER_PROJECT_DAY:
        raise TooMany(f"не больше {NEW_PER_PROJECT_DAY} просьб о выходе от проекта за сутки")

    req = _insert(
        conn,
        target,
        project_id=project_id,
        reason=reason,
        origin="session",
        status="pending",
        token_id=principal.token_id,
        client_name=client_name,
        now_ms=now_ms,
        hub_session_id=held_mod.open_id_of(conn, session),
    )
    _audit(
        conn,
        req,
        action="leave_request_created",
        result="pending",
        now_ms=now_ms,
        actor=principal.kind,
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return req, True


def create_ui(
    conn: sqlite3.Connection,
    *,
    principal,
    channel_id: int,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
) -> Req:
    """A2 (владелец, кнопка в шапке): новая заявка сразу `approved`; открытая `pending` — одобряется (решение
    владельца), `approved` — как есть. Проверки беседы — как у сессии, кроме проекта (любая беседа, и без
    проекта); уже вышли, идёт вступление — 409."""
    src = _chan(conn, channel_id)
    if src is None:
        raise NotFound(f"беседа #{channel_id} не найдена")
    target = _target(conn, src)
    _check_not_left(conn, target)
    existing = _open_for(conn, target.id)
    if existing is not None:
        if existing.status == "pending":
            return decide(
                conn,
                existing.id,
                "approve",
                now_ms=now_ms,
                api_token_id=principal.token_id,
                client_name=client_name,
                audit_request_id=request_id,
            )
        return existing
    _check_no_join(conn, target)
    req = _insert(
        conn,
        target,
        project_id=target.project_id,
        reason=None,
        origin="ui",
        status="approved",
        token_id=principal.token_id,
        client_name=client_name,
        now_ms=now_ms,
    )
    _audit(
        conn,
        req,
        action="leave_request_created",
        result="approved",
        now_ms=now_ms,
        actor="ui",
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return req


# --- A3: решение владельца ---------------------------------------------------------------------------------------


def decide(
    conn: sqlite3.Connection,
    request_id: int,
    action: str,
    *,
    now_ms: int,
    api_token_id: int | None = None,
    client_name: str | None = None,
    audit_request_id: str = "",
    auto_session: int | None = None,
) -> Req:
    """Ф41-5б (5.5, П7): `auto_session` — заявку принимает не кнопка владельца, а выключатель автопилота
    `tg_leave` для этой сессии хаба: тот же код, отметка `auto`, своё действие аудита `leave_auto`.

    `approve` — только из `pending` (идёт вступление по той же беседе — 409, П6); `decline` — из
    `pending`/`approved`, пока шаг ни разу не выдавался (иначе 409 «уже выхожу»)."""
    req = get(conn, request_id)
    if req is None:
        raise NotFound(f"заявка {request_id} не найдена")
    if action == "approve":
        if req.status != "pending":
            raise Conflict(f"одобрить можно только ждущую решения заявку (сейчас {req.status})")
        join_id = open_join_id(conn, req.account_id, req.ext_id)
        if join_id is not None:
            raise Conflict(f"по этой беседе идёт вступление (заявка #{join_id})")
        _update(
            conn,
            request_id,
            now_ms,
            status="approved",
            decided_ts=now_ms,
            attempts=0,
            not_before_ms=max(now_ms, req.pause_until_ms, _account_pause(conn, req.account_id)),
            **({"auto": 1} if auto_session is not None else {}),
        )
        audit_action = "leave_auto" if auto_session is not None else "leave_request_approved"
    elif action == "decline":
        if req.status == "approved" and req.first_leased_ts is not None:
            raise Conflict("уже выхожу — оставить нельзя")
        if req.status not in OPEN_STATUSES:
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
        audit_action = "leave_request_declined"
    else:
        raise Invalid(f"action: approve или decline, получено {action!r}")
    req = get(conn, request_id)
    assert req is not None
    _audit(
        conn,
        req,
        action=audit_action,
        result=req.status,
        now_ms=now_ms,
        actor="api" if auto_session is not None else "ui",
        request_id=audit_request_id,
        api_token_id=api_token_id,
        client_name=client_name,
        **({"detail": {"hub_session_id": auto_session}} if auto_session is not None else {}),
    )
    return req


# --- A7: выдача ---------------------------------------------------------------------------------------------


def _scope(principal, where: list[str], params: list) -> bool:
    """Скоуп проектов принципала в `where`; `False` — ему не видно ничего."""
    if principal.project_ids is None:
        return True
    ids = sorted(principal.project_ids)
    if not ids:
        return False
    where.append(f"l.project_id IN ({','.join('?' for _ in ids)})")
    params += ids
    return True


def list_requests(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    project_id: int | None = None,
    channel_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """`GET /leaves`: видимые проекты принципала (без проекта — только без списка проектов, П12); по
    умолчанию — открытые и закрытые за 7 дней, новые первыми. `status` — один статус (`open` — все открытые).
    `channel_id` (П12) — заявки беседы; у темы форума — её родителя."""
    where = ["1 = 1"]
    params: list = []
    if not _scope(principal, where, params):
        return []
    if project_id is not None:
        where.append("l.project_id = ?")
        params.append(project_id)
    if channel_id is not None:
        src = _chan(conn, channel_id)
        target_id = src.parent_channel_id if src is not None and src.kind == "topic" else channel_id
        where.append("l.channel_id = ?")
        params.append(target_id if target_id is not None else channel_id)
    if status == "open":
        where.append("l.status IN ('pending', 'approved')")
    elif status is not None:
        where.append("l.status = ?")
        params.append(status)
    else:
        where.append("(l.status IN ('pending', 'approved') OR l.updated_ts > ?)")
        params.append(now_ms - LIST_RECENT_MS)
    limit = max(1, min(int(limit), LIST_MAX))
    rows = _select(conn, " AND ".join(where) + " ORDER BY l.id DESC LIMIT ?", [*params, limit])
    return [request_dict(r) for r in rows]


def changes(conn: sqlite3.Connection, principal, *, since: int, watermark: int, limit: int) -> list[dict]:
    """A7: заявки с `since < seq <= watermark` в скоупе принципала, по возрастанию `seq` (`/sync`, SSE)."""
    where = ["l.seq > ? AND l.seq <= ?"]
    params: list = [since, watermark]
    if not _scope(principal, where, params):
        return []
    rows = _select(conn, " AND ".join(where) + " ORDER BY l.seq LIMIT ?", [*params, limit])
    return [request_dict(r) for r in rows]


# --- A4: очередь коннектору ---------------------------------------------------------------------------------


def _account(conn: sqlite3.Connection, account_id: int) -> tuple[str, str, str] | None:
    """`(схема, статус, источник)` аккаунта или `None`."""
    row = conn.execute("SELECT db, status, source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


def _blocked(conn: sqlite3.Connection, account_id: int) -> bool:
    info = _account(conn, account_id)
    return info is None or info[1] in _BLOCKED_STATUSES or info[2] not in SOURCES


def _candidates(conn: sqlite3.Connection, account_id: int) -> list[tuple[int, Req]]:
    """`(срок, заявка)` одобренных заявок аккаунта (старший `decided_ts` первым). Срок — max паузы строки,
    лиза и темпа аккаунта (`LEAVE_GAP_MS` от последней выдачи)."""
    last = conn.execute(
        "SELECT MAX(last_leased_ts) FROM main.leave_request WHERE account_id = ?", (account_id,)
    ).fetchone()[0]
    gap_due = (last + LEAVE_GAP_MS) if last is not None else 0
    rows = _select(
        conn, "l.account_id = ? AND l.status = 'approved' ORDER BY l.decided_ts, l.id", (account_id,)
    )
    return [(max(r.not_before_ms, r.pause_until_ms, r.lease_until_ms or 0, gap_due), r) for r in rows]


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """A4: задания своего аккаунта `{id, lease_gen, step: "leave", ext_id, chat_kind, project_id}`, лиз
    `LEASE_MS`; `limited`/`auth_required` — пусто, строки ждут."""
    if _blocked(conn, account_id):
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    items: list[dict] = []
    while len(items) < limit:
        ready = [(due, r) for due, r in _candidates(conn, account_id) if due <= now_ms]
        if not ready:
            break
        _due, req = ready[0]
        sets: dict = {
            "lease_until_ms": now_ms + LEASE_MS,
            "lease_gen": req.lease_gen + 1,
            "last_leased_ts": now_ms,
        }
        if req.first_leased_ts is None:
            sets["first_leased_ts"] = now_ms  # граница «Оставить»
        _update(conn, req.id, now_ms, **sets)
        items.append(
            {
                "id": req.id,
                "lease_gen": req.lease_gen + 1,
                "step": "leave",
                "ext_id": req.ext_id,
                "chat_kind": req.chat_kind,
                "project_id": req.project_id,
            }
        )
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait` (как у вступления): `(готово, ближайший срок в мс)`."""
    if _blocked(conn, account_id):
        return False, None
    dues = [due for due, _r in _candidates(conn, account_id)]
    if not dues:
        return False, None
    due = min(dues)
    if due <= now_ms:
        return True, None
    return False, due


def backoff_ms(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки (с 1): 30 с, 60 с, 120 с, …"""
    return BACKOFF_BASE_MS * 2 ** (max(attempt, 1) - 1)


def _pause_account(conn: sqlite3.Connection, account_id: int, until_ms: int, now_ms: int) -> None:
    """FloodWait — пауза ВСЕМ открытым заявкам выхода аккаунта (в строках — переживает рестарт)."""
    for req in _select(
        conn,
        "l.account_id = ? AND l.status IN ('pending', 'approved') AND "
        "(l.pause_until_ms < ? OR l.not_before_ms < ?)",
        (account_id, until_ms, until_ms),
    ):
        _update(
            conn,
            req.id,
            now_ms,
            pause_until_ms=max(req.pause_until_ms, until_ms),
            not_before_ms=max(req.not_before_ms, until_ms),
        )


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


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
) -> str:
    """A4: `retry` с `retry_after_ms` (FloodWait) — пауза всем открытым заявкам аккаунта, попыткой не
    считается; без — бэкофф, 5-я неудача -> `failed`/`gave_up`. `drop` -> `failed` с причиной. `done` ->
    `left` (`result`: `left` | `not_member`) и A5. Итог для журнала — новый статус (или `paused`)."""
    if outcome not in REPORT_OUTCOMES:
        raise Invalid(f"outcome: {outcome!r}")
    req = get(conn, request_id)
    if req is None or req.account_id != account_id:
        raise NotFound(f"leave {request_id}")
    if req.lease_gen != lease_gen or req.status != "approved":
        raise StaleLease(f"leave {request_id}: lease_gen {lease_gen} != {req.lease_gen} или шаг закрыт")

    if outcome == "retry":
        if retry_after_ms:
            _update(conn, request_id, now_ms, lease_until_ms=None)
            _pause_account(conn, account_id, now_ms + retry_after_ms, now_ms)
            return "paused"
        attempt = req.attempts + 1
        if attempt >= MAX_ATTEMPTS:
            _fail(conn, request_id, "gave_up", now_ms)
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
        _fail(conn, request_id, _clip(reason, ERROR_MAX) or "other", now_ms)
        return "failed"

    if result not in RESULTS:
        raise Invalid("result: left или not_member")
    _update(conn, request_id, now_ms, status="left", result=result, done_ts=now_ms, lease_until_ms=None)
    _after_left(conn, req, now_ms)
    left = get(conn, request_id)
    assert left is not None
    _audit(conn, left, action="leave_request_left", result=result, now_ms=now_ms)
    return "left"


def _fail(conn: sqlite3.Connection, request_id: int, error: str, now_ms: int) -> None:
    _update(conn, request_id, now_ms, status="failed", error=error, done_ts=now_ms, lease_until_ms=None)
    req = get(conn, request_id)
    assert req is not None
    _audit(conn, req, action="leave_request_failed", result="failed", now_ms=now_ms, detail={"error": error})


# --- A5: после выхода ---------------------------------------------------------------------------------------


def _topic_ids(conn: sqlite3.Connection, parent_id: int) -> list[int]:
    schema = db_of(parent_id)
    rows = conn.execute(
        f"SELECT id FROM {schema}.channel WHERE parent_channel_id = ? ORDER BY id",  # noqa: S608
        (parent_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _set_archived(conn: sqlite3.Connection, channel_id: int, archived: bool, now_ms: int) -> bool:
    """Архив беседы обычным путём (`update_channel`: `seq`, при архиве — закрытие карточек разборщика). Уже в
    нужном состоянии — ничего (без лишнего `seq`)."""
    chan = _chan(conn, channel_id)
    if chan is None or bool(chan.archived) == archived:
        return False
    channels_mod.update_channel(conn, channel_id, archived=archived, now_ms=now_ms)
    return True


def _after_left(conn: sqlite3.Connection, req: Req, now_ms: int) -> None:
    """A5 + П9, строго по порядку: (1) архив беседы и всех её тем (закрывает карточки разборщика); (2) ещё не
    выданные сигналы подписке на проект по ним — `skipped` (не будят сессию «каждое сообщение»); (3) живые
    подписки сессий на эти беседы — `channel_gone` (П10; `end` зовёт добор 13-1 и открытых карточек уже не
    найдёт)."""
    ids = [req.channel_id, *_topic_ids(conn, req.channel_id)]
    archived = [channel_id for channel_id in ids if _set_archived(conn, channel_id, True, now_ms)]
    # Что заархивировали сами — только это снимет возврат (архив владельца до выхода остаётся).
    _update(conn, req.id, now_ms, archived_ids_json=json.dumps(archived))
    chan_subs.skip_project_signals(conn, ids, reason="left", now_ms=now_ms)
    placeholders = ",".join("?" for _ in ids)
    subs = conn.execute(
        f"SELECT id FROM main.session_chan_sub WHERE channel_id IN ({placeholders}) "  # noqa: S608
        "AND ended_ts IS NULL ORDER BY id",
        ids,
    ).fetchall()
    for (sub_id,) in subs:
        chan_subs.end(conn, sub_id, "channel_gone", now_ms=now_ms, actor="core", request_id=f"leave-{req.id}")


# --- A6 + П2/П3: возврат из архива --------------------------------------------------------------------------


def _returning_ids(conn: sqlite3.Connection, req: Req) -> list[int]:
    """Что снимает возврат: беседы, которые A5 сам перевёл в архив (`archived_ids_json`), и темы форума,
    появившиеся после выхода (`first_seen_ts > done_ts`). Архив владельца, сделанный до выхода, не трогается."""
    ids: list[int] = json.loads(req.archived_ids_json) if req.archived_ids_json else []
    if req.done_ts is not None:
        schema = db_of(req.channel_id)
        rows = conn.execute(
            f"SELECT id FROM {schema}.channel WHERE parent_channel_id = ? AND first_seen_ts > ? "  # noqa: S608
            "ORDER BY id",
            (req.channel_id, req.done_ts),
        ).fetchall()
        ids += [r[0] for r in rows if r[0] not in ids]
    return ids


def _return(conn: sqlite3.Connection, req: Req, now_ms: int, *, why: str) -> bool:
    """Возврат после выхода: снять архив с того, что заархивировал выход (и с тем, появившихся после него),
    отметить `returned_ts` — выход больше не в силе; аудит `leave_request_returned`. `True` — что-то сняли."""
    changed = False
    for channel_id in _returning_ids(conn, req):
        changed = _set_archived(conn, channel_id, False, now_ms) or changed
    _update(conn, req.id, now_ms, returned_ts=now_ms)
    if changed:
        _audit(conn, req, action="leave_request_returned", result="ok", now_ms=now_ms, detail={"why": why})
    return changed


def on_message_inserted(conn: sqlite3.Connection, schema: str, channel_id: int, ts: int, now_ms: int) -> bool:
    """A6 + П3: хук `ingest` на вставку НОВОГО сообщения. Беседа (у темы — её родитель) в архиве, её
    последний выход в силе (`left`, `returned_ts IS NULL`), и сообщение позже выхода больше чем на
    `RETURN_GRACE_MS` — возврат (`_return`: архив снимается только с того, что заархивировал выход). Прочие
    архивы (Ф11-9c, заготовки, архив владельца после возврата) не трогаются: выхода в силе нет."""
    row = conn.execute(
        f"SELECT archived, parent_channel_id FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if row is None:
        return False
    archived, parent_id = row
    target_id = channel_id
    if parent_id is not None:
        # Тема: решает архив РОДИТЕЛЯ (П3 — тема, созданная без владельца, приходит без архива). Тема в архиве
        # при родителе не в архиве — архив Ф11-9c (форум уже возвращён), его не трогаем.
        parent = conn.execute(
            f"SELECT archived FROM {schema}.channel WHERE id = ?",  # noqa: S608
            (parent_id,),
        ).fetchone()
        if parent is None:
            return False
        archived = parent[0]
        target_id = parent_id
    if not archived:
        return False
    last = _last_request(conn, target_id)
    if last is None or not _in_force(last) or last.done_ts is None:
        return False
    if ts <= last.done_ts + RETURN_GRACE_MS:
        return False
    return _return(conn, last, now_ms, why="message")


def on_joined(conn: sqlite3.Connection, account_id: int, ext_id: str | None, now_ms: int) -> bool:
    """П2: вступление (`joined`/`already`) в беседу, из которой вышли, — архив снимается сразу, не дожидаясь
    первого поста (граница `first_seen_ts` у `join.bind` беседу, известную хабу, не трогает)."""
    if not ext_id:
        return False
    info = _account(conn, account_id)
    if info is None:
        return False
    row = conn.execute(
        f"SELECT id FROM {info[0]}.channel WHERE account_id = ? AND ext_id = ?",  # noqa: S608
        (account_id, ext_id),
    ).fetchone()
    if row is None:
        return False
    target = _chan(conn, row[0])
    if target is None:
        return False
    last = _left_archived(conn, target)
    if last is None:
        return False
    return _return(conn, last, now_ms, why="join")
