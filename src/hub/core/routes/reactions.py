"""Ручки реакций из хаба в источник (Ф10-3, docs/architecture.md и поправки П1, П11).

Публичная: `POST /api/v1/messages/{id}/reactions` `{"emoji", "on"}` — только UI-сессия (`ui_only`: bearer ->
403 с аудитом `ui_only_denied`), CSRF — middleware. Внутренние (сокет коннектора, по образцу
`routes/read_ack.py`): `GET /reactions/wait` (long-poll), `POST /reactions/lease`,
`POST /reactions/{message_id}/report`. Маршруты не пишут SQL: чтение и запись — `core/reactions.py`.
"""

import asyncio
import contextlib
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from .. import connector_auth, queries
from .. import handled as handled_mod
from .. import reactions as reactions_mod
from .. import read_ack as read_ack_mod
from .. import subs as subs_mod
from ..audit import write_audit
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, check_attach, principal_dependency
from .subs import SessionIn
from .write import touch_activity

public_router = APIRouter(tags=["reactions"])
internal_router = APIRouter(tags=["internal-reactions"])

_connector = Depends(connector_auth.connector_dependency())
_authed = Depends(principal_dependency())

_MAX_WAIT_S = 30.0
# Нижняя граница одного ожидания до ближайшего срока (как `routes/read_ack.py`).
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


# --- публичная ручка (A3 + П1; Ф15-1 A3 + П7) ------------------------------------------------------------


class ReactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emoji: StrictStr
    on: StrictBool
    # Ф15-1 П7: bearer сессии — pid + `starttime` + разговор (как `hub_subscribe`); UI-сессия — без него.
    session: SessionIn | None = None
    # Ф20-2 П6: беседа сообщения — по желанию. Дана — сообщение обязано лежать в ней (иначе 422).
    channel_id: int | None = Field(default=None, ge=1)


class _Denied(Exception):
    """Ф15-1 A3: bearer не может ставить реакцию — 403 с аудитом `reaction_denied`."""


class _Invalid(Exception):
    """Ф20-2 П6/П7: неверное тело — 422 с причиной."""


def _token_name(conn, principal: Principal) -> str | None:
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    return row[0] if row and isinstance(row[0], str) else None


def _session_error(principal: Principal, name: str | None) -> str | None:
    """Ф15-1 решение 2: bearer со скоупом `draft`, `interactive`, не оркестратор — те же запреты, что у
    `attach` (`security.check_attach`). Причина отказа или `None`."""
    if not principal.has_scope("draft"):
        return "реакции сессии нужен скоуп draft"
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError:
        return "реакции — только из интерактивной сессии (не дежурная и не оркестратор)"
    return None


async def _deny(request: Request, principal: Principal, message_id: int, reason: str) -> HTTPException:
    """Аудит `reaction_denied` отдельной транзакцией (как `ui_only_denied`) и 403 с причиной."""
    now_ms = _now_ms(request)
    await request.app.state.db.write(
        lambda conn: write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=_client_name(request),
            request_id=uuid.uuid4().hex,
            action="reaction_denied",
            result="denied",
            detail={"message_id": message_id, "reason": reason[:200]},
        )
    )
    return HTTPException(403, reason)


@public_router.post("/messages/{message_id}/reactions")
async def set_reaction(
    message_id: int, body: ReactionIn, request: Request, response: Response, principal: Principal = _authed
) -> dict:
    """П1: одна операция `{emoji, on}`. Ответ `{"status": "queued"|"off"|"already", "not_before_ts"?}`; 404 —
    не видно или скрыто у себя; 409 — источник без реакций (`unsupported`), реакцию некуда ставить (нет
    `ext_id`, служебное, удалённое) или отказ правилами хаба (Ф15-1: `not own`, `owner reaction`, `owner_side
    pending`, `not google`, `no reply`, `sent, cannot undo`, `too many`, `sending`, `receive only`); 422 — не
    эмодзи. Аудит: `reaction_set` (id сообщения, `on`, `origin`, `not_before_ts`) или `reaction_off`
    (выключатель аккаунта); Ф19-1 D — и `emoji` (после нормализации).

    Ф15-1 A3 + П7: кроме UI-сессии — bearer сессии Claude Code (решение 2) с `session`, подписанной на проект
    беседы или на саму беседу; иначе 403 с аудитом `reaction_denied`. Реакции сессий в проекте выключены —
    403 `session reactions off`; больше 60 за час на сессию — 429."""
    touch_activity(request, principal)  # Ф20-3 A2: реакция владельца из UI — «активен в хабе»
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    client_name = _client_name(request)
    session = body.session
    origin = "ui" if principal.is_ui else "session"
    if principal.is_ui and session is not None:
        raise HTTPException(422, "session — только у bearer-токена сессии")

    def _fn(conn):
        if origin == "session":
            error = _session_error(principal, _token_name(conn, principal))
            if error is not None:
                raise _Denied(error)
        channel_id = queries.message_visible(conn, principal, message_id)
        if body.channel_id is not None and body.channel_id != channel_id:
            raise _Invalid("message not in channel")
        if origin == "session":
            if session is None or not subs_mod.is_alive(session.pid, session.proc_start, session.host):
                # Ф16-4 (П1): сессии OpenCode — нейтральный текст, без «Claude Code».
                if session is not None and subs_mod.many_convs(session.session_id):
                    raise _Denied("реакции — только из живой сессии (hub_react)")
                raise _Denied("реакции — только из живой сессии Claude Code (hub_react)")
            located = reactions_mod.message_project(conn, message_id)
            project_id = located[1] if located is not None else None
            if not reactions_mod.session_subscribed(
                conn,
                token_id=principal.token_id,
                pid=session.pid,
                proc_start=session.proc_start,
                session_id=session.session_id,
                channel_id=channel_id,
                project_id=project_id,
            ):
                raise _Denied("реакции — только из сессии, подписанной на этот проект")
            count = reactions_mod.session_rate_count(
                conn,
                token_id=principal.token_id,
                pid=session.pid,
                proc_start=session.proc_start,
                now_ms=now_ms,
                session_id=session.session_id if subs_mod.many_convs(session.session_id) else None,
            )
            if count >= reactions_mod.SESSION_RATE_PER_HOUR:
                raise _RateLimited()
        result = reactions_mod.enqueue(
            conn,
            message_id,
            body.emoji,
            body.on,
            now_ms,
            origin=origin,
            token_id=None if principal.is_ui else principal.token_id,
        )
        # Ф19-1 D + П11: эмодзи — как его записала очередь (`normalize_emoji`), а не сырое значение запроса.
        detail: dict = {"message_id": message_id, "on": body.on, "origin": origin, "emoji": result.emoji}
        if body.on and result.status == "queued":
            detail.update(
                _mark_handled(
                    conn, request, principal, session, channel_id, message_id, result, now_ms, request_id
                )
            )
        if result.not_before_ms is not None:
            detail["not_before_ts"] = result.not_before_ms
        if session is not None:
            detail["pid"] = session.pid
            detail["proc_start"] = session.proc_start
            if subs_mod.many_convs(session.session_id):
                detail["session_id"] = session.session_id  # Ф16-4 (П1): ключ лимита сессии OpenCode
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="reaction_off" if result.status == "off" else "reaction_set",
            result=result.status,
            channel_id=result.channel_id,
            detail=detail,
        )
        return result

    try:
        result = await db.write(_fn)
    except _Denied as exc:
        raise await _deny(request, principal, message_id, str(exc)) from exc
    except _RateLimited as exc:
        raise HTTPException(429, "too many reactions") from exc
    except _Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except (queries.NotFound, reactions_mod.NotFound) as exc:
        raise HTTPException(404, "not found") from exc
    except reactions_mod.Unsupported as exc:
        raise HTTPException(409, "unsupported") from exc
    except reactions_mod.NotReactable as exc:
        raise HTTPException(409, "not reactable") from exc
    except reactions_mod.InvalidEmoji as exc:
        raise HTTPException(422, "invalid emoji") from exc
    except reactions_mod.Refused as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    out: dict = {"status": result.status}
    if origin == "session" and result.not_before_ms is not None:
        out["not_before_ts"] = result.not_before_ms  # сессии — когда уйдёт (UI знает свою задержку)
    return out


def _mark_handled(
    conn,
    request: Request,
    principal: Principal,
    session,
    channel_id: int,
    message_id: int,
    result,
    now_ms: int,
    request_id: str,
) -> dict:
    """Ф66-4 B/D: реакция на чужое сообщение = «разобрано до него». Владелец из хаба — прочтение, карточки и
    отметка в источник; сессия — `handled.mark` (условия `hub_handled`, пометка «реакция <эмодзи>»), любой
    отказ глотается и реакцию не роняет."""
    if principal.is_ui:
        info = handled_mod.reacted_up_to(conn, channel_id=channel_id, message_id=message_id, now_ms=now_ms)
        if info is None:
            return {}
        if info["reaction_read"] == "changed" and not info["reaction_thread"]:
            info["reaction_read_ack"] = read_ack_mod.enqueue(conn, channel_id, message_id, now_ms)
        return info
    if session is None:
        return {}
    conn.execute("SAVEPOINT react_handled")
    try:
        located = reactions_mod.message_project(conn, message_id)
        done = handled_mod.mark(
            conn,
            principal=principal,
            token_name=_token_name(conn, principal),
            session=session,
            channel_id=channel_id,
            project_id=located[1] if located is not None else None,
            up_to_message_id=message_id,
            note=f"реакция {result.emoji}",
            now_ms=now_ms,
            waiting=getattr(getattr(request.app.state, "sub_waiters", None), "waiting", None),
            client_name=_client_name(request),
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 — отметка не обязательна: реакция ставится как обычно
        conn.execute("ROLLBACK TO react_handled")
        conn.execute("RELEASE react_handled")
        return {}
    conn.execute("RELEASE react_handled")
    return {"handled_id": done["id"]}


class _RateLimited(Exception):
    """Решение 8: больше 60 реакций за час на сессию — 429."""


# --- внутренние ручки (сокет коннектора) -------------------------------------------------------------------


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        ready, due = reactions_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/reactions/wait")
async def reactions_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/read-ack/wait`: «готово» = `lease` выдал бы хоть что-то своему аккаунту; ожидание
    ограничено ещё и временем до ближайшего срока строки (задержка 1.5 с не ждёт конца long-poll)."""
    notifier = request.app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)
    timeout_s = min(timeout_s, _MAX_WAIT_S)
    deadline = loop.time() + timeout_s

    gen = notifier.gen
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout_s):
            while True:
                ready, due_ms, now_ms = _state(request, principal)
                if ready:
                    return {"ready": True}
                wait_s = deadline - loop.time()
                if wait_s <= 0:
                    break
                if due_ms is not None:
                    wait_s = min(wait_s, max((due_ms - now_ms) / 1000, _MIN_DUE_WAIT_S))
                gen = await notifier.wait(gen, wait_s)
                if notifier.closed:
                    break  # Ф31-1 П5: ядро останавливается — пусто, ничего не выдавая
    return {"ready": False}


class ReactionsLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/reactions/lease")
async def reactions_lease(
    body: ReactionsLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """П6: задания `{message_id, channel_id, gen, lease_gen, channel: {ext_id, kind, parent_ext_id},
    message_ext_id, ops: [[emoji, on], …], current_mine?}` (`current_mine` — только Telegram), по умолчанию
    по одному."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: reactions_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class ReactionReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_gen: int
    outcome: Literal["done", "retry", "drop"]
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)
    # Ф15-1 П1: причина итога (`done` + `owner_reacted` — операции сессии не применены, пометок нет).
    reason: str | None = Field(default=None, max_length=64)


@internal_router.post("/reactions/{message_id}/report")
async def reactions_report(
    message_id: int, body: ReactionReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """Чужое сообщение/нет строки — 404, устаревший `lease_gen` — 409."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return reactions_mod.report(
            conn,
            account_id=principal.account_id,
            message_id=message_id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            retry_after_ms=body.retry_after_ms,
            reason=body.reason,
        )

    try:
        result = await db.write(_fn)
    except reactions_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except reactions_mod.StaleLease as exc:
        raise HTTPException(409, "stale lease") from exc
    return {"ok": True, "result": result}
