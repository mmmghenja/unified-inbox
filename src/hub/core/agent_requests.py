"""Заявки агентов владельцу (Ф41-5б, docs/architecture.md–5.3 и поправки П5, П9, П12, П13).

Два вида: `move_channel` — перенести беседу в проект (когда сам перенос ядро не пропустило: он расширяет
видимость беседы, либо проситель — сессия хаба в авто со стоящим выключателем `move_channel`); `new_session`
— завести напарника (сессию хаба с ролью) и поручить ему задачу. Таблица `main.agent_request` (миграция
`0061_main.sql`).

Заявку подаёт только интерактивная bearer-сессия (`check_attach`); решает владелец в интерфейсе («Перенести» /
«Завести» / «Нет»). Проходит СРАЗУ (`auto = 1`, аудит `agent_request_auto`) только когда проситель — сессия
хаба, которой действие разрешено выключателем автопилота (`autopilot.session_free`: ключ запуска, режим
`auto`, свой проект, выключатель вида снят); у напарника — ещё и не сама заведённая агентом (П13).

Исход заявки, решённой владельцем, — сообщение «ОТ ВЛАДЕЛЬЦА» линии роли просителя: только номера и постоянные слова
плюс слова владельца (П9); принятая автопилотом сообщения не порождает (исход — в ответе инструмента).

Все функции записи — только внутри `Db.write()`, время — параметром `now_ms`. Аудит — без текстов.
"""

import json
import sqlite3
from typing import NamedTuple

from . import autopilot as autopilot_mod
from . import autosend as autosend_mod
from . import channels as channels_mod
from . import draft_channels, queries
from . import held as held_mod
from . import hub_sessions as hub_sessions_mod
from . import peer_msgs as peer_msgs_mod
from . import rule_requests as rule_requests_mod
from . import subs as subs_mod
from .audit import write_audit
from .db import db_of, next_seq
from .security import check_attach

KINDS = ("move_channel", "new_session")
STATUSES = ("pending", "approved", "declined")
SWITCH = {"move_channel": "move_channel", "new_session": "new_session"}

REASON_MAX = 300
NOTE_MAX = 300
TASK_MAX = 4000
FIELD_MAX = 100
OPEN_PER_KIND = 5  # открытых заявок на проект и вид
TOKEN_PER_DAY = 20
DAY_MS = 24 * 60 * 60 * 1000
LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200
STALE_NOTE = "устарела"


class AgentRequestError(Exception):
    """Отказ: `status` — HTTP-код, `detail` — готовый русский текст."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class _Stale(Exception):
    """Перенос больше не нужен или невозможен: беседы нет, она в архиве или уже в проекте."""


_COLUMNS = (
    "id",
    "seq",
    "kind",
    "project_id",
    "payload_json",
    "match_key",
    "reason",
    "who",
    "token_id",
    "sub_id",
    "hub_session_id",
    "status",
    "auto",
    "note",
    "result_json",
    "created_ts",
    "decided_ts",
)
_SELECT = ", ".join(f"r.{c}" for c in _COLUMNS)


class Req(NamedTuple):
    id: int
    seq: int
    kind: str
    project_id: int
    payload_json: str
    match_key: str
    reason: str | None
    who: str | None
    token_id: int | None
    sub_id: int | None
    hub_session_id: int | None
    status: str
    auto: int
    note: str | None
    result_json: str | None
    created_ts: int
    decided_ts: int | None


class Done(NamedTuple):
    """Итог создания или решения: заявка (публичная форма), новая ли, и сессия хаба, заведённая этим вызовом
    (`None` — не заводилась) — ручка после коммита добавляет её в реестр незакрытых."""

    request: dict
    created: bool = False
    session: held_mod.HubSession | None = None


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Req]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.agent_request r WHERE {where}",  # noqa: S608 — колонки константа
        params,
    ).fetchall()
    return [Req(*row) for row in rows]


def get(conn: sqlite3.Connection, request_id: int) -> Req | None:
    rows = _select(conn, "r.id = ?", (request_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, request_id: int, **sets) -> None:
    sets["seq"] = next_seq(conn)
    columns = ", ".join(f"{name} = ?" for name in sets)
    conn.execute(
        f"UPDATE main.agent_request SET {columns} WHERE id = ?",  # noqa: S608 — имена колонок из кода
        [*sets.values(), request_id],
    )


def _payload(req: Req) -> dict:
    try:
        value = json.loads(req.payload_json)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _channel_title(conn: sqlite3.Connection, channel_id) -> str | None:
    if not isinstance(channel_id, int) or isinstance(channel_id, bool):
        return None
    row = conn.execute(
        f"SELECT title FROM {db_of(channel_id)}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _disk_warnings(conn: sqlite3.Connection, req: Req, payload: dict) -> dict:
    """Ф50-7: открытая заявка на напарника — чего сейчас нет на диске (по образцу `role_file_warning`
    записи сессии): `subdir_warning` — папки нет, она вне проекта или в ней свои настройки движка
    («Завести» ответит 422 тем же текстом); `role_file_warning` — файл роли не читается (сессия заведётся
    без него). Готовые строки; ключа нет — замечаний нет."""
    if req.kind != "new_session" or req.status != "pending":
        return {}
    subdir, role_file = payload.get("subdir"), payload.get("role_file")
    if not subdir and not role_file:
        return {}
    from hub import roles as roles_mod  # импорт внутри: каталог нужен только здесь

    row = conn.execute("SELECT workdir FROM main.project WHERE id = ?", (req.project_id,)).fetchone()
    workdir = row[0] if row is not None else None
    found: dict = {}
    if subdir:
        try:
            roles_mod.subdir_check(workdir, subdir)
        except ValueError as exc:
            found["subdir_warning"] = str(exc)
    if role_file:
        loaded = roles_mod.read_file(workdir, role_file)
        if loaded.reason is not None:
            found["role_file_warning"] = f"файл роли {role_file}: {loaded.reason}"
    return found


def request_dict(conn: sqlite3.Connection, req: Req) -> dict:
    """Публичная форма (раздел 12). `who`, `reason`, `note` и поля `payload` — чужой ввод."""
    payload = _payload(req)
    if req.kind == "move_channel":
        shown = {
            "channel_id": payload.get("channel_id"),
            "channel_title": _channel_title(conn, payload.get("channel_id")),
            "from_project_id": payload.get("from_project_id"),
        }
    else:
        shown = {
            key: payload.get(key)
            for key in ("role", "task", "engine", "model", "effort", "mode", "subdir", "role_file")
        }
    return {
        "id": req.id,
        "seq": req.seq,
        "kind": req.kind,
        "project_id": req.project_id,
        "who": req.who,
        "reason": req.reason,
        "status": req.status,
        "auto": bool(req.auto),
        "note": req.note,
        "created_ts": req.created_ts,
        "decided_ts": req.decided_ts,
        "payload": shown,
        **_disk_warnings(conn, req, payload),
    }


def _audit(conn, req: Req, *, action: str, result: str, now_ms: int, audit: dict, detail=None) -> None:
    """Аудит без текстов: номер заявки, вид, проект, номера из нагрузки."""
    payload = _payload(req)
    write_audit(
        conn,
        now_ms=now_ms,
        action=action,
        result=result,
        channel_id=payload.get("channel_id") if req.kind == "move_channel" else None,
        detail={
            "agent_request_id": req.id,
            "kind": req.kind,
            "project_id": req.project_id,
            **({"hub_session_id": req.hub_session_id} if req.hub_session_id is not None else {}),
            # Ф50-7 (П3): папка и файл роли напарника — пути внутри проекта, не тексты.
            **(
                {k: payload[k] for k in ("subdir", "role_file") if payload.get(k)}
                if req.kind == "new_session"
                else {}
            ),
            **(detail or {}),
        },
        **audit,
    )


# --- создание -----------------------------------------------------------------------------------------------


def _one_line(value, limit: int, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentRequestError(422, f"{name}: нужна строка")
    if len(value) > limit:
        raise AgentRequestError(422, f"{name}: не длиннее {limit} символов")
    return " ".join(value.split()) or None


def _session_dict(session) -> dict | None:
    if session is None:
        return None
    if isinstance(session, dict):
        return {k: session.get(k) for k in ("pid", "proc_start", "session_id")}
    return {"pid": session.pid, "proc_start": session.proc_start, "session_id": session.session_id}


def _requester_sub(conn, token_id: int, project_id: int, session: dict | None):
    """Живая подписка ЭТОЙ сессии ЭТИМ токеном (на проект заявки — первой); её нет — `None`."""
    if not session:
        return None
    pid, proc_start, session_id = session.get("pid"), session.get("proc_start"), session.get("session_id")
    if not isinstance(pid, int) or not isinstance(proc_start, int) or not isinstance(session_id, str):
        return None
    if not subs_mod.is_alive(pid, proc_start, session.get("host")):
        return None
    row = conn.execute(
        "SELECT id FROM main.session_sub WHERE api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL "
        "ORDER BY (project_id = ?) DESC, id DESC LIMIT 1",
        (token_id, pid, proc_start, session_id, project_id),
    ).fetchone()
    return subs_mod.get_sub(conn, row[0]) if row is not None else None


def _hub_session_id(conn, session: dict | None) -> int | None:
    """Номер сессии хаба просителя по условной тройке (подпись и адрес исхода; права даёт только
    `autopilot.session_free`)."""
    if not session:
        return None
    hub_id = held_mod.held_id(session.get("pid"))
    if hub_id is None:
        return None
    if (session.get("pid"), session.get("proc_start"), session.get("session_id")) != held_mod.triple(hub_id):
        return None
    return hub_id if held_mod.get_open(conn, hub_id) is not None else None


def _move_fields(conn, principal, project_id: int, channel_id) -> tuple[str, dict]:
    if not isinstance(channel_id, int) or isinstance(channel_id, bool):
        raise AgentRequestError(422, "channel_id: нужен номер беседы")
    try:
        from_project = queries.channel_visible(conn, principal, channel_id)
    except queries.NotFound as exc:
        raise AgentRequestError(404, "беседа не найдена или не видна профилю") from exc
    row = conn.execute(
        f"SELECT archived, ext_id FROM {db_of(channel_id)}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if row is None:
        raise AgentRequestError(404, "беседа не найдена или не видна профилю")
    if row[0]:
        raise AgentRequestError(422, "беседа в архиве")
    if draft_channels.is_temp_ext_id(row[1]):
        raise AgentRequestError(422, "новая беседа-заготовка — её проект задаётся при создании")
    if from_project == project_id:
        raise AgentRequestError(422, "беседа уже в этом проекте")
    return f"channel:{channel_id}", {"channel_id": channel_id, "from_project_id": from_project}


def _session_fields(body: dict) -> tuple[str, dict]:
    role_raw = body.get("role")
    if not isinstance(role_raw, str):
        raise AgentRequestError(422, "role: назови роль напарника")
    try:
        role, role_key = subs_mod.role_form(role_raw)
    except subs_mod.SubError as exc:
        raise AgentRequestError(422, str(exc.detail)) from exc
    if role is None or role_key is None:
        raise AgentRequestError(422, "role: назови роль напарника")
    task = body.get("task")
    if not isinstance(task, str) or not task.strip():
        raise AgentRequestError(422, "task: что поручить напарнику")
    if len(task) > TASK_MAX:
        raise AgentRequestError(422, f"task: не длиннее {TASK_MAX} символов")
    engine = body.get("engine")
    if engine is not None and engine not in held_mod.ENGINES:
        raise AgentRequestError(422, f"engine: {' или '.join(held_mod.ENGINES)}")
    mode = body.get("mode")
    if mode is not None and mode not in held_mod.MODES:
        raise AgentRequestError(422, f"mode: {' или '.join(held_mod.MODES)}")
    # Ф50-7 (Р8): папка и файл роли напарника — оба пути от папки ПРОЕКТА. При подаче проверяется только
    # форма: папку и файл агент может создать к моменту «Завести».
    try:
        subdir = hub_sessions_mod.subdir_form(body.get("subdir"))
        role_file = hub_sessions_mod.role_file_form(body.get("role_file"))
    except hub_sessions_mod.SessionError as exc:
        raise AgentRequestError(422, str(exc.detail)) from exc
    payload = {
        "role": role,
        "task": task.strip(),
        "engine": engine,
        "model": _one_line(body.get("model"), FIELD_MAX, "model"),
        "effort": _one_line(body.get("effort"), FIELD_MAX, "effort"),
        "mode": mode,
        "subdir": subdir,
        "role_file": role_file,
    }
    # П1: ключ совпадения — роль + папка (без папки — прежний `role:<ключ>`, старые заявки не трогаются).
    return (f"role:{role_key}|{subdir}" if subdir else f"role:{role_key}"), payload


def create(
    conn: sqlite3.Connection,
    *,
    principal,
    kind: str,
    project_id: int,
    body: dict,
    reason: str | None,
    session,
    session_key: str | None,
    now_ms: int,
    audit: dict,
    waiting=None,
) -> Done:
    """`POST /agent-requests`. Проверки по порядку: запреты `check_attach` (403) -> вид (422) -> проект виден
    и существует (404) -> поля вида (422/404; напарника просит только сессия с живой подпиской на проект с
    ролью — 409) -> открытая заявка той же тройки (вид, проект, ключ) — она же -> потолки (429). Затем —
    сразу, если `autopilot.session_free` (и П13), иначе `pending`."""
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    token_name = row[0] if row and isinstance(row[0], str) else None
    if principal.is_ui:
        raise AgentRequestError(403, "заявку подаёт сессия, не интерфейс")
    try:
        check_attach(name=token_name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise AgentRequestError(
            403, "заявку подаёт только интерактивная сессия (не дежурная и не оркестратор)"
        ) from exc
    if kind not in KINDS:
        raise AgentRequestError(422, f"kind: {' или '.join(KINDS)}")
    if (
        not isinstance(project_id, int)
        or project_id <= 0
        or not queries.project_visible(conn, principal, project_id)
        or conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is None
    ):
        raise AgentRequestError(404, "project_id: нет такого проекта или он не виден профилю")
    reason = _one_line(reason, REASON_MAX, "reason")
    who_session = _session_dict(session)
    if kind == "move_channel":
        match_key, payload = _move_fields(conn, principal, project_id, body.get("channel_id"))
        sub = _requester_sub(conn, principal.token_id, project_id, who_session)
    else:
        try:
            sub = autosend_mod.role_sub(
                conn,
                principal=principal,
                token_name=token_name,
                session=session,
                project_id=project_id,
                now_ms=now_ms,
                waiting=waiting,
            )
        except autosend_mod.NoRoleSub as exc:
            raise AgentRequestError(409, f"напарника просит сессия проекта с ролью: {exc.reason}") from exc
        match_key, payload = _session_fields(body)

    existing = _select(
        conn,
        "r.kind = ? AND r.project_id = ? AND r.match_key = ? AND r.status = 'pending'",
        (kind, project_id, match_key),
    )
    if existing:
        return Done(request_dict(conn, existing[0]), False)
    open_count = conn.execute(
        "SELECT COUNT(*) FROM main.agent_request WHERE project_id = ? AND kind = ? AND status = 'pending'",
        (project_id, kind),
    ).fetchone()[0]
    if open_count >= OPEN_PER_KIND:
        raise AgentRequestError(
            429, f"у проекта уже {OPEN_PER_KIND} открытых заявок этого вида — дождись решения владельца"
        )
    day_count = conn.execute(
        "SELECT COUNT(*) FROM main.agent_request WHERE token_id = ? AND created_ts > ?",
        (principal.token_id, now_ms - DAY_MS),
    ).fetchone()[0]
    if day_count >= TOKEN_PER_DAY:
        raise AgentRequestError(429, f"не больше {TOKEN_PER_DAY} заявок за сутки")

    cur = conn.execute(
        "INSERT INTO main.agent_request (seq, kind, project_id, payload_json, match_key, reason, who, "
        "token_id, sub_id, hub_session_id, status, auto, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
        (
            next_seq(conn),
            kind,
            project_id,
            json.dumps(payload, ensure_ascii=False),
            match_key,
            reason,
            rule_requests_mod._who(conn, principal.token_id, project_id, who_session),
            principal.token_id,
            sub.id if sub is not None else None,
            _hub_session_id(conn, who_session),
            now_ms,
        ),
    )
    req = get(conn, cur.lastrowid)
    assert req is not None  # noqa: S101 — только что вставлена в этой транзакции
    _audit(conn, req, action="agent_request_create", result="pending", now_ms=now_ms, audit=audit)

    # Сразу — только сессии хаба, которой действие разрешено выключателем (П1, П12); напарник — ещё и П13.
    free = autopilot_mod.session_free(
        conn, autopilot_mod.ident(session, session_key), SWITCH[kind], project_id
    )
    if free is None or (kind == "new_session" and free.created_by == "agent"):
        return Done(request_dict(conn, req), True)
    if kind == "move_channel" and not principal.has_scope("organize"):
        # Ревью 41-5: раскладку меняет только токен со скоупом `organize` (7a A3) — без него заявка ждёт.
        return Done(request_dict(conn, req), True)
    made = None
    try:
        if kind == "move_channel":
            _apply_move(conn, req, now_ms=now_ms, audit=audit)
            detail = None
        else:
            # П13: движок, модель, усилие и режим — строго как у просителя; поля заявки не действуют.
            made = _apply_session(
                conn,
                req,
                role=payload["role"],
                engine=free.engine,
                model=free.model,
                effort=free.effort,
                mode=free.mode,
                # Ф50-7 (П3): папка и файл роли из заявки действуют и на пути «сразу».
                subdir=payload.get("subdir"),
                role_file=payload.get("role_file"),
                want_sub=None,
                autosend_ok=False,
                created_by="agent",
                now_ms=now_ms,
                audit=audit,
                waiting=waiting,
            )
            detail = {"new_session_id": made.id}
    except _Stale, AgentRequestError:
        # Не вышло сразу (линия занята, потолок сессий) — заявка остаётся ждать владельца.
        return Done(request_dict(conn, req), True)
    _update(
        conn,
        req.id,
        status="approved",
        auto=1,
        decided_ts=now_ms,
        result_json=json.dumps(detail) if detail else None,
    )
    req = get(conn, req.id)
    assert req is not None  # noqa: S101
    _audit(
        conn, req, action="agent_request_auto", result="approved", now_ms=now_ms, audit=audit, detail=detail
    )
    return Done(request_dict(conn, req), True, made)


# --- исполнение ---------------------------------------------------------------------------------------------


def _apply_move(conn, req: Req, *, now_ms: int, audit: dict) -> None:
    """Перенос тем же кодом, что перенос из интерфейса (`channels.update_channel`), в транзакции решения.
    Беседы нет, она в архиве или уже в проекте — `_Stale`. Ф52-2 П8: устарела и заявка, поданная из другого
    проекта беседы, чем сейчас (`payload.from_project_id`): беседу успели переложить — вторая одобренная
    заявка её не уводит. Заявки без этого поля (старые строки) — по-прежнему."""
    payload = _payload(req)
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, int) or isinstance(channel_id, bool):
        raise _Stale
    row = conn.execute(
        f"SELECT project_id, archived FROM {db_of(channel_id)}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if row is None or row[1] or row[0] == req.project_id:
        raise _Stale
    if "from_project_id" in payload and row[0] != payload["from_project_id"]:
        raise _Stale
    try:
        channels_mod.update_channel(conn, channel_id, now_ms=now_ms, project_id=req.project_id)
    except (channels_mod.NotFound, channels_mod.ChannelError) as exc:
        raise _Stale from exc
    write_audit(
        conn,
        now_ms=now_ms,
        action="channel_update",
        result="ok",
        channel_id=channel_id,
        detail={"project_id": req.project_id, "agent_request_id": req.id},
        **audit,
    )


def _apply_session(
    conn,
    req: Req,
    *,
    role: str,
    engine: str,
    model: str | None,
    effort: str | None,
    mode: str,
    want_sub,
    autosend_ok: bool,
    created_by: str,
    now_ms: int,
    audit: dict,
    waiting=None,
    subdir: str | None = None,
    role_file: str | None = None,
) -> held_mod.HubSession:
    """5.3: сессия хаба-напарник (`want_sub` по умолчанию — только сообщения агентов, `autosend_ok` по
    умолчанию 0) и первая задача — сообщением агента от линии просителя. Отказ создания — 409 с причиной;
    Ф50-7: негодная форма, папки нет или в ней свои настройки (422 у `create`) — 422 с тем же текстом."""
    try:
        rec = hub_sessions_mod.create(
            conn,
            project_id=req.project_id,
            engine=engine,
            role=role,
            model=model,
            effort=effort,
            created_by=created_by,
            now_ms=now_ms,
            audit=audit,
            waiting=waiting,
            want_sub=want_sub if want_sub is not None else {"mode": "direct"},
            mode=mode,
            subdir=subdir,
            role_file=role_file,
        )
    except hub_sessions_mod.SessionError as exc:
        raise AgentRequestError(422 if exc.status == 422 else 409, str(exc.detail)) from exc
    if not autosend_ok:
        conn.execute("UPDATE main.hub_session SET autosend_ok = 0 WHERE id = ?", (rec.id,))
        rec = held_mod.get(conn, rec.id)
        assert rec is not None  # noqa: S101
    sub = subs_mod.get_sub(conn, req.sub_id) if req.sub_id is not None else None
    task = _payload(req).get("task")
    if sub is not None and isinstance(task, str) and task and rec.role_key and rec.role_no:
        peer_msgs_mod.task_to_line(
            conn,
            sub=sub,
            project_id=rec.project_id,
            role=rec.role,
            role_key=rec.role_key,
            role_no=rec.role_no,
            text=task,
            now_ms=now_ms,
        )
    return rec


def _outcome_text(req: Req, note: str | None) -> str:
    """П9: только номера и постоянные слова плюс слова владельца — ни роли, ни задачи, ни названия беседы."""
    if req.kind == "move_channel":
        what = f"перенос беседы #{_payload(req).get('channel_id')} в проект #{req.project_id}"
    else:
        what = "напарник"
    text = f"Заявка №{req.id} ({what}) — {'выполнена' if req.status == 'approved' else 'отклонена'}"
    return f"{text}. {note}" if note else text


def _notify(conn, req: Req, *, now_ms: int, waiting=None) -> int | None:
    """Исход решённой владельцем заявки — сообщением от владельца линии роли просителя; нет линии — только статус.
    Возвращает живую подписку адресата (её ожидание будит ручка)."""
    sub = subs_mod.get_sub(conn, req.sub_id) if req.sub_id is not None else None
    sent = peer_msgs_mod.note_from_user(
        conn, sub=sub, text=_outcome_text(req, req.note), now_ms=now_ms, waiting=waiting
    )
    return sent.live_sub_id if sent is not None else None


class Decided(NamedTuple):
    request: dict
    session: held_mod.HubSession | None = None
    wake_sub_id: int | None = None


def decide(
    conn: sqlite3.Connection,
    request_id: int,
    action: str,
    *,
    fields: dict | None = None,
    note: str | None = None,
    now_ms: int,
    audit: dict,
    waiting=None,
) -> Decided:
    """«Перенести» / «Завести» / «Нет» — только из `pending` (иначе 409). Перенос устарел (беседа в архиве
    или уже в проекте) — заявка закрывается `declined` с пометкой «устарела». Напарник: поля тела меняют
    поля заявки; отказ создания (потолок сессий, линия занята) — 409, заявка остаётся `pending`."""
    req = get(conn, request_id)
    if req is None:
        raise AgentRequestError(404, "not found")
    if action not in ("approve", "decline"):
        raise AgentRequestError(422, "action: approve или decline")
    if req.status != "pending":
        raise AgentRequestError(409, f"заявка уже решена ({req.status})")
    note = _one_line(note, NOTE_MAX, "note")
    made = None
    detail = None
    if action == "approve" and req.kind == "move_channel":
        try:
            _apply_move(conn, req, now_ms=now_ms, audit=audit)
        except _Stale:
            _update(conn, request_id, status="declined", decided_ts=now_ms, note=STALE_NOTE)
        else:
            _update(conn, request_id, status="approved", decided_ts=now_ms)
    elif action == "approve":
        payload = _payload(req)
        fields = fields or {}
        role = fields.get("role") or payload.get("role")
        engine = fields.get("engine") or payload.get("engine")
        mode = fields.get("mode") or payload.get("mode")
        requester = held_mod.get(conn, req.hub_session_id) if req.hub_session_id is not None else None
        if engine is None:
            engine = requester.engine if requester is not None else held_mod.ENGINES[0]
        if mode is None:
            mode = requester.mode if requester is not None else "manual"
        made = _apply_session(
            conn,
            req,
            role=role,
            engine=engine,
            model=fields["model"] if "model" in fields else payload.get("model"),
            effort=fields["effort"] if "effort" in fields else payload.get("effort"),
            mode=mode,
            # Ф50-7 (П2): «ключ есть» — пустая строка владельца значит корень / без файла и к значению агента
            # не откатывается; ключа нет — как в заявке.
            subdir=fields["subdir"] if "subdir" in fields else payload.get("subdir"),
            role_file=fields["role_file"] if "role_file" in fields else payload.get("role_file"),
            want_sub=fields.get("want_sub"),
            autosend_ok=bool(fields.get("autosend_ok", False)),
            created_by="request",
            now_ms=now_ms,
            audit=audit,
            waiting=waiting,
        )
        detail = {"new_session_id": made.id}
        _update(conn, request_id, status="approved", decided_ts=now_ms, result_json=json.dumps(detail))
    else:
        _update(conn, request_id, status="declined", decided_ts=now_ms, note=note)
    req = get(conn, request_id)
    assert req is not None  # noqa: S101
    _audit(
        conn, req, action="agent_request_decide", result=req.status, now_ms=now_ms, audit=audit, detail=detail
    )
    wake = _notify(conn, req, now_ms=now_ms, waiting=waiting)
    return Decided(request_dict(conn, req), made, wake)


# --- выдача ------------------------------------------------------------------------------------------------


def list_requests(
    conn: sqlite3.Connection, principal, *, now_ms: int, status: str | None = None, limit: int = LIST_MAX
) -> list[dict]:
    """`GET /agent-requests`: интерфейсу — все, токену — свои; по умолчанию — открытые и решённые за 7 дней,
    свежие первыми."""
    where, params = ["1 = 1"], []
    if not principal.is_ui:
        where.append("r.token_id = ?")
        params.append(principal.token_id)
    if status is not None:
        where.append("r.status = ?")
        params.append(status)
    else:
        where.append("(r.status = 'pending' OR r.decided_ts > ?)")
        params.append(now_ms - LIST_RECENT_MS)
    limit = max(1, min(int(limit), LIST_MAX))
    rows = _select(conn, " AND ".join(where) + " ORDER BY r.id DESC LIMIT ?", [*params, limit])
    return [request_dict(conn, r) for r in rows]


def changes(conn: sqlite3.Connection, principal, *, since: int, watermark: int, limit: int) -> list[dict]:
    """Заявки с `since < seq <= watermark` по возрастанию `seq` (`/sync`, SSE) — только UI-сессии: в них
    задача напарнику и причина (чужой ввод)."""
    if not principal.is_ui:
        return []
    rows = _select(conn, "r.seq > ? AND r.seq <= ? ORDER BY r.seq LIMIT ?", [since, watermark, limit])
    return [request_dict(conn, r) for r in rows]
