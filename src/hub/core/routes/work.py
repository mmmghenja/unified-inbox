"""Ручки хоста сессий хаба `/api/v1/work/*` и «Снять паузу лимита». Задачи по «Го» (Ф9-1) убраны в Ф78-1;
ручки постоянных сессий хаба — `routes/sessions.py` (зависимость хоста `_host_dep` — отсюда).

Кто что может:
- `POST /work/pause/clear` — ТОЛЬКО UI-сессия (cookie + CSRF middleware) или агент владельца (Ф88-1);
- хост (`/work/host/*`) — bearer со скоупом `work_host`."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from hub.engine import registry as engine_registry

from .. import engine as engine_mod
from .. import engine_fallback as engine_fallback_mod
from .. import engine_models as engine_models_mod
from .. import held as held_mod
from .. import held_signals as held_signals_mod
from .. import held_work as held_work_mod
from .. import queries
from ..audit import write_audit
from ..security import Principal, principal_dependency
from .write import ui_or_agent

router = APIRouter(tags=["work"])

_host_scope = principal_dependency("work_host")

WAIT_MAX_S = 60.0


async def _host(request: Request) -> Principal:
    """`work_host` — только bearer (UI-сессии этот скоуп не выдаётся никогда; проверка — на всякий случай)."""
    principal = await _host_scope(request)
    if principal.is_ui:
        raise HTTPException(403, "forbidden")
    return principal


_host_dep = Depends(_host)


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


def _request_id(response: Response) -> str:
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    return request_id


async def _run(request: Request, fn):
    """Одна транзакция; «не найдено» — 404."""
    try:
        return await request.app.state.db.write(fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


def _audit(conn, request: Request, principal: Principal, request_id: str, *, action: str, result, detail):
    write_audit(
        conn,
        now_ms=_now_ms(request),
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=_client_name(request),
        request_id=request_id,
        action=action,
        result=result,
        detail=detail,
    )


@router.post("/work/pause/clear")
async def post_work_pause_clear(
    request: Request, response: Response, principal: Principal = ui_or_agent
) -> dict:
    """Ф66-3 F: «Снять паузу лимита» — владелец сбросил лимит раньше времени Claude. Ядро пишет отметку
    (`main.meta` `work_pause_cleared_ts`) и аудит; хост видит её в снимке ожидания и убирает паузы и
    «работу сверх подписки». Лимит ещё не сброшен — Claude ответит «лимит», и пауза встанет заново. Только
    кука интерфейса или агент владельца (Ф88-1)."""
    request_id = _request_id(response)
    now_ms = _now_ms(request)

    def _fn(conn):
        engine_mod.set_pause_cleared(conn, now_ms)
        _audit(conn, request, principal, request_id, action="work_pause_clear", result="changed", detail={})

    await _run(request, _fn)
    return {"ok": True, "cleared_ts": now_ms}


@router.post("/work/host/recover")
async def post_host_recover(request: Request, principal: Principal = _host_dep) -> dict:
    """Старт хоста до Ф78-1 возвращал здесь задачи по «Го» в `idle`; задач больше нет — ручка оставлена для
    хоста прежней версии (на время выкладки) и ничего не делает. Сессии хаба — `/host/sessions/recover`."""
    return {"touched": 0}


@router.get("/work/host/open")
async def get_host_open(request: Request, principal: Principal = _host_dep) -> dict:
    """Старт хоста: текущий `max_seq`, движок (Ф16-1, П11), отметка «паузу лимита снял владелец» и незакрытые
    сессии хаба с ждущими сигналами."""
    with request.app.state.db.reader() as conn:
        max_seq = queries.get_healthz(conn, 0)["max_seq"]
        engine = engine_mod.get_engine(conn)
        pause_cleared_ts = engine_mod.get_pause_cleared(conn)
        sessions = held_work_mod.host_open(conn)  # Ф41-2б (3.2): незакрытые постоянные сессии хаба
        # Ф41-4 П2: что уже ждёт сессии (после выключения — сигналы, пришедшие без хоста).
        now_ms = _now_ms(request)
        sessions["signals"] = held_signals_mod.snapshot(
            conn, now_ms=now_ms, presence=_presence(request, now_ms)
        )
    return {
        "max_seq": max_seq,
        "engine": engine,
        "sessions": sessions,
        "pause_cleared_ts": pause_cleared_ts,
    }


def _presence(request: Request, now_ms: int) -> list[dict]:
    store = getattr(request.app.state, "presence", None)
    if store is None:
        return []
    return [{"channel_id": c, "thread_ext_id": t} for c, t in store.items(now_ms)]


def _signals_known(raw: str | None) -> dict[int, int] | None:
    """Ф41-4 П2: `signals_known=id:число,id:число` — готовность, о которой хост уже знает. Параметра нет —
    `None` (хост сигналов не понимает: ожидание по ним не будит); мусор в паре — пара пропускается."""
    if raw is None:
        return None
    known: dict[int, int] = {}
    for pair in raw.split(","):
        head, sep, tail = pair.partition(":")
        if sep and head.strip().isdigit() and tail.strip().isdigit():
            known[int(head)] = int(tail)
    return known


class EngineModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: str = Field(min_length=1, max_length=16)
    models: list[dict] = Field(max_length=engine_models_mod.MAX_MODELS * 2)


@router.post("/work/host/engine-models")
async def post_host_engine_models(
    body: EngineModelsIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Ф74-5б: живой список моделей движка сессий хаба (`model/list` Codex) — в профиль `/status` полем
    `models`. Движок не из сессий хаба — 422."""
    if engine_registry.held(body.engine) is None:
        raise HTTPException(422, "engine: не движок сессий хаба")
    now_ms = _now_ms(request)
    saved = await _run(
        request, lambda conn: engine_models_mod.store(conn, body.engine, body.models, now_ms=now_ms)
    )
    return {"engine": body.engine, "models": len(saved)}


@router.get("/work/host/wait")
async def get_host_wait(
    request: Request,
    since: int = Query(0, ge=0, le=2**63 - 1),
    timeout_s: float = Query(25.0, ge=0, le=WAIT_MAX_S),
    engine: str | None = Query(None, max_length=16),
    pause_cleared: int | None = Query(None, ge=0, le=2**63 - 1),
    signals_known: str | None = Query(None, max_length=4000),
    pauses: str | None = Query(None, max_length=4000),
    principal: Principal = _host_dep,
) -> dict:
    """Ф41-4 (2.3, П2, П11, П12): раздел `sessions.signals` — `[{"id", "ready", "next_ms"}]` по сессиям хаба
    с ждущими сигналами (`ready` — годное к выдаче сейчас). Хост шлёт `signals_known` — что он уже знает;
    ожидание будит отличие `ready` от известного (нет в списке — известно 0) и не спит дольше ближайшего
    `next_ms` (конец дебаунса или пачки). Подписки сессий хаба на беседы в архиве и перенесённые — завершаются
    здесь же, как в цикле `/chan-subs/wait`. Каждый круг ожидания отмечает «хост слушает» (`listening`).

    Длинное ожидание будит всё, что двигает `seq` сессии хаба или её запроса. Пусто до `timeout_s` — пустой
    ответ с тем же `max_seq`.

    Ф83-1 (П9): `pauses` — JSON `{движок: {until, kind, since}}` действующих пауз хоста; ядро хранит их
    (`main.meta` `work_host_pauses`) для «Вернуть на основной» и ответа «Перейти на …» карточки паузы.

    Ф16-1 (3.1): `engine` — движок, о котором знает хост. Не совпал с нынешним — ответ сразу (смена движка
    не двигает `seq`, без этого хост ждал бы до таймаута)."""
    db = request.app.state.db
    notifier = request.app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)
    deadline = loop.time() + timeout_s
    known = _signals_known(signals_known)
    # Ф83-1 (П9): паузы движков хоста — в ядро (запись только при отличии; ожидание она не будит).
    host_pauses = engine_fallback_mod.parse_host_pauses(pauses)
    if host_pauses is not None:
        # Ревью Ф83: сверка до записи — пустая транзакция будила бы все долгие ожидания на каждом круге хоста.
        with db.reader() as conn:
            same = engine_fallback_mod.same_host_pauses(conn, host_pauses)
        if not same:
            await db.write(lambda conn: engine_fallback_mod.set_host_pauses(conn, host_pauses))
    while True:
        gen = notifier.gen
        now_ms = _now_ms(request)
        held_mod.host_waited(now_ms)  # П11: хост слушает
        presence = _presence(request, now_ms)
        with db.reader() as conn:
            snapshot = held_work_mod.host_wait_snapshot(conn, since=since)
            signals = held_signals_mod.snapshot(conn, now_ms=now_ms, presence=presence)
            actions = held_signals_mod.chan_actions(conn) if not notifier.closed else []
        if actions:
            # П12: архив и перенос беседы завершают подписку сессии хаба на неё не позже этого круга.
            await db.write(
                lambda conn, actions=actions, now_ms=now_ms: held_signals_mod.apply_chan_actions(
                    conn, actions, now_ms=now_ms
                )
            )
            continue
        snapshot["sessions"]["signals"] = signals
        signals_changed = False
        next_ms: int | None = None
        if known is not None:
            ready_now = {item["id"]: item["ready"] for item in signals}
            signals_changed = any(
                ready_now.get(sid, 0) != known.get(sid, 0) for sid in set(ready_now) | set(known)
            )
            pending_ms = [item["next_ms"] for item in signals if item["next_ms"] is not None]
            next_ms = min(pending_ms) if pending_ms else None
        remaining = deadline - loop.time()
        engine_changed = engine is not None and engine != snapshot["engine"]
        # Ф66-3 F: владелец снял паузу лимита — ответ сразу, хост убирает паузу и будит круг.
        engine_changed = engine_changed or (
            pause_cleared is not None and pause_cleared != snapshot["pause_cleared_ts"]
        )
        # Ф41-2б (3.2): ожидание просыпается и по постоянным сессиям хаба (реплика владельца, решение карточки,
        # «прервать», «усыпить», закрытие).
        held = snapshot["sessions"]["items"] or snapshot["sessions"]["requests"]
        if held or engine_changed or signals_changed or remaining <= 0:
            return snapshot
        wait_s = remaining if next_ms is None else min(remaining, max(next_ms / 1000, 0.05))
        await notifier.wait(gen, wait_s)
        if notifier.closed:
            return snapshot  # Ф31-1 П5: ядро останавливается — пусто, ничего не выдавая
