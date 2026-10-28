"""Заявки сессий на правило раскладки (Ф38-2, docs/architecture.md — они главнее текста выше них).

Сессия предлагает правило (`create`, bearer `draft`, запреты `check_attach`), владелец заводит его кнопкой
«Завести» или отклоняет (`decide`, только UI-сессия). Правило по-прежнему создаёт только владелец: заявка —
предложение, запись правила идёт той же связкой, что `POST /map-rules` (`mapping.check_rule_target` ->
`mapping.add_rule` -> аудит `map_rule_add`). Образец — заявки на вступление Ф17-1 (`join.py`) без очереди
коннектора, темпа и автоодобрения.

П1 (граница): названия чужих бесед, папок и аккаунтов уходят только принципалу, который видит все проекты
(`full_view`); прочим — число `moved`, описание без названий из хаба и отказ по папке без перечня папок.

Каждая запись в строку заявки двигает глобальный `seq`: `/sync` (поле `rule_requests`) и кадр SSE
`rule_request`. Пишет только внутри `Db.write()`, время — параметром `now_ms`. Импорт `queries` здесь нельзя
(`queries` -> этот модуль): видимость проекта — по `principal.project_ids`.
"""

import json
import sqlite3
from datetime import datetime
from typing import NamedTuple

from . import held as held_mod
from . import mapping, subs
from .audit import write_audit
from .db import next_seq
from .security import check_attach

# Решение 2: `channel` заявкой не подаётся — одну беседу сессия переносит сама (`hub_move_channel`).
KINDS = ("domain", "label", "folder", "all")
STATUSES = ("pending", "approved", "declined")

REASON_MAX = 300
VALUE_MAX = 300
EXAMPLES_MAX = 3

_MIN_MS = 60_000
DAY_MS = 24 * 60 * _MIN_MS
# Решение 5: от спама карточками.
OPEN_PER_PROJECT = 10
NEW_PER_PROJECT_DAY = 30
DECLINE_MEMORY_MS = 7 * DAY_MS
LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200

# П1: отказ по папке принципалу с ограниченным списком проектов — без перечня названий папок.
_FOLDER_REFUSAL = "папка: у аккаунта нет такой папки"


class RuleRequestError(Exception):
    """Базовая ошибка; текст — для человека/сессии."""


class Invalid(RuleRequestError):
    """422: вид, значение или причина не годятся."""


class Denied(RuleRequestError):
    """403: этому токену предлагать правила нельзя."""


class NotFound(RuleRequestError):
    """404: заявки, проекта или аккаунта нет (или проект не виден принципалу)."""


class Conflict(RuleRequestError):
    """409: такое правило уже есть, условие ведёт в другой проект, владелец отказал недавно, решение не к этому
    статусу, условие перестало собираться."""


class TooMany(RuleRequestError):
    """429: лимиты заявок проекта (решение 5)."""


# --- строки -------------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "seq",
    "account_id",
    "project_id",
    "kind",
    "value_json",
    "match_key",
    "reason",
    "who",
    "status",
    "token_id",
    "client_name",
    "created_ts",
    "decided_ts",
    "rule_id",
    "moved",
    "auto",
)
_SELECT = ", ".join(f"r.{c}" for c in _COLUMNS)


class Req(NamedTuple):
    id: int
    seq: int
    account_id: int
    project_id: int
    kind: str
    value_json: str | None
    match_key: str
    reason: str | None
    who: str | None
    status: str
    token_id: int | None
    client_name: str | None
    created_ts: int
    decided_ts: int | None
    rule_id: int | None
    moved: int | None
    auto: int = 0  # Ф41-5б (5.5): принята автопилотом


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Req]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.rule_request r WHERE {where}",  # noqa: S608 — where — из кода
        tuple(params),
    ).fetchall()
    return [Req(*r) for r in rows]


def get(conn: sqlite3.Connection, request_id: int) -> Req | None:
    rows = _select(conn, "r.id = ?", (request_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, request_id: int, **sets) -> None:
    """Любая запись в заявку двигает `seq` (решение 9)."""
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.rule_request SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), request_id],
    )


def _value(req: Req):
    if req.value_json is None:
        return None
    try:
        return json.loads(req.value_json)
    except ValueError:
        return None


def _bare_match(kind: str, value) -> dict:
    """Условие без `label` из вида и сохранённого значения — без обращения к БД (аккаунт и папка могли
    пропасть, П4); то же, что `build_match` без `label`."""
    if kind == "all":
        return {"all": True}
    attr = {"folder": "tg_folders", "label": "labels", "domain": "from_domains"}[kind]
    return {"attr": attr, "has": value}


# --- выдача -------------------------------------------------------------------------------------------------


def full_view(principal) -> bool:
    """П1: названия бесед, папок и аккаунта — только тому, кто видит все проекты (UI-сессия владельца или bearer
    без списка проектов). Одно правило на `POST`, `GET` и `/sync`."""
    return principal.is_ui or principal.project_ids is None


def _visible(principal, project_id: int) -> bool:
    return principal.project_ids is None or project_id in principal.project_ids


def _plain_description(kind: str, value) -> str:
    """П1: описание без названий из хаба — значение из самой заявки (оно и так принадлежит проекту-цели)."""
    if kind == "all":
        return "весь аккаунт"
    if kind == "folder":
        return f"папка {value}"
    if kind == "label":
        return f"метка «{value}»"
    return f"отправитель @{value}"


def request_dict(conn: sqlite3.Connection, req: Req, *, full: bool) -> dict:
    """Выдача API (`/rule-requests`, `/sync`): без токена и ключа условия. `description`, `reason`,
    `examples`, `account_title` — данные (их пишут сессия и посторонние): UI показывает только текстом, MCP —
    внутри блока данных. Решение 8: у `pending` `moved` и `examples` считаются при выдаче
    (`mapping.preview`), у `approved` `moved` — сохранённое. П1: без `full` — ни названий бесед, ни названия
    аккаунта, ни названия папки. П4: аккаунт пропал — `account_source`/`account_title` пусты, `moved` —
    `null`."""
    value = _value(req)
    match = _bare_match(req.kind, value)
    account = conn.execute(
        "SELECT source, title FROM main.account WHERE id = ?", (req.account_id,)
    ).fetchone()
    description = (
        mapping.describe_rule(conn, req.account_id, match)[2] if full else _plain_description(req.kind, value)
    )
    moved = req.moved
    examples = None
    if req.status == "pending":
        try:
            preview = mapping.preview(
                conn, req.account_id, add=(match, req.project_id, mapping.default_priority(match))
            )
        except mapping.MappingError:
            moved = None
        else:
            moved = preview["moved"]
            if full:
                titles = [e["title"] for e in preview["examples"] if e.get("title")]
                examples = titles[:EXAMPLES_MAX]
    return {
        "id": req.id,
        "seq": req.seq,
        "status": req.status,
        "project_id": req.project_id,
        "account_id": req.account_id,
        "account_source": account[0] if account is not None else None,
        "account_title": account[1] if account is not None and full else None,
        "kind": req.kind,
        "value": value,
        "description": description,
        "reason": req.reason,
        "who": req.who,
        "created_ts": req.created_ts,
        "decided_ts": req.decided_ts,
        # Ф41-5б: принята автопилотом (без владельца) — поле есть только у таких заявок.
        **({"auto": True} if req.auto else {}),
        "rule_id": req.rule_id,
        "moved": moved,
        "examples": examples,
    }


def _audit(
    conn: sqlite3.Connection,
    req: Req,
    *,
    action: str,
    result: str,
    now_ms: int,
    actor: str,
    request_id: str = "",
    api_token_id: int | None = None,
    client_name: str | None = None,
    detail: dict | None = None,
) -> None:
    """Аудит без текстов: id заявки, проект, аккаунт, вид условия (не значение и не причина)."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or f"rule-request-{req.id}",
        action=action,
        result=result,
        detail={
            "rule_request_id": req.id,
            "project_id": req.project_id,
            "account_id": req.account_id,
            "kind": req.kind,
            **(detail or {}),
        },
    )


# --- создание (решения 2–5, 7; П1–П3) -----------------------------------------------------------------------


def _who(conn: sqlite3.Connection, token_id: int, project_id: int, session: dict | None) -> str | None:
    """Решение 7: имя агента — роль живой подписки ЭТОЙ сессии ЭТИМ токеном на любой проект (своя подписка на
    проект-цель — первой), с номером через пробел, если он ≥ 2 («маркетолог», «маркетолог 2» — как имя агента
    в текстах MCP). Только роль строгой формы (`subs.ROLE_RE`); сессия не опознана, процесс не жив, подписки с
    ролью нет — `None`."""
    if not session:
        return None
    pid, proc_start, session_id = session.get("pid"), session.get("proc_start"), session.get("session_id")
    if not isinstance(pid, int) or not isinstance(proc_start, int) or not isinstance(session_id, str):
        return None
    if not subs.is_alive(pid, proc_start, session.get("host")):
        return None
    row = conn.execute(
        "SELECT role, role_no FROM main.session_sub WHERE api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL AND role IS NOT NULL "
        "ORDER BY (project_id = ?) DESC, id DESC LIMIT 1",
        (token_id, pid, proc_start, session_id, project_id),
    ).fetchone()
    if row is None:
        return None
    role, role_no = row
    if not isinstance(role, str) or not subs.ROLE_RE.fullmatch(role) or "--" in role:
        return None
    return f"{role} {role_no}" if isinstance(role_no, int) and role_no >= 2 else role


def _day_month(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%d.%m")


def _reason(reason: str | None) -> str | None:
    """Решение 7: причина — одна строка (переводы строк — в пробел), не длиннее `REASON_MAX`."""
    if reason is None:
        return None
    if len(reason) > REASON_MAX:
        raise Invalid(f"reason: не длиннее {REASON_MAX} символов")
    return " ".join(reason.split()) or None


def create(
    conn: sqlite3.Connection,
    *,
    principal,
    project_id: int,
    account_id: int,
    kind: str,
    value,
    reason: str | None,
    session: dict | None,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
) -> tuple[dict, bool]:
    """`(заявка, новая ли)`. Проверки по порядку: запреты `check_attach` (403) -> проект виден и существует
    (404; П1 — ДО условия) -> вид из четырёх и причина (422) -> условие и аккаунт
    (`mapping.check_rule_target`: 422 текстом ядра / 404 / 409 «такое правило уже есть») -> условие уже ведёт
    в другой проект правилом или
    открытой заявкой (409, П3) -> открытая заявка той же тройки — она же -> отказ владельца за 7 дней (409) ->
    лимиты проекта (429)."""
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = row[0] if row and isinstance(row[0], str) else None
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise Denied(
            "правило раскладки предлагает только интерактивная сессия (не дежурная и не оркестратор)"
        ) from exc
    full = full_view(principal)
    if (
        project_id <= 0
        or not _visible(principal, project_id)
        or conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is None
    ):
        raise NotFound("project_id: нет такого проекта или он не виден профилю")
    if kind == "channel":
        raise Invalid(
            "kind: одну беседу переноси сам — hub_move_channel; заявкой — domain, label, folder, all"
        )
    if kind not in KINDS:
        raise Invalid(f"kind: неизвестный вид условия {kind!r} — domain, label, folder или all")
    reason = _reason(reason)
    if isinstance(value, str) and len(value) > VALUE_MAX:
        raise Invalid(f"value: не длиннее {VALUE_MAX} символов")
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        raise Invalid("value: номер папки слишком велик")

    try:
        match, storable = mapping.check_rule_target(conn, account_id, kind, value, project_id)
    except mapping.InvalidMatch as exc:
        # П1: текст ядра по папке перечисляет названия папок аккаунта — их видит только `full`.
        if kind == "folder" and not full and isinstance(value, int) and not isinstance(value, bool):
            raise Invalid(_FOLDER_REFUSAL) from exc
        raise Invalid(str(exc)) from exc
    except mapping.NotFound as exc:
        raise NotFound(str(exc)) from exc
    except mapping.DuplicateRule as exc:
        raise Conflict(str(exc)) from exc
    other_rule = mapping.find_other_project(conn, account_id, storable, project_id)
    if other_rule is not None:
        raise Conflict(f"это условие уже ведёт в другой проект (правило #{other_rule})")
    key = mapping.match_key(storable)

    open_rows = _select(
        conn, "r.account_id = ? AND r.match_key = ? AND r.status = 'pending'", (account_id, key)
    )
    for existing in open_rows:
        if existing.project_id == project_id:
            return request_dict(conn, existing, full=full), False
    if open_rows:
        raise Conflict(f"с этим условием уже лежит заявка #{open_rows[0].id} в другой проект")
    declined = conn.execute(
        "SELECT MAX(decided_ts) FROM main.rule_request WHERE account_id = ? AND project_id = ? "
        "AND match_key = ? AND status = 'declined' AND decided_ts > ?",
        (account_id, project_id, key, now_ms - DECLINE_MEMORY_MS),
    ).fetchone()
    if declined is not None and declined[0] is not None:
        raise Conflict(f"владелец отказал {_day_month(declined[0])} — повторять эту заявку неделю нельзя")

    open_count = conn.execute(
        "SELECT COUNT(*) FROM main.rule_request WHERE project_id = ? AND status = 'pending'", (project_id,)
    ).fetchone()[0]
    if open_count >= OPEN_PER_PROJECT:
        raise TooMany(f"у проекта уже {OPEN_PER_PROJECT} открытых заявок на правило — дождись решения владельца")
    day_count = conn.execute(
        "SELECT COUNT(*) FROM main.rule_request WHERE project_id = ? AND created_ts > ?",
        (project_id, now_ms - DAY_MS),
    ).fetchone()[0]
    if day_count >= NEW_PER_PROJECT_DAY:
        raise TooMany(f"не больше {NEW_PER_PROJECT_DAY} заявок на правило проекта за сутки")

    stored_value = match.get("has")  # нормализованное значение (домен — в нижнем регистре); `all` — None
    cur = conn.execute(
        "INSERT INTO main.rule_request (seq, account_id, project_id, kind, value_json, match_key, reason, "
        "who, status, token_id, client_name, created_ts, hub_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (
            next_seq(conn),
            account_id,
            project_id,
            kind,
            json.dumps(stored_value, ensure_ascii=False) if stored_value is not None else None,
            key,
            reason,
            _who(conn, principal.token_id, project_id, session),
            principal.token_id,
            client_name,
            now_ms,
            # Ф56-1а (А9): сессия хаба просителя — по условной тройке (привязка к каналу владельца, прав не даёт).
            held_mod.open_id_of(conn, session),
        ),
    )
    req = get(conn, cur.lastrowid)
    assert req is not None
    _audit(
        conn,
        req,
        action="rule_request_create",
        result="pending",
        now_ms=now_ms,
        actor=principal.kind,
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return request_dict(conn, req, full=full), True


# --- выдача списком и для `/sync` ---------------------------------------------------------------------------


def _scope(principal) -> tuple[str, list] | None:
    if principal.project_ids is None:
        return "1 = 1", []
    ids = sorted(principal.project_ids)
    if not ids:
        return None
    return f"r.project_id IN ({','.join('?' for _ in ids)})", ids


def list_requests(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    project_id: int | None = None,
    status: str | None = None,
    limit: int = LIST_MAX,
) -> list[dict]:
    """`GET /rule-requests`: видимые проекты принципала; по умолчанию — открытые и решённые за 7 дней, свежие
    первыми. `status` — один статус (`open` = `pending`)."""
    scope = _scope(principal)
    if scope is None:
        return []
    where, params = [scope[0]], list(scope[1])
    if project_id is not None:
        where.append("r.project_id = ?")
        params.append(project_id)
    if status in ("open", "pending"):
        where.append("r.status = 'pending'")
    elif status is not None:
        where.append("r.status = ?")
        params.append(status)
    else:
        where.append("(r.status = 'pending' OR r.decided_ts > ?)")
        params.append(now_ms - LIST_RECENT_MS)
    limit = max(1, min(int(limit), LIST_MAX))
    rows = _select(conn, " AND ".join(where) + " ORDER BY r.id DESC LIMIT ?", [*params, limit])
    full = full_view(principal)
    return [request_dict(conn, r, full=full) for r in rows]


def changes(conn: sqlite3.Connection, principal, *, since: int, watermark: int, limit: int) -> list[dict]:
    """П6: заявки с `since < seq <= watermark` в скоупе проекта, по возрастанию `seq` (`/sync`, SSE)."""
    scope = _scope(principal)
    if scope is None:
        return []
    rows = _select(
        conn,
        f"r.seq > ? AND r.seq <= ? AND {scope[0]} ORDER BY r.seq LIMIT ?",
        [since, watermark, *scope[1], limit],
    )
    full = full_view(principal)
    return [request_dict(conn, r, full=full) for r in rows]


AUDIT_CHANNELS_MAX = 200


def _project_channels(conn: sqlite3.Connection, account_id: int, project_id: int) -> set[int]:
    """Беседы аккаунта, лежащие в проекте (обе базы) — разница «до» и «после» даёт переложенное правилом."""
    found: set[int] = set()
    for schema in ("main", "work"):
        rows = conn.execute(
            f"SELECT id FROM {schema}.channel WHERE account_id = ? AND project_id = ?",  # noqa: S608
            (account_id, project_id),
        ).fetchall()
        found.update(row[0] for row in rows)
    return found


# --- решение владельца (решение 6, П4) ---------------------------------------------------------------------------


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
) -> dict:
    """Ф41-5б (5.5, П6, П7): `auto_session` — правило заводит не кнопка владельца, а выключатели автопилота
    `map_rule` и `move_channel` для этой сессии хаба: тот же код, отметка `auto`, своё действие аудита
    `rule_request_auto` с числом и номерами переложенных бесед.

    «Завести» и «Отклонить» — только из `pending` (иначе 409). «Завести»: условие строится ЗАНОВО из
    `kind`/`value` и идёт той же связкой, что `POST /map-rules`; такое правило в этот проект уже есть —
    заявка `approved` с `rule_id` существующего и `moved = 0`; условие больше не собирается (папку удалили,
    аккаунта или проекта нет — `InvalidMatch`/`NotFound`, П4) — 409 с текстом ядра, заявка остаётся
    `pending`. Аудит `map_rule_add` (как в `routes/organize.py`) и `rule_request_decide` (actor `ui`)."""
    req = get(conn, request_id)
    if req is None:
        raise NotFound(f"заявка {request_id} не найдена")
    if action not in ("approve", "decline"):
        raise Invalid(f"action: approve или decline, получено {action!r}")
    if req.status != "pending":
        raise Conflict(f"заявка уже решена ({req.status})")
    detail: dict = {}
    actor = "api" if auto_session is not None else "ui"
    before = _project_channels(conn, req.account_id, req.project_id) if auto_session is not None else set()
    if action == "approve":
        try:
            _match, storable = mapping.check_rule_target(
                conn, req.account_id, req.kind, _value(req), req.project_id
            )
        except (mapping.InvalidMatch, mapping.NotFound) as exc:
            raise Conflict(f"правило не завести: {exc}") from exc
        except mapping.DuplicateRule as exc:
            rule_id, moved = exc.rule_id, 0
        else:
            priority = mapping.default_priority(storable)
            rule_id, moved = mapping.add_rule(
                conn,
                account_id=req.account_id,
                project_id=req.project_id,
                match=storable,
                priority=priority,
                now_ms=now_ms,
            )
            write_audit(
                conn,
                now_ms=now_ms,
                actor=actor,
                api_token_id=api_token_id,
                client_name=client_name,
                request_id=audit_request_id or f"rule-request-{req.id}",
                action="map_rule_add",
                result="ok",
                detail={
                    "rule_id": rule_id,
                    "account_id": req.account_id,
                    "project_id": req.project_id,
                    "match": storable,
                    "priority": priority,
                    "channels_relaid": moved,
                },
            )
        _update(
            conn,
            request_id,
            status="approved",
            decided_ts=now_ms,
            rule_id=rule_id,
            moved=moved,
            **({"auto": 1} if auto_session is not None else {}),
        )
        detail = {"rule_id": rule_id, "moved": moved}
        if auto_session is not None:
            after = _project_channels(conn, req.account_id, req.project_id)
            detail["hub_session_id"] = auto_session
            detail["channels"] = sorted(after - before)[:AUDIT_CHANNELS_MAX]
    else:
        _update(conn, request_id, status="declined", decided_ts=now_ms)
    req = get(conn, request_id)
    assert req is not None
    _audit(
        conn,
        req,
        action="rule_request_auto" if auto_session is not None else "rule_request_decide",
        result=req.status,
        now_ms=now_ms,
        actor=actor,
        request_id=audit_request_id,
        api_token_id=api_token_id,
        client_name=client_name,
        detail=detail,
    )
    return request_dict(conn, req, full=True)
