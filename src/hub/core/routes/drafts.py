"""`POST /api/v1/drafts/new` — черновик ПЕРВОГО сообщения новому адресату (Ф12-1, A1 + П1, П7).

Только bearer со скоупом `draft` (П7: UI-сессия — 403 до отдельного куска «новое сообщение из UI»; иначе
`origin='ui'` ушёл бы сразу `queued` мимо подтверждения). Проект — существующий и видимый токену (проектный
токен — только свой), «Неразобранное»/`null` — 422. Беседа-заготовка, черновик (`pending_approval`) и аудит
`draft_new` (без текста и без адресата: аккаунт, проект, вид адресата, длина) — одной транзакцией.

Ф12-5 A1: `POST /api/v1/channels/new` — пустая беседа-заготовка для самого владельца (только UI-сессия).

Ф14-2 A2/A5 (`docs/architecture.md`): `channels/new` — адресат строкой или объектом
(`tg_user`/`slack_users`), проект необязателен («по правилам»); `GET /api/v1/recipients?q=` — поиск «Кому»
(беседы и люди), только UI-сессия.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import autosend as autosend_mod
from .. import card_route as card_route_mod
from .. import draft_outcomes, new_recipient, queries, recipients
from .. import peer_flood as peer_flood_mod
from .. import tg_lookup as lookup_mod
from ..audit import write_audit
from ..security import Principal, principal_dependency
from .write import SendSessionIn, _client_name, _deny_bearer, _new_request_id, _now_ms, ui_only

router = APIRouter(tags=["drafts"])

_draft_scope = Depends(principal_dependency("draft"))


class DraftNewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    project_id: int | None
    # Ф46-1 П5: строка или объект; объектом от bearer — только `{"kind": "tg_msg_author", "message_id": N}`
    # (отсечка — в `new_recipient`, тексты 422 — там же).
    to: Annotated[str, Field(max_length=1000)] | dict
    # Ф14-3 A5 + П3: при `hold` текст не нужен; без `hold` пустой/не передан — 422, как было.
    text: str | None = None
    subject: str | None = Field(default=None, max_length=1000)
    # `from` — ключевое слово Python: в теле ключ `from`, в модели — `from_address`.
    from_address: str | None = Field(default=None, alias="from", max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=200)
    # Ф14-3 A5: заготовка БЕЗ строки outbox — сессия затем кладёт в неё загрузки и `/messages/send` с файлами.
    hold: bool = False
    # Ф39 B2: сессия, положившая черновик (та же форма, что у `POST /messages/send`) — по ней пишется автор.
    # Необязательна; при `hold` строки outbox нет — автора запишет последующий `/messages/send`.
    session: SendSessionIn | None = None
    # Ф41-5б П1: ключ запуска сессии хаба — только для `autopilot.session_free`; не сохраняется.
    session_key: str | None = Field(default=None, max_length=128)


def _sees_project(conn, principal: Principal):
    """Ревью Ф34-3: видит ли токен проект существующей заготовки («Неразобранное» — по `sees_unsorted`)."""

    def _sees(project_id: int | None) -> bool:
        if project_id is None:
            return principal.sees_unsorted
        return queries.project_visible(conn, principal, project_id)

    return _sees


@router.post("/drafts/new")
async def create_draft_new(
    body: DraftNewIn, request: Request, response: Response, principal: Principal = _draft_scope
) -> dict:
    if principal.is_ui:
        await _deny_bearer(request, principal)  # П7: пока только bearer (аудит `ui_only_denied`, 403)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    if body.project_id is None or body.project_id < 0:
        raise HTTPException(422, "project_id: нужен настоящий проект (не «Неразобранное»)")
    await _refuse_not_person(body, request, principal, now_ms, request_id, client_name)
    if body.hold:
        return await _create_hold(body, request, principal, now_ms, request_id, client_name)
    sub_waiters = getattr(request.app.state, "sub_waiters", None)

    def _fn(conn):
        if not queries.project_visible(conn, principal, body.project_id):
            raise new_recipient.Invalid("project_id: нет такого проекта или он не виден профилю")
        result = new_recipient.create_draft(
            conn,
            account_id=body.account_id,
            project_id=body.project_id,
            to=body.to,
            text=body.text or "",
            subject=body.subject,
            from_address=body.from_address,
            api_token_id=principal.token_id,
            idempotency_key=body.idempotency_key,
            now_ms=now_ms,
            sees_project=_sees_project(conn, principal),
            principal=principal,
        )
        if body.session is not None and result.created:
            # Ф39 B2: автор — только у строки, созданной этим запросом (не повтор по ключу).
            draft_outcomes.set_author(
                conn,
                result.outbox_id,
                token_id=principal.token_id,
                session=body.session,
                project_id=body.project_id,
            )
        # Ф66-1, ревью Р1: сессия хаба — автор заготовки (по условной тройке, с подпиской или без) — для
        # маршрута карточки: ручная сессия хаба — всегда владельцу (`card_route.draft_hub`).
        hub_id = card_route_mod.creating_hub(conn, body.session) if result.created else None
        author_hub = {card_route_mod.DRAFT_HUB_KEY: hub_id} if hub_id is not None else {}
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="draft_new",
            result=result.status,
            channel_id=result.channel_id,
            outbox_id=result.outbox_id,
            detail={
                "account_id": body.account_id,
                "project_id": body.project_id,
                "to_kind": result.kind,
                "text_len": len(body.text or ""),
                "repeat": not result.created,
                **author_hub,
            },
        )
        # Ф41-5б (5.4): первое сообщение новому адресату от сессии хаба в авто при снятом выключателе
        # `hub_send_new` подтверждается в ТОЙ ЖЕ транзакции (все условия — `autosend.try_send`).
        auto = None
        if body.session is not None:
            name_row = conn.execute(
                "SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)
            ).fetchone()
            auto = autosend_mod.try_send(
                conn,
                principal=principal,
                token_name=name_row[0] if name_row and isinstance(name_row[0], str) else None,
                session=body.session,
                channel_id=result.channel_id,
                project_id=body.project_id,
                result=autosend_mod.Draft(result.outbox_id, result.created, result.status, True),
                send_at_ts=None,
                now_ms=now_ms,
                waiting=sub_waiters.waiting if sub_waiters is not None else None,
                session_key=body.session_key,
            )
        if not principal.is_ui and result.created:
            # Ф66-1 (П9, П11): заготовка новому адресату, которая ждёт владельца, — «сразу да» по таблице маршрута.
            card_route_mod.auto_draft(
                conn,
                result.outbox_id,
                now_ms=now_ms,
                waiting=sub_waiters.waiting if sub_waiters is not None else None,
            )
        return result, auto, peer_flood_mod.stop_for_channel(conn, result.channel_id)

    try:
        result, auto, peer_flood_ts = await db.write(_fn)
    except (new_recipient.DraftExists, new_recipient.DraftElsewhere, new_recipient.DmExists) as exc:
        raise HTTPException(409, str(exc)) from exc
    except new_recipient.IdempotencyConflict as exc:
        raise HTTPException(409, "idempotency conflict") from exc
    except new_recipient.TooMany as exc:
        raise HTTPException(429, str(exc)) from exc
    except new_recipient.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc

    answer = {
        "channel_id": result.channel_id,
        "outbox_id": result.outbox_id,
        "message_id": result.message_id,
        "status": result.status,
        "existing": result.existing,
        "paced": True,  # Ф12-2, решение 1: черновик новому адресату — всегда по графику
        # Ф69-1 П7: стоп первых сообщений новым людям, который держит этот черновик (иначе `null`).
        "peer_flood_ts": peer_flood_ts,
    }
    if auto is not None and auto.role is not None:
        # Ф41-5б (5.4): подтверждено выключателем автопилота — уйдёт по графику аккаунта, без владельца.
        answer["status"] = "queued"
        answer["auto"] = {"role": auto.role, "by": auto.by, "mode": auto.mode}
        if auto.by == autosend_mod.BOT_CHAT:
            answer["paced"] = False  # Ф63-2 П1: первое сообщение боту — без графика
    elif auto is not None and auto.denied is not None:
        answer["auto_denied"] = auto.denied
        answer["auto_limit"] = auto.limit_of
        if auto.guard is not None:
            answer["auto_guard"] = auto.guard  # Ф63-2 (1.3): страховка беседы с ботом — категория
    return answer


async def _refuse_not_person(
    body: DraftNewIn, request: Request, principal: Principal, now_ms: int, request_id: str,
    client_name: str | None,
) -> None:  # fmt: skip
    """24.10 (черновик на @канал проходил подтверждение и падал только при отправке): Telegram по
    @имени — до заготовки спросить коннектор, кто это (`TgLookup.resolve_kind`: свой суточный счёт, пауза
    FloodWait, кэш час). Канал или группа — 422 и аудит `draft_new` `not_person`; человек, бот, ник не найден,
    коннектор молчит или не ответил за `RESOLVE_TIMEOUT_S` — дальше как раньше (проверка при отправке
    остаётся)."""
    lookup = getattr(request.app.state, "tg_lookup", None)
    if lookup is None or not isinstance(body.to, str) or new_recipient.looks_like_phone(body.to):
        return
    try:
        username = new_recipient.parse_tg_username(body.to)
    except new_recipient.Invalid:
        return  # неверный адресат — 422 даст сам `new_recipient`
    db = request.app.state.db
    with db.reader() as conn:
        if not lookup_mod.resolve_ready(conn, body.account_id, now_ms=now_ms):
            return
    kind = await lookup.resolve_kind(body.account_id, username, now_ms=now_ms)
    if kind not in lookup_mod.NOT_PERSON_KINDS:
        return

    def _fn(conn):
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="draft_new",
            result="not_person",
            detail={"account_id": body.account_id, "project_id": body.project_id, "to_kind": kind},
        )

    await db.write(_fn)
    raise HTTPException(422, lookup_mod.not_person_text(username, kind))


async def _create_hold(
    body: DraftNewIn, request: Request, principal: Principal, now_ms: int, request_id: str,
    client_name: str | None,
) -> dict:  # fmt: skip
    """Ф14-3 A5 + П2, П3: `hold` — заготовка без строки outbox (`new_recipient.create_hold`), текст и ключ
    идемпотентности не используются, `existing` всегда `false`. Аудит `draft_new` —
    `{hold: true, text_len: 0}`."""
    db = request.app.state.db

    def _fn(conn):
        if not queries.project_visible(conn, principal, body.project_id):
            raise new_recipient.Invalid("project_id: нет такого проекта или он не виден профилю")
        result = new_recipient.create_hold(
            conn,
            account_id=body.account_id,
            project_id=body.project_id,
            to=body.to,
            subject=body.subject,
            from_address=body.from_address,
            now_ms=now_ms,
            sees_project=_sees_project(conn, principal),
            principal=principal,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="draft_new",
            result="held",
            channel_id=result.channel_id,
            detail={
                "account_id": body.account_id,
                "project_id": body.project_id,
                "to_kind": result.kind,
                "text_len": 0,
                "hold": True,
                "repeat": not result.created,
            },
        )
        return result, peer_flood_mod.stop_for_channel(conn, result.channel_id)

    try:
        result, peer_flood_ts = await db.write(_fn)
    except (new_recipient.DraftExists, new_recipient.DraftElsewhere, new_recipient.DmExists) as exc:
        raise HTTPException(409, str(exc)) from exc
    except new_recipient.TooMany as exc:
        raise HTTPException(429, str(exc)) from exc
    except new_recipient.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "channel_id": result.channel_id,
        "outbox_id": None,
        "status": "held",
        "existing": False,
        "created": result.created,
        "peer_flood_ts": peer_flood_ts,  # Ф69-1 П7
    }


# --- Ф12-5 A1: «Новое сообщение» из хаба — пустая беседа-заготовка для самого владельца -------------------------


class ChannelNewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    project_id: int | None
    # Ф14-2 A2: строка (Telegram `@username` / адрес почты) или объект `tg_user`/`slack_users` — его поля
    # проверяет `new_recipient` (тексты 422 — там же).
    to: Annotated[str, Field(max_length=1000)] | dict
    subject: str | None = Field(default=None, max_length=1000)


@router.post("/channels/new")
async def create_channel_new(
    body: ChannelNewIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Ф12-5 A1 (`docs/architecture.md`): только UI-сессия (bearer -> 403 с аудитом
    `ui_only_denied`), CSRF — middleware. Беседа-заготовка без строки outbox (первое сообщение — обычным
    `POST /messages/send`, `origin='ui'` -> сразу `queued`). Проверки и тексты 422 — общий код
    `new_recipient`; ящик «только на приём» -> 422, больше 100 открытых заготовок -> 429; открытая заготовка
    тому же адресату в том же проекте -> она же, `created: false`. Аудит `channel_new` — без адресата и
    темы.

    Ф14-2 A2: `project_id: null` — «по правилам» (заготовка `auto`, сразу пересчёт правилами), `≤ 0` — 422;
    `to` — строка или объект (`new_recipient.create_channel`); у `tg_user` с настоящей личкой — она,
    `created: false`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    if body.project_id is not None and body.project_id <= 0:
        raise HTTPException(422, "project_id: нужен настоящий проект или null («по правилам»)")

    def _fn(conn):
        if body.project_id is not None and not queries.project_visible(conn, principal, body.project_id):
            raise new_recipient.Invalid("project_id: нет такого проекта")
        result = new_recipient.create_channel(
            conn,
            account_id=body.account_id,
            project_id=body.project_id,
            to=body.to,
            subject=body.subject,
            now_ms=now_ms,
            principal=principal,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="channel_new",
            result="created" if result.created else "existing",
            channel_id=result.channel_id,
            detail={
                "account_id": body.account_id,
                "project_id": body.project_id,
                "to_kind": result.kind,
                "created": result.created,
            },
        )
        return result, queries.get_channel(conn, principal, result.channel_id)

    try:
        result, channel = await db.write(_fn)
    except new_recipient.TooMany as exc:
        raise HTTPException(429, str(exc)) from exc
    except new_recipient.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"channel": channel, "created": result.created}


# --- Ф14-2 A5: поиск «Кому» ---------------------------------------------------------------------------------


@router.get("/recipients")
async def get_recipients(
    request: Request, q: str = Query(max_length=1000), principal: Principal = ui_only
) -> list[dict]:
    """Ф14-2 A5: беседы и люди для поля «Кому» формы «Новое сообщение» — только UI-сессия (bearer -> 403 с
    аудитом `ui_only_denied`). `q` после `strip()` — 2..100 символов, иначе 422. Ответ — данные людей: в
    журнал и аудит не пишется."""
    del principal  # только UI-сессия (`ui_only`); видимость — вся, как у UI
    query = q.strip()
    if not recipients.QUERY_MIN <= len(query) <= recipients.QUERY_MAX:
        raise HTTPException(422, f"q: {recipients.QUERY_MIN}–{recipients.QUERY_MAX} символов")
    with request.app.state.db.reader() as conn:
        return recipients.search(conn, query)
