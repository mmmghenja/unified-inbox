"""Читающие ручки `/api/v1`. Спека, раздел «Этап 4a — читающие ручки». Маршруты не пишут SQL — весь SQL
(и весь project_scope) в `queries.py`; здесь только парсинг параметров и перевод `queries.NotFound` в 404.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from hub.engine import registry as engine_registry

from .. import chan_subs, channel_wait, queries
from .. import engine as engine_mod
from .. import engine_models as engine_models_mod
from .. import inbound_rules as inbound_rules_mod
from .. import media as media_mod
from .. import mention_names as mention_names_mod
from ..security import Principal, principal_dependency
from .reminders import settle_stale
from .sync import session_checker
from .write import read_or_agent

router = APIRouter(tags=["read"])

_read = Depends(principal_dependency("read"))


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _annotate_keys(conn, principal: Principal, channel_id: int, messages: list[dict]) -> None:
    """Ф25-1 A5/П3: пометка `keys` (совпавшие ключи подписки «лично мне») — только bearer: интерфейс поле
    не читает, а лента UI до 200 сообщений. Ф63-1 П5: поле `rules` (`[{id, title}]` по `rule_hits`) — всем:
    одна выборка правил на страницу."""
    if not principal.is_ui:
        chan_subs.annotate_keys(conn, channel_id, messages)
    inbound_rules_mod.annotate(conn, messages, principal.project_ids)


def _parse_before(before: str | None) -> tuple[int, int, int] | None:
    if before is None:
        return None
    parts = before.split(",")
    if len(parts) != 3:
        raise HTTPException(422, "invalid before cursor")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise HTTPException(422, "invalid before cursor") from exc


@router.get("/healthz")
async def read_healthz(request: Request) -> dict:
    """БЕЗ авторизации — единственная ручка без Depends(principal_dependency)."""
    db = request.app.state.db
    with db.reader() as conn:
        return queries.get_healthz(conn, _now_ms(request))


@router.get("/projects")
async def read_projects(request: Request, principal=read_or_agent) -> list[dict]:
    db = request.app.state.db
    with db.reader() as conn:
        return queries.list_projects(conn, principal)


@router.get("/projects/{project_id}/channels")
async def read_project_channels(
    project_id: int, request: Request, stage: str | None = None, principal=read_or_agent
) -> list[dict]:
    """Ф34-1 A3: `?stage=<стадия>` — только беседы этой стадии (регистр и пробелы по краям не важны),
    `?stage=` (пустая) — только беседы без стадии; без параметра — все."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.list_channels(conn, principal, project_id, stage=stage)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/projects/{project_id}/stages")
async def read_project_stages(project_id: int, request: Request, principal=read_or_agent) -> list[dict]:
    """Ф34-1 A3: стадии бесед проекта с числом бесед — `[{stage, count}]` (архивные не считаются)."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.list_stages(conn, principal, project_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/channels/{channel_id}")
async def read_channel(channel_id: int, request: Request, principal=read_or_agent) -> dict:
    """Спека 7a, П2: один канал — шапка ленты для MCP (`hub_read`/`hub_thread`)."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.get_channel(conn, principal, channel_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/channels/{channel_id}/messages")
async def read_channel_messages(
    channel_id: int,
    request: Request,
    before: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal=read_or_agent,
) -> dict:
    cursor = _parse_before(before)
    db = request.app.state.db
    try:
        with db.reader() as conn:
            # Ф11-1 A4: курсор ожидания — ДО страницы: строка, вставленная между ними, попадёт и в страницу, и
            # в первый `hub_wait` (повтор безвреден), а не потеряется.
            wait_cursor = queries.channel_max_seq(conn, principal, channel_id) if cursor is None else None
            page = queries.list_messages(conn, principal, channel_id, before=cursor, limit=limit)
            _annotate_keys(conn, principal, channel_id, page.messages)
    except queries.NotFound as exc:
        raise HTTPException(404, queries.not_found_detail(exc)) from exc
    result = {"messages": page.messages, "next_before": page.next_before}
    if wait_cursor is not None:
        result["wait_cursor"] = wait_cursor
    return result


@router.get("/messages/{message_id}")
async def read_message(message_id: int, request: Request, principal=read_or_agent) -> dict:
    """Ф10-2 A3: одно сообщение в форме элемента `/channels/{id}/messages` (с `channel_id`) — для
    `hub_message`. Права — как у ленты канала: вне скоупа и несуществующее — 404."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            message = queries.get_message(conn, principal, message_id)
            inbound_rules_mod.annotate(conn, [message], principal.project_ids)  # Ф63-1 П5
            return message
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/messages/{message_id}/edits")
async def read_message_edits(message_id: int, request: Request, principal=_read) -> dict:
    """Ф8-4 C3/П12: прежние версии сообщения от старой к новой (скоуп `read`, `project_scope` через канал;
    удалённое у себя в хабе — 404)."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            edits = queries.message_edits(conn, principal, message_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    return {"message_id": message_id, "edits": edits}


@router.get("/channels/{channel_id}/threads/{thread_ext_id}/messages")
async def read_thread_messages(
    channel_id: int,
    thread_ext_id: str,
    request: Request,
    before: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal=read_or_agent,
) -> dict:
    """Раздел E спеки 2b: ответы треда (включая заготовки и broadcast), тем же курсором, что лента канала;
    родитель — отдельным полем, не внутри страниц. Неизвестный тред при видимом канале — пустая страница."""
    cursor = _parse_before(before)
    db = request.app.state.db
    try:
        with db.reader() as conn:
            wait_cursor = (
                queries.channel_max_seq(conn, principal, channel_id, thread=thread_ext_id)
                if cursor is None
                else None
            )
            page = queries.list_thread_messages(
                conn, principal, channel_id, thread_ext_id, before=cursor, limit=limit
            )
            # П4: тред Slack — сигнал «по ключу» ведёт в `hub_thread`.
            _annotate_keys(
                conn,
                principal,
                channel_id,
                page.messages + ([page.parent] if isinstance(page.parent, dict) else []),
            )
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    result = {"messages": page.messages, "next_before": page.next_before, "parent": page.parent}
    if wait_cursor is not None:
        result["wait_cursor"] = wait_cursor  # Ф11-1 A4
    return result


# --- Ф11-1 (docs/architecture.md): ожидание новых сообщений беседы ----------------------

_SEQ_MAX = 2**63 - 1
WAITS_PER_TOKEN = 8


async def serve_channel_wait(
    request: Request,
    principal: Principal,
    channel_id: int,
    *,
    after: int | None,
    timeout_s: int,
    thread: str | None,
    skip_mine: bool,
    still_valid: Callable[[], bool] | None,
):
    """Общее тело публичной (A2) и админской (A3) ручек ожидания."""
    notifier = request.app.state.notifier
    if notifier.loop is None:
        notifier.attach(asyncio.get_running_loop())  # lifespan не отработал (ASGITransport) — как `/stream`
    clock = asyncio.get_running_loop().time
    started = clock()
    try:
        page = await channel_wait.wait(
            request.app.state.db,
            notifier,
            principal,
            channel_id,
            after=after,
            thread=thread or None,
            skip_mine=skip_mine,
            timeout_s=timeout_s,
            clock=clock,
            still_valid=still_valid,
            is_disconnected=request.is_disconnected,
        )
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except channel_wait.SessionRevoked as exc:
        raise HTTPException(401, "session revoked") from exc
    except channel_wait.ClientGone:
        return Response(status_code=204)  # отвечать некому
    return channel_wait.to_json(page, channel_id=channel_id, waited_ms=int((clock() - started) * 1000))


@router.get("/channels/{channel_id}/wait")
async def wait_channel(
    channel_id: int,
    request: Request,
    after: int | None = Query(None, ge=0, le=_SEQ_MAX),
    timeout_s: int = Query(60, ge=channel_wait.TIMEOUT_MIN_S, le=channel_wait.TIMEOUT_MAX_S),
    thread: str | None = None,
    skip_mine: bool = True,
    principal=_read,
):
    """A2: long-poll по одной беседе (скоуп `read`, `project_scope` — в `poll`); не больше
    `WAITS_PER_TOKEN` одновременных ожиданий на токен, девятое — 429."""
    active: dict[int, int] = getattr(request.app.state, "channel_waits", None)
    if active is None:
        active = request.app.state.channel_waits = {}
    if active.get(principal.token_id, 0) >= WAITS_PER_TOKEN:
        raise HTTPException(429, "слишком много ожиданий")
    active[principal.token_id] = active.get(principal.token_id, 0) + 1
    try:
        result = await serve_channel_wait(
            request,
            principal,
            channel_id,
            after=after,
            timeout_s=timeout_s,
            thread=thread,
            skip_mine=skip_mine,
            still_valid=session_checker(request, principal),
        )
        if isinstance(result, dict) and result.get("messages"):
            with request.app.state.db.reader() as conn:
                _annotate_keys(conn, principal, channel_id, result["messages"])
        return result
    finally:
        left = active.get(principal.token_id, 1) - 1
        if left > 0:
            active[principal.token_id] = left
        else:
            active.pop(principal.token_id, None)


@router.get("/channels/{channel_id}/threads")
async def read_channel_threads(
    channel_id: int,
    request: Request,
    unread: int = 0,
    limit: int = Query(50, ge=1, le=200),
    principal=_read,
) -> list[dict]:
    """Раздел E: треды канала, свежие первыми — нужна UI, когда подписанный тред есть, а родителя в ленте
    нет."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.list_channel_threads(
                conn, principal, channel_id, unread_only=bool(unread), limit=limit
            )
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/inbox")
async def read_inbox(
    request: Request, project: int | None = None, tier: str | None = None, principal=_read
) -> list[dict]:
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.list_inbox(conn, principal, project_id=project, tier=tier)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/search")
async def read_search(
    request: Request, q: str, project: int | None = None, principal=read_or_agent
) -> list[dict]:
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.search(conn, principal, q=q, project_id=project)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/outbox")
async def read_outbox_list(
    request: Request,
    status: str,
    limit: int = Query(100, ge=1, le=500),
    since: int | None = Query(None, ge=0),
    principal=_read,
) -> list[dict]:
    """Ф8-6 A2: `sent` — фильтр по `sent_ts >= since`, остальные — по `created_ts >= since`."""
    if status not in queries.OUTBOX_LIST_STATUSES:
        raise HTTPException(422, "invalid request")
    db = request.app.state.db
    with db.reader() as conn:
        return queries.list_outbox(
            conn, principal, status=status, limit=limit, since=since, now_ms=_now_ms(request)
        )


@router.get("/home/react")
async def read_home_react(request: Request, principal=_read) -> list[dict]:
    """Ф8-6 A3: «Нужна реакция» — беседы, где ждут меня, по всем проектам в скоупе. Ф34-2: и сработавшие
    напоминания (П6: с ответом, принятым с опозданием, перед выдачей закрываются)."""
    await settle_stale(request, principal)
    db = request.app.state.db
    with db.reader() as conn:
        return queries.home_react(conn, principal)


# Ф19-1 E + П6: объявлена ВЫШЕ `/outbox/{outbox_id}` — иначе `feedback` ушёл бы туда как id (422).
@router.get("/outbox/feedback")
async def read_outbox_feedback(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    since: int | None = Query(None, ge=0),
    channel_id: int | None = None,
    principal=_read,
) -> list[dict]:
    """Ф19-1 E: правки (`edited`) и отказы (`rejected`) черновиков сессий владельцем — новые сверху по времени
    решения; `since` — по нему же (мс)."""
    db = request.app.state.db
    with db.reader() as conn:
        return queries.draft_feedback(conn, principal, limit=limit, since=since, channel_id=channel_id)


@router.get("/outbox/{outbox_id}")
async def read_outbox(outbox_id: int, request: Request, principal=_read) -> dict:
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.get_outbox(conn, principal, outbox_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/accounts/{account_id}/mention_people")
async def read_mention_people(
    account_id: int,
    request: Request,
    q: str = Query("", max_length=mention_names_mod.QUERY_MAX),
    channel_id: int | None = None,
    principal=_read,
) -> list[dict]:
    """Ф27-1 B: подсказка `@` в поле ввода беседы Slack — до 8 людей воркспейса `{id, name, handle}`:
    сначала участники беседы `channel_id`, потом справочник. Аккаунт не Slack и не Mattermost — 404."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return mention_names_mod.mention_people(conn, principal, account_id, q=q, channel_id=channel_id)
    except mention_names_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@router.get("/status")
async def read_status(request: Request, principal=_read) -> dict:
    db = request.app.state.db
    with db.reader() as conn:
        result = queries.get_status(conn, principal, _now_ms(request))
        # Ф16-1: движок сессий — в шапке UI без лишнего запроса (чип «Claude Code» / «OpenCode»).
        result["engine"] = engine_mod.get_engine(conn)
        # Ф78-2: модель и усилие переключателя (умолчание подставлено) — для чипа «<движок> · <модель>».
        config_dir = getattr(request.app.state, "config_dir", None)
        picked = engine_mod.get_choice(conn, config_dir=config_dir if isinstance(config_dir, Path) else None)
        result["engine_model"] = picked["model"]
        result["engine_effort"] = picked["effort"]
        # Ф74-1: профили движков сессий хаба — интерфейс берёт отсюда подписи, усилие, подсказки модели.
        # Ф74-5б: живой список моделей движка (хост сообщает `model/list` Codex) — поле `models`.
        result["session_engines"] = engine_registry.status_profiles(engine_models_mod.load_for_ui(conn))
        if principal.is_ui:
            # Ф8-5 A3/П12: объём медиа по аккаунтам, `pending`/`failed`, свободное место — только UI-сессии.
            summary = media_mod.status_summary(conn, db.data_dir)
            result["media"] = {**summary, "line": media_mod.summary_line(summary)}
    return result
