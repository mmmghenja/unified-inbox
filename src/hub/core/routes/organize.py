"""Раскладка и проекты из интерфейса (Ф14-1, docs/architecture.md): правила раскладки с
предпросмотром, варианты условий аккаунта, создание и правка проекта, перенос пачки бесед.

Все ручки — ТОЛЬКО UI-сессия (решение 6): bearer любого скоупа -> 403 с аудитом `ui_only_denied`; CSRF —
middleware. Маршруты не пишут SQL: чтение и предпросмотр — `mapping` через `db.reader()`, запись —
`mapping`/`admin`/`channels` внутри одной `db.write()` вместе с аудитом.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from .. import admin as admin_mod
from .. import card_route as card_route_mod
from .. import channels as channels_mod
from .. import digest as digest_mod
from .. import engine_fallback as engine_fallback_mod
from .. import handled as handled_mod
from .. import join as join_mod
from .. import mapping, queries
from .. import subs as subs_mod
from .. import unsorted as unsorted_mod
from .. import workday as workday_mod
from ..audit import write_audit
from ..hub_sessions import SessionError
from ..security import Principal
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["organize"])

_MOVE_MAX = 200


def _audit(conn, request: Request, principal: Principal, request_id: str, now_ms: int, action: str, detail):
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=_client_name(request),
        request_id=request_id,
        action=action,
        result="ok",
        detail=detail,
    )


# --- правила ------------------------------------------------------------------------------------------------


@public_router.get("/map-rules")
async def get_map_rules(request: Request, principal: Principal = ui_only) -> list[dict]:
    with request.app.state.db.reader() as conn:
        return mapping.rules_for_ui(conn)


class MapRuleAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: StrictInt
    project_id: StrictInt
    kind: StrictStr
    value: StrictInt | StrictStr | None = None
    priority: StrictInt | None = None
    dry_run: bool = False


def _check_rule_target(conn, body: MapRuleAddIn) -> tuple[dict, dict]:
    """Общая часть предпросмотра и записи — `mapping.check_rule_target` (Ф38-2: та же связка у заявок сессий
    на правило). Возвращает `(match, storable)`: первое — для описания, второе — то, что ляжет в `main`."""
    return mapping.check_rule_target(conn, body.account_id, body.kind, body.value, body.project_id)


@public_router.post("/map-rules")
async def post_map_rule(
    body: MapRuleAddIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    try:
        if body.dry_run:
            with db.reader() as conn:
                match, storable = _check_rule_target(conn, body)
                preview = mapping.preview(
                    conn, body.account_id, add=(storable, body.project_id, body.priority)
                )
                description = mapping.describe_rule(conn, body.account_id, match)[2]
            return {"dry_run": True, "description": description, **preview}

        request_id = _new_request_id(response)

        def _fn(conn):
            match, storable = _check_rule_target(conn, body)
            priority = body.priority if body.priority is not None else mapping.default_priority(storable)
            rule_id, changed = mapping.add_rule(
                conn,
                account_id=body.account_id,
                project_id=body.project_id,
                match=storable,
                priority=priority,
                now_ms=now_ms,
            )
            _audit(
                conn,
                request,
                principal,
                request_id,
                now_ms,
                "map_rule_add",
                {
                    "rule_id": rule_id,
                    "account_id": body.account_id,
                    "project_id": body.project_id,
                    "match": storable,
                    "priority": priority,
                    "channels_relaid": changed,
                },
            )
            description = mapping.describe_rule(conn, body.account_id, match)[2]
            return {
                "rule_id": rule_id,
                "channels_relaid": changed,
                "priority": priority,
                "description": description,
            }

        return await db.write(_fn)
    except mapping.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except mapping.InvalidMatch as exc:
        raise HTTPException(422, str(exc)) from exc
    except mapping.DuplicateRule as exc:
        raise HTTPException(409, str(exc)) from exc


@public_router.delete("/map-rules/{rule_id}")
async def delete_map_rule(
    rule_id: int, request: Request, response: Response, dry_run: bool = False, principal: Principal = ui_only
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    try:
        if dry_run:
            with db.reader() as conn:
                account_id = mapping.rule_account(conn, rule_id)
                description = mapping.rule_description(conn, rule_id)
                preview = mapping.preview(conn, account_id, remove=rule_id)
            return {"dry_run": True, "description": description, **preview}

        request_id = _new_request_id(response)

        def _fn(conn):
            account_id, changed = mapping.remove_rule(conn, rule_id, now_ms)
            _audit(
                conn,
                request,
                principal,
                request_id,
                now_ms,
                "map_rule_rm",
                {"rule_id": rule_id, "account_id": account_id, "channels_relaid": changed},
            )
            return changed

        changed = await db.write(_fn)
    except mapping.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "channels_relaid": changed}


@public_router.get("/accounts/{account_id}/map-options")
async def get_map_options(account_id: int, request: Request, principal: Principal = ui_only) -> dict:
    try:
        with request.app.state.db.reader() as conn:
            return mapping.map_options(conn, account_id)
    except mapping.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# --- проекты ------------------------------------------------------------------------------------------------


class ProjectAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    color: StrictStr | None = None


class ProjectPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr | None = None
    color: StrictStr | None = None
    # Ф17-1 A4: вступление сессий в группы Telegram — `confirm` (через владельца) или `auto` (сразу, до 5 в день).
    session_join: Literal["confirm", "auto"] | None = None
    # Ф37-3 A: отметка «разобрано» сессиями проекта (`hub_handled`) — включает владелец; по умолчанию выключено.
    session_handled: StrictBool | None = None
    # Ф86-1 (C4): сводка дня после сжатия и слова владельца из соседних сессий — по умолчанию оба включены.
    digest_after_compact: StrictBool | None = None
    sibling_says: StrictBool | None = None
    # Ф48-4 п.4: умолчание интервала пачки сообщений агентов у подписок проекта, минуты (0 — сразу).
    agent_batch_min: StrictInt | None = Field(default=None, ge=0, le=subs_mod.AGENT_BATCH_MAX)
    # Ф52-2 A2: проект слышит «Неразобранное» этих аккаунтов (сигнал подпискам проекта); `[]` — снять.
    unsorted_accounts: list[StrictInt] | None = None
    # Ф64-1: проект «по времени» — рабочий день кнопками (выключение открывает день).
    workday: StrictBool | None = None
    # Ф66-1 (A.6): кто отвечает на карточки проекта — `{группа: me|agent|auto}` (частичное слияние;
    # неизвестная группа, кривое значение, `auto` у группы без «сразу да» — 422). С телефона тоже.
    card_route: dict[StrictStr, StrictStr] | None = None
    # Ф83-1: запасной движок сессий хаба проекта по умолчанию `{engine, model?, effort?}`; `null` или
    # `{"engine": null}` — снять. С телефона тоже.
    session_fallback: dict | None = None


@public_router.post("/projects")
async def post_project(
    body: ProjectAddIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Решение 4: из UI — только название и цвет; рабочая папка, политика и подписки — CLI."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        project_id = admin_mod.create_project(conn, name=body.name, color=body.color, workdir=None)
        name = conn.execute("SELECT name FROM main.project WHERE id = ?", (project_id,)).fetchone()[0]
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "project_add",
            {"project_id": project_id, "name": name},
        )
        return project_id

    try:
        project_id = await db.write(_fn)
    except admin_mod.InvalidProject as exc:
        raise HTTPException(422, str(exc)) from exc
    except admin_mod.ProjectNameTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"project_id": project_id}


class ProjectOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[StrictInt]


@public_router.put("/projects/order")
async def put_projects_order(
    body: ProjectOrderIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Ф99-8: очерёдность проектов — `project.sort` = место в списке; только кука интерфейса."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        admin_mod.set_project_order(conn, body.ids)
        _audit(conn, request, principal, request_id, now_ms, "project_order", {"ids": body.ids})

    try:
        await db.write(_fn)
    except admin_mod.InvalidProject as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


@public_router.patch("/projects/{project_id}")
async def patch_project(
    project_id: int,
    body: ProjectPatchIn,
    request: Request,
    response: Response,
    principal: Principal = ui_only,
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    changes = body.model_dump(exclude_unset=True)
    if project_id <= 0:
        raise HTTPException(404, "not found")
    if not changes:
        raise HTTPException(422, "nothing to change")
    request_id = _new_request_id(response)

    if "session_join" in changes and changes["session_join"] is None:
        raise HTTPException(422, "session_join: нужно confirm или auto")
    join_mode = changes.pop("session_join", None)
    if "session_handled" in changes and changes["session_handled"] is None:
        raise HTTPException(422, "session_handled: нужно true или false")
    handled_on = changes.pop("session_handled", None)
    digest_flags: dict[str, bool] = {}
    for key in (digest_mod.DIGEST_KEY, digest_mod.SIBLINGS_KEY):
        if key in changes and changes[key] is None:
            raise HTTPException(422, f"{key}: нужно true или false")
        if key in changes:
            digest_flags[key] = changes.pop(key)
    if "agent_batch_min" in changes and changes["agent_batch_min"] is None:
        raise HTTPException(422, "agent_batch_min: нужно целое 0–60")
    agent_batch = changes.pop("agent_batch_min", None)
    if "unsorted_accounts" in changes and changes["unsorted_accounts"] is None:
        raise HTTPException(422, "unsorted_accounts: нужен список номеров аккаунтов ([] — снять)")
    unsorted = changes.pop("unsorted_accounts", None)
    if "workday" in changes and changes["workday"] is None:
        raise HTTPException(422, "workday: нужно true или false")
    workday_on = changes.pop("workday", None)
    if "card_route" in changes and not changes["card_route"]:
        raise HTTPException(422, "card_route: {группа: me|agent|auto}")
    card_route = changes.pop("card_route", None)
    fallback_given = "session_fallback" in changes
    session_fallback = changes.pop("session_fallback", None)

    def _fn(conn):
        if not admin_mod.update_project(conn, project_id, changes):
            return False
        heard = None
        if unsorted is not None:
            heard = unsorted_mod.set_project(conn, project_id, unsorted)  # Ф52-2 A2
        if join_mode is not None:
            join_mod.set_project_join(conn, project_id, join_mode)
        if handled_on is not None:
            handled_mod.set_project_enabled(conn, project_id, handled_on)  # Ф37-3 A
        for key, on in digest_flags.items():
            digest_mod.set_project(conn, project_id, key, on)  # Ф86-1 (C4)
        if agent_batch is not None:
            subs_mod.set_project_agent_batch(conn, project_id, agent_batch)  # Ф48-4 п.4
        if workday_on is not None:
            workday_mod.set_workday(conn, project_id, workday_on, now_ms=now_ms)  # Ф64-1
        if fallback_given:
            # Ф83-1: своя запись аудита `project_session_fallback` {project_id, old, new}.
            engine_fallback_mod.set_project_value(
                conn,
                project_id,
                session_fallback,
                now_ms=now_ms,
                audit={
                    "actor": principal.kind,
                    "api_token_id": principal.token_id,
                    "client_name": _client_name(request),
                    "request_id": request_id,
                },
            )
        if card_route is not None:
            # Ф66-1 (A.6): своя запись аудита `card_route` {project_id, old, new}.
            card_route_mod.set_project(
                conn,
                project_id,
                card_route,
                now_ms=now_ms,
                audit={
                    "actor": principal.kind,
                    "api_token_id": principal.token_id,
                    "client_name": _client_name(request),
                    "request_id": request_id,
                },
            )
        keys = sorted(
            [
                *changes,
                *(["session_join"] if join_mode is not None else []),
                *(["session_handled"] if handled_on is not None else []),
                *digest_flags,
                *(["agent_batch_min"] if agent_batch is not None else []),
                *(["unsorted_accounts"] if unsorted is not None else []),
                *(["workday"] if workday_on is not None else []),
                *(["card_route"] if card_route is not None else []),
                *(["session_fallback"] if fallback_given else []),
            ]
        )
        detail: dict = {"project_id": project_id, "keys": keys}
        if join_mode is not None:
            detail["session_join"] = join_mode
        if handled_on is not None:
            detail["session_handled"] = handled_on
        detail.update(digest_flags)
        if agent_batch is not None:
            detail["agent_batch_min"] = agent_batch
        if unsorted is not None:
            detail["unsorted_accounts"] = heard
        if workday_on is not None:
            detail["workday"] = workday_on
        _audit(conn, request, principal, request_id, now_ms, "project_update", detail)
        return True

    try:
        found = await db.write(_fn)
    except admin_mod.InvalidProject as exc:
        raise HTTPException(422, str(exc)) from exc
    except unsorted_mod.UnsortedError as exc:
        raise HTTPException(422, str(exc)) from exc
    except card_route_mod.RouteError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    except SessionError as exc:  # Ф83-1: негодный запасной сессий проекта
        raise HTTPException(exc.status, exc.detail) from exc
    except admin_mod.ProjectNameTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    if not found:
        raise HTTPException(404, "not found")
    return {"ok": True}


# --- перенос пачки бесед ------------------------------------------------------------------------------------


class ChannelsMoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_ids: list[StrictInt] = Field(min_length=1, max_length=_MOVE_MAX)
    # Ключ обязателен (без умолчания): число — ручная привязка, `null` — «Неразобранное» вручную, `"auto"` —
    # назад к правилам с пересчётом.
    project_id: StrictInt | Literal["auto"] | None

    @field_validator("channel_ids")
    @classmethod
    def _no_repeats(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("channel_ids: без повторов")
        return value


@public_router.post("/channels/move")
async def post_channels_move(
    body: ChannelsMoveIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Решение 5: пачка — одна транзакция, всё или ничего (неизвестная беседа или проект -> 404, ничего не
    записано); аудит один на пачку, без названий бесед."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    if body.project_id == "auto":
        fields: dict = {"map_source": "auto"}
    else:
        fields = {"project_id": body.project_id}

    def _fn(conn):
        for channel_id in body.channel_ids:
            queries.channel_visible(conn, principal, channel_id)
            channels_mod.update_channel(conn, channel_id, now_ms=now_ms, **fields)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "channels_move",
            {"channel_ids": list(body.channel_ids), "project_id": body.project_id},
        )
        return len(body.channel_ids)

    try:
        moved = await db.write(_fn)
    except (queries.NotFound, channels_mod.NotFound, mapping.NotFound) as exc:
        raise HTTPException(404, "not found") from exc
    except channels_mod.ChannelError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"moved": moved}
