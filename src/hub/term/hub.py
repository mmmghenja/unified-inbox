"""Реестр терминалов ядра (Ф56-5): сервер tmux, мосты, лимиты владельцев (К12), подписчики потока (К2).

Всё в памяти процесса ядра и в самом tmux: номер, владелец (`@hub_owner` у сессии), размер и время создания
живут в tmux и переживают перезапуск ядра; ядро при старте находит живые `hub-t*` и поднимает мосты лениво, по
первому обращению (раздел 2.5). Все методы — в event loop ядра."""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable

from . import text as text_mod
from .bridge import Bridge, TermGone
from .config import TermConfig
from .tmux import OWNER_UI, TermInfo, TmuxError, TmuxServer

_logger = logging.getLogger("hub.term")

MAX_SUBSCRIBERS = 16
DEFAULT_COLS = 80
DEFAULT_ROWS = 24


class TermLimit(Exception):
    """Лимит терминалов владельца исчерпан (К12: 4 у владельца, 2 у ключа агента)."""


class TermBusy(Exception):
    """В панели не оболочка или полноэкранная программа (К8)."""


class _Subscriber:
    def __init__(self) -> None:
        self.event = asyncio.Event()


class _StarterCancelled(Exception):
    """Подъём моста отменили у поднимавшего (обрыв потока) — ждущий поднимает сам."""


class TermHub:
    def __init__(self, cfg: TermConfig) -> None:
        self.cfg = cfg
        self.server = TmuxServer(cfg)
        self._bridges: dict[int, Bridge] = {}
        self._starting: dict[int, asyncio.Future] = {}
        self._live: set[int] = set()
        self._subs: set[_Subscriber] = set()
        self._create_lock = asyncio.Lock()
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    # --- подписчики ----------------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    @property
    def full(self) -> bool:
        return len(self._subs) >= MAX_SUBSCRIBERS

    def subscribe(self) -> _Subscriber | None:
        if self.full:
            return None
        sub = _Subscriber()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subs.discard(sub)

    def notify(self) -> None:
        for sub in self._subs:
            sub.event.set()

    def close_streams(self) -> None:
        """Остановка ядра (`Notifier.on_close`): потоки закрываются сразу; мосты гасит `aclose`."""
        self._closed = True
        self.notify()

    # --- терминалы -----------------------------------------------------------------------------------------

    def live_ids(self) -> set[int]:
        return set(self._live)

    async def list(self) -> list[TermInfo]:
        """Свежий список из tmux; заодно обновляет множество живых (для потока)."""
        infos = await self.server.list_terms()
        ids = {info.id for info in infos}
        gone = self._live - ids
        self._live = ids
        if gone:
            self.notify()
        return infos

    async def info(self, term_id: int) -> TermInfo | None:
        for info in await self.list():
            if info.id == term_id:
                return info
        return None

    async def create(
        self, *, owner: str, alloc_id: Callable[[], Awaitable[int]], cols: int, rows: int
    ) -> TermInfo:
        """К12: лимит владельца проверяется и номер выдаётся под блокировкой — двух лишних не будет."""
        async with self._create_lock:
            await self.server.ensure()
            infos = await self.list()
            limit = self.cfg.max_ui if owner == OWNER_UI else self.cfg.max_agent
            if sum(1 for info in infos if info.owner == owner) >= limit:
                raise TermLimit(owner)
            term_id = await alloc_id()
            await self.server.new_session(term_id, owner=owner, cols=cols, rows=rows)
            self._live.add(term_id)
        _logger.info("терминал %s открыт (%s)", term_id, "владелец" if owner == OWNER_UI else "ключ агента")
        with contextlib.suppress(TermGone, TmuxError):
            await self.bridge(term_id)  # мост сразу: выход оболочки замечается и без открытого потока
        self.notify()
        info = await self.info(term_id)
        if info is None:
            raise TermGone(str(term_id))
        return info

    async def kill(self, term_id: int) -> bool:
        killed = await self.server.kill_session(term_id)
        self._live.discard(term_id)
        bridge = self._bridges.pop(term_id, None)
        if bridge is not None:
            await bridge.close()
        if killed:
            _logger.info("терминал %s закрыт (из хаба)", term_id)
        self.notify()
        return killed

    async def bridge(self, term_id: int) -> Bridge:
        """Живой мост терминала; нет — поднять (новая эпоха). Сессии нет — `TermGone` и терминал забыт."""
        bridge = self._bridges.get(term_id)
        if bridge is not None and bridge.alive:
            return bridge
        if self._closed:  # остановка ядра: новых мостов нет, терминал при этом жив — не забывать его
            raise TermGone(str(term_id))
        starting = self._starting.get(term_id)
        if starting is not None:
            try:
                return await asyncio.shield(starting)
            except _StarterCancelled:
                return await self.bridge(term_id)
        future = asyncio.get_running_loop().create_future()
        self._starting[term_id] = future
        try:
            if not await self.server.has_session(term_id):
                raise TermGone(str(term_id))
            bridge = Bridge(
                self.cfg, term_id, tmux=self.server.tmux, on_change=self.notify, on_exit=self._on_bridge_exit
            )
            await bridge.start()
            self._bridges[term_id] = bridge
            self._live.add(term_id)
            future.set_result(bridge)
            return bridge
        except BaseException as exc:
            if isinstance(exc, TermGone):
                self._forget(term_id)
            if not future.done():
                # Ревью 5а: отмену поднявшего ждущим не передавать — их никто не отменял; они поднимут сами.
                future.set_exception(_StarterCancelled() if isinstance(exc, asyncio.CancelledError) else exc)
                future.exception()  # ожидающих может не быть — не ругаться «исключение не забрано»
            raise
        finally:
            self._starting.pop(term_id, None)

    def _forget(self, term_id: int) -> None:
        if term_id in self._live:
            self._live.discard(term_id)
            _logger.info("терминал %s закрыт (оболочка вышла)", term_id)
            self.notify()

    def _on_bridge_exit(self, bridge: Bridge) -> None:
        if self._bridges.get(bridge.term_id) is bridge:
            del self._bridges[bridge.term_id]
        if self._closed:
            return
        task = asyncio.ensure_future(self._after_exit(bridge.term_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _after_exit(self, term_id: int) -> None:
        try:
            alive = await self.server.has_session(term_id)
        except Exception:  # noqa: BLE001
            alive = False
        if not alive:
            self._forget(term_id)
        else:
            self.notify()  # мост умер, сессия жива — подписчики поднимут новый (новая эпоха, снимок)

    # --- действия ------------------------------------------------------------------------------------------

    async def run(self, term_id: int, text: str, *, force: bool) -> None:
        """К8: проверка «занят» (не оболочка или alt-screen) — 409, если не `force`; затем буфер вставки
        отдельным клиентом и `paste-buffer -p` + Enter через клиента управления."""
        bridge = await self.bridge(term_id)
        if not force:
            alt, command = await bridge.state()
            if alt or command not in text_mod.SHELLS:
                raise TermBusy(command)
        name = f"hubrun-{secrets.token_hex(6)}"
        await self.server.load_buffer(name, text_mod.prepare(text))
        try:
            await bridge.paste_and_enter(name)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.server.delete_buffer(name)
            raise

    async def aclose(self) -> None:
        self.close_streams()
        bridges = list(self._bridges.values())
        self._bridges.clear()
        for bridge in bridges:
            with contextlib.suppress(Exception):
                await bridge.close()
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
