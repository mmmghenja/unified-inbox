"""`TgGateway` — ЕДИНСТВЕННОЕ место с `TelegramClient` (спека, раздел «Устройство пакета»). Сеть и Telethon
здесь; всё остальное в пакете — чистые функции над TL-объектами (`convert.py`, `folders.py`) или логика поверх
интерфейса гейтвея (`connector.py`), которую тесты гоняют с двойником без сети (тесты держат его сами —
живого Telegram в тестах нет вообще, раздел «Жёсткие правила» задания).

Интерфейс (методы ниже + `subscribe`) — контракт, который `connector.py` использует полиморфно; тестовый
двойник (`FakeGateway`, `tests/connectors/telegram/...`) реализует те же методы над TL-объектами, собранными
руками, без единого сетевого вызова.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from telethon import TelegramClient, events, utils
from telethon.errors import RPCError
from telethon.network.requeststate import RequestState
from telethon.sessions import SQLiteSession
from telethon.tl import types
from telethon.tl.core.messagecontainer import MessageContainer
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import (
    GetContactsRequest,
    ResolvePhoneRequest,
    ResolveUsernameRequest,
    SearchRequest,
)
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    DeleteChatUserRequest,
    EditMessageRequest,
    GetBotCallbackAnswerRequest,
    GetDialogFiltersRequest,
    GetForumTopicsByIDRequest,
    GetForumTopicsRequest,
    GetHistoryRequest,
    GetMessagesReactionsRequest,
    ImportChatInviteRequest,
    ReadDiscussionRequest,
    SendMediaRequest,
    SendMessageRequest,
    SendMultiMediaRequest,
    SendReactionRequest,
    UploadMediaRequest,
)
from telethon.tl.functions.users import GetUsersRequest

_logger = logging.getLogger(__name__)

_FORUM_TOPICS_PAGE = 100


# --- payload-объекты, которыми гейтвей говорит с connector.py -----------------------------------------------


@dataclass(frozen=True)
class DialogInfo:
    """Один диалог `iter_dialogs()` — то, что нужно сверке и папкам, без утечки Telethon-специфики глубже."""

    peer: object  # TL Peer (marked ext_id — utils.get_peer_id)
    entity: object  # User | Chat | ChatForbidden | Channel
    top_message: int | None
    read_inbox_max_id: int
    pinned: bool
    archived: bool
    mute_until: object | None  # datetime | None (PeerNotifySettings.mute_until)
    last_message_date: object | None  # datetime | None — активность для «стартовой загрузки» (≤30 дней)
    # Ф11-7: собеседник прочитал мои до этого id (`dialog.read_outbox_max_id`) — догон «прочитано» после сна.
    read_outbox_max_id: int = 0


@dataclass(frozen=True)
class NewMessageGE:
    message: object  # types.Message | types.MessageService
    # Раздел «Идентификаторы»/находка 2 приёмки куска 1b (round 1): чат/отправитель из САМОГО апдейта
    # (`event.message.chat`/`.sender`, если Telethon УЖЕ их знает из кэша) — раньше `connector.py` смотрел
    # только в собственный кэш `_entities`, наполняемый ЧАСТИЧНЫМ проходом сверки (останов после 20 тихих
    # диалогов), и живое сообщение из не увиденного этим проходом чата тихо терялось. НАХОДКА F12 (round 2):
    # сетевой fallback `event.get_chat()`/`get_sender()` УБРАН — он бил по сети ИЗНУТРИ последовательного
    # обработчика апдейтов (`sequential_updates=True`), блокируя обработку всех следующих апдейтов, пока не
    # придёт ответ. `{marked_id: entity}`, может быть пустым — тогда решает `connector._handle_live_message`
    # (свой кэш, иначе `_request_sync("unknown_chat")`, без сети здесь).
    entities: dict[int, object] = field(default_factory=dict)


@dataclass(frozen=True)
class EditedMessageGE:
    message: object
    entities: dict[int, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DeletedMessagesGE:
    channel_marked_id: int | None  # None — без канала (личка/малая группа, план 4.1)
    message_ids: list[int]


@dataclass(frozen=True)
class ReadInboxGE:
    peer_marked_id: int
    max_id: int


@dataclass(frozen=True)
class PeerReadGE:
    """Ф11-7: собеседник прочитал МОИ сообщения до `max_id` (`UpdateReadHistoryOutbox` — лички и группы,
    `UpdateReadChannelOutbox` — супергруппы)."""

    peer_marked_id: int
    max_id: int


def peer_read_ge(event: object) -> PeerReadGE | None:
    """`events.MessageRead.Event` -> `PeerReadGE` только для прочтения собеседником (`outbox=True`);
    прочтение мной, «прочитано содержимое» и событие без чата/`max_id` -> `None`. Без сети."""
    if not getattr(event, "outbox", False) or getattr(event, "contents", False):
        return None
    chat_id = getattr(event, "chat_id", None)
    max_id = getattr(event, "max_id", None)
    if chat_id is None or max_id is None:
        return None
    return PeerReadGE(peer_marked_id=chat_id, max_id=max_id)


@dataclass(frozen=True)
class ReactionsGE:
    peer_marked_id: int
    msg_id: int
    reactions: object  # types.MessageReactions
    # Находка F4 (round 2): реакция в теме форума несёт `top_msg_id` (id корня темы) прямо в
    # `UpdateMessageReactions` — коннектор может адресовать её СРАЗУ в тему, не полагаясь только на
    # ингест-фолбэк (поиск по родителю + детям, `ingest._find_channel_ids_with_topics`).
    top_msg_id: int | None = None


class FoldersChangedGE:
    """`UpdateDialogFilter`/`UpdateDialogFilterOrder`/`UpdateDialogFilters` — перезапросить ВСЕ фильтры
    сразу."""


class FoldersDirtyGE:
    """`UpdateNotifySettings`/`UpdateFolderPeers` — «грязно», пересчёт в ближайшей сверке (не немедленно)."""


class MediaUploadError(Exception):
    """Ф10-4 B2/П3: сбой ДО `SendMediaRequest` (`upload_file`: сеть, FloodWait, диск) — сообщение источник
    показать не мог. Исходное исключение — `cause` (и `__cause__`): коннектор решает по нему (`flood`,
    потеря входа, иначе `temporary`).

    `stage` (Ф11-3b П1): `"upload_file"` — заливка байтов; `"upload_media"` — `messages.UploadMediaRequest`
    элемента альбома (здесь Telegram проверяет фото: его отказ «фото не принято» — повод повторить альбом
    документами, а не `temporary`)."""

    def __init__(self, cause: BaseException, *, stage: str = "upload_file") -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause
        self.stage = stage


@dataclass(frozen=True)
class AlbumItem:
    """Ф11-3b B2: один файл альбома для `send_album` — путь временного файла, вид (`photo`|`document`), имя,
    mime и `force_file` (картинка документом)."""

    path: str
    kind: str
    name: str
    mime: str | None
    force_file: bool = False


@dataclass(frozen=True)
class ContactsResult:
    """Ф14-2 B1: ответ `contacts.GetContacts` — пользователи-контакты (TL `User`, как пришли) и `hash` для
    следующего запроса (`contacts_hash`)."""

    hash: int
    users: list


_HASH_MASK = (1 << 64) - 1


def contacts_hash(saved_count: int, user_ids: Iterable[int]) -> int:
    """Ф14-2 B1: `hash` для `contacts.GetContacts` — обычный алгоритм Telegram («Hash generation»,
    core.telegram.org/api/offsets) над `saved_count` и id контактов по возрастанию: 64-битное
    `h ^= h >> 21; h ^= h << 35; h ^= h >> 4; h += id`, результат — как знаковое 64-битное. Неверный хэш
    безвреден: сервер просто вернёт весь список (коннектор сравнивает список сам)."""
    h = 0
    for value in (saved_count, *sorted(user_ids)):
        h ^= h >> 21
        h = (h ^ (h << 35)) & _HASH_MASK
        h ^= h >> 4
        h = (h + (value & _HASH_MASK)) & _HASH_MASK
    return h - (1 << 64) if h >= 1 << 63 else h


GatewayEvent = (
    NewMessageGE
    | EditedMessageGE
    | DeletedMessagesGE
    | ReadInboxGE
    | PeerReadGE
    | ReactionsGE
    | FoldersChangedGE
    | FoldersDirtyGE
)


class PressUnknown(Exception):
    """Ф44-1c П1: запрос нажатия кнопки ушёл (или мог уйти) в сеть, а ответа нет — обрыв, переподключение,
    таймаут. Нажал ли бот действие — неизвестно; повторять нельзя. `reason` — короткое слово для журнала."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Unsendable:
    """«Данные», которые упаковщик Telethon (`MessagePacker.get`) не отправит: он сравнивает только длину
    с `MessageContainer.MAXIMUM_SIZE` и выбрасывает запрос из очереди, ничего не записав в сеть."""

    def __len__(self) -> int:
        return MessageContainer.MAXIMUM_SIZE + 1


_UNSENDABLE = _Unsendable()


class _OneShotState(RequestState):
    """Ф44-1c П1: состояние запроса, которое уходит в сеть не больше одного раза за сеанс MTProto.

    Telethon переотправляет ожидающие запросы сам, мимо вызывающего: `MTProtoSender._reconnect` кладёт все
    `_pending_state` обратно в очередь (отменён ли их future — не смотрит), и сервер получает запрос второй
    раз под новым сеансом. Для нажатия кнопки это второе действие бота. Здесь `data` читает упаковщик в момент
    отправки: первая выдача запоминает id сеанса (`MTProtoState.id`, меняется только при переподключении);
    повтор в ТОМ ЖЕ сеансе — ответ сервера `bad_server_salt`/`bad_msg_notification`, запрос сервером
    отвергнут и не выполнен, переотправка законна; в другом сеансе или после `close()` — «неотправляемые»
    данные: упаковщик снимет запрос сам и поставит future ошибку `ValueError`."""

    def __init__(self, request: object, sender: object) -> None:
        self._sender = sender
        self._bytes = b""
        self._session: int | None = None
        self._closed = False
        super().__init__(request)

    @property
    def data(self):
        if self._closed:
            return _UNSENDABLE
        session = self._sender._state.id
        if self._session is not None and self._session != session:
            return _UNSENDABLE
        self._session = session
        return self._bytes

    @data.setter
    def data(self, value: bytes) -> None:
        self._bytes = value

    @property
    def sent(self) -> bool:
        """Упаковщик уже брал запрос на отправку (в сеть он мог уйти)."""
        return self._session is not None

    def close(self) -> None:
        self._closed = True


class Gateway(Protocol):
    """Контракт, которым пользуется `connector.py`. `TgGateway` (ниже) и тестовый `FakeGateway` — две его
    реализации; сигнатуры — то немногое, что коннектору нужно от источника."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def is_authorized(self) -> bool: ...
    async def get_me(self) -> object | None: ...
    def iter_dialogs(self) -> AsyncIterator[DialogInfo]: ...
    async def get_messages(
        self, peer: object, *, min_id: int | None, limit: int
    ) -> tuple[list, dict[int, object]]: ...
    async def get_dialog_filters(self) -> list: ...
    async def get_forum_topics(self, peer: object, *, ids: Iterable[int] | None = None) -> list: ...
    def subscribe(self, handler: Callable[[GatewayEvent], None]) -> None: ...
    # --- отправка (кусок 1c, docs/architecture.md) --------------------------------------------
    async def get_input_entity(self, marked_id: int) -> object: ...
    async def send_message(
        self, peer: object, *, message: str, random_id: int, reply_to: object | None = None
    ) -> object: ...
    async def get_authorizations(self) -> list: ...
    # --- отправка файла (Ф10-4 B2) ------------------------------------------------------------------------
    async def send_media(
        self,
        peer: object,
        *,
        path: str,
        kind: str,
        name: str,
        mime: str | None,
        text: str,
        random_id: int,
        reply_to: object | None = None,
        force_file: bool = False,
        duration: int = 0,
    ) -> object: ...
    # --- альбом (Ф11-3b B2) --------------------------------------------------------------------------------
    async def send_album(
        self,
        peer: object,
        *,
        items: list[AlbumItem],
        text: str,
        random_ids: list[int],
        reply_to: object | None = None,
    ) -> object: ...
    # --- медиа (Ф8-5 C1) ----------------------------------------------------------------------------------
    async def get_messages_by_ids(self, peer: object, ids: list[int]) -> list: ...
    async def download_media(self, message: object, file: object) -> object | None: ...
    # --- прочитанность из хаба (Ф8-7 C1, П5) ----------------------------------------------------------------
    async def read_history(self, peer: object, max_id: int) -> None: ...
    async def read_discussion(self, peer: object, topic_id: int, max_id: int) -> None: ...
    # --- реакции из хаба (Ф10-3 C1, П4) -------------------------------------------------------------------
    async def send_reaction(self, peer: object, msg_id: int, emojis: list[str]) -> object: ...
    async def get_reactions(self, peer: object, msg_id: int) -> object: ...
    async def get_reactions_many(self, peer: object, ids: list[int]) -> object: ...
    # Ф44-1c: нажатие callback-кнопки бота — ОДИН запрос, без повторов (исходы — `TgGateway.press_button`)
    async def press_button(self, peer: object, msg_id: int, data: bytes, *, timeout_s: float) -> object: ...
    # --- правка и «удалить у всех» (Ф14-8 C1) ---------------------------------------------------------------
    async def edit_message(self, peer: object, msg_id: int, text: str) -> object: ...
    async def delete_messages(self, peer: object, ids: list[int]) -> int: ...
    # --- черновик новому адресату (Ф12-1 B2) ----------------------------------------------------------------
    async def resolve_username(self, username: str) -> object: ...
    async def resolve_phone(self, phone: str) -> object: ...
    async def get_user_from_message(self, chat_peer: object, msg_id: int, user_id: int) -> object | None: ...
    # --- контакты (Ф14-2 B1/B2) -----------------------------------------------------------------------------
    async def get_contacts(self, hash: int) -> ContactsResult | None: ...
    # --- вступление в группы и каналы (Ф17-1 C) ------------------------------------------------------------
    async def join_channel(self, input_channel: object) -> object: ...
    async def import_invite(self, invite_hash: str) -> object: ...
    async def check_invite(self, invite_hash: str) -> object: ...
    async def get_full_channel(self, input_channel: object) -> object: ...
    # Ф17-2 C: выход из групп и каналов.
    async def get_entity(self, input_peer: object) -> object: ...
    async def leave_channel(self, input_channel: object) -> object: ...
    async def leave_chat(self, chat_id: int) -> object: ...
    # Ф29-1 C2/C3: поиск каналов и групп, история публичного чата без вступления.
    async def search_chats(self, query: str, *, limit: int) -> object: ...
    async def get_history(
        self, peer: object, *, offset_id: int, limit: int
    ) -> tuple[list, dict[int, object]]: ...


def _event_entities(event: object) -> dict[int, object]:
    """Чат и отправитель уже готового живого события (`events.NewMessage`/`MessageEdited`) — БЕЗ СЕТИ
    (находка F12, round 2 критика 1b). `event.message.chat`/`.sender` — то, что Telethon УЖЕ разрешил из
    своего кэша сущностей при разборе апдейта (`_finish_init`, см. докстринг `convert.py`), синхронно, без
    единого запроса. Раньше здесь был fallback `event.get_chat()`/`get_sender()` — он бил по сети ПРЯМО ИЗ
    последовательного обработчика (`sequential_updates=True`), застревая на каждом «непрогретом» чате и
    блокируя обработку всех следующих апдейтов, пока не придёт ответ (проверено по исходникам: `get_chat`/
    `get_sender` на непопавший в кэш peer шлют `users.getUsers`/аналог). Пусто -> решает
    `connector._handle_live_message` (свой кэш `_entities`, иначе `_request_sync("unknown_chat")`)."""
    return message_entities(event.message)


def message_entities(message: object) -> dict[int, object]:
    """Чат и отправитель сообщения, которые Telethon УЖЕ разрешил при разборе (`_finish_init`) — без сети.
    Годится и для сообщений из ответа `get_messages` (там сущности — из `users`/`chats` того же ответа):
    Ф14-8, ревью — правда о своей правке не должна зависеть от кэша `_entities` коннектора."""
    out: dict[int, object] = {}
    chat = getattr(message, "chat", None)
    if chat is not None:
        out[utils.get_peer_id(chat)] = chat
    sender = getattr(message, "sender", None)
    if sender is not None:
        out[utils.get_peer_id(sender)] = sender
    return out


# --- настоящий гейтвей -----------------------------------------------------------------------------------


def _stray_cancel_to_connection_error(exc: asyncio.CancelledError) -> ConnectionError | None:
    """`None` — эту отмену нужно пробросить как есть (наша задача реально отменена, `task.cancelling() >
    0`); иначе — готовое исключение для `raise ... from exc` (раздел «Отправка», находка F2)."""
    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        return None
    return ConnectionError("Telethon: сторонняя отмена запроса (обрыв соединения)")


async def _guard_cancel(coro):
    """Находка F2 критика куска 1c: Telethon (`sender.disconnect()`, в т.ч. вызванный из `on_resume()`)
    отменяет СВОИ внутренние pending-future при разрыве соединения — это всплывает как
    `asyncio.CancelledError` из середины сетевого запроса, хотя НАШУ задачу никто не просил остановиться
    (проверено по исходникам: `network/mtprotosender.py::_disconnect` при `error=None` делает
    `state.future.cancel()` на каждый ожидающий запрос). Настоящую отмену пропускаем как есть; чужую —
    превращаем в `ConnectionError`, которую `connector.py` уже умеет классифицировать (`send()` -> `abandon`,
    сверка — обычная сетевая ошибка, ловится существующими `except`/`_log_dialog_error`)."""
    try:
        return await coro
    except asyncio.CancelledError as exc:
        mapped = _stray_cancel_to_connection_error(exc)
        if mapped is None:
            raise
        raise mapped from exc


class TgGateway:
    """`TelegramClient` ровно так, как решил архитектор (раздел «Устройство пакета», факт-чек 11.5 плана):
    `raise_last_call_error=True` (иначе `_call` после исчерпанных повторов на `ServerError` молча возвращает
    `None`), `flood_sleep_threshold=0` (он общий на клиент, не учитывает контекст «это отправка или сверка» —
    `FloodWaitError` обрабатывается явно вызывающим), `sequential_updates=True` (иначе удаление может обогнать
    вставку — обработчики здесь только конвертируют и кладут в очередь, без await на медленное),
    `catch_up=False` (открытый баг Telethon #3041 — целостность держит сверка, не catch_up).

    `client_factory` — для `hub auth tg`/тестов: подменяемая фабрика вместо самого `TelegramClient`
    (сеть в тестах запрещена целиком)."""

    def __init__(
        self,
        *,
        session_path: str,
        api_id: int,
        api_hash: str,
        proxy: dict | None,
        device_model: str,
        system_version: str,
        app_version: str,
        client_factory: Callable[..., TelegramClient] = TelegramClient,
    ) -> None:
        self._client = client_factory(
            SQLiteSession(session_path),
            api_id,
            api_hash,
            proxy=proxy,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
            raise_last_call_error=True,
            flood_sleep_threshold=0,
            sequential_updates=True,
            catch_up=False,
        )

    @property
    def client(self) -> TelegramClient:
        """Только для `auth.py` (`client.start(...)`, интерактивный вход) — обычный `connector.py` этим не
        пользуется, весь его доступ — через методы ниже."""
        return self._client

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def is_connected(self) -> bool:
        """Находка F2 (round 2): после исчерпания `connection_retries` Telethon остаётся отключённым
        НАВСЕГДА — никто, кроме `connect()` на старте и раз в `on_resume`, больше не пытается
        переподключиться.
        `connector.health()` проверяет это на каждом пульсе и переподключается сам (`_client.is_connected()`
        — синхронный метод сендера, без сети)."""
        return bool(self._client.is_connected())

    async def is_authorized(self) -> bool:
        return bool(await _guard_cancel(self._client.is_user_authorized()))

    async def get_me(self) -> object | None:
        """Сырой `GetUsersRequest([InputUserSelf()])`, НЕ `client.get_me()` — тот глотает `UnauthorizedError`
        и молча возвращает `None` (`client/users.py`: `except errors.UnauthorizedError: return None`), из-за
        чего потеря входа никогда бы не обнаруживалась ни в стартовом резолве `me_id`, ни в пульсе (находка
        F1, round 2). Здесь `UnauthorizedError`/`AuthKeyError` пробрасываются как есть — вызывающий
        (`connector.py`) сам решает, что это значит. Пустой ответ (не должен случаться, но не наше дело
        гадать) -> тоже `None`."""
        result = await _guard_cancel(self._client(GetUsersRequest([types.InputUserSelf()])))
        return result[0] if result else None

    async def iter_dialogs(self) -> AsyncIterator[DialogInfo]:
        # Без `archived=`/`folder=` — по исходникам (`client/dialogs.py::iter_dialogs`) `folder=None`
        # означает «Telegram сам решает, вернуть всё» (GetDialogsRequest.folder_id не задан) — ОДИН проход
        # отдаёт и основной список, и архив (см. отчёт исполнителя, раздел «Факт-чек»).
        try:
            async for d in self._client.iter_dialogs():
                notify = d.dialog.notify_settings
                yield DialogInfo(
                    peer=d.dialog.peer,
                    entity=d.entity,
                    top_message=d.dialog.top_message or None,
                    read_inbox_max_id=d.dialog.read_inbox_max_id,
                    pinned=bool(d.pinned),
                    archived=bool(d.archived),
                    mute_until=getattr(notify, "mute_until", None),
                    last_message_date=d.date,
                    read_outbox_max_id=d.dialog.read_outbox_max_id or 0,
                )
        except asyncio.CancelledError as exc:
            # Находка F2 критика куска 1c: см. `_guard_cancel` — генератор оборачивается так же, просто
            # руками (`await`-обёртку сюда не приладить).
            mapped = _stray_cancel_to_connection_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

    async def get_messages(
        self, peer: object, *, min_id: int | None, limit: int
    ) -> tuple[list, dict[int, object]]:
        """Сырой `GetHistoryRequest` (НЕ `client.get_messages`/`client.iter_messages` — те отдают
        `custom.Message`, обёрнутый клиентом; здесь и так `types.Message`/`MessageService`, патченные тем же
        классом — см. докстринг `convert.py` — но сырой запрос даёт индекс сущностей одним вызовом, без
        скрытых догрузок). Порядок — КАК ОТДАЁТ TELEGRAM (по убыванию id, offset_id=0 — от последнего);
        разворот в возрастающий для эмиссии — дело вызывающего (сверка, раздел «Сверка» спеки куска 1b)."""
        req = GetHistoryRequest(
            peer=peer,
            offset_id=0,
            offset_date=None,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=min_id or 0,
            hash=0,
        )
        result = await _guard_cancel(self._client(req))
        entities = {utils.get_peer_id(x): x for x in (*result.users, *result.chats)}
        # `MessageEmpty` (сообщение "провалено" на стороне Telegram — id есть, содержимого нет) — Telethon
        # сам фильтрует их в `client.iter_messages()`/`get_messages()`, но сырой `GetHistoryRequest` отдаёт
        # как есть; `convert.py` на нём падает (нет `peer_id`/`date`/т.д.) — находка F6/F11 (round 2): одно
        # такое сообщение в истории роняло бы всю сверку. Отфильтровываем здесь же, у источника.
        messages = [m for m in result.messages if not isinstance(m, types.MessageEmpty)]
        return messages, entities

    async def get_dialog_filters(self) -> list:
        result = await _guard_cancel(self._client(GetDialogFiltersRequest()))
        return list(result.filters)

    async def get_input_entity(self, marked_id: int) -> object:
        """Кэш сущностей сессии (раздел «Отправка» спеки куска 1c): передаём `int` marked id напрямую —
        по докстрингу `client.get_input_entity()` это ищется ТОЛЬКО в кэше (in-memory, потом диск-кэш
        сессии), без сети, кроме последнего резервного шага (`GetUsers`/`GetChannels` с `access_hash=0`),
        ненадёжного для каналов (не сработает без уже известного access_hash) — поэтому вызывающий
        (`connector.py`) на промах сам делает полный проход `iter_dialogs()` (наполняет кэш надёжно) и
        повторяет, а не полагается на этот резерв. Промах после этого -> `ValueError`, как документирует
        сам Telethon."""
        return await _guard_cancel(self._client.get_input_entity(marked_id))

    async def send_message(
        self, peer: object, *, message: str, random_id: int, reply_to: object | None = None
    ) -> object:
        """Сырой `messages.SendMessageRequest` (решение 11.5/раздел «Отправка»): `send_message()` клиента
        НЕ принимает `random_id`, а `random_id` у нас детерминирован из `send_token` — нужен сырой запрос.
        `peer` — уже `InputPeer` (результат `get_input_entity()`); `resolve()` запроса на нём — быстрый
        no-op (`utils.get_input_peer` на уже-InputPeer возвращает его же, без кэша/сети)."""
        req = SendMessageRequest(
            peer=peer, message=message, random_id=random_id, reply_to=reply_to, no_webpage=False
        )
        return await _guard_cancel(self._client(req))

    async def send_media(
        self,
        peer: object,
        *,
        path: str,
        kind: str,
        name: str,
        mime: str | None,
        text: str,
        random_id: int,
        reply_to: object | None = None,
        force_file: bool = False,
        duration: int = 0,
    ) -> object:
        """Ф10-4 B2: `client.upload_file(path, file_name=name)` -> сырой `messages.SendMediaRequest` с нашим
        `random_id` (как `send_message`: у клиентского `send_file` его не задать). `kind == "photo"` ->
        `InputMediaUploadedPhoto`, иначе `InputMediaUploadedDocument` с именем файла (`force_file` — картинка
        документом, П11). Сбой загрузки (до `SendMediaRequest`) -> `MediaUploadError`; ошибки самого запроса
        — как есть, классифицирует `connector.py`.

        Ф14-4 B2: `kind == "voice"` (файл — ogg/opus) -> документ `audio/ogg` с
        `DocumentAttributeAudio(voice=True, duration=…)` — настоящее голосовое (волна и длительность)."""
        try:
            uploaded = await _guard_cancel(self._client.upload_file(path, file_name=name))
        except Exception as exc:
            raise MediaUploadError(exc) from exc
        if kind == "photo":
            media: object = types.InputMediaUploadedPhoto(file=uploaded)
        elif kind == "voice":
            media = types.InputMediaUploadedDocument(
                file=uploaded,
                mime_type="audio/ogg",
                attributes=[types.DocumentAttributeAudio(duration=max(int(duration), 0), voice=True)],
            )
        else:
            media = types.InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime or "application/octet-stream",
                attributes=[types.DocumentAttributeFilename(file_name=name)],
                force_file=True if force_file else None,
            )
        req = SendMediaRequest(peer=peer, media=media, message=text, random_id=random_id, reply_to=reply_to)
        return await _guard_cancel(self._client(req))

    async def send_album(
        self,
        peer: object,
        *,
        items: list[AlbumItem],
        text: str,
        random_ids: list[int],
        reply_to: object | None = None,
    ) -> object:
        """Ф11-3b B2 (как `_send_album` Telethon 1.45, но со своими `random_id`): каждый файл —
        `upload_file` -> `messages.UploadMediaRequest(peer, InputMediaUploaded…)` -> `get_input_media`
        (`InputMediaUploaded*` прямо в альбом нельзя — `MediaInvalidError`), затем ОДИН
        `messages.SendMultiMediaRequest(peer, multi_media=[InputSingleMedia(media, random_id, message)],
        reply_to)`. Подпись — только у элемента 0. Сбой `upload_file` -> `MediaUploadError(stage=
        "upload_file")`, сбой `UploadMediaRequest` -> `MediaUploadError(stage="upload_media")` (до отправки,
        П1); ошибки `SendMultiMediaRequest` — как есть, классифицирует `connector.py`."""
        multi: list[object] = []
        for k, item in enumerate(items):
            try:
                uploaded = await _guard_cancel(self._client.upload_file(item.path, file_name=item.name))
            except Exception as exc:
                raise MediaUploadError(exc, stage="upload_file") from exc
            if item.kind == "photo":
                media: object = types.InputMediaUploadedPhoto(file=uploaded)
            else:
                media = types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type=item.mime or "application/octet-stream",
                    attributes=[types.DocumentAttributeFilename(file_name=item.name)],
                    force_file=True if item.force_file else None,
                )
            try:
                r = await _guard_cancel(self._client(UploadMediaRequest(peer=peer, media=media)))
                if isinstance(r, types.MessageMediaPhoto):
                    input_media = utils.get_input_media(r.photo)
                elif isinstance(r, types.MessageMediaDocument):
                    input_media = utils.get_input_media(r.document)
                else:
                    raise TypeError(type(r).__name__)
            except Exception as exc:
                raise MediaUploadError(exc, stage="upload_media") from exc
            multi.append(
                types.InputSingleMedia(
                    media=input_media, random_id=random_ids[k], message=text if k == 0 else ""
                )
            )
        req = SendMultiMediaRequest(peer=peer, multi_media=multi, reply_to=reply_to)
        return await _guard_cancel(self._client(req))

    async def get_authorizations(self) -> list:
        """Раздел «Алерт о новой сессии Telegram» — `account.GetAuthorizationsRequest`, сырой список
        `Authorization` (хеш/устройство/платформа/страна/дата/`current`)."""
        result = await _guard_cancel(self._client(GetAuthorizationsRequest()))
        return list(result.authorizations)

    async def get_messages_by_ids(self, peer: object, ids: list[int]) -> list:
        """Ф8-5 C1/П15: сообщения по id (перечитать перед загрузкой медиа — свежий `file_reference`).
        `client.get_messages(peer, ids=…)` сам выбирает `channels.getMessages`/`messages.getMessages` и
        отдаёт `None` на месте несуществующего/удалённого. `peer` — уже `InputPeer` (без сети на разрешение).
        Имя — не `get_messages`: тот уже занят историей (`min_id`/`limit`)."""
        result = await _guard_cancel(self._client.get_messages(peer, ids=list(ids)))
        return list(result) if result is not None else []

    async def download_media(self, message: object, file: object) -> object | None:
        """Ф8-5 П4: `client.download_media(message, file=…)` — Telethon сам берёт наибольший размер фото;
        `file` — пишущий объект загрузчика (`MediaSink`, куски пишутся по мере прихода). `None` — у
        сообщения нет скачиваемого медиа."""
        return await _guard_cancel(self._client.download_media(message, file=file))

    async def read_history(self, peer: object, max_id: int) -> None:
        """Ф8-7 C1: «прочитано до `max_id`» обычного чата — `client.send_read_acknowledge(peer, max_id=…)`
        (Telethon сам выбирает `channels.readHistory`/`messages.readHistory`). Для ФОРУМА не звать никогда:
        id сообщений у тем общие — отметило бы все темы (П5); тема — только `read_discussion`. `peer` — уже
        `InputPeer` (без сети на разрешение)."""
        await _guard_cancel(self._client.send_read_acknowledge(peer, max_id=max_id))

    async def read_discussion(self, peer: object, topic_id: int, max_id: int) -> None:
        """Ф8-7 C1/П5: «прочитано до `max_id`» в теме форума, включая General (`topic_id == 1`) — сырой
        `messages.ReadDiscussionRequest(peer, msg_id=topic_id, read_max_id=max_id)`."""
        await _guard_cancel(
            self._client(ReadDiscussionRequest(peer=peer, msg_id=topic_id, read_max_id=max_id))
        )

    async def send_reaction(self, peer: object, msg_id: int, emojis: list[str]) -> object:
        """Ф10-3 C1: сырой `messages.SendReactionRequest` — ЗАМЕНЯЕТ весь набор своих реакций на сообщении
        (`[]` — снять). Тема форума: `peer` — сам чат, `msg_id` — id сообщения. Ответ — `Updates` (П4: в нём
        обычно `UpdateMessageReactions` этого сообщения; эха обработчикам на свой запрос Telethon не даёт)."""
        req = SendReactionRequest(
            peer=peer,
            msg_id=msg_id,
            reaction=[types.ReactionEmoji(emoticon=e) for e in emojis],
            big=False,
            add_to_recent=True,
        )
        return await _guard_cancel(self._client(req))

    async def get_reactions(self, peer: object, msg_id: int) -> object:
        """Ф10-3 П4: `messages.GetMessagesReactionsRequest(peer, id=[msg_id])` — `Updates` с
        `UpdateMessageReactions`; разбирает вызывающий (`convert.message_reactions_from_updates`)."""
        return await _guard_cancel(self._client(GetMessagesReactionsRequest(peer=peer, id=[msg_id])))

    async def get_reactions_many(self, peer: object, ids: list[int]) -> object:
        """Ф18-2: тот же `messages.GetMessagesReactionsRequest`, но пачкой (до 100 id) — `Updates` с
        `UpdateMessageReactions` по каждому сообщению (`convert.message_reactions_all`)."""
        return await _guard_cancel(self._client(GetMessagesReactionsRequest(peer=peer, id=list(ids))))

    async def press_button(self, peer: object, msg_id: int, data: bytes, *, timeout_s: float) -> object:
        """Ф44-1c П1: `messages.GetBotCallbackAnswerRequest` — ровно ОДИН запрос. Не `client(request)`: его
        цикл `_call` повторяет `TimedOutError`/`ServerError` до `request_retries` раз, а у этого запроса
        `TimedOutError` — обычный исход (бот не ответил на нажатие, которое уже получил), и каждый повтор —
        ещё одно нажатие. Запрос кладётся в очередь отправителя напрямую своим состоянием `_OneShotState`
        (после переподключения Telethon его не переотправит).

        Исходы: ответ `BotCallbackAnswer`; `RPCError` сервера — как есть (`TimedOutError`,
        `BotResponseTimeoutError`, `FloodWaitError`, `MessageIdInvalidError`, …: запрос сервером обработан
        один раз); `ConnectionError` — запрос в сеть НЕ уходил (нет соединения, не ушёл за `timeout_s`);
        `PressUnknown` — ушёл или мог уйти, ответа нет (обрыв, переподключение, `timeout_s`). Настоящая отмена
        нашей задачи — пробрасывается, запрос при этом снимается из очередей отправителя."""
        client = self._client
        request = GetBotCallbackAnswerRequest(peer=peer, msg_id=msg_id, data=data)
        await _guard_cancel(request.resolve(client, utils))
        sender = client._sender
        if sender is None or not sender._user_connected:
            raise ConnectionError("Telethon: нет соединения — нажатие не отправлено")
        state = _OneShotState(request, sender)
        sender._send_queue.append(state)
        try:
            # `shield`: отмена ожидания не должна отменить сам future — упаковщик и разбор ответа ставят
            # ему результат без проверки и упали бы с `InvalidStateError`, уронив цикл отправителя.
            return await asyncio.wait_for(asyncio.shield(state.future), timeout_s)
        except TimeoutError as exc:
            self._drop_press(sender, state)
            if state.sent:
                raise PressUnknown("no_answer") from exc
            raise ConnectionError("Telethon: нажатие не ушло в сеть за отведённое время") from exc
        except asyncio.CancelledError as exc:
            self._drop_press(sender, state)
            if _stray_cancel_to_connection_error(exc) is None:
                raise
            # Отмену поставил Telethon (`_disconnect` отменяет ожидающие future при обрыве).
            if state.sent:
                raise PressUnknown("disconnected") from exc
            raise ConnectionError("Telethon: обрыв соединения — нажатие не отправлено") from exc
        except ValueError as exc:
            # Упаковщик снял запрос: это повторная отправка в новом сеансе после переподключения.
            self._drop_press(sender, state)
            raise PressUnknown("reconnected") from exc
        except RPCError:
            raise
        except Exception as exc:
            # Ошибка соединения, поставленная Telethon ожидающим запросам (`_disconnect(error=…)`), и прочее.
            self._drop_press(sender, state)
            if state.sent:
                raise PressUnknown(type(exc).__name__) from exc
            raise ConnectionError(f"Telethon: {type(exc).__name__} — нажатие не отправлено") from exc

    @staticmethod
    def _drop_press(sender: object, state: _OneShotState) -> None:
        """Снять запрос нажатия отовсюду, откуда Telethon мог бы отправить его снова: из очереди отправки и
        из ожидающих ответа; сам запрос помечается «неотправляемым» (на случай, если он уже у упаковщика)."""
        state.close()
        with contextlib.suppress(ValueError):
            sender._send_queue._deque.remove(state)
        if state.msg_id is not None and sender._pending_state.get(state.msg_id) is state:
            del sender._pending_state[state.msg_id]

    async def edit_message(self, peer: object, msg_id: int, text: str) -> object:
        """Ф14-8 C1: сырой `messages.EditMessageRequest` — текст (у сообщения с медиа — подпись) простым
        текстом, без entities: URL, @username и #теги сервер размечает сам. Тема форума: `peer` — сам чат.
        Ответ — `Updates` (эха обработчикам на свой запрос Telethon не даёт — правду перечитывает
        `connector.py`)."""
        req = EditMessageRequest(peer=peer, id=msg_id, message=text, no_webpage=False)
        return await _guard_cancel(self._client(req))

    async def delete_messages(self, peer: object, ids: list[int]) -> int:
        """Ф14-8 C1: «удалить у всех» — `client.delete_messages(peer, ids, revoke=True)` (сам выбирает
        `messages.DeleteMessages` / `channels.DeleteMessages`). Возвращает сумму `pts_count` ответов
        (`AffectedMessages`): 0 — ничего не удалено."""
        result = await _guard_cancel(self._client.delete_messages(peer, list(ids), revoke=True))
        items = result if isinstance(result, list) else [result]
        return sum(int(getattr(r, "pts_count", 0) or 0) for r in items if r is not None)

    async def resolve_username(self, username: str) -> object:
        """Ф12-1 B2: сырой `contacts.ResolveUsernameRequest` — сущность (`User`/`Chat`/`Channel`) по
        @username; Telethon сам кладёт сущности ответа в кэш сессии. Ошибки (`UsernameNotOccupied`,
        `UsernameInvalid`, `FloodWait`, сеть) — как есть, классифицирует `connector.py`."""
        result = await _guard_cancel(self._client(ResolveUsernameRequest(username=username)))
        peer_id = utils.get_peer_id(result.peer)
        for entity in (*result.users, *result.chats):
            if utils.get_peer_id(entity) == peer_id:
                return entity
        raise ValueError("resolve_username: сущности нет в ответе")

    async def resolve_phone(self, phone: str) -> object:
        """Ф34-3: сырой `contacts.ResolvePhoneRequest` — сущность по номеру телефона (только цифры, без
        «+») БЕЗ добавления в контакты; импорт контактов хаб не использует вовсе. Ошибки
        (`PhoneNotOccupied`, `PhoneNumberInvalid`, `FloodWait`, сеть) — как есть, классифицирует
        `connector.py`. Номер — ПДн: запрос и его `repr`/`to_dict()` в журнал не писать."""
        result = await _guard_cancel(self._client(ResolvePhoneRequest(phone=phone)))
        peer_id = utils.get_peer_id(result.peer)
        for entity in (*result.users, *result.chats):
            if utils.get_peer_id(entity) == peer_id:
                return entity
        raise ValueError("resolve_phone: сущности нет в ответе")

    async def get_user_from_message(self, chat_peer: object, msg_id: int, user_id: int) -> object | None:
        """Ф46-1 (раздел 4, шаг 4): сырой `users.GetUsers([InputUserFromMessage])` — пользователь по
        сообщению группы, в котором он автор (доступ к человеку без @ника, номера и контакта). Только
        чтение. Пустой ответ — `None`. Ошибки (`MsgIdInvalid`, `PeerIdInvalid`, `ChannelPrivate`,
        `FloodWait`, сеть) — как есть, классифицирует `connector.py`. В списке запроса —
        `InputPeerUserFromMessage`: `resolve()` запроса отдаёт его `get_input_entity` как есть (без сети) и
        сам превращает в `InputUserFromMessage`."""
        ref = types.InputPeerUserFromMessage(peer=chat_peer, msg_id=msg_id, user_id=user_id)
        result = await _guard_cancel(self._client(GetUsersRequest([ref])))
        return result[0] if result else None

    async def get_contacts(self, hash: int) -> ContactsResult | None:
        """Ф14-2 B1/B2: сырой `contacts.GetContactsRequest(hash)`; `ContactsNotModified` -> `None`. Telethon
        сам кладёт пользователей ответа в кэш сущностей сессии (после этого `get_input_entity(user_id)`
        находит контакт). Ошибки (`FloodWait`, сеть) — как есть, классифицирует `connector.py`."""
        result = await _guard_cancel(self._client(GetContactsRequest(hash=hash)))
        if isinstance(result, types.contacts.ContactsNotModified):
            return None
        ids = [c.user_id for c in result.contacts]
        return ContactsResult(hash=contacts_hash(result.saved_count, ids), users=list(result.users))

    # --- вступление в группы и каналы (Ф17-1 C): сырые вызовы, ошибки как есть — классифицирует connector.py

    async def join_channel(self, input_channel: object) -> object:
        """`channels.JoinChannelRequest` — вступить в публичный канал/супергруппу. Ответ — `Updates` (в
        `chats` — сам чат)."""
        return await _guard_cancel(self._client(JoinChannelRequest(channel=input_channel)))

    async def import_invite(self, invite_hash: str) -> object:
        """`messages.ImportChatInviteRequest` — вступить по ссылке-приглашению. Ответ — `Updates`; при
        одобрении админами — `InviteRequestSentError`."""
        return await _guard_cancel(self._client(ImportChatInviteRequest(hash=invite_hash)))

    async def check_invite(self, invite_hash: str) -> object:
        """`messages.CheckChatInviteRequest` — `ChatInvite` (не участник, id нет) | `ChatInviteAlready` (уже
        участник, `chat`) | `ChatInvitePeek` (предпросмотр, `chat`)."""
        return await _guard_cancel(self._client(CheckChatInviteRequest(hash=invite_hash)))

    async def get_full_channel(self, input_channel: object) -> object:
        """`channels.GetFullChannelRequest` — `messages.ChatFull` (`full_chat.participants_count`,
        `full_chat.about`)."""
        return await _guard_cancel(self._client(GetFullChannelRequest(channel=input_channel)))

    # --- выход из групп и каналов (Ф17-2 C): сырые вызовы, ошибки как есть — классифицирует connector.py ----

    async def get_entity(self, input_peer: object) -> object:
        """Свежая сущность чата с сервера (`channels.GetChannels` / `messages.GetChats` по `InputPeer*`): флаг
        `left`, `ChannelForbidden`/`ChatForbidden`. Закрытый канал, где владелец не участник, —
        `ChannelPrivateError`."""
        return await _guard_cancel(self._client.get_entity(input_peer))

    async def leave_channel(self, input_channel: object) -> object:
        """`channels.LeaveChannelRequest` — выйти из канала/супергруппы (и форума целиком). Ответ —
        `Updates`."""
        return await _guard_cancel(self._client(LeaveChannelRequest(channel=input_channel)))

    async def leave_chat(self, chat_id: int) -> object:
        """`messages.DeleteChatUserRequest(chat_id, InputUserSelf())` — выйти из обычной (basic) группы БЕЗ
        чистки истории (не `client.delete_dialog`: тот у basic-группы ещё и чистит историю диалога)."""
        return await _guard_cancel(
            self._client(DeleteChatUserRequest(chat_id=chat_id, user_id=types.InputUserSelf()))
        )

    # --- поиск и чтение без вступления (Ф29-1 C2/C3): сырые вызовы, ошибки классифицирует connector.py

    async def search_chats(self, query: str, *, limit: int) -> object:
        """`contacts.SearchRequest(q, limit)` -> `contacts.Found` (`my_results` — среди своих диалогов,
        `results` — глобально, `chats`, `users`). Telethon кладёт сущности ответа в кэш сессии."""
        return await _guard_cancel(self._client(SearchRequest(q=query, limit=limit)))

    async def get_history(
        self, peer: object, *, offset_id: int, limit: int
    ) -> tuple[list, dict[int, object]]:
        """Сырой `GetHistoryRequest` с `offset_id` (листать назад: сообщения с id меньше `offset_id`; 0 — с
        последнего) — публичный канал и публичная супергруппа читаются без вступления, закрытые —
        `ChannelPrivateError`. Порядок — как отдаёт Telegram (новые первыми); `MessageEmpty` отброшены;
        сущности ответа — индексом по помеченному id (как `get_messages`)."""
        req = GetHistoryRequest(
            peer=peer,
            offset_id=offset_id,
            offset_date=None,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
        )
        result = await _guard_cancel(self._client(req))
        entities = {utils.get_peer_id(x): x for x in (*result.users, *result.chats)}
        messages = [m for m in result.messages if not isinstance(m, types.MessageEmpty)]
        return messages, entities

    async def get_forum_topics(self, peer: object, *, ids: Iterable[int] | None = None) -> list:
        if ids:
            result = await _guard_cancel(self._client(GetForumTopicsByIDRequest(peer=peer, topics=list(ids))))
        else:
            result = await _guard_cancel(
                self._client(
                    GetForumTopicsRequest(
                        peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=_FORUM_TOPICS_PAGE
                    )
                )
            )
        return list(result.topics)

    def subscribe(self, handler: Callable[[GatewayEvent], None]) -> None:
        """`sequential_updates=True`: обработчики только конвертируют апдейт в `GatewayEvent` и кладут в
        буфер `connector.py` (`handler` синхронный, БЕЗ сети — находка F12, round 2), раздел 11.5 плана.

        `events.NewMessage` сам ВЫБРАСЫВАЕТ `MessageService` (`events/newmessage.py::NewMessage.build`
        возвращает `None`, если `update.message` — не обычный `Message`) — живые служебные (миграция, темы,
        закрепы, вход/выход) без этого не приходили бы никогда (находка F3, round 2). Ловим их отдельно,
        через `events.Raw` на `UpdateNewMessage`/`UpdateNewChannelMessage` с проверкой типа сообщения —
        обычные `Message` тем же путём НЕ дублируются (тот же `Raw`-хендлер их явно пропускает)."""
        client = self._client

        @client.on(events.NewMessage())
        async def _on_new(event: events.NewMessage.Event) -> None:
            handler(NewMessageGE(message=event.message, entities=_event_entities(event)))

        @client.on(events.MessageEdited())
        async def _on_edit(event: events.MessageEdited.Event) -> None:
            handler(EditedMessageGE(message=event.message, entities=_event_entities(event)))

        @client.on(events.MessageDeleted())
        async def _on_delete(event: events.MessageDeleted.Event) -> None:
            channel_marked_id = event.chat_id  # None без канала (личка/малая группа) — ChatGetter/EventCommon
            handler(
                DeletedMessagesGE(channel_marked_id=channel_marked_id, message_ids=list(event.deleted_ids))
            )

        @client.on(events.MessageRead(inbox=True))
        async def _on_read(event: events.MessageRead.Event) -> None:
            if event.chat_id is None or event.max_id is None:
                return
            # Находка F5 (round 2): "прочитано содержимое" (`UpdateChannelReadMessagesContents` — открыли
            # голосовое/спойлер или нажали кнопку "@"-упоминаний) строит `MessageRead.Event` с тем же
            # `max_id`, что и НАСТОЯЩЕЕ прочтение истории, хотя историю никто не листал — двигать маркер
            # всего канала этим нельзя. `event.contents=True` — именно этот случай, пропускаем целиком.
            if event.contents:
                return
            handler(ReadInboxGE(peer_marked_id=event.chat_id, max_id=event.max_id))

        @client.on(events.MessageRead(inbox=False))
        async def _on_peer_read(event: events.MessageRead.Event) -> None:
            # Ф11-7: собеседник прочитал мои сообщения (лички, группы, супергруппы).
            ge = peer_read_ge(event)
            if ge is not None:
                handler(ge)

        @client.on(events.Raw())
        async def _on_raw(update: object) -> None:
            if isinstance(update, types.UpdateNewMessage | types.UpdateNewChannelMessage):
                # Обычные Message сюда тоже попадают (Raw видит ВСЕ апдейты) — но их уже забрал
                # `events.NewMessage` выше; здесь нас интересуют ТОЛЬКО MessageService (F3).
                if isinstance(update.message, types.MessageService):
                    handler(NewMessageGE(message=update.message, entities={}))
            elif isinstance(update, types.UpdateMessageReactions):
                handler(
                    ReactionsGE(
                        peer_marked_id=utils.get_peer_id(update.peer),
                        msg_id=update.msg_id,
                        reactions=update.reactions,
                        top_msg_id=update.top_msg_id,
                    )
                )
            elif isinstance(
                update, types.UpdateDialogFilter | types.UpdateDialogFilterOrder | types.UpdateDialogFilters
            ):
                handler(FoldersChangedGE())
            elif isinstance(update, types.UpdateNotifySettings | types.UpdateFolderPeers):
                handler(FoldersDirtyGE())
