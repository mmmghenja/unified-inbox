"""Ручки очереди канала владельца (Ф56-1а, docs/architecture.md).

Допуск: кука интерфейса (и с телефона) и bearer ровно со скоупом `human` (агент владельца); бот `human_bot` и
любой другой bearer — 403. Сборка очереди и ответы — в `core/owner_queue.py`; маршруты SQL не пишут.

| ручка | кука | агент `human` |
| `GET /owner_channel/queue`, `/owner_channel/changes` | всё | карточки «в канале» своих проектов, поручения |
| `GET /owner_channel/queue?scope=all` (Ф75-1) | всё | вся открытая очередь своих проектов (для разговора) |
| `POST /owner_channel/{ref}/answer` | да | «в канале» (иначе 403 `not_in_channel`); с `word` — по слову владельца |
| `GET /owner_channel/agent-actions` | да | да |
| `PUT /owner_channel/agent-actions` | 403 | да |
| `POST /owner_channel/{ref}/errand` | да | только себе, без действия и текста (авторешение; с `word` — по слову) |
| `DELETE /owner_channel/errands/{id}` | да | 403 |
| `POST /owner_channel/errands/{id}/progress`, `/return` | 403 | да |
| `GET /owner_channel/closed` | да | 403 |
| `GET /owner_channel/talk`, `POST /owner_channel/talk` (Ф58-1) | да (пишет `owner_channel`) | да (пишет `agent`) |
| `GET /owner_channel/talk/open`, `POST /owner_channel/talk/{id}/handled`, `PUT /owner_channel/talk/{id}/progress` | 403
| да |
| `POST /owner_channel/talk/read` | да | 403 |
| `POST /owner_channel/talk/files`, `DELETE /owner_channel/talk/files/{id}` (23.10) | да | 403 |
| `GET /owner_channel/talk/files/{id}` (23.10) | да | да |
| `POST /owner_channel/talk/{id}/closed` (Ф61-1) | 403 | да |
| `GET /owner_channel/talk/{id}/answer` (Ф85-1) | 403 | да |
| `POST /owner_channel/errands/{id}/ask`, `GET /owner_channel/errands/{id}/state` (Ф85-1) | 403 | да |
| `POST /owner_channel/errands/{id}/hands` (Ф96-1; ещё и бот `human_bot`) | да | 403 |
| `POST /owner_channel/errands/{id}/handoff` (Ф96-1) | 403 | да |
| `GET /owner_channel/{ref}/session` (Ф68-1) | да | только карточки «в канале» (иначе 403 `not_in_channel`) |
| `GET /owner_channel/talk/branches` (Ф66-2) | да | да (ветки сессий своих проектов) |
| `GET /owner_channel/sessions/{id}/context` (Ф66-2) | да | да (сессии своих проектов) |
| `GET /owner_channel/talk/branches/{id|general}/summary` (Ф66-2) | да | да |
| `PUT /owner_channel/talk/branches/{id|general}/summary` (Ф66-2) | 403 | да |
| `GET /owner_channel/agent-settings` (Ф75-1) | да | да |
| `PUT /owner_channel/agent-settings` (Ф75-1) | да | 403 |
| `GET /owner_channel/agent-settings/projects/{id}` (Ф76-3) | да | проекты своего токена |
| `PUT /owner_channel/agent-settings/projects/{id}` (Ф76-3) | да | 403 |
| `PUT /owner_channel/agent-models` (Ф75-1) | 403 | да |

Голосовой ответ бота: `POST /owner_channel/bot-dictate` — тоже только `human_bot`.
Ф61-1 (П11): две ручки бота `GET /owner_channel/talk/asks` и `POST /owner_channel/talk/bot-reply` пускают ТОЛЬКО токен
ровно
`human_bot` (своя зависимость); кука и агент — 403. Ф93-1 (B.1): так же — `GET /owner_channel/money` и
`POST /owner_channel/card-reply` (денежные карточки владельцу и его ответ из бота), F2 — `GET /owner_channel/returned`
(карточки, которые агент вернул владельцу). Остальные ручки этого файла боту
по-прежнему 403."""

import asyncio
import hashlib
import json
import os
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .. import held_files
from .. import human_requests as human_mod
from .. import owner_agent_model as agent_model_mod
from .. import owner_queue as zq
from .. import owner_session as session_mod
from .. import owner_talk as talk_mod
from ..hub_sessions import SessionError
from ..security import HUMAN_BOT_SCOPES, Principal, principal_dependency
from . import media as media_routes
from . import sessions as sessions_routes
from .write import _client_name, _now_ms, touch_activity

public_router = APIRouter(tags=["owner_channel"])

_principal = Depends(principal_dependency(human_ok=True))


def _caller(principal: Principal = _principal) -> Principal:
    """П10: кука владельца или его агент (bearer ровно `human`); бот `human_bot` и прочий bearer — 403 раньше
    разбора тела."""
    if principal.is_ui or zq.is_agent(principal):
        return principal
    raise HTTPException(403, "forbidden")


def _ui_caller(principal: Principal = _principal) -> Principal:
    if principal.is_ui:
        return principal
    raise HTTPException(403, "forbidden")


def _agent_caller(principal: Principal = _principal) -> Principal:
    if zq.is_agent(principal):
        return principal
    raise HTTPException(403, "forbidden")


def _bot_caller(principal: Principal = _principal) -> Principal:
    """Ф61-1 П11: только бот владельца — bearer ровно `human_bot`."""
    if not principal.is_ui and principal.scopes == HUMAN_BOT_SCOPES:
        return principal
    raise HTTPException(403, "forbidden")


def _ui_or_bot_caller(principal: Principal = _principal) -> Principal:
    """Ф96-1 (П8): кука владельца или его бот (bearer ровно `human_bot`); агент и прочий bearer — 403."""
    if principal.is_ui or principal.scopes == HUMAN_BOT_SCOPES:
        return principal
    raise HTTPException(403, "forbidden")


_any = Depends(_caller)
_ui = Depends(_ui_caller)
_agent = Depends(_agent_caller)
_bot = Depends(_bot_caller)
_ui_or_bot = Depends(_ui_or_bot_caller)

_ID_MAX = 2**53
_REF_MAX = 64
WAIT_MAX_S = 25


def _source(principal: Principal) -> str:
    """`ui` — кука владельца, `agent` — его агент."""
    return "ui" if principal.is_ui else "agent"


def _audit(request: Request, principal: Principal, request_id: str) -> dict:
    return {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": _client_name(request),
        "request_id": request_id,
    }


def _request_id(response: Response) -> str:
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    return request_id


def _presence(request: Request, now_ms: int) -> list[dict]:
    store = getattr(request.app.state, "presence", None)
    if store is None:
        return []
    return [{"channel_id": c, "thread_ext_id": t} for c, t in store.items(now_ms)]


def _waiting(request: Request):
    waiters = getattr(request.app.state, "sub_waiters", None)
    return waiters.waiting if waiters is not None else None


async def _write(request: Request, fn):
    try:
        return await request.app.state.db.write(fn)
    except Exception as exc:
        mapped = zq.error_of(exc)
        if mapped is None:
            raise
        raise HTTPException(mapped[0], mapped[1]) from exc


async def _prepare(request: Request, principal: Principal, now_ms: int) -> None:
    """Перед чтением: снять просроченные заявки «нужен человек» (как их ручки), закрыть поручения, чья
    карточка закрыта другим путём, вернуть владельцу зависшие поручения (Ф65-1 Ж2), отметить опрос агента («на
    связи»). Каждое — своей записью, только когда
    нужно."""
    db = request.app.state.db
    presence = _presence(request, now_ms)
    with db.reader() as conn:
        human_due = human_mod.has_due(conn, now_ms)
        stale = zq.stale_errands(conn, principal, now_ms=now_ms, presence=presence)
        hung = zq.hung_errands(conn, now_ms=now_ms)  # Ф65-1 (Ж2)
        seen_due = zq.is_agent(principal) and zq.agent_seen_due(conn, principal, now_ms)
    if human_due:
        await db.write(lambda conn: human_mod.sweep(conn, now_ms))
    if stale:
        await db.write(lambda conn: zq.close_errands(conn, stale, now_ms=now_ms))
    if hung:
        await db.write(lambda conn: zq.return_hung(conn, now_ms=now_ms))
    if seen_due:
        await db.write(lambda conn: zq.agent_seen(conn, principal, now_ms))


def _snapshot(request: Request, principal: Principal, now_ms: int, *, everything: bool = False) -> dict:
    with request.app.state.db.reader() as conn:
        seq = zq.watermark(conn)  # ДО сборки: изменение посреди чтения разбудит следующий опрос
        cards = zq.items(
            conn,
            principal=principal,
            now_ms=now_ms,
            waiting=_waiting(request),
            presence=_presence(request, now_ms),
            everything=everything,
        )
        state = zq.agent_state(conn, now_ms)
        talk = talk_mod.counts(conn)  # Ф58-1: разговор владельца с агентом
    agent = {"online": state["online"], "seen_ts": state["seen_ts"], **talk}
    return {"items": cards, "seq": seq, "agent": agent}


@public_router.get("/owner_channel/queue")
async def get_owner_queue(
    request: Request,
    scope: Literal["channel", "all"] = Query("channel"),
    principal: Principal = _any,
) -> dict:
    """Очередь: `{items, seq, agent: {online, seen_ts}}`. Кука — все открытые карточки; агент — карточки «в
    канале» своих проектов и свои поручения (опрос отмечает его «на связи»). Ф75-1: `scope=all` — агенту вся
    открытая очередь своих проектов (у карточки `in_channel` — маршрут), для разговора «Мой агент»."""
    now_ms = _now_ms(request)
    await _prepare(request, principal, now_ms)
    return _snapshot(request, principal, now_ms, everything=scope == "all")


@public_router.get("/owner_channel/changes")
async def get_owner_changes(
    request: Request,
    since: int = Query(0, ge=0, le=2**62),
    wait: float = Query(0, ge=0, le=WAIT_MAX_S),
    principal: Principal = _any,
):
    """Длинный опрос: тот же ответ, что у `/owner_channel/queue` (полный снимок), когда очередь изменилась после
    `since` или вышло `wait` секунд (0…25). Клиент уходит — 204. Ревью Ф56 (3): общий `seq` двигает любая
    сессия хаба (шаг хода, входящее), поэтому пробуждение сверяется с отпечатком очереди: клиент с отметкой
    «на текущем `seq`» видел нынешний снимок — пока отпечаток тот же, опрос ждёт дальше, а `since` уходит
    на новый `seq`. Отметка позади текущего `seq` на входе — снимок сразу (что видел клиент, неизвестно)."""
    app = request.app
    notifier = app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)  # lifespan не отработал (ASGITransport) — как `/stream`
    deadline = loop.time() + wait
    seen: str | None = None  # отпечаток снимка, который клиент уже видел
    while True:
        now_ms = _now_ms(request)
        await _prepare(request, principal, now_ms)
        gen = notifier.gen  # ДО чтения БД — notify() между чтением и wait() не потеряется
        with app.state.db.reader() as conn:
            ready = zq.has_changes(conn, since)
        remaining = deadline - loop.time()
        if ready or remaining <= 0 or notifier.closed:
            snap = _snapshot(request, principal, _now_ms(request))
            if seen is None and ready and remaining > 0 and not notifier.closed:
                return snap
            if seen is not None and remaining > 0 and not notifier.closed and _fingerprint(snap) == seen:
                since = snap["seq"]  # очередь та же — ждать дальше с новой отметки
                continue
            return snap
        if seen is None:
            # Круг 2 (3): отпечаток «клиент это видел» верен, только если снимок снят на той же отметке, что
            # у клиента. Писатель успел между проверкой и снимком (`seq` ушёл) — снимок отдаётся сразу.
            snap = _snapshot(request, principal, _now_ms(request))
            if snap["seq"] != since:
                return snap
            seen = _fingerprint(snap)
        await notifier.wait(gen, remaining)
        if await request.is_disconnected():
            return Response(status_code=204)  # отвечать некому


def _fingerprint(snap: dict) -> str:
    """Отпечаток снимка очереди: карточки целиком (ref, сроки, `hold_ts`, поручение, кнопки агента, пометки),
    «на связи» агента и разговор (Ф58-1: `talk_unread`, `talk_open`, id последней реплики — иначе новая
    реплика не будит опрос); `seq` и `seen_ts` — нет (они меняются без изменения очереди)."""
    agent = snap["agent"]
    talk = [agent["talk_unread"], agent["talk_open"], agent["talk_last_id"], agent["talk_progress"]]
    body = {"items": snap["items"], "online": agent["online"], "talk": talk}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class WordIn(BaseModel):
    """Ф75-1: слово владельца — id его реплики в разговоре «Мой агент», по которой действует агент. Ф76-1:
    или `errand_id` — его указание текстом к «Реши» по этой карточке (только в ответе по карточке). Ровно
    одно поле — сверяет `owner_word.check`."""

    model_config = ConfigDict(extra="forbid")

    talk_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    errand_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: StrictStr = Field(min_length=1, max_length=32)
    # Точные пределы (по виду) проверяет ядро — отказ уходит готовым русским текстом.
    text: StrictStr | None = Field(default=None, max_length=64 * 1024)
    answers: dict[str, str] | None = None
    send_now: StrictBool = False
    # Ф65-1 (Ж4): итог агента «что сделал» (до 300; только агент — у куки 422).
    result: StrictStr | None = Field(default=None, max_length=zq.RESULT_MAX * 4)
    # Ф75-1: агент действует по реплике владельца в разговоре (`owner_word.check`; у куки 422).
    word: WordIn | None = None
    # Ф76-3: основание ответа агента (`decisions.check_basis`, до 200; у куки 422).
    basis: StrictStr | None = Field(default=None, max_length=800)
    # Файлы в реплике сессии (`turn` -> `reply`, состояние -> `say`/`interrupt`): id из
    # `POST /sessions/{id}/files` той же сессии; пределы проверяет `held_work.say`.
    files: list[StrictStr] | None = Field(default=None, max_length=held_files.SAY_MAX_FILES * 10)


@public_router.post("/owner_channel/{ref}/answer")
async def post_owner_answer(
    body: AnswerIn,
    request: Request,
    response: Response,
    ref: str = Path(min_length=3, max_length=_REF_MAX),
    principal: Principal = _any,
) -> dict:
    """Ответ по карточке: `{action, text?, answers?, send_now?, result?}` -> `{ref, status}`. 404 — нет
    карточки или чужой проект; 403 `not_in_channel` — агенту карточка не в канале; 422 — действие не из
    `answers` вида, `result` от куки; 409 — уже решена или решает делегат. Ф65-1: ответ владельца по карточке,
    которую решает агент, закрывает поручение и проходит (Ж1); `result` агента — итог в поручении (Ж4)."""
    source = _source(principal)
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    presence = _presence(request, now_ms)
    waiting = _waiting(request)
    if source == "ui":
        touch_activity(request, principal)  # П9: «На связи» — только от клика владельца

    def _fn(conn):
        return zq.answer(
            conn,
            ref,
            action=body.action,
            text=body.text,
            answers=body.answers,
            send_now=body.send_now,
            principal=principal,
            source=source,
            now_ms=now_ms,
            audit=audit,
            waiting=waiting,
            presence=presence,
            result=body.result,
            word=body.word.model_dump(exclude_none=True) if body.word is not None else None,
            basis=body.basis,
            config_dir=config_dir,
            files=body.files,
        )

    config_dir = _config_dir(request)
    done = await _write(request, _fn)
    if done.replaced is not None:
        # Ф83-1 (П6): «Перейти на запасной» — передача дел: старая сессия уходит из реестра (как у `handoff`).
        request.app.state.held.discard(done.replaced)
        live = getattr(request.app.state, "live", None)
        if live is not None:
            live.drop(done.replaced)
    if done.session is not None:
        request.app.state.held.add(done.session.id)  # заявка «новая сессия»: в реестр после коммита
    return {"ref": done.ref, "status": done.status}


@public_router.get("/owner_channel/{ref}/session")
async def get_owner_card_session(
    request: Request,
    ref: str = Path(min_length=3, max_length=_REF_MAX),
    before: int | None = Query(None, ge=1, le=_ID_MAX),
    limit: int = Query(session_mod.LIMIT_DEFAULT, ge=1, le=session_mod.LIMIT_MAX),
    principal: Principal = _any,
) -> dict:
    """Ф68-1: сессия хаба карточки — `{session, role_file, events, more_before, last_answer, window}`
    (`window` — Ф76-5: окно браузера сессии из её последней заявки «нужен человек» или null); у карточки
    без сессии хаба — `{"session": null}`. `events` — последние `limit` событий ленты до `before` (без него —
    с конца), по возрастанию; `more_before` — курсор «дочитать раньше». Только чтение. Нет карточки, закрыта
    или чужой проект — 404; агенту карточка не в канале — 403 `not_in_channel`."""
    now_ms = _now_ms(request)
    presence = _presence(request, now_ms)
    try:
        with request.app.state.db.reader() as conn:
            found = session_mod.find(conn, ref, principal=principal, now_ms=now_ms, presence=presence)
            rec = found.session
            if rec is None:
                return {"session": None}
            reply = session_mod.read_context(conn, rec, before=before, limit=limit)
            reply["window"] = session_mod.window(conn, rec)
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return await asyncio.to_thread(session_mod.finish, reply, rec)


@public_router.get("/owner_channel/sessions/{session_id}/context")
async def get_owner_session_context(
    request: Request,
    session_id: int = Path(ge=1, le=_ID_MAX),
    before: int | None = Query(None, ge=1, le=_ID_MAX),
    limit: int = Query(session_mod.LIMIT_DEFAULT, ge=1, le=session_mod.LIMIT_MAX),
    principal: Principal = _any,
) -> dict:
    """Ф66-2 (A.3): то же, что `GET /owner_channel/{ref}/session`, но по id сессии хаба (ветка разговора с агентом):
    `{session, role_file, events, more_before, last_answer, window}`. Сессия и закрытая; нет такой или проект
    вне скоупа токена — 404. Только чтение."""
    try:
        with request.app.state.db.reader() as conn:
            rec = session_mod.by_id(conn, session_id, principal=principal)
            reply = session_mod.read_context(conn, rec, before=before, limit=limit)
            reply["window"] = session_mod.window(conn, rec)
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return await asyncio.to_thread(session_mod.finish, reply, rec)


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    label: StrictStr
    kinds: list[StrictStr] = Field(default_factory=list, max_length=len(zq.KINDS) + len(zq.RETIRED_KINDS))
    needs_text: StrictBool = False


class ActionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[ActionIn] = Field(max_length=zq.ACTIONS_MAX)


@public_router.get("/owner_channel/agent-actions")
async def get_owner_agent_actions(request: Request, principal: Principal = _any) -> dict:
    """Объявленные агентом действия: `{online, seen_ts, actions}` (не на связи — `actions` пуст)."""
    with request.app.state.db.reader() as conn:
        return zq.agent_state(conn, _now_ms(request))


@public_router.put("/owner_channel/agent-actions")
async def put_owner_agent_actions(body: ActionsIn, request: Request, principal: Principal = _agent) -> dict:
    """Агент объявляет свои действия `{actions: [{id, label, kinds, needs_text}]}` (до 20)."""
    now_ms = _now_ms(request)
    actions = [a.model_dump() for a in body.actions]
    return await _write(request, lambda conn: zq.set_agent_actions(conn, principal, actions, now_ms))


# --- Ф75-1: модель агента владельца (docs/architecture.md) ------------------


def _config_dir(request: Request):
    """Каталог конфига: `app.state.config_dir` (тесты, стенд), иначе каталог хаба (`engine.json` машины)."""
    configured = getattr(request.app.state, "config_dir", None)
    if configured is not None:
        return configured
    from ... import paths  # здесь: ядро не тянет `paths` на уровне модуля

    return paths.config_dir()


class AgentSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные правила («провайдер/модель», уровень) проверяет ядро — отказ русским текстом.
    model: StrictStr | None = Field(default=None, max_length=agent_model_mod.MODEL_MAX * 4)
    # Ф78-2: усилие текущего движка; `variant` — прежнее имя поля (старый интерфейс до 78-2c).
    effort: StrictStr | None = Field(default=None, max_length=agent_model_mod.VARIANT_MAX * 4)
    variant: StrictStr | None = Field(default=None, max_length=agent_model_mod.VARIANT_MAX * 4)

    def level(self) -> str | None:
        return self.effort if self.effort is not None else self.variant


class AgentModelIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: StrictStr = Field(max_length=agent_model_mod.MODEL_MAX * 4)
    name: StrictStr | None = Field(default=None, max_length=agent_model_mod.NAME_MAX * 4)
    images: StrictBool | None = None
    variants: list[StrictStr] = Field(default_factory=list, max_length=64)
    efforts: list[StrictStr] = Field(default_factory=list, max_length=64)  # Ф78-2: другое имя `variants`


class AgentModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: StrictStr = Field(default="opencode", max_length=16)  # Ф78-2: чей список
    models: list[AgentModelIn] = Field(max_length=agent_model_mod.MODELS_MAX * 4)


async def _settings_write(request: Request, fn):
    try:
        return await request.app.state.db.write(fn)
    except agent_model_mod.SettingsError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.get("/owner_channel/agent-settings")
async def get_owner_agent_settings(request: Request, principal: Principal = _any) -> dict:
    """Модель агента — выбор ТЕКУЩЕГО движка (Ф78-2, 3.3): `{engine, model, effort, variant, set_ts, default:
    {model, effort, variant}, models: [{id, name, images, efforts, variants}], models_ts, projects: [...]}`.
    `model = null` — владелец не выбирал (умолчание движка). Форма — `owner_agent_model.settings`."""
    config_dir = _config_dir(request)
    with request.app.state.db.reader() as conn:
        reply = agent_model_mod.settings(conn, config_dir=config_dir)
    # Ф76-3: замены по проектам — только проекты, видные вызывающему.
    reply["projects"] = [p for p in reply["projects"] if zq._visible(principal, p["project_id"])]  # noqa: SLF001
    return reply


@public_router.get("/owner_channel/agent-settings/projects/{project_id}")
async def get_owner_agent_project_model(
    request: Request, project_id: int = Path(ge=1, le=2**53), principal: Principal = _any
) -> dict:
    """Ф76-3 (П15): замена модели агента в проекте `{project_id, engine, model, effort, variant, set_ts}`;
    `model = null` — общий выбор. Нет проекта или проект вне токена агента — 404."""
    if not zq._visible(principal, project_id):  # noqa: SLF001
        raise HTTPException(404, "not found")
    with request.app.state.db.reader() as conn:
        reply = agent_model_mod.project_choice(conn, project_id)
    if reply is None:
        raise HTTPException(404, "not found")
    return reply


@public_router.put("/owner_channel/agent-settings/projects/{project_id}")
async def put_owner_agent_project_model(
    body: AgentSettingsIn,
    request: Request,
    response: Response,
    project_id: int = Path(ge=1, le=2**53),
    principal: Principal = _ui,
) -> dict:
    """Ф76-3 (П15): только владелец (кука): `{model|null, effort}` (`variant` — старое имя `effort`) -> как GET.
    Ф78-2: замена пишется с текущим движком. `model = null` — снять замену (проект снова на общем выборе).
    Действует со следующего хода модели."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    touch_activity(request, principal)
    config_dir = _config_dir(request)
    reply = await _settings_write(
        request,
        lambda conn: agent_model_mod.set_project_choice(
            conn,
            project_id,
            model=body.model,
            effort=body.level(),
            now_ms=now_ms,
            audit=audit,
            config_dir=config_dir,
        ),
    )
    if reply is None:
        raise HTTPException(404, "not found")
    return reply


@public_router.put("/owner_channel/agent-settings")
async def put_owner_agent_settings(
    body: AgentSettingsIn, request: Request, response: Response, principal: Principal = _ui
) -> dict:
    """Только владелец (кука интерфейса): `{model|null, effort|null}` (`variant` — старое имя `effort`) -> то же,
    что GET. Ф78-2: это выбор ТЕКУЩЕГО движка (та же запись, что `PUT /engine`); `null` — умолчание движка.
    Действует со следующего хода модели."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    touch_activity(request, principal)
    config_dir = _config_dir(request)
    return await _settings_write(
        request,
        lambda conn: agent_model_mod.set_choice(
            conn, model=body.model, effort=body.level(), now_ms=now_ms, audit=audit, config_dir=config_dir
        ),
    )


@public_router.put("/owner_channel/agent-models")
async def put_owner_agent_models(
    body: AgentModelsIn, request: Request, principal: Principal = _agent
) -> dict:
    """Служба агента: модели движка `{engine?: "opencode", models: [{id, name?, images?,
    variants?|efforts?}]}` (при старте) -> `{engine, count, models_ts}`. Ф78-2: `engine` — чей список
    (умолчание `opencode`)."""
    now_ms = _now_ms(request)
    items = [m.model_dump() for m in body.models]
    engine = body.engine
    return await _settings_write(
        request, lambda conn: agent_model_mod.set_models(conn, items, now_ms=now_ms, engine=engine)
    )


class ErrandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: StrictStr | None = Field(default=None, max_length=32)
    text: StrictStr | None = Field(default=None, max_length=zq.ERRAND_TEXT_MAX * 4)
    word: WordIn | None = None  # Ф75-1: «Реши» по слову владельца (только агент)


@public_router.post("/owner_channel/{ref}/errand")
async def post_owner_errand(
    body: ErrandIn,
    request: Request,
    response: Response,
    ref: str = Path(min_length=3, max_length=_REF_MAX),
    principal: Principal = _any,
) -> dict:
    """Владелец поручает карточку агенту: `{action?, text?}` -> `{errand}`; агент берёт карточку своего канала
    себе сам (`{}`, авторешение)."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    presence = _presence(request, now_ms)
    errand = await _write(
        request,
        lambda conn: zq.errand_create(
            conn,
            ref,
            body.action,
            body.text,
            principal=principal,
            now_ms=now_ms,
            audit=audit,
            presence=presence,
            word=body.word.model_dump(exclude_none=True) if body.word is not None else None,
        ),
    )
    return {"errand": errand}


@public_router.delete("/owner_channel/errands/{errand_id}")
async def delete_owner_errand(
    request: Request,
    response: Response,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _ui,
) -> dict:
    """Владелец отзывает поручение."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    await _write(request, lambda conn: zq.errand_cancel(conn, errand_id, now_ms=now_ms, audit=audit))
    return {"id": errand_id, "status": "cancelled"}


class ProgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(max_length=zq.PROGRESS_MAX * 4)


@public_router.post("/owner_channel/errands/{errand_id}/progress")
async def post_owner_errand_progress(
    body: ProgressIn, request: Request, errand_id: int = Path(ge=1, le=_ID_MAX), principal: Principal = _agent
) -> dict:
    """Агент: строка хода поручения -> `{errand}`."""
    now_ms = _now_ms(request)
    errand = await _write(
        request,
        lambda conn: zq.errand_progress(conn, errand_id, body.text, principal=principal, now_ms=now_ms),
    )
    return {"errand": errand}


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(max_length=zq.RETURN_MAX * 4)
    # Ф65-1 (Ж3): что пробовал и что нужно от владельца (до 2000, маскируется).
    detail: StrictStr | None = Field(default=None, max_length=zq.RETURN_DETAIL_MAX * 4)


@public_router.post("/owner_channel/errands/{errand_id}/return")
async def post_owner_errand_return(
    body: ReturnIn,
    request: Request,
    response: Response,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _agent,
) -> dict:
    """Агент возвращает поручение `{reason, detail?}` («не вышло: …», что пробовал и что нужно от владельца) —
    карточка снова у владельца -> `{errand}`."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    errand = await _write(
        request,
        lambda conn: zq.errand_return(
            conn, errand_id, body.reason, principal=principal, now_ms=now_ms, audit=audit, detail=body.detail
        ),
    )
    return {"errand": errand}


class ErrandAskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные пределы (ключ 1–100, текст до 8000, срок 0 или 60…7200) проверяет ядро — русским текстом.
    operation_key: StrictStr = Field(max_length=zq.ASK_OP_KEY_MAX * 4)
    text: StrictStr = Field(max_length=talk_mod.TEXT_MAX * 4)
    ask_ttl_s: StrictInt | None = Field(default=None, ge=-(2**31), le=2**31)


@public_router.post("/owner_channel/errands/{errand_id}/ask")
async def post_owner_errand_ask(
    body: ErrandAskIn,
    request: Request,
    response: Response,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _agent,
) -> dict:
    """Ф85-1 (E): агент спрашивает владельца по своему открытому поручению `{operation_key, text, ask_ttl_s?}`
    (`ask_ttl_s`: 0 — без срока, 60…7200, по умолчанию 1800) -> строка вопроса (как `POST /owner_channel/talk`).
    Повтор с тем же `operation_key` — тот же вопрос. Поручения нет — 404, закрыто — 409."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    return await _write(
        request,
        lambda conn: zq.errand_ask(
            conn,
            errand_id,
            operation_key=body.operation_key,
            text=body.text,
            ask_ttl_s=body.ask_ttl_s,
            principal=principal,
            now_ms=now_ms,
            audit=audit,
        ),
    )


class ErrandTellIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные пределы (ключ 1–100, текст 1–8000) проверяет ядро — русским текстом.
    operation_key: StrictStr = Field(max_length=zq.ASK_OP_KEY_MAX * 4)
    text: StrictStr = Field(max_length=talk_mod.TEXT_MAX * 4)


@public_router.post("/owner_channel/errands/{errand_id}/tell")
async def post_owner_errand_tell(
    body: ErrandTellIn,
    request: Request,
    response: Response,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _agent,
) -> dict:
    """Ф94-1 (A.2): агент сообщает владельцу по своему открытому поручению `{operation_key, text}` — без ожидания
    ответа; бот владельца шлёт сообщение в Telegram один раз -> строка ленты. Повтор с тем же `operation_key` — та
    же строка. Поручения нет — 404, закрыто — 409 (итог шлётся ДО закрытия)."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    return await _write(
        request,
        lambda conn: zq.errand_tell(
            conn,
            errand_id,
            operation_key=body.operation_key,
            text=body.text,
            principal=principal,
            now_ms=now_ms,
            audit=audit,
        ),
    )


class HandsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: Literal["owner_channel", "agent"]
    # Точные пределы (до 2000) проверяет ядро — русским текстом.
    note: StrictStr | None = Field(default=None, max_length=zq.HANDS_NOTE_MAX * 4)
    reply_to: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)  # только бот: ответ свайпом (П2)
    tg_message_id: StrictInt | None = Field(default=None, ge=1, le=2**62)
    hands_s: StrictInt | None = Field(default=None, ge=0, le=2**40)  # секунды `hands_ts` с кнопки бота (П8)


@public_router.post("/owner_channel/errands/{errand_id}/hands")
async def post_owner_errand_hands(
    body: HandsIn,
    request: Request,
    response: Response,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _ui_or_bot,
):
    """Ф96-1 (A.1): владелец или его бот забирает руки у агента посреди поручения (`{to: "owner_channel"}`) или отдаёт
    обратно (`{to: "agent", note?}`) -> `{errand, changed[, repeat]}`; поручение остаётся открытым. Уже так —
    200 без изменений. Кнопка бота со старым `hands_s` — 409 `{detail: "stale: …", errand}`. Закрытое — 409,
    нет — 404, иное тело — 422."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    try:
        return await request.app.state.db.write(
            lambda conn: zq.errand_hands(
                conn,
                errand_id,
                body.to,
                by="ui" if principal.is_ui else "bot",
                principal=principal,
                now_ms=now_ms,
                audit=audit,
                note=body.note,
                reply_to=body.reply_to,
                tg_message_id=body.tg_message_id,
                hands_s=body.hands_s,
            )
        )
    except zq.HandsStale as exc:
        return JSONResponse(status_code=409, content={"detail": exc.detail, "errand": exc.errand})
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


class HandoffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные пределы (ключ 1–100, `why` 1–500) проверяет ядро — русским текстом.
    operation_key: StrictStr = Field(max_length=zq.ASK_OP_KEY_MAX * 4)
    why: StrictStr = Field(max_length=zq.HANDS_WHY_MAX * 4)


@public_router.post("/owner_channel/errands/{errand_id}/handoff")
async def post_owner_errand_handoff(
    body: HandoffIn,
    request: Request,
    response: Response,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _agent,
) -> dict:
    """Ф96-1 (A.2): агент застрял и отдаёт руки владельцу `{operation_key, why}` — руки у владельца и сообщение
    «Передаю тебе: <why>» (бот его доставит) -> `{errand, talk[, repeat]}`. Повтор с тем же `operation_key` —
    та же реплика, руки не трогаются. Поручения нет — 404, закрыто — 409, пустой `why` — 422."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    return await _write(
        request,
        lambda conn: zq.errand_handoff(
            conn,
            errand_id,
            operation_key=body.operation_key,
            why=body.why,
            principal=principal,
            now_ms=now_ms,
            audit=audit,
        ),
    )


@public_router.get("/owner_channel/errands/{errand_id}/state")
async def get_owner_errand_state(
    request: Request,
    errand_id: int = Path(ge=1, le=_ID_MAX),
    target: str | None = Query(None, min_length=3, max_length=_REF_MAX),
    principal: Principal = _agent,
) -> dict:
    """Ф85-1 (F): состояние поручения агента (и закрытого) -> `{id, state: open|closed, can_act, target_ref,
    closed_by, closed_ts, result, returned}`. `target` — цель, которую агент помнит: переехала — `can_act`
    false. Ничего не меняет. Ф96-1: ещё `hands`, `hands_by`, `hands_ts`, `hands_why`, `hands_note`."""
    now_ms = _now_ms(request)
    presence = _presence(request, now_ms)
    try:
        with request.app.state.db.reader() as conn:
            return zq.errand_state(
                conn, errand_id, principal, now_ms=now_ms, presence=presence, target=target
            )
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.get("/owner_channel/closed")
async def get_owner_closed(
    request: Request, limit: int = Query(50, ge=1, le=zq.CLOSED_MAX), principal: Principal = _ui
) -> dict:
    """Последние закрытые строки очереди с источником ответа — только владельцу."""
    with request.app.state.db.reader() as conn:
        return {"items": zq.closed(conn, limit=limit)}


# --- разговор владельца с агентом (Ф58-1, docs/architecture.md.2) --------------------------


class TalkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные пределы (8000 символов, `ref` до 64, `reply_to` существует) проверяет ядро — русским текстом.
    text: StrictStr = Field(max_length=talk_mod.TEXT_MAX * 4)
    ref: StrictStr | None = Field(default=None, max_length=talk_mod.REF_MAX * 4)
    reply_to: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    # Ф61-1 П2: вопрос агента владельцу и срок ожидания (60…7200 с, по умолчанию 1800; Ф85-1: 0 — без срока); у
    # куки `ask` — 422.
    ask: StrictBool = False
    ask_ttl_s: StrictInt | None = Field(default=None, ge=-(2**31), le=2**31)
    # Ф66-2 (A.2): ветка — id сессии хаба или `null` («Общее»); не передан — ветку выбирает ядро.
    session_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    # 23.10: id файлов из `POST /owner_channel/talk/files` (только кука; с файлами текст может быть пустым).
    files: list[StrictStr] | None = Field(default=None, max_length=held_files.SAY_MAX_FILES * 10)


class TalkReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upto: StrictInt = Field(ge=0, le=_ID_MAX)
    # Ф66-2: читать только ветку (id сессии хаба или `null` — «Общее»); не передан — вся лента.
    session_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)


class SummaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точный предел (6000) и `upto_id` по ветке проверяет ядро — русским текстом.
    summary: StrictStr = Field(max_length=talk_mod.SUMMARY_MAX * 4)
    upto_id: StrictInt = Field(ge=0, le=_ID_MAX)


def _scope(principal: Principal):
    """Скоуп проектов для веток: кука — все, агент — его `project_ids` (`None` — все)."""
    return None if principal.is_ui else principal.project_ids


def _body_branch(body: BaseModel):
    """`session_id` из тела: передан (`null` или число) — значение; не передан — `UNSET`."""
    return body.session_id if "session_id" in body.model_fields_set else talk_mod.UNSET


def _branch_param(raw: str | None):
    try:
        return talk_mod.parse_branch(raw)
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.get("/owner_channel/talk")
async def get_owner_talk(
    request: Request,
    before: int | None = Query(None, ge=1, le=_ID_MAX),
    limit: int = Query(talk_mod.LIST_DEFAULT, ge=1, le=talk_mod.LIST_MAX),
    session_id: str | None = Query(None, min_length=1, max_length=20),
    principal: Principal = _any,
) -> dict:
    """Лента разговора `{items, has_more}`: последние `limit` реплик до `before` (не включая), новые снизу.
    Ф66-2: `session_id=<id>|general` — только ветка; без него — вся лента (старый клиент). Ветка сессии,
    которой нет или вне скоупа токена, — 404."""
    branch = _branch_param(session_id)
    try:
        with request.app.state.db.reader() as conn:
            if branch is not talk_mod.ALL:
                talk_mod.check_branch(conn, branch, project_ids=_scope(principal))
            return talk_mod.listing(conn, before=before, limit=limit, branch=branch)
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.get("/owner_channel/talk/branches")
async def get_owner_talk_branches(request: Request, principal: Principal = _any) -> dict:
    """Ф66-2 (A.3): ветки разговора `{items: [{session_id, title, project_id, role, last_id, last_ts,
    unread}]}` — где есть реплики, плюс «Общее» (`session_id: null`) всегда; с непрочитанным сверху, дальше по
    `last_ts`."""
    with request.app.state.db.reader() as conn:
        return {"items": talk_mod.branches(conn, project_ids=_scope(principal))}


def _summary_key(branch: str):
    key = _branch_param(branch)
    if key is talk_mod.ALL:
        raise HTTPException(422, "branch: номер сессии хаба или general")
    return key


@public_router.get("/owner_channel/talk/branches/{branch}/summary")
async def get_owner_talk_summary(
    request: Request, branch: str = Path(min_length=1, max_length=20), principal: Principal = _any
) -> dict:
    """Ф66-2: пересказ ветки (`<id сессии>` или `general`) -> `{session_id, summary, upto_id, updated_ts}`."""
    key = _summary_key(branch)
    try:
        with request.app.state.db.reader() as conn:
            talk_mod.check_branch(conn, key, project_ids=_scope(principal))
            return talk_mod.summary(conn, key)
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.put("/owner_channel/talk/branches/{branch}/summary")
async def put_owner_talk_summary(
    body: SummaryIn,
    request: Request,
    branch: str = Path(min_length=1, max_length=20),
    principal: Principal = _agent,
) -> dict:
    """Ф66-2: агент сохраняет пересказ ветки `{summary, upto_id}` (до 6000; `upto_id` не дальше последней
    реплики ветки и не меньше прежнего — иначе 422) -> то же, что GET."""
    key = _summary_key(branch)
    now_ms = _now_ms(request)
    scope = _scope(principal)

    def _fn(conn):
        talk_mod.check_branch(conn, key, project_ids=scope)
        return talk_mod.save_summary(conn, key, text=body.summary, upto_id=body.upto_id, now_ms=now_ms)

    return await _write(request, _fn)


@public_router.post("/owner_channel/talk")
async def post_owner_talk(
    body: TalkIn, request: Request, response: Response, principal: Principal = _any
) -> dict:
    """Реплика `{text, ref?, reply_to?, ask?, ask_ttl_s?, session_id?}` -> строка. Кука пишет от владельца,
    агент — от себя; `ask` (вопрос владельцу) — только агент. Ф66-2: `session_id` (число или `null`) — ветка
    явно; не передан — ветка `reply_to`, иначе сессия карточки `ref`, иначе «Общее». Нет такой сессии хаба
    (или вне скоупа токена) — 422."""
    source = _source(principal)
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    if source == "ui":
        touch_activity(request, principal)
    author = "owner_channel" if source == "ui" else "agent"
    return await _write(
        request,
        lambda conn: talk_mod.post(
            conn,
            author=author,
            text=body.text,
            ref=body.ref,
            reply_to=body.reply_to,
            ask=body.ask,
            ask_ttl_s=body.ask_ttl_s,
            session_id=_body_branch(body),
            project_ids=_scope(principal),
            files=body.files,
            now_ms=now_ms,
            audit=audit,
        ),
    )


# --- 23.10: файлы реплик разговора ---------------------

_TALK_FILE_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"


@public_router.post("/owner_channel/talk/files")
async def post_owner_talk_file(
    request: Request, name: str = Query("", max_length=4000), principal: Principal = _ui
) -> dict:
    """Файл к будущей реплике владельца: тело — сырые байты, ответ — `{id, name, mime, size, kind}`; дальше id идёт
    в `files` у `POST /owner_channel/talk`. Пределы и отказы — как у файла сессии (`POST /sessions/{id}/files`)."""
    db = request.app.state.db
    declared = sessions_routes.declared_length(request)
    try:
        with db.reader() as conn:
            held_files.talk_room(conn)
    except SessionError as exc:
        raise HTTPException(exc.status, str(exc.detail)) from exc
    fields = await sessions_routes.receive_file(request, declared, name)
    now_ms = _now_ms(request)
    touch_activity(request, principal)
    try:
        return await _write(
            request,
            lambda conn: held_files.talk_commit(conn, file_id=held_files.new_id(), now_ms=now_ms, **fields),
        )
    finally:
        fields["tmp"].unlink(missing_ok=True)  # принят — уже переименован; отказ — убрать


@public_router.delete("/owner_channel/talk/files/{file_id}")
async def delete_owner_talk_file(file_id: str, request: Request, principal: Principal = _ui) -> dict:
    """Убрать неприложенный файл. Приложенный — 409; нет такого — 404."""
    await _write(request, lambda conn: held_files.talk_remove(conn, file_id))
    return {"id": file_id}


@public_router.get("/owner_channel/talk/files/{file_id}")
async def get_owner_talk_file(file_id: str, request: Request, principal: Principal = _any):
    """Байты приложенного к реплике файла, потоком: владельцу — показать (картинка — `inline` со своим типом,
    прочее — скачать), агенту — подать модели. Неприложенный, стёртый, чужой id — 404."""
    try:
        with request.app.state.db.reader() as conn:
            path, size, mime, kind, name = held_files.talk_file(conn, file_id)
    except SessionError as exc:
        raise HTTPException(exc.status, str(exc.detail)) from exc
    try:
        f = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb")
    except OSError as exc:
        raise HTTPException(404, "not found") from exc
    if os.fstat(f.fileno()).st_size != size:
        f.close()
        raise HTTPException(404, "not found")
    headers = {
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _TALK_FILE_CSP,
        "Cache-Control": "private, max-age=86400",
    }
    if kind == "image":
        headers["Content-Disposition"] = "inline"
        media_type = mime
    else:
        headers["Content-Disposition"] = media_routes.content_disposition(name, 0)
        media_type = "application/octet-stream"
    return StreamingResponse(sessions_routes._file_chunks(f), media_type=media_type, headers=headers)  # noqa: SLF001


@public_router.get("/owner_channel/talk/open")
async def get_owner_talk_open(request: Request, principal: Principal = _agent) -> dict:
    """Агент: реплики владельца без отметки «обработано» `{items}`, по возрастанию id."""
    with request.app.state.db.reader() as conn:
        return {"items": talk_mod.open_replies(conn)}


@public_router.post("/owner_channel/talk/read")
async def post_owner_talk_read(body: TalkReadIn, request: Request, principal: Principal = _ui) -> dict:
    """Владелец прочёл реплики агента до `upto` включительно -> `{read}`. Ф66-2: `session_id` (число или
    `null` — «Общее») — только в этой ветке; не передан — во всей ленте."""
    now_ms = _now_ms(request)
    touch_activity(request, principal)
    branch = _body_branch(body)
    branch = talk_mod.ALL if branch is talk_mod.UNSET else branch
    return await _write(
        request, lambda conn: talk_mod.mark_read(conn, body.upto, now_ms=now_ms, branch=branch)
    )


@public_router.post("/owner_channel/talk/{talk_id}/handled")
async def post_owner_talk_handled(
    request: Request, talk_id: int = Path(ge=1, le=_ID_MAX), principal: Principal = _agent
) -> dict:
    """Агент обработал реплику владельца -> строка. Повтор — 200 без изменений; реплика агента — 409."""
    now_ms = _now_ms(request)
    return await _write(request, lambda conn: talk_mod.mark_handled(conn, talk_id, now_ms=now_ms))


class TalkProgressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(max_length=talk_mod.PROGRESS_MAX * 4)


@public_router.put("/owner_channel/talk/{talk_id}/progress")
async def put_owner_talk_progress(
    body: TalkProgressIn,
    request: Request,
    talk_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _agent,
) -> dict:
    """Строка хода агента у реплики владельца (план, шаги, время) -> строка реплики. Уже обработана — 409."""
    now_ms = _now_ms(request)
    return await _write(
        request, lambda conn: talk_mod.set_progress(conn, talk_id, text=body.text, now_ms=now_ms)
    )


# --- Ф61-1: вопросы агента в Telegram и почта агенту (docs/architecture.md) -------------


class TalkClosedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: StrictStr = Field(max_length=16)


class BotReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_to: StrictInt = Field(ge=1, le=_ID_MAX)
    text: StrictStr = Field(max_length=talk_mod.TEXT_MAX * 4)
    tg_message_id: StrictInt = Field(ge=1, le=2**62)


@public_router.post("/owner_channel/talk/{talk_id}/closed")
async def post_owner_talk_closed(
    body: TalkClosedIn,
    request: Request,
    talk_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _agent,
) -> dict:
    """П2: агент закрывает свой вопрос `{outcome: answered|gave_up}` -> строка. Повтор — 200 без изменений."""
    now_ms = _now_ms(request)
    return await _write(request, lambda conn: talk_mod.close_ask(conn, talk_id, body.outcome, now_ms=now_ms))


@public_router.get("/owner_channel/talk/{talk_id}/answer")
async def get_owner_talk_answer(
    request: Request, talk_id: int = Path(ge=1, le=_ID_MAX), principal: Principal = _agent
) -> dict:
    """Ф85-1 (A.2): ответ владельца на вопрос агента -> `{id, state: open|answered|expired|closed, answer}`;
    `answer` — последняя реплика владельца с `reply_to = id` (строка ленты) или `null`, отметка «обработано» не
    мешает. Не вопрос агента или нет такой реплики — 404."""
    now_ms = _now_ms(request)
    try:
        with request.app.state.db.reader() as conn:
            return talk_mod.answer_of(conn, talk_id, now_ms=now_ms)
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.get("/owner_channel/talk/asks")
async def get_owner_talk_asks(
    request: Request,
    since_ms: int = Query(ge=0, le=2**62),
    principal: Principal = _bot,
) -> dict:
    """Бот: вопросы агента не старше `since_ms` (старше суток — подрезается, П1) ->
    `{items: [{id, ts, text, ref, ref_title, state, errand_id, hands, hands_ts}]}`, по возрастанию id
    (Ф96-1: поручение по карточке вопроса и руки, нет поручения — `null`)."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        items = talk_mod.asks(
            conn,
            since_ms=since_ms,
            now_ms=now_ms,
            titles=lambda refs: zq.card_titles(conn, refs, now_ms=now_ms),
            errands=lambda refs: zq.errand_marks(conn, refs),
        )
    return {"items": items}


@public_router.get("/owner_channel/talk/tells")
async def get_owner_talk_tells(
    request: Request,
    since_ms: int = Query(ge=0, le=2**62),
    principal: Principal = _bot,
) -> dict:
    """Ф94-1 (A.3) бот: сообщения агента владельцу (`tell`) не старше `since_ms` (старше суток — подрезается) ->
    `{items: [{id, ts, text, ref, ref_title, errand_id, hands, hands_ts, handoff}]}`, по возрастанию id
    (Ф96-1: поручение по карточке и руки, `handoff` — это «Передаю тебе» агента)."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        items = talk_mod.tells(
            conn,
            since_ms=since_ms,
            now_ms=now_ms,
            titles=lambda refs: zq.card_titles(conn, refs, now_ms=now_ms),
            errands=lambda refs: zq.errand_marks(conn, refs),
        )
    return {"items": items}


@public_router.post("/owner_channel/talk/bot-reply")
async def post_owner_talk_bot_reply(
    body: BotReplyIn, request: Request, response: Response, principal: Principal = _bot
):
    """Бот: ответ владельца из Telegram на вопрос агента `{reply_to, text, tg_message_id}` -> строка. Повтор с
    тем же `tg_message_id` — та же строка (П3). Вопрос не открыт — 409 `{detail, state}`; не вопрос — 409."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    try:
        return await request.app.state.db.write(
            lambda conn: talk_mod.bot_reply(
                conn,
                reply_to=body.reply_to,
                text=body.text,
                tg_message_id=body.tg_message_id,
                now_ms=now_ms,
                audit=audit,
            )
        )
    except talk_mod.AskNotOpen as exc:
        return JSONResponse(status_code=409, content={"detail": exc.detail, "state": exc.state})
    except zq.OwnerError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@public_router.post("/owner_channel/bot-dictate")
async def post_owner_bot_dictate(request: Request, principal: Principal = _bot) -> dict:  # noqa: ARG001
    """Бот: голосовой ответ владельца — кусок LPCM 16 кГц Int16LE моно -> `{text}`. Проверки, пределы и коды —
    как у `POST /dictate`; звук и текст не хранятся и в журнал не пишутся."""
    from .dictate import recognize_body  # здесь: dictate тянет речевые модули только когда ручка нужна

    return await recognize_body(request)


class CardReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: StrictStr = Field(min_length=1, max_length=_REF_MAX)
    text: StrictStr = Field(min_length=1, max_length=zq.MONEY_REPLY_MAX)
    tg_message_id: StrictInt = Field(ge=1, le=2**62)


@public_router.get("/owner_channel/money")
async def get_owner_money(
    request: Request,
    since_ms: int = Query(ge=0, le=2**62),
    principal: Principal = _bot,
) -> dict:
    """Ф93-1 (B.1), бот: денежные карточки `turn` и `agent_msg` не старше `since_ms` (старше суток —
    подрезается): открытые — у владельца по строке «Деньги», закрытые — все -> `{items: [{ref, ts, kind, project,
    title, text, state}]}`."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        items = zq.money_cards(conn, since_ms=since_ms, now_ms=now_ms)
    return {"items": items}


@public_router.get("/owner_channel/returned")
async def get_owner_returned(
    request: Request,
    since_ms: int = Query(ge=0, le=2**62),
    principal: Principal = _bot,
) -> dict:
    """Ф93-1 (F2), бот: карточки, которые агент вернул владельцу («вернул», «завис», «не вышло»), не старше
    `since_ms` (старше суток — подрезается) -> `{items: [{id, ref, ts, kind, project, title, reason, detail,
    text, replyable, state}]}`."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        items = zq.returned_cards(conn, since_ms=since_ms, now_ms=now_ms)
    return {"items": items}


@public_router.post("/owner_channel/card-reply")
async def post_owner_card_reply(
    body: CardReplyIn, request: Request, response: Response, principal: Principal = _bot
):
    """Ф93-1 (B.1, F2), бот: ответ владельца из Telegram `{ref, text, tg_message_id}` по карточке `turn` /
    `agent_msg`, которая у него — денежная или возвращённая агентом, — = его ответ в интерфейсе (`turn` —
    реплика сессии, `agent_msg` — «ОТ ВЛАДЕЛЬЦА» автору) -> `{ref, status, repeat}`. Повтор с тем же
    `tg_message_id` — тот же ответ без второй реплики. Карточка закрыта, не денежная и не возвращённая или
    не у владельца — 409 `{detail, state}`; нет — 404."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, _request_id(response))
    presence = _presence(request, now_ms)
    waiting = _waiting(request)
    try:
        return await request.app.state.db.write(
            lambda conn: zq.card_reply(
                conn,
                ref=body.ref,
                text=body.text,
                tg_message_id=body.tg_message_id,
                principal=principal,
                now_ms=now_ms,
                audit=audit,
                waiting=waiting,
                presence=presence,
            )
        )
    except zq.CardNotOpen as exc:
        return JSONResponse(status_code=409, content={"detail": exc.detail, "state": exc.state})
    except Exception as exc:
        mapped = zq.error_of(exc)
        if mapped is None:
            raise
        raise HTTPException(mapped[0], mapped[1]) from exc
