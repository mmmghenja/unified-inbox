"""Ручки внутреннего сокета (`/internal/v1`) — коннекторы (секрет `hubc_`) и CLI-админ (секрет `huba_`).
Спецификация: docs/architecture.md. Маршруты не пишут SQL:
запись — через `ingest`/`outbox`/`admin`/`security` внутри `Db.write()`, чтение — через
`outbox_queries`/`queries`.

`account_id` коннектора берётся ТОЛЬКО из `ConnectorPrincipal` (аутентификация), никогда из тела запроса —
все Pydantic-модели ниже `extra="forbid"`, лишнее поле (в т.ч. `account_id`) -> 422.
"""

import asyncio
import contextlib
import logging
import os
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .. import admin as admin_mod
from .. import autosend as autosend_mod
from .. import backup as backup_mod
from .. import card_route as card_route_mod
from .. import channel_wait, conn_sleep, connector_auth, ingest, mapping, outbox_queries, queries, security
from .. import channels as channels_mod
from .. import digest as digest_mod
from .. import handled as handled_mod
from .. import inbound_rules as inbound_rules_mod
from .. import join as join_mod
from .. import media as media_mod
from .. import outbox as outbox_mod
from .. import peer_msgs as peer_msgs_mod
from .. import reactions as reactions_mod
from .. import remote as remote_mod
from .. import snapshot as snapshot_mod
from .. import subs as subs_mod
from .. import transcripts as transcripts_mod
from .. import unsorted as unsorted_mod
from .. import workday as workday_mod
from ..audit import write_audit
from ..connector_auth import ConnectorPrincipal
from ..events import CursorIn
from .read import serve_channel_wait

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

_connector = Depends(connector_auth.connector_dependency())
_admin = Depends(connector_auth.admin_dependency())

_MAX_EVENTS = 500  # тот же порог, что ingest._MAX_BATCH — проверяем ЗДЕСЬ, чтобы отдать 413, а не 422/500
_MAX_WAIT_S = 30.0
# Ф70-1 (ревью, п.5): `Notifier` будит на КАЖДЫЙ коммит базы — сон в одном ожидании пересчитывается не чаще.
_SLEEP_RECHECK_S = 2.0
_DAY_MS = 24 * 60 * 60 * 1000
_ACTIVITY_TTL_MS = 10 * 60 * 1000  # раздел E: backfill-флаг живёт в памяти core, коннектор продлевает сам
_BACKUP_WAIT_POLL_S = 10.0
_BACKUP_WAIT_MAX_S = 30 * 60.0


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


# --- коннектор: события, курсор ----------------------------------------------------------------------------


class EventsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[dict]
    cursor: CursorIn | None = None


@router.post("/events")
async def post_events(body: EventsIn, request: Request, principal: ConnectorPrincipal = _connector) -> dict:
    if len(body.events) > _MAX_EVENTS:
        raise HTTPException(413, "batch too large")
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return ingest.apply_batch(
            conn, account_id=principal.account_id, events=body.events, cursor=body.cursor, now_ms=now_ms
        )

    result = await db.write(_fn)
    return {"applied": result.applied, "noop": result.noop, "dead": result.dead, "max_seq": result.max_seq}


@router.get("/cursor")
async def get_cursor(request: Request, principal: ConnectorPrincipal = _connector) -> dict | None:
    db = request.app.state.db
    with db.reader() as conn:
        return queries.connector_cursor(conn, principal.account_id)


@router.get("/account")
async def get_account(request: Request, principal: ConnectorPrincipal = _connector) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        account = queries.connector_account(conn, principal.account_id)
        # Ф70-1 A.3: аккаунт спит вместе с днём своего проекта — `{project_id, since_ts}` или `None`.
        sleep = conn_sleep.asleep(conn, principal.account_id, now_ms)
    # ConnectorPrincipal нашла аутентификация — строка гарантированно существует (как в set_account_status).
    assert account is not None  # noqa: S101
    return {**account, "sleep": sleep}


def _sleep_now(request: Request, principal: ConnectorPrincipal) -> dict | None:
    with request.app.state.db.reader() as conn:
        return conn_sleep.asleep(conn, principal.account_id, _now_ms(request))


@router.get("/sleep/wait")
async def get_sleep_wait(
    request: Request,
    asleep: bool = Query(False),
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Ф70-1 A.4: long-poll сна аккаунта. `asleep` — что коннектор знает сейчас; ответ `{"sleep": …}` (как
    поле `sleep` у `GET /account`) приходит сразу, если сон уже не такой, иначе — как только станет другим
    (любая запись в базу будит `Notifier`: «Закончить/Начать день», правило раскладки, перенос беседы) или по
    сроку с тем же значением. `timeout_s=0` — просто прочитать."""
    notifier = request.app.state.notifier
    if notifier.loop is None:
        notifier.attach(asyncio.get_running_loop())  # тот же приём, что у `/outbox/wait`
    timeout_s = min(timeout_s, _MAX_WAIT_S)
    gen = notifier.gen  # ДО чтения БД — запись между чтением и ожиданием не потеряется
    current = _sleep_now(request, principal)
    if (current is not None) != asleep or timeout_s <= 0:
        return {"sleep": current}
    loop = asyncio.get_running_loop()
    checked = loop.time()
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout_s):
            while True:
                gen = await notifier.wait(gen, timeout_s)
                if notifier.closed:
                    break  # ядро останавливается — ответ сразу, без ожидания
                pause = _SLEEP_RECHECK_S - (loop.time() - checked)
                if pause > 0:
                    # Пачка коммитов — один пересчёт: записи за паузу покрывает чтение после неё.
                    await asyncio.sleep(pause)
                    if notifier.closed:
                        break
                gen = notifier.gen  # ДО чтения БД, как выше
                current = _sleep_now(request, principal)
                checked = loop.time()
                if (current is not None) != asleep:
                    return {"sleep": current}
    return {"sleep": current}


@router.get("/channels/heads")
async def get_channels_heads(
    request: Request,
    principal: ConnectorPrincipal = _connector,
    since_ts: int | None = Query(None, ge=0),
) -> list[dict]:
    # Ф42-3 B: `since_ts` — границы добора истории (`project_id`, `refill_lo_ext_sort`); без него — как было.
    db = request.app.state.db
    with db.reader() as conn:
        return queries.connector_channel_heads(conn, principal.account_id, since_ts)


_MAX_WINDOW_LIMIT = 200


@router.get("/channels/window")
async def get_channels_window(
    request: Request,
    ext_id: str,
    limit: int = Query(30, ge=1, le=_MAX_WINDOW_LIMIT),
    thread_ext_id: str | None = None,
    principal: ConnectorPrincipal = _connector,
) -> list[str]:
    """Кусок 1b фазы 1, раздел «Сверка»: `ext_id` неудалённых сообщений канала `ext_id` этого аккаунта, по
    убыванию — опора «перечитывания окна» (правки/удаления, пропущенные во сне). `?thread_ext_id=` (раздел A
    спеки 2b) -> окно ОТВЕТОВ этого треда (сверка через `conversations.replies`); без него — только
    `top_level` (Telegram не меняется — у него ответов нет)."""
    db = request.app.state.db
    with db.reader() as conn:
        return queries.connector_channel_window(
            conn, principal.account_id, ext_id, limit, thread_ext_id=thread_ext_id
        )


@router.get("/threads/heads")
async def get_threads_heads(
    request: Request,
    since_ms: int = 0,
    principal: ConnectorPrincipal = _connector,
) -> list[dict]:
    """Раздел H спеки 2b: треды своего аккаунта с `last_reply_ts >= since_ms` или родителем моложе
    `since_ms` (<= 2000, свежие первыми) — опора коннектора 2c для `conversations.replies`."""
    db = request.app.state.db
    with db.reader() as conn:
        return queries.connector_threads_heads(conn, principal.account_id, since_ms)


class ActivityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backfill: bool


def _activity_state(request: Request) -> dict[int, int]:
    """`{account_id: истекает_ts_мс}` в памяти процесса (раздел E) — намеренно НЕ в `app.state` из
    `create_internal_app`/`runner.py` (эти два файла вне объёма куска 1a): атрибут заводится лениво, первым
    же обращением, `request.app` — один и тот же объект на все запросы процесса."""
    state = request.app.state
    if not hasattr(state, "connector_activity"):
        state.connector_activity = {}
    return state.connector_activity


@router.post("/account/activity")
async def post_account_activity(
    body: ActivityIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    activity = _activity_state(request)
    if body.backfill:
        activity[principal.account_id] = _now_ms(request) + _ACTIVITY_TTL_MS
    else:
        activity.pop(principal.account_id, None)
    return {"ok": True}


# --- коннектор: outbox ----------------------------------------------------------------------------------


class LeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = None
    lease_ms: int | None = None


def _lease_to_dict(lease: outbox_mod.Lease) -> dict:
    return {
        "outbox_id": lease.outbox_id,
        "channel_ext_id": lease.channel_ext_id,
        "text": lease.text,
        "reply_to_ext_id": lease.reply_to_ext_id,
        "thread_ext_id": lease.thread_ext_id,
        "send_token": lease.send_token,
        "mode": lease.mode,
        "send_started_ts": lease.send_started_ts,
        "attempts": lease.attempts,
        "lease_gen": lease.lease_gen,
        "maybe_sent": lease.maybe_sent,
        "known_own_ext_ids": lease.known_own_ext_ids,
        # Ф11-3b A4 (контракт A↔B): None или список {size, mime, name, kind, sha256} в порядке вложений.
        "files": lease.files,
        "new_to": lease.new_to,  # Ф12-1 A3: адресат беседы-заготовки, у обычной беседы — None
    }


@router.post("/outbox/lease")
async def post_lease(
    body: LeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    db = request.app.state.db
    now_ms = _now_ms(request)
    kwargs = {}
    if body.limit is not None:
        kwargs["limit"] = body.limit
    if body.lease_ms is not None:
        kwargs["lease_ms"] = body.lease_ms

    def _fn(conn):
        return outbox_mod.lease(
            conn, account_id=principal.account_id, owner=principal.owner, now_ms=now_ms, **kwargs
        )

    leases = await db.write(_fn)
    return [_lease_to_dict(item) for item in leases]


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outbox_id: int
    lease_gen: int
    outcome: str
    ext_id: str | None = None
    error: str | None = None
    retry_after_ms: int | None = None
    # Ф12-1 A4: настоящая беседа черновика новому адресату (Telegram — id пользователя, почта — X-GM-THRID) и
    # её название. Только при `sent` по беседе-заготовке; иначе ядро их игнорирует (с аудитом) — длину и форму
    # проверяет ядро, отчёт из-за них не отклоняется (потерялось бы «отправлено»).
    peer_ext_id: str | None = None
    peer_title: str | None = None
    # Ф63-2 П2: адресат заготовки — бот (Telegram). Тип не проверяется здесь: не `True` — как отсутствие.
    peer_bot: Any = None
    # Ф11-3b A5 (контракт A↔B): альбом Telegram (и `fake`) — `ext_ids` всех N сообщений (`ext_ids[0] ==
    # ext_id`) и `grouped_id`. Тип не проверяется здесь: негодные ядро игнорирует с аудитом, отчёт из-за них
    # не отклоняется никогда (потерялось бы «отправлено»).
    ext_ids: Any = None
    grouped_id: Any = None
    # Ревью Ф34-3: `temporary` + `retry_after_ms` + `postponed` — строка отложена до запроса к источнику:
    # попытка не тратится, аккаунт не останавливается. Старый коннектор поля не шлёт.
    postponed: bool = False
    # Ф40-1 A: время сообщения по часам источника, мс; только при `sent`. Тип не проверяется здесь: негодное
    # ядро игнорирует (строка остаётся со своим временем), отчёт из-за него не отклоняется никогда.
    ts_ms: Any = None


@router.post("/outbox/report")
async def post_report(body: ReportIn, request: Request, principal: ConnectorPrincipal = _connector) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        # report() сам не знает про account_id (принимает только owner/lease_gen) — принадлежность outbox_id
        # аккаунту проверяем здесь, ДО report(), иначе чужой outbox_id ловился бы как StaleLease (409),
        # а спека требует 404 (тест 3 куска 5: "не подтверждаем чужой outbox_id даже статус-кодом").
        owner_account_id = outbox_queries.outbox_account_id(conn, body.outbox_id)
        if owner_account_id is None or owner_account_id != principal.account_id:
            raise outbox_mod.NotFound(f"outbox {body.outbox_id} не найден")
        outbox_mod.report(
            conn,
            outbox_id=body.outbox_id,
            owner=principal.owner,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            ext_id=body.ext_id,
            error=body.error,
            retry_after_ms=body.retry_after_ms,
            now_ms=now_ms,
            peer_ext_id=body.peer_ext_id,
            peer_title=body.peer_title,
            peer_bot=body.peer_bot is True,
            ext_ids=body.ext_ids,
            grouped_id=body.grouped_id,
            postponed=body.postponed,
            ts_ms=body.ts_ms,
        )

    try:
        await db.write(_fn)
    except outbox_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except outbox_mod.StaleLease as exc:
        raise HTTPException(409, "stale lease") from exc
    except outbox_mod.InvalidTransition as exc:
        raise HTTPException(409, "invalid transition") from exc
    except outbox_mod.OutboxError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {"ok": True}


@router.get("/outbox/{outbox_id}/file")
async def get_outbox_file(
    outbox_id: int,
    request: Request,
    lease_gen: int = Query(...),
    idx: int = Query(0),
    principal: ConnectorPrincipal = _connector,
):
    """Ф10-4 A5 + П9: байты файла строки outbox — только держателю живого лиза того же поколения своего
    аккаунта (как `report`: чужой outbox_id не подтверждаем даже статус-кодом); иначе 404. Потоком,
    `Content-Length`, `application/octet-stream`. Ф11-3b A4: `idx` — номер файла в порядке вложений
    (по умолчанию 0), вне списка — 404."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        found = outbox_queries.file_for_lease(
            conn,
            outbox_id=outbox_id,
            account_id=principal.account_id,
            owner=principal.owner,
            lease_gen=lease_gen,
            now_ms=now_ms,
            idx=idx,
        )
    if found is None:
        raise HTTPException(404, "not found")
    account_id, sha256 = found
    path = media_mod.file_path(db.data_dir, account_id, sha256)
    try:
        st = await asyncio.to_thread(os.stat, path)
    except FileNotFoundError:
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        _logger.warning("outbox %s: файла нет на складе", outbox_id)
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="application/octet-stream", stat_result=st)


async def _leasable_now(request: Request, principal: ConnectorPrincipal) -> bool:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        # Раздел C спеки куска 1a: `limited`/`auth_required` -> «нечего выдавать», не трогая
        # `outbox_queries.has_leasable` (вне объёма куска — см. отчёт исполнителя). Строки остаются как есть.
        account = queries.connector_account(conn, principal.account_id)
        if account is not None and account["status"] in ("limited", "auth_required"):
            return False
        return outbox_queries.has_leasable(conn, account_id=principal.account_id, now_ms=now_ms)


@router.get("/outbox/wait")
async def get_wait(
    request: Request,
    timeout_s: float = Query(30.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll: «готово» = `outbox.lease` выдал бы хоть что-то для этого аккаунта, сам лиз не берётся.
    Поколение `Notifier` снимается ДО чтения БД (как в `feed.stream`) — уведомление, случившееся между
    первой проверкой и входом в ожидание, не теряется.
    """
    notifier = request.app.state.notifier
    if notifier.loop is None:
        # lifespan публичного приложения не запускался (ASGITransport в тестах) — привязываем лениво,
        # тот же приём, что в routes/sync.py.
        notifier.attach(asyncio.get_running_loop())

    timeout_s = min(timeout_s, _MAX_WAIT_S)

    gen = notifier.gen
    if await _leasable_now(request, principal):
        return {"ready": True}

    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout_s):
            while True:
                gen = await notifier.wait(gen, timeout_s)
                if notifier.closed:
                    break  # Ф31-1 П5: ядро останавливается — пусто, ничего не выдавая
                if await _leasable_now(request, principal):
                    return {"ready": True}
    return {"ready": False}


# --- коннектор: статус аккаунта --------------------------------------------------------------------------


class AccountStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    last_error: str | None = None


@router.post("/account/status")
async def post_account_status(
    body: AccountStatusIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        admin_mod.set_account_status(
            conn, principal.account_id, status=body.status, last_error=body.last_error, now_ms=now_ms
        )

    try:
        await db.write(_fn)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(422, "invalid status") from exc

    return {"ok": True}


class AccountAlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str
    detail: dict | None = None


# Находка F5(б) критика куска 1c: `detail` пришёл бы напрямую от коннектора (у него нет причин слать
# лишнее, но принцип «не доверяй телу запроса» — белый список, а не bare-merge). `account_id` НЕ в этом
# списке намеренно — он всегда берётся из `principal` (аутентификация), никогда из тела.
_ALERT_DETAIL_ALLOWED_KEYS = frozenset({"device_model", "platform", "country", "date_created", "app_name"})


@router.post("/account/alert")
async def post_account_alert(
    body: AccountAlertIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """Кусок 1c фазы 1, раздел «Алерт о новой сессии Telegram»: `audit(action='connector_alert')` —
    `account_id` в `detail_json` (таблица `audit` не несёт своей колонки на него), устройство/платформа/
    страна/дата из `body.detail` (белый список, находка F5б), БЕЗ IP (правило логов — коннектор его и не
    присылает). `hub-watch` находит свежие записи через `queries.watch_snapshot`."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        detail = {k: v for k, v in (body.detail or {}).items() if k in _ALERT_DETAIL_ALLOWED_KEYS}
        # account_id/text — ПОСЛЕ распаковки белого списка: тело запроса не может подменить их своим ключом
        # (находка F5б: раньше `{"account_id": principal.account_id, **body.detail}` — bare-merge позже по
        # порядку перезаписывал настоящий account_id значением из тела).
        detail["account_id"] = principal.account_id
        detail["text"] = body.text
        write_audit(
            conn,
            now_ms=now_ms,
            actor="connector",
            api_token_id=None,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="connector_alert",
            result=body.kind,
            detail=detail,
        )

    await db.write(_fn)
    return {"ok": True}


# --- CLI-админ --------------------------------------------------------------------------------------------


@router.post("/admin/login-code")
async def post_login_code(request: Request, _: None = _admin) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    code = await db.write(lambda conn: security.create_login_code(conn, now_ms))
    return {"code": code}


class AdminAccountsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    title: str | None = None
    db: str
    config: dict | None = None


@router.post("/admin/accounts")
async def post_admin_accounts(body: AdminAccountsIn, request: Request, _: None = _admin) -> dict:
    db_ = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return admin_mod.create_account(
            conn, source=body.source, title=body.title, db=body.db, config=body.config, now_ms=now_ms
        )

    try:
        created = await db_.write(_fn)
    except admin_mod.ConfigHasSecrets as exc:
        raise HTTPException(422, "config содержит секрет") from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(422, "invalid account") from exc

    return {"account_id": created.account_id, "connector_secret": created.connector_secret}


class AdminTokensIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    scopes: list[str]
    project_ids: list[int] | None = None
    ttl_days: int | None = None


@router.post("/admin/tokens")
async def post_admin_tokens(body: AdminTokensIn, request: Request, _: None = _admin) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    expires_ts = now_ms + body.ttl_days * _DAY_MS if body.ttl_days is not None else None

    def _fn(conn):
        return security.create_api_token(
            conn,
            name=body.name,
            kind=body.kind,
            scopes=body.scopes,
            project_ids=body.project_ids,
            expires_ts=expires_ts,
        )

    try:
        created = await db.write(_fn)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {"token": created.token, "token_id": created.token_id}


@router.get("/admin/tokens")
async def get_admin_tokens(request: Request, _: None = _admin) -> list[dict]:
    """Спека 7a, A6: bearer-токены (`interactive`/`background`) без хешей; UI-сессии не показываются."""
    db = request.app.state.db
    with db.reader() as conn:
        return queries.list_bearer_tokens(conn)


class AdminTokenRevokeIn(BaseModel):
    """Ф92-1 (B3): `replaced_by` — токен перевыпущен (`hub mcp setup`), а не отозван владельцем: новый токен того
    же имени уже выдан. Подписки прежнего кончаются `token_reissued` (беседы вернутся сессии под новым)."""

    model_config = ConfigDict(extra="forbid")

    replaced_by: StrictInt | None = None


@router.post("/admin/tokens/{token_id}/revoke")
async def post_admin_token_revoke(
    token_id: int, request: Request, body: AdminTokenRevokeIn | None = None, _: None = _admin
) -> dict:
    """Спека 7a, A6: UI-сессии этой ручкой не отзываются -> 404, как и несуществующий id. Ф92-1 (B3): с
    `replaced_by` — перевыпуск: прежний ещё не отозван, новый — живой (не отозван, не истёк) bearer-токен с
    тем же именем (иначе 422, ничего не отозвано; ревью: явный отзыв не превращается в перевыпуск); живые
    подписки прежнего завершаются сразу причиной `token_reissued`. Без тела — явный отзыв: подписки снимет
    чистка с причиной `token_revoked` (насовсем)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    replaced_by = body.replaced_by if body is not None else None

    def _fn(conn):
        if replaced_by is not None:
            rows = {
                tid: (name, revoked, expires_ts)
                for tid, name, revoked, expires_ts in conn.execute(
                    "SELECT id, name, revoked, expires_ts FROM main.api_token WHERE id IN (?, ?) "
                    "AND kind IN ('interactive', 'background')",
                    (token_id, replaced_by),
                ).fetchall()
            }
            if token_id not in rows:
                return None
            old, new = rows[token_id], rows.get(replaced_by)
            if (
                replaced_by == token_id
                or old[1]
                or new is None
                or new[1]
                or (new[2] is not None and new[2] <= now_ms)
                or new[0] != old[0]
            ):
                return "replaced_by"
        ok = security.revoke_api_token(conn, token_id)
        if ok:
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=token_id,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="token_revoke",
                result="ok",
                **({"detail": {"replaced_by": replaced_by}} if replaced_by is not None else {}),
            )
            if replaced_by is not None:
                subs_mod.end_reissued_token(conn, token_id, now_ms=now_ms)
        return ok or None

    done = await db.write(_fn)
    if done == "replaced_by":
        raise HTTPException(422, "replaced_by: прежний не отозван, новый — живой токен с тем же именем")
    if not done:
        raise HTTPException(404, "not found")
    return {"ok": True}


class AdminTokenAttachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool


@router.post("/admin/tokens/{token_id}/attach")
async def post_admin_token_attach(
    token_id: int, body: AdminTokenAttachIn, request: Request, _: None = _admin
) -> dict:
    """Ф14-3 A1 + П8: дописать/убрать скоуп `attach` у живого bearer-токена без перевыпуска
    (`hub mcp attach`).
    Нет такого токена — 404; отозван, истёк или запрет (`security.check_attach`: не `interactive`, вместе с
    `orchestrate`/`runs`, дежурная `mcp:wake-*`, оркестратор) — 409 с текстом. Аудит `token_attach` `{on}`."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        scopes = security.set_attach(conn, token_id=token_id, on=body.on, now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="admin",
            api_token_id=token_id,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="token_attach",
            result="ok",
            detail={"on": body.on},
        )
        return scopes

    try:
        scopes = await db.write(_fn)
    except security.AttachError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"ok": True, "scopes": scopes}


class AdminProjectsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    color: str | None = None
    workdir: str | None = None


@router.post("/admin/projects")
async def post_admin_projects(body: AdminProjectsIn, request: Request, _: None = _admin) -> dict:
    db = request.app.state.db

    def _fn(conn):
        return admin_mod.create_project(conn, name=body.name, color=body.color, workdir=body.workdir)

    try:
        project_id = await db.write(_fn)
    except admin_mod.InvalidProject as exc:
        raise HTTPException(422, str(exc)) from exc
    except admin_mod.ProjectNameTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"project_id": project_id}


class AdminProjectPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workdir: str | None = None
    # Ф8-3 A6: политика внимания — слияние `{вид: режим | null}`.
    attention: dict[str, str | None] | None = None
    # Ф13-1 B0: подписки сессий в проекте (`hub project set --subs on|off`); выключение завершает живые.
    subs: bool | None = None
    # Ф15-1 A5/E: реакции сессий (`--session-reactions on|off`), пауза в минутах `A-B` (`--reaction-delay`).
    session_reactions: bool | None = None
    reaction_delay: str | None = None
    # Ф35-2 решение 9: обмен сообщениями агентов в проекте (`--agent-msgs on|off`; нет ключа — включено).
    agent_msgs: bool | None = None
    # Ф17-1 A4/E: вступление сессий в группы Telegram (`--session-join confirm|auto`).
    session_join: Literal["confirm", "auto"] | None = None
    # Ф37-3 A: отметка «разобрано» сессиями проекта (`--session-handled on|off`).
    session_handled: bool | None = None
    # Ф86-1 (C4): сводка дня после сжатия (`--digest on|off`) и слова владельца из соседних сессий
    # (`--siblings on|off`); нет ключа — включено.
    digest_after_compact: bool | None = None
    sibling_says: bool | None = None
    # Ф48-4 п.4: умолчание интервала пачки сообщений агентов у подписок проекта, минуты
    # (`--agent-batch-min 0…60`; 0 — сразу).
    agent_batch_min: StrictInt | None = Field(default=None, ge=0, le=subs_mod.AGENT_BATCH_MAX)
    # Ф52-2 A2: проект слышит «Неразобранное» этих аккаунтов (`--unsorted-accounts 8,5|off`; `[]` — снять).
    unsorted_accounts: list[StrictInt] | None = None
    # Ф64-1: проект «по времени» (`--workday on|off`; выключение открывает день).
    workday: bool | None = None
    # Ф66-1 (A.6): таблица маршрута карточек (`--card-route группа=значение,…`; частичное слияние).
    card_route: dict[str, str] | None = None


@router.get("/admin/projects")
async def get_admin_projects(request: Request, _: None = _admin) -> list[dict]:
    """Ф8-3 A6: `hub project list` — та же выдача, что `GET /api/v1/projects` для UI (с политикой
    внимания)."""
    db = request.app.state.db
    with db.reader() as conn:
        return queries.list_projects(conn, _ADMIN_READ_PRINCIPAL)


@router.get("/admin/sync/busy")
async def get_admin_sync_busy(request: Request, _: None = _admin) -> dict:
    """Ф81-1 (раздел 2, Б3): `hub sync` пропускает проект, пока у его сессии хаба идёт ход (`state ==
    running`) или фоновая задача (`bg.items[].state == running`). Только номера сессий и вид занятости."""
    from .. import held as held_mod
    from .. import hub_sessions as hub_sessions_mod

    db = request.app.state.db
    with db.reader() as conn:
        recs = held_mod.list_sessions(conn)
    out: dict[str, list[str]] = {}
    for rec in recs:
        why = []
        if rec.state == "running":
            why.append("ход")
        bg = hub_sessions_mod.bg_of(rec)
        if bg and any(isinstance(i, dict) and i.get("state") == "running" for i in bg["items"]):
            why.append("фоновая задача")
        if why:
            out.setdefault(str(rec.project_id), []).append(f"сессия #{rec.id}: {', '.join(why)}")
    return {"projects": out}


@router.patch("/admin/projects/{project_id}")
async def patch_admin_project(
    project_id: int, body: AdminProjectPatchIn, request: Request, _: None = _admin
) -> dict:
    """Спека 7c, W2: `hub project set ID --workdir PATH|--no-workdir`. Ядро проверяет только абсолютность пути
    (существование и безопасность — CLI и hub-wake); в аудит — `project_workdir` с id проекта, без пути.

    Ф8-3 A6: `--attention kind=mode …` (слияние; `null` — удалить ключ); аудит `project_attention`
    (id проекта и ключи, без путей). Присутствие ключа
    в теле — признак «менять»: `{"workdir": null}` снимает папку, как раньше."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    given = body.model_fields_set
    if not given:
        raise HTTPException(422, "nothing to change")

    def _fn(conn):
        result: dict = {"project_id": project_id}
        if "workdir" in given:
            if not admin_mod.set_project_workdir(conn, project_id, body.workdir):
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_workdir",
                result="ok",
                detail={"project_id": project_id, "cleared": body.workdir is None},
            )
            result["workdir"] = body.workdir
        if "attention" in given:
            policy = admin_mod.set_project_attention(conn, project_id, changes=body.attention or {})
            if policy is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_attention",
                result="ok",
                detail={"project_id": project_id, "policy": policy},
            )
            result["attention_policy"] = policy
        if "subs" in given:
            if body.subs is None:
                raise ValueError("subs: нужно true или false")
            ended = subs_mod.set_project_subs(conn, project_id, body.subs, now_ms=now_ms)
            if ended is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_subs",
                result="ok",
                detail={"project_id": project_id, "subs": body.subs, "ended": ended},
            )
            result["subs"] = body.subs
        if "session_reactions" in given or "reaction_delay" in given:
            if "session_reactions" in given and body.session_reactions is None:
                raise ValueError("session_reactions: нужно true или false")
            delay_s = None
            if "reaction_delay" in given:
                delay_s = reactions_mod.parse_delay_minutes(body.reaction_delay)
            policy = reactions_mod.set_project_reactions(
                conn, project_id, sessions=body.session_reactions, delay_s=delay_s
            )
            if policy is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_reactions",
                result="ok",
                detail={"project_id": project_id, "policy": policy},
            )
            result["session_reactions"] = policy
        if "agent_msgs" in given:
            if body.agent_msgs is None:
                raise ValueError("agent_msgs: нужно true или false")
            if peer_msgs_mod.set_project_enabled(conn, project_id, body.agent_msgs) is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_agent_msgs",
                result="ok",
                detail={"project_id": project_id, "agent_msgs": body.agent_msgs},
            )
            result["agent_msgs"] = body.agent_msgs
        if "session_join" in given:
            if body.session_join is None:
                raise ValueError("session_join: нужно confirm или auto")
            mode = join_mod.set_project_join(conn, project_id, body.session_join)
            if mode is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_join",
                result="ok",
                detail={"project_id": project_id, "session_join": mode},
            )
            result["session_join"] = mode
        if "session_handled" in given:
            if body.session_handled is None:
                raise ValueError("session_handled: нужно true или false")
            if handled_mod.set_project_enabled(conn, project_id, body.session_handled) is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_session_handled",
                result="ok",
                detail={"project_id": project_id, "session_handled": body.session_handled},
            )
            result["session_handled"] = body.session_handled
        for key in (digest_mod.DIGEST_KEY, digest_mod.SIBLINGS_KEY):
            if key not in given:
                continue
            value = getattr(body, key)
            if value is None:
                raise ValueError(f"{key}: нужно true или false")
            if digest_mod.set_project(conn, project_id, key, value) is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_digest",
                result="ok",
                detail={"project_id": project_id, key: value},
            )
            result[key] = value
        if "agent_batch_min" in given:
            if body.agent_batch_min is None:
                raise ValueError("agent_batch_min: нужно целое 0–60")
            if subs_mod.set_project_agent_batch(conn, project_id, body.agent_batch_min) is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_agent_batch",
                result="ok",
                detail={"project_id": project_id, "agent_batch_min": body.agent_batch_min},
            )
            result["agent_batch_min"] = body.agent_batch_min
        if "unsorted_accounts" in given:
            if body.unsorted_accounts is None:
                raise ValueError("unsorted_accounts: нужен список номеров аккаунтов ([] — снять)")
            heard = unsorted_mod.set_project(conn, project_id, body.unsorted_accounts)
            if heard is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_unsorted_accounts",
                result="ok",
                detail={"project_id": project_id, "unsorted_accounts": heard},
            )
            result["unsorted_accounts"] = heard
        if "workday" in given:
            if body.workday is None:
                raise ValueError("workday: нужно true или false")
            if workday_mod.set_workday(conn, project_id, body.workday, now_ms=now_ms) is None:
                return None
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="project_workday",
                result="ok",
                detail={"project_id": project_id, "workday": body.workday},
            )
            result["workday"] = body.workday
        if "card_route" in given:
            table = card_route_mod.set_project(
                conn,
                project_id,
                body.card_route,
                now_ms=now_ms,
                audit={
                    "actor": "admin",
                    "api_token_id": None,
                    "client_name": None,
                    "request_id": uuid.uuid4().hex,
                },
            )
            if table is None:
                return None
            result["card_route"] = table
        return result

    try:
        result = await db.write(_fn)
    except admin_mod.InvalidWorkdir as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, unsorted_mod.UnsortedError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except card_route_mod.RouteError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    if result is None:
        raise HTTPException(404, "not found")
    return result


@router.post("/admin/accounts/{account_id}/resume")
async def post_admin_account_resume(account_id: int, request: Request, _: None = _admin) -> dict:
    """Раздел C: снимает `limited` (и `flood_until`) — единственный способ, кроме входа заново после
    `auth_required`. `hub account resume ID`."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        ok = admin_mod.resume_account(conn, account_id)
        if ok:
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="account_resume",
                result="ok",
                detail={"account_id": account_id},
            )
        return ok

    ok = await db.write(_fn)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


@router.delete("/admin/accounts/{account_id}")
async def delete_admin_account(account_id: int, request: Request, _: None = _admin) -> dict:
    """`hub account rm ID`: убрать пустой аккаунт (заведён, но ни разу не подключался и без данных). Не
    пустой — 409 с причиной. Файлы секрета и сессии убирает CLI."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        source = admin_mod.delete_account(conn, account_id)
        if source is not None:
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="account_delete",
                result="ok",
                detail={"account_id": account_id, "source": source},
            )
        return source

    try:
        source = await db.write(_fn)
    except admin_mod.AccountNotEmpty as exc:
        raise HTTPException(409, str(exc)) from exc
    if source is None:
        raise HTTPException(404, "not found")
    return {"deleted": True, "source": source}


class AccountConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: dict


@router.patch("/admin/accounts/{account_id}/config")
async def patch_admin_account_config(
    account_id: int, body: AccountConfigIn, request: Request, _: None = _admin
) -> dict:
    """`hub account set ID k=v …`: слияние верхнего уровня `config_json` (null удаляет ключ). В аудит — только
    имена ключей. Коннектор подхватывает конфиг после перезапуска."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        config = admin_mod.update_account_config(conn, account_id, body.set)
        if config is not None:
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="account_config",
                result="ok",
                detail={"account_id": account_id, "keys": sorted(body.set)},
            )
        return config

    try:
        config = await db.write(_fn)
    except admin_mod.ConfigHasSecrets as exc:
        raise HTTPException(422, "config содержит секрет") from exc
    if config is None:
        raise HTTPException(404, "not found")
    return {"config": config}


class MapRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    project_id: int
    # Ф14-1 A5 (П1): ЛИБО готовый `match` (как раньше), ЛИБО `kind`+`value` — условие собирает ядро
    # (`mapping.build_match`, тот же путь, что у UI). Оба сразу или ни одного -> 422.
    match: dict | None = None
    kind: str | None = None
    value: StrictInt | StrictStr | None = None
    # Раздел G спеки 2b: None -> ядро решает (mapping.default_priority) — по умолчанию -100 для {"all"},
    # иначе 0. Явный приоритет уважается как раньше.
    priority: int | None = None


@router.get("/admin/inbound-rules")
async def get_inbound_rules_admin(request: Request, project_id: int, _: None = _admin) -> list[dict]:
    """Ф63-1: `hub rules list --project N` — правила проекта для входящих, только чтение. Нет проекта —
    404."""
    with request.app.state.db.reader() as conn:
        if not inbound_rules_mod.project_exists(conn, project_id):
            raise HTTPException(404, "проекта нет")
        return inbound_rules_mod.list_rules(conn, project_id)


@router.post("/admin/inbound-rules")
async def post_inbound_rule_admin(request: Request, project_id: int, _: None = _admin) -> dict:
    """`hub rules add --project N --json '{...}'` — правило входящих от владельца через админский сокет (в том
    числе «в архив»); поля — `inbound_rules.normalize`, аудит `actor='admin'`."""
    try:
        fields = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "тело: JSON-объект с полями правила") from exc
    if not isinstance(fields, dict):
        raise HTTPException(422, "тело: JSON-объект с полями правила")
    now_ms = _now_ms(request)
    actor = {"kind": "admin", "token_id": None, "client_name": "hub-cli", "request_id": uuid.uuid4().hex}

    def _fn(conn):
        rec = inbound_rules_mod.add(conn, project_id, fields, actor=actor, now_ms=now_ms)
        return inbound_rules_mod.rule_dict(conn, rec)

    try:
        return await request.app.state.db.write(_fn)
    except inbound_rules_mod.RuleError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.delete("/admin/inbound-rules/{rule_id}")
async def delete_inbound_rule_admin(
    request: Request, rule_id: int, project_id: int, _: None = _admin
) -> dict:
    """`hub rules rm --project N ID` — снять правило входящих (любое, и «в архив»)."""
    now_ms = _now_ms(request)
    actor = {"kind": "admin", "token_id": None, "client_name": "hub-cli", "request_id": uuid.uuid4().hex}

    def _fn(conn):
        rec = inbound_rules_mod.remove(conn, project_id, rule_id, actor=actor, now_ms=now_ms)
        return {"id": rec.id, "removed": True}

    try:
        return await request.app.state.db.write(_fn)
    except inbound_rules_mod.RuleError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.get("/admin/map-rules")
async def get_map_rules(request: Request, account_id: int | None = None, _: None = _admin) -> list[dict]:
    db = request.app.state.db
    with db.reader() as conn:
        return mapping.list_rules(conn, account_id)


@router.post("/admin/map-rules")
async def post_map_rules(body: MapRuleIn, request: Request, _: None = _admin) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    has_match = body.match is not None
    has_kind = "kind" in body.model_fields_set or "value" in body.model_fields_set
    if has_match == has_kind:
        raise HTTPException(422, "нужно либо match, либо kind и value")
    if has_kind and body.kind is None:
        raise HTTPException(422, "нужен kind")

    def _fn(conn):
        raw = body.match
        if raw is None:
            raw = mapping.build_match(conn, body.account_id, body.kind, body.value)
        match = mapping.storable_match(conn, body.account_id, raw)
        mapping.validate_match(match)  # до resolved_priority: {"all": false} и подобное -> 422 здесь же
        duplicate = mapping.find_duplicate(conn, body.account_id, match, body.project_id)
        if duplicate is not None:
            raise mapping.DuplicateRule(duplicate)
        resolved_priority = body.priority if body.priority is not None else mapping.default_priority(match)
        rule_id, changed = mapping.add_rule(
            conn,
            account_id=body.account_id,
            project_id=body.project_id,
            match=match,
            priority=body.priority,
            now_ms=now_ms,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="admin",
            api_token_id=None,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="map_rule_add",
            result="ok",
            detail={
                "rule_id": rule_id,
                "account_id": body.account_id,
                "project_id": body.project_id,
                "match": match,
                "priority": resolved_priority,
                "channels_relaid": changed,
            },
        )
        return rule_id, changed, resolved_priority

    try:
        rule_id, changed, resolved_priority = await db.write(_fn)
    except mapping.InvalidMatch as exc:
        raise HTTPException(422, str(exc)) from exc
    except mapping.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except mapping.DuplicateRule as exc:
        raise HTTPException(409, str(exc)) from exc

    return {"rule_id": rule_id, "channels_relaid": changed, "priority": resolved_priority}


@router.delete("/admin/map-rules/{rule_id}")
async def delete_map_rule(rule_id: int, request: Request, _: None = _admin) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        account_id, changed = mapping.remove_rule(conn, rule_id, now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="admin",
            api_token_id=None,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="map_rule_rm",
            result="ok",
            detail={"rule_id": rule_id, "account_id": account_id, "channels_relaid": changed},
        )
        return changed

    try:
        changed = await db.write(_fn)
    except mapping.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc

    return {"ok": True, "channels_relaid": changed}


# Админский принципал для чтения «как UI»: все проекты, псевдо-проекты видны (приём `/admin/status`).
_ADMIN_READ_PRINCIPAL = security.Principal(
    token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None
)


@router.get("/admin/channels/{channel_id}")
async def get_admin_channel(channel_id: int, request: Request, _: None = _admin) -> dict:
    """Спека 7a, П4: `hub channel show ID` — та же выдача, что `GET /api/v1/channels/{id}` (П2)."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.get_channel(conn, _ADMIN_READ_PRINCIPAL, channel_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/admin/channels/{channel_id}/wait")
async def get_admin_channel_wait(
    channel_id: int,
    request: Request,
    after: int | None = Query(None, ge=0, le=2**63 - 1),
    timeout_s: int = Query(60, ge=channel_wait.TIMEOUT_MIN_S, le=channel_wait.TIMEOUT_MAX_S),
    thread: str | None = None,
    skip_mine: bool = True,
    _: None = _admin,
):
    """Ф11-1 A3: `hub channel wait` — то же ожидание, что `GET /api/v1/channels/{id}/wait`, «как UI» (все
    проекты), без предела одновременных ожиданий и без перепроверки сессии (админский ключ — не сессия)."""
    return await serve_channel_wait(
        request,
        _ADMIN_READ_PRINCIPAL,
        channel_id,
        after=after,
        timeout_s=timeout_s,
        thread=thread,
        skip_mine=skip_mine,
        still_valid=None,
    )


class AdminChannelPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int | None = None
    map_source: str | None = None
    # Ф8-3 A6: `hub channel attention ID MODE` (`auto` -> `null`).
    attention: Literal["react", "info"] | None = None
    # Ф11-9c: `hub channel archive ID … [--undo]`; в архив — открытые карточки беседы закрываются.
    archived: StrictBool | None = None


@router.patch("/admin/channels/{channel_id}")
async def patch_admin_channel(
    channel_id: int, body: AdminChannelPatchIn, request: Request, _: None = _admin
) -> dict:
    """Спека 7a, П4: `hub channel move` — перенос канала владельцем через админский сокет. Правило «не расширять
    видимость проектных токенов» здесь НЕ действует (оно для bearer-агента); аудит `actor='admin'`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    fields = body.model_dump(exclude_unset=True)
    if "archived" in fields and fields["archived"] is None:
        raise HTTPException(422, "archived: true или false")

    def _fn(conn):
        closed = channels_mod.update_channel(conn, channel_id, now_ms=now_ms, **fields)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="admin",
            api_token_id=None,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="channel_update",
            result="ok",
            channel_id=channel_id,
            detail=fields or None,
        )
        channel = queries.get_channel(conn, _ADMIN_READ_PRINCIPAL, channel_id)
        if "archived" in fields:
            channel["closed_cards"] = closed
        return channel

    try:
        return await db.write(_fn)
    except channels_mod.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except channels_mod.ChannelError as exc:
        raise HTTPException(422, str(exc)) from exc
    except mapping.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


class AdminAutosendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=200)
    on: StrictBool


@router.put("/admin/channels/{channel_id}/autosend")
async def put_admin_channel_autosend(
    channel_id: int, body: AdminAutosendIn, request: Request, _: None = _admin
) -> dict:
    """Ф37-1 A3: `hub channel autosend ID --role РОЛЬ on|off` — аварийная правка списка «без подтверждения»
    по внутреннему сокету; те же проверки, что у ручки UI (`autosend.set_role`), аудит `actor='admin'`."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        changed = autosend_mod.set_role(conn, channel_id, role=body.role, on=body.on, now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="admin",
            api_token_id=None,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="channel_autosend",
            result="changed" if changed else "unchanged",
            channel_id=channel_id,
            detail={"role": body.role.strip()[:64], "on": body.on},
        )
        return {"id": channel_id, "autosend": autosend_mod.roles(conn, channel_id), "changed": changed}

    try:
        return await db.write(_fn)
    except autosend_mod.AutosendError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.get("/admin/autosend")
async def get_admin_autosend(request: Request, _: None = _admin) -> dict:
    """Ф37-1 A3: `hub channel autosend list` — весь список «без подтверждения» с названиями бесед."""
    db = request.app.state.db
    with db.reader() as conn:
        items = autosend_mod.list_all(conn)
        for item in items:
            try:
                channel = queries.get_channel(conn, _ADMIN_READ_PRINCIPAL, item["channel_id"])
            except queries.NotFound:
                channel = {}
            item["title"] = channel.get("title")
            item["project_id"] = channel.get("project_id")
            item["archived"] = channel.get("archived")
    return {"items": items}


@router.get("/admin/accounts/{account_id}/meta/{key}")
async def get_admin_account_meta(account_id: int, key: str, request: Request, _: None = _admin) -> dict:
    db = request.app.state.db
    with db.reader() as conn:
        value = queries.account_meta_value(conn, account_id, key)
    if value is None:
        raise HTTPException(404, "not found")
    return value


@router.get("/admin/status")
async def get_admin_status(request: Request, _: None = _admin) -> dict:
    """То же самое, что публичный `/status` для UI-сессии: `project_ids=None` -> видно всё (спека)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    admin_principal = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    with db.reader() as conn:
        return queries.get_status(conn, admin_principal, now_ms)


# --- CLI-админ: hub-watch, hub backup ---------------------------------------------------------------------
#
# SQL `/admin/watch` теперь в `queries.watch_snapshot` — закрытый долг (см. «Поправки» кусков 5a/7 и
# раздел E спеки куска 1a).


@router.get("/admin/watch")
async def get_admin_watch(request: Request, _: None = _admin) -> dict:
    """`hub watch` дёргает это ПОСЛЕ `GET /api/v1/healthz` (без авторизации, вне этого роутера) — сюда
    попадают только когда core уже жив. Ни одного текста сообщений; `last_error` — только причина отказа
    входа у аккаунта в `auth_required` (Ф51-1 A4), у прочих статусов его нет (спека, тест 8)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        snapshot = queries.watch_snapshot(conn, now_ms)
        # Ф8-5 П12: объём медиа и свободное место — `hub watch` поднимает `disk_low` ниже 5 ГиБ.
        summary = media_mod.status_summary(conn, db.data_dir)
        snapshot["media"] = {"total_bytes": summary["total_bytes"], "free_bytes": summary["free_bytes"]}
        # Ф10-7, решение 4: неверные пароли входа с телефона за сутки — `hub watch` поднимает `remote_login`.
        snapshot["remote_login_failures_24h"] = remote_mod.failures_24h(conn, now_ms)
        # Ф14-9 П1: пара https удалённого слушателя — `hub watch` поднимает `remote_tls` (ошибка, < 14 дней).
        snapshot["remote_tls"] = _remote_tls_summary(request)
        # Ф14-6 E: очередь расшифровки — `hub watch` поднимает `transcribe_stalled`, `hub transcribe status`.
        snapshot["transcribe"] = transcripts_mod.watch_summary(conn, now_ms)
    return snapshot


def _remote_tls_summary(request: Request) -> dict | None:
    tls = request.app.state.remote.tls
    if tls is None:
        return None
    return {"not_after_ms": tls.not_after_ms, "error": tls.error}


async def _write_backup_audit(request: Request, now_ms: int, *, result: str, detail: dict | None) -> None:
    db = request.app.state.db

    def _fn(conn):
        write_audit(
            conn,
            now_ms=now_ms,
            actor="admin",
            api_token_id=None,
            client_name=None,
            request_id=uuid.uuid4().hex,
            action="backup",
            result=result,
            detail=detail,
        )

    await db.write(_fn)


async def _wait_for_backfill(request: Request) -> int:
    """Раздел E, долг «бэкап под нагрузкой»: ждёт, пока у ВСЕХ аккаунтов `backfill` не снимется, проверяя
    раз в 10 с, но не дольше 30 мин — дальше бэкап идёт всё равно. Возвращает, сколько ждал (`waited_ms`).

    `sleeper` — `request.app.state.backup_wait_sleep`, если он выставлен (тесты подменяют его на функцию,
    которая двигает `FakeClock` вместо реального ожидания); по умолчанию — настоящий `asyncio.sleep`. Тот же
    приём, что `_activity_state` — атрибут `app.state`, не требующий правки `app_internal.py`/`runner.py`.
    """
    sleeper = getattr(request.app.state, "backup_wait_sleep", asyncio.sleep)
    start_ms = _now_ms(request)
    while True:
        now_ms = _now_ms(request)
        if now_ms - start_ms >= _BACKUP_WAIT_MAX_S * 1000:
            return now_ms - start_ms
        activity = _activity_state(request)
        active = any(expires > now_ms for expires in activity.values())
        if not active:
            return now_ms - start_ms
        await sleeper(_BACKUP_WAIT_POLL_S)


@router.post("/admin/backup")
async def post_admin_backup(request: Request, _: None = _admin) -> dict:
    """`config_dir` приходит из `app.state.config_dir` — `runner.py` выставляет его сразу после
    `create_internal_app()` (`app_internal.py` кусок 7 не трогает, см. отчёт исполнителя). Не более одного
    бэкапа одновременно — замок внутри `backup.run_backup_sync` (`BackupBusy`), не флаг здесь: поток
    `to_thread` переживает отмену запроса."""
    db = request.app.state.db
    config_dir = request.app.state.config_dir

    # Ожидание backfill — ДО проверки recipient: ранний 409 обошёл бы уборку открытых копий, а она живёт под
    # замком бэкапа (`run_backup_sync`), и звать её отсюда — гонка с параллельным прогоном. Без recipient
    # запрос просто подождёт; в бою recipient задан.
    waited_ms = await _wait_for_backfill(request)
    now_ms = _now_ms(request)

    try:
        result = await asyncio.to_thread(
            backup_mod.run_backup_sync, db=db, config_dir=config_dir, now_ms=now_ms
        )
    except backup_mod.BackupBusy as exc:
        raise HTTPException(409, "backup already running") from exc
    except backup_mod.RecipientNotConfigured as exc:
        raise HTTPException(409, "backup recipient not configured") from exc
    except backup_mod.AgeNotInstalled as exc:
        raise HTTPException(409, "age not installed") from exc
    except backup_mod.BackupFailed as exc:
        # `reason` — вид сбоя («age failed», «age timeout», имя класса исключения): без путей,
        # без stderr age.
        await _write_backup_audit(
            request, now_ms, result="error", detail={"reason": str(exc), "waited_ms": waited_ms}
        )
        raise HTTPException(500, "backup failed") from exc
    else:
        await _write_backup_audit(
            request,
            now_ms,
            result="ok",
            detail={
                "files": result["files"],
                "kept": result["kept"],
                "removed": result["removed"],
                "waited_ms": waited_ms,
            },
        )
        return result


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dir: StrictStr = Field(min_length=1, max_length=200)


@router.post("/admin/snapshot")
async def post_admin_snapshot(body: SnapshotIn, request: Request, _: None = _admin) -> dict:
    """Ф33-3 (К4, К5): открытый снимок обеих БД и сессий мессенджеров в каталог `dir` в памяти
    (`/dev/shm/hub-restic-*`, создаёт и удаляет `scripts/hub-offsite-backup.sh`). БД копирует ядро — оно же
    единственный писатель. Без ожидания догона: `VACUUM INTO` из отдельного read-only соединения писателя не
    держит. Удача в `audit` не пишется (раз в час — шум), сбой — пишется."""
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    now_ms = _now_ms(request)
    try:
        result = await asyncio.to_thread(
            snapshot_mod.make_snapshot_sync,
            db=db,
            config_dir=config_dir,
            dest=Path(body.dir),
            now_ms=now_ms,
        )
    except snapshot_mod.SnapshotBadDir as exc:
        raise HTTPException(400, str(exc)) from exc
    except snapshot_mod.SnapshotBusy as exc:
        raise HTTPException(409, "snapshot already running") from exc
    except snapshot_mod.SnapshotFailed as exc:
        reason = str(exc)

        def _fn(conn):
            write_audit(
                conn,
                now_ms=now_ms,
                actor="admin",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="snapshot",
                result="error",
                detail={"reason": reason},
            )

        await db.write(_fn)
        raise HTTPException(500, f"snapshot failed: {exc}") from exc
    return result
