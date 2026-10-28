"""Ручки переключателя движка (Ф16, docs/architecture.md; Ф78-2, 3.2).

- публичные (`/api/v1`): `GET /engine` — любой авторизованный (UI, профили MCP, токен hub-work, агент, факты);
  `PUT /engine` `{engine, model?, effort?}` — только UI-сессия (кука); Ф78-2 — и с
  телефона (удалённый слушатель), прежнего «только локально» нет;
- Ф78-2 (раздел 12): `PUT /engine/facts` `{engine|null, model?, effort?}` — свой выбор службы фактов, тот же
  доступ, что у `PUT /engine` (кука, ноутбук и телефон; bearer — 403); `engine: null` — снова за агентом;
- внутренние (`/internal/v1`, админ-сокет): `GET /admin/engine` и `POST /admin/engine` — для `hub engine`,
  `POST /admin/engine/facts` — для `hub engine facts`;
- Ф83-1 (запасные движки): `PUT /engine/fallback/{agent|facts}` `{engine|null, model?, effort?}` и
  `POST /engine/fallback/{who}/return` — только кука; `POST /engine/fallback/{who}/failed` `{kind, reason?}` —
  агент владельца (bearer ровно `human`) только `agent`, служба фактов (bearer ровно `read facts`) только `facts`;
  админ-сокет `POST /admin/engine/fallback/{who}` и `/return` — для `hub engine fallback`.

Маршруты не пишут SQL: чтение и запись — `core/engine.py` (запись внутри `Db.write()`). Будить длинные
ожидания отдельно не нужно: запись сама зовёт `notifier.notify()` через `db.on_commit` (П15), поэтому
`/work/host/wait` и `hub sub wait` выходят сразу после смены движка.
"""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from .. import connector_auth
from .. import engine as engine_mod
from .. import engine_fallback as fallback_mod
from ..security import FACTS_SCOPES, HUMAN_SCOPES, Principal, principal_dependency

public_router = APIRouter(tags=["engine"])
internal_router = APIRouter(tags=["engine-internal"])

_authed = Depends(principal_dependency())
_admin = Depends(connector_auth.admin_dependency())


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _config_dir(request: Request) -> Path | None:
    """Каталог конфига хаба (его ставит `runner`): там `engine.json` — готовность OpenCode (П13)."""
    value = getattr(request.app.state, "config_dir", None)
    return value if isinstance(value, Path) else None


def _require_ui(principal: Principal) -> None:
    if not principal.is_ui:
        raise HTTPException(403, "forbidden: ui session required")


def _status(request: Request) -> dict:
    with request.app.state.db.reader() as conn:
        return engine_mod.status(conn, config_dir=_config_dir(request))


class EngineIn(BaseModel):
    """Ф78-2 (3.2): `{engine, model?, effort?}`; поле не передано — прежний выбор этого движка, `null` —
    умолчание движка."""

    model_config = ConfigDict(extra="forbid")

    engine: Literal["claude", "opencode", "codex", "cursor"]
    model: StrictStr | None = Field(default=None, max_length=800)
    effort: StrictStr | None = Field(default=None, max_length=128)


async def _set(request: Request, body: EngineIn, *, actor: str) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    config_dir = _config_dir(request)
    given = body.model_fields_set
    model = body.model if "model" in given else engine_mod.KEEP
    effort = body.effort if "effort" in given else engine_mod.KEEP
    try:
        change = await db.write(
            lambda conn: engine_mod.set_engine(
                conn,
                body.engine,
                now_ms=now_ms,
                actor=actor,
                config_dir=config_dir,
                model=model,
                effort=effort,
            )
        )
    except engine_mod.EngineError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {**_status(request), "old_engine": change.old}


class FactsEngineIn(BaseModel):
    """Ф78-2 (раздел 12): `{engine, model?, effort?}` выбора фактов; `engine: null` — снять запись (факты
    снова за агентом). Поле не передано — прежнее значение записи фактов того же движка, `null` —
    умолчание."""

    model_config = ConfigDict(extra="forbid")

    engine: Literal["claude", "opencode", "codex", "cursor"] | None
    model: StrictStr | None = Field(default=None, max_length=800)
    effort: StrictStr | None = Field(default=None, max_length=128)


async def _set_facts(request: Request, body: FactsEngineIn, *, actor: str) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    config_dir = _config_dir(request)
    given = body.model_fields_set
    model = body.model if "model" in given else engine_mod.KEEP
    effort = body.effort if "effort" in given else engine_mod.KEEP
    try:
        await db.write(
            lambda conn: engine_mod.set_facts_engine(
                conn,
                body.engine,
                now_ms=now_ms,
                actor=actor,
                config_dir=config_dir,
                model=model,
                effort=effort,
            )
        )
    except engine_mod.EngineError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return _status(request)


@public_router.get("/engine")
async def get_engine(request: Request, principal: Principal = _authed) -> dict:
    return _status(request)


@public_router.put("/engine")
async def put_engine(body: EngineIn, request: Request, principal: Principal = _authed) -> dict:
    _require_ui(principal)
    return await _set(request, body, actor="ui")


@public_router.put("/engine/facts")
async def put_engine_facts(body: FactsEngineIn, request: Request, principal: Principal = _authed) -> dict:
    _require_ui(principal)
    return await _set_facts(request, body, actor="ui")


# --- запасные агента и фактов (Ф83-1, П3, П10, П11) ------------------------------------------------------

_any_with_human = Depends(principal_dependency(human_ok=True))
Who = Literal["agent", "facts"]


class FallbackIn(BaseModel):
    """`{engine|null, model?, effort?}` запасного; `engine: null` — нет запасного (снимает и
    «на запасном»)."""

    model_config = ConfigDict(extra="forbid")

    engine: Literal["claude", "opencode", "codex", "cursor"] | None
    model: StrictStr | None = Field(default=None, max_length=800)
    effort: StrictStr | None = Field(default=None, max_length=128)


class FailedIn(BaseModel):
    """Отказ основного: `{kind, reason?}` (`kind` — `failure.KINDS`)."""

    model_config = ConfigDict(extra="forbid")

    # Ф85-1 (C): `refusal` (модель отказалась), `transient` (обрыв, 5xx, таймаут) — повод только у агента
    # владельца.
    kind: Literal["limit", "balance", "auth", "model", "retry", "other", "refusal", "transient"]
    reason: StrictStr | None = Field(default=None, max_length=2000)


def _ring_add(request: Request, title: str, text: str) -> None:
    notifier = getattr(request.app.state, "notifier", None)
    ring = getattr(notifier, "ring", None)
    if ring is not None:
        ring.add(title, text, "normal")


async def _set_fallback(request: Request, who: str, body: FallbackIn, *, actor: str) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    config_dir = _config_dir(request)
    try:
        await db.write(
            lambda conn: fallback_mod.set_fallback(
                conn,
                who,
                body.engine,
                model=body.model,
                effort=body.effort,
                now_ms=now_ms,
                actor=actor,
                config_dir=config_dir,
            )
        )
    except engine_mod.EngineError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return _status(request)


async def _return(request: Request, who: str, *, actor: str) -> dict:
    now_ms = _now_ms(request)
    returned = await request.app.state.db.write(
        lambda conn: fallback_mod.return_main(conn, who, now_ms=now_ms, actor=actor)
    )
    return {**_status(request), "returned": returned}


@public_router.put("/engine/fallback/{who}")
async def put_engine_fallback(
    who: Who, body: FallbackIn, request: Request, principal: Principal = _authed
) -> dict:
    """Ф83-1: запасной агента или фактов — только кука (ноутбук и телефон). Ответ — как `GET /engine`."""
    _require_ui(principal)
    return await _set_fallback(request, who, body, actor="ui")


@public_router.post("/engine/fallback/{who}/failed")
async def post_engine_fallback_failed(
    who: str, raw: dict, request: Request, principal: Principal = _any_with_human
) -> dict:
    """Ф83-1 (П11): отказ основного сообщает сам исполнитель — агент владельца (bearer ровно `human`) только
    `who=agent`, служба фактов (bearer ровно `read facts`) только `who=facts`; остальным 403. Ответ —
    `{switched, why?, on_fallback, effective}`; переход — одно уведомление владельцу."""
    agent = not principal.is_ui and principal.scopes == HUMAN_SCOPES
    facts = not principal.is_ui and principal.scopes == FACTS_SCOPES
    if not ((who == "agent" and agent) or (who == "facts" and facts)):
        raise HTTPException(403, "forbidden")
    # Тело — после прав: чужому токену 403 и на кривое тело (обход ручек тестами П3 Ф54).
    try:
        body = FailedIn.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            422, "тело: {kind: limit|balance|auth|model|retry|other|refusal|transient, reason?}"
        ) from exc
    now_ms = _now_ms(request)
    config_dir = _config_dir(request)
    try:
        done = await request.app.state.db.write(
            lambda conn: fallback_mod.failed(
                conn,
                who,
                kind=body.kind,
                reason=body.reason,
                now_ms=now_ms,
                actor=principal.kind,
                token_id=principal.token_id,
                config_dir=config_dir,
            )
        )
    except engine_mod.EngineError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    note = done.pop("notify", None)
    if note is not None:
        _ring_add(request, *note)
    return done


@public_router.post("/engine/fallback/{who}/return")
async def post_engine_fallback_return(who: Who, request: Request, principal: Principal = _authed) -> dict:
    """Ф83-1: «Вернуть на основной» — только кука (ноутбук и телефон). Ответ — как `GET /engine` и
    `returned` (было ли «на запасном»)."""
    _require_ui(principal)
    return await _return(request, who, actor="ui")


# --- админ-сокет -------------------------------------------------------------------------------------------


@internal_router.get("/admin/engine")
async def get_admin_engine(request: Request, _: None = _admin) -> dict:
    return _status(request)


@internal_router.post("/admin/engine")
async def post_admin_engine(body: EngineIn, request: Request, _: None = _admin) -> dict:
    return await _set(request, body, actor="admin")


@internal_router.post("/admin/engine/facts")
async def post_admin_engine_facts(body: FactsEngineIn, request: Request, _: None = _admin) -> dict:
    return await _set_facts(request, body, actor="admin")


@internal_router.post("/admin/engine/fallback/{who}")
async def post_admin_engine_fallback(who: Who, body: FallbackIn, request: Request, _: None = _admin) -> dict:
    """Ф83-1: `hub engine fallback <agent|facts> set|off`."""
    return await _set_fallback(request, who, body, actor="admin")


@internal_router.post("/admin/engine/fallback/{who}/return")
async def post_admin_engine_fallback_return(who: Who, request: Request, _: None = _admin) -> dict:
    """Ф83-1: `hub engine fallback <agent|facts> return`."""
    return await _return(request, who, actor="admin")
