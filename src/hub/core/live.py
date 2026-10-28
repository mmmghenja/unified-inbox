"""Текст постоянной сессии по мере набора (Ф41-3, docs/architecture.md).

Хост шлёт `POST /work/host/sessions/{id}/live` — ВЕСЬ текст идущего блока ответа (или размышлений) на этот
момент; интерфейс читает поток `GET /sessions/live`. Только в ПАМЯТИ процесса ядра (как `presence.py`): в
базу, аудит и журналы текст не попадает, `seq` и `host_seen_ts` не двигаются, рестарт ядра записи теряет — это
нормально: окончательный текст приходит прежним путём, событием `text` с `ref` = `block`.

На сессию хранится одно, последнее состояние `{block, kind, text, cut, done, ts}`. Запись снимается:
- через `DONE_KEEP_MS` после `done: true` (опоздавший клиент ещё увидит конец блока);
- когда состояние сессии перестало быть `running` (`drop` — зовут маршруты после коммита);
- когда ей больше `STALE_MS` (хост замолчал).

Рассылка — свой маленький механизм: у каждого подписчика словарь «сессия -> последнее состояние» и
`asyncio.Event`. Медленному клиенту промежуточные состояния одной сессии схлопываются до последнего (текст
приходит целиком, пропуск букв не теряет). Общие `Notifier` и `/stream` не трогаются. Все методы зовутся в
event loop ядра (маршруты и генератор потока), блокировок нет.

Ф60-1 (docs/architecture.md): тот же поток несёт кадры интервью `event: interview` —
новые строки расшифровки и смена записи интервью (`publish_interview`). Они НЕ схлопываются (каждая строка —
свой кадр) и при подключении не повторяются (окно читает `GET /interviews/current`). Очередь подписчика —
до `INTERVIEW_QUEUE_MAX` кадров; переполнилась — очередь сброшена и уходит один кадр `{"type": "resync"}`
(интерфейс перечитывает `GET /interviews/current`). Только память, как набираемый текст."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

KINDS = ("text", "thinking")
BLOCK_MAX = 200
TEXT_MAX = 8000
DONE_KEEP_MS = 10_000
STALE_MS = 120_000
HEARTBEAT_S = 15.0
MAX_SUBSCRIBERS = 32
INTERVIEW_QUEUE_MAX = 1000
# Комментарий SSE: браузерный EventSource его не диспатчит, соединение при этом живо (3.1).
HEARTBEAT_FRAME = ": hb\n\n"


class _Subscriber:
    def __init__(self) -> None:
        self.pending: dict[int, dict] = {}
        self.interview: list[dict] = []
        self.interview_lost = False
        self.event = asyncio.Event()

    def take(self) -> list[tuple[int, dict]]:
        items, self.pending = list(self.pending.items()), {}
        self.event.clear()
        return items

    def take_interview(self) -> tuple[list[dict], bool]:
        """Ф60-1: накопленные кадры интервью и признак «очередь переполнялась» (тогда — `resync`)."""
        frames, self.interview = self.interview, []
        lost, self.interview_lost = self.interview_lost, False
        return frames, lost


class LiveHub:
    """Последнее состояние набираемого текста по сессиям и его подписчики."""

    def __init__(self) -> None:
        self._entries: dict[int, dict] = {}
        self._subscribers: set[_Subscriber] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def close(self) -> None:
        """Остановка ядра (`Notifier.on_close`): потоки закрываются сразу."""
        self._closed = True
        for sub in self._subscribers:
            sub.event.set()

    def _purge(self, now_ms: int) -> None:
        for session_id, entry in list(self._entries.items()):
            age = now_ms - entry["ts"]
            if age > STALE_MS or (entry["done"] and age > DONE_KEEP_MS):
                del self._entries[session_id]

    def publish(
        self, session_id: int, *, block: str, kind: str, text: str, cut: bool, done: bool, now_ms: int
    ) -> None:
        if len(text) > TEXT_MAX:
            text, cut = text[-TEXT_MAX:], True
        entry = {
            "block": block,
            "kind": kind,
            "text": text,
            "cut": bool(cut),
            "done": bool(done),
            "ts": now_ms,
        }
        self._purge(now_ms)
        self._entries[session_id] = entry
        for sub in self._subscribers:
            sub.pending[session_id] = entry
            sub.event.set()

    def publish_interview(self, data: dict) -> None:
        """Ф60-1 (2.5): кадр интервью всем подписчикам, без схлопывания."""
        for sub in self._subscribers:
            if len(sub.interview) >= INTERVIEW_QUEUE_MAX:
                sub.interview = []
                sub.interview_lost = True
            sub.interview.append(data)
            sub.event.set()

    def drop(self, session_id: int) -> None:
        """Сессия больше не в ходу (конец хода, сон, закрытие) — запись снята. Кадра подписчикам нет: окно
        убирает строку само, по состоянию сессии."""
        self._entries.pop(session_id, None)
        for sub in self._subscribers:
            sub.pending.pop(session_id, None)

    def get(self, session_id: int, now_ms: int) -> dict | None:
        self._purge(now_ms)
        return self._entries.get(session_id)

    def snapshot(self, now_ms: int) -> list[tuple[int, dict]]:
        self._purge(now_ms)
        return sorted(self._entries.items())

    @property
    def full(self) -> bool:
        return len(self._subscribers) >= MAX_SUBSCRIBERS

    def subscribe(self) -> _Subscriber | None:
        """Новый подписчик; `None` — потоков уже `MAX_SUBSCRIBERS` (предохранитель от забытых вкладок)."""
        if self.full:
            return None
        sub = _Subscriber()
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subscribers.discard(sub)


def frame(session_id: int, entry: dict) -> str:
    data = {
        "session_id": session_id,
        "block": entry["block"],
        "kind": entry["kind"],
        "text": entry["text"],
        "cut": entry["cut"],
        "done": entry["done"],
    }
    return f"event: live\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def interview_frame(data: dict) -> str:
    return f"event: interview\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


RESYNC = {"type": "resync"}


async def stream(
    hub: LiveHub,
    *,
    now_ms: Callable[[], int],
    heartbeat_s: float = HEARTBEAT_S,
    still_valid: Callable[[], bool] | None = None,
) -> AsyncIterator[str]:
    """Кадры потока `GET /sessions/live`: сначала текущие записи всех сессий, дальше — каждое изменение
    (схлопнутое до последнего на сессию). Кадров `id:` нет: поток не возобновляется. Тишина —
    комментарий-пульс раз в `heartbeat_s`. `still_valid()` (сессия UI отозвана, истекла, доступ с телефона
    выключен) перепроверяется раз в `heartbeat_s` — и при тишине, и под потоком кадров. Остановка ядра
    (`hub.close()`) закрывает поток. Подписка заводится здесь же, при первом кадре: брошенный до чтения
    ответ её не оставляет."""
    clock = asyncio.get_running_loop().time
    sub = hub.subscribe()
    if sub is None:
        return
    try:
        if hub.closed or (still_valid is not None and not still_valid()):
            return
        for session_id, entry in hub.snapshot(now_ms()):
            yield frame(session_id, entry)
        next_hb = clock() + heartbeat_s
        next_check = clock() + heartbeat_s
        while not hub.closed:
            timeout = max(0.0, min(next_hb, next_check) - clock())
            try:
                async with asyncio.timeout(timeout):
                    await sub.event.wait()
            except TimeoutError:
                pass
            if hub.closed:
                return
            if clock() >= next_check:
                if still_valid is not None and not still_valid():
                    return
                next_check = clock() + heartbeat_s
            items = sub.take()
            interview, lost = sub.take_interview()
            for session_id, entry in items:
                yield frame(session_id, entry)
            if lost:
                yield interview_frame(RESYNC)
            for data in interview:
                yield interview_frame(data)
            if items or interview or lost:
                next_hb = clock() + heartbeat_s
            elif clock() >= next_hb:
                yield HEARTBEAT_FRAME
                next_hb = clock() + heartbeat_s
    finally:
        hub.unsubscribe(sub)
