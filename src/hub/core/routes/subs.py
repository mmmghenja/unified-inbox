"""Ручки подписки сессии на проект (Ф13-1, docs/architecture.md, B4–B5). Маршруты не пишут
SQL: запись и чтение — `subs.py` внутри `Db.write()`/`db.reader()`.

- `public_router` (`/api/v1`, bearer; UI-сессия — 403 на запись, кроме `end`):
  `POST /subs`, `POST /subs/{id}/end`, `GET /subs`, `POST /subs/{id}/wait`.
- `internal_router` (`/internal/v1`, CLI-админ): `GET /admin/subs`, `POST /admin/subs/{id}/end` (`hub sub
  list|end`), `POST /admin/subs/free` (`hub sub free on|off`, Ф35-1 Н3).

Идентичность сессии — pid + `starttime` процесса в теле запроса: общий токен `all` у всех сессий владельца, чужая
сессия на том же токене ждать и снимать подписку не может (403, подписка не трогается). Ответ ожидания —
только числа и метка из перечня.
"""

import asyncio
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from ... import hostid
from ...work.session import mask_secrets
from .. import adopt as adopt_mod
from .. import chan_subs, connector_auth, peer_msgs, queries, reminders, sub_accounts, subs
from .. import engine as engine_mod
from .. import held as held_mod
from .. import hub_sessions as hub_sessions_mod
from .. import keywords as keywords_mod
from .. import machines as machines_mod
from .. import owner_queue as zq
from .. import session_host as session_host_mod
from .. import transcript as transcript_mod
from .. import unsorted as unsorted_mod
from ..security import Principal, principal_dependency
from .write import authed_or_agent, read_or_agent

public_router = APIRouter(tags=["subs"])
internal_router = APIRouter(tags=["subs-admin"])

_any = Depends(principal_dependency())
_admin = Depends(connector_auth.admin_dependency())

_PID_MAX = 2**31 - 1
_PROC_START_MAX = 2**62
WAIT_TIMEOUT_MAX_S = 120
# Ф25-1: предел сырых списков ключей до нормализации (дубли убираются ядром; после — не больше 100).
_KEYS_RAW_MAX = 1000
_KEY_RAW_MAX = 200
# Версия протокола ожидания у клиента (`hub sub wait` шлёт заголовок). 2 — клиент понимает сигналы сообщений
# (Ф14-5 П20). Старый клиент (без заголовка) сигналы режима `messages` отбрасывал бы как негодные, а ядро уже
# пометило бы их выданными — сигнал потерян (живой дефект 28.09). Поэтому такому клиенту — 409 ДО выдачи.
# Ф20-1 П3: клиент протокола 3 печатает категорию сигнала (`cat`, `thread_ext`), но минимум ядра остаётся 2 —
# живые ожидания и плагин OpenCode протокола 2 после выкладки не получают 409; новые ключи сигнала старый
# клиент отбрасывает (`sub_wait.valid_signals` собирает только известные).
# Ф35-2 (решение 7): сигналы сообщений агентов (`kind: peer`) ядро считает и выдаёт только клиенту протокола
# ≥ 4 — клиент старше отбросил бы их все и вышел с кодом 6, а ядро уже пометило бы их выданными.
SUB_WAIT_HEADER = "x-hub-sub-wait"
SUB_WAIT_PROTOCOL = 2
SUB_WAIT_PEERS_PROTOCOL = 4
# Ф39 B5: то же для сигналов исхода черновика (`kind: draft`) — только клиенту протокола ≥ 5.
SUB_WAIT_DRAFTS_PROTOCOL = 5
# Ф44-1c: то же для сигналов исхода нажатия кнопки бота (`kind: press`) — только клиенту протокола ≥ 6
# (клиент протокола 5 незнакомый вид отбрасывает: `sub_wait.valid_signals`).
SUB_WAIT_PRESSES_PROTOCOL = 6
# Ф49-1 (П4): протокол 7 — сигналы карточек делегату (`kind: card`). Ф66-1: делегат убран, сигналов `card`
# нет; номер протокола не уменьшается, клиент протокола 7 просто их не получает.
SUB_WAIT_CARDS_PROTOCOL = 7
# Ф54-1 (П11): то же для сигналов исхода заявки «нужен человек» (`kind: human`) — только клиенту протокола
# ≥ 8 (клиент протокола 7 незнакомый вид отбросил бы, а ядро уже пометило бы его выданным).
SUB_WAIT_HUMAN_PROTOCOL = 8
# Ф62-6: исходы операций над группами WhatsApp — клиенту протокола ≥ 9.
SUB_WAIT_GROUP_OPS_PROTOCOL = 9


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


def _waiters(request: Request) -> subs.SubWaiters:
    waiters = getattr(request.app.state, "sub_waiters", None)
    if waiters is None:
        waiters = request.app.state.sub_waiters = subs.SubWaiters()
    return waiters


def _chan_waiters(request: Request) -> chan_subs.ChanWaiters:
    """Ф14-5 П8: ожидания подписок на беседы — ключ `(token_id, pid, proc_start, session_id)` (Ф16-4, П1)."""
    waiters = getattr(request.app.state, "chan_sub_waiters", None)
    if waiters is None:
        waiters = request.app.state.chan_sub_waiters = chan_subs.ChanWaiters()
    return waiters


def _client_protocol(request: Request) -> int:
    """Версия протокола ожидания из заголовка клиента; нет или мусор — 0 (старый клиент)."""
    raw = request.headers.get(SUB_WAIT_HEADER)
    try:
        return int(raw) if raw is not None and raw.strip().isdigit() else 0
    except ValueError:
        return 0


def _client_outdated() -> HTTPException:
    return HTTPException(409, "client_outdated")


def _http(exc: subs.SubError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


# --- модели (контракт B5) -------------------------------------------------------------------------------


def triple_query(pid: int | None, proc_start: int | None, session_id: str | None):
    """Своя сессия в параметрах GET-ручек (`/subs`, `/reminders`, `/peer-msgs`): все три или ни одного, тройка
    — по общему валидатору `held.triple_error` (Ф41-2а П4: и условная тройка сессии хаба с `pid < 0`). Тройка
    или `None`; иначе 422."""
    given = (pid, proc_start, session_id)
    if all(v is None for v in given):
        return None
    if any(v is None for v in given):
        raise HTTPException(422, "pid, proc_start и session_id — все три или ни одного")
    if held_mod.triple_error(pid, proc_start, session_id) is not None:
        raise HTTPException(422, "invalid request")
    return given


# Ф33-1 (19.10): машина сессии в параметрах GET-ручек — клиент шлёт её вместе с тройкой; без неё ядро на
# другой машине судило сессию ноутбука по своему `/proc` и отвечало «сессия не найдена» (входящие агентов,
# напоминания, состояние ролей), хотя подписка и ожидание шли.
HOST_QUERY = Query(None, pattern=session_host_mod.HOST_PATTERN)


def query_session(pid: int, proc_start: int, session_id: str, host: str | None) -> dict:
    """Сессия из параметров GET-ручки — как тело `session` (`host` — только если пришёл)."""
    session = {"pid": pid, "proc_start": proc_start, "session_id": session_id}
    if host is not None:
        session["host"] = host
    return session


class MachineIn(BaseModel):
    """Ф77-1 (A3): что клиент сообщает о своей машине (`hub.machine_info.local_machine`). Не в `SessionIn` —
    её валидатор не пишет в базу; регистрирует ручка (`machines.register_from_body`), только когда в `session`
    есть `host`."""

    model_config = ConfigDict(extra="forbid")

    hostname: str | None = Field(default=None, max_length=machines_mod.HOSTNAME_MAX)
    route: Literal["lan", "s1", "s1-socks"] | None = None
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,12}$")

    @field_validator("hostname")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        if value is not None and not value.isprintable():
            raise ValueError("hostname: только печатные символы")
        return value


def machine_reply(conn, session, *, now_ms: int) -> dict:
    """Ф77-1 (A5): поля ответа подписки — машина этой сессии и машина ядра."""
    labels = machines_mod.Labels(conn, now_ms=now_ms)
    return {
        "machine": labels.of_session(session_host_mod.host_of(session), getattr(session, "pid", None)),
        "core_machine": labels.core_label(),
    }


class SessionIn(BaseModel):
    """Тройка сессии. Ф41-2а П4: `pid` — настоящего процесса (>= 1) или условный сессии хаба (<= -1, тогда
    `proc_start == 0` и `session_id == "hub:<id>"`); прочее — 422 (`held.triple_error` — один валидатор на
    все ручки, принимающие `session`)."""

    model_config = ConfigDict(extra="forbid")

    pid: StrictInt = Field(ge=-_PID_MAX, le=_PID_MAX)
    proc_start: StrictInt = Field(ge=0, le=_PROC_START_MAX)
    session_id: str = Field(min_length=1, max_length=128)
    # Ф33-1: машина сессии (`hub.hostid`); нет или совпала с машиной ядра — сессия своя (`/proc`).
    host: str | None = Field(default=None, pattern=session_host_mod.HOST_PATTERN)
    # Ф33-1 (boot): загрузка машины сессии; новая хоронит подписки прежних загрузок этой машины.
    boot: str | None = Field(default=None, pattern=hostid.BOOT_PATTERN)

    @model_validator(mode="after")
    def _triple(self):
        error = held_mod.triple_error(self.pid, self.proc_start, self.session_id)
        if error is not None:
            raise ValueError(error)
        # Ф33-1 (К9): запрос с телом `session` и чужой машиной — контакт сессии (её живость). Зависимость
        # принципала ручки решается раньше тела — без годного токена сюда не доходит.
        session_host_mod.touch_session(self)
        return self


class SessionFullIn(SessionIn):
    cwd: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=subs.SESSION_NAME_MAX)
    name_source: str | None = Field(default=None, max_length=32)
    kind: str = Field(min_length=1, max_length=32)

    @field_validator("cwd")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("cwd: нужен абсолютный путь")
        return value

    @model_validator(mode="after")
    def _hub_kind(self):
        # Ф41-2а (3.2): `kind == "hub"` — только с условной тройкой, и наоборот.
        error = held_mod.triple_error(self.pid, self.proc_start, self.session_id, self.kind)
        if error is None:
            error = engine_mod.ext_kind_error(self.kind, self.session_id)  # Ф74-2
        if error is not None:
            raise ValueError(error)
        return self


class SubscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: StrictInt | None = Field(default=None, ge=1, le=2**53)
    session: SessionFullIn
    # Ф14-5 П20: `messages` — каждое чужое входящее беседы проекта; `None` — новая `cards`, своя живая —
    # режим не меняется.
    # Ф35-1 (решение 4): `direct` — «только адресованное агенту» (без `msg_filter`/`keywords`/`exclude`).
    mode: Literal["cards", "messages", "direct"] | None = None
    # Ф20-1 П1: фильтр режима `messages` — `personal` («лично мне»); только вместе с `mode`; при `cards`
    # — 422.
    msg_filter: Literal["all", "personal"] | None = None
    # Ф25-1 A3: ключи и минус-слова фильтра «лично мне» (нормализует и проверяет `subs.subscribe`); только с
    # `mode="messages"` + `msg_filter="personal"`; `keywords=[]` — снять.
    keywords: list[Annotated[str, Field(max_length=_KEY_RAW_MAX)]] | None = Field(
        default=None, max_length=_KEYS_RAW_MAX
    )
    exclude: list[Annotated[str, Field(max_length=_KEY_RAW_MAX)]] | None = Field(
        default=None, max_length=_KEYS_RAW_MAX
    )
    # Ф36-1 A3 + Ф67-1: интервал пачки сигналов подписки, минуты (0 — сразу); с ключами и без.
    batch_min: StrictInt | None = Field(default=None, ge=0, le=keywords_mod.BATCH_MIN_MAX)
    # Ф26-1: роль сессии в проекте (форму проверяет `subs.role_form`); `""` — снять.
    role: str | None = Field(default=None, max_length=128)
    # Ф35-1 (решение 2): номер в роли — адрес агента `(проект, роль, номер)`.
    number: StrictInt | None = Field(default=None, ge=1, le=subs.ROLE_NO_MAX)
    # Ф38-1 (п.1, П1): фильтр по аккаунтам бесед — «только эти» или «все, кроме этих» (вместе — 422, пустой
    # список — снять); существование аккаунтов проверяет `subs.subscribe`.
    accounts: list[Annotated[StrictInt, Field(ge=1, le=2**53)]] | None = Field(
        default=None, max_length=sub_accounts.ACCOUNTS_MAX
    )
    accounts_except: list[Annotated[StrictInt, Field(ge=1, le=2**53)]] | None = Field(
        default=None, max_length=sub_accounts.ACCOUNTS_MAX
    )
    # Ф32-1: новая подписка с ролью возвращает подписки на беседы прежней сессии этой роли; `False` — нет.
    restore_channels: bool = True
    # Ф48-4 п.1: свой интервал пачки сообщений агентов, минуты (0 — сразу); нет — умолчание проекта.
    agent_batch_min: StrictInt | None = Field(default=None, ge=0, le=subs.AGENT_BATCH_MAX)
    # Ф77-1 (A3): машина клиента; нет (старый клиент) — строка машины без имени хоста.
    machine: MachineIn | None = None


class EndIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionIn | None = None
    reason: Literal["unsubscribed", "conversation_changed", "registry_unreadable"] | None = None


class WaitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionIn
    waiter: str = Field(pattern=r"^[0-9a-f]{16}$")
    timeout_s: StrictInt = Field(ge=1, le=WAIT_TIMEOUT_MAX_S)


# --- кто может подписываться (B4) -----------------------------------------------------------------------


def _subscriber_error(conn, principal: Principal) -> str | None:
    """Скоуп `draft`, токен `interactive`, не старый токен оркестратора (`mcp:orchestrator`) и не старый токен
    дежурной (`mcp:wake-*`, скоуп
    `runs` — запрет остался после Ф57-2), скоупа `orchestrate` нет. Причина отказа или `None`."""
    if principal.is_ui:
        return "подписка — только bearer-токеном сессии"
    if principal.kind != "interactive":
        return "подписка — только interactive-токеном"
    if not principal.has_scope("draft"):
        return "подписке нужен скоуп draft"
    if principal.has_scope("runs") or principal.has_scope("orchestrate"):
        return "токен hub-wake или оркестратора не подписывается"
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = row[0] if row and isinstance(row[0], str) else ""
    if name.startswith(queries.WAKE_TOKEN_PREFIX) or name == "mcp:orchestrator":
        return "токен hub-wake или оркестратора не подписывается"
    return None


def _same_process(rec: subs.SubRec, session: SessionIn) -> bool:
    return rec.session_pid == session.pid and rec.session_proc_start == session.proc_start


def _other_oc_session(rec, session: SessionIn) -> bool:
    """Ф16-4 (П1): у OpenCode один процесс держит много сессий — подписка OpenCode с другим `session_id` на
    том же процессе принадлежит ДРУГОЙ живой сессии (403, подписку не трогать), а не «сменившемуся
    разговору» (у Claude Code — 410 `conversation_changed`)."""
    return subs.rec_many_convs(rec) and rec.session_id != session.session_id


# --- POST /subs ----------------------------------------------------------------------------------------


@public_router.post("/subs")
async def post_subs(body: SubscribeIn, request: Request, principal: Principal = _any) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    waiters = _waiters(request)
    chan_waiters = _chan_waiters(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)
    session = body.session.model_dump()

    def _fn(conn):
        error = _subscriber_error(conn, principal)
        if error is not None:
            raise subs.SubError(403, error)
        # Ф33-1 (boot): машина сессии перезагрузилась — линии прежних загрузок свободны до разбора ролей.
        _end_rebooted(conn, principal, body.session, now_ms, _audit(principal, client_name, request_id))
        machines_mod.register_from_body(conn, body.session, body.machine, now_ms=now_ms)  # Ф77-1 (A3)
        done = subs.subscribe(
            conn,
            principal=principal,
            session=session,
            project_id=body.project_id,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            mode=body.mode,
            msg_filter=body.msg_filter,
            keywords=body.keywords,
            exclude=body.exclude,
            role=body.role,
            number=body.number,
            waiting=waiters.waiting,
            batch_min=body.batch_min,
            accounts=body.accounts,
            accounts_except=body.accounts_except,
            agent_batch_min=body.agent_batch_min,
        )
        restore = None
        if done.role_source_id is not None and body.restore_channels:
            # Ф32-1 (П3): источник роли найден — в той же транзакции вернуть его подписки на беседы.
            restore = chan_subs.restore_for_role(
                conn,
                principal=principal,
                session=session,
                source_sub_id=done.role_source_id,
                source_reason=done.role_source_reason,
                now_ms=now_ms,
                client_name=client_name,
                request_id=request_id,
                waiting=lambda rec: chan_waiters.waiting(chan_subs.rec_key(rec), rec.session_id),
                # Ф92-1 (B2): источник с другой линии роли — беседы живой сессии роли не трогать.
                role_key=done.rec.role_key if done.role_source_wide else None,
                # Ревью Ф92-1: аудит отказа целиком — только у новой подписки (повторы не засоряют).
                sub_id=done.rec.id if done.created else None,
            )
        elif done.rec.role_key is not None and body.restore_channels and done.created:
            # Ф92-1 (B4): у новой подписки с ролью нет прежней подписки роли — не молчать (ответ и аудит).
            chan_subs.audit_restore_blocked(
                conn,
                principal=principal,
                sub_id=done.rec.id,
                source_id=None,
                blocked="no_source",
                now_ms=now_ms,
                client_name=client_name,
                request_id=request_id,
            )
        row = subs.one_json(conn, done.rec, now_ms=now_ms, waiters=waiters)
        subs.touch_held(conn, done.rec.session_pid, now_ms)  # Ф41-4: окно и хост видят подписку сессии хаба
        # Ф34-2 решение 5: сработавшие напоминания роли, которые ещё никто не прочёл (`hub_reminders`).
        waiting = reminders.waiting_count(conn, done.rec)
        # Ф35-2 решение 7: непрочитанные сообщения агентов линии (в том числе уже отсигналенные).
        unread = peer_msgs.unread_count(conn, done.rec)
        # Ф52-2 П2: аккаунты, чьё «Неразобранное» слышит проект подписки (`direct` не слышит).
        # Ревью: только токену, который сам видит «Неразобранное», — иначе сигналов по нему нет.
        heard = (
            unsorted_mod.project_accounts(conn, done.rec.project_id)
            if not done.rec.direct and principal.sees_unsorted
            else []
        )
        where = machine_reply(conn, body.session, now_ms=now_ms)  # Ф77-1 (A5)
        return row, done, subs.neighbours(conn, done.rec), restore, waiting, unread, heard, where

    try:
        row, done, neighbours, restore, reminders_waiting, peer_unread, heard, where = await db.write(_fn)
    except subs.SubError as exc:
        raise _http(exc) from exc
    result = {"sub": row, "created": done.created, "backfilled": done.backfilled, "neighbours": neighbours}
    result.update(where)
    if heard:
        result["unsorted_accounts"] = heard
    result["reminders_waiting"] = reminders_waiting
    result["peer_unread"] = peer_unread
    if restore is not None and (restore.restored or restore.skipped or restore.blocked):
        result["restored_channels"] = restore.restored
        result["skipped_channels"] = restore.skipped
        if restore.blocked is not None:
            result["restore_blocked"] = restore.blocked  # П6/П7: `old` или причина конца источника роли
        # П6: идёт ли ожидание бесед этой сессии — для текста MCP (запускать ли команду ожидания).
        s = body.session
        key = chan_subs.session_key(principal.token_id, s.pid, s.proc_start, s.session_id)
        result["channels_waiting"] = chan_waiters.waiting(key, s.session_id)
        if restore.restored:
            request.app.state.notifier.notify()  # после коммита: идущее ожидание бесед подхватит вернувшиеся
    elif done.role_source_id is not None and body.restore_channels:
        # Ф92-1 (B4): источник роли есть, возвращать нечего (беседы уже при сессии или сняты насовсем).
        result["restore_source"] = done.role_source_id
    elif done.rec.role_key is not None and body.restore_channels:
        result["restore_blocked"] = "no_source"  # Ф92-1 (B4): прежней подписки роли нет
    if done.inherited_from is not None:
        result["inherited_from"] = done.inherited_from  # Ф25-1 П2
        result["inherited_why"] = done.inherited_why  # Ф26-1 A2
    if done.inherit_blocked:
        result["inherit_blocked"] = "several_roleless"  # Ф26-1 Р11
    if done.accounts_inherited:
        result["accounts_inherited"] = True  # Ф38-1 П6: фильтр по аккаунтам взят у прежней подписки
    # Ф38-3 C: другие линии роли с другими настройками (номер не задан и взят наименьший свободный).
    result["other_lines"] = list(done.other_lines)
    return result


# --- POST /subs/beat (Ф33-1) ---------------------------------------------------------------------------


class TailEventIn(BaseModel):
    """Ф99-7.3: событие хвоста стенограммы в пульсе (форма ленты сессии хаба); текст маскируется ещё раз."""

    model_config = ConfigDict(extra="forbid")

    id: StrictInt = Field(ge=0, le=2**62)
    ts: StrictInt = Field(ge=0, le=2**53)
    kind: Literal["say", "text", "tool", "tool_done"]
    tool: str | None = Field(default=None, max_length=128)
    summary: str = Field(max_length=8100)
    is_error: bool = False
    ref: str | None = Field(default=None, max_length=128)


class BeatIn(BaseModel):
    """Ф33-1: пульс MCP-сервера сессии Claude Code (раз в `BEAT_S`). `session` — с `host`; `gone` — процесс
    сессии мёртв по `/proc` её машины (К1: шлёт только MCP-процесс на той же машине); числа кэша и контекста —
    посчитаны на машине сессии тем же `core.transcript` (у ядра на другой машине стенограммы нет)."""

    model_config = ConfigDict(extra="forbid")

    session: SessionIn
    gone: bool = False
    cache_until_ms: StrictInt | None = Field(default=None, ge=0, le=2**53)
    context_tokens: StrictInt | None = Field(default=None, ge=0, le=10**9)
    ttl_s: StrictInt | None = Field(default=None, ge=1, le=86_400)
    model: str | None = Field(default=None, max_length=128)
    # Ф77-1 (A3): машина клиента — каждый пульс освежает её строку (последний контакт).
    machine: MachineIn | None = None
    # Ф99-7.3: последние события стенограммы (до 50) — лента «только смотреть» для сессий другой машины.
    tail: list[TailEventIn] | None = Field(default=None, max_length=50)


def _audit(principal: Principal, client_name, request_id: str) -> dict:
    return {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": client_name,
        "request_id": request_id,
    }


def _end_rebooted(conn, principal: Principal, session, now_ms: int, audit: dict) -> int:
    """Ф33-1 (boot): тело сессии другой машины с загрузкой — снять подписки прежних загрузок этой машины под
    этим токеном (ревью Б1)."""
    host, boot = session.host, session.boot
    if principal.is_ui or boot is None or not session_host_mod.current().foreign(host):
        return 0
    return subs.end_rebooted(
        conn, host=host, boot=boot, token_id=principal.token_id, now_ms=now_ms, audit=audit
    )


async def _save_seen(request: Request, principal: Principal, session) -> None:
    """Ф33-1 (ревью В1): контакт сессии другой машины — в её строки подписок, не чаще раза в 10 мин. Правка
    17.10: и сессии своей машины, названной в теле явно (после переезда ядра она станет чужой)."""
    if principal.is_ui:
        return
    seen = session_host_mod.current().to_save(session.host, session.pid, session.proc_start)
    if seen is None:
        return
    token_id = principal.token_id
    await request.app.state.db.write(
        lambda conn: subs.mark_seen(
            conn, session.host, session.pid, session.proc_start, token_id=token_id, seen_ms=seen
        )
    )


@public_router.post("/subs/beat")
async def post_subs_beat(body: BeatIn, request: Request, principal: Principal = _any) -> dict:
    """Ф33-1 (12 «33-1» п.2, 12а К1, К2, К9). Контакт личности уже записал валидатор `SessionIn`. Ответ:
    `remote` — сессия на другой машине (своя: пульс ей не нужен, ядро видит её `/proc`); `subscribed` — у
    этой сессии под этим токеном есть живые подписки на проект ИЛИ на беседы; `beat` — слать ли пульс дальше
    (`remote and subscribed`); `neighbours` — другие живые личности той же машины под этим токеном
    `{pid, proc_start, session_id}`: клиент сверяет их со своим `/proc` и за мёртвые шлёт `gone` (ревью П1).

    Всё разрушающее — только строки ЭТОЙ машины (`session_host` == `host` тела) под ЭТИМ токеном (ревью Б1).
    Пульс с новым `session_id` при живых подписках прежнего разговора того же процесса снимает их
    `conversation_changed` (как `hub sub wait`). `gone` — процесс мёртв по `/proc` своей машины: живые
    подписки процесса снимаются `session_gone` сразу. Новая загрузка машины (`boot`) — подписки прежних
    загрузок тоже."""
    if principal.is_ui:
        raise HTTPException(403, "пульс — только bearer-токеном сессии")
    s = body.session
    hosts = session_host_mod.current()
    db = request.app.state.db
    now_ms = _now_ms(request)
    if hosts.get(s.host, s.pid, s.proc_start) is None:
        if s.host is not None:
            # Ф77-1 (A3): машина клиента — своя строка (последний контакт не чаще раза в минуту).
            await db.write(lambda conn: machines_mod.register_from_body(conn, s, body.machine, now_ms=now_ms))
        # Правка 17.10: сессия своей машины (проба пульса раз в 600 с) — тоже контакт для `session_seen_ts`.
        await _save_seen(request, principal, s)
        return {"remote": False, "subscribed": False, "beat": False}
    client_name = _client_name(request)
    request_id = uuid.uuid4().hex
    token_id = principal.token_id
    audit = _audit(principal, client_name, request_id)

    def _contact(conn) -> int:
        machines_mod.register_from_body(conn, s, body.machine, now_ms=now_ms)  # Ф77-1 (A3)
        return _end_rebooted(conn, principal, s, now_ms, audit)

    rebooted = await db.write(_contact)

    if body.gone:
        ended = await db.write(
            lambda conn: subs.end_process(
                conn,
                s.host,
                s.pid,
                s.proc_start,
                "session_gone",
                token_id=token_id,
                now_ms=now_ms,
                audit=audit,
            )
        )
        return {"remote": True, "subscribed": False, "beat": False, "ended": ended + rebooted}

    hosts.set_cache(s.host, s.pid, s.proc_start, session_host_mod.cache_from_beat(body.model_dump()))
    await _save_seen(request, principal, s)
    if body.tail is not None:
        hosts.set_tail(
            s.host,
            s.pid,
            s.proc_start,
            [{**e.model_dump(), "summary": mask_secrets(e.summary)} for e in body.tail],
            now_ms,
        )

    def _fn(conn):
        return subs.beat(conn, token_id=token_id, session=s.model_dump(), now_ms=now_ms, audit=audit)

    subscribed, neighbours = await db.write(_fn)
    return {"remote": True, "subscribed": subscribed, "beat": subscribed, "neighbours": neighbours}


# --- POST /subs/rebind (Ф99-0) --------------------------------------------------------------------------


class RebindIn(BaseModel):
    """Ф99-0: тело `POST /subs/rebind` — сессия внешнего Codex/Cursor с её нынешним процессом."""

    model_config = ConfigDict(extra="forbid")

    session: SessionIn


@public_router.post("/subs/rebind")
async def post_subs_rebind(body: RebindIn, request: Request, principal: Principal = _any) -> dict:
    """Ф99-0 (долг 23.10): MCP-сервер окна Codex/Cursor впервые увидел разговор (сервер живёт под общим
    демоном `codex app-server`; демон перезапустился — новый сервер и новый процесс) — живые подписки этого
    разговора под этим токеном переезжают на нынешний процесс (`subs.ext_rebind`): роль, номер, беседы и
    ждущие сигналы остаются. Только bearer; только внешняя сессия (`<движок>:<id>`, иначе 422); процесс тела
    должен быть жив (иначе 409). Ответ — сколько строк переехало."""
    if principal.is_ui:
        raise HTTPException(403, "только bearer-токеном сессии")
    s = body.session
    if not subs.ext_idle(s.pid, s.session_id):
        raise HTTPException(422, "только внешняя сессия Codex или Cursor")
    if not subs.is_alive(s.pid, s.proc_start, s.host):
        raise HTTPException(409, "сессия не найдена")
    now_ms = _now_ms(request)
    audit = _audit(principal, _client_name(request), uuid.uuid4().hex)
    token_id = principal.token_id
    session = s.model_dump()
    rebound = await request.app.state.db.write(
        lambda conn: subs.ext_rebind(conn, token_id=token_id, session=session, now_ms=now_ms, audit=audit)
    )
    return {"rebound": rebound}


# --- POST /subs/{id}/end -------------------------------------------------------------------------------


def _sub_visible(conn, principal: Principal, rec: subs.SubRec) -> bool:
    """Ф35-1 (решение 14): подписку агента вне проектов (проект 0 — `queries.project_visible` его не знает)
    видит и снимает только принципал без списка проектов (UI владельца, токен `all`)."""
    if rec.project_id == subs.NO_PROJECT_ID:
        return principal.project_ids is None
    return queries.project_visible(conn, principal, rec.project_id)


@public_router.post("/subs/{sub_id}/end")
async def post_sub_end(
    sub_id: int, body: EndIn, request: Request, principal: Principal = authed_or_agent
) -> dict:
    """Своя сессия (тот же токен и pid + `starttime`) — с причиной из `CLIENT_END_REASONS`; UI-сессия — без
    `session`, причина `ended_by_owner`. Уже завершённая — 200 с её строкой, ничего не пишется. Ф88-1 (П1):
    агент владельца — как UI-сессия; бот `human_bot` — 403, как прежде."""
    owner = principal.is_ui or zq.is_agent(principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    waiters = _waiters(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)

    def _fn(conn):
        rec = subs.get_sub(conn, sub_id)
        if rec is None or not _sub_visible(conn, principal, rec):
            raise subs.SubError(404, "not found")
        if owner:
            if body.session is not None or body.reason is not None:
                raise subs.SubError(422, "invalid request")
            reason = "ended_by_owner"
        else:
            if body.session is None or body.reason is None:
                raise subs.SubError(422, "invalid request")
            if (
                principal.token_id != rec.api_token_id
                or not _same_process(rec, body.session)
                or _other_oc_session(rec, body.session)
            ):
                raise subs.SubError(403, "подписка другой сессии")
            reason = body.reason
        ended = subs.end_sub(
            conn,
            rec.id,
            reason,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
        )
        hub_id = held_mod.held_id(rec.session_pid)
        if ended and reason == "unsubscribed" and hub_id is not None:
            # Ф64-1 (раздел 5): своя отписка сессии хаба — «не подписывать» до слова владельца (хост не будит её
            # ради подписки). Отписка владельцем из интерфейса (`ended_by_owner`) желаемое не трогает.
            hub_sessions_mod.drop_want_self(
                conn,
                hub_id,
                now_ms=now_ms,
                audit={
                    "actor": principal.kind,
                    "api_token_id": principal.token_id,
                    "client_name": client_name,
                    "request_id": request_id,
                },
            )
        current = subs.get_sub(conn, rec.id)
        return subs.one_json(conn, current, now_ms=now_ms, waiters=waiters)

    try:
        row = await db.write(_fn)
    except subs.SubError as exc:
        raise _http(exc) from exc
    return {"sub": row}


# --- GET /subs/{id}/transcript (Ф99-7.3) ---------------------------------------------------------------

TRANSCRIPT_REFRESH_S = 60


@public_router.get("/subs/{sub_id}/transcript")
async def get_sub_transcript(
    sub_id: int, request: Request, limit: int = Query(100, ge=1, le=transcript_mod.TAIL_MAX),
    principal: Principal = _any,
) -> dict:
    """Ф99-7.3 (П12): хвост стенограммы внешней сессии Claude Code — лента «только смотреть». ТОЛЬКО кука
    интерфейса (bearer, агент владельца — 403). Ответ: `{supported, engine, remote, refresh_s,
    events, updated_ms}`; `supported=false` — не Claude Code или сессия хаба (у неё своя лента);
    `remote` — сессия другой машины:
    события из её пульса, обновляются раз в `refresh_s` секунд. События маскированы (`mask_secrets`)."""
    if not principal.is_ui:
        raise HTTPException(403, "лента сессии — только интерфейс")
    with request.app.state.db.reader() as conn:
        rec = subs.get_sub(conn, sub_id)
        if rec is None or not _sub_visible(conn, principal, rec):
            raise HTTPException(404, "not found")
    engine = subs.rec_engine(rec)
    base = {"engine": engine, "remote": False, "refresh_s": None, "events": [], "updated_ms": None}
    if engine != "claude" or held_mod.is_held(rec.session_pid) or not rec.session_id:
        return {**base, "supported": False}
    far, tail, tail_ms = session_host_mod.tail_of(rec.session_host, rec.session_pid, rec.session_proc_start)
    if far:
        events = (tail or [])[-limit:]
        return {**base, "supported": True, "remote": True, "refresh_s": TRANSCRIPT_REFRESH_S,
                "events": events, "updated_ms": tail_ms or None}
    events = await asyncio.to_thread(transcript_mod.tail_for, rec.session_id, limit)
    return {**base, "supported": True, "events": events}


# --- GET /subs -----------------------------------------------------------------------------------------


@public_router.get("/subs")
async def get_subs(
    request: Request,
    live: int = Query(1, ge=0, le=1),
    pid: int | None = Query(None, ge=-_PID_MAX, le=_PID_MAX),
    proc_start: int | None = Query(None, ge=0, le=_PROC_START_MAX),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    offline: int = Query(0, ge=0, le=1),
    principal: Principal = read_or_agent,
) -> dict:
    """Видимость — по проекту подписки. `live=1` — незавершённые (живость процесса — поле `live`), `live=0` —
    ещё и завершённые за сутки. Ничего не пишет. Ф16-4 (П17): своя сессия в запросе (`pid`, `proc_start`,
    `session_id` — все три) — в каждой строке поле `mine` (ядро сравнивает полную тройку; `session_id8`
    соседних сессий OpenCode совпадает, `session_pid` у них общий). Ф41-6 (раздел 5, п.3): `offline=1` —
    вместо списков `{"lines": [...]}`: линии роли «не в сети» (`adopt.offline_lines`)."""
    me = triple_query(pid, proc_start, session_id)
    db = request.app.state.db
    now_ms = _now_ms(request)
    if offline:
        with db.reader() as conn:
            lines = adopt_mod.offline_lines(conn, project_ids=principal.project_ids, now_ms=now_ms)
        return {"lines": lines}
    with db.reader() as conn:
        items = subs.list_subs(
            conn,
            project_ids=principal.project_ids,
            live_only=bool(live),
            now_ms=now_ms,
            waiters=_waiters(request),
            me=me,
            # Ф41-1: срок кэша сессии Claude Code — только интерфейсу владельца (Ф88-1: и его агенту)
            with_cache=principal.is_ui or zq.is_agent(principal),
        )
        # Ф14-5 A4/П6: подписки на беседы, видимые токену (по нынешнему проекту беседы).
        channels = chan_subs.list_chan_subs(
            conn,
            project_ids=principal.project_ids,
            live_only=bool(live),
            now_ms=now_ms,
            waiters=_chan_waiters(request),
            me=me,
        )
    return {"items": items, "channels": channels}


# --- POST /subs/{id}/wait ------------------------------------------------------------------------------


async def _end_and_gone(
    request: Request, sub_id: int, reason: str, principal: Principal | None
) -> HTTPException:
    """Завершить подписку (`session_gone` — от имени ядра, `conversation_changed` — токеном сессии) и вернуть
    410 с фактической причиной (подписку могла уже снять фоновая задача)."""
    now_ms = _now_ms(request)
    client_name = _client_name(request)

    def _fn(conn):
        subs.end_sub(
            conn,
            sub_id,
            reason,
            now_ms=now_ms,
            actor="core" if principal is None else principal.kind,
            api_token_id=None if principal is None else principal.token_id,
            client_name=None if principal is None else client_name,
        )
        rec = subs.get_sub(conn, sub_id)
        return rec.end_reason if rec is not None else reason

    return _http(subs.gone(await request.app.state.db.write(_fn)))


async def _precheck(request: Request, sub_id: int, body: WaitIn, principal: Principal) -> None:
    """Порядок B4: чужой токен — 403; завершена — 410; не та pid + `starttime` — 403 (подписку не трогать);
    процесс мёртв — `session_gone`, 410; тот же процесс, другой разговор — `conversation_changed`, 410."""
    with request.app.state.db.reader() as conn:
        rec = subs.get_sub(conn, sub_id)
        # Ф41-2а П2: по «мертва» подписка ЗАВЕРШАЕТСЯ — у сессии хаба живость по базе, не по реестру.
        alive = rec is not None and subs.rec_session_alive(conn, rec)
    if rec is None:
        raise HTTPException(404, "not found")
    if principal.token_id != rec.api_token_id or principal.is_ui:
        raise HTTPException(403, "подписка другого токена")
    if rec.ended_ts is not None:
        raise _http(subs.gone(rec.end_reason))
    if not _same_process(rec, body.session) or _other_oc_session(rec, body.session):
        raise HTTPException(403, "подписка другой сессии")
    if not alive:
        raise await _end_and_gone(request, sub_id, "session_gone", None)
    if rec.session_id != body.session.session_id:
        raise await _end_and_gone(request, sub_id, "conversation_changed", principal)


@public_router.post("/subs/{sub_id}/wait")
async def post_sub_wait(sub_id: int, body: WaitIn, request: Request, principal: Principal = _any):
    """Long-poll на `Notifier` (снять поколение -> прочитать -> выдать или ждать) до годного сигнала с
    дебаунсом или таймаута. Одно ожидание на подписку: новое вытесняет прежнее (409 `superseded`); свой
    счётчик — не `WAITS_PER_TOKEN` ожиданий бесед. Клиент ушёл до выдачи — 204."""
    await _precheck(request, sub_id, body, principal)
    app = request.app
    db = app.state.db
    notifier = app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)  # lifespan не отработал (ASGITransport) — как `/stream`
    waiters = _waiters(request)
    client_name = _client_name(request)
    request_id = uuid.uuid4().hex
    presence_store = getattr(app.state, "presence", None)
    deadline = loop.time() + body.timeout_s
    protocol = _client_protocol(request)
    peers = protocol >= SUB_WAIT_PEERS_PROTOCOL
    drafts = protocol >= SUB_WAIT_DRAFTS_PROTOCOL
    presses = protocol >= SUB_WAIT_PRESSES_PROTOCOL
    cards = protocol >= SUB_WAIT_CARDS_PROTOCOL
    humans = protocol >= SUB_WAIT_HUMAN_PROTOCOL
    group_ops = protocol >= SUB_WAIT_GROUP_OPS_PROTOCOL

    mark, had_previous = waiters.enter(sub_id, protocol)
    if had_previous:
        notifier.notify()  # разбудить прежнее ожидание — оно увидит, что вытеснено
    # Ф33-1: идущее ожидание сессии другой машины держит её живой (и закрытое — ещё `STALE_MS`).
    hosts = session_host_mod.current()
    far = hosts.wait_begin(body.session.host, body.session.pid, body.session.proc_start)
    await _save_seen(request, principal, body.session)
    try:
        while True:
            if not waiters.is_current(sub_id, mark):
                raise HTTPException(409, "superseded")
            gen = notifier.gen  # ДО чтения БД — notify() между чтением и wait() не потеряется
            now_ms = _now_ms(request)
            with db.reader() as conn:
                rec = subs.get_sub(conn, sub_id)
                # Ф14-6b П1: сигналы сообщений, ждущие расшифровку, в `pending` не входят — ожидание спит до
                # конца удержания ближайшего (`held_ms`) или до пробуждения (готовность расшифровки — коммит).
                pending, held_ms = subs.wait_state(
                    conn,
                    sub_id,
                    now_ms=now_ms,
                    peers=peers,
                    drafts=drafts,
                    presses=presses,
                    cards=cards,
                    humans=humans,
                    group_ops=group_ops,
                )
                alive = rec is not None and subs.rec_session_alive(conn, rec)  # Ф41-2а П2: хаб — по базе
            if rec is None or rec.ended_ts is not None:
                raise _http(subs.gone(rec.end_reason if rec is not None else None))
            if not alive:
                raise await _end_and_gone(request, sub_id, "session_gone", None)
            if rec.mode == "messages" and protocol < SUB_WAIT_PROTOCOL:
                raise _client_outdated()  # ничего не выдано и не помечено
            if notifier.closed:
                # Ф31-1 П5: ядро останавливается — пусто и без `deliver`: отмена после коммита выдачи потеряла
                # бы сигнал.
                return {"sub_id": sub_id, "project_id": rec.project_id, "signals": []}
            remaining = deadline - loop.time()
            wait_s = remaining
            if held_ms:
                wait_s = min(wait_s, held_ms / 1000)
            if pending:
                left_ms = subs.debounce_left_ms(rec, now_ms)
                if left_ms == 0:
                    presence = (
                        [{"channel_id": c, "thread_ext_id": t} for c, t in presence_store.items(now_ms)]
                        if presence_store is not None
                        else []
                    )

                    def _deliver(conn, now_ms=now_ms, presence=presence):
                        return subs.deliver(
                            conn,
                            sub_id,
                            now_ms=now_ms,
                            visible_projects=principal.project_ids,
                            presence=presence,
                            api_token_id=principal.token_id,
                            client_name=client_name,
                            request_id=request_id,
                            peers=peers,
                            drafts=drafts,
                            presses=presses,
                            cards=cards,
                            humans=humans,
                            group_ops=group_ops,
                        )

                    signals = await db.write(_deliver)
                    if signals:
                        return {"sub_id": sub_id, "project_id": rec.project_id, "signals": signals}
                    continue  # негодные сняты (`skipped`) или подписка завершена — перечитать
                wait_s = min(wait_s, left_ms / 1000)
            if remaining <= 0:
                await db.write(lambda conn, now_ms=now_ms: subs.touch_wait(conn, sub_id, now_ms=now_ms))
                return {"sub_id": sub_id, "project_id": rec.project_id, "signals": []}
            await notifier.wait(gen, max(wait_s, 0.0))
            if not waiters.is_current(sub_id, mark):
                raise HTTPException(409, "superseded")
            if await request.is_disconnected():
                await db.write(lambda conn: subs.touch_wait(conn, sub_id, now_ms=_now_ms(request)))
                return Response(status_code=204)  # отвечать некому
    finally:
        waiters.leave(sub_id, mark)
        if far:
            hosts.wait_end(body.session.host, body.session.pid, body.session.proc_start)


# --- подписка на беседу (Ф14-5, A4) --------------------------------------------------------------------


class ChanSubscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: StrictInt = Field(ge=1, le=2**62)
    session: SessionFullIn
    # Ф31-1 решение 4: ключи и минус-слова подписки на беседу (нормализует `chan_subs.subscribe`, пределы —
    # как у подписки на проект); `keywords=[]` — снять, `None` — у своей живой как есть.
    keywords: list[Annotated[str, Field(max_length=_KEY_RAW_MAX)]] | None = Field(
        default=None, max_length=_KEYS_RAW_MAX
    )
    exclude: list[Annotated[str, Field(max_length=_KEY_RAW_MAX)]] | None = Field(
        default=None, max_length=_KEYS_RAW_MAX
    )
    # Ф36-1 A3 + Ф67-1: интервал пачки сигналов подписки, минуты (0 — сразу); с ключами и без.
    batch_min: StrictInt | None = Field(default=None, ge=0, le=keywords_mod.BATCH_MIN_MAX)
    # Ф77-1 (A3): машина клиента.
    machine: MachineIn | None = None


class ChanEndIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionIn
    channel_id: StrictInt | None = Field(default=None, ge=1, le=2**62)
    reason: Literal["unsubscribed", "conversation_changed", "registry_unreadable"] = "unsubscribed"


@public_router.post("/chan-subs")
async def post_chan_subs(body: ChanSubscribeIn, request: Request, principal: Principal = _any) -> dict:
    """A2/П14: живая интерактивная сессия, беседа видна токену, не в архиве (заготовка — можно, 24.10); до
    `chan_subs.MAX_CHAN_SUBS` бесед на разговор. `count` — живых подписок разговора на беседы.

    Ф39 A0/A3: подписка на беседу — своя у каждой сессии (одна живая на пару беседа + сессия), подписки
    разных сессий независимы. `others` — другие живые сессии, слушающие эту беседу: `{id, role, role_no,
    session_name, keys, exclude}` (только ЧИСЛА ключей)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    waiters = _chan_waiters(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)
    session = body.session

    def _fn(conn):
        error = _subscriber_error(conn, principal)
        if error is not None:
            raise subs.SubError(403, error)
        _end_rebooted(conn, principal, session, now_ms, _audit(principal, client_name, request_id))  # Ф33-1
        machines_mod.register_from_body(conn, session, body.machine, now_ms=now_ms)  # Ф77-1 (A3)
        rec, created = chan_subs.subscribe(
            conn,
            principal=principal,
            session=session.model_dump(),
            channel_id=body.channel_id,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            keywords=body.keywords,
            exclude=body.exclude,
            batch_min=body.batch_min,
        )
        mine = chan_subs.session_subs(
            conn, principal.token_id, session.pid, session.proc_start, session.session_id
        )
        count = len(mine)
        others = []
        for other in chan_subs.listeners(conn, rec.channel_id, except_key=chan_subs.rec_key(rec)):
            role, role_no = subs.session_role(conn, chan_subs.rec_key(other), rec.project_id)
            keys, minus = keywords_mod.spec_counts(other.keywords_json)
            others.append(
                {
                    "id": other.id,
                    "role": role,
                    "role_no": role_no if role is not None else None,
                    "session_name": other.session_name,
                    "keys": keys,
                    "exclude": minus,
                }
            )
        where = machine_reply(conn, session, now_ms=now_ms)  # Ф77-1 (A5)
        return chan_subs.one_json(conn, rec, now_ms=now_ms, waiters=waiters), created, count, others, where

    try:
        row, created, count, others, where = await db.write(_fn)
    except subs.SubError as exc:
        raise _http(exc) from exc
    return {"sub": row, "created": created, "count": count, "others": others, **where}


@public_router.post("/chan-subs/end")
async def post_chan_subs_end(body: ChanEndIn, request: Request, principal: Principal = _any) -> dict:
    """Снять одну беседу (`channel_id`) или все подписки ЭТОГО разговора на беседы. П17: подписки другого
    `session_id` того же процесса не трогаются — 410 `conversation_changed`. Уже завершённая подписка беседы
    этого разговора — 200 с её строкой. Ф39 A3: чужие подписки беседы сессии не касаются — своей нет (ни
    живой, ни завершённой) — 404 (было 403 «подписка другой сессии»)."""
    if principal.is_ui:
        raise HTTPException(403, "снять подписку сессии из UI — hub sub end c<id>")
    db = request.app.state.db
    now_ms = _now_ms(request)
    waiters = _chan_waiters(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)
    s = body.session

    def _end(conn, rec):
        chan_subs.end(
            conn,
            rec.id,
            body.reason,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
        )
        return chan_subs.one_json(conn, chan_subs.get(conn, rec.id), now_ms=now_ms, waiters=waiters)

    oc = subs.many_convs(s.session_id)  # Ф74-2: OpenCode, внешние Codex и Cursor

    def _fn(conn):
        if body.channel_id is None:
            mine = chan_subs.session_subs(conn, principal.token_id, s.pid, s.proc_start, s.session_id)
            # Ф16-4 (П1): у OpenCode подписки других сессий того же процесса — не «сменившийся разговор».
            others = not oc and chan_subs.live_of_process(conn, s.pid, s.proc_start, principal.token_id)
            if not mine and others:
                raise subs.gone("conversation_changed")
            return [_end(conn, rec) for rec in mine]
        key = chan_subs.session_key(principal.token_id, s.pid, s.proc_start, s.session_id)
        live = chan_subs.live_of_session(conn, body.channel_id, key)
        if live is not None:
            return [_end(conn, live)]
        if not oc and any(
            rec.channel_id == body.channel_id
            for rec in chan_subs.live_of_process(conn, s.pid, s.proc_start, principal.token_id)
        ):
            # П17: беседу держит другой разговор того же процесса и токена — его подписку не трогать.
            raise subs.gone("conversation_changed")
        ended = [
            rec
            for rec in chan_subs.last_of_channel(conn, body.channel_id)
            if rec.api_token_id == principal.token_id
            and _same_process(rec, s)
            and rec.session_id == s.session_id
        ]
        if not ended:
            raise subs.SubError(404, "not found")
        return [chan_subs.one_json(conn, ended[0], now_ms=now_ms, waiters=waiters)]

    try:
        rows = await db.write(_fn)
    except subs.SubError as exc:
        raise _http(exc) from exc
    return {"items": rows}


class ChanWaitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionIn
    waiter: str = Field(pattern=r"^[0-9a-f]{16}$")
    timeout_s: StrictInt = Field(ge=1, le=WAIT_TIMEOUT_MAX_S)


@public_router.post("/chan-subs/wait")
async def post_chan_subs_wait(body: ChanWaitIn, request: Request, principal: Principal = _any):
    """A4 + П8, П17: long-poll как `/subs/{id}/wait`, одно ожидание на сессию (ключ — токен, pid,
    `starttime`; новое вытесняет прежнее — 409 `superseded`). Подписки разговора перечитываются на каждой
    итерации (новая подхватывается, `moved`/`channel_gone` — и без `pending`); дебаунс 5 с от последней выдачи
    сессии. Мёртвая сессия — `session_gone` всем подпискам процесса, 410; у этого `session_id` живых нет, а у
    процесса есть с другим — 410 `conversation_changed` (подписки не трогаются); живых нет вовсе — 410 с
    последней причиной (никогда не было — 404). Ответ — только числа; клиент ушёл — 204."""
    if principal.is_ui:
        raise HTTPException(403, "ожидание — только bearer-токеном сессии")
    # Ревью Ф14-6: метку протокола ожидание бесед НЕ требует — ручка появилась в Ф14-5, её клиенты сигналы
    # бесед понимают; отказ 409 лишь глушил бы уже идущие ожидания (старый клиент на 409 пишет
    # «не перезапускай»).
    app = request.app
    db = app.state.db
    notifier = app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)
    waiters = _chan_waiters(request)
    client_name = _client_name(request)
    request_id = uuid.uuid4().hex
    s = body.session
    token_id = principal.token_id
    key = chan_subs.session_key(token_id, s.pid, s.proc_start, s.session_id)
    oc = subs.many_convs(s.session_id)  # Ф74-2: OpenCode, внешние Codex и Cursor
    deadline = loop.time() + body.timeout_s

    mark, had_previous = waiters.enter(key, s.session_id)
    if had_previous:
        notifier.notify()
    hosts = session_host_mod.current()
    far = hosts.wait_begin(s.host, s.pid, s.proc_start)  # Ф33-1: как у `/subs/{id}/wait`
    await _save_seen(request, principal, s)
    try:
        while True:
            if not waiters.is_current(key, mark):
                raise HTTPException(409, "superseded")
            gen = notifier.gen
            now_ms = _now_ms(request)
            with db.reader() as conn:
                procs = chan_subs.live_of_process(conn, s.pid, s.proc_start, token_id)
                mine = [r for r in procs if r.session_id == s.session_id]
                if oc:
                    # Ф16-4 (П1): подписки других сессий OpenCode того же процесса — не наши и не
                    # «сменившийся разговор».
                    procs = mine
                actions = chan_subs.lazy_actions(conn, mine, principal.project_ids)
                # Ф14-6b П1: удержанные (ждут расшифровку) — не в `pending`, спим до конца удержания.
                pending, held_ms = chan_subs.ready_state(
                    conn, chan_sub_ids=[r.id for r in mine], now_ms=now_ms
                )
                last = (
                    None
                    if mine or procs
                    else chan_subs.last_ended_of_session(conn, token_id, s.pid, s.proc_start, s.session_id)
                )
                # Ф41-2а П2: сессия хаба — по базе. Ф33-1: машина — строки подписки, не тела запроса.
                alive = subs.session_alive(
                    conn, s.pid, s.proc_start, mine[0].session_host if mine else s.host
                )
            if not mine:
                if procs:
                    # П17: подписки нового разговора этим устаревшим запросом не трогать.
                    raise _http(subs.gone("conversation_changed"))
                if last is None:
                    raise HTTPException(404, "у сессии нет подписок на беседы")
                raise _http(subs.gone(last.end_reason))
            if not alive:

                def _gone(conn, now_ms=now_ms):
                    for rec in chan_subs.live_of_process(conn, s.pid, s.proc_start):
                        chan_subs.end(conn, rec.id, "session_gone", now_ms=now_ms, actor="core")

                await db.write(_gone)
                raise _http(subs.gone("session_gone"))
            if notifier.closed:
                return {"signals": []}  # Ф31-1 П5: ядро останавливается — пусто и без `deliver`
            if actions:

                def _apply(conn, now_ms=now_ms, actions=actions):
                    chan_subs.apply_actions(
                        conn,
                        actions,
                        now_ms=now_ms,
                        actor=principal.kind,
                        api_token_id=token_id,
                        client_name=client_name,
                        request_id=request_id,
                    )

                await db.write(_apply)
                continue
            remaining = deadline - loop.time()
            wait_s = remaining
            if held_ms:
                wait_s = min(wait_s, held_ms / 1000)
            if pending:
                left_ms = chan_subs.debounce_left_ms(mine, now_ms)
                if left_ms == 0:

                    def _deliver(conn, now_ms=now_ms):
                        return chan_subs.deliver(
                            conn,
                            token_id=token_id,
                            pid=s.pid,
                            proc_start=s.proc_start,
                            session_id=s.session_id,
                            project_ids=principal.project_ids,
                            now_ms=now_ms,
                            client_name=client_name,
                            request_id=request_id,
                        )

                    signals = await db.write(_deliver)
                    if signals:
                        return {"signals": signals}
                    continue  # негодные сняты (`skipped`) — перечитать
                wait_s = min(wait_s, left_ms / 1000)
            ids = [r.id for r in mine]
            if remaining <= 0:
                await db.write(
                    lambda conn, now_ms=now_ms, ids=ids: chan_subs.touch_wait(conn, ids, now_ms=now_ms)
                )
                return {"signals": []}
            await notifier.wait(gen, max(wait_s, 0.0))
            if not waiters.is_current(key, mark):
                raise HTTPException(409, "superseded")
            if await request.is_disconnected():
                await db.write(lambda conn, ids=ids: chan_subs.touch_wait(conn, ids, now_ms=_now_ms(request)))
                return Response(status_code=204)
    finally:
        waiters.leave(key, mark)
        if far:
            hosts.wait_end(s.host, s.pid, s.proc_start)


# --- CLI-админ (`hub sub list|end`) --------------------------------------------------------------------


@internal_router.get("/admin/subs")
async def get_admin_subs(
    request: Request,
    all: int = Query(0, ge=0, le=1),  # noqa: A002
    days: int = Query(1, ge=1, le=subs.ENDED_DAYS_MAX),
    _: None = _admin,
) -> dict:
    """Без `all` — незавершённые; `all=1` — ещё и завершённые за `days` суток (Ф92-1 B5: `hub sub list --days
    N`, по умолчанию сутки). Плюс `session_id8`, `session_cwd`; у завершённых — `end_reason`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    ended_ms = days * subs.ENDED_RECENT_MS
    with db.reader() as conn:
        items = subs.list_subs(
            conn,
            project_ids=None,
            live_only=not all,
            now_ms=now_ms,
            waiters=_waiters(request),
            admin=True,
            ended_ms=ended_ms,
        )
        channels = chan_subs.list_chan_subs(
            conn,
            project_ids=None,
            live_only=not all,
            now_ms=now_ms,
            waiters=_chan_waiters(request),
            admin=True,
            ended_ms=ended_ms,
        )
    return {"items": items, "channels": channels}


@internal_router.post("/admin/subs/{sub_id}/end")
async def post_admin_sub_end(sub_id: int, request: Request, _: None = _admin) -> dict:
    """Владелец снимает подписку (`ended_by_owner`). Уже завершённая — 200 с её строкой."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    waiters = _waiters(request)

    def _fn(conn):
        rec = subs.get_sub(conn, sub_id)
        if rec is None:
            return None
        subs.end_sub(conn, sub_id, "ended_by_owner", now_ms=now_ms, actor="admin")
        return subs.one_json(conn, subs.get_sub(conn, sub_id), now_ms=now_ms, waiters=waiters, admin=True)

    row = await db.write(_fn)
    if row is None:
        raise HTTPException(404, "not found")
    return {"sub": row}


class FreeAgentsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@internal_router.post("/admin/subs/free")
async def post_admin_subs_free(body: FreeAgentsIn, request: Request, _: None = _admin) -> dict:
    """Ф35-1 Н3 (`hub sub free on|off`): общий выключатель агентов вне проектов. Выключение завершает живые
    подписки проекта 0 (`subs_off`) и отказывает новым."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    ended = await db.write(lambda conn: subs.set_free_agents(conn, body.enabled, now_ms=now_ms))
    return {"enabled": body.enabled, "ended": ended}


@internal_router.post("/admin/chan-subs/{sub_id}/end")
async def post_admin_chan_sub_end(sub_id: int, request: Request, _: None = _admin) -> dict:
    """Ф14-5 A4: владелец снимает подписку на беседу (`hub sub end c<id>`, `ended_by_owner`)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    waiters = _chan_waiters(request)

    def _fn(conn):
        rec = chan_subs.get(conn, sub_id)
        if rec is None:
            return None
        chan_subs.end(conn, sub_id, "ended_by_owner", now_ms=now_ms, actor="admin")
        current = chan_subs.get(conn, sub_id)
        return chan_subs.one_json(conn, current, now_ms=now_ms, waiters=waiters, admin=True)

    row = await db.write(_fn)
    if row is None:
        raise HTTPException(404, "not found")
    return {"sub": row}
