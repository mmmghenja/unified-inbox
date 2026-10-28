"""Fake-коннектор с управляемыми отказами. Спецификация: docs/architecture.md, раздел
«Fake-коннектор», и тесты 10-22 (`tests/e2e/`).

`FakeSource` — «внешний мир» в памяти: живёт ДОЛЬШЕ одного экземпляра `FakeConnector` (тесты пересоздают
коннектор, чтобы изобразить рестарт процесса, но источник — Telegram/Slack/итд — про этот рестарт не знает
и своего состояния не теряет). `FaultPlan` — сценарий отказов, расходуется по одному элементу за ПОПЫТКУ:
`send()`/`reconcile()` берут из одной очереди (судьба одной выдачи лиза), `sync()` — из отдельной (судьба
одного захода за новыми событиями источника).

`ConnectorCrash` — приём, которого нет в `ConnectorBase`: настоящее падение процесса ДО того, как каркас
успел бы отчитаться. `ConnectorBase.step_outbox()` на любое исключение сам вызывает `report(unknown)` — это
верно для «сеть моргнула», но не для «упал до/после отправки»: там report вообще не должен уйти, лиз должен
повиснуть и истечь по часам (спека, «шаг просто обрывается, лиз остаётся висеть»). Поэтому `FakeConnector`
переопределяет `step_outbox()` целиком (`base.py` не трогаем — единственная разрешённая правка там уже
сделана в конструкторе `CoreClient`, см. отчёт исполнителя).
"""

from collections import deque
from dataclasses import dataclass, field

from .base import AuthRequired, ConnectorBase, SendResult, lease_files_meta
from .msg_ops import MsgOpResult
from .reactions import ReactionResult
from .read_ack import ReadAckResult

_ECHO_SUFFIX = " (переформатировано источником)"


class ConnectorCrash(BaseException):  # noqa: N818 — не ошибка, а смерть процесса
    """Симуляция падения процесса коннектора ДО report()/до сохранения своего состояния. Тест наблюдает
    эффект только через сервер (лиз висит до истечения / та же пачка событий приходит снова)."""


@dataclass
class Delivery:
    ext_id: str
    channel_ext_id: str
    text: str
    ts: int
    send_token: str | None
    # Приёмка 2e (архитектор): тред исходящего — эхо обязано его нести (как `thread_ts` у эха Slack), иначе
    # upsert эха затирает `thread_ext_id` заготовки и ответ в тред «выпадает» в ленту канала.
    thread_ext_id: str | None = None
    # Ф10-4 П12: отправка с файлом — эхо несёт вложение (`[{type, name, size, mime}]`), как у источника.
    attachments: list[dict] | None = None


def _channel_ref(channel_ext_id: str, *, title: str | None = None, kind: str = "dm") -> dict:
    return {"ext_id": channel_ext_id, "kind": kind, "title": title}


def _upsert_raw(
    channel_ext_id: str,
    *,
    ext_id: str,
    text: str | None,
    ts: int,
    is_me: bool,
    kind: str = "text",
    sender_ext_id: str | None = None,
    send_token: str | None = None,
    channel_kind: str = "dm",
    channel_title: str | None = None,
    event_type: str = "upsert",
    thread_ext_id: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    raw = {
        "type": event_type,
        "channel": _channel_ref(channel_ext_id, title=channel_title, kind=channel_kind),
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": kind,
        "text": text,
        "mentions_me": False,
        "sender": None if sender_ext_id is None else {"ext_id": sender_ext_id},
        "send_token": send_token,
        "thread_ext_id": thread_ext_id,
    }
    if attachments is not None:
        raw["kind"] = "file"
        raw["attachments"] = attachments
    return raw


class FakeSource:
    """«Внешний мир»: доставки исходящих (`deliveries`), очередь событий для `sync()` (`events_since`),
    история канала для `reconcile()` (`history`). `idempotent=True` — повтор `deliver()` с тем же
    `send_token` схлопывается в ТУ ЖЕ доставку (как MTProto `random_id`); `False` — вторая доставка, так
    тест ловит двойную отправку.
    """

    def __init__(self, *, idempotent: bool = False) -> None:
        self.idempotent = idempotent
        self.deliveries: list[Delivery] = []
        self._token_to_ext_id: dict[str, str] = {}
        self._next_ext_id = 1000
        self._event_log: list[dict] = []
        self._history_unavailable: set[str] = set()
        # Ф8-7 B4: задания «прочитано в источник», которые коннектор выполнил (`push_read` -> `done`).
        self.read_acks: list[dict] = []
        # Ф10-3 B: задания реакций, которые коннектор выполнил (`push_reaction` -> `done`), и реакции
        # сообщений у «источника»: (channel_ext_id, ext_id) -> [{emoji, count, me}].
        self.reaction_jobs: list[dict] = []
        self._reactions: dict[tuple[str, str], list[dict]] = {}
        # Ф14-8 B: задания правки/«удалить у всех», которые коннектор выполнил (`push_msg_op` -> `done`).
        self.msg_op_jobs: list[dict] = []

    # --- доставка исходящих (вызывает FakeConnector.send) ------------------------------------------------

    def _alloc_ext_id(self) -> str:
        self._next_ext_id += 1
        return str(self._next_ext_id)

    def deliver(
        self,
        *,
        channel_ext_id: str,
        text: str,
        send_token: str | None,
        ts: int,
        thread_ext_id: str | None = None,
        attachments: list[dict] | None = None,
    ) -> tuple[str, bool]:
        """Вернёт `(ext_id, is_new)`. `is_new=False`, если `idempotent` и этот `send_token` уже доставлялся
        — новая запись НЕ создаётся, схлопывание в прежнюю доставку."""
        if self.idempotent and send_token is not None and send_token in self._token_to_ext_id:
            return self._token_to_ext_id[send_token], False
        ext_id = self._alloc_ext_id()
        self.deliveries.append(
            Delivery(ext_id, channel_ext_id, text, ts, send_token, thread_ext_id, attachments)
        )
        if send_token is not None:
            self._token_to_ext_id[send_token] = ext_id
        self._event_log.append(
            _upsert_raw(
                channel_ext_id,
                ext_id=ext_id,
                text=text,
                ts=ts,
                is_me=True,
                send_token=send_token,
                thread_ext_id=thread_ext_id,
                attachments=attachments,
            )
        )
        return ext_id, True

    def delivered_count(self, text: str) -> int:
        return sum(1 for d in self.deliveries if d.text == text)

    def echo_for(self, ext_id: str) -> dict:
        """Готовое эхо-событие по уже доставленному `ext_id` — для faults, которые проталкивают эхо
        напрямую, в обход `sync()` (`echo_before_report`)."""
        for d in self.deliveries:
            if d.ext_id == ext_id:
                return _upsert_raw(
                    d.channel_ext_id,
                    ext_id=d.ext_id,
                    text=d.text,
                    ts=d.ts,
                    is_me=True,
                    send_token=d.send_token,
                    thread_ext_id=d.thread_ext_id,
                    attachments=d.attachments,
                )
        raise KeyError(ext_id)

    def edit_own(self, channel_ext_id: str, ext_id: str, text: str, edited_ts: int) -> dict | None:
        """Ф14-8 B: правка своего доставленного сообщения — текст доставки меняется, возвращается событие
        `edit` (всё сообщение, как эхо, с `edited_ts`). `None` — такой доставки нет (удалена или чужое)."""
        for d in self.deliveries:
            if d.ext_id == ext_id and d.channel_ext_id == channel_ext_id:
                d.text = text
                event = self.echo_for(ext_id)
                event.update({"type": "edit", "edited_ts": edited_ts, "send_token": None})
                return event
        return None

    def revoke_own(self, channel_ext_id: str, ext_id: str) -> bool:
        """Ф14-8 B: «удалить у всех» своей доставки; `False` — такой доставки нет (уже удалена)."""
        for i, d in enumerate(self.deliveries):
            if d.ext_id == ext_id and d.channel_ext_id == channel_ext_id:
                del self.deliveries[i]
                return True
        return False

    # --- внешний мир: входящее, правка, удаление, реакция, прочтение, переименование ----------------------

    def incoming(
        self, channel_ext_id: str, *, ext_id: str, text: str, ts: int, sender_ext_id: str = "peer", kind="dm"
    ) -> None:
        self._event_log.append(
            _upsert_raw(
                channel_ext_id,
                ext_id=ext_id,
                text=text,
                ts=ts,
                is_me=False,
                sender_ext_id=sender_ext_id,
                channel_kind=kind,
            )
        )

    def edit(self, channel_ext_id: str, *, ext_id: str, text: str, ts: int) -> None:
        self._event_log.append(
            _upsert_raw(channel_ext_id, ext_id=ext_id, text=text, ts=ts, is_me=False, event_type="edit")
        )

    def delete(self, channel_ext_id: str, *, ext_ids: list[str]) -> None:
        self._event_log.append(
            {"type": "delete", "ext_ids": list(ext_ids), "channel": _channel_ref(channel_ext_id)}
        )

    def reaction(self, channel_ext_id: str, *, ext_id: str, reactions: list) -> None:
        self._reactions[(channel_ext_id, ext_id)] = [dict(r) for r in reactions if isinstance(r, dict)]
        self._event_log.append(
            {
                "type": "reaction",
                "channel": _channel_ref(channel_ext_id),
                "ext_id": ext_id,
                "reactions": reactions,
            }
        )

    def apply_reaction_ops(self, channel_ext_id: str, ext_id: str, ops: list) -> list[dict]:
        """Ф10-3 B: реакции владельца на сообщении приводятся к операциям `[[emoji, on], …]` (как Slack: своих
        несколько, прочие реакции не трогаются). Возвращает полный список реакций сообщения после."""
        current = self._reactions.setdefault((channel_ext_id, ext_id), [])
        for emoji, on in ops:
            entry = next((r for r in current if r.get("emoji") == emoji), None)
            if on:
                if entry is None:
                    current.append({"emoji": emoji, "count": 1, "me": True})
                elif not entry.get("me"):
                    entry["count"] = entry.get("count", 0) + 1
                    entry["me"] = True
            elif entry is not None and entry.get("me"):
                entry["count"] = entry.get("count", 1) - 1
                entry["me"] = False
                if entry["count"] <= 0:
                    current.remove(entry)
        return [dict(r) for r in current]

    def read_on_phone(self, channel_ext_id: str, *, up_to_ext_id: str) -> None:
        self._event_log.append(
            {"type": "read", "channel": _channel_ref(channel_ext_id), "up_to_ext_id": up_to_ext_id}
        )

    def peer_read(self, channel_ext_id: str, *, up_to_ext_id: str, kind: str = "dm") -> None:
        """Ф11-7: собеседник прочитал мои сообщения до `up_to_ext_id` (для стенда: «✓✓» в UI)."""
        self._event_log.append(
            {
                "type": "peer_read",
                "channel": _channel_ref(channel_ext_id, kind=kind),
                "up_to_ext_id": up_to_ext_id,
            }
        )

    def rename_channel(self, channel_ext_id: str, *, title: str, ts: int, kind: str = "dm") -> None:
        # Переименование — обычное upsert-событие с новым title в ref (см. ingest._ensure_channel):
        # системное сообщение без текста, kind='event', чтобы не путаться с настоящими строками ленты.
        self._event_log.append(
            _upsert_raw(
                channel_ext_id,
                ext_id=f"sys-rename-{len(self._event_log)}",
                text=None,
                ts=ts,
                is_me=False,
                kind="event",
                channel_kind=kind,
                channel_title=title,
            )
        )

    def set_history_unavailable(self, channel_ext_id: str) -> None:
        self._history_unavailable.add(channel_ext_id)

    def history(self, channel_ext_id: str, *, since_ts: int, text: str) -> str | None:
        """`ext_id` доставленного сообщения с точным текстом, ts >= since_ts — для `reconcile()`. `None` —
        и когда история недоступна, и когда просто не нашлось: `reconcile()` в обоих случаях отвечает
        `unknown` (спека, тест 11)."""
        if channel_ext_id in self._history_unavailable:
            return None
        for d in self.deliveries:
            if d.channel_ext_id == channel_ext_id and d.text == text and d.ts >= since_ts:
                return d.ext_id
        return None

    # --- для sync(): события с курсором, источник их никогда не «выдаёт один раз» -------------------------

    def events_since(self, offset: int) -> list[dict]:
        return list(self._event_log[offset:])

    @property
    def log_len(self) -> int:
        return len(self._event_log)


@dataclass
class FaultPlan:
    """Очередь отказов, расходуется по одному элементу на попытку. `send`/`reconcile` делят одну очередь
    (описывают судьбу ОДНОЙ выдачи лиза, независимо от режима), `sync` — отдельную (судьба ОДНОГО захода
    за событиями источника).

    Элемент очереди `outbox` — либо имя отказа (`str`), либо `(name, retry_after_ms)` для `"flood"`.
    Имена: `crash_before_send`, `crash_after_delivery`, `flood`, `temporary`, `temporary_but_delivered`,
    `permanent`, `echo_before_report`, `auth_required`.
    Имена `sync`: `poison_event`, `gap_in_batch`, `crash_after_push`, `lose_token_map`, `mangle_echo_text`.
    Очередь `read_ack` (Ф8-7 B4) — судьба одного `push_read`: `drop`, `error` (исключение), `auth_required`
    или `(flood, retry_after_s)` -> `retry` с паузой. Очередь `reactions` (Ф10-3 B) — то же для одного
    `push_reaction`.
    """

    _outbox: deque = field(default_factory=deque)
    _sync: deque = field(default_factory=deque)
    _read_ack: deque = field(default_factory=deque)
    _reactions: deque = field(default_factory=deque)
    _msg_ops: deque = field(default_factory=deque)

    def queue_msg_ops(self, *faults) -> FaultPlan:
        """Ф14-8 B: судьба одного `push_msg_op` — `auth_required`, `error` (исключение), `(flood, s)` ->
        `retry` с паузой, `(fail, code)` -> `fail`."""
        self._msg_ops.extend(faults)
        return self

    def next_msg_ops_fault(self):
        return self._msg_ops.popleft() if self._msg_ops else None

    def queue_reactions(self, *faults) -> FaultPlan:
        self._reactions.extend(faults)
        return self

    def next_reactions_fault(self):
        return self._reactions.popleft() if self._reactions else None

    def queue_read_ack(self, *faults) -> FaultPlan:
        self._read_ack.extend(faults)
        return self

    def next_read_ack_fault(self):
        return self._read_ack.popleft() if self._read_ack else None

    def queue_outbox(self, *faults) -> FaultPlan:
        self._outbox.extend(faults)
        return self

    def queue_sync(self, *faults: str) -> FaultPlan:
        self._sync.extend(faults)
        return self

    def next_outbox_fault(self):
        return self._outbox.popleft() if self._outbox else None

    def next_sync_fault(self) -> str | None:
        return self._sync.popleft() if self._sync else None


class FakeConnector(ConnectorBase):
    """`send()`/`reconcile()` берут из `FaultPlan.next_outbox_fault()`, `sync()` — из `next_sync_fault()`,
    `push_read()` — из `next_read_ack_fault()`. Шаги вызываются из теста явно: `step_outbox()`
    (переопределён — см. модульный докстринг), `sync()` (алиас `step_sync()` — для симметрии с
    `step_outbox()`, оба имени равнозначны), `step_read_ack()` и `step_reactions()`."""

    read_ack_enabled = True
    reactions_enabled = True  # Ф10-3 B
    msg_ops_enabled = True  # Ф14-8 B
    # Ф70-1: спит вместе с днём проекта; приём — только сверкой, а её при сне каркас не зовёт (хуки приёма —
    # умолчания каркаса: `stop_source`/`start_source` здесь пустые).
    sleep_enabled = True

    def __init__(self, client, source: FakeSource, *, plan: FaultPlan | None = None, **base_kwargs) -> None:
        # `**base_kwargs` — `wall`/`mono`/`sleep` для `ConnectorBase.run()` (раздел D куска 1a): тесты
        # подменяют часы, обычные вызовы `FakeConnector(client, source)` (кусок 5b) их не передают и
        # получают настоящие `time.time`/`asyncio.sleep` по умолчанию из `ConnectorBase`.
        super().__init__(client, **base_kwargs)
        self._source = source
        self._plan = plan if plan is not None else FaultPlan()
        self._offset = 0
        self._retry_batch: list[dict] | None = None
        self._read_acker = None
        self._reactor = None
        self._msg_op_worker = None

    @property
    def source(self) -> FakeSource:
        return self._source

    async def get_cursor(self) -> dict | None:
        return await self._client.get_cursor()

    # --- цикл отправки: переопределён целиком, см. докстринг модуля -----------------------------------

    async def step_outbox(self) -> int:
        """Шаг НАСТОЯЩЕГО каркаса `ConnectorBase.step_outbox`: сквозные сценарии гоняют его, а не копию.
        `ConnectorCrash` — `BaseException`: каркас ловит только `Exception`, поэтому «смерть процесса»
        пролетает сквозь него без `report()`, и лиз висит до истечения по настенным часам. Здесь она лишь
        гасится, чтобы тест мог продолжить «после рестарта»."""
        try:
            return await super().step_outbox()
        except ConnectorCrash:
            return 0

    async def send(self, lease: dict) -> SendResult:
        fault = self._plan.next_outbox_fault()
        fault_name = fault[0] if isinstance(fault, tuple) else fault
        channel_ext_id = lease["channel_ext_id"]
        text = lease["text"]
        send_token = lease["send_token"]
        ts = lease["send_started_ts"]

        if fault_name == "crash_before_send":
            raise ConnectorCrash("коннектор упал до отправки")
        if fault_name == "auth_required":
            raise AuthRequired("источник отказал в авторизации")
        if fault_name == "temporary":
            return SendResult("temporary")
        if fault_name == "permanent":
            return SendResult("permanent", error="источник отказал окончательно")
        if fault_name == "flood":
            retry_after_ms = fault[1] if isinstance(fault, tuple) else 30_000
            return SendResult("flood", retry_after_ms=retry_after_ms)

        files = lease_files_meta(lease)
        # Ф10-4 П12 / Ф11-3b B5: файлы (контракт A↔B: `files` — список `{size, mime, name, kind, sha256}`) —
        # ОДНО эхо с N вложениями в порядке задания (как Slack). Байты fake-источнику не нужны.
        attachments = (
            [{"type": f["kind"], "name": f["name"], "size": f["size"], "mime": f["mime"]} for f in files]
            if files
            else None
        )
        ext_id, _is_new = self._source.deliver(
            channel_ext_id=channel_ext_id,
            text=text,
            send_token=send_token,
            ts=ts,
            thread_ext_id=lease.get("thread_ext_id"),
            attachments=attachments,
        )

        if fault_name == "crash_after_delivery":
            raise ConnectorCrash("коннектор упал после доставки, до report")

        if fault_name == "echo_before_report":
            # Эхо уходит в core РАНЬШЕ, чем каркас успевает отчитаться — симулируем прямым push_events,
            # в обход обычного sync()/оффсета (см. модульный докстринг).
            echo = self._source.echo_for(ext_id)
            await self._client.push_events([echo])
            self._offset = self._source.log_len

        if fault_name == "temporary_but_delivered":
            # Врущий коннектор: сообщение реально ушло, но отчёт — temporary.
            return SendResult("temporary")

        return SendResult("sent", ext_id=ext_id)

    async def reconcile(self, lease: dict) -> SendResult:
        fault = self._plan.next_outbox_fault()
        fault_name = fault[0] if isinstance(fault, tuple) else fault
        if fault_name in ("crash_before_send", "crash_after_delivery"):
            # Сверка ничего не доставляет — «после доставки» тут не осмысленно; любой crash-отказ во время
            # reconcile() означает то же самое: процесс упал ДО того, как успел отчитаться.
            raise ConnectorCrash("коннектор упал во время сверки")
        if fault_name == "auth_required":
            raise AuthRequired("источник отказал в авторизации")

        channel_ext_id = lease["channel_ext_id"]
        text = lease["text"]
        since_ts = lease["send_started_ts"]
        found_ext_id = self._source.history(channel_ext_id, since_ts=since_ts, text=text)
        if found_ext_id is None:
            return SendResult("unknown")
        return SendResult("sent", ext_id=found_ext_id)

    async def health(self) -> None:
        return None

    # --- прочитанность в источник (Ф8-7 B4) ---------------------------------------------------------------

    async def push_read(self, item: dict) -> ReadAckResult:
        fault = self._plan.next_read_ack_fault()
        fault_name = fault[0] if isinstance(fault, tuple) else fault
        if fault_name == "auth_required":
            raise AuthRequired("источник отказал в авторизации")
        if fault_name == "error":
            raise RuntimeError("источник упал посреди отметки")
        if fault_name == "drop":
            return ReadAckResult("drop", reason="fake_drop")
        if fault_name == "flood":
            retry_after_s = fault[1] if isinstance(fault, tuple) else 30.0
            return ReadAckResult("retry", retry_after_s=retry_after_s, reason="fake_flood")
        self._source.read_acks.append(item)
        return ReadAckResult("done")

    async def step_read_ack(self) -> int:
        """Один шаг НАСТОЯЩЕГО толкателя (`ReadAcker.step`) — для тестов и стенда, без long-poll."""
        if self._read_acker is None:
            self._read_acker = self._make_read_acker()
        return await self._read_acker.step()

    # --- реакции в источник (Ф10-3 B) ---------------------------------------------------------------------

    async def push_reaction(self, job: dict) -> ReactionResult:
        """Пишет задание в журнал источника, применяет операции к реакциям сообщения и шлёт событие
        `reaction` с полным списком — сразу в ядро (`push_events`), как эхо `echo_before_report`: стенд
        (`scripts/dev-demo.py`) и тесты гоняют шаги без буфера `run()`."""
        fault = self._plan.next_reactions_fault()
        fault_name = fault[0] if isinstance(fault, tuple) else fault
        if fault_name == "auth_required":
            raise AuthRequired("источник отказал в авторизации")
        if fault_name == "error":
            raise RuntimeError("источник упал посреди реакции")
        if fault_name == "drop":
            return ReactionResult("drop", reason="fake_drop")
        if fault_name == "flood":
            retry_after_s = fault[1] if isinstance(fault, tuple) else 30.0
            return ReactionResult("retry", retry_after_s=retry_after_s, reason="fake_flood")
        channel = job["channel"]
        reactions = self._source.apply_reaction_ops(channel["ext_id"], job["message_ext_id"], job["ops"])
        self._source.reaction_jobs.append(job)
        await self._client.push_events(
            [
                {
                    "type": "reaction",
                    "channel": {"ext_id": channel["ext_id"], "kind": channel["kind"]},
                    "ext_id": job["message_ext_id"],
                    "reactions": reactions,
                }
            ]
        )
        return ReactionResult("done")

    async def step_reactions(self) -> int:
        """Один шаг НАСТОЯЩЕГО толкателя реакций (`Reactor.step`) — для тестов и стенда, без long-poll."""
        if self._reactor is None:
            self._reactor = self._make_reactor()
        return await self._reactor.step()

    # --- правка и «удалить у всех» (Ф14-8 B) ------------------------------------------------------------

    async def push_msg_op(self, job: dict) -> MsgOpResult:
        """Правит/удаляет свою доставку у «источника» и шлёт событие `edit` (новый текст, `edited_ts = now`) /
        `delete` — сразу в ядро (`push_events`), как `push_reaction`: стенд гоняет шаги без буфера `run()`.
        Для стенда: текст, начинающийся с «!fail», — `fail`/`expired`. Своей доставки нет — `fail`/
        `not_found`."""
        fault = self._plan.next_msg_ops_fault()
        fault_name = fault[0] if isinstance(fault, tuple) else fault
        if fault_name == "auth_required":
            raise AuthRequired("источник отказал в авторизации")
        if fault_name == "error":
            raise RuntimeError("источник упал посреди правки")
        if fault_name == "flood":
            retry_after_s = fault[1] if isinstance(fault, tuple) else 30.0
            return MsgOpResult("retry", retry_after_s=retry_after_s, detail="fake_flood")
        if fault_name == "fail":
            code = fault[1] if isinstance(fault, tuple) else "other"
            return MsgOpResult("fail", code=code, detail="fake_fail")
        channel = job["channel"]
        ext_id = job["message_ext_id"]
        ref = {"ext_id": channel["ext_id"], "kind": channel["kind"]}
        if job["op"] == "edit":
            text = job.get("text") or ""
            if text.startswith("!fail"):
                return MsgOpResult("fail", code="expired", detail="fake_expired")
            event = self._source.edit_own(channel["ext_id"], ext_id, text, int(self._wall() * 1000))
            if event is None:
                return MsgOpResult("fail", code="not_found", detail="fake_no_message")
            event["channel"] = ref
        else:
            self._source.revoke_own(channel["ext_id"], ext_id)
            event = {"type": "delete", "ext_ids": [ext_id], "channel": ref}
        self._source.msg_op_jobs.append(job)
        await self._client.push_events([event])
        return MsgOpResult("done")

    async def step_msg_ops(self) -> int:
        """Один шаг НАСТОЯЩЕГО толкателя правок (`MsgOpWorker.step`) — для тестов и стенда, без long-poll."""
        if self._msg_op_worker is None:
            self._msg_op_worker = self._make_msg_op_worker()
        return await self._msg_op_worker.step()

    # --- приём событий источника ------------------------------------------------------------------------

    async def sync(self, reason: str = "scheduled") -> None:
        del reason  # FakeConnector не различает причину сверки — сквозные тесты 10-22 зовут sync() напрямую
        fault = self._plan.next_sync_fault()

        if self._retry_batch is not None:
            raw_events = self._retry_batch
        else:
            raw_events = self._source.events_since(self._offset)
        reported_offset = self._offset + len(raw_events)

        if not raw_events:
            self._retry_batch = None
            return

        payload = self._build_payload(raw_events, fault)
        await self._client.push_events(payload, cursor={"value": str(reported_offset)})

        if fault == "crash_after_push":
            # Push дошёл (core применил и сохранил курсор), но СВОЁ состояние коннектор не сохранил —
            # при следующем sync() пришлёт ту же пачку снова (идемпотентно для ingest, см. отчёт).
            self._retry_batch = raw_events
            return

        self._retry_batch = None
        self._offset = reported_offset

    async def step_sync(self) -> None:
        await self.sync()

    @staticmethod
    def _build_payload(raw_events: list[dict], fault: str | None) -> list[dict]:
        events = list(raw_events)
        if fault == "poison_event":
            # Ни одного обязательного поля -> pydantic ValidationError -> dead_letter, соседи применяются.
            events.insert(len(events) // 2, {"type": "upsert"})
        elif fault == "gap_in_batch" and len(events) >= 2:
            # Дыра: событие потеряно источником безвозвратно (не переотправляется), курсор всё равно
            # продвигается мимо неё — см. `reported_offset`, который считается от `raw_events`, не от
            # мутированного `events`.
            del events[len(events) // 2]
        elif fault == "lose_token_map":
            events = [{**ev, "send_token": None} if ev.get("send_token") else ev for ev in events]
        elif fault == "mangle_echo_text":
            events = [
                {**ev, "text": ev["text"] + _ECHO_SUFFIX} if ev.get("is_me") and ev.get("text") else ev
                for ev in events
            ]
        return events
