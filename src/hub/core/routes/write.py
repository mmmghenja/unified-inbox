"""Пишущие ручки `/api/v1`. Спека, раздел «Этап 4b — пишущие ручки». Маршруты не пишут SQL: видимость
канала/outbox — через `queries.py`, запись — через `outbox.py`/`channels.py`, аудит — через `audit.py`; всё
это в ОДНОЙ транзакции `Db.write()`, начиная с проверки видимости.

`send` — scope `send` ИЛИ `draft`, origin по типу принципала (`ui` под cookie, `api` под bearer; от bearer —
всегда черновик `pending_approval`, спека 7a, A1; единственное исключение — Ф37-1: беседа из списка «без
подтверждения» владельца и роль подписки сессии, все условия — `autosend.try_send`). `PUT /channels/{id}/autosend`
— сам этот список, только UI-сессия. `approve`/`send-now`/`resolve-unknown`/`read` — только UI-сессия
(`ui_only`). `cancel` — UI-сессия или bearer со скоупом `draft`/`send`, но только СВОЙ черновик в
`pending_approval` (спека 7a, A4/П1). `PATCH channel` — UI-сессия или bearer со скоупом `organize`, только
раскладка (`project_id`, `map_source: "auto"`) и без расширения видимости проектных токенов (спека 7a,
A3/П4). `POST /channels/resolve` — обычная читающая ручка (scope `read`), просто через POST,
потому что берёт тело; SQL для неё — в `queries.resolve_channels`, читается через `db.reader()`, аудита не
пишет (ничего не меняет).
"""

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from ... import hostid
from .. import answered as answered_mod
from .. import autopilot as autopilot_mod
from .. import autosend as autosend_mod
from .. import card_route as card_route_mod
from .. import channels as channels_mod
from .. import decisions as decisions_mod
from .. import draft_channels, hiding, mapping, outbox_ops, queries
from .. import draft_outcomes as draft_outcomes_mod
from .. import held as held_mod
from .. import outbox as outbox_mod
from .. import owner_queue as zq
from .. import peer_flood as peer_flood_mod
from .. import read_ack as read_ack_mod
from .. import session_host as session_host_mod
from .. import threads as threads_mod
from .. import uploads as uploads_mod
from ..audit import write_audit
from ..db import db_of
from ..security import Principal, human_only, principal_any_scope, principal_dependency

router = APIRouter(tags=["write"])

_send_scope = Depends(principal_any_scope("send", "draft"))
_read_scope = Depends(principal_dependency("read"))
_authed = Depends(principal_dependency())


async def _deny_bearer(request: Request, principal: Principal) -> None:
    """Аудит `ui_only_denied` отдельной транзакцией и 403. Bearer полез в действие, которое ему не положено
    (подтвердить свой черновик, пометить прочитанным, отменить чужой черновик) — то событие из модели угроз,
    которое обязано оставить след (критик, F8). Без текста, путь — в detail."""
    db = request.app.state.db
    now_ms = int(request.app.state.clock() * 1000)
    await db.write(
        lambda conn: write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=_client_name(request),
            request_id=uuid.uuid4().hex,
            action="ui_only_denied",
            result="denied",
            detail={"method": request.method, "path": request.url.path[:200]},
        )
    )
    raise HTTPException(403, "forbidden: ui session required")


async def _ui_only(request: Request, principal: Principal = _authed) -> Principal:
    if not principal.is_ui:
        await _deny_bearer(request, principal)
    return principal


ui_only = Depends(_ui_only)

_human_ok = Depends(principal_dependency(human_ok=True))


async def _ui_or_agent(request: Request, principal: Principal = _human_ok) -> Principal:
    """Ф88-1 (A1): действие владельца над сессией хаба — кука интерфейса или его агент (bearer ровно `human`,
    `owner_queue.is_agent`). Прочие (бот `human_bot`, токены сессий, хост) — тот же отказ, что у `ui_only`:
    403 и аудит `ui_only_denied`; ключ терминала и прочие служебные — 403 уже в `principal_dependency`."""
    if not (principal.is_ui or zq.is_agent(principal)):
        await _deny_bearer(request, principal)
    return principal


ui_or_agent = Depends(_ui_or_agent)


async def _read_or_agent(request: Request, principal: Principal = _human_ok) -> Principal:
    """Ф88-1 (A2, П6): чтение сессий хаба — кука, агент владельца (у него нет `read`, видит как кука) или bearer со
    скоупом `read`, как прежде у `principal_dependency("read")`; бот `human_bot` — 403."""
    if principal.is_ui or zq.is_agent(principal):
        return principal
    if principal.has_scope("read") and not human_only(principal):
        return principal
    raise HTTPException(403, "forbidden")


read_or_agent = Depends(_read_or_agent)


async def _authed_or_agent(principal: Principal = _human_ok) -> Principal:
    """Ф88-1 (П1, П4): ручка, открытая любому принципалу (сессиям — по своим проверкам в ручке), плюс агенту
    владельца. Бот `human_bot` — 403 сразу, как у `principal_dependency()` (до разбора тела)."""
    if human_only(principal) and not zq.is_agent(principal):
        raise HTTPException(403, "forbidden")
    return principal


authed_or_agent = Depends(_authed_or_agent)


def touch_activity(request: Request, principal: Principal) -> None:
    """Ф20-3 A2: действие владельца из UI (отправка, подтверждение, прочтение, реакция) — «активен в хабе»."""
    if principal.is_ui:
        request.app.state.activity.touch(int(request.app.state.clock() * 1000))


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


def _new_request_id(response: Response) -> str:
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    return request_id


class SendSessionIn(BaseModel):
    """Ф37-1 B1: сессия черновика — те же поля и пределы, что `routes.subs.SessionIn` (pid + `starttime` +
    разговор). Своя модель, а не импорт: `routes.subs` тянет `routes.work`, а тот — этот модуль (цикл)."""

    model_config = ConfigDict(extra="forbid")

    # Ф41-2а П4: и условная тройка сессии хаба (`pid < 0`) — тем же валидатором, что `routes.subs.SessionIn`.
    pid: StrictInt = Field(ge=-(2**31 - 1), le=2**31 - 1)
    proc_start: StrictInt = Field(ge=0, le=2**62)
    session_id: str = Field(min_length=1, max_length=128)
    # Ф33-1: машина сессии — как у `routes.subs.SessionIn` (там же — почему контакт в валидаторе).
    host: str | None = Field(default=None, pattern=session_host_mod.HOST_PATTERN)
    boot: str | None = Field(default=None, pattern=hostid.BOOT_PATTERN)

    @model_validator(mode="after")
    def _triple(self):
        error = held_mod.triple_error(self.pid, self.proc_start, self.session_id)
        if error is not None:
            raise ValueError(error)
        session_host_mod.touch_session(self)  # Ф33-1 (К9): запрос сессии другой машины — её контакт
        return self


class SendIn(BaseModel):
    channel_id: int
    text: str
    reply_to: str | None = None
    thread: str | None = None
    idempotency_key: str
    # Ф10-4 A3 + Ф11-3b A2: до 10 `upload_id` без повторов (загрузки этой беседы, `POST /uploads`) — одно
    # сообщение источнику; с ними текст — подпись и может быть пустым. Ф14-3 A4: bearer — только со скоупом
    # `attach` (без него 422), и строка у него всегда черновик `pending_approval`.
    files: list[str] | None = None
    # Ф12-2 A2/П3: черновик «по графику» (окно и суточный лимит аккаунта после подтверждения владельца) — только
    # bearer (повторные касания по списку); UI с `paced: true` — 422, чтобы не путать владельца.
    paced: bool = False
    # Ф30-1 A2: отправить к этому времени после подтверждения владельца (`"YYYY-MM-DD HH:MM"` в поясе аккаунта или
    # ISO со смещением) — только bearer, не вместе с `paced`; UI — 422.
    send_at: str | None = None
    # Ф30-2 решение 1: догон к сроку не уйдёт, если после подтверждения в беседе ответили — только bearer и
    # только вместе с `send_at` (без него — 422 из ядра).
    cancel_if_reply: bool = False
    # Ф37-1 B1: сессия, положившая черновик (MCP кладёт её, как у `hub_react`); только bearer, UI — 422. Без
    # неё — всё как раньше. С ней черновик может уйти сразу — только при всех условиях B2
    # (`autosend.try_send`).
    session: SendSessionIn | None = None
    # Ф39 B2 (приёмка): сессия-автор черновика, который сессия сама просит подтвердить (MCP `confirm=True`).
    # Только для записи автора и `heard`: авто-отправка (`autosend.try_send`) это поле не видит — ей нужна
    # именно `session`. Отдельное поле, а не флаг при `session`: ядро старее Ф39 неизвестное поле отбросит и
    # положит обычный черновик, а флаг отбросило бы и отправило сразу. Только bearer; вместе с `session` —
    # 422.
    author_session: SendSessionIn | None = None
    # Ф41-5б П1: ключ запуска сессии хаба (MCP кладёт его из `HUB_SESSION_KEY`) — только для проверки
    # `autopilot.session_free`; нигде не сохраняется. Только bearer и только вместе с `session`.
    session_key: str | None = Field(default=None, max_length=128)


@router.post("/messages/send")
async def send_message(
    body: SendIn, request: Request, response: Response, principal: Principal = _send_scope
) -> dict:
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    origin = "ui" if principal.kind == "ui" else "api"
    files = body.files or []
    if files and not principal.is_ui and not principal.has_scope("attach"):
        # Ф14-3 A4: bearer с файлами — только со скоупом `attach`; строка всё равно `pending_approval` (7a).
        raise HTTPException(422, "files: профилю не выдано право прикладывать файлы")
    if len(files) > uploads_mod.MAX_FILES_PER_MESSAGE:
        raise HTTPException(422, "files: не больше 10 файлов в одном сообщении")
    if len(set(files)) != len(files):
        raise HTTPException(422, "files: один и тот же файл дважды")
    if body.paced and principal.is_ui:
        raise HTTPException(422, "paced: только для черновиков сессий")
    if body.send_at is not None and principal.is_ui:
        raise HTTPException(422, "send_at: только для черновиков сессий")
    if body.cancel_if_reply and principal.is_ui:
        raise HTTPException(422, "cancel_if_reply: только для черновиков сессий")
    if body.session is not None and principal.is_ui:
        raise HTTPException(422, "session: только для черновиков сессий")
    if body.author_session is not None and principal.is_ui:
        raise HTTPException(422, "author_session: только для черновиков сессий")
    if body.author_session is not None and body.session is not None:
        raise HTTPException(422, "author_session: не вместе с session")
    if body.session_key is not None and (principal.is_ui or body.session is None):
        raise HTTPException(422, "session_key: только вместе с session")
    author_session = body.session if body.session is not None else body.author_session
    sub_waiters = getattr(request.app.state, "sub_waiters", None)

    def _fn(conn):
        project_id = queries.channel_visible(conn, principal, body.channel_id)
        result = outbox_mod.enqueue(
            conn,
            channel_id=body.channel_id,
            text=body.text,
            reply_to_ext_id=body.reply_to,
            thread_ext_id=body.thread,
            origin=origin,
            api_token_id=principal.token_id,
            idempotency_key=body.idempotency_key,
            now_ms=now_ms,
            upload_ids=files,
            paced=body.paced,
            send_at=body.send_at,
            cancel_if_reply=body.cancel_if_reply,
        )
        send_at_ts = outbox_mod.send_at_of(conn, result.outbox_id)
        cancel_if_reply = outbox_mod.cancel_if_reply_of(conn, result.outbox_id)
        detail: dict = {
            "repeat": not result.created,
            "text_len": len(body.text),
            "files": len(files),
            "paced": result.paced,  # Ф12-5 П6: фактический, не запрошенный
            "send_at_ts": send_at_ts,
        }
        if cancel_if_reply:
            detail["cancel_if_reply"] = True  # Ф30-2 A2
        if not principal.is_ui and result.created and author_session is not None:
            # Ф66-1, ревью Р1: сессия хаба — автор строки (по условной тройке запроса, с подпиской или без) —
            # для маршрута карточки: ручная сессия хаба — всегда владельцу (`card_route.draft_hub`).
            author_hub = card_route_mod.creating_hub(conn, author_session)
            if author_hub is not None:
                detail[card_route_mod.DRAFT_HUB_KEY] = author_hub
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="message_send",
            result=result.status,
            channel_id=body.channel_id,
            outbox_id=result.outbox_id,
            detail=detail,
        )
        # Ф39 B2: автор черновика — подписка сессии на проект и её роль; только у строки, созданной ЭТИМ
        # запросом (не повтор по ключу). До `try_send` и на его решение не влияет. B4 + П7: `heard` — чем
        # эта сессия слышит беседу (для подсказки «подпишись»).
        heard = None
        if not principal.is_ui and author_session is not None:
            if result.created:
                draft_outcomes_mod.set_author(
                    conn,
                    result.outbox_id,
                    token_id=principal.token_id,
                    session=author_session,
                    project_id=project_id,
                )
            heard = draft_outcomes_mod.heard(
                conn, token_id=principal.token_id, session=author_session, channel_id=body.channel_id
            )
        # Ф37-1 B2: в ТОЙ ЖЕ транзакции — черновик сессии с ролью из списка владельца подтверждается сам (все
        # условия — `autosend.try_send`; журнал — `outbox_autosend`, не `outbox_approve`).
        auto = None
        if not principal.is_ui and body.session is not None:
            name_row = conn.execute(
                "SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)
            ).fetchone()
            auto = autosend_mod.try_send(
                conn,
                principal=principal,
                token_name=name_row[0] if name_row and isinstance(name_row[0], str) else None,
                session=body.session,
                channel_id=body.channel_id,
                project_id=project_id,
                result=result,
                send_at_ts=send_at_ts,
                now_ms=now_ms,
                waiting=sub_waiters.waiting if sub_waiters is not None else None,
                session_key=body.session_key,
            )
        if not principal.is_ui and result.created:
            # Ф66-1 (П9, П11): черновик, который ждёт владельца, — «сразу да» по таблице маршрута проекта. Ответ
            # сессии — как до клика владельца (исход придёт сигналом автору).
            card_route_mod.auto_draft(
                conn,
                result.outbox_id,
                now_ms=now_ms,
                waiting=sub_waiters.waiting if sub_waiters is not None else None,
            )
        return result, send_at_ts, cancel_if_reply, auto, heard

    try:
        result, send_at_ts, cancel_if_reply, auto, heard = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, queries.not_found_detail(exc)) from exc
    except outbox_mod.IdempotencyConflict as exc:
        raise HTTPException(409, "idempotency conflict") from exc
    except outbox_mod.DraftChannelBusy as exc:
        raise HTTPException(409, str(exc)) from exc  # Ф12-1, решение 4
    except outbox_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.OutboxError as exc:
        raise HTTPException(422, str(exc)) from exc

    answer = {
        "outbox_id": result.outbox_id,
        "message_id": result.message_id,
        "status": result.status,
        "created": result.created,
        "paced": result.paced,
        "send_at_ts": send_at_ts,  # Ф30-1 A2
        "cancel_if_reply": cancel_if_reply,  # Ф30-2 A2
    }
    if auto is not None and auto.role is not None:
        # Ф37-1 B4: ушло сразу, без подтверждения владельца.
        answer["status"] = "queued"
        answer["auto"] = {"role": auto.role}
        if auto.by is not None:
            # Ф41-5б (5.4): ушло веткой выключателя автопилота; `mode` — сразу (`null`), по графику
            # аккаунта (`paced`) или в срок (`send_at`).
            answer["auto"].update(by=auto.by, mode=auto.mode)
        if auto.by == autosend_mod.BOT_CHAT:
            # Ф63-2 П1 + раздел 8 спеки: ветка `bot_chat` — без графика, кроме явного `paced=True` сессии.
            answer["paced"] = auto.mode == "paced"
    elif auto is not None and auto.denied is not None:
        # Ф37-1 B4 + П8: не прошло только по лимиту (беседы или токена) — обычный черновик с пометкой.
        answer["auto_denied"] = auto.denied
        answer["auto_limit"] = auto.limit_of
        if auto.guard is not None:
            answer["auto_guard"] = auto.guard  # Ф63-2 (1.3): страховка беседы с ботом — категория
    if heard is not None:
        answer["heard"] = heard  # Ф39 B4 + П7: только запросу с `session`
    return answer


@router.post("/outbox/{outbox_id}/cancel")
async def cancel_outbox(
    outbox_id: int, request: Request, response: Response, principal: Principal = _authed
) -> dict:
    """Спека 7a, A4/П1: UI — как раньше; bearer со скоупом `draft`/`send` — только СВОЙ черновик в
    `pending_approval`. Проверка владельца и статуса — в `outbox.cancel` В ТОЙ ЖЕ транзакции (иначе гонка с
    подтверждением владельца); отказ — `NotOwnDraft`, откат пустой транзакции, аудит `ui_only_denied` отдельной
    записью, 403. Невидимый outbox — 404 раньше проверки владельца."""
    if not principal.is_ui and not (principal.has_scope("draft") or principal.has_scope("send")):
        await _deny_bearer(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    owner_token_id = None if principal.is_ui else principal.token_id
    audit = {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": client_name,
        "request_id": request_id,
    }

    def _fn(conn):
        if principal.is_ui:
            # Ф65-1: черновик решал агент — ответ владельца поручение закрывает (сессия свой снимает как раньше).
            zq.take_from_agent(conn, f"draft:{outbox_id}", now_ms=now_ms)  # Ф65-1 Ж1
        card = principal.is_ui and outbox_mod.status_of(conn, outbox_id) == "pending_approval"
        # Ф56-1а: тело — `outbox_ops.cancel_draft` (тот же код зовёт ответ из канала владельца).
        new_status = outbox_ops.cancel_draft(
            conn,
            outbox_id,
            principal=principal,
            owner_token_id=owner_token_id,
            record_outcome=principal.is_ui,
            now_ms=now_ms,
            audit=audit,
        )
        if card:  # Ф76-1 (П5, П6): отклонил владелец; отмена своего черновика сессией — не решение
            decisions_mod.record(conn, f"draft:{outbox_id}", decisions_mod.BY_UI, "cancel", now_ms=now_ms)
        return new_status

    try:
        new_status = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.NotOwnDraft:
        await _deny_bearer(request, principal)
    except outbox_mod.InvalidTransition as exc:
        raise HTTPException(409, "invalid transition") from exc

    return {"status": new_status}


@router.post("/outbox/{outbox_id}/send-now")
async def send_now_outbox(
    outbox_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Отправить сейчас»: подтверждённая строка, ждущая слота графика или срока, уходит
    при ближайшей выдаче (`outbox.release_now`). Только кука интерфейса (`ui_only`: bearer — 403 с аудитом
    `ui_only_denied`), в том числе с телефона. Не ждёт (уже уходит / ушло / не в очереди) — 409 с причиной."""
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    _new_request_id(response)

    def _fn(conn):
        queries.outbox_visible(conn, principal, outbox_id)
        outbox_mod.release_now(conn, outbox_id, now_ms=now_ms)  # аудит `outbox_send_now` — в ядре

    try:
        await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.NotWaiting as exc:
        return JSONResponse({"detail": exc.reason, "error": "not_waiting"}, status_code=409)

    return {"status": "queued"}


class ApproveIn(BaseModel):
    text: str | None = None
    # Ф30-1 решение 4: «Отправить сейчас» — время `send_at` прошло, владелец отправляет обычным порядком.
    send_now: bool = False


def _send_at_passed_text(exc: outbox_mod.SendAtPassed) -> str:
    """Ф30-1 решение 4: «время отправки ЧЧ:ММ прошло». Ф32-2 п.3 + П8: в поясе аккаунта строки (`pace_tz`,
    тот же, в котором сессия задавала `send_at`); пояса нет — местное время ядра."""
    if exc.tz is not None:
        moment = dt.datetime.fromtimestamp(exc.send_at_ts / 1000, exc.tz)
    else:
        moment = dt.datetime.fromtimestamp(exc.send_at_ts / 1000).astimezone()
    return f"время отправки {moment:%H:%M} прошло"


_approve_detail = outbox_ops.approve_detail  # Ф56-1а: вынесено в ядро (подтверждение пачкой зовёт его же)


def _approve_answer(approved: outbox_mod.Approved) -> dict:
    """Ф30-1 П7: `paced` — только у графика; `not_before_ts` — у графика и у точного времени; `send_at_ts` —
    только у точного времени (прежний ответ остальных строк не меняется)."""
    answer: dict = {"paced": approved.mode == "paced", "not_before_ts": approved.not_before_ts}
    if approved.mode == "send_at":
        answer["send_at_ts"] = approved.send_at_ts
    return answer


@router.post("/outbox/{outbox_id}/approve")
async def approve_outbox(
    outbox_id: int, body: ApproveIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    audit = {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": client_name,
        "request_id": request_id,
    }

    def _fn(conn):
        zq.take_from_agent(conn, f"draft:{outbox_id}", now_ms=now_ms)  # Ф65-1 Ж1
        # Ф56-1а (П9): тело — `outbox_ops.approve_draft` (тот же код зовёт ответ из канала владельца).
        approved = outbox_ops.approve_draft(
            conn,
            outbox_id,
            principal=principal,
            text=body.text,
            send_now=body.send_now,
            now_ms=now_ms,
            source="ui",
            audit=audit,
        )
        decisions_mod.record(  # Ф76-1 (П5)
            conn, f"draft:{outbox_id}", decisions_mod.BY_UI, "approve", body.text, now_ms=now_ms
        )
        return approved

    try:
        approved = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.SendAtPassed as exc:
        # Ф30-1 решение 4 + П8: ничего не изменено; UI предлагает «Отправить сейчас» (`send_now: true`).
        return JSONResponse(
            {
                "detail": _send_at_passed_text(exc),
                "error": "send_at_passed",
                "send_at_ts": exc.send_at_ts,
            },
            status_code=409,
        )
    except outbox_mod.InvalidTransition as exc:
        raise HTTPException(409, "invalid transition") from exc
    except outbox_mod.OutboxError as exc:
        raise HTTPException(422, str(exc)) from exc

    # Ф12-2 A3: черновик по графику — слот, когда уйдёт; Ф30-1 — и время `send_at`.
    return {"status": "queued", **_approve_answer(approved)}


APPROVE_BATCH_MAX = 50
BATCH_FILES_ERROR = "файлы — подтвердите из карточки"


class ApproveBatchIn(BaseModel):
    ids: list[int]


@router.post("/outbox/approve-batch")
async def approve_outbox_batch(
    body: ApproveBatchIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Ф12-2 A4, решение 4: подтверждение пачкой, только UI-сессия, без правки текста. Каждый id — как
    одиночный `approve` (видимость, свои ошибки), слоты графика раздаются по порядку `ids`. Одна транзакция,
    у каждого id — своя точка сохранения: ошибка одного откатывает только его."""
    touch_activity(request, principal)
    if not body.ids:
        raise HTTPException(422, "ids: пусто")
    if len(body.ids) > APPROVE_BATCH_MAX:
        raise HTTPException(422, f"ids: не больше {APPROVE_BATCH_MAX}")
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _one(conn, outbox_id: int) -> dict:
        channel_id = queries.outbox_visible(conn, principal, outbox_id)
        zq.take_from_agent(conn, f"draft:{outbox_id}", now_ms=now_ms)  # Ф65-1 Ж1
        if outbox_mod.has_files(conn, outbox_id):
            # Ф14-3 A8 (П11 плана): черновик с файлами владелец подтверждает только из карточки, глядя на файлы.
            raise outbox_mod.OutboxError(BATCH_FILES_ERROR)
        approved = outbox_mod.approve_mode(conn, outbox_id, now_ms=now_ms)
        outbox_mod.set_by_hand(conn, outbox_id, True)  # Ф70-1 B.1: пачку подтверждает только кука интерфейса
        detail = _approve_detail(approved, {"batch": True})
        # Ф37-2 A1: каждая беседа пачки отмечается по своему черновику.
        detail.update(answered_mod.mark_on_approve(conn, outbox_id, now_ms=now_ms) or {})
        # Ф39 B5 + П6: исход — под точкой сохранения этой строки (пачкой текст не правится).
        draft_outcomes_mod.record(conn, outbox_id, "approved", now_ms=now_ms)
        decisions_mod.record(conn, f"draft:{outbox_id}", decisions_mod.BY_UI, "approve", now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="outbox_approve",
            result="queued",
            channel_id=channel_id,
            outbox_id=outbox_id,
            detail=detail,
        )
        item: dict = {"id": outbox_id, "status": "queued", "paced": approved.mode == "paced"}
        if approved.not_before_ts is not None:
            item["not_before_ts"] = approved.not_before_ts
        if approved.mode == "send_at":
            item["send_at_ts"] = approved.send_at_ts
        return item

    def _fn(conn):
        results = []
        for outbox_id in body.ids:
            conn.execute("SAVEPOINT approve_one")
            try:
                item = _one(conn, outbox_id)
            except queries.NotFound, outbox_mod.NotFound:
                item = {"id": outbox_id, "status": "error", "error": "not found"}
            except outbox_mod.SendAtPassed as exc:
                # Ф30-1 П8: ошибка только этого id, пачка не падает; «Отправить сейчас» — из UI по этому id.
                item = {
                    "id": outbox_id,
                    "status": "error",
                    "error": "send_at_passed",
                    "send_at_ts": exc.send_at_ts,
                    "message": _send_at_passed_text(exc),
                }
            except outbox_mod.InvalidTransition:
                item = {"id": outbox_id, "status": "error", "error": "invalid transition"}
            except outbox_mod.OutboxError as exc:
                item = {"id": outbox_id, "status": "error", "error": str(exc)}
            if item["status"] == "error":
                conn.execute("ROLLBACK TO approve_one")
            conn.execute("RELEASE approve_one")
            results.append(item)
        return results

    results = await db.write(_fn)
    return {"results": results}


class ResolveUnknownIn(BaseModel):
    decision: str


@router.post("/outbox/{outbox_id}/resolve-unknown")
async def resolve_unknown_outbox(
    outbox_id: int,
    body: ResolveUnknownIn,
    request: Request,
    response: Response,
    principal: Principal = ui_only,
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        channel_id = queries.outbox_visible(conn, principal, outbox_id)
        new_status = outbox_mod.resolve_unknown(conn, outbox_id, decision=body.decision, now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="outbox_resolve_unknown",
            result=new_status,
            channel_id=channel_id,
            outbox_id=outbox_id,
            detail={"decision": body.decision},
        )
        return new_status

    try:
        new_status = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.InvalidTransition as exc:
        raise HTTPException(409, "invalid transition") from exc
    except outbox_mod.OutboxError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {"status": new_status}


class ReadIn(BaseModel):
    up_to_message_id: int


@router.post("/channels/{channel_id}/read")
async def mark_channel_read(
    channel_id: int, body: ReadIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        queries.channel_visible(conn, principal, channel_id)
        changed = channels_mod.mark_read(
            conn, channel_id, up_to_message_id=body.up_to_message_id, now_ms=now_ms
        )
        detail: dict = {"up_to_message_id": body.up_to_message_id}
        if changed:
            # Ф8-7 A2: толкаем в источник только то, что сдвинул UI, — в той же транзакции.
            detail["read_ack"] = read_ack_mod.enqueue(conn, channel_id, body.up_to_message_id, now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="channel_read",
            result="changed" if changed else "unchanged",
            channel_id=channel_id,
            detail=detail,
        )
        return changed

    try:
        changed = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except channels_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except channels_mod.InvalidReplyTarget as exc:
        raise HTTPException(422, str(exc)) from exc

    return {"changed": changed}


class ThreadReadIn(BaseModel):
    up_to_message_id: int


@router.post("/channels/{channel_id}/threads/{thread_ext_id}/read")
async def mark_thread_read_route(
    channel_id: int,
    thread_ext_id: str,
    body: ThreadReadIn,
    request: Request,
    response: Response,
    principal: Principal = ui_only,
) -> dict:
    """Раздел E спеки 2b: тредовая `read` — только UI-сессия, только вперёд. Сообщение не из этого треда
    (родитель допустим) — 422."""
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        queries.channel_visible(conn, principal, channel_id)  # только проверка видимости (404 иначе)
        changed = threads_mod.mark_thread_read(
            conn,
            db_of(channel_id),
            channel_id,
            thread_ext_id,
            up_to_message_id=body.up_to_message_id,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="thread_read",
            result="changed" if changed else "unchanged",
            channel_id=channel_id,
            detail={"thread_ext_id": thread_ext_id, "up_to_message_id": body.up_to_message_id},
        )
        return changed

    try:
        changed = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except threads_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except threads_mod.WrongThread as exc:
        raise HTTPException(422, str(exc)) from exc

    return {"changed": changed}


class ChannelPatchIn(BaseModel):
    project_id: int | None = None
    muted: bool | None = None
    archived: bool | None = None
    map_source: str | None = None
    # Ф8-3 A6/П6: ручной режим внимания, `null` — «авто». Только UI-сессия: bearer с этим ключом (даже
    # `null`, любой скоуп) — 403, как `muted` (7a: `organize` — только раскладка).
    attention: Literal["react", "info"] | None = None
    # Ф34-1 A2: стадия беседы и заметка к ней; `null`/пустая строка — снять. Длину и переводы строк проверяет
    # `channels.update_channel` (422).
    stage: str | None = None
    stage_note: str | None = None
    # Ф41-5б П5: сессия (тройка) и ключ запуска сессии хаба — по ним ядро узнаёт сессию в авто: её
    # перенос регулирует выключатель `move_channel`. Только bearer; в запись беседы не идут.
    session: SendSessionIn | None = None
    session_key: str | None = Field(default=None, max_length=128)


# Спека 7a, A3: bearer со скоупом `organize` меняет только раскладку; `muted`/`archived` — про внимание владельца.
# Ф34-1 A2: и стадию беседы (тот же скоуп, беседа видна токену).
_BEARER_PATCH_FIELDS = frozenset(
    {"project_id", "map_source", "stage", "stage_note", "session", "session_key"}
)
_PATCH_IDENT_FIELDS = frozenset({"session", "session_key"})


class WidensScope(Exception):
    """Спека 7a, П4: перенос открыл бы канал проектному токену (T в его списке, S — нет). 409."""

    def __init__(self, token_names: list[str], hint: str) -> None:
        super().__init__(", ".join(token_names))
        self.token_names = token_names
        self.hint = hint


class MoveHeld(Exception):
    """Ф41-5б П5: перенос просит сессия хаба в авто, а выключатель `move_channel` для проекта
    назначения стоит (или цель — не её проект). 409; `request_hint` — можно ли подать заявку."""

    def __init__(self, request_hint: bool) -> None:
        super().__init__("move_channel")
        self.request_hint = request_hint


MOVE_HELD_TEXT = "move_channel: в авто перенос беседы придержан выключателем автопилота — решает владелец"


def _check_auto_move(conn, principal: Principal, channel_id: int, fields: dict, ident) -> bool:
    """П5: `True` — запрос от сессии хаба в авто и перенос ей разрешён выключателем (в том числе
    расширяющий видимость): проверку `widens_scope` пропускаем, цель обязана быть видимым проектом.
    `False` — это не сессия хаба в авто (обычный путь). Сессия в авто без разрешения — `MoveHeld`."""
    target = fields.get("project_id") if fields.get("map_source") != "auto" else None
    explicit = isinstance(target, int) and not isinstance(target, bool) and target > 0
    if autopilot_mod.auto_session(conn, ident) is None:
        if autopilot_mod.claims_auto(conn, ident):
            # Ревью 41-5: сессия в авто без подтверждённого ключа — карточки инструмента у неё нет,
            # обычным путём перенос не идёт.
            raise MoveHeld(request_hint=explicit)
        return False
    if not explicit or autopilot_mod.session_free(conn, ident, "move_channel", target) is None:
        raise MoveHeld(request_hint=explicit)
    if not queries.project_visible(conn, principal, target):
        raise channels_mod.NotFound(f"project_id {target} не существует")
    return True


def _move_hint(channel_id: int, fields: dict) -> str:
    """Команда CLI владельца для того же переноса — в текст отказа `widens_scope` (агент передаст её владельцу)."""
    if fields.get("map_source") == "auto":
        return f"hub channel move {channel_id} --rules"
    if fields.get("project_id") is None:
        return f"hub channel move {channel_id} --unsorted"
    return f"hub channel move {channel_id} --project {fields['project_id']}"


def _check_bearer_move(conn, principal: Principal, channel_id: int, fields: dict, now_ms: int) -> None:
    """Спека 7a, П4: цель T (явный `project_id` или то, что дадут правила при `map_source: "auto"` —
    вычислено БЕЗ записи тем же резолвером; `auto` в одном запросе с `project_id` побеждает, как в
    `update_channel`) обязана быть видимым проектом, а сам перенос — не открывать канал ни одному
    действующему проектному токену, которому он сейчас не виден. Вызывается в той же транзакции, что и
    запись; источник S — текущий проект канала."""
    source_project_id = queries.channel_visible(conn, principal, channel_id)
    if fields.get("map_source") == "auto":
        target_project_id = mapping.resolve_auto_project(conn, db_of(channel_id), channel_id)
    else:
        target_project_id = fields.get("project_id")
        if target_project_id is not None and not queries.project_visible(conn, principal, target_project_id):
            raise channels_mod.NotFound(f"project_id {target_project_id} не существует")
    gaining = queries.tokens_gaining_project(
        conn, source_project_id=source_project_id, target_project_id=target_project_id, now_ms=now_ms
    )
    if gaining:
        raise WidensScope([t["name"] for t in gaining], _move_hint(channel_id, fields))


@router.patch("/channels/{channel_id}")
async def patch_channel(
    channel_id: int,
    body: ChannelPatchIn,
    request: Request,
    response: Response,
    principal: Principal = _authed,
) -> dict:
    if principal.is_ui and body.model_fields_set & _PATCH_IDENT_FIELDS:
        raise HTTPException(422, "session: только для сессий")
    if not principal.is_ui:
        if not principal.has_scope("organize"):
            await _deny_bearer(request, principal)
        # П9: присутствие ключа (даже `null`) — уже отказ: `muted`/`archived` не про раскладку.
        if body.model_fields_set - _BEARER_PATCH_FIELDS:
            await _deny_bearer(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    fields = body.model_dump(exclude_unset=True, exclude=set(_PATCH_IDENT_FIELDS))
    ident = autopilot_mod.ident(body.session, body.session_key)
    moving = "project_id" in fields or fields.get("map_source") == "auto"
    # Ф34-1 П1: заметка к стадии в аудит не пишется (ни при успехе, ни при отказе).
    audit_fields = {key: value for key, value in fields.items() if key != "stage_note"}

    def _fn(conn):
        queries.channel_visible(conn, principal, channel_id)
        if not principal.is_ui:
            # Ф41-5б П5: перенос сессией хаба в авто регулирует выключатель `move_channel`.
            if moving and _check_auto_move(conn, principal, channel_id, fields, ident):
                audit_fields["autopilot"] = True
            else:
                _check_bearer_move(conn, principal, channel_id, fields, now_ms)
        channels_mod.update_channel(conn, channel_id, now_ms=now_ms, **fields)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="channel_update",
            result="ok",
            channel_id=channel_id,
            detail=audit_fields or None,
        )

    try:
        await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except channels_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except channels_mod.ChannelError as exc:
        raise HTTPException(422, str(exc)) from exc
    except mapping.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except WidensScope as exc:
        # П4/П1: откат унёс только пустую транзакцию — отказ пишется отдельной записью.
        denied_detail = {**audit_fields, "widens_scope_tokens": exc.token_names}
        await db.write(
            lambda conn: write_audit(
                conn,
                now_ms=now_ms,
                actor=principal.kind,
                api_token_id=principal.token_id,
                client_name=client_name,
                request_id=request_id,
                action="channel_update",
                result="denied_widens_scope",
                channel_id=channel_id,
                detail=denied_detail,
            )
        )
        names = ", ".join(exc.token_names)
        target = fields.get("project_id") if fields.get("map_source") != "auto" else None
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"widens_scope: перенос открыл бы канал профилю {names}; попроси владельца: {exc.hint}"
                ),
                # Ф41-5б (5.2): можно подать заявку владельцу (`POST /agent-requests`, вид `move_channel`).
                "request_hint": isinstance(target, int) and target > 0,
            },
            headers={"X-Request-Id": request_id},
        )
    except MoveHeld as exc:
        await db.write(
            lambda conn: write_audit(
                conn,
                now_ms=now_ms,
                actor=principal.kind,
                api_token_id=principal.token_id,
                client_name=client_name,
                request_id=request_id,
                action="channel_update",
                result="denied_autopilot",
                channel_id=channel_id,
                detail=dict(audit_fields),
            )
        )
        return JSONResponse(
            status_code=409,
            content={"detail": MOVE_HELD_TEXT, "request_hint": exc.request_hint},
            headers={"X-Request-Id": request_id},
        )

    return {"ok": True}


class AutosendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=200)
    on: StrictBool


@router.put("/channels/{channel_id}/autosend")
async def put_channel_autosend(
    channel_id: int, body: AutosendIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Ф37-1 A2: список «без подтверждения» беседы ведёт только владелец — UI-сессия (`ui_only`: bearer — 403 с
    аудитом `ui_only_denied`, как у подтверждения черновика). Включить можно беседе в проекте, не в архиве,
    не заготовке `new:…` и не `no_reply` — иначе 422. Аудит `channel_autosend` (беседа, роль, вкл/выкл).
    Ответ — `{"autosend": [роли], "changed": bool}`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        queries.channel_visible(conn, principal, channel_id)
        changed = autosend_mod.set_role(conn, channel_id, role=body.role, on=body.on, now_ms=now_ms)
        roles = autosend_mod.roles(conn, channel_id)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="channel_autosend",
            result="changed" if changed else "unchanged",
            channel_id=channel_id,
            detail={"role": body.role.strip()[:64], "on": body.on},
        )
        return roles, changed

    try:
        roles, changed = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except autosend_mod.AutosendError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

    return {"autosend": roles, "changed": changed}


class ResolveIn(BaseModel):
    query: str
    project: int | None = None


@router.post("/channels/resolve")
async def resolve_channels_route(
    body: ResolveIn, request: Request, principal: Principal = _read_scope
) -> list[dict]:
    db = request.app.state.db
    if len(body.query.strip()) < queries.RESOLVE_MIN_QUERY:
        raise HTTPException(422, "query too short")
    try:
        with db.reader() as conn:
            return queries.resolve_channels(conn, principal, query=body.query, project_id=body.project)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


# --- Ф8-4: удалить у себя в хабе (docs/architecture.md, B2–B3, П1, П12) -----------------


@router.delete("/messages/{message_id}", status_code=204)
async def hide_message_route(message_id: int, request: Request, principal: Principal = ui_only) -> Response:
    """B2: только UI-сессия (bearer любого скоупа — 403 с аудитом `ui_only_denied`; оркестратору — позже
    отдельным скоупом), CSRF — middleware. У источника ничего не удаляется. Своё исходящее в `sending`/
    `unknown` — 409; повтор по уже скрытому — 204 (П12)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)

    def _fn(conn):
        queries.message_visible(conn, principal, message_id, include_hidden=True)
        result = hiding.hide_message(conn, message_id, now_ms=now_ms)
        if result.changed:
            write_audit(
                conn,
                now_ms=now_ms,
                actor=principal.kind,
                api_token_id=principal.token_id,
                client_name=client_name,
                request_id=request_id,
                action="message_hidden",
                result="ok",
                channel_id=result.channel_id,
                detail={"message_id": message_id, "count": result.count},
            )

    try:
        await db.write(_fn)
    except (queries.NotFound, hiding.NotFound) as exc:
        raise HTTPException(404, "not found") from exc
    except hiding.SendInProgress as exc:
        raise HTTPException(409, "дождитесь итога отправки") from exc
    except hiding.OpPending as exc:  # Ф14-8 П1
        raise HTTPException(409, "op_pending") from exc
    return Response(status_code=204, headers={"X-Request-Id": request_id})


@router.delete("/channels/{channel_id}", status_code=204)
async def hide_channel_route(channel_id: int, request: Request, principal: Principal = ui_only) -> Response:
    """B3 + П2: беседа целиком, одной транзакцией. Правила раскладки, ручная привязка и режим внимания
    остаются; беседа вернётся в списки с первым сообщением новее границы.

    Ф12-5 A5 + П3: беседа-заготовка — сначала то же скрытие (очистка текста и файлов, отмена невыданного),
    затем в архив (`hidden`), если живой строки outbox не осталось: место в лимите 100 освобождается."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)

    def _fn(conn):
        queries.channel_visible(conn, principal, channel_id)
        result = hiding.hide_channel(conn, channel_id, now_ms=now_ms)
        archived = draft_channels.archive_hidden(conn, db_of(channel_id), channel_id, now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="channel_hidden",
            result="ok",
            channel_id=channel_id,
            detail={"count": result.count, "archived": True} if archived else {"count": result.count},
        )

    try:
        await db.write(_fn)
    except (queries.NotFound, hiding.NotFound) as exc:
        raise HTTPException(404, "not found") from exc
    except hiding.SendInProgress as exc:
        raise HTTPException(409, "дождитесь итога отправки") from exc
    except hiding.OpPending as exc:  # Ф14-8 П1
        raise HTTPException(409, "op_pending") from exc
    return Response(status_code=204, headers={"X-Request-Id": request_id})


@router.post("/accounts/{account_id}/resume")
async def post_account_resume(
    account_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Ф69-1 D + П6: кнопка «Снять стоп» — только кука интерфейса (bearer, агент владельца, бот — 403). Снимает
    стоп первых сообщений новым людям (`peer_flood_ts`) и `limited` с `last_error='PeerFlood'`; прочие
    статусы и `flood_until` не трогает. 200 `{ok, cleared}`; 404 — нет аккаунта; 409 `nothing_to_resume`.
    Запись будит `notifier` (`db.on_commit`) — коннектор берёт задержанные строки, не дожидаясь своих 30 с."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        cleared = peer_flood_mod.resume_ui(conn, account_id)
        if cleared:
            write_audit(
                conn,
                now_ms=now_ms,
                actor=principal.kind,
                api_token_id=principal.token_id,
                client_name=client_name,
                request_id=request_id,
                action="account_resume",
                result="ok",
                detail={"account_id": account_id, "cleared": cleared},
            )
        return cleared

    cleared = await db.write(_fn)
    if cleared is None:
        raise HTTPException(404, "not found")
    if not cleared:
        raise HTTPException(409, "nothing_to_resume")
    notifier = getattr(request.app.state, "notifier", None)
    if notifier is not None:
        notifier.notify()  # П6: и без `db.on_commit` (одиночное приложение в тестах) — ожидание коннектора
    return {"ok": True, "cleared": cleared}
