"""Внутреннее ASGI-приложение: unix-сокет `<data_dir>/internal.sock`, только `/internal/v1`, только JSON.
Спецификация: docs/architecture.md.

В отличие от `app_public.py` — без `SecurityMiddleware` (Host/Origin/CSRF защищают от браузера; сюда браузер
не достаёт вообще, права сокета 0600 — первая линия защиты, план 1.1). Обработчики исключений — тот же приём,
что в `app_public.py`: JSON везде и никогда ничего другого, даже на непойманном исключении.
"""

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..term.hub import TermHub
from .activity import Activity
from .chan_subs import ChanWaiters
from .db import Db
from .feed import Notifier
from .held import install as held_install
from .remote import RemoteState
from .routes.activity import internal_router as activity_internal_router
from .routes.autopilot import internal_router as autopilot_internal_router
from .routes.digest import internal_router as digest_internal_router
from .routes.engine import internal_router as engine_internal_router
from .routes.group_ops import internal_router as group_ops_internal_router
from .routes.human_requests import internal_router as human_requests_internal_router
from .routes.internal import router as internal_router
from .routes.interviews import internal_router as interviews_internal_router
from .routes.joins import internal_router as joins_internal_router
from .routes.leaves import internal_router as leaves_internal_router
from .routes.load import internal_router as load_internal_router
from .routes.machines import internal_router as machines_internal_router
from .routes.media import internal_router as media_internal_router
from .routes.msg_ops import internal_router as msg_ops_internal_router
from .routes.notify import internal_router as notify_internal_router
from .routes.presses import internal_router as presses_internal_router
from .routes.reactions import internal_router as reactions_internal_router
from .routes.read_ack import internal_router as read_ack_internal_router
from .routes.remote import internal_router as remote_internal_router
from .routes.subs import internal_router as subs_internal_router
from .routes.term import internal_router as term_internal_router
from .routes.tg_lookup import internal_router as tg_lookup_internal_router
from .routes.trace import internal_router as trace_internal_router
from .routes.workday import internal_router as workday_internal_router
from .security import Throttle
from .session_host import install as session_host_install
from .subs import SubWaiters
from .tg_lookup import TgLookup


def create_internal_app(
    db: Db,
    *,
    clock: Callable[[], float],
    notifier: Notifier,
    admin_key_hash: str,
    remote: RemoteState | None = None,
    sub_waiters: SubWaiters | None = None,
    chan_sub_waiters: ChanWaiters | None = None,
    activity: Activity | None = None,
    tg_lookup: TgLookup | None = None,
    term: TermHub | None = None,
    interviews=None,
) -> FastAPI:
    """`admin_key_hash` — sha256 секрета CLI-админа; сам секрет `create_internal_app` не видит и не читает
    файл `admin.key` — это забота `runner.py` (единственное место, которое держит секрет в памяти).

    Ф10-7, П1: `remote` — ТО ЖЕ состояние удалённого доступа, что у публичного приложения (`hub remote off`
    обязан закрыть вход паролем сразу); `None` -> своё, загруженное из БД (тесты внутреннего сокета).

    Ф13-1: `sub_waiters` — тот же учёт идущих ожиданий подписок, что у публичного приложения.

    Ф29-1: `tg_lookup` — тот же посредник поиска/чтения Telegram, что у публичного приложения."""
    app = FastAPI(title="hub-internal", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.clock = clock
    app.state.notifier = notifier
    app.state.throttle = Throttle()
    app.state.admin_key_hash = admin_key_hash
    if remote is None:
        remote = RemoteState()
        with db.reader() as conn:
            remote.load(conn)
    app.state.remote = remote
    app.state.held = held_install(db)  # Ф41-2а: тот же реестр сессий хаба, что у публичного (один на базу)
    app.state.session_hosts = session_host_install(db, clock)  # Ф33-1: тот же реестр, что у публичного
    app.state.sub_waiters = sub_waiters if sub_waiters is not None else SubWaiters()
    app.state.activity = activity if activity is not None else Activity()  # Ф20-3 A2: общее с публичным
    app.state.chan_sub_waiters = chan_sub_waiters if chan_sub_waiters is not None else ChanWaiters()  # Ф14-5
    app.state.tg_lookup = tg_lookup if tg_lookup is not None else TgLookup()  # Ф29-1 A1: общий с публичным
    app.state.term = term  # Ф56-5: те же терминалы, что у публичного (`hub term list`); None -> 503
    app.state.interviews = interviews  # Ф60-1: та же память интервью, что у публичного (`hub listen status`)

    app.include_router(internal_router, prefix="/internal/v1")
    app.include_router(media_internal_router, prefix="/internal/v1")  # Ф8-5 A2.4
    app.include_router(activity_internal_router, prefix="/internal/v1")  # Ф20-3
    app.include_router(read_ack_internal_router, prefix="/internal/v1")  # Ф8-7 A5
    app.include_router(reactions_internal_router, prefix="/internal/v1")  # Ф10-3
    app.include_router(msg_ops_internal_router, prefix="/internal/v1")  # Ф14-8
    app.include_router(remote_internal_router, prefix="/internal/v1")  # Ф10-7
    app.include_router(subs_internal_router, prefix="/internal/v1")  # Ф13-1: `hub sub list|end`
    app.include_router(engine_internal_router, prefix="/internal/v1")  # Ф16-1: `hub engine`
    app.include_router(autopilot_internal_router, prefix="/internal/v1")  # Ф41-5: `hub autopilot`
    app.include_router(workday_internal_router, prefix="/internal/v1")  # Ф64-1: `hub project day`
    app.include_router(joins_internal_router, prefix="/internal/v1")  # Ф17-1: вступление в группы Telegram
    app.include_router(presses_internal_router, prefix="/internal/v1")  # Ф44-1c: нажатия кнопок ботов
    app.include_router(leaves_internal_router, prefix="/internal/v1")  # Ф17-2: выход из групп Telegram
    app.include_router(group_ops_internal_router, prefix="/internal/v1")  # Ф62-6: группы WhatsApp
    app.include_router(tg_lookup_internal_router, prefix="/internal/v1")  # Ф29-1: поиск и чтение Telegram
    app.include_router(human_requests_internal_router, prefix="/internal/v1")  # Ф54-2: id бота хаба
    app.include_router(load_internal_router, prefix="/internal/v1")  # Ф52-1: `hub load`
    app.include_router(trace_internal_router, prefix="/internal/v1")  # Ф97-1: `hub trace`
    app.include_router(machines_internal_router, prefix="/internal/v1")  # Ф77-1: `hub machine`
    app.include_router(term_internal_router, prefix="/internal/v1")  # Ф56-5: `hub term list`
    app.include_router(interviews_internal_router, prefix="/internal/v1")  # Ф60-1: `hub listen status`
    app.include_router(notify_internal_router, prefix="/internal/v1")  # Ф33-2: уведомления владельца
    app.include_router(digest_internal_router, prefix="/internal/v1")  # Ф86-1: `hub digest show`

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(_request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(_request, _exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app
