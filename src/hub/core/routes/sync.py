"""`GET /sync` и `GET /stream` — раздел «Этап 4b» спеки. Маршруты не пишут SQL: чтение —
`queries.changes_since`, пробуждение — `feed.Notifier`/`feed.stream`.
"""

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import queries
from ..feed import stream
from ..security import principal_dependency, request_is_remote, session_still_valid

router = APIRouter(tags=["sync"])

_read = Depends(principal_dependency("read"))

_SEQ_MAX = 2**63 - 1

_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


@router.get("/sync")
async def get_sync(
    request: Request,
    since: int = Query(0, ge=0, le=_SEQ_MAX),
    limit: int = Query(200, ge=1, le=500),
    principal=_read,
) -> dict:
    db = request.app.state.db
    with db.reader() as conn:
        changes = queries.changes_since(conn, principal, since=since, limit=limit)
    result = {
        "messages": changes.messages,
        "channels": changes.channels,
        "max_seq": changes.max_seq,
        "more": changes.more,
        # Ф17-1 П6: заявки на вступление в группы и каналы Telegram.
        "joins": list(changes.joins),
        # Ф17-2 A7: заявки на выход из групп и каналов Telegram.
        "leaves": list(changes.leaves),
        # Ф62-6: операции над группами WhatsApp (создать, участники, ссылка).
        "group_ops": list(changes.group_ops),
        # Ф34-2 П9: напоминания по беседам (постановка, срабатывание, перенос, закрытие).
        "reminders": list(changes.reminders),
        # Ф38-2 П6: заявки сессий на правило раскладки.
        "rule_requests": list(changes.rule_requests),
        # Ф41-2б (3.3, П12): постоянные сессии хаба и их запросы (события клиент дочитывает сам по
        # `last_event_id`).
        "hub_sessions": list(changes.hub_sessions),
        "session_requests": list(changes.session_requests),
    }
    if principal.is_ui:
        # Ф35-2 решение 6: сообщения агентов (с текстом) — тоже только UI-сессии.
        result["peer_msgs"] = list(changes.peer_msgs)
        # Ф41-5б (5.1): заявки агентов — только UI-сессии.
        result["agent_requests"] = list(changes.agent_requests)
        # Ф44-1c П8: нажатия кнопок ботов (карточки и исходы с ответом бота) — только UI-сессии.
        result["presses"] = list(changes.presses)
        # Ф54-1 П13: заявки «нужен человек» (карточки и исходы) — только UI-сессии.
        result["human_requests"] = list(changes.human_requests)
    return result


def _start_seq(request: Request, since: int | None) -> int:
    """`Last-Event-ID` из заголовка, иначе query `since`, иначе ТЕКУЩИЙ max seq — новый клиент без курсора
    не получает всю историю (поправка архитектора после приёмки 4a)."""
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        # Только цифры и диапазон SQLite: голый int() принял бы «-1» (вся история), «+3», «1_0», пробелы.
        if not (last_event_id.isascii() and last_event_id.isdigit()) or int(last_event_id) > _SEQ_MAX:
            raise HTTPException(422, "invalid Last-Event-ID")
        return int(last_event_id)
    if since is not None:
        return since
    db = request.app.state.db
    with db.reader() as conn:
        return queries.get_healthz(conn, 0)["max_seq"]


def session_checker(request: Request, principal) -> Callable[[], bool]:
    """Ф10-7, П4: долгий запрос (поток `/stream`, ожидание беседы Ф11-1) перепроверяет сессию (отзыв, срок,
    слушатель) и, на удалённом слушателе, что доступ с телефона включён."""
    db = request.app.state.db
    remote = request_is_remote(request)
    remote_state = request.app.state.remote
    clock = request.app.state.clock

    def _still_valid() -> bool:
        if remote and not remote_state.enabled:
            return False
        with db.reader() as conn:
            return session_still_valid(
                conn, token_id=principal.token_id, remote=remote, now_ms=int(clock() * 1000)
            )

    return _still_valid


@router.get("/stream")
async def get_stream(request: Request, since: int | None = Query(None, ge=0, le=_SEQ_MAX), principal=_read):
    db = request.app.state.db
    notifier = request.app.state.notifier
    if notifier.loop is None:
        # lifespan ещё не отработал (например, в тестах с ASGITransport) — привязываем лениво.
        notifier.attach(asyncio.get_running_loop())

    start_seq = _start_seq(request, since)
    still_valid = session_checker(request, principal)

    async def _generate():
        async for frame in stream(
            db, principal, last_seq=start_seq, notifier=notifier, still_valid=still_valid
        ):
            yield frame

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)
