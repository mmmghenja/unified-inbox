"""Ручки сессий хаба (Ф41-2а, docs/architecture.mdв, П8, П10).
Маршруты не пишут SQL: записи — `hub_sessions.py` внутри `Db.write()`.

Кто что может:
- создать и закрыть (`POST /sessions`, `POST /sessions/{id}/close`) — ТОЛЬКО владелец из интерфейса (кука +
  CSRF); bearer любого скоупа, включая `work_host`, — 403 и аудит `ui_only_denied`. С удалённого слушателя —
  можно, как прочие действия владельца;
- читать (`GET /sessions`) — кука UI или bearer со скоупом `read` (только проекты токена, П10); `workdir` —
  только куке UI; настоящий процесс не отдаётся никому, кроме хоста;
- хост (`/work/host/sessions…`, bearer `work_host`) — только привязка разговора движка и состояние. Создать,
  закрыть, сменить роль или режим хост НЕ может.

Ф41-2б (docs/architecture.md) — работа сессии,
`held_work.py`:
- Владелец (кука + CSRF; bearer — 403 и аудит): реплика в очередь, «прервать», «усыпить», решение карточки;
- читать запись с ждущими запросами и очередью (`GET /sessions/{id}`) и ленту событий — кука UI или bearer
  `read` в пределах проектов токена (чужой проект — 404);
- хост: события, запросы, доставка решений и реплик, ход (`turn`), восстановление (`recover`). Написать
  сессии на вход, решить карточку, прервать или усыпить хост НЕ может.

Отказы 409/410/422 несут готовый русский текст в `detail` — интерфейс показывает его как есть.

Реестр в памяти (`app.state.held`) меняется здесь ПОСЛЕ `await db.write(...)` (П2в): откат транзакции его не
трогает; разрушающие решения ядро принимает по базе, реестр — для быстрых отказов и показа.

Ф41-3в (docs/architecture.md) — файлы во вход сессии, `held_files.py`:
- Владелец (кука + CSRF; bearer — 403 и аудит): загрузить файл в сессию, убрать неприложенный, приложить к реплике
  (`files` в `say`). Приём — потоком на диск: sha256 и размер считаются по ходу, заявленному `Content-Length`
  не верим (лишний байт — обрыв), тип — по сигнатуре байтов, имя чистится;
- хост: забрать байты файла, приложенного к реплике (`GET /work/host/session-files/{id}`, потоком). Загрузить,
  убрать или приложить файл хост НЕ может; неприложенный, стёртый и файл закрытой сессии ему не отдаются.

Ф88-1 (docs/architecture.md): всё, что выше названо «только владелец (кука)», кроме
решения карточки (`/session-requests/{id}/decide` — агенту штатный путь `/owner_channel/req:{id}/answer`) и потока
`/sessions/live`, может и его агент (bearer ровно `human`): `ui_or_agent` / `read_or_agent`, те же тела."""

import asyncio
import hashlib
import logging
import os
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette.requests import ClientDisconnect

from hub import roles as roles_mod
from hub.engine import failure
from hub.engine import registry as engine_registry

from .. import adopt as adopt_mod
from .. import decisions as decisions_mod
from .. import engine_fallback, held_files, held_signals, held_work, hub_sessions, session_always, subs
from .. import handoff as handoff_mod
from .. import held as held_mod
from .. import interview as interview_mod
from .. import live as live_mod
from .. import media as media_mod
from .. import owner_queue as zq
from .. import transcript as transcript_mod
from ..security import Principal, principal_dependency
from .sync import _SSE_HEADERS, session_checker
from .work import _host_dep
from .write import read_or_agent, ui_only, ui_or_agent

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])

_authed = Depends(principal_dependency())

# Ф74-1: поле `engine` — движок сессий хаба по профилю (`engine/registry.py`), а не список в `Literal`.
HeldEngine = Annotated[StrictStr, AfterValidator(engine_registry.check_held)]

_ID_MAX = held_mod.ID_MAX
_PID_MAX = 2**31 - 1
_PROC_START_MAX = 2**62
_SEQ_MAX = 2**63 - 1
_USAGE_TOKENS_MAX = 10**12
_USAGE_COST_MAX = 10**6
# Ф56-1б (3.2): пределы `turn_result.answer` с запасом над пределами хоста (64 000 полного, 1000 краткого) —
# точные пределы держит ядро (`owner_queue.TURN_FULL_MAX`, `TURN_BRIEF_MAX`).
_TURN_FULL_RAW_MAX = 80_000
_TURN_BRIEF_RAW_MAX = 4_000


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


def _as_owner(principal: Principal) -> bool:
    """Ф88-1 (C3, П7): кука владельца или его агент — вид как у интерфейса (все проекты, `today`, без
    `sub_stale`)."""
    return principal.is_ui or zq.is_agent(principal)


def _audit(request: Request, principal: Principal) -> dict:
    return {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": _client_name(request),
        "request_id": uuid.uuid4().hex,
    }


async def _role_file_reply(row: dict) -> dict:
    """Ф48-3: ответ записи, где задавался файл роли. Файла сейчас не прочитать (нет, вышел за папку, велик)
    — рядом с записью поле `role_file_warning` (готовая строка); запись при этом принята."""
    path = row.get("role_file")
    if not path or not row.get("workdir"):
        return {"session": row}
    loaded = await asyncio.to_thread(roles_mod.read_file, row["workdir"], path)
    if loaded.reason is None:
        return {"session": row}
    return {"session": row, "role_file_warning": f"файл роли {path}: {loaded.reason}"}


def _http(exc: hub_sessions.SessionError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _waiters(request: Request):
    return getattr(request.app.state, "sub_waiters", None)


def _waiting(request: Request):
    waiters = _waiters(request)
    return waiters.waiting if waiters is not None else None


def _held(request: Request) -> held_mod.Held:
    return request.app.state.held


def _presence(request: Request, now_ms: int) -> list[dict]:
    """Ф41-4: присутствие владельца — годность карточек (в счёте ждущих сигналов и при выдаче хосту)."""
    store = getattr(request.app.state, "presence", None)
    if store is None:
        return []
    return [{"channel_id": c, "thread_ext_id": t} for c, t in store.items(now_ms)]


def _live(request: Request) -> live_mod.LiveHub:
    return request.app.state.live


def _live_follow(request: Request, row: dict) -> None:
    """Ф41-3 (3.1): запись набираемого текста живёт, только пока сессия в ходу; после коммита, как реестр."""
    if row.get("state") != "running" or row.get("closed_ts") is not None:
        _live(request).drop(row["id"])


def _check_id(session_id: int) -> None:
    if not 1 <= session_id <= _ID_MAX:
        raise HTTPException(404, "not found")


def _check_row_id(row_id: int) -> None:
    """id запроса или реплики сессии (в рабочем файле — от 2^40)."""
    if not 1 <= row_id <= _SEQ_MAX:
        raise HTTPException(404, "not found")


async def _write(request: Request, fn):
    """Одна транзакция и перевод отказа модуля в HTTP."""
    try:
        return await request.app.state.db.write(fn)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc


# --- владелец: создать, закрыть -----------------------------------------------------------------------------


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: StrictInt = Field(ge=1, le=2**53)
    # Роль задаёт владелец (форму и запрет зарезервированных проверяет `subs.role_form`); нет — сессия без роли.
    role: str | None = Field(default=None, max_length=128)
    number: StrictInt | None = Field(default=None, ge=1, le=subs.ROLE_NO_MAX)
    engine: HeldEngine
    model: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    effort: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    title: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    # Ф41-4 (2.1): желаемый фильтр подписки — те же поля и пределы, что у `POST /subs`; проверяет и
    # нормализует `subs.want_form` (отказ — готовым русским текстом). `null` или нет поля — не подписывать.
    want_sub: dict | None = None
    # Ф41-5 (3.1): режим — к сессии в `auto` применяются выключатели автопилота.
    mode: Literal["manual", "auto"] = "manual"
    # Ф48-3: файл роли — путь к MD-файлу относительно рабочей папки проекта (форму проверяет
    # `roles.file_form`, отказ — русским текстом); `null` или пустая строка — нет.
    role_file: str | None = Field(default=None, max_length=roles_mod.FILE_PATH_MAX * 4)
    # Ф50-7: подпапка процесса сессии внутри рабочей папки проекта (форму проверяет `roles.subdir_form`,
    # папку на диске — `roles.subdir_check`; отказ — 422 русским текстом); `null`, пустая строка или «.» —
    # корень проекта. Задаётся только здесь: `PATCH` её не принимает.
    subdir: str | None = Field(default=None, max_length=roles_mod.FILE_PATH_MAX * 4)
    # Ф99-1: id прошлого разговора Claude Code папки проекта (`GET /projects/{id}/conversations`) — сессия
    # продолжает его. Только `engine=claude`, без `subdir`; `null` или нет поля — новый разговор.
    resume_conv: str | None = Field(default=None, max_length=64)


class PatchIn(BaseModel):
    """`PATCH /sessions/{id}`: `want_sub` (ключ есть — заменить, и `null`) и/или `mode` (Ф41-5)."""

    model_config = ConfigDict(extra="forbid")

    want_sub: dict | None = None
    mode: Literal["manual", "auto"] | None = None
    # Ф41-7а: модель и усилие (`null` — умолчание движка); применятся со следующего входа.
    model: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    effort: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    # Смена роли (09.10.2026): ключ `role` есть — сменить; `null` или пустая строка — снять. `number` —
    # номер линии, только вместе с непустой ролью (нет — наименьший свободный).
    role: str | None = Field(default=None, max_length=128)
    number: StrictInt | None = Field(default=None, ge=1, le=subs.ROLE_NO_MAX)
    # Сессия отвечает в беседы «без подтверждения» своей роли (список Ф37-1 ведёт владелец). При смене роли
    # сбрасывается в `false`, если здесь не пришло явное `true`.
    autosend_ok: StrictBool | None = None
    # Ф48-2 (раздел D): свой порог авто-сжатия (`null` — умолчание хоста) и выключатель авто-сжатия.
    # Действуют сразу, процесс не перезапускается.
    compact_tokens: StrictInt | None = Field(
        default=None, ge=held_mod.COMPACT_TOKENS_MIN, le=held_mod.COMPACT_TOKENS_MAX
    )
    compact_auto: StrictBool | None = None
    # Ф48-3: файл роли (ключ есть — заменить; `null` или пустая строка — снять). Действует со следующего
    # запуска процесса сессии.
    role_file: str | None = Field(default=None, max_length=roles_mod.FILE_PATH_MAX * 4)
    # Ф83-1: запасной движок сессии — ключ есть: `null` — как у проекта, `{"engine": null}` — нет запасного,
    # `{engine, model?, effort?}` — свой (движок — другой, не тот, на котором сессия; иначе 422).
    fallback: dict | None = None


class HandoffIn(BaseModel):
    """`POST /sessions/{id}/handoff`: чего нет в теле — как у прежней сессии."""

    model_config = ConfigDict(extra="forbid")

    engine: HeldEngine | None = None
    model: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    effort: str | None = Field(default=None, max_length=hub_sessions.FIELD_MAX)
    mode: Literal["manual", "auto"] | None = None
    # Ф50-7 (П5): папка новой сессии; ключа нет — папка прежней, `null` или пустая строка — корень проекта.
    subdir: str | None = Field(default=None, max_length=roles_mod.FILE_PATH_MAX * 4)


@router.post("/sessions")
async def post_session(body: CreateIn, request: Request, principal: Principal = ui_or_agent) -> dict:
    """3.1: новая сессия хаба — запись без процесса (`asleep`), режим `manual`. Линия роли занята с момента
    создания (П3). Ответ — запись."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        rec = hub_sessions.create(
            conn,
            project_id=body.project_id,
            engine=body.engine,
            role=body.role,
            number=body.number,
            model=body.model,
            effort=body.effort,
            title=body.title,
            created_by="ui",
            now_ms=now_ms,
            audit=audit,
            waiting=_waiting(request),
            want_sub=body.want_sub,
            mode=body.mode,
            role_file=body.role_file,
            subdir=body.subdir,
            resume_conv=body.resume_conv,
        )
        return rec.id, hub_sessions.session_json(conn, rec, now_ms=now_ms, waiters=waiters, ui=True)

    try:
        session_id, row = await request.app.state.db.write(_fn)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    _held(request).add(session_id)  # после коммита (П2в)
    reply = await _role_file_reply(row)
    if body.resume_conv:
        hint = await asyncio.to_thread(_resume_hint, row.get("workdir") or "", body.resume_conv, now_ms)
        if hint:
            reply["resume_warning"] = hint
    return reply


def _resume_hint(workdir: str, conv_id: str, now_ms: int) -> str | None:
    """Ф99-1 (П2): разговор менялся последние 2 минуты — возможно, он открыт в другом окне. Только подсказка:
    сам запуск хост не делает, пока `claude agents` видит разговор открытым."""
    path = transcript_mod.conv_path(workdir, conv_id)
    if path is None:
        return None
    try:
        age_ms = now_ms - path.stat().st_mtime_ns // 1_000_000
    except OSError:
        return None
    if age_ms >= hub_sessions.RESUME_FRESH_MS:
        return None
    return (
        "разговор менялся меньше 2 минут назад — если он открыт в другом окне Claude Code, закрой его: "
        "пока он открыт, сессия хаба не запустится"
    )


@router.get("/projects/{project_id}/conversations")
async def get_project_conversations(
    project_id: int, request: Request, principal: Principal = ui_only
) -> dict:
    """Ф99-1: прошлые разговоры Claude Code папки проекта на машине ядра (ядро и хост — одна машина) — для
    «Продолжить разговор» в форме новой сессии. Только кука интерфейса. Свежие сверху, не больше 50: `id`,
    `last_ts` (последняя запись, мс), `size` (байт), `preview` (начало первой реплики человека, маскировано),
    `hub_session_id` — номер незакрытой сессии хаба, которая ведёт этот разговор (`null` — свободен)."""
    _check_id(project_id)
    with request.app.state.db.reader() as conn:
        row = conn.execute("SELECT workdir FROM main.project WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        workdir = row[0] or ""
        bound = dict(
            conn.execute(
                "SELECT engine_session_id, id FROM main.hub_session WHERE closed_ts IS NULL "
                "AND engine_session_id IS NOT NULL"
            ).fetchall()
        )
    if not workdir:
        return {"items": [], "workdir": None}
    items = await asyncio.to_thread(transcript_mod.conversations, workdir)
    for item in items:
        item["hub_session_id"] = bound.get(item["id"])
    return {"items": items, "workdir": workdir}


@router.post("/sessions/{session_id}/close")
async def post_session_close(session_id: int, request: Request, principal: Principal = ui_or_agent) -> dict:
    """3.1 + П8: закрыть насовсем; подписки сессии завершаются в той же транзакции (`ended_by_owner`).
    Идемпотентно."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        # Ф60-1 (П8): открытое интервью сессии закрытие кончает (`hub_sessions.close`) — запомнить, чьё.
        open_interview = interview_mod.open_for_session(conn, session_id)
        rec, closed = hub_sessions.close(conn, session_id, now_ms=now_ms, audit=audit)
        row = hub_sessions.session_json(conn, rec, now_ms=now_ms, waiters=waiters, ui=True)
        return row, closed, open_interview["id"] if open_interview is not None else None

    try:
        row, closed, ended_interview = await request.app.state.db.write(_fn)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    _held(request).discard(session_id)  # после коммита (П2в); идущее ожидание разбудил коммит
    _live(request).drop(session_id)
    runtime = getattr(request.app.state, "interviews", None)
    if ended_interview is not None and runtime is not None:
        runtime.stop(ended_interview)
        await runtime.publish_state_of(ended_interview)
    return {"session": row, "closed": closed}


@router.post("/sessions/{session_id}/handoff")
async def post_session_handoff(
    session_id: int, request: Request, body: HandoffIn | None = None, principal: Principal = ui_or_agent
) -> dict:
    """Ф41-7б: «Новая с передачей дел» — прежняя закрывается (`close_reason: "handoff"`), на той же
    линии создаётся новая с первой репликой — текстом передачи дел; всё одной транзакцией. Идёт ход —
    409. Ответ — `{"session": <новая>, "closed": <прежняя>}`."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)
    body = body or HandoffIn()

    def _fn(conn):
        new, closed = handoff_mod.handoff(
            conn,
            session_id,
            engine=body.engine,
            model=body.model,
            effort=body.effort,
            mode=body.mode,
            subdir=body.subdir,
            given=frozenset(body.model_fields_set),
            now_ms=now_ms,
            audit=audit,
            waiting=_waiting(request),
        )
        return (
            new.id,
            hub_sessions.session_json(conn, new, now_ms=now_ms, waiters=waiters, ui=True),
            hub_sessions.session_json(conn, closed, now_ms=now_ms, waiters=waiters, ui=True),
        )

    new_id, row, closed_row = await _write(request, _fn)
    _held(request).discard(session_id)  # после коммита — как закрытие и создание
    _live(request).drop(session_id)
    _held(request).add(new_id)
    return {"session": row, "closed": closed_row}


def _config_dir(request: Request):
    from pathlib import Path  # noqa: PLC0415

    value = getattr(request.app.state, "config_dir", None)
    return value if isinstance(value, Path) else None


def _swapped(request: Request, old_id: int, new_id: int) -> None:
    """После коммита передачи дел — как у `POST /sessions/{id}/handoff` (П2в)."""
    _held(request).discard(old_id)
    _live(request).drop(old_id)
    _held(request).add(new_id)


def fallback_notify(request: Request, key: tuple, title: str, text: str) -> None:
    """Ф83-1 (П7): одно уведомление владельцу на движок за эпизод паузы (`key` — движок и начало паузы)."""
    noted = getattr(request.app.state, "fallback_noted", None)
    if noted is None:
        noted = request.app.state.fallback_noted = set()
    if key in noted:
        return
    noted.add(key)
    ring = getattr(getattr(request.app.state, "notifier", None), "ring", None)
    if ring is not None:
        ring.add(title, text, "normal")


@router.post("/sessions/{session_id}/fallback/return")
async def post_session_fallback_return(
    session_id: int, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """Ф83-1: «Вернуть на основной» — передача дел на движок, модель и усилие из пометки «на запасном»;
    пометка снимается. Только кука (ноутбук и телефон). Не на запасном или основной на паузе — 409 (текст
    «<движок> на паузе до ЧЧ:ММ»); идёт ход — 409, как у передачи дел. Ответ — `{"session", "closed"}`."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        new, closed = engine_fallback.return_session(
            conn, session_id, now_ms=now_ms, audit=audit, waiting=_waiting(request)
        )
        return (
            new.id,
            hub_sessions.session_json(conn, new, now_ms=now_ms, waiters=waiters, ui=True),
            hub_sessions.session_json(conn, closed, now_ms=now_ms, waiters=waiters, ui=True),
        )

    new_id, row, closed_row = await _write(request, _fn)
    _swapped(request, session_id, new_id)
    return {"session": row, "closed": closed_row}


def _errand_answered(conn, principal: Principal, ref: str, now_ms: int) -> None:
    """Ф65-1 Ж1 + Ф88-1 (П3): действие по карточке `ref` ручкой окна. Кука — поручение агенту отбирается
    (`take_from_agent`, `ui`); сам агент — его поручение закрывается `agent` (не возвращается «завис»)."""
    if zq.is_agent(principal):
        zq.close_own_errand(conn, ref, now_ms=now_ms, token_id=principal.token_id)
    else:
        zq.take_from_agent(conn, ref, now_ms=now_ms)


def _today(request: Request, conn, now_ms: int) -> dict:
    """Ф41-6 (раздел 5, п.2): расход сессий хаба с местной полуночи; пересчёт не чаще раза в 30 с."""
    cache = getattr(request.app.state, "sessions_today", None)
    if cache is None:
        cache = request.app.state.sessions_today = adopt_mod.TodayCache()
    value = cache.get(now_ms)
    if value is None:
        value = adopt_mod.today(conn, now_ms=now_ms)
        cache.put(now_ms, value)
    return value


@router.get("/sessions")
async def get_sessions(
    request: Request,
    project_id: int | None = Query(None, ge=1, le=2**53),
    closed: int = Query(0, ge=0, le=1),
    principal: Principal = read_or_agent,
) -> dict:
    """Сессии хаба: `closed=0` — незакрытые, `closed=1` — закрытые. Bearer — только проекты токена (П10).
    Ф41-6: интерфейсу рядом со списком — `today` (расход всех сессий хаба за сегодня)."""
    now_ms = _now_ms(request)
    waiters = _waiters(request)
    presence = _presence(request, now_ms)
    with request.app.state.db.reader() as conn:
        recs = held_mod.list_sessions(
            conn, project_id=project_id, closed=bool(closed), project_ids=principal.project_ids
        )
        items = [
            hub_sessions.session_json(
                conn, rec, now_ms=now_ms, waiters=waiters, ui=_as_owner(principal), presence=presence
            )
            for rec in recs
        ]
        today = _today(request, conn, now_ms) if _as_owner(principal) else None
    return {"items": items} if today is None else {"items": items, "today": today}


@router.get("/sessions/autosend-count")
async def get_sessions_autosend_count(
    request: Request,
    project_id: int = Query(ge=1, le=2**53),
    role: str = Query("", max_length=128),
    principal: Principal = ui_only,
) -> dict:
    """Ф41-4 П15, П16: сколько бесед проекта разрешено роли «без подтверждения» — для формы новой сессии
    (сессия хаба с такой ролью отвечает в них сама). Только чтение списка Ф37-1; пустая или негодная роль —
    0. Объявлен раньше `GET /sessions/{id}`."""
    try:
        _role, role_key = subs.role_form(role)
    except subs.SubError:
        role_key = None
    with request.app.state.db.reader() as conn:
        return {"count": hub_sessions.autosend_count(conn, project_id, role_key)}


@router.get("/sessions/live")
async def get_sessions_live(request: Request, principal: Principal = ui_only):
    """Ф41-3 (3.1): поток SSE набираемого текста постоянных сессий — ТОЛЬКО кука UI (bearer — 403 и аудит, как
    у действий владельца); видимость — все проекты, как у `GET /sessions` для куки. При подключении — текущие
    записи, дальше — изменения (кадры `event: live`, без `id:`), тишина — комментарий-пульс раз в 15 с. С
    удалённого слушателя — по правилам `/stream`: сессия перепроверяется (`still_valid`). Объявлен раньше
    `GET /sessions/{id}`: иначе `live` ушёл бы в разбор номера сессии."""
    hub = _live(request)
    if hub.full:
        raise HTTPException(429, "слишком много открытых потоков текста — закрой лишние вкладки хаба")
    still_valid = session_checker(request, principal)
    clock = request.app.state.clock

    async def _generate():
        async for frame in live_mod.stream(hub, now_ms=lambda: int(clock() * 1000), still_valid=still_valid):
            yield frame

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, request: Request, principal: Principal = read_or_agent) -> dict:
    """Ф41-2б (3.1, П13): запись, ждущие решения запросы (с входом инструмента) и очередь реплик. Чужой
    проект токена — 404."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    waiters = _waiters(request)
    try:
        with request.app.state.db.reader() as conn:
            rec = held_work.visible(conn, principal, session_id)
            session = hub_sessions.session_json(
                conn,
                rec,
                now_ms=now_ms,
                waiters=waiters,
                ui=_as_owner(principal),
                presence=_presence(request, now_ms),
            )
            if not _as_owner(principal) and hub_sessions.sub_of_other_token(conn, rec, principal.token_id):
                # Ф33: подписка оформлена токеном профиля другой машины (сессия переехала с площадкой) —
                # инструменты этим токеном её не найдут; сервер MCP сессии оформит её заново.
                session["sub_stale"] = True
            return {
                "session": session,
                "requests": held_work.pending_requests(conn, rec),
                "says": held_work.queued_says(conn, rec),
            }
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc


@router.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: int,
    request: Request,
    after: int | None = Query(None, ge=0, le=_SEQ_MAX),
    before: int | None = Query(None, ge=1, le=_SEQ_MAX),
    limit: int = Query(200, ge=1, le=1000),
    principal: Principal = read_or_agent,
) -> list[dict]:
    """Ф41-2б (3.1): лента событий сессии. `after` — новые после курсора; `before` — последние `limit`
    событий до курсора."""
    _check_id(session_id)
    try:
        with request.app.state.db.reader() as conn:
            rec = held_work.visible(conn, principal, session_id)
            return held_work.list_events(conn, rec, after=after, before=before, limit=limit)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc


@router.get("/sessions/{session_id}/events/{event_id}")
async def get_session_event(
    session_id: int, event_id: int, request: Request, principal: Principal = read_or_agent
) -> dict:
    """Ф41-3 (2.1): событие целиком — `brief`, `has_detail` и сам `detail` (diff правки, вывод команды, вход
    инструмента). Кто может — как у списка; чужая сессия или чужое событие — 404."""
    _check_id(session_id)
    _check_row_id(event_id)
    try:
        with request.app.state.db.reader() as conn:
            rec = held_work.visible(conn, principal, session_id)
            return held_work.get_event(conn, rec, event_id)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc


# --- владелец: подписка и подъём (Ф41-4, 2.1, 2.5) ----------------------------------------------------------


@router.patch("/sessions/{session_id}")
async def patch_session(
    session_id: int, body: PatchIn, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """Заменить желаемый фильтр подписки (`null` — не подписывать; применится при следующем подъёме процесса
    сессии) и/или режим (Ф41-5: `manual` | `auto`; можно и с телефона). Закрытая — 409.

    Роль (09.10.2026): `role` (ключ есть — сменить; `null` или пустая — снять) и `number` (только с непустой
    ролью). Живая подписка сессии переходит на новую линию на месте, процесс хост перезапустит при простое.
    `autosend_ok` — признак «отвечает в беседы „без подтверждения“ своей роли»; при смене роли без явного
    `autosend_ok: true` сбрасывается.

    Ф48-2 (D): `compact_tokens` (50 000…900 000 или `null` — умолчание хоста) и `compact_auto` — порог и
    выключатель авто-сжатия; действуют сразу, без перезапуска процесса.

    Ф48-3: `role_file` — путь к MD-файлу роли относительно рабочей папки проекта (`null` или пустая строка —
    снять); действует со следующего запуска процесса. Негодный путь — 422; файла сейчас не прочитать —
    запись принята, в ответе `role_file_warning`."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    presence = _presence(request, now_ms)
    given = body.model_fields_set
    tuned = frozenset(given & {"model", "effort"})
    squeeze = frozenset(given & {"compact_tokens", "compact_auto"})  # Ф48-2 (D)
    if "compact_auto" in given and body.compact_auto is None:
        raise HTTPException(422, "compact_auto: true или false")
    role_given = "role" in given
    autosend = body.autosend_ok if "autosend_ok" in given else None
    if "number" in given and body.number is not None and not (role_given and (body.role or "").strip()):
        raise HTTPException(422, "number — только вместе с role")
    if (
        "want_sub" not in given
        and body.mode is None
        and not tuned
        and not role_given
        and autosend is None
        and not squeeze
        and "role_file" not in given
        and "fallback" not in given
    ):
        raise HTTPException(
            422,
            "нужен want_sub, mode, model, effort, role, role_file, autosend_ok, compact_tokens, "
            "compact_auto или fallback",
        )
    waiting = _waiting(request)

    def _fn(conn):
        rec = None
        if "want_sub" in given:
            rec = hub_sessions.set_want(conn, session_id, body.want_sub, now_ms=now_ms, audit=audit)
        if body.mode is not None:
            rec = hub_sessions.set_mode(conn, session_id, body.mode, now_ms=now_ms, audit=audit)
        if tuned:
            rec = hub_sessions.set_model(
                conn,
                session_id,
                given=tuned,
                model=body.model,
                effort=body.effort,
                now_ms=now_ms,
                audit=audit,
            )
        if role_given:
            rec = hub_sessions.set_role(
                conn,
                session_id,
                role=body.role,
                number=body.number,
                keep_autosend=autosend is True,
                now_ms=now_ms,
                audit=audit,
                waiting=waiting,
            )
        if autosend is not None:
            rec = hub_sessions.set_autosend(conn, session_id, autosend, now_ms=now_ms, audit=audit)
        if squeeze:
            rec = hub_sessions.set_compact(
                conn,
                session_id,
                given=squeeze,
                compact_tokens=body.compact_tokens,
                compact_auto=body.compact_auto,
                now_ms=now_ms,
                audit=audit,
            )
        if "role_file" in given:
            rec = hub_sessions.set_role_file(conn, session_id, body.role_file, now_ms=now_ms, audit=audit)
        if "fallback" in given:
            rec = engine_fallback.set_session_value(
                conn, session_id, body.fallback, now_ms=now_ms, audit=audit
            )
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, ui=True, presence=presence)

    row = await _write(request, _fn)
    return await _role_file_reply(row) if "role_file" in given else {"session": row}


@router.get("/sessions/{session_id}/role-text")
async def get_session_role_text(
    session_id: int, request: Request, principal: Principal = ui_or_agent  # Ф88-1: как было — только владелец
) -> dict:
    """Ф48-3: окно «Инструкция роли» — общий текст хаба (`common`) и файл роли сессии: путь (`role_file`,
    `null` — не задан), содержимое (`file_text`) или причина, почему не прочитан (`file_reason`). Файл
    читается той же функцией и с теми же ограничениями, что у хоста (`roles.read_file`: внутри рабочей папки
    проекта после разрешения ссылок, до 64 КиБ). Только чтение: кука или агент владельца (Ф88-1)."""
    _check_id(session_id)
    try:
        with request.app.state.db.reader() as conn:
            rec = held_work.visible(conn, principal, session_id)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    result = {
        "role": hub_sessions.role_label(rec.role, rec.role_no),
        "common": roles_mod.common_text(),
        "role_file": rec.role_file,
        "file_text": None,
        "file_reason": None,
    }
    if rec.role_file:
        loaded = await asyncio.to_thread(roles_mod.read_file, rec.workdir, rec.role_file)
        result["file_text"], result["file_reason"] = loaded.text, loaded.reason
    # Ф48-5: совпадает ли текущий файл роли с закреплённым в системном тексте разговора Claude Code
    # (`null` — снимка нет или движок другой).
    result["pinned"] = hub_sessions.role_file_pinned(rec, result["file_text"])
    return result


@router.post("/sessions/{session_id}/compact")
async def post_session_compact(session_id: int, request: Request, principal: Principal = ui_or_agent) -> dict:
    """«Сжать» (Ф41-4б, 2.5): просьба сжать контекст в ближайший момент, когда ход не идёт. Спящая — 409
    (сжимать нечем: процесса нет), закрытая — 409."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    presence = _presence(request, now_ms)

    def _fn(conn):
        rec = held_signals.compact(conn, session_id, now_ms=now_ms, audit=audit)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, ui=True, presence=presence)

    return {"session": await _write(request, _fn)}


@router.post("/sessions/{session_id}/wake")
async def post_session_wake(session_id: int, request: Request, principal: Principal = ui_or_agent) -> dict:
    """«Поднять»: поднять сессию под ждущие сигналы, хотя она тяжёлая и холодная. Поднимать не на что —
    409."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    presence = _presence(request, now_ms)

    def _fn(conn):
        # Ф58-3: «Поднять» — ответ по карточке «тяжёлая» / «на паузе»; решал агент — его поручение закрыть.
        for ref in (f"heavy:{session_id}", f"paused:{session_id}"):
            _errand_answered(conn, principal, ref, now_ms)
        rec = held_signals.wake(conn, session_id, now_ms=now_ms, presence=presence, audit=audit)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, ui=True, presence=presence)

    return {"session": await _write(request, _fn)}


@router.post("/sessions/{session_id}/mcp-reload")
async def post_session_mcp_reload(
    session_id: int, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """«Перечитать MCP» (Ф73-1): процесс сессии перезапускается с продолжением разговора, чтобы движок
    заново прочитал MCP-серверы. Идущий ход не обрывается (перезапуск — при простое), спящую хост поднимет
    без хода. Закрытая — 409."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        rec = held_work.mcp_reload(conn, session_id, now_ms=now_ms, audit=audit)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, waiters=waiters, ui=True)

    return {"session": await _write(request, _fn)}


# --- владелец: работа сессии (Ф41-2б, 3.1) ------------------------------------------------------------------


class SayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные пределы (не пусто, до 16 000) проверяет `held_work.say` — отказ уходит русским текстом (П13).
    text: str = Field(max_length=held_work.SAY_MAX * 4)
    # `now` — «прервать и написать»: идущий ход прерывает хост (П3а); у спящей и простаивающей — как `next`.
    priority: Literal["next", "now"] = "next"
    # Ф41-3в: id файлов, загруженных в эту сессию. Точные пределы (до 10, без повторов, свои, не приложенные)
    # проверяет `held_work.say` — отказ уходит русским текстом.
    files: list[str] = Field(default_factory=list, max_length=held_files.SAY_MAX_FILES * 10)


@router.post("/sessions/{session_id}/say")
async def post_session_say(
    session_id: int, body: SayIn, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """Реплика владельца в очередь сессии. Закрытая — 409. Ф88-1 (C1, П3, П8): реплика его агента —
    `source="agent"`, `typed=False` (не в сводку дня и не в `<hub_siblings>`), карточки `turn`/`stall`
    закрываются `agent` с его токеном, его поручения по ним — `agent`."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    agent = zq.is_agent(principal)

    def _fn(conn):
        if agent:
            zq.close_own_session_errands(conn, session_id, now_ms=now_ms, token_id=principal.token_id)
        return held_work.say(
            conn,
            session_id,
            text=body.text,
            priority=body.priority,
            now_ms=now_ms,
            audit=audit,
            files=body.files,
            source="agent" if agent else "ui",
            token_id=principal.token_id if agent else None,
            typed=not agent,  # Ф86-1 (C1.2): набрал сам владелец — только кука окна сессии (набор и диктовка)
        )

    say_id = await _write(request, _fn)
    return {"id": say_id, "session_id": session_id, "priority": body.priority, "status": "queued"}


# --- владелец: файлы во вход сессии (Ф41-3в, 4.1) ------------------------------------------------------------

_HEAD_BYTES = 64
_FILE_CHUNK = 256 * 1024


class _LengthMismatch(Exception):
    pass


def declared_length(request: Request) -> int:
    """Заявленный размер тела файла: без `Content-Length` — 411; больше 20 МиБ — 413 без чтения тела; пустой
    — 422."""
    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise HTTPException(411, "content-length required")
    declared = int(raw_length)
    if declared > held_files.MAX_BYTES:
        raise HTTPException(413, "файл больше 20 МиБ")
    if declared == 0:
        raise HTTPException(422, "пустой файл")
    return declared


async def receive_file(request: Request, declared: int, name: str) -> dict:
    """Тело запроса -> временный файл склада (0600): поля для `held_files.commit` / `talk_commit` без
    `file_id` и `now_ms`. Мало места — 507; пришло не столько, сколько заявлено (в том числе больше), — 400,
    приём обрывается на первом лишнем куске. Тип — по сигнатуре, `Content-Type` и расширение не учитываются.
    Временный файл убирает вызывающий (`finally`), отказ здесь — сам."""
    db = request.app.state.db
    if not media_mod.disk_ok(db.data_dir):
        _logger.warning("файлы сессий: мало места на диске")
        raise HTTPException(507, "insufficient storage")
    clean = held_files.clean_name(name)
    tmp = held_files.tmp_path(db.data_dir)
    hasher = hashlib.sha256()
    head = b""
    received = 0
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                # Заявленному размеру не верим: счёт — по принятым байтам, проверка — до записи куска.
                # Заявлено не больше 20 МиБ, значит и на диск больше не ляжет.
                if received > declared:
                    raise _LengthMismatch
                if len(head) < _HEAD_BYTES:
                    head += chunk[: _HEAD_BYTES - len(head)]
                hasher.update(chunk)
                f.write(chunk)
            if received != declared:
                raise _LengthMismatch
            f.flush()
            await asyncio.to_thread(os.fsync, f.fileno())
    except _LengthMismatch as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "length mismatch") from exc
    except ClientDisconnect as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "client disconnected") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {
        "tmp": tmp,
        "sha256": hasher.hexdigest(),
        "size": received,
        "detected": media_mod.detect(head),
        "name": clean,
    }


@router.post("/sessions/{session_id}/files")
async def post_session_file(
    session_id: int,
    request: Request,
    name: str = Query("", max_length=4000),
    principal: Principal = ui_or_agent,
) -> dict:
    """Файл в сессию: тело — сырые байты. Без `Content-Length` — 411; заявлено больше 20 МиБ — 413 без
    чтения тела; пришло не столько, сколько заявлено (в том числе больше), — 400, приём обрывается на первом
    лишнем куске; пустой — 422; закрытая сессия и 30 неприложенных — 409.
    Тип — по сигнатуре, `Content-Type` и расширение не учитываются."""
    _check_id(session_id)
    db = request.app.state.db
    declared = declared_length(request)
    try:
        with db.reader() as conn:
            held_work.file_room(conn, session_id)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    fields = await receive_file(request, declared, name)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    fields["file_id"] = held_files.new_id()
    try:
        return await _write(
            request, lambda conn: held_work.file_add(conn, session_id, now_ms=now_ms, audit=audit, **fields)
        )
    finally:
        fields["tmp"].unlink(missing_ok=True)  # принят — уже переименован; отказ — убрать


@router.delete("/sessions/{session_id}/files/{file_id}")
async def delete_session_file(
    session_id: int, file_id: str, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """Убрать неприложенный файл. Приложенный к реплике — 409; чужой и несуществующий — 404."""
    _check_id(session_id)
    if not held_files.valid_id(file_id):
        raise HTTPException(404, "not found")
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    await _write(
        request, lambda conn: held_work.file_remove(conn, session_id, file_id, now_ms=now_ms, audit=audit)
    )
    return {"id": file_id}


@router.post("/sessions/{session_id}/says/{say_id}/resend")
async def post_session_say_resend(
    session_id: int, say_id: int, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """Ф48-2 (раздел A): «Отправить снова» — прерванная реплика подаётся как обычная следующая. Реплика не
    прервана — 409; чужая или несуществующая — 404; закрытая сессия — 409."""
    _check_id(session_id)
    _check_row_id(say_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)

    def _fn(conn):
        _errand_answered(conn, principal, f"say:{say_id}", now_ms)
        rec = held_work.say_resend(conn, session_id, say_id, now_ms=now_ms, audit=audit)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, ui=True)

    return {"id": say_id, "status": "queued", "session": await _write(request, _fn)}


@router.delete("/sessions/{session_id}/says/{say_id}")
async def delete_session_say(
    session_id: int, say_id: int, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """Ф48-2 (раздел A): «Убрать» прерванную реплику — очередь за ней идёт дальше. Отказы — как у
    «Отправить снова»."""
    _check_id(session_id)
    _check_row_id(say_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)

    def _fn(conn):
        _errand_answered(conn, principal, f"say:{say_id}", now_ms)
        rec = held_work.say_remove(conn, session_id, say_id, now_ms=now_ms, audit=audit)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, ui=True)

    return {"id": say_id, "status": "dropped", "session": await _write(request, _fn)}


@router.post("/sessions/{session_id}/interrupt")
async def post_session_interrupt(
    session_id: int, request: Request, principal: Principal = ui_or_agent
) -> dict:
    """«Прервать» идущий ход; хода нет — 409."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        rec = held_work.interrupt(conn, session_id, now_ms=now_ms, audit=audit)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, waiters=waiters, ui=True)

    return {"session": await _write(request, _fn)}


@router.post("/sessions/{session_id}/sleep")
async def post_session_sleep(session_id: int, request: Request, principal: Principal = ui_or_agent) -> dict:
    """«Усыпить»: очередь реплик снимается, процессу (если есть) — просьба хосту. Уже спит — без изменений."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        rec, changed, dropped = held_work.sleep(conn, session_id, now_ms=now_ms, audit=audit)
        row = hub_sessions.session_json(conn, rec, now_ms=now_ms, waiters=waiters, ui=True)
        return {"session": row, "changed": changed, "dropped_says": dropped}

    return await _write(request, _fn)


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["allow", "deny"]
    answers: dict[str, str] | None = None
    # Предел (2000) проверяет `held_work.decide` — отказ уходит готовым русским текстом (П13).
    message: str | None = Field(default=None, max_length=held_work.MESSAGE_MAX * 4)
    # Ф99-7.2 (П11): «Всегда в этой сессии» — только с `allow` и только у карточки с ключом (иначе 422).
    always: StrictBool | None = None


@router.post("/session-requests/{request_id}/decide")
async def post_session_request_decide(
    request_id: int, body: DecideIn, request: Request, principal: Principal = ui_only
) -> dict:
    """Решение карточки сессии — те же тела, что у `POST /work/requests/{id}/decide`."""
    _check_row_id(request_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)

    def _fn(conn):
        zq.take_from_agent(conn, f"req:{request_id}", now_ms=now_ms)  # Ф65-1 Ж1
        decided = held_work.decide(
            conn,
            request_id,
            decision=body.decision,
            answers=body.answers,
            message=body.message,
            now_ms=now_ms,
            audit=audit,
            always=body.always is True,  # Ф99-7.2: только эта ручка (кука интерфейса)
        )
        action = "deny" if body.decision == "deny" else "answer" if decided[0] == "answered" else "allow"
        decisions_mod.record(  # Ф76-1 (П5)
            conn,
            f"req:{request_id}",
            decisions_mod.BY_UI,
            action,
            body.message,
            now_ms=now_ms,
            answers=body.answers,
        )
        return decided

    status, session_id = await _write(request, _fn)
    return {"id": request_id, "session_id": session_id, "status": status}


class AlwaysOffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: StrictStr = Field(min_length=1, max_length=1000)


@router.post("/sessions/{session_id}/always/remove")
async def post_session_always_remove(
    session_id: int, body: AlwaysOffIn, request: Request, principal: Principal = ui_only
) -> dict:
    """Ф99-7.2 (П11): снять «Всегда в этой сессии» («⋯ сессии → Разрешения»). Только кука интерфейса; ключа
    нет — 404. Ответ — запись сессии (форма интерфейса)."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)

    def _fn(conn):
        rec = held_mod.get(conn, session_id)
        if rec is None:
            raise hub_sessions.SessionError(404, "not found")
        session_always.remove(conn, rec, body.key, now_ms=now_ms, audit=audit)
        return hub_sessions.session_json(conn, held_mod.get(conn, session_id), now_ms=now_ms, ui=True)

    return {"session": await _write(request, _fn)}


# --- хост (`work_host`) ---------------------------------------------------------------------------------


class SysPinIn(BaseModel):
    """Ф48-5: составляющие системного текста на момент создания разговора Claude Code."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["manual", "auto"]
    role: str | None = Field(default=None, max_length=200)
    role_no: StrictInt | None = Field(default=None, ge=1, le=_SEQ_MAX)
    role_file: str | None = Field(default=None, max_length=4096)
    # sha256 текста файла роли; `null` — блока файла в системном тексте не было.
    file_sha: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    # Ф56-1б (3.7): sha256 общего текста ролей (`_common.md`); `null` — общего текста в системном нет
    # (сессия без роли).
    common_sha: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    # Ф99-1: разговор начат вне хаба (сессия из прошлого разговора) — признак живёт в снимке постоянно; `null`
    # или нет ключа — обычный разговор.
    imported: StrictBool | None = None


def _pin_dump(pin: SysPinIn) -> dict:
    """Снимок как прислан; `common_sha: null` не хранится — снимок без общего текста тот же, что до 56-1б;
    `imported` — только `true` (Ф99-1)."""
    out = pin.model_dump()
    if out.get("common_sha") is None:
        out.pop("common_sha", None)
    if not out.get("imported"):
        out.pop("imported", None)
    return out


class BindIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine_session_id: str = Field(min_length=1, max_length=hub_sessions.ENGINE_SID_MAX)
    # Настоящий процесс: `starttime` нулём не бывает (как у тройки настоящего процесса, О8).
    proc_pid: StrictInt | None = Field(default=None, ge=1, le=_PID_MAX)
    proc_start: StrictInt | None = Field(default=None, ge=1, le=_PROC_START_MAX)
    state: Literal["running", "idle", "asleep"]
    # Ф41-5 П1: SHA-256 ключа запуска идущего процесса (сам ключ ядру не передаётся).
    launch_key_hash: str | None = Field(default=None, min_length=64, max_length=64)
    # Ф48-5: снимок закреплённого системного текста нового разговора Claude Code.
    sys_pin: SysPinIn | None = None


class BgItemIn(BaseModel):
    """Ф48-1 (контракт раздела 3 спеки): фоновая задача процесса сессии — подагент или фоновая команда."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    kind: Literal["agent", "command"]
    title: str = Field(max_length=200)
    agent_type: str | None = Field(default=None, max_length=64)
    parent: str | None = Field(default=None, min_length=1, max_length=64)
    # `running` | `completed` | `failed` | `stopped`; иное — строкой как есть.
    state: str = Field(min_length=1, max_length=20)
    started_ts: StrictInt = Field(ge=0, le=_SEQ_MAX)
    ended_ts: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    doing: str | None = Field(default=None, max_length=200)
    last_tool: str | None = Field(default=None, max_length=64)
    tool_uses: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    tokens: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    note: str | None = Field(default=None, max_length=300)


class BgIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BgItemIn] = Field(max_length=hub_sessions.BG_ITEMS_MAX)
    # Ф56-1в (2.4): объявленное пробуждение движка (`ScheduleWakeup`), мс; нет — `null` или ключа нет.
    wake_at_ts: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)


class StateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["running", "idle", "asleep"]
    proc_pid: StrictInt | None = Field(default=None, ge=1, le=_PID_MAX)
    proc_start: StrictInt | None = Field(default=None, ge=1, le=_PROC_START_MAX)
    launch_key_hash: str | None = Field(default=None, min_length=64, max_length=64)
    # Ф48-1: фоновые задачи процесса; нет ключа или `null` — «не менять», `{"items": []}` — «задач нет».
    bg: BgIn | None = None


@router.get("/work/host/sessions")
async def get_host_sessions(request: Request, principal: Principal = _host_dep) -> dict:
    """Незакрытые сессии хаба целиком (рабочая папка, разговор движка, роль, модель, состояние) — хосту для
    подъёма."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        items = [
            hub_sessions.session_json(conn, rec, now_ms=now_ms, host=True)
            for rec in held_mod.list_sessions(conn)
        ]
    return {"items": items}


@router.post("/work/host/sessions/{session_id}/bind")
async def post_host_session_bind(
    session_id: int, body: BindIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Хост запустил процесс или сменил разговор движка. Подписки сессии не трогаются (П1)."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)

    def _fn(conn):
        rec = hub_sessions.bind(
            conn,
            session_id,
            engine_session_id=body.engine_session_id,
            proc_pid=body.proc_pid,
            proc_start=body.proc_start,
            state=body.state,
            now_ms=now_ms,
            audit=audit,
            launch_key_hash=body.launch_key_hash,
            sys_pin=None if body.sys_pin is None else _pin_dump(body.sys_pin),
        )
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, host=True)

    try:
        row = await request.app.state.db.write(_fn)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    _live_follow(request, row)
    return {"session": row}


class HostFallbackIn(BaseModel):
    """Ф83-1: пауза движка сессии — `{kind, reason?, until?, since?}` (мс часов хоста)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["limit", "balance", "auth", "model", "retry", "other"]
    reason: StrictStr | None = Field(default=None, max_length=2000)
    until: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    since: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)


@router.post("/work/host/sessions/{session_id}/fallback")
async def post_host_session_fallback(
    session_id: int, body: HostFallbackIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Ф83-1 (П1, П4, П7, П8): сессия хаба в авто держится паузой своего движка, ей есть что подавать — хост
    просит перейти на запасной. Ядро: авто, повод (`limit`/`balance`), пометки нет, запасной задан, другой
    движок и доступен — «Новая с передачей дел» на запасном движке с его моделью и усилием (`by: auto`).
    Ответ `{switched: true, session, closed}` или `{switched: false, why, code}` (строка для окна сессии;
    хост запоминает отказ на эпизод паузы). Отказ передачи дел (идёт ход, интервью, потолок, папка) — тоже
    `switched: false`. Нет сессии — 404, закрыта — 410 (хост гасит раннер)."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)
    config_dir = _config_dir(request)

    def _fn(conn):
        new, closed, goal = engine_fallback.switch_session(
            conn,
            session_id,
            by="auto",
            kind=body.kind,
            reason=body.reason,
            since=body.since,
            now_ms=now_ms,
            audit=audit,
            waiting=_waiting(request),
            config_dir=config_dir,
        )
        return (
            new.id,
            hub_sessions.session_json(conn, new, now_ms=now_ms, waiters=waiters, host=True),
            hub_sessions.session_json(conn, closed, now_ms=now_ms, waiters=waiters, host=True),
            closed.engine,
            goal,
        )

    try:
        new_id, row, closed_row, main_engine, goal = await request.app.state.db.write(_fn)
    except engine_fallback.Refused as exc:
        return {"switched": False, "why": exc.why, "code": exc.code}
    except hub_sessions.SessionError as exc:
        if exc.status in (404, 410):
            raise _http(exc) from exc
        return {"switched": False, "why": exc.detail, "code": "handoff"}
    _swapped(request, session_id, new_id)
    fallback_notify(
        request,
        (main_engine, body.since),
        "Хаб: сессии на запасном движке",
        f"{engine_registry.label(main_engine)} — {failure.label(body.kind)}: сессии хаба в авто переходят на "
        f"{engine_fallback.choice_label(goal)}",
    )
    return {"switched": True, "session": row, "closed": closed_row}


@router.post("/work/host/sessions/{session_id}/state")
async def post_host_session_state(
    session_id: int, body: StateIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Хост подтверждает состояние (`asleep` обнуляет настоящий процесс). Закрытая сессия — 410."""
    _check_id(session_id)
    now_ms = _now_ms(request)

    def _fn(conn):
        rec = hub_sessions.set_state(
            conn,
            session_id,
            state=body.state,
            proc_pid=body.proc_pid,
            proc_start=body.proc_start,
            now_ms=now_ms,
            launch_key_hash=body.launch_key_hash,
            bg=body.bg.model_dump() if body.bg is not None else None,
        )
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, host=True)

    try:
        row = await request.app.state.db.write(_fn)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    _live_follow(request, row)
    return {"session": row}


# --- хост: работа сессии (Ф41-2б, 3.2; П1, П3, П8, П13) -------------------------------------------------


@router.post("/work/host/sessions/recover")
async def post_host_sessions_recover(request: Request, principal: Principal = _host_dep) -> dict:
    """П8: старт (или остановка) хоста — одной транзакцией: `running`/`idle` -> `asleep`, запросы без
    доставленного решения — `expired`, просьбы владельца сняты. Очереди реплик остаются. Ф89-1 (П12 б): ключ
    `bg` — `{"<id сессии>": {"items": [...]}}`: сессии, чей процесс оборван этим восстановлением, и их
    фоновые задачи (снимок до сброса, задач нет — пустой список); ключ — только когда такие есть."""
    now_ms = _now_ms(request)
    bg: dict[int, dict] = {}
    touched = await _write(request, lambda conn: held_work.recover(conn, now_ms=now_ms, bg_out=bg))
    for session_id in touched:
        _live(request).drop(session_id)
    out: dict = {"touched": touched}
    if bg:
        out["bg"] = {str(sid): value for sid, value in bg.items()}
    return out


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "start",
        "tool",
        "tool_done",
        "text",
        "request",
        "decision",
        "say",
        "turn_end",
        "status",
        "error",
        "warn",
        "interrupt",
        "compact",
    ]
    ts: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    tool: str | None = Field(default=None, max_length=held_work.TOOL_MAX)
    summary: str = Field(default="", max_length=held_work.TEXT_SUMMARY_MAX)  # по виду — в held_work
    is_error: bool = False
    ref: str | None = Field(default=None, max_length=held_work.REF_MAX)
    # Ф41-3 (2.1): подробности — строке ленты (`brief`, до 1000 байт) и по клику (`detail`, до 60 000 байт).
    # Пределы проверяет `held_work.pack_detail`: не влезшее поле отбрасывается, событие принимается.
    brief: dict | None = None
    detail: dict | None = None


class EventsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(min_length=1, max_length=held_work.EVENTS_BATCH_MAX)


@router.post("/work/host/sessions/{session_id}/events")
async def post_host_session_events(
    session_id: int, body: EventsIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Пачка событий ленты (до 200). Закрытая сессия — 410: хост гасит раннер."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    events = [e.model_dump() for e in body.events]
    count = await _write(request, lambda conn: held_work.add_events(conn, session_id, events, now_ms=now_ms))
    return {"accepted": count}


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["permission", "question"]
    tool: str | None = Field(default=None, max_length=held_work.TOOL_MAX)
    input: dict | None = None
    reason: str | None = Field(default=None, max_length=held_work.REASON_MAX)
    # Ф41-5 (3.4): ключ выключателя автопилота, под которым пришёл запрос (каталог; иначе 422).
    rule: str | None = Field(default=None, max_length=64)


@router.post("/work/host/sessions/{session_id}/requests")
async def post_host_session_request(
    session_id: int, body: RequestIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Запрос разрешения или вопрос сессии -> `pending`. Ф66-1 (П3): разрешение с маршрутом «сразу да» —
    `{"status": "allowed", "decided": "allow"}`: хост отвечает движку сам и владельцу не сообщает."""
    _check_id(session_id)
    now_ms = _now_ms(request)

    def _fn(conn):
        created = held_work.create_request(
            conn,
            session_id,
            kind=body.kind,
            tool=body.tool,
            tool_input=body.input,
            reason=body.reason,
            now_ms=now_ms,
            rule=body.rule,
        )
        return created, held_work.auto_decided(conn, created)

    request_id, auto = await _write(request, _fn)
    if auto:
        return {"id": request_id, "session_id": session_id, "status": "allowed", "decided": "allow"}
    return {"id": request_id, "session_id": session_id, "status": "pending"}


@router.post("/work/host/session-requests/{request_id}/expire")
async def post_host_session_request_expire(
    request_id: int, request: Request, principal: Principal = _host_dep
) -> dict:
    _check_row_id(request_id)
    now_ms = _now_ms(request)
    await _write(request, lambda conn: held_work.expire_request(conn, request_id, now_ms=now_ms))
    return {"id": request_id, "status": "expired"}


@router.post("/work/host/session-requests/{request_id}/auto")
async def post_host_session_request_auto(
    request_id: int, request: Request, principal: Principal = _host_dep
) -> dict:
    """Ф51-1 B: карточку закрыла снятая галочка «Автопилота» — хост уже ответил процессу сам. Только хост
    (`work_host`); 409 — карточка уже решена или её ключ у сессии стоит."""
    _check_row_id(request_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    status = await _write(
        request, lambda conn: held_work.auto_request(conn, request_id, now_ms=now_ms, audit=audit)
    )
    return {"id": request_id, "status": status, "decided_by": dict(held_work.AUTO_BY)}


@router.post("/work/host/session-requests/{request_id}/delivered")
async def post_host_session_request_delivered(
    request_id: int, request: Request, principal: Principal = _host_dep
) -> dict:
    _check_row_id(request_id)
    now_ms = _now_ms(request)
    await _write(request, lambda conn: held_work.request_delivered(conn, request_id, now_ms=now_ms))
    return {"id": request_id}


@router.post("/work/host/session-says/{say_id}/delivered")
async def post_host_session_say_delivered(
    say_id: int, request: Request, principal: Principal = _host_dep
) -> dict:
    """П4: реплика записана во ввод процесса."""
    _check_row_id(say_id)
    now_ms = _now_ms(request)
    session_id = await _write(request, lambda conn: held_work.say_delivered(conn, say_id, now_ms=now_ms))
    return {"id": say_id, "session_id": session_id, "status": "delivered"}


def _file_chunks(f):
    try:
        while chunk := f.read(_FILE_CHUNK):
            yield chunk
    finally:
        f.close()


@router.get("/work/host/session-files/{file_id}")
async def get_host_session_file(file_id: str, request: Request, principal: Principal = _host_dep):
    """Ф41-3в (4.1): байты файла для хоста, потоком. Только приложенный к реплике, не стёртый, незакрытой
    сессии; иначе 404. Имя и тип сюда не входят — они в реплике."""
    if not held_files.valid_id(file_id):
        raise HTTPException(404, "not found")
    try:
        with request.app.state.db.reader() as conn:
            path, size = held_files.host_file(conn, file_id)
    except hub_sessions.SessionError as exc:
        raise _http(exc) from exc
    try:
        # Открыт до ответа: стирание после этого читателю не мешает (байты живут, пока открыт дескриптор).
        f = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb")
    except OSError as exc:
        raise HTTPException(404, "not found") from exc
    if os.fstat(f.fileno()).st_size != size:
        f.close()
        raise HTTPException(404, "not found")
    return StreamingResponse(
        _file_chunks(f),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(size),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


class UsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ф41-4б (П4в): после сжатия размер контекста неизвестен до следующего хода — `null`.
    context_tokens: StrictInt | None = Field(ge=0, le=_USAGE_TOKENS_MAX)
    context_limit: StrictInt | None = Field(default=None, ge=0, le=_USAGE_TOKENS_MAX)
    # Ф41-4 П17: база сессии — размер контекста после первого хода разговора (и первого хода за сжатием).
    base_tokens: StrictInt | None = Field(default=None, ge=0, le=_USAGE_TOKENS_MAX)
    turn_cost_usd: float | None = Field(default=None, ge=0, le=_USAGE_COST_MAX, allow_inf_nan=False)


class TurnAnswerIn(BaseModel):
    """Ф56-1б (3.2, П7): последний ответ хода — полный текст и краткое для карточки `turn`."""

    model_config = ConfigDict(extra="forbid")

    full: StrictStr = Field(min_length=1, max_length=_TURN_FULL_RAW_MAX)
    brief: StrictStr = Field(default="", max_length=_TURN_BRIEF_RAW_MAX)
    brief_kind: Literal["owner_channel", "tail"]
    cut: StrictBool = False


class TurnStuckStepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: StrictStr | None = Field(default=None, max_length=held_work.TOOL_MAX)


class TurnStuckIn(BaseModel):
    """Ф56-1в (5.1): ход идёт, шагов нет дольше порога хоста — с какого времени тишина и последний шаг."""

    model_config = ConfigDict(extra="forbid")

    since_ts: StrictInt = Field(ge=0, le=_SEQ_MAX)
    step: TurnStuckStepIn | None = None


class TurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["running", "idle", "asleep"]
    last_turn_ts: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    # Только вместе с `state: "asleep"`: запуск не удался (`launch_fails` растёт).
    failure: str | None = Field(default=None, max_length=held_work.FAILURE_MAX)
    # П3б: просьба «прервать» снимается, только если она не новее этого времени.
    clear_interrupt_upto: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    # Ф41-4 П3г: и исполненные «поднять», «сжать», признак оборванного хода по сигналу.
    clear: list[Literal["sleep", "wake", "compact", "signal_lost", "mcp_reload"]] = Field(
        default_factory=list, max_length=8
    )
    # Ф73-1: «Перечитать MCP» исполнена этим значением `mcp_reload_ts` (только с `clear: ["mcp_reload"]`).
    mcp_reload_upto: StrictInt | None = Field(default=None, ge=0, le=_SEQ_MAX)
    # П13: ключ есть — пишется (и `null`); ключа нет — причину снимает только начало хода.
    # Ф48-2 (B): `compact` — срочная реплика ждёт конца идущего сжатия.
    wait_reason: Literal["slot", "elsewhere", "paused", "heavy", "compact"] | None = None
    # Ф41-4 П5: ход начат по сигналу хаба (ядро держит `signal_turn_ts` до обычного конца хода).
    origin: Literal["signal"] | None = None
    # Ревью 41-4 (а): ход по сигналу отклонён движком без вызова модели — признак выданного сигнала остаётся.
    keep_signal: bool = False
    # 10.10: идущий ход оборвал сам хаб (поставлено ограничение автопилота) — после подъёма сессии подаётся
    # постоянная строка «прошлый ход был прерван» (тот же признак `signal_lost_ts`). Только с `asleep`.
    cut: bool = False
    # Ф41-3 (2.3): расход хода — хост шлёт в том `turn`, которым закрывает ход.
    usage: UsageIn | None = None
    # Ф48-1: фоновые задачи процесса — как у `…/state`.
    bg: BgIn | None = None
    # Ф48-2 (A): реплика владельца, ход по которой оборван, — вернуть в очередь прерванной.
    interrupted_say: StrictInt | None = Field(default=None, ge=1, le=_SEQ_MAX)
    # Ф55-1 (2.4): ход начат пачкой реплик — прерванными возвращаются все.
    interrupted_says: list[Annotated[StrictInt, Field(ge=1, le=_SEQ_MAX)]] | None = Field(
        default=None, max_length=200
    )
    # Ревью Ф56 (8): реплики владельца, поданные этим ходом (конец хода, `idle`): ещё `queued` в ядре (отметка
    # «доставлено» не дошла или опоздала) — отмечаются доставленными до разбора конца хода.
    delivered_says: list[Annotated[StrictInt, Field(ge=1, le=_SEQ_MAX)]] | None = Field(
        default=None, max_length=200
    )
    # Ф48-2 (D): действующий порог авто-сжатия, как его посчитал хост (ключ есть — пишется, и `null`).
    compact_at: StrictInt | None = Field(default=None, ge=0, le=_USAGE_TOKENS_MAX)
    # Ф50-1 (П3): сигнал, поданный посреди хода, не прочитан — `signal_lost_ts` при любом состоянии.
    mid_lost: bool = False
    # Ф50-4: источник начатого хода (только с `running`): реплика владельца, сигнал, сам движок, сжатие.
    source: Literal["say", "signal", "self", "compact"] | None = None
    # Ф56-1б (3.2): последний ответ хода (`null` — ход без текста) и строка конца хода — для карточки `turn`.
    answer: TurnAnswerIn | None = None
    summary: StrictStr | None = Field(default=None, max_length=held_work.SUMMARY_MAX * 4)
    # Ф56-1в (5.1): ход завис (только с `running`; ключ есть — пишется: `null` — шаг пришёл).
    stuck: TurnStuckIn | None = None
    # Ф93-1 (F3): отчёт конца хода запоздал (ядро было недоступно, повтор хоста или старт следующего хоста):
    # принимается, только если его `last_turn_ts` новее записанного; иначе отбрасывается целиком.
    late: bool = False


@router.post("/work/host/sessions/{session_id}/turn")
async def post_host_session_turn(
    session_id: int, body: TurnIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Ход сессии: состояние, конец хода, снятие исполненных просьб, неудачный запуск, причина ожидания.
    Закрытая сессия — 410. Ф48-2: `interrupted_say` — реплика оборванного хода возвращается в очередь
    прерванной; `compact_at` — действующий порог авто-сжатия."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    changes = body.model_dump(exclude_unset=True)
    if body.bg is not None:
        changes["bg"] = body.bg.model_dump()  # все ключи записи, и не названные хостом

    def _fn(conn):
        rec = held_work.turn(conn, session_id, changes, now_ms=now_ms)
        back = body.interrupted_say if held_work.is_interrupted(conn, rec, body.interrupted_say) else None
        backs = [i for i in body.interrupted_says or () if held_work.is_interrupted(conn, rec, i)]
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, host=True), back, backs

    row, back, backs = await _write(request, _fn)
    _live_follow(request, row)
    # Ф48-2 (A): `interrupted_say` — реплика стоит в очереди прерванной (`null` — возвращать было нечего).
    # Ф55-1 (2.4): `interrupted_says` — какие из присланных списком стоят прерванными.
    return {"session": row, "interrupted_say": back, "interrupted_says": backs}


class MidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queued: StrictBool


@router.post("/work/host/sessions/{session_id}/mid")
async def post_host_session_mid(
    session_id: int, body: MidIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Ф50-4 (П9): строка сигнала, поданная посреди хода, стоит в очереди движка (`queued: true` —
    `mid_queued_ts` ставится, если ещё не стоит) или уже нет (`false` — снимается). Состояние сессии не
    меняется. Закрытая — 410."""
    _check_id(session_id)
    now_ms = _now_ms(request)

    def _fn(conn):
        rec = held_work.mid_queued(conn, session_id, body.queued, now_ms=now_ms)
        return hub_sessions.session_json(conn, rec, now_ms=now_ms, host=True)

    return {"session": await _write(request, _fn)}


class SignalsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ф50-1: подача посреди хода — выдать при `running`, `signal_turn_ts` не писать.
    mid: bool = False


@router.post("/work/host/sessions/{session_id}/signals")
async def post_host_session_signals(
    session_id: int, request: Request, body: SignalsIn | None = None, principal: Principal = _host_dep
) -> dict:
    """Ф41-4 (2.3, П6): выдача сигналов сессии хосту — то же, что отдали бы `/subs/{id}/wait` и
    `/chan-subs/wait` её токену: `{"sub_id", "project_id", "signals", "chan_signals"}` (только числа и
    метки). Сессия не готова принять (не `idle`, усыпляется, в очереди реплика владельца) или ядро
    останавливается — оба списка пусты и ничего не отмечено. Закрытая — 410. Ф50-1: `{"mid": true}` —
    подача посреди хода: выдаётся только при `running`, ход не помечается ходом по сигналу."""
    _check_id(session_id)
    now_ms = _now_ms(request)
    presence = _presence(request, now_ms)
    closing = bool(request.app.state.notifier.closed)
    mid = bool(body is not None and body.mid)
    return await _write(
        request,
        lambda conn: held_signals.deliver(
            conn, session_id, now_ms=now_ms, presence=presence, closing=closing, mid=mid
        ),
    )


class LiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: str = Field(min_length=1, max_length=live_mod.BLOCK_MAX)
    kind: Literal["text", "thinking"]
    # ВЕСЬ текст блока на этот момент; длиннее 8000 — хост шлёт последние 8000 символов и `cut: true`.
    text: str = Field(max_length=live_mod.TEXT_MAX)
    cut: bool = False
    done: bool = False


@router.post("/work/host/sessions/{session_id}/live")
async def post_host_session_live(
    session_id: int, body: LiveIn, request: Request, principal: Principal = _host_dep
) -> dict:
    """Ф41-3 (3.1): текст идущего блока ответа (или размышлений) — только в память ядра и подписчикам потока
    `GET /sessions/live`. В базу не пишется, `seq` и `host_seen_ts` не двигает, в аудит и журналы текст не
    попадает. Сессии нет — 404, закрыта — 410 (как у прочих ручек хоста). Сессия не в ходу — принято и
    отброшено: запись живёт только при `running`."""
    _check_id(session_id)
    with request.app.state.db.reader() as conn:
        rec = held_mod.get(conn, session_id)
    if rec is None:
        raise HTTPException(404, "not found")
    if rec.closed_ts is not None:
        raise HTTPException(410, held_work.CLOSED)
    if rec.state == "running":
        _live(request).publish(
            session_id,
            block=body.block,
            kind=body.kind,
            text=body.text,
            cut=body.cut,
            done=body.done,
            now_ms=_now_ms(request),
        )
    return {"ok": True}
