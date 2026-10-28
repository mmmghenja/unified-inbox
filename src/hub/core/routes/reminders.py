"""Ручки напоминаний по беседе (Ф34-2, docs/architecture.md и поправки П2, П3, П5, П6, П10).
Маршруты не пишут SQL: `core/reminders.py` внутри `Db.write()`/`db.reader()`.

- `POST /reminders` — поставить: UI-сессия владельца или bearer со скоупом `draft` ПОДПИСАННОЙ сессии (в теле
  `session`, как у реакций; дежурной и оркестратору — 403 тем же правилом, что подписка).
- `PATCH /reminders/{id}` — перенести/поправить (`at`, `note`, `cancel_if_reply`) или закрыть (`close`):
  UI — любые, сессия — только напоминания своей роли (П3).
- `GET /reminders` — открытые (и закрытые за 7 дней — `all=1`); со своей сессией (`pid`, `proc_start`,
  `session_id`) — только напоминания роли её подписки.
- `POST /reminders/delivered` — сессия прочла заметки (`hub_reminders`): отметка `delivered_ts`.
- `GET /agent/reminders/due`, `POST /agent/reminders/notified` — скоуп `triage`: уведомление владельцу шлёт
  hub-agent и отмечает, что оно ушло.

П6: перед выдачей (список, главная, уведомления) сработавшие с ответом, принятым с опозданием, переводятся в
`answered` — отдельной записью и только когда такие нашлись (`settle_stale`).

Ф89-1 (docs/architecture.md — контракт): `every` — период, `channel_id: null` — без
беседы (у владельца тогда `project_id` обязателен), `cancel_if_reply` по умолчанию — по виду (П4); в `PATCH` —
`every` (`null` — «сделать разовым», отличается от отсутствия поля) и `stop`; `GET /reminders?counts=1` —
`new_since_done` у каждой строки.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .. import owner_queue as zq
from .. import reminders as reminders_mod
from ..security import Principal, principal_dependency
from .subs import (
    _PID_MAX,
    _PROC_START_MAX,
    HOST_QUERY,
    SessionIn,
    _subscriber_error,
    query_session,
    triple_query,
)
from .write import _client_name, _new_request_id, _now_ms, touch_activity

public_router = APIRouter(tags=["reminders"])

_any = Depends(principal_dependency())
_read = Depends(principal_dependency("read"))
_triage = Depends(principal_dependency("triage"))

_AT_MAX = 64
_NOTE_RAW_MAX = 2000  # до нормализации; предел заметки (200) проверяет `reminders.note_form`


def _http(exc: reminders_mod.ReminderError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _writer_error(conn, principal: Principal) -> str | None:
    """Кто ставит и правит: UI-сессия владельца или bearer, которому положена подписка (скоуп `draft`,
    `interactive`, не дежурная и не оркестратор — `_subscriber_error`)."""
    if principal.is_ui:
        return None
    return _subscriber_error(conn, principal)


async def settle_stale(request: Request, principal: Principal) -> None:
    """П6: читатель ищет сработавшие с опоздавшим ответом; нашлись — одна запись переводит их в
    `answered` (запись будит все long-poll — без нужды не пишем)."""
    db = request.app.state.db
    with db.reader() as conn:
        stale = reminders_mod.stale_for(conn, principal)
    if stale:
        now_ms = _now_ms(request)
        await db.write(lambda conn: reminders_mod.settle(conn, now_ms))


class ReminderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ф89-1: нет беседы — `null` (у владельца тогда нужен `project_id`; у сессии проект — из подписки).
    channel_id: StrictInt | None = Field(default=None, ge=1, le=2**62)
    project_id: StrictInt | None = Field(default=None, ge=1, le=2**53)
    at: StrictStr = Field(min_length=1, max_length=_AT_MAX)
    note: StrictStr | None = Field(default=None, max_length=_NOTE_RAW_MAX)
    # Ф89-1 (П4): не задано — `true` у разового по беседе, `false` у повторяемого и без беседы.
    cancel_if_reply: StrictBool | None = None
    every: StrictStr | None = Field(default=None, max_length=reminders_mod.EVERY_MAX)
    # П2: bearer сессии — pid + `starttime` + разговор (как `hub_react`); UI-сессия — без него.
    session: SessionIn | None = None


class ReminderPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: StrictStr | None = Field(default=None, min_length=1, max_length=_AT_MAX)
    note: StrictStr | None = Field(default=None, max_length=_NOTE_RAW_MAX)
    cancel_if_reply: StrictBool | None = None
    close: StrictBool = False
    # Ф89-1: `every: null` — «сделать разовым» (отличается от отсутствия поля, П6); `stop` — снять регламент.
    every: StrictStr | None = Field(default=None, max_length=reminders_mod.EVERY_MAX)
    stop: StrictBool = False
    session: SessionIn | None = None


class DeliveredIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[StrictInt] = Field(min_length=1, max_length=reminders_mod.MARK_MAX)
    session: SessionIn


class NotifiedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[StrictInt] = Field(min_length=1, max_length=reminders_mod.MARK_MAX)


@public_router.post("/reminders", status_code=201)
async def post_reminder(
    body: ReminderIn, request: Request, response: Response, principal: Principal = _any
) -> dict:
    """Поставить напоминание по беседе или без неё. 201 — строка напоминания. 403 — токену не положено; 404 —
    беседа (проект) не видна; 409 — сессия без подписки на проект («сначала hub_subscribe»), разовое по беседе
    в архиве; 422 — время вне «через минуту … 90 дней», заметка длиннее 200, кривой `every`, `cancel_if_reply:
    true` у повторяемого или без беседы, без беседы у владельца нет `project_id`; 429 — 50 ждущих на проект, 5 на
    беседу."""
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        error = _writer_error(conn, principal)
        if error is not None:
            raise reminders_mod.ReminderError(403, error)
        rec = reminders_mod.create(
            conn,
            principal=principal,
            channel_id=body.channel_id,
            at=body.at,
            note=body.note,
            cancel_if_reply=body.cancel_if_reply,
            session=session,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            every=body.every,
            project_id=body.project_id,
        )
        return reminders_mod.reminder_dict(conn, rec, principal)

    try:
        return await db.write(_fn)
    except reminders_mod.ReminderError as exc:
        raise _http(exc) from exc


@public_router.patch("/reminders/{reminder_id}")
async def patch_reminder(
    reminder_id: int, body: ReminderPatchIn, request: Request, response: Response, principal: Principal = _any
) -> dict:
    """Перенести (`at`: сработавшее снова ждёт срока), поправить (`note`, `cancel_if_reply`) или закрыть
    (`close: true`: ждущее — `cancelled`, сработавшее — `done`; уже закрытое — как есть). 403 — напоминание
    другой роли; 404 — нет или не видно; 409 — уже закрыто, беседа в архиве, сессия без подписки; 422.

    Ф89-1: у повторяемого `close` — «сделано» (строка снова `pending` к ближайшему сроку), `stop: true` —
    снять регламент (`done`, `end_reason: "stopped"`); у разового `stop` — как `close`."""
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        error = _writer_error(conn, principal)
        if error is not None:
            raise reminders_mod.ReminderError(403, error)
        if principal.is_ui and (body.close or body.stop or body.at is not None):
            # Ф58-3: закрыть или перенести сработавшее — ответ по карточке; поручение агента закрыть.
            zq.take_from_agent(conn, f"reminder:{reminder_id}", now_ms=now_ms)  # Ф65-1 Ж1
        rec = reminders_mod.update(
            conn,
            reminder_id,
            principal=principal,
            session=session,
            at=body.at,
            note=body.note,
            cancel_if_reply=body.cancel_if_reply,
            close=body.close,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            every=body.every if "every" in body.model_fields_set else reminders_mod.UNSET,
            stop=body.stop,
        )
        return reminders_mod.reminder_dict(conn, rec, principal)

    try:
        return await db.write(_fn)
    except reminders_mod.ReminderError as exc:
        raise _http(exc) from exc


@public_router.get("/reminders")
async def get_reminders(
    request: Request,
    project_id: int | None = Query(None, ge=1, le=2**53),
    channel_id: int | None = Query(None, ge=1, le=2**62),
    all: int = Query(0, ge=0, le=1),  # noqa: A002 — имя параметра API
    limit: int = Query(reminders_mod.LIST_MAX, ge=1, le=reminders_mod.LIST_MAX),
    pid: int | None = Query(None, ge=-_PID_MAX, le=_PID_MAX),  # Ф41-2а П4: и сессия хаба
    proc_start: int | None = Query(None, ge=0, le=_PROC_START_MAX),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    host: str | None = HOST_QUERY,
    counts: int = Query(0, ge=0, le=1),
    principal: Principal = _read,
) -> dict:
    """Открытые напоминания: сработавшие, затем ждущие по сроку; `all=1` — ещё и закрытые за 7 дней. Видимость
    — по проекту напоминания; название беседы и заметка — только если беседа видна принципалу (П10). Своя
    сессия (`pid`, `proc_start`, `session_id` — все три) — только напоминания роли её подписки (409 — подписки
    нет). Сам список ничего не отмечает: прочитанное сессия отмечает `POST /reminders/delivered`. Ф89-1:
    `counts=1` — у строк по беседе `new_since_done` (сообщений с прошлого «сделано»)."""
    triple_query(pid, proc_start, session_id)
    if pid is not None and principal.is_ui:
        raise HTTPException(422, "session — только у bearer-токена сессии")
    await settle_stale(request, principal)
    now_ms = _now_ms(request)
    try:
        with request.app.state.db.reader() as conn:
            sub = None
            if pid is not None:
                session = query_session(pid, proc_start, session_id, host)
                sub = reminders_mod.session_sub(conn, principal, session)
            items = reminders_mod.list_reminders(
                conn,
                principal,
                now_ms=now_ms,
                project_id=project_id,
                channel_id=channel_id,
                include_closed=bool(all),
                sub=sub,
                limit=limit,
                counts=bool(counts),
            )
    except reminders_mod.ReminderError as exc:
        raise _http(exc) from exc
    result: dict = {"items": items}
    if sub is not None:
        # Ф35-1: номер линии — для имени агента в `hub_reminders`.
        result["sub"] = {"id": sub.id, "project_id": sub.project_id, "role": sub.role, "role_no": sub.role_no}
    return result


@public_router.post("/reminders/delivered")
async def post_reminders_delivered(body: DeliveredIn, request: Request, principal: Principal = _any) -> dict:
    """Решение 5: сессия прочла сработавшие напоминания своей роли — `delivered_ts`. Чужие, закрытые и уже
    отмеченные — мимо; `marked` — сколько отмечено."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    session = body.session.model_dump()

    def _fn(conn):
        error = _writer_error(conn, principal)
        if error is not None:
            raise reminders_mod.ReminderError(403, error)
        if principal.is_ui:
            raise reminders_mod.ReminderError(403, "отметка — только bearer-токеном сессии")
        sub = reminders_mod.session_sub(conn, principal, session)
        return reminders_mod.mark_delivered(conn, body.ids, sub, now_ms=now_ms)

    try:
        return {"marked": await db.write(_fn)}
    except reminders_mod.ReminderError as exc:
        raise _http(exc) from exc


@public_router.get("/agent/reminders/due")
async def get_agent_reminders_due(request: Request, principal: Principal = _triage) -> dict:
    """Решение 6: сработавшие напоминания, которым пора на экран владельцу и уведомление ещё не уходило: свои и
    безрольные — сразу, поставленные сессией с ролью — через 20 минут после срабатывания, если не закрыты."""
    await settle_stale(request, principal)
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        return {"items": reminders_mod.due_notify(conn, principal, now_ms=now_ms)}


@public_router.post("/agent/reminders/notified")
async def post_agent_reminders_notified(
    body: NotifiedIn, request: Request, principal: Principal = _triage
) -> dict:
    """Уведомление ушло — `notified_ts` (один раз); в тихие часы hub-agent эту ручку не зовёт (П5)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    marked = await db.write(
        lambda conn: reminders_mod.mark_notified(conn, body.ids, principal, now_ms=now_ms)
    )
    return {"marked": marked}
