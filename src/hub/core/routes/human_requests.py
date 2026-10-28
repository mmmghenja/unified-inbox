"""Ручки заявок «нужен человек» (Ф54-1, docs/architecture.md;
фактический контракт — раздел 10 спеки).

Кто что может (П2) — проверка в `core/human_requests.py`, здесь только перевод отказов в коды:

| ручка | кука UI | bearer автора | bearer `human` | прочий bearer |
| `POST /human-requests` | 403 | 201 / 200 повтор | 403 | по допуску |
| `GET /human-requests`, `/{id}` | всё | заявки своей линии | по выключателю вида | 403 |
| `GET /{id}/shot` | 200 | 403 | по выключателю вида | 403 |
| `POST /{id}/done|failed|later` | 200 | 403 | по выключателю вида | 403 |
| `POST /{id}/cancel` | 403 | 200 | 403 | 403 |

Все ручки объявляют `human_ok`: токену со скоупом `human` остальные ручки ядра отвечают 403
(`security.principal_dependency`). Маршруты не пишут SQL. Скриншот приходит тем же запросом, что и заявка
(`multipart/form-data`: часть `data` — JSON полей, часть `shot` — файл, П10)."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path as FsPath
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .. import connector_auth
from .. import decisions as decisions_mod
from .. import human_requests as human_mod
from .. import media as media_mod
from .. import owner_queue as zq
from .. import service_bot as service_bot_mod
from ..security import Principal, human_only, principal_dependency
from .write import SendSessionIn, _client_name, _deny_bearer, _new_request_id, _now_ms, touch_activity

_logger = logging.getLogger(__name__)

public_router = APIRouter(tags=["human-requests"])
internal_router = APIRouter(tags=["human-requests-internal"])
_admin = Depends(connector_auth.admin_dependency())

_any = Depends(principal_dependency(human_ok=True))

_ID_MAX = 2**53
_HEAD_BYTES = 64
# Часть `data` (JSON полей) и служебные строки multipart — с запасом; всё тело — скриншот плюс это.
_DATA_MAX_BYTES = 64 * 1024
_BODY_MAX_BYTES = human_mod.SHOT_MAX_BYTES + _DATA_MAX_BYTES
_SHOT_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"
_SHOT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _SHOT_CSP,
    "Content-Disposition": "inline",
    "Cache-Control": "private, no-store",
}
_TOO_BIG = "скриншот больше 5 МиБ"


def _http(exc: human_mod.HumanError) -> HTTPException:
    status = {
        human_mod.Invalid: 422,
        human_mod.Denied: 403,
        human_mod.NotFound: 404,
        human_mod.Conflict: 409,
        human_mod.Limited: 429,
    }.get(type(exc), 422)
    return HTTPException(status, str(exc))


class HumanIn(BaseModel):
    """Поля заявки — JSON в части `data`. Пределы длины — как в таблице (раздел 2 спеки)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["captcha", "login", "code", "payment", "consent", "other"]
    need: StrictStr = Field(min_length=1, max_length=human_mod.NEED_MAX)
    site: StrictStr = Field(default="", max_length=human_mod.SITE_MAX)
    url: StrictStr = Field(default="", max_length=human_mod.URL_MAX)
    doing: StrictStr = Field(default="", max_length=human_mod.DOING_MAX)
    profile: StrictStr = Field(default="", max_length=human_mod.PROFILE_MAX)
    tab: StrictStr = Field(default="", max_length=human_mod.TAB_MAX)
    # 1…1440 минут; 0 — без срока (снимается через сутки).
    ttl_min: StrictInt = Field(default=human_mod.TTL_MIN_DEFAULT, ge=0, le=human_mod.TTL_MIN_MAX)
    # П7: проект — когда у сессии подписки на несколько проектов.
    project_id: StrictInt | None = Field(default=None, ge=1, le=2**31 - 1)
    session: SendSessionIn
    # Ключ запуска сессии хаба: MCP кладёт его во все запросы с `session`; здесь не используется и не
    # сохраняется (режим сессии для заявки не важен).
    session_key: StrictStr | None = Field(default=None, max_length=128)


class _BodyTooLarge(Exception):
    pass


async def _limited(request: Request):
    """Тело запроса потоком с пределом (П10): лишнее не читается и на диск не ложится."""
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _BODY_MAX_BYTES:
            raise _BodyTooLarge
        yield chunk


async def _parse(request: Request) -> tuple[HumanIn, bytes | None]:
    """Разобрать `multipart/form-data`: `(поля, байты скриншота или None)`. 413 — тело или скриншот больше
    предела; 422 — не multipart, нет части `data`, кривой JSON или поля, пустой скриншот."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(422, "тело — multipart/form-data: часть data (JSON полей) и часть shot (файл)")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _BODY_MAX_BYTES:
        raise HTTPException(413, _TOO_BIG)
    parser = MultiPartParser(
        request.headers, _limited(request), max_files=1, max_fields=1, max_part_size=_DATA_MAX_BYTES
    )
    try:
        form = await parser.parse()
    except _BodyTooLarge as exc:
        raise HTTPException(413, _TOO_BIG) from exc
    except MultiPartException as exc:
        raise HTTPException(422, f"multipart: {exc.message}") from exc
    except ClientDisconnect as exc:
        raise HTTPException(400, "client disconnected") from exc
    try:
        raw = form.get("data")
        if not isinstance(raw, str):
            raise HTTPException(422, "data: нужна часть data — JSON полей заявки")
        try:
            body = HumanIn.model_validate(json.loads(raw))
        except ValueError as exc:  # и `json.JSONDecodeError`, и `pydantic.ValidationError`
            detail = exc.errors(include_url=False, include_input=False, include_context=False) if isinstance(
                exc, ValidationError
            ) else "data: не JSON"  # fmt: skip
            raise HTTPException(422, detail) from exc
        upload = form.get("shot")
        if upload is None:
            return body, None
        if isinstance(upload, str):
            raise HTTPException(422, "shot: нужен файл")
        data = await upload.read(human_mod.SHOT_MAX_BYTES + 1)
        if len(data) > human_mod.SHOT_MAX_BYTES:
            raise HTTPException(413, _TOO_BIG)
        if not data:
            raise HTTPException(422, "shot: пустой файл")
        return body, data
    finally:
        await form.close()


def _write_tmp(path: FsPath, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _precheck_author(principal: Principal) -> None:
    """Отказы, известные до чтения тела: кука, ответчик-агент, bearer без `draft`."""
    if principal.is_ui or human_only(principal):
        raise HTTPException(403, "forbidden")
    if not (principal.has_scope("draft") or principal.has_scope("send")):
        raise HTTPException(403, "forbidden")


@public_router.post("/human-requests")
async def post_human_request(request: Request, response: Response, principal: Principal = _any) -> dict:
    """Завести заявку «нужен человек» — bearer подписанной сессии проекта с ролью. Тело —
    `multipart/form-data`: часть `data` — JSON (`HumanIn`), часть `shot` — скриншот (необязательна; PNG, JPEG
    или WebP по сигнатуре байтов, 1 байт … 5 МиБ). 201 — новая заявка, 200 — уже открытая заявка той же роли,
    вида и адреса (`created: false`). Ответ `{request, created, mail_rule}`: `mail_rule` — Ф95-1, правила
    почты по домену регистрации, заведённые этой заявкой (`{domain, project_id, project_name, rules:
    [{rule_id, account_id}]}`), или `null`.

    403 — кука, токен `human`, bearer без `draft`, дежурная, оркестратор; 409 — нет подписки на проект, не
    назван проект, подписка без роли; 413 — скриншот больше 5 МиБ; 422 — форма, не картинка; 429 — пределы."""
    _precheck_author(principal)
    body, data = await _parse(request)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    shot = None
    tmp = None
    if data is not None:
        detected = media_mod.detect(data[:_HEAD_BYTES])
        if detected not in human_mod.SHOT_TYPES:
            raise HTTPException(422, "shot: скриншот — только PNG, JPEG или WebP")
        if not media_mod.disk_ok(db.data_dir):
            _logger.warning("заявки «нужен человек»: мало места на диске")
            raise HTTPException(507, "insufficient storage")
        tmp = human_mod.tmp_path(db.data_dir)
        await asyncio.to_thread(_write_tmp, tmp, data)
        shot = {
            "tmp": tmp,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mime": human_mod.SHOT_TYPES[detected],
        }

    def _fn(conn):
        made = human_mod.create(
            conn,
            principal=principal,
            session=body.session,
            project_id=body.project_id,
            kind=body.kind,
            need=body.need,
            site=body.site,
            url=body.url,
            doing=body.doing,
            profile=body.profile,
            tab=body.tab,
            ttl_min=body.ttl_min,
            shot=shot,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
        )
        return made.created, human_mod.out_dict(conn, principal, made.human), made.mail_rule

    try:
        created, item, mail_rule = await db.write(_fn)
    except human_mod.HumanError as exc:
        raise _http(exc) from exc
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)  # принят — уже переименован; отказ и повтор — убрать
    response.status_code = 201 if created else 200
    return {"request": item, "created": created, "mail_rule": mail_rule}


class _Triple(BaseModel):
    """Тройка сессии из строки запроса — той же формой и проверкой, что в теле (`SendSessionIn`)."""

    pid: int
    proc_start: int
    session_id: str


def _query_session(pid: int | None, proc_start: int | None, session_id: str | None) -> SendSessionIn | None:
    if pid is None or proc_start is None or session_id is None:
        return None
    try:
        return SendSessionIn(pid=pid, proc_start=proc_start, session_id=session_id)
    except ValidationError as exc:
        raise HTTPException(422, "session: неверная тройка сессии") from exc


async def _sweep_if_due(request: Request, now_ms: int) -> None:
    """П1: чтение само снимает просроченное — опрос видит правду и между тиками минутного цикла."""
    db = request.app.state.db
    with db.reader() as conn:
        due = human_mod.has_due(conn, now_ms)
    if due:
        await db.write(lambda conn: human_mod.sweep(conn, now_ms))


@public_router.get("/human-requests")
async def get_human_requests(
    request: Request,
    status: Literal["pending", "done", "failed", "later", "expired", "cancelled"] | None = Query(None),
    project_id: int | None = Query(None, ge=1, le=2**31 - 1),
    limit: int = Query(50, ge=1, le=human_mod.LIST_MAX),
    pid: int | None = Query(None, ge=-(2**31 - 1), le=2**31 - 1),
    proc_start: int | None = Query(None, ge=0, le=2**62),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    principal: Principal = _any,
) -> list[dict]:
    """Владельцу — все заявки; ответчику-агенту (`human`) — заявки видов, чей выключатель снят для их проекта, в
    проектах токена; сессии (`pid`, `proc_start`, `session_id` в запросе) — заявки своей линии; прочему
    bearer — 403. По умолчанию — ждущие и закрытые за сутки, новые первыми."""
    now_ms = _now_ms(request)
    session = _query_session(pid, proc_start, session_id)
    await _sweep_if_due(request, now_ms)
    try:
        with request.app.state.db.reader() as conn:
            return human_mod.list_requests(
                conn,
                principal,
                now_ms=now_ms,
                session=session,
                project_id=project_id,
                status=status,
                limit=limit,
            )
    except human_mod.HumanError as exc:
        raise _http(exc) from exc


@public_router.get("/human-requests/changes")
async def get_human_changes(
    request: Request,
    since: int = Query(0, ge=0, le=2**62),
    wait: float = Query(0, ge=0, le=human_mod.CHANGES_WAIT_MAX_S),
    principal: Principal = _any,
):
    """Ф54-2 (П6): длинный опрос изменений заявок — ТОЛЬКО боту владельца (`human_bot`); кука, ответчик `human` и
    прочий bearer — 403. Ответ `{"items": [...], "seq": N}`: заявки с `since < seq` по возрастанию `seq` (не
    больше 100, поля — как у `GET /human-requests/{id}`, с `decided_by`); `seq` — отметка для следующего
    запроса. Изменений нет — ждёт до `wait` секунд (0…25) и отвечает пустым списком с текущим `seq`."""
    if not human_mod.is_bot(principal):
        raise HTTPException(403, "forbidden")
    app = request.app
    db = app.state.db
    notifier = app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)  # lifespan не отработал (ASGITransport) — как `/stream`
    deadline = loop.time() + wait
    while True:
        gen = notifier.gen  # ДО чтения БД — notify() между чтением и wait() не потеряется
        await _sweep_if_due(request, _now_ms(request))
        with db.reader() as conn:
            ready = human_mod.bot_has_changes(conn, since=since)
            remaining = deadline - loop.time()
            if ready or remaining <= 0 or notifier.closed:
                return human_mod.bot_changes(conn, principal, since=since, now_ms=_now_ms(request))
        await notifier.wait(gen, remaining)
        if await request.is_disconnected():
            return Response(status_code=204)  # отвечать некому


@public_router.get("/human-requests/{human_id}")
async def get_human_request(
    request: Request,
    human_id: int = Path(ge=1, le=_ID_MAX),
    pid: int | None = Query(None, ge=-(2**31 - 1), le=2**31 - 1),
    proc_start: int | None = Query(None, ge=0, le=2**62),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    principal: Principal = _any,
) -> dict:
    """Одна заявка; невидимая и несуществующая — 404; bearer без подписки с ролью — 403."""
    now_ms = _now_ms(request)
    session = _query_session(pid, proc_start, session_id)
    await _sweep_if_due(request, now_ms)
    try:
        with request.app.state.db.reader() as conn:
            req = human_mod.read_one(conn, principal, human_id, session, now_ms=now_ms)
            return human_mod.out_dict(conn, principal, req)
    except human_mod.HumanError as exc:
        raise _http(exc) from exc


@public_router.get("/human-requests/{human_id}/shot")
async def get_human_shot(
    request: Request, human_id: int = Path(ge=1, le=_ID_MAX), principal: Principal = _any
):
    """Скриншот заявки — владельцу (кука) и ответчику-агенту (по выключателю вида); прочему bearer, в том числе
    автору, — 403. Тип — тот, что определён по сигнатуре при приёме; `nosniff`, `inline`, CSP как у медиа."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            path, mime, _size = human_mod.shot_file(conn, principal, human_id, now_ms=_now_ms(request))
    except human_mod.HumanError as exc:
        raise _http(exc) from exc
    try:
        st = await asyncio.to_thread(os.stat, path)
    except OSError as exc:
        raise HTTPException(404, "not found") from exc
    return FileResponse(path, media_type=mime, headers=_SHOT_HEADERS, stat_result=st)


class DecideIn(BaseModel):
    """Ф93-2 (A): необязательное тело «Готово» / «Не вышло» / «Не сейчас» — причина исхода."""

    model_config = ConfigDict(extra="forbid")

    note: StrictStr | None = Field(default=None, max_length=human_mod.NOTE_MAX)


async def _decide(
    request: Request,
    response: Response,
    principal: Principal,
    human_id: int,
    outcome: str,
    body: DecideIn | None = None,
) -> dict:
    if not principal.is_ui and not human_only(principal):
        await _deny_bearer(request, principal)  # аудит `ui_only_denied` и 403
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    # Срок снимается своей записью: отказ «уже закрыта» откатил бы снятие, сделанное внутри решения.
    await _sweep_if_due(request, now_ms)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        if human_mod.sees_source(principal):  # владелец (кука) и его бот; агент `human` отвечает как раньше
            current = human_mod.get(conn, human_id)
            if current is not None and current.status == "pending":
                zq.take_from_agent(conn, f"human:{human_id}", now_ms=now_ms)  # Ф65-1 Ж1
        req = human_mod.decide(
            conn,
            human_id,
            outcome,
            principal=principal,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            note=body.note if body is not None else None,
        )
        by = {"ui": decisions_mod.BY_UI, "bot": decisions_mod.BY_BOT}.get(
            req.decided_by, decisions_mod.BY_AGENT
        )
        decisions_mod.record(conn, f"human:{human_id}", by, outcome, now_ms=now_ms)  # Ф76-1 (П5, П6)
        return human_mod.out_dict(conn, principal, req)

    try:
        return await db.write(_fn)
    except human_mod.HumanError as exc:
        raise _http(exc) from exc


@public_router.post("/human-requests/{human_id}/done")
async def post_human_done(
    request: Request,
    response: Response,
    human_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _any,
    body: DecideIn | None = None,
) -> dict:
    """«Готово» — владелец (кука, и с телефона) или ответчик-агент (вид под снятым выключателем, иначе 409 «ждёт
    владельца»). Закрытая заявка — 409; прочий bearer — 403."""
    return await _decide(request, response, principal, human_id, "done", body)


@public_router.post("/human-requests/{human_id}/failed")
async def post_human_failed(
    request: Request,
    response: Response,
    human_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _any,
    body: DecideIn | None = None,
) -> dict:
    """«Не вышло» — те же права и отказы, что у «Готово»."""
    return await _decide(request, response, principal, human_id, "failed", body)


@public_router.post("/human-requests/{human_id}/later")
async def post_human_later(
    request: Request,
    response: Response,
    human_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _any,
    body: DecideIn | None = None,
) -> dict:
    """«Не сейчас» — те же права и отказы, что у «Готово»."""
    return await _decide(request, response, principal, human_id, "later", body)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SendSessionIn
    session_key: StrictStr | None = Field(default=None, max_length=128)


@public_router.post("/human-requests/{human_id}/cancel")
async def post_human_cancel(
    body: CancelIn,
    request: Request,
    response: Response,
    human_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _any,
) -> dict:
    """Автор снимает заявку (препятствие исчезло само) — сессия с живой подпиской той же роли проекта.
    Сигнала автору нет. Кука, токен `human`, bearer без подписки с ролью — 403; заявка другой роли — 404;
    закрытая — 409."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    await _sweep_if_due(request, now_ms)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        req = human_mod.cancel(
            conn,
            human_id,
            principal=principal,
            session=body.session,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
        )
        return human_mod.out_dict(conn, principal, req)

    try:
        return await db.write(_fn)
    except human_mod.HumanError as exc:
        raise _http(exc) from exc


# --- бот владельца в Telegram: id бота для границы приёма (Ф54-2, П2) -------------------------------------------


class HumanBotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # id бота у Telegram (число из `getMe`). Добавляется в множество ботов хаба; прежние не забываются.
    tg_id: StrictInt = Field(ge=1, le=2**62)
    # Привязка подтверждена владельцем — существующую личку с ботом убрать в архив.
    archive: StrictBool = False


@internal_router.get("/admin/human-bot")
async def get_admin_human_bot(request: Request, _: None = _admin) -> dict:
    with request.app.state.db.reader() as conn:
        known = service_bot_mod.bot_id_list(conn)
        return {"tg_id": known[-1] if known else None, "tg_ids": known}


@internal_router.post("/admin/human-bot")
async def post_admin_human_bot(body: HumanBotIn, request: Request, _: None = _admin) -> dict:
    """`hub auth human-bot`: записать id бота хаба. С этой записи личка аккаунтов Telegram с этим ботом на
    приёме отбрасывается, а уже заведённая закрыта bearer-токенам (сообщения не стираются); с
    `archive=true` (привязка подтверждена) она уходит в архив. Прежние id ботов остаются в множестве.
    Ответ `{tg_id, tg_ids, closed}`."""
    now_ms = _now_ms(request)
    try:
        return await request.app.state.db.write(
            lambda conn: service_bot_mod.set_bot_id(
                conn, body.tg_id, now_ms=now_ms, archive=body.archive
            )
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
