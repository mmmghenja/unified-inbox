"""Общие постройки TL-объектов и `FakeGateway` для тестов коннектора Telegram (кусок 1b фазы 1). Ничего из
этого файла не ходит в сеть — `telethon.tl.types` собираются руками, `FakeGateway` реализует интерфейс
`hub.connectors.telegram.gateway.Gateway` полностью в памяти.
"""

from __future__ import annotations

import datetime as dt
from collections import deque

from telethon import errors, utils
from telethon.tl import types

from hub.connectors.fake import ConnectorCrash
from hub.connectors.telegram.gateway import ContactsResult, DialogInfo, MediaUploadError, contacts_hash

UTC = dt.UTC
ME_ID = 1


def ts(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=UTC)


def make_user(
    id: int,
    *,
    first_name: str = "User",
    last_name: str | None = None,
    username: str | None = None,
    bot: bool = False,
    contact: bool = False,
    is_self: bool = False,
) -> types.User:
    return types.User(
        id=id,
        is_self=is_self,
        contact=contact,
        bot=bot,
        access_hash=0,
        first_name=first_name,
        last_name=last_name,
        username=username,
    )


def make_chat(id: int, *, title: str = "Group") -> types.Chat:
    return types.Chat(
        id=id, title=title, photo=types.ChatPhotoEmpty(), participants_count=2, date=ts(0), version=1
    )


def make_channel(
    id: int,
    *,
    title: str = "Channel",
    megagroup: bool = False,
    forum: bool = False,
    broadcast: bool = False,
    username: str | None = None,
) -> types.Channel:
    return types.Channel(
        id=id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=ts(0),
        megagroup=megagroup,
        forum=forum,
        broadcast=broadcast,
        access_hash=0,
        username=username,
    )


def user_peer(user: types.User) -> types.PeerUser:
    return types.PeerUser(user.id)


def chat_peer(chat: types.Chat) -> types.PeerChat:
    return types.PeerChat(chat.id)


def channel_peer(channel: types.Channel) -> types.PeerChannel:
    return types.PeerChannel(channel.id)


def peer_of(entity: object) -> object:
    if isinstance(entity, types.User):
        return types.PeerUser(entity.id)
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return types.PeerChat(entity.id)
    if isinstance(entity, types.Channel):
        return types.PeerChannel(entity.id)
    raise TypeError(type(entity))


def make_message(
    id: int,
    peer: object,
    *,
    message: str | None = "hi",
    date_ms: int = 1_700_000_000_000,
    out: bool = False,
    mentioned: bool = False,
    from_id: object | None = None,
    entities: list | None = None,
    media: object | None = None,
    fwd_from: object | None = None,
    reply_to: object | None = None,
    grouped_id: int | None = None,
    edit_date_ms: int | None = None,
    reactions: object | None = None,
    action: object | None = None,
    reply_markup: object | None = None,
    edit_hide: bool | None = None,
) -> types.Message | types.MessageService:
    kwargs = dict(
        id=id,
        peer_id=peer,
        date=ts(date_ms),
        out=out,
        mentioned=mentioned,
        from_id=from_id,
        reply_to=reply_to,
        grouped_id=grouped_id,
        edit_date=ts(edit_date_ms) if edit_date_ms is not None else None,
        reactions=reactions,
    )
    if action is not None:
        return types.MessageService(action=action, **kwargs)
    return types.Message(
        message=message,
        entities=entities,
        media=media,
        fwd_from=fwd_from,
        reply_markup=reply_markup,
        edit_hide=edit_hide,
        **kwargs,
    )


def make_reply(*, top_id: int | None = None, msg_id: int | None = None, forum_topic: bool = False):
    return types.MessageReplyHeader(forum_topic=forum_topic, reply_to_top_id=top_id, reply_to_msg_id=msg_id)


def make_dialog(
    entity: object,
    *,
    top_message: int | None,
    read_inbox_max_id: int = 0,
    read_outbox_max_id: int = 0,
    pinned: bool = False,
    archived: bool = False,
    mute_until: dt.datetime | None = None,
    last_message_date_ms: int | None = 1_700_000_000_000,
) -> DialogInfo:
    return DialogInfo(
        peer=peer_of(entity),
        entity=entity,
        top_message=top_message,
        read_inbox_max_id=read_inbox_max_id,
        pinned=pinned,
        archived=archived,
        mute_until=mute_until,
        last_message_date=ts(last_message_date_ms) if last_message_date_ms is not None else None,
        read_outbox_max_id=read_outbox_max_id,
    )


def make_dialog_filter(
    id: int,
    *,
    title: str = "Folder",
    include: list | None = None,
    exclude: list | None = None,
    pinned: list | None = None,
    contacts: bool = False,
    non_contacts: bool = False,
    groups: bool = False,
    broadcasts: bool = False,
    bots: bool = False,
    exclude_muted: bool = False,
    exclude_read: bool = False,
    exclude_archived: bool = False,
) -> types.DialogFilter:
    return types.DialogFilter(
        id=id,
        title=types.TextWithEntities(text=title, entities=[]),
        pinned_peers=pinned or [],
        include_peers=include or [],
        exclude_peers=exclude or [],
        contacts=contacts,
        non_contacts=non_contacts,
        groups=groups,
        broadcasts=broadcasts,
        bots=bots,
        exclude_muted=exclude_muted,
        exclude_read=exclude_read,
        exclude_archived=exclude_archived,
    )


def make_authorization(
    hash_: int,
    *,
    current: bool = False,
    device_model: str = "iPhone 15",
    platform: str = "iOS",
    country: str = "RU",
    date_created_ms: int = 1_700_000_000_000,
) -> types.Authorization:
    """`account.GetAuthorizationsRequest` -> `Authorization` (раздел «Алерт о новой сессии Telegram», кусок
    1c). `ip`/`region` — заполнены заглушкой: правило логов запрещает коннектору отправлять IP ядру, но само
    поле у TL-объекта Telethon обязательное."""
    return types.Authorization(
        hash=hash_,
        device_model=device_model,
        platform=platform,
        system_version="17.0",
        api_id=1,
        app_name="hub",
        app_version="0.1",
        date_created=ts(date_created_ms),
        date_active=ts(date_created_ms),
        ip="0.0.0.0",
        country=country,
        region="",
        current=current,
    )


def input_peer_of(entity: object) -> object:
    if isinstance(entity, types.User):
        return types.InputPeerUser(entity.id, 0)
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return types.InputPeerChat(entity.id)
    if isinstance(entity, types.Channel):
        return types.InputPeerChannel(entity.id, 0)
    raise TypeError(type(entity))


# Ф42-1: Telegram за один `GetHistoryRequest` отдаёт не больше 100 сообщений, сколько бы ни просили.
SERVER_PAGE_MAX = 100


class FakeGateway:
    """Реализация `Gateway` целиком в памяти — без единого сетевого вызова (жёсткое правило задания)."""

    def __init__(self, *, me: types.User | None = None) -> None:
        self.me = me or make_user(ME_ID, first_name="владелец", is_self=True)
        self.authorized = True
        self.connected = False
        self.dialogs: list[DialogInfo] = []
        # marked_id канала -> список сообщений (любой порядок; get_messages сам сортирует по убыванию id)
        self.history: dict[int, list] = {}
        self.entities: dict[int, object] = {}  # дополнительные сущности (senders и т.п.) для get_messages
        self.filters: list = []
        self.forum_topics: dict[tuple[int, int], types.ForumTopic] = {}
        self.get_messages_calls: list[tuple[int, int | None, int]] = []
        self.get_forum_topics_calls: list[tuple[int, tuple[int, ...] | None]] = []
        self._handler = None
        self.connect_calls = 0
        self.disconnect_calls = 0

        # --- отправка (кусок 1c): «сервер Telegram» — дедуп по random_id, счётчик доставок ------------------
        self._peer_cache: dict[int, object] = {}  # что видит get_input_entity ДО iter_dialogs (кэш сессии)
        self.get_input_entity_calls: list[int] = []
        self.iter_dialogs_calls = 0
        self._random_id_registry: dict[int, str] = {}
        self._next_sent_ext_id = 9000
        self.sent: list[dict] = []  # каждая ФАКТИЧЕСКИ доставленная отправка (без дублей по random_id)
        self.send_calls = 0  # каждый ВЫЗОВ send_message(), включая отказы/дубли
        self._send_faults: deque = deque()
        self.now_ms = 1_700_000_000_000  # дата доставленных сообщений (тест двигает сам, как часы outbox)
        self.authorizations: list = []
        self.get_authorizations_calls = 0
        # Тестовый рычаг для сценария «история не видит на узком поиске, видит на широком после дубликата»
        # (тест 4 куска 1c): `get_messages()` отдаёт пусто, пока запрошенный `limit` меньше этого порога.
        self.get_messages_min_limit_visible = 0
        # --- прочитанность из хаба (Ф8-7): вызовы и очередь отказов (экземпляр исключения — бросить)
        self.read_calls: list[tuple] = []
        self.read_faults: deque = deque()
        # Ф10-3 C1: реакции — вызовы `send_reaction`/`get_reactions`, их отказы и ответы (`Updates`).
        self.reaction_calls: list[tuple] = []
        self.reaction_faults: deque = deque()
        self.send_reaction_result: object | None = None
        self.get_reactions_result: object | None = None
        self.get_reactions_results: deque = deque()  # по одному на вызов, дальше — `get_reactions_result`
        self.get_reactions_faults: deque = deque()
        # Ф44-1c: нажатия кнопок — вызовы `press_button`, очередь отказов и ответ бота.
        self.press_calls: list[tuple] = []
        self.press_faults: deque = deque()
        self.press_answer: object | None = None
        # Ф10-4 B2: отправка файла — вызовы `send_media` (с байтами файла на момент вызова) и очередь отказов:
        # экземпляр исключения — бросить из `SendMediaRequest` (не доставлено); `("upload", exc)` — сбой
        # `upload_file` (`MediaUploadError`); `"network_after"` — доставить, потом `ConnectionError`.
        self.media_calls: list[dict] = []
        self.media_faults: deque = deque()
        # Ответ `send_media` без узнаваемого id (`Updates` без `UpdateMessageID`) — тест поиска в истории.
        self.media_result_without_id = False
        # Ф12-1 B2: `contacts.ResolveUsername` — username (без регистра) -> сущность; вызовы, отказы.
        self.usernames: dict[str, object] = {}
        self.resolve_calls: list[str] = []
        self.resolve_faults: deque = deque()
        # Ф34-3: `contacts.ResolvePhone` — цифры номера -> сущность; вызовы, отказы. Импорта контактов у
        # шлюза нет вовсе: `import_contacts` — ловушка (падает при любом вызове).
        self.phones: dict[str, object] = {}
        self.resolve_phone_calls: list[str] = []
        self.resolve_phone_faults: deque = deque()
        # Ф46-1: `users.GetUsers([InputUserFromMessage])` — (marked чата, id сообщения) -> пользователь;
        # вызовы `(marked чата, id сообщения, id человека)`, очередь отказов. `peer_cache_faults` —
        # исключение, которое `get_input_entity` бросает для этого id (min-пользователь -> `TypeError`).
        self.message_authors: dict[tuple[int, int], object] = {}
        self.user_from_message_calls: list[tuple[int, int, int]] = []
        self.user_from_message_faults: deque = deque()
        self.peer_cache_faults: dict[int, BaseException] = {}
        # Ф11-3b B2: альбом — вызовы `send_album` (с байтами файлов на момент вызова) и очередь отказов:
        # экземпляр исключения — бросить из `SendMultiMediaRequest` (не доставлено); `("upload_file", exc)` /
        # `("upload_media", exc)` — `MediaUploadError` этого этапа; `"network_after"` — доставить, потом
        # `ConnectionError`. `album_result_without_ids` — ответ без `UpdateMessageID`.
        self.album_calls: list[dict] = []
        self.album_faults: deque = deque()
        self.album_result_without_ids = False
        self._next_grouped_id = 77_000
        # Ф14-2 B1/B2: `contacts.GetContacts` — пользователи-контакты, вызовы (`hash`), очередь отказов. Как
        # Telethon: пользователи ответа попадают в кэш сессии (`get_input_entity` их потом знает).
        self.contacts: list = []
        self.contacts_calls: list[int] = []
        self.contacts_faults: deque = deque()
        # Ф14-8 C1: правка и «удалить у всех» — вызовы (`("edit", marked, id, text)` /
        # `("delete", marked, ids)`), очередь отказов (экземпляр исключения — бросить), `delete_pts` —
        # подменить `pts_count` ответа (`None` — сколько реально удалено); перечитывание по id — вызовы и
        # очередь отказов.
        self.msg_op_calls: list[tuple] = []
        self.msg_op_faults: deque = deque()
        self.delete_pts: int | None = None
        self.by_ids_calls: list[tuple[int, list[int]]] = []
        self.by_ids_faults: deque = deque()
        # Ф17-1 C: вступление — вызовы (`("join_channel", marked)` / `("import_invite", hash)` /
        # `("check_invite", hash)` / `("full", marked)`), очередь отказов на вступление/проверку приглашения
        # (экземпляр исключения — бросить), ответы `check_invite` по hash, чат `import_invite` по hash, полная
        # информация каналов (`messages.ChatFull`) и её отказы.
        self.join_calls: list[tuple] = []
        self.join_faults: deque = deque()
        self.invites: dict[str, object] = {}
        self.invite_chats: dict[str, object] = {}
        self.full_channels: dict[int, object] = {}
        self.full_faults: deque = deque()
        # Ф17-2 C: выход — вызовы (`("get_entity", marked)` / `("leave_channel", marked)` /
        # `("leave_chat", chat_id)`), очередь отказов выхода и свежей сущности (экземпляр исключения —
        # бросить), свежие сущности с сервера по marked id (нет — сущность диалога из `entities`).
        self.leave_calls: list[tuple] = []
        self.leave_faults: deque = deque()
        self.entity_faults: deque = deque()
        self.fresh_entities: dict[int, object] = {}
        # Ф29-1 C2/C3: поиск (`contacts.Found` по строке как есть; нет — пустой ответ) и история без
        # вступления — вызовы (`(строка, limit)` / `(marked, offset_id, limit)`) и очереди отказов (экземпляр
        # исключения — бросить).
        self.search_found: dict[str, object] = {}
        self.search_calls: list[tuple[str, int]] = []
        self.search_faults: deque = deque()
        self.history_calls: list[tuple[int, int, int]] = []
        self.history_faults: deque = deque()

    def add_dialog(self, dialog: DialogInfo) -> None:
        self.dialogs.append(dialog)
        marked = utils.get_peer_id(dialog.peer)
        self.entities[marked] = dialog.entity
        self._peer_cache[marked] = input_peer_of(dialog.entity)

    def seed_peer(self, marked_id: int, input_peer: object) -> None:
        """Сущность уже «в кэше сессии» — `get_input_entity` найдёт её сразу, без прохода `iter_dialogs()`."""
        self._peer_cache[marked_id] = input_peer

    def forget_peer(self, marked_id: int) -> None:
        """Симуляция «кэш сессии ещё не знает эту сущность» — тест промаха/прогрева `_resolve_peer()`."""
        self._peer_cache.pop(marked_id, None)

    def delivered_count(self, text: str) -> int:
        return sum(1 for s in self.sent if s["text"] == text)

    def queue_send_faults(self, *faults) -> None:
        """Очередь на `send_message()`: экземпляр исключения — бросить как есть; `"network_before"` —
        `ConnectionError` ДО доставки (сервер не получил); `"network_after"` — доставить, потом
        `ConnectionError` (сервер получил, ответ потерян); `"crash_before_send"`/`"crash_after_delivery"` —
        то же самое, но `ConnectorCrash` (настоящая смерть процесса, `BaseException`, не `Exception`)."""
        self._send_faults.extend(faults)

    def add_message(self, channel_marked_id: int, msg) -> None:
        self.history.setdefault(channel_marked_id, []).append(msg)

    # --- интерфейс Gateway ------------------------------------------------------------------------------

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def is_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> object:
        return self.me

    async def iter_dialogs(self):
        self.iter_dialogs_calls += 1
        for d in self.dialogs:
            # Реальный Telethon наполняет `_mb_entity_cache` из ответа `GetDialogsRequest` как побочный
            # эффект перечисления — здесь то же самое для `_peer_cache` (раздел «Отправка»: «промах ->
            # один проход диалогов для наполнения кэша»).
            self._peer_cache[utils.get_peer_id(d.peer)] = input_peer_of(d.entity)
            yield d

    async def get_messages(self, peer, *, min_id, limit):
        marked = utils.get_peer_id(peer)
        self.get_messages_calls.append((marked, min_id, limit))
        if limit < self.get_messages_min_limit_visible:
            return [], dict(self.entities)
        msgs = sorted(self.history.get(marked, []), key=lambda m: m.id, reverse=True)
        if min_id:
            msgs = [m for m in msgs if m.id > min_id]
        msgs = msgs[: min(limit, SERVER_PAGE_MAX)]  # Ф42-1: как сервер — не больше 100 при любом `limit`
        return list(msgs), dict(self.entities)

    async def get_dialog_filters(self) -> list:
        return list(self.filters)

    async def get_forum_topics(self, peer, *, ids=None):
        marked = utils.get_peer_id(peer)
        self.get_forum_topics_calls.append((marked, tuple(ids) if ids else None))
        if ids:
            return [self.forum_topics[(marked, i)] for i in ids if (marked, i) in self.forum_topics]
        return [t for (m, _tid), t in self.forum_topics.items() if m == marked]

    def subscribe(self, handler) -> None:
        self._handler = handler

    # --- отправка (кусок 1c) -----------------------------------------------------------------------------

    async def get_input_entity(self, marked_id: int) -> object:
        self.get_input_entity_calls.append(marked_id)
        if marked_id in self.peer_cache_faults:
            raise self.peer_cache_faults[marked_id]
        if marked_id in self._peer_cache:
            return self._peer_cache[marked_id]
        raise ValueError(f"FakeGateway: нет сущности {marked_id} в кэше сессии")

    def _alloc_ext_id(self) -> str:
        self._next_sent_ext_id += 1
        return str(self._next_sent_ext_id)

    def _record_delivery(self, marked_id: int, message: str, random_id: int, reply_to: object | None) -> str:
        ext_id = self._alloc_ext_id()
        self._random_id_registry[random_id] = ext_id
        reply_header = None
        if reply_to is not None:
            top_msg_id = getattr(reply_to, "top_msg_id", None)
            reply_header = types.MessageReplyHeader(
                forum_topic=top_msg_id is not None,
                reply_to_top_id=top_msg_id,
                reply_to_msg_id=getattr(reply_to, "reply_to_msg_id", None),
            )
        msg = types.Message(
            id=int(ext_id),
            peer_id=utils.get_peer(marked_id),
            date=ts(self.now_ms),
            out=True,
            message=message,
            reply_to=reply_header,
        )
        self.history.setdefault(marked_id, []).append(msg)
        self.sent.append(
            {
                "marked_id": marked_id,
                "text": message,
                "random_id": random_id,
                "ext_id": ext_id,
                "reply_to": reply_to,
            }
        )
        return ext_id

    async def send_message(
        self, peer: object, *, message: str, random_id: int, reply_to: object | None = None
    ) -> object:
        """«Сервер Telegram»: дедуп по `random_id` (естественное поведение MTProto — НЕ отдельный «фолт»),
        счётчик доставок в `self.sent`. Очередь `_send_faults` моделирует flood/permanent/сетевые ошибки/
        падение процесса — см. `queue_send_faults()`."""
        self.send_calls += 1
        marked_id = utils.get_peer_id(peer)
        fault = self._send_faults.popleft() if self._send_faults else None

        if fault == "crash_before_send":
            raise ConnectorCrash("fake gateway: процесс упал до отправки")
        if fault == "network_before":
            raise ConnectionError("fake gateway: сеть оборвалась до отправки")
        if isinstance(fault, BaseException):
            raise fault

        if random_id in self._random_id_registry:
            raise errors.RandomIdDuplicateError(request=None)

        ext_id = self._record_delivery(marked_id, message, random_id, reply_to)

        if fault == "network_after":
            raise ConnectionError("fake gateway: сеть оборвалась после отправки")
        if fault == "crash_after_delivery":
            raise ConnectorCrash("fake gateway: процесс упал после отправки, до report")

        return types.UpdateShortSentMessage(id=int(ext_id), pts=0, pts_count=0, date=ts(self.now_ms))

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
        """«Сервер Telegram» для файла (Ф10-4): дедуп по `random_id`, доставка — сообщение с медиа
        (фото -> `MessageMediaPhoto`, иначе `MessageMediaDocument`) в историю и `self.sent`."""
        with open(path, "rb") as f:  # noqa: ASYNC230 — двойник в тестах, локальный файл
            data = f.read()
        marked_id = utils.get_peer_id(peer)
        self.media_calls.append(
            {
                "marked_id": marked_id,
                "path": path,
                "data": data,
                "kind": kind,
                "name": name,
                "mime": mime,
                "text": text,
                "random_id": random_id,
                "reply_to": reply_to,
                "force_file": force_file,
                "duration": duration,  # Ф14-4: длительность голосового
            }
        )
        fault = self.media_faults.popleft() if self.media_faults else None
        if isinstance(fault, tuple) and fault[0] == "upload":
            raise MediaUploadError(fault[1])
        if isinstance(fault, BaseException):
            raise fault
        if random_id in self._random_id_registry:
            raise errors.RandomIdDuplicateError(request=None)
        ext_id = self._alloc_ext_id()
        self._random_id_registry[random_id] = ext_id
        media = (
            types.MessageMediaPhoto(photo=types.PhotoEmpty(id=int(ext_id)))
            if kind == "photo"
            else types.MessageMediaDocument(document=types.DocumentEmpty(id=int(ext_id)))
        )
        msg = types.Message(
            id=int(ext_id),
            peer_id=utils.get_peer(marked_id),
            date=ts(self.now_ms),
            out=True,
            message=text,
            media=media,
        )
        self.history.setdefault(marked_id, []).append(msg)
        self.sent.append(
            {"marked_id": marked_id, "text": text, "random_id": random_id, "ext_id": ext_id, "media": kind}
        )
        if fault == "network_after":
            raise ConnectionError("fake gateway: сеть оборвалась после отправки файла")
        updates = [] if self.media_result_without_id else [
            types.UpdateMessageID(id=int(ext_id), random_id=random_id)
        ]
        return types.Updates(updates=updates, users=[], chats=[], date=ts(self.now_ms), seq=0)

    async def send_album(
        self,
        peer: object,
        *,
        items: list,
        text: str,
        random_ids: list[int],
        reply_to: object | None = None,
    ) -> object:
        """«Сервер Telegram» для альбома (Ф11-3b): дедуп по `random_id` элемента 0, доставка — N сообщений с
        медиа и общим `grouped_id` (подпись — у первого) в историю и `self.sent`; ответ — `Updates` с
        `UpdateMessageID` на каждый `random_id` и `UpdateNewMessage` каждого сообщения."""
        datas = []
        for item in items:
            with open(item.path, "rb") as f:  # noqa: ASYNC230 — двойник в тестах, локальный файл
                datas.append(f.read())
        marked_id = utils.get_peer_id(peer)
        self.album_calls.append(
            {
                "marked_id": marked_id,
                "items": [
                    {"kind": i.kind, "name": i.name, "mime": i.mime, "force_file": i.force_file, "data": d}
                    for i, d in zip(items, datas, strict=True)
                ],
                "text": text,
                "random_ids": list(random_ids),
                "reply_to": reply_to,
            }
        )
        fault = self.album_faults.popleft() if self.album_faults else None
        if isinstance(fault, tuple) and fault[0] in ("upload_file", "upload_media"):
            raise MediaUploadError(fault[1], stage=fault[0])
        if isinstance(fault, BaseException):
            raise fault
        if random_ids[0] in self._random_id_registry:
            raise errors.RandomIdDuplicateError(request=None)
        self._next_grouped_id += 1
        grouped_id = self._next_grouped_id
        updates: list = []
        new_messages: list = []
        for k, (item, random_id) in enumerate(zip(items, random_ids, strict=True)):
            ext_id = self._alloc_ext_id()
            self._random_id_registry[random_id] = ext_id
            media = (
                types.MessageMediaPhoto(photo=types.PhotoEmpty(id=int(ext_id)))
                if item.kind == "photo"
                else types.MessageMediaDocument(document=types.DocumentEmpty(id=int(ext_id)))
            )
            msg = types.Message(
                id=int(ext_id),
                peer_id=utils.get_peer(marked_id),
                date=ts(self.now_ms),
                out=True,
                message=text if k == 0 else "",
                media=media,
                grouped_id=grouped_id,
            )
            self.history.setdefault(marked_id, []).append(msg)
            self.sent.append(
                {
                    "marked_id": marked_id,
                    "text": msg.message,
                    "random_id": random_id,
                    "ext_id": ext_id,
                    "media": item.kind,
                    "grouped_id": grouped_id,
                }
            )
            updates.append(types.UpdateMessageID(id=int(ext_id), random_id=random_id))
            new_messages.append(types.UpdateNewMessage(message=msg, pts=0, pts_count=0))
        if fault == "network_after":
            raise ConnectionError("fake gateway: сеть оборвалась после отправки альбома")
        if self.album_result_without_ids:
            updates = []
        return types.Updates(updates=updates + new_messages, users=[], chats=[], date=ts(self.now_ms), seq=0)

    async def get_authorizations(self) -> list:
        self.get_authorizations_calls += 1
        return list(self.authorizations)

    async def read_history(self, peer, max_id: int) -> None:
        self.read_calls.append(("history", utils.get_peer_id(peer), max_id))
        if self.read_faults:
            raise self.read_faults.popleft()

    async def read_discussion(self, peer, topic_id: int, max_id: int) -> None:
        self.read_calls.append(("discussion", utils.get_peer_id(peer), topic_id, max_id))
        if self.read_faults:
            raise self.read_faults.popleft()

    async def send_reaction(self, peer, msg_id: int, emojis: list[str]) -> object:
        self.reaction_calls.append(("send", utils.get_peer_id(peer), msg_id, list(emojis)))
        if self.reaction_faults:
            raise self.reaction_faults.popleft()
        return self.send_reaction_result if self.send_reaction_result is not None else types.Updates(
            updates=[], users=[], chats=[], date=None, seq=0
        )

    async def press_button(self, peer, msg_id: int, data: bytes, *, timeout_s: float) -> object:
        self.press_calls.append((utils.get_peer_id(peer), msg_id, data, timeout_s))
        if self.press_faults:
            raise self.press_faults.popleft()
        if self.press_answer is not None:
            return self.press_answer
        return types.messages.BotCallbackAnswer(cache_time=0)

    async def get_reactions(self, peer, msg_id: int) -> object:
        self.reaction_calls.append(("get", utils.get_peer_id(peer), msg_id))
        if self.get_reactions_faults:
            raise self.get_reactions_faults.popleft()
        if self.get_reactions_results:
            return self.get_reactions_results.popleft()
        return self.get_reactions_result if self.get_reactions_result is not None else types.Updates(
            updates=[], users=[], chats=[], date=None, seq=0
        )

    async def get_reactions_many(self, peer, ids) -> object:
        """Ф18-2: пачка id одним запросом; ответы и сбои — те же очереди, что у `get_reactions`."""
        self.reaction_calls.append(("get_many", utils.get_peer_id(peer), list(ids)))
        if self.get_reactions_faults:
            raise self.get_reactions_faults.popleft()
        if self.get_reactions_results:
            return self.get_reactions_results.popleft()
        return self.get_reactions_result if self.get_reactions_result is not None else types.Updates(
            updates=[], users=[], chats=[], date=None, seq=0
        )

    # --- правка и «удалить у всех» (Ф14-8 C1) ---------------------------------------------------------

    async def get_messages_by_ids(self, peer, ids):
        marked = utils.get_peer_id(peer)
        self.by_ids_calls.append((marked, list(ids)))
        if self.by_ids_faults:
            raise self.by_ids_faults.popleft()
        by_id = {m.id: m for m in self.history.get(marked, [])}
        return [by_id.get(i) for i in ids]

    async def edit_message(self, peer, msg_id: int, text: str) -> object:
        """Как сервер: текст меняется, `edit_date` — `now_ms`; тот же текст — `MessageNotModifiedError`."""
        marked = utils.get_peer_id(peer)
        self.msg_op_calls.append(("edit", marked, msg_id, text))
        if self.msg_op_faults:
            raise self.msg_op_faults.popleft()
        msgs = self.history.get(marked, [])
        msg = next((m for m in msgs if m.id == msg_id), None)
        if msg is None:
            raise errors.MessageIdInvalidError(request=None)
        if msg.message == text and not msg.entities:
            raise errors.MessageNotModifiedError(request=None)
        msg.message = text
        msg.entities = None
        msg.edit_date = ts(self.now_ms)
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)

    async def delete_messages(self, peer, ids: list[int]) -> int:
        marked = utils.get_peer_id(peer)
        self.msg_op_calls.append(("delete", marked, list(ids)))
        if self.msg_op_faults:
            raise self.msg_op_faults.popleft()
        msgs = self.history.get(marked, [])
        if self.delete_pts is not None:
            return self.delete_pts
        before = len(msgs)
        self.history[marked] = [m for m in msgs if m.id not in ids]
        return before - len(self.history[marked])

    # --- черновик новому адресату (Ф12-1 B2) -------------------------------------------------------------

    def add_username(self, username: str, entity: object) -> None:
        self.usernames[username.casefold()] = entity

    async def resolve_username(self, username: str) -> object:
        """Как Telethon: найденная сущность попадает в кэш сессии (`get_input_entity` её потом знает)."""
        self.resolve_calls.append(username)
        if self.resolve_faults:
            raise self.resolve_faults.popleft()
        entity = self.usernames.get(username.casefold())
        if entity is None:
            raise errors.UsernameNotOccupiedError(request=None)
        marked = utils.get_peer_id(entity)
        self.entities[marked] = entity
        self._peer_cache[marked] = input_peer_of(entity)
        return entity

    # --- первое сообщение по номеру телефона (Ф34-3) -----------------------------------------------------

    def add_phone(self, digits: str, entity: object) -> None:
        self.phones[digits] = entity

    async def resolve_phone(self, phone: str) -> object:
        """Как Telethon: найденная сущность попадает в кэш сессии; номера нет — `PhoneNotOccupiedError`."""
        self.resolve_phone_calls.append(phone)
        if self.resolve_phone_faults:
            raise self.resolve_phone_faults.popleft()
        entity = self.phones.get(phone)
        if entity is None:
            raise errors.PhoneNotOccupiedError(request=None)
        marked = utils.get_peer_id(entity)
        self.entities[marked] = entity
        self._peer_cache[marked] = input_peer_of(entity)
        return entity

    # --- автор сообщения группы (Ф46-1) ------------------------------------------------------------------

    def add_message_author(self, chat_marked: int, msg_id: int, user: object) -> None:
        self.message_authors[(chat_marked, msg_id)] = user

    async def get_user_from_message(self, chat_peer: object, msg_id: int, user_id: int) -> object | None:
        """Как сервер: сообщения нет — `MsgIdInvalidError`; в кэш сессии ничего не кладётся."""
        self.user_from_message_calls.append((utils.get_peer_id(chat_peer), msg_id, user_id))
        if self.user_from_message_faults:
            fault = self.user_from_message_faults.popleft()
            if isinstance(fault, BaseException):
                raise fault
            return fault  # заранее заданный ответ (в том числе `None` — пустой)
        user = self.message_authors.get((utils.get_peer_id(chat_peer), msg_id))
        if user is None:
            raise errors.MsgIdInvalidError(request=None)
        return user

    async def import_contacts(self, *args, **kwargs) -> object:
        raise AssertionError("contacts.ImportContacts вызывать нельзя ни на одном пути (Ф34-3)")

    # --- контакты (Ф14-2 B1/B2) --------------------------------------------------------------------------

    def contacts_hash_now(self) -> int:
        return contacts_hash(len(self.contacts), [u.id for u in self.contacts])

    async def get_contacts(self, hash: int) -> ContactsResult | None:
        self.contacts_calls.append(hash)
        if self.contacts_faults:
            raise self.contacts_faults.popleft()
        current = self.contacts_hash_now()
        if hash and hash == current:
            return None
        for user in self.contacts:
            marked = utils.get_peer_id(user)
            self.entities[marked] = user
            self._peer_cache[marked] = input_peer_of(user)
        return ContactsResult(hash=current, users=list(self.contacts))

    # --- вступление в группы и каналы (Ф17-1 C) ----------------------------------------------------------

    def _updates_with(self, chat: object | None) -> types.Updates:
        chats = [chat] if chat is not None else []
        return types.Updates(updates=[], users=[], chats=chats, date=ts(0), seq=0)

    async def join_channel(self, input_channel) -> object:
        marked = utils.get_peer_id(types.PeerChannel(input_channel.channel_id))
        self.join_calls.append(("join_channel", marked))
        if self.join_faults:
            raise self.join_faults.popleft()
        return self._updates_with(self.entities.get(marked))

    async def import_invite(self, invite_hash: str) -> object:
        self.join_calls.append(("import_invite", invite_hash))
        if self.join_faults:
            raise self.join_faults.popleft()
        chat = self.invite_chats.get(invite_hash)
        if chat is not None:
            self.entities[utils.get_peer_id(chat)] = chat
        return self._updates_with(chat)

    async def check_invite(self, invite_hash: str) -> object:
        self.join_calls.append(("check_invite", invite_hash))
        if self.join_faults:
            raise self.join_faults.popleft()
        invite = self.invites.get(invite_hash)
        if invite is None:
            raise errors.InviteHashInvalidError(request=None)
        return invite

    async def get_full_channel(self, input_channel) -> object:
        marked = utils.get_peer_id(types.PeerChannel(input_channel.channel_id))
        self.join_calls.append(("full", marked))
        if self.full_faults:
            raise self.full_faults.popleft()
        full = self.full_channels.get(marked)
        if full is None:
            raise errors.ChannelPrivateError(request=None)
        return full

    # --- выход из групп и каналов (Ф17-2 C) --------------------------------------------------------------

    async def get_entity(self, input_peer) -> object:
        marked = utils.get_peer_id(input_peer)
        self.leave_calls.append(("get_entity", marked))
        if self.entity_faults:
            raise self.entity_faults.popleft()
        return self.fresh_entities.get(marked, self.entities.get(marked))

    async def leave_channel(self, input_channel) -> object:
        marked = utils.get_peer_id(types.PeerChannel(input_channel.channel_id))
        self.leave_calls.append(("leave_channel", marked))
        if self.leave_faults:
            raise self.leave_faults.popleft()
        return self._updates_with(None)

    async def leave_chat(self, chat_id: int) -> object:
        self.leave_calls.append(("leave_chat", chat_id))
        if self.leave_faults:
            raise self.leave_faults.popleft()
        return self._updates_with(None)

    # --- поиск и чтение без вступления (Ф29-1 C2/C3) -------------------------------------------------------

    async def search_chats(self, query: str, *, limit: int) -> object:
        self.search_calls.append((query, limit))
        if self.search_faults:
            raise self.search_faults.popleft()
        found = self.search_found.get(query)
        if found is None:
            return types.contacts.Found(my_results=[], results=[], chats=[], users=[])
        return found

    async def get_history(self, peer, *, offset_id: int, limit: int):
        marked = utils.get_peer_id(peer)
        self.history_calls.append((marked, offset_id, limit))
        if self.history_faults:
            raise self.history_faults.popleft()
        msgs = sorted(self.history.get(marked, []), key=lambda m: m.id, reverse=True)
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        return list(msgs[: min(limit, SERVER_PAGE_MAX)]), dict(self.entities)

    def push(self, event) -> None:
        assert self._handler is not None, "subscribe() ещё не вызывался (start_source не выполнялся)"
        self._handler(event)


def make_forum_topic(chat_id: int, topic_id: int, *, title: str = "Topic", top_message: int | None = None):
    return types.ForumTopic(
        id=topic_id,
        date=ts(0),
        peer=types.PeerChannel(chat_id),
        title=title,
        icon_color=0,
        top_message=top_message or topic_id,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(ME_ID),
        notify_settings=types.PeerNotifySettings(),
    )
