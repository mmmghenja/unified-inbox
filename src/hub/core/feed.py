"""Пробуждение SSE/long-poll после коммита пишущей транзакции. Спецификация: docs/architecture.md,
раздел «Этап 4b», пункты про Notifier и SSE.

`Notifier.notify()` вызывается в потоке-писателе (`Db.on_commit`) — здесь она только переводит это в
`loop.call_soon_threadsafe`, вся работа с состоянием происходит в целевом event loop. `stream()` — отдельная
функция-генератор SSE-кадров, тестируется напрямую, без HTTP (тест 16).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable

from . import queries
from .notify_ring import NotifyRing
from .security import Principal


class Notifier:
    """Монотонно растущее поколение + набор `asyncio.Event` на ожидающего. Поколение, а не голый `Event`,
    чтобы `notify()`, случившийся между чтением БД и входом в `wait()`, не терялся: вызывающий снимает
    поколение ДО чтения БД и передаёт его в `wait()` — если оно уже устарело, `wait()` возвращает немедленно.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gen = 0
        self._waiters: set[asyncio.Event] = set()
        self._closed = False
        self._on_close: list[Callable[[], None]] = []
        self.ring = NotifyRing()  # Ф33-2: кольцо уведомлений владельца (память ядра)
        self._on_close.append(self.ring.close)

    @property
    def closed(self) -> bool:
        """Ф31-1 решение 6 + П5: ядро останавливается — долгие ожидания (`/stream`, long-poll сессий и
        коннекторов) выходят сразу и отвечают пусто, ничего не выдавая."""
        return self._closed

    def close(self) -> None:
        """Ф31-1 П5: остановка ядра — звать в целевом event loop ДО `should_exit` серверов. Будит всех ждущих;
        дальше `wait()` возвращает сразу. Один флаг на все приложения (публичное, удалённый слушатель,
        внутреннее): ожидания живут во всех. Ф32-2 п.4: будит и ожидания со своими событиями (`on_close`)."""
        self._closed = True
        self._bump()
        for hook in self._on_close:
            hook()

    def on_close(self, hook: Callable[[], None]) -> None:
        """Ф32-2 п.4 + П3: ожидание не на `wait()` этого объекта (очередь `TgLookup` — свои `asyncio.Event`)
        узнаёт об остановке ядра: `hook` зовётся из `close()` в том же event loop."""
        self._on_close.append(hook)

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def gen(self) -> int:
        return self._gen

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Вызывать из корутины, выполняющейся в целевом event loop (lifespan приложения, либо лениво при
        первом `/stream`, если lifespan не отработал — см. app_public.py)."""
        self._loop = loop

    def notify(self) -> None:
        """Потокобезопасно: вызывается из потока-писателя. Если цикл ещё не привязан или уже закрыт —
        молча ничего не делает (будить некого)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # цикл может закрываться прямо сейчас
            loop.call_soon_threadsafe(self._bump)

    def _bump(self) -> None:
        # Выполняется в целевом event loop (мы внутри колбэка call_soon_threadsafe) — без гонок.
        self._gen += 1
        waiters, self._waiters = self._waiters, set()
        for event in waiters:
            event.set()

    async def wait(self, last_gen: int, timeout_s: float) -> int:
        """Вернуть новое поколение. Если оно уже обогнало `last_gen` — вернуть немедленно, не ожидая
        (защита от потерянного пробуждения). Иначе ждать `notify()` до `timeout_s` и вернуть поколение,
        каким бы оно ни было к этому моменту (то же — таймаут, другое — было пробуждение)."""
        if self._gen > last_gen or self._closed:
            return self._gen
        event = asyncio.Event()
        self._waiters.add(event)
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout_s):
                    await event.wait()
        finally:
            self._waiters.discard(event)
        return self._gen


# Heartbeat — ИМЕНОВАННЫЙ кадр, а не комментарий `: hb`: браузерный EventSource комментарии в JS не
# отдаёт, и UI не мог бы посчитать давность потока (поправка куска 6). Без строки `id:` —
# `Last-Event-ID` не сдвигается; `data` непустой — кадр с пустым `data` браузер не диспатчит.
HEARTBEAT_FRAME = "event: hb\ndata: {}\n\n"


def _frame(event: str, *, id_: int, channel_id: int, project_id: int | None, seq: int) -> str:
    data = json.dumps({"id": id_, "channel_id": channel_id, "project_id": project_id, "seq": seq})
    return f"id: {seq}\nevent: {event}\ndata: {data}\n\n"


def _join_frame(item: dict) -> str:
    """Ф17-1 П6: кадр заявки на вступление — id, проект, статус; без цели, названий и причин."""
    data = {"id": item["id"], "project_id": item["project_id"], "status": item["status"], "seq": item["seq"]}
    return f"id: {item['seq']}\nevent: join\ndata: {json.dumps(data)}\n\n"


def _leave_frame(item: dict) -> str:
    """Ф17-2 A7: кадр заявки на выход — id, проект, статус; без названия беседы и причины."""
    data = {"id": item["id"], "project_id": item["project_id"], "status": item["status"], "seq": item["seq"]}
    return f"id: {item['seq']}\nevent: leave\ndata: {json.dumps(data)}\n\n"


def _group_op_frame(item: dict) -> str:
    """Ф62-6: кадр операции над группой WhatsApp — id, вид, проект, беседа, статус; без названия, участников,
    причины и ссылки (их дочитывают `/sync` и `GET /group-ops`)."""
    data = {
        "id": item["id"],
        "kind": item["kind"],
        "project_id": item["project_id"],
        "channel_id": item["channel_id"],
        "status": item["status"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: group_op\ndata: {json.dumps(data)}\n\n"


def _reminder_frame(item: dict) -> str:
    """Ф34-2 П9: кадр напоминания — id, проект, беседа, статус; без заметки и названия беседы."""
    data = {
        "id": item["id"],
        "project_id": item["project_id"],
        "channel_id": item["channel_id"],
        "status": item["status"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: reminder\ndata: {json.dumps(data)}\n\n"


def _peer_msg_frame(item: dict) -> str:
    """Ф35-2: кадр сообщения агентов — id и статус; без текста и адресов. Такие изменения `changes_since`
    отдаёт только UI-сессии."""
    data = {"id": item["id"], "status": item["status"], "seq": item["seq"]}
    return f"id: {item['seq']}\nevent: peer_msg\ndata: {json.dumps(data)}\n\n"


def _rule_request_frame(item: dict) -> str:
    """Ф38-2 П6: кадр заявки на правило раскладки — id, проект, статус; без условия, причины и названий."""
    data = {"id": item["id"], "project_id": item["project_id"], "status": item["status"], "seq": item["seq"]}
    return f"id: {item['seq']}\nevent: rule_request\ndata: {json.dumps(data)}\n\n"


def _agent_request_frame(item: dict) -> str:
    """Ф41-5б: кадр заявки агента — id, вид, проект, статус; без задачи, причины и названий."""
    data = {
        "id": item["id"],
        "kind": item["kind"],
        "project_id": item["project_id"],
        "status": item["status"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: agent_request\ndata: {json.dumps(data)}\n\n"


def _press_frame(item: dict) -> str:
    """Ф44-1c: кадр нажатия кнопки бота — номера и статус; без текста кнопки и ответа бота."""
    data = {
        "id": item["id"],
        "channel_id": item["channel_id"],
        "message_id": item["message_id"],
        "project_id": item["project_id"],
        "status": item["status"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: press\ndata: {json.dumps(data)}\n\n"


def _human_request_frame(item: dict) -> str:
    """Ф54-1 (П13): кадр заявки «нужен человек» — номер, проект, вид, статус; без текстов автора (площадка,
    адрес, что делала, что нужно) — их интерфейс читает из `/sync`."""
    data = {
        "id": item["id"],
        "project_id": item["project_id"],
        "kind": item["kind"],
        "status": item["status"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: human_request\ndata: {json.dumps(data)}\n\n"


def _hub_session_frame(item: dict) -> str:
    """Ф41-2б (3.3): кадр постоянной сессии хаба — id, проект, состояние, закрыта ли, последнее событие; без
    названия, роли и id разговора движка."""
    data = {
        "id": item["id"],
        "project_id": item["project_id"],
        "state": item["state"],
        "closed": item["closed_ts"] is not None,
        "last_event_id": item["last_event_id"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: hub_session\ndata: {json.dumps(data)}\n\n"


def _session_request_frame(item: dict) -> str:
    """Ф41-2б (3.3): кадр запроса постоянной сессии — id, сессия, проект, статус; без входа инструмента."""
    data = {
        "id": item["id"],
        "session_id": item["session_id"],
        "project_id": item["project_id"],
        "status": item["status"],
        "seq": item["seq"],
    }
    return f"id: {item['seq']}\nevent: session_request\ndata: {json.dumps(data)}\n\n"


def _frames(changes: queries.Changes) -> list[str]:
    """Кадры в едином порядке по `seq` (общий у всех записей) — метаданные, без текста."""
    combined = (
        [("message", m) for m in changes.messages]
        + [("channel", c) for c in changes.channels]
        + [("join", j) for j in changes.joins]
        + [("leave", r) for r in changes.leaves]
        + [("reminder", r) for r in changes.reminders]
        + [("peer_msg", m) for m in changes.peer_msgs]
        + [("rule_request", r) for r in changes.rule_requests]
        + [("hub_session", r) for r in changes.hub_sessions]
        + [("session_request", r) for r in changes.session_requests]
        + [("agent_request", r) for r in changes.agent_requests]
        + [("press", r) for r in changes.presses]
        + [("human_request", r) for r in changes.human_requests]
        + [("group_op", r) for r in changes.group_ops]
    )
    combined.sort(key=lambda item: item[1]["seq"])

    frames = []
    for kind, item in combined:
        if kind == "join":
            frames.append(_join_frame(item))
        elif kind == "leave":
            frames.append(_leave_frame(item))
        elif kind == "reminder":
            frames.append(_reminder_frame(item))
        elif kind == "peer_msg":
            frames.append(_peer_msg_frame(item))
        elif kind == "rule_request":
            frames.append(_rule_request_frame(item))
        elif kind == "hub_session":
            frames.append(_hub_session_frame(item))
        elif kind == "session_request":
            frames.append(_session_request_frame(item))
        elif kind == "agent_request":
            frames.append(_agent_request_frame(item))
        elif kind == "press":
            frames.append(_press_frame(item))
        elif kind == "human_request":
            frames.append(_human_request_frame(item))
        elif kind == "group_op":
            frames.append(_group_op_frame(item))
        elif kind == "message":
            frames.append(
                _frame(
                    "message",
                    id_=item["id"],
                    channel_id=item["channel_id"],
                    project_id=item["project_id"],
                    seq=item["seq"],
                )
            )
        else:
            frames.append(
                _frame(
                    "channel",
                    id_=item["id"],
                    channel_id=item["id"],
                    project_id=item["project_id"],
                    seq=item["seq"],
                )
            )
    return frames


async def _wait_or_stop(
    notifier: Notifier, gen: int, timeout_s: float, stop: asyncio.Event | None
) -> tuple[int, bool]:
    """`notifier.wait()`, но прерываемо `stop` — иначе `stop.set()` не будил бы генератор, застрявший в
    ожидании до `heartbeat_s` (секунды могут быть большими, `stop` обязан срабатывать немедленно).
    Возвращает `(новое_поколение, стоп_сработал)`.
    """
    if stop is None:
        return await notifier.wait(gen, timeout_s), False

    wait_task = asyncio.ensure_future(notifier.wait(gen, timeout_s))
    stop_task = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait({wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            return gen, True
        return wait_task.result(), False
    finally:
        for task in (wait_task, stop_task):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def stream(
    db,
    principal: Principal,
    *,
    last_seq: int,
    notifier: Notifier,
    heartbeat_s: float = 15.0,
    batch: int = 500,
    stop: asyncio.Event | None = None,
    still_valid: Callable[[], bool] | None = None,
) -> AsyncIterator[str]:
    """Генератор готовых SSE-кадров. Транзакций/соединений между итерациями не держит: проснулся -> открыл
    `db.reader()` -> прочитал -> закрыл. При тишине — heartbeat-кадр `HEARTBEAT_FRAME` раз в `heartbeat_s`.
    `stop` (либо отмена самой задачи генератора) завершает поток без висящих задач; Ф31-1 П5 — и
    `notifier.close()` при остановке ядра.

    Ф10-7, П4: `still_valid()` зовётся на каждом пробуждении и перед каждым heartbeat — сессия отозвана,
    истекла или удалённый доступ выключен -> поток закрывается (запись об отзыве сама будит поток).
    """
    seq = last_seq
    # Heartbeat — по ДЕДЛАЙНУ, а не «таймаут с нуля после каждого пробуждения»: иначе записи в чужих проектах
    # (они будят всех, но кадров этому принципалу не дают) глушили бы heartbeat — клиент не отличал бы живое
    # соединение от мёртвого и видел бы чужую активность по пропавшим heartbeat (критик, F6). Длительность,
    # не срок — монотонные часы цикла здесь законны (план, 1.3).
    clock = asyncio.get_running_loop().time
    next_hb = clock() + heartbeat_s
    while True:
        if (stop is not None and stop.is_set()) or notifier.closed:
            return
        if still_valid is not None and not still_valid():
            return

        gen = notifier.gen  # снимаем ДО чтения БД — notify() между этой строкой и wait() не потеряется
        with db.reader() as conn:
            changes = queries.changes_since(conn, principal, since=seq, limit=batch)

        for frame in _frames(changes):
            yield frame
            next_hb = clock() + heartbeat_s  # настоящий кадр — тоже признак жизни

        if (
            changes.messages
            or changes.channels
            or changes.joins
            or changes.leaves
            or changes.reminders
            or changes.peer_msgs  # Ф35-2 П9: иначе кадр `peer_msg` шёл бы UI по кругу
            or changes.rule_requests  # Ф38-2 П6: то же для кадра `rule_request`
            or changes.hub_sessions  # Ф41-2б: то же для кадров `hub_session` и `session_request`
            or changes.session_requests
            or changes.agent_requests  # Ф41-5б
            or changes.presses  # Ф44-1c
            or changes.human_requests  # Ф54-1
            or changes.group_ops  # Ф62-6
        ):
            seq = changes.max_seq
            continue

        if stop is not None and stop.is_set():
            return

        _new_gen, stopped = await _wait_or_stop(notifier, gen, max(0.0, next_hb - clock()), stop)
        if stopped or notifier.closed:
            return  # Ф31-1 П5: остановка ядра закрывает поток сразу
        if clock() >= next_hb:
            if still_valid is not None and not still_valid():
                return
            yield HEARTBEAT_FRAME
            next_hb = clock() + heartbeat_s
