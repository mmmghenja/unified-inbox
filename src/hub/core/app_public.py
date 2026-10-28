"""Публичное ASGI-приложение: 127.0.0.1:8787, `/api/v1` (+ SSE и статика UI — куски 4b/5, сюда не входят).

`create_public_app` собирает FastAPI и оборачивает его `SecurityMiddleware` — порядок важен: Host/Origin/CSRF
и заголовки должны сработать даже для несуществующего пути или уже брошенного исключения (раздел «Middleware»
спеки), поэтому middleware навешивается СНАРУЖИ роутов и обработчиков исключений.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..speech.local import SpeechBroker
from ..term.hub import TermHub
from .activity import Activity
from .chan_subs import ChanWaiters
from .db import Db
from .feed import Notifier
from .held import install as held_install
from .interview_runtime import InterviewRuntime
from .live import LiveHub
from .middleware import SecurityMiddleware, security_headers
from .presence import Presence
from .remote import RemoteState
from .routes.agent import router as agent_router
from .routes.agent_requests import public_router as agent_requests_router
from .routes.agent_state import public_router as agent_state_router
from .routes.auth import router as auth_router
from .routes.autopilot import public_router as autopilot_router
from .routes.dictate import public_router as dictate_router
from .routes.digest import public_router as digest_router
from .routes.drafts import router as drafts_router
from .routes.engine import public_router as engine_router
from .routes.group_ops import public_router as group_ops_router
from .routes.handled import router as handled_router
from .routes.human_requests import public_router as human_requests_router
from .routes.images import router as images_router
from .routes.inbound_rules import public_router as inbound_rules_router
from .routes.interviews import public_router as interviews_router
from .routes.joins import public_router as joins_router
from .routes.leaves import public_router as leaves_router
from .routes.load import public_router as load_router
from .routes.machines import public_router as machines_router
from .routes.md_files import public_router as md_files_router
from .routes.media import public_router as media_router
from .routes.msg_ops import public_router as msg_ops_router
from .routes.mute import router as mute_router
from .routes.notify import public_router as notify_router
from .routes.organize import public_router as organize_router
from .routes.owner_channel import public_router as owner_router
from .routes.peer_msgs import public_router as peer_msgs_router
from .routes.people import public_router as people_router
from .routes.person_facts import public_router as person_facts_router
from .routes.presence import router as presence_router
from .routes.presses import public_router as presses_router
from .routes.reactions import public_router as reactions_router
from .routes.read import router as read_router
from .routes.reminders import public_router as reminders_router
from .routes.remote import public_router as remote_router
from .routes.roles import public_router as roles_router
from .routes.rule_requests import public_router as rule_requests_router
from .routes.sessions import router as sessions_router
from .routes.speech import public_router as speech_router
from .routes.subs import public_router as subs_router
from .routes.sync import router as sync_router
from .routes.term import public_router as term_router
from .routes.tg_lookup import public_router as tg_lookup_router
from .routes.transcripts import public_router as transcripts_router
from .routes.ui_layout import public_router as ui_layout_router
from .routes.uploads import router as uploads_router
from .routes.work import router as work_router
from .routes.workday import public_router as workday_router
from .routes.write import router as write_router
from .security import Throttle
from .session_host import install as session_host_install
from .static_ui import StaticUIApp
from .subs import SubWaiters
from .tg_lookup import TgLookup

DEFAULT_PORT = 8787


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Привязка event loop к Notifier здесь — обычный путь (uvicorn); ASGITransport в тестах lifespan не
    # запускает, тогда routes/sync.py привязывает его лениво при первом /stream.
    app.state.notifier.attach(asyncio.get_running_loop())
    yield


def create_public_app(
    db: Db,
    *,
    clock: Callable[[], float],
    port: int = DEFAULT_PORT,
    ui_dir: Path | None = None,
    notifier: Notifier | None = None,
    remote_port: int = 0,
    remote: RemoteState | None = None,
    sub_waiters: SubWaiters | None = None,
    chan_sub_waiters: ChanWaiters | None = None,
    activity: Activity | None = None,
    tg_lookup: TgLookup | None = None,
    term: TermHub | None = None,
    interviews: InterviewRuntime | None = None,
) -> FastAPI:
    """`clock` — как у `Db`: секунды (совместим с `time.time`); в мс переводится на границе (в роутах),
    внутри core везде только мс (правило «Время» спеки). `ui_dir` — каталог собранного `ui/dist`; `None`
    (или отсутствующий каталог) -> ядро работает без UI, `GET /` отдаёт JSON 404 (кусок 6a).

    Ф10-7: `remote_port` — порт удалённого слушателя (0 — нет); `remote` — общее с внутренним приложением
    состояние удалённого доступа (П1, его создаёт `runner`); `None` -> своё, загруженное из БД (тесты).

    Ф13-1: `sub_waiters` — общий с внутренним приложением учёт идущих ожиданий подписок (`waiting` в
    `/admin/subs`); `None` -> свой.

    Ф29-1: `tg_lookup` — общий с внутренним приложением посредник поиска/чтения Telegram; `None` -> свой.

    Ф56-5: `term` — терминалы хаба (общий с внутренним приложением); `None` -> ручки `/term*` отвечают 503
    (одиночное приложение тестов не поднимает tmux).

    Ф60-1: `interviews` — память помощника на интервью (таймер `live`, живость службы записи), общая с
    внутренним приложением; `None` -> своя (тесты).
    """
    app = FastAPI(title="hub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.db = db
    app.state.clock = clock
    app.state.throttle = Throttle()
    app.state.activity = activity if activity is not None else Activity()  # Ф20-3 A2: общее с внутренним
    app.state.presence = Presence()  # Ф8-3 A7: присутствие — в памяти процесса, рестарт его обнуляет
    if (
        notifier is None
    ):  # `hub core` передаёт общий с внутренним приложением; одиночное приложение заводит свой
        notifier = Notifier()
        db.on_commit(notifier.notify)
    app.state.notifier = notifier
    if remote is None:
        remote = RemoteState(port=remote_port)
        with db.reader() as conn:
            remote.load(conn)
    app.state.remote = remote
    # Ф41-2а (2.1, П2б): реестр незакрытых сессий хаба — один на базу (общий с внутренним приложением),
    # перечитан из неё и поставлен текущим для `subs.is_alive` — до запуска фоновых задач ядра.
    app.state.held = held_install(db)
    # Ф33-1 (К9): реестр сессий другой машины — один на базу, засеян живыми подписками (свежесть от старта).
    app.state.session_hosts = session_host_install(db, clock)
    # Ф41-3 (3.1): текст постоянных сессий по мере набора — в памяти процесса; остановка ядра закрывает
    # потоки.
    app.state.live = LiveHub()
    notifier.on_close(app.state.live.close)
    app.state.sub_waiters = sub_waiters if sub_waiters is not None else SubWaiters()
    # Ф14-5 П8: ожидания подписок на беседы — свой общий с внутренним приложением объект.
    app.state.chan_sub_waiters = chan_sub_waiters if chan_sub_waiters is not None else ChanWaiters()
    # Ф29-1 A1: поиск и чтение Telegram — очередь в памяти, общая с внутренним приложением.
    app.state.tg_lookup = tg_lookup if tg_lookup is not None else TgLookup()
    # Ф56-5 (К13): потоки терминала закрываются при остановке ядра, как `/sessions/live`.
    app.state.term = term
    if term is not None:
        notifier.on_close(term.close_streams)
    # Ф60-1: помощник на интервью — кадры идут потоком `/sessions/live`; остановка ядра снимает таймеры.
    if interviews is None:
        interviews = InterviewRuntime(db, clock=clock, live=app.state.live)
    else:
        interviews.live = app.state.live
    app.state.interviews = interviews
    notifier.on_close(interviews.close)
    # Ф79-1: посредник своего whisper (диктовка и куски интервью) — память процесса; остановка ядра будит
    # ждущих работников пустым ответом.
    app.state.speech_broker = SpeechBroker()
    notifier.on_close(app.state.speech_broker.close)

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(read_router, prefix="/api/v1")
    app.include_router(write_router, prefix="/api/v1")
    app.include_router(sync_router, prefix="/api/v1")
    app.include_router(agent_router, prefix="/api/v1")
    app.include_router(presence_router, prefix="/api/v1")
    app.include_router(media_router, prefix="/api/v1")  # Ф8-5 A2.3, A3
    app.include_router(work_router, prefix="/api/v1")  # Ф9-1 (9a): рабочие сессии проектов
    app.include_router(reactions_router, prefix="/api/v1")  # Ф10-3: реакции из хаба в источник
    app.include_router(msg_ops_router, prefix="/api/v1")  # Ф14-8: правка и «удалить у всех»
    app.include_router(uploads_router, prefix="/api/v1")  # Ф10-4: загрузка файлов для отправки
    app.include_router(remote_router, prefix="/api/v1")  # Ф10-7: доступ с телефона
    app.include_router(drafts_router, prefix="/api/v1")  # Ф12-1: черновик новому адресату
    app.include_router(subs_router, prefix="/api/v1")  # Ф13-1: подписка сессии на проект
    app.include_router(organize_router, prefix="/api/v1")  # Ф14-1: раскладка и проекты из UI
    app.include_router(transcripts_router, prefix="/api/v1")  # Ф14-6: расшифровка голосовых
    app.include_router(engine_router, prefix="/api/v1")  # Ф16-1: переключатель движка сессий
    app.include_router(autopilot_router, prefix="/api/v1")  # Ф41-5: выключатели автопилота
    app.include_router(workday_router, prefix="/api/v1")  # Ф64-1: рабочий день проекта
    app.include_router(ui_layout_router, prefix="/api/v1")  # Ф41-6: раскладка доски
    app.include_router(roles_router, prefix="/api/v1")  # Ф41-8: каталог ролей
    app.include_router(joins_router, prefix="/api/v1")  # Ф17-1: вступление в группы и каналы Telegram
    app.include_router(presses_router, prefix="/api/v1")  # Ф44-1c: нажатия кнопок Telegram-ботов
    app.include_router(human_requests_router, prefix="/api/v1")  # Ф54-1: заявки «нужен человек»
    app.include_router(leaves_router, prefix="/api/v1")  # Ф17-2: выход из групп и каналов Telegram
    app.include_router(group_ops_router, prefix="/api/v1")  # Ф62-6: группы WhatsApp
    app.include_router(rule_requests_router, prefix="/api/v1")  # Ф38-2: заявки на правило раскладки
    app.include_router(inbound_rules_router, prefix="/api/v1")  # Ф63-1: правила проекта для входящих
    app.include_router(agent_requests_router, prefix="/api/v1")  # Ф41-5б: заявки агентов
    app.include_router(people_router, prefix="/api/v1")  # Ф21-1: люди во всех источниках
    app.include_router(person_facts_router, prefix="/api/v1")  # Ф24-1: факты о людях
    app.include_router(tg_lookup_router, prefix="/api/v1")  # Ф29-1: поиск и чтение публичных чатов Telegram
    app.include_router(reminders_router, prefix="/api/v1")  # Ф34-2: напоминания по беседе в срок
    app.include_router(peer_msgs_router, prefix="/api/v1")  # Ф35-2: сообщения агентов хаба
    app.include_router(agent_state_router, prefix="/api/v1")  # Ф50-4: состояние роли для оркестратора
    app.include_router(handled_router, prefix="/api/v1")  # Ф37-3: «Разобрано сессией»
    app.include_router(mute_router, prefix="/api/v1")  # Ф99-2: сессия глушит беседу (`hub_mute`)
    app.include_router(sessions_router, prefix="/api/v1")  # Ф41-2а: сессии хаба (записи ядра)
    app.include_router(load_router, prefix="/api/v1")  # Ф52-1: учёт нагрузки ноутбука
    app.include_router(owner_router, prefix="/api/v1")  # Ф56-1а: очередь канала владельца
    app.include_router(digest_router, prefix="/api/v1")  # Ф86-1: сводка дня и слова владельца соседям
    app.include_router(term_router, prefix="/api/v1")  # Ф56-5: терминал в хабе
    app.include_router(dictate_router, prefix="/api/v1")  # Ф59-1: диктовка через SpeechKit
    app.include_router(images_router, prefix="/api/v1")  # Ф91: картинки Alibaba qwen-image
    app.include_router(interviews_router, prefix="/api/v1")  # Ф60-1: помощник на интервью
    app.include_router(speech_router, prefix="/api/v1")  # Ф79-1: быстрый работник whisper
    app.include_router(notify_router, prefix="/api/v1")  # Ф33-2: уведомления владельца через ядро
    app.include_router(md_files_router, prefix="/api/v1")  # 19.10: просмотр MD-файлов
    app.include_router(machines_router, prefix="/api/v1")  # Ф77-1: реестр машин
    # Статика UI — ПОСЛЕ роутеров `/api/v1` (раздел «6a» спеки): всё, что у роутеров выше есть, отвечает
    # раньше и сюда не доходит; StaticUIApp сама на всякий случай никогда не отдаёт /api/* статикой.
    app.mount("/", StaticUIApp(ui_dir))

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(_request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    @app.exception_handler(OverflowError)
    async def _overflow_handler(_request, _exc: OverflowError) -> JSONResponse:
        # Целое больше 2^63-1 в пути/параметре/теле: sqlite3 бросает OverflowError. Ошибка клиента, не 500.
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, _exc: Exception) -> JSONResponse:
        # Только JSON нигде и никогда (правило спеки «Формат сообщения»): без этого Starlette в debug=False
        # отдал бы text/plain "Internal Server Error" на непойманном исключении. Этот обработчик живёт в
        # ServerErrorMiddleware — СНАРУЖИ SecurityMiddleware, поэтому заголовки безопасности ставим сами.
        headers = {k.decode(): v.decode() for k, v in security_headers(request.url.path)}
        return JSONResponse({"detail": "internal error"}, status_code=500, headers=headers)

    app.add_middleware(SecurityMiddleware, port=port, remote_port=remote_port, remote_state=remote)
    return app


def run_public(db: Db, *, clock: Callable[[], float], host: str, port: int = DEFAULT_PORT) -> None:
    """Единственное место, где host превращается в реальный bind — `assert` в коде, а не в конфиге."""
    assert host == "127.0.0.1", "публичное приложение слушает только 127.0.0.1"
    import uvicorn

    app = create_public_app(db, clock=clock, port=port)
    uvicorn.run(app, host=host, port=port)
