"""Ручки сообщений агентов хаба (Ф35-2, docs/architecture.md–9, 15, 16; П3–П5, П7, П12;
Н4, Н6–Н8). Маршруты не пишут SQL: `core/peer_msgs.py` внутри `Db.write()`/`db.reader()`.

- `POST /peer-msgs` — написать: bearer ПОДПИСАННОЙ сессии (в теле `session`; дежурной, оркестратору и
  рабочим сессиям — 403 тем же правилом, что подписка) или UI-сессия владельца (без `session` — `from_user`).
- `GET /peer-msgs` — UI: вся переписка в скоупе проектов; bearer со своей сессией (`pid`, `proc_start`,
  `session_id`) — входящие (и с `all=1` отправленные) своей линии или подписки; bearer без сессии — 403.
- `POST /peer-msgs/read` — прочитано: сессия — свои входящие, UI — только ответы владельцу (`to_user`).
- `POST /peer-msgs/ask` — вопрос владельцу (`hub_ask_owner`, Ф56-1б): bearer подписанной сессии с ролью; UI — 422.
- `GET /agent/peer-msgs/alerts`, `POST /agent/peer-msgs/notified` — скоуп `triage`: уведомление владельцу о
  ответах агентов владельцу шлёт hub-agent и отмечает, что оно ушло.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .. import held as held_mod
from .. import machines as machines_mod
from .. import peer_msgs as peer_msgs_mod
from .. import subs as subs_mod
from .. import workday as workday_mod
from ..security import Principal, principal_dependency
from .subs import (
    _PID_MAX,
    _PROC_START_MAX,
    HOST_QUERY,
    SUB_WAIT_PEERS_PROTOCOL,
    SessionIn,
    _subscriber_error,
    _waiters,
    query_session,
    triple_query,
)
from .write import _client_name, _new_request_id, _now_ms, authed_or_agent, touch_activity

public_router = APIRouter(tags=["peer-msgs"])

_any = Depends(principal_dependency())
_read = Depends(principal_dependency("read"))
_triage = Depends(principal_dependency("triage"))

_TEXT_RAW_MAX = 20_000  # предел текста (4000) проверяет `peer_msgs.send` — с понятным отказом
_ID_MAX = 2**62


def _http(exc: peer_msgs_mod.PeerError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _writer_error(conn, principal: Principal) -> str | None:
    """Кто пишет и отмечает: UI-сессия владельца или bearer, которому положена подписка (`_subscriber_error`).
    Ф88-1 (П4): агент владельца (bearer ровно `human`) пишет как кука; бота отсекает `authed_or_agent`."""
    if principal.is_ui or peer_msgs_mod.is_owner_agent(principal):
        return None
    return _subscriber_error(conn, principal)


class ToIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 0 — агенты вне проектов (решение 14).
    project_id: StrictInt = Field(ge=0, le=2**53)
    role: StrictStr | None = Field(default=None, max_length=128)
    number: StrictInt | None = Field(default=None, ge=1, le=subs_mod.ROLE_NO_MAX)
    # Только UI: реплика владельца — именно этой живой подписке (диалог с безрольным и с агентом вне проектов
    # привязан к подписке); по адресу уже другая сессия — 409. Bearer — 422.
    sub_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)


class PeerMsgIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(min_length=1, max_length=_TEXT_RAW_MAX)
    to: ToIn | None = None
    reply_to: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    # Bearer сессии — pid + `starttime` + разговор (как у напоминаний); UI-сессия — без него.
    session: SessionIn | None = None
    # Ф48-4 п.2: срочное сообщение агента — мимо пачки адресата и забирает накопленное. Только bearer сессии.
    urgent: StrictBool = False


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(min_length=1, max_length=_TEXT_RAW_MAX)
    # Роль, о которой вопрос (спрашивает головная за подчинённую); точный предел (128) проверяет ядро.
    about_role: StrictStr | None = Field(default=None, max_length=512)
    session: SessionIn | None = None
    # Ф93-1 (A.4): вопрос о тратах — ответит только тот, кого владелец выбрал в строке «Деньги» (по умолчанию он).
    money: StrictBool = False


class ReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[StrictInt] = Field(min_length=1, max_length=peer_msgs_mod.MARK_MAX)
    session: SessionIn | None = None


class NotifiedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[StrictInt] = Field(min_length=1, max_length=peer_msgs_mod.MARK_MAX)


@public_router.post("/peer-msgs", status_code=201)
async def post_peer_msg(
    body: PeerMsgIn, request: Request, response: Response, principal: Principal = authed_or_agent
) -> dict:
    """Написать агенту (`to`) или ответить (`reply_to`) — ровно одно. 201 — `{"msg", "recipient": {"live",
    "waiting"}}` (`waiting` — у адресата идёт ожидание протокола ≥ 4, П12; Ф48-4: в `recipient` ещё
    `batch_ts` — срок пачки, до которого адресат сигнала не получит; ключа нет — сигнал сразу).

    403 — токену не положено или обмен
    выключен у проекта; 404 — проекта, агента или сообщения нет; 409 — сессия без подписки, адресат не в сети
    (безрольный, вне проектов), несколько агентов по адресу, `to.sub_id` владельца — уже не та сессия; 422 —
    текст, себе, ответ владельцу позже суток, `to.sub_id` у bearer."""
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    waiters = _waiters(request)
    session = body.session.model_dump() if body.session is not None else None
    to = body.to.model_dump() if body.to is not None else None

    def _fn(conn):
        error = _writer_error(conn, principal)
        if error is not None:
            raise peer_msgs_mod.PeerError(403, error)
        sent = peer_msgs_mod.send(
            conn,
            principal=principal,
            session=session,
            text=body.text,
            to=to,
            reply_to=body.reply_to,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            waiting=waiters.waiting,
            urgent=body.urgent,
        )
        row = peer_msgs_mod.msg_dict(conn, sent.rec) if sent.rec is not None else None
        return sent, row, _day_closed(conn, sent.live_sub_id)

    try:
        sent, row, day_closed = await db.write(_fn)
    except peer_msgs_mod.PeerError as exc:
        raise _http(exc) from exc
    live = sent.rec.to_user == 1 or sent.live_sub_id is not None
    waiting = (
        sent.live_sub_id is not None
        and waiters.waiting(sent.live_sub_id)
        and waiters.protocol(sent.live_sub_id) >= SUB_WAIT_PEERS_PROTOCOL
    )
    recipient: dict = {"live": live, "waiting": waiting}
    if sent.batch_ts is not None:
        recipient["batch_ts"] = sent.batch_ts  # Ф48-4 п.5: без пачки ответ прежний
    if day_closed:
        recipient["day_closed"] = True  # Ф64-1: адресат — сессия хаба проекта с закрытым днём
    return {"msg": row, "recipient": recipient}


def _day_closed(conn, live_sub_id: int | None) -> bool:
    """Ф64-1 (раздел 3): адресат — сессия хаба (не внешняя) проекта, чей рабочий день закрыт: сообщение
    дождётся «Начать день»."""
    if live_sub_id is None:
        return False
    sub = subs_mod.get_sub(conn, live_sub_id)
    if sub is None or held_mod.held_id(sub.session_pid) is None:
        return False
    return workday_mod.project_closed_ts(conn, sub.project_id) is not None


@public_router.get("/peer-msgs")
async def get_peer_msgs(
    request: Request,
    all: int = Query(0, ge=0, le=1),  # noqa: A002 — имя параметра API
    limit: int = Query(peer_msgs_mod.LIST_DEFAULT, ge=1, le=peer_msgs_mod.LIST_MAX),
    before: int | None = Query(None, ge=1, le=_ID_MAX),
    pid: int | None = Query(None, ge=-_PID_MAX, le=_PID_MAX),  # Ф41-2а П4: и сессия хаба
    proc_start: int | None = Query(None, ge=0, le=_PROC_START_MAX),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    host: str | None = HOST_QUERY,
    principal: Principal = _read,
) -> dict:
    """UI-сессия: вся переписка в скоупе проектов, новые первыми (`limit`, `before`). Bearer со своей сессией
    (`pid`, `proc_start`, `session_id` — все три): непрочитанные входящие своей линии или подписки (Н6);
    `all=1` — ещё прочитанные входящие и свои отправленные за 7 дней; в ответе `sub` — адрес сессии. Bearer
    без сессии — 403. Сам список ничего не отмечает: прочитанное — `POST /peer-msgs/read`."""
    triple_query(pid, proc_start, session_id)
    now_ms = _now_ms(request)
    db = request.app.state.db
    if principal.is_ui:
        if pid is not None:
            raise HTTPException(422, "session — только у bearer-токена сессии")
        with db.reader() as conn:
            return {"items": peer_msgs_mod.list_for_ui(conn, principal, limit=limit, before=before)}
    if pid is None:
        raise HTTPException(
            403, "переписку агентов читает сессия с подпиской — нужны pid, proc_start, session_id"
        )
    try:
        with db.reader() as conn:
            error = _subscriber_error(conn, principal)
            if error is not None:
                raise peer_msgs_mod.PeerError(403, error)
            session = query_session(pid, proc_start, session_id, host)
            sub = peer_msgs_mod.session_sub(conn, principal, session)
            items = peer_msgs_mod.inbox(conn, sub, include_all=bool(all), now_ms=now_ms)
            machine = machines_mod.of_rec(conn, sub, now_ms=now_ms)
    except peer_msgs_mod.PeerError as exc:
        raise _http(exc) from exc
    return {
        "items": items,
        "sub": {
            "id": sub.id,
            "project_id": sub.project_id,
            "role": sub.role,
            "role_no": sub.role_no,
            "machine": machine,  # Ф77-1 (C4): машина получателя — сравнить с машиной отправителя
        },
    }


@public_router.post("/peer-msgs/ask", status_code=201)
async def post_peer_msg_ask(
    body: AskIn, request: Request, response: Response, principal: Principal = _any
) -> dict:
    """Ф56-1б (4.3, А4б): вопрос владельцу от сессии с живой подпиской-ролью — без его предыдущего сообщения и
    без предела суток; карточка `agent_msg` в канале владельца, ответ придёт сообщением «ОТ ВЛАДЕЛЬЦА». 201 — `{id,
    seq}`. 403 — токену подписка не положена; 409 — нет подписки или она без роли; 422 — UI-сессия, текст,
    `about_role`. Ф93-1: `money` — вопрос о тратах (его карточка идёт по строке «Деньги» маршрута)."""
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        if principal.is_ui:
            raise peer_msgs_mod.PeerError(422, "вопрос владельцу задаёт сессия с подпиской, а не интерфейс")
        error = _writer_error(conn, principal)
        if error is not None:
            raise peer_msgs_mod.PeerError(403, error)
        return peer_msgs_mod.ask_user(
            conn,
            principal=principal,
            session=session,
            text=body.text,
            about_role=body.about_role,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            money=body.money,
        ).rec

    try:
        rec = await db.write(_fn)
    except peer_msgs_mod.PeerError as exc:
        raise _http(exc) from exc
    return {"id": rec.id, "seq": rec.seq}


@public_router.post("/peer-msgs/read")
async def post_peer_msgs_read(
    body: ReadIn, request: Request, response: Response, principal: Principal = _any
) -> dict:
    """Прочитано: сессия (`session`) — свои непрочитанные входящие; UI-сессия владельца (без `session`) — только
    ответы агентов ему (`to_user`). Чужие и уже прочитанные — мимо; `marked` — сколько отмечено."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        error = _writer_error(conn, principal)
        if error is not None:
            raise peer_msgs_mod.PeerError(403, error)
        if principal.is_ui:
            if session is not None:
                raise peer_msgs_mod.PeerError(422, "session — только у bearer-токена сессии")
            sub = None
        else:
            sub = peer_msgs_mod.session_sub(conn, principal, session)
        return peer_msgs_mod.mark_read(
            conn,
            body.ids,
            principal=principal,
            sub=sub,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
        )

    try:
        return {"marked": await db.write(_fn)}
    except peer_msgs_mod.PeerError as exc:
        raise _http(exc) from exc


@public_router.get("/agent/peer-msgs/alerts")
async def get_agent_peer_alerts(request: Request, principal: Principal = _triage) -> dict:
    """П7, Н8: непрочитанные ответы агентов владельцу, уведомление по которым ещё не уходило;
    принципалу со списком проектов — только строки его проектов."""
    with request.app.state.db.reader() as conn:
        return {"items": peer_msgs_mod.alerts(conn, principal)}


@public_router.post("/agent/peer-msgs/notified")
async def post_agent_peer_notified(
    body: NotifiedIn, request: Request, principal: Principal = _triage
) -> dict:
    """Уведомление ушло (или пропущено пределом Н8) — `notified_ts`, один раз; в тихие часы hub-agent эту
    ручку не зовёт."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    marked = await db.write(
        lambda conn: peer_msgs_mod.mark_notified(conn, body.ids, principal, now_ms=now_ms)
    )
    return {"marked": marked}
