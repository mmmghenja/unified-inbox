"""TL -> события ядра: ЧИСТЫЕ функции, никаких обращений к клиенту/сети. Спецификация:
`docs/architecture.md`.

Вход — сырые поля TL (`telethon.tl.types`): `peer_id`, `from_id`, `message`, `entities`, `reply_to`, `media`,
`fwd_from`, `action`, `reactions`, `grouped_id`, `mentioned`, `out`, `date`, `edit_date`. Ничего из этого
модуля не ходит в сеть и не трогает `TelegramClient` — только объекты, уже полученные коннектором/гейтвеем.

Результат детерминирован: одно и то же сообщение -> побайтно тот же dict (раздел «Устройство пакета»,
`gateway.py`) — иначе сверка каждые 10 минут шумила бы правками в ленту (ядро no-op'ит только идентичное).
Поэтому здесь НЕТ `datetime.now()`, случайности и сортировки словарей с недетерминированным порядком (кроме
там, где сам Python гарантирует порядок вставки — обычные `dict`/`list`).

`telethon.tl.types.Message`/`MessageService` после импорта пакета `telethon` патчатся в подклассы
`telethon.tl.custom.message.Message` (`telethon/tl/patched/__init__.py`) — то есть `isinstance(msg,
types.MessageService)` истинно и для «сырых» объектов из ответа API, и для объектов, собранных тестами
напрямую через `telethon.tl.types.MessageService(...)`: это один и тот же класс, не обёртка.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from telethon import utils
from telethon.tl import types

from .. import refs

# --- идентификаторы, заголовки, kind диалога -------------------------------------------------------------

FAVORITES_TITLE = "Избранное"
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"
# Ф20-1 П5: служебный аккаунт Telegram (коды входа, уведомления) — отправитель-бот.
TELEGRAM_SERVICE_ID = "777000"


def peer_ext_id(peer: object) -> str:
    """Помеченный id (план, раздел 2): пользователь > 0, чат < 0, канал -100… — `utils.get_peer_id`."""
    return str(utils.get_peer_id(peer))


def dialog_kind(entity: object, *, is_bot: bool = False) -> str:
    """`kind` ВЕРХНЕГО диалога (не топика): пользователь/бот -> dm; Chat и мегагруппа -> group; форум —
    тоже group (топики — отдельные дочерние `topic`-каналы, см. `topic_ref`); broadcast -> channel."""
    del is_bot  # пользователь остаётся "dm" независимо от бота (лички с ботами — тоже личка)
    if isinstance(entity, types.User):
        return "dm"
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return "group"
    if isinstance(entity, types.Channel):
        return "group" if entity.megagroup else "channel"
    return "dm"


def dialog_own_id_space(entity: object) -> bool:
    """`own_id_space=True` только у `PeerChannel` (канал/супергруппа/форум) — план, раздел
    «Идентификаторы»."""
    return isinstance(entity, types.Channel)


def display_title(entity: object, *, me_id: int | None = None) -> str:
    """Имя+фамилия для пользователя (или «Избранное» для себя), `title` для чата/канала."""
    if isinstance(entity, types.User):
        if me_id is not None and entity.id == me_id:
            return FAVORITES_TITLE
        parts = [p for p in (entity.first_name, entity.last_name) if p]
        if parts:
            return " ".join(parts)
        if entity.username:
            return f"@{entity.username}"
        return str(entity.id)
    title = getattr(entity, "title", None)
    if title:
        return title
    return str(getattr(entity, "id", ""))


def dialog_channel_ref(
    peer: object, entity: object, *, me_id: int, attrs: dict | None = None
) -> dict[str, Any]:
    """Payload `ChannelRef` (dict, JSON-совместимый) для ВЕРХНЕГО диалога."""
    return {
        "ext_id": peer_ext_id(peer),
        "kind": dialog_kind(entity),
        "title": display_title(entity, me_id=me_id),
        "own_id_space": dialog_own_id_space(entity),
        "attrs": attrs,
    }


def topic_channel_ref(chat_ext_id: str, topic_id: int, title: str) -> dict[str, Any]:
    """Топик форума (план, раздел «Идентификаторы»): `ext_id = "<chat>:<topic_id>"`, `parent_ext_id = <chat>`,
    `own_id_space=True` (пространство id топика — то же, что у форума, помеченного `PeerChannel`)."""
    return {
        "ext_id": f"{chat_ext_id}:{topic_id}",
        "kind": "topic",
        "title": title,
        "parent_ext_id": chat_ext_id,
        "own_id_space": True,
    }


# --- топик сообщения (чистое вычисление из reply_to, без сети) --------------------------------------------


def message_topic_id(msg: object) -> int | None:
    """`None` — сообщение не в форуме (обычный чат/группа). Иначе — id корня темы: `reply_to_top_id or
    reply_to_msg_id or 1` (General, план, раздел «Идентификаторы» куска 1b)."""
    reply = getattr(msg, "reply_to", None)
    if not isinstance(reply, types.MessageReplyHeader) or not reply.forum_topic:
        return None
    return reply.reply_to_top_id or reply.reply_to_msg_id or GENERAL_TOPIC_ID


def message_reply_to_ext_id(
    msg: object, *, topic_id: int | None, chat_peer: object | None = None
) -> str | None:
    """Настоящий ответ (не корень темы): `reply_to_msg_id`, если он не совпадает с `topic_id` (в теме
    Telegram подставляет `reply_to_msg_id = reply_to_top_id`, когда это просто пост в тему, а не ответ).

    Мелочь из отчёта исполнителя (round 2): `reply.reply_to_peer_id` — ответ на сообщение в ДРУГОМ чате
    (комментарий из канала в чат обсуждений и т.п.). Наше `reply_to_ext_id` живёт в пространстве id ТЕКУЩЕГО
    канала — id из чужого чата там может случайно совпасть с посторонним сообщением. `chat_peer` — peer
    текущего канала (`None` — сравнение не делается, вызывающий не передал)."""
    reply = getattr(msg, "reply_to", None)
    if not isinstance(reply, types.MessageReplyHeader):
        return None
    reply_peer = getattr(reply, "reply_to_peer_id", None)
    if reply_peer is not None and chat_peer is not None:
        try:
            if utils.get_peer_id(reply_peer) != utils.get_peer_id(chat_peer):
                return None
        except TypeError:
            pass
    msg_id = reply.reply_to_msg_id
    if msg_id is None:
        return None
    if topic_id is not None and msg_id == topic_id:
        return None
    return str(msg_id)


# --- сущности текста (UTF-16, offset/length как у Telegram, БЕЗ пересчёта) --------------------------------

_ENTITY_TYPE_MAP: dict[type, str] = {
    types.MessageEntityBold: "bold",
    types.MessageEntityItalic: "italic",
    types.MessageEntityCode: "code",
    types.MessageEntityPre: "pre",
}


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Срез ПО UTF-16 code units (единицы Telegram), а не по индексам Python-строки (кодовые точки) —
    расходятся на эмодзи вне BMP (суррогатная пара = 1 код. точка, но 2 code unit)."""
    encoded = text.encode("utf-16-le")
    chunk = encoded[offset * 2 : (offset + length) * 2]
    return chunk.decode("utf-16-le")


def convert_entities(entities: list | None, text: str | None) -> list[dict]:
    """`bold|italic|code|pre|link|mention`; неизвестные типы отбрасываются (раздел «Идентификаторы»)."""
    if not entities or not text:
        return []
    out: list[dict] = []
    for e in entities:
        offset, length = getattr(e, "offset", None), getattr(e, "length", None)
        if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
            continue
        if isinstance(e, types.MessageEntityUrl):
            out.append(
                {
                    "type": "link",
                    "offset": offset,
                    "length": length,
                    "url": _utf16_slice(text, offset, length),
                }
            )
        elif isinstance(e, types.MessageEntityTextUrl):
            out.append({"type": "link", "offset": offset, "length": length, "url": e.url})
        elif isinstance(e, types.MessageEntityMention | types.MessageEntityMentionName):
            out.append({"type": "mention", "offset": offset, "length": length})
        elif isinstance(e, types.InputMessageEntityMentionName):
            out.append({"type": "mention", "offset": offset, "length": length})
        elif type(e) in _ENTITY_TYPE_MAP:
            out.append({"type": _ENTITY_TYPE_MAP[type(e)], "offset": offset, "length": length})
        # прочие типы (hashtag, bot_command, phone, spoiler, custom_emoji, …) — вне формата UI, отбрасываются
    return out


def entity_link_urls(entities: list[dict]) -> list[str]:
    return [e["url"] for e in entities if e["type"] == "link" and "url" in e]


# --- вложения ------------------------------------------------------------------------------------------

_DOC_KIND_FLAGS: tuple[tuple[type, str], ...] = (
    (types.DocumentAttributeSticker, "sticker"),
    (types.DocumentAttributeAnimated, "gif"),
    (types.DocumentAttributeVideo, "video"),
)

# Анимированный стикер `.tgs` (lottie в gzip). Ф8-5 C1 делал его «документом»; теперь это `sticker` с
# `sticker_format: animated` — UI рисует lottie, сессиям выдача пишет «стикер 😀 (анимированный)».
_TGS_MIME = "application/x-tgsticker"
_STICKER_FORMATS = {_TGS_MIME: "animated", "video/webm": "video", "image/webp": "static"}


def _set_dims(item: dict, w: object, h: object) -> None:
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        item["width"] = w
        item["height"] = h


def _document_attachment(document: object) -> dict:
    """Ф8-5 C1: `mime`, `size`, `name`, `width/height` и `duration_s` из атрибутов видео/аудио/картинки;
    `src_id` = `document.id` (П10 — идентичность файла у источника)."""
    if not isinstance(document, types.Document):
        return {"type": "document"}
    kind = "document"
    name: str | None = None
    dims: tuple[object, object] | None = None
    duration: object = None
    round_message = False
    sticker_alt: str | None = None
    is_sticker = False
    for attr in document.attributes:
        matched = next((label for cls, label in _DOC_KIND_FLAGS if isinstance(attr, cls)), None)
        if matched is not None:
            kind = matched
            if isinstance(attr, types.DocumentAttributeSticker):
                is_sticker = True
                alt = getattr(attr, "alt", None)
                sticker_alt = alt if isinstance(alt, str) and alt else None
            if isinstance(attr, types.DocumentAttributeVideo):
                dims = (attr.w, attr.h)
                duration = attr.duration
                # Ф14-6 D: видео-кружок — `round: true`, вид остаётся `video` (ядро расшифровывает кружки).
                round_message = getattr(attr, "round_message", None) is True
        elif isinstance(attr, types.DocumentAttributeAudio):
            kind = "voice" if attr.voice else "audio"
            duration = attr.duration
        elif isinstance(attr, types.DocumentAttributeFilename):
            name = attr.file_name
        elif isinstance(attr, types.DocumentAttributeImageSize) and dims is None:
            dims = (attr.w, attr.h)
    if is_sticker:
        kind = "sticker"  # видео-стикер несёт ещё и атрибут видео — вид остаётся «стикер»
    item: dict[str, Any] = {"type": kind, "size": document.size, "mime": document.mime_type}
    if kind == "sticker":
        item["sticker_format"] = _STICKER_FORMATS.get(str(document.mime_type), "static")
        if sticker_alt is not None:
            item["emoji"] = sticker_alt[:16]
    if name is not None:
        item["name"] = name
    if dims is not None:
        _set_dims(item, *dims)
    if isinstance(duration, int | float) and duration >= 0:
        item["duration_s"] = int(duration)
    if round_message and kind == "video":
        item["round"] = True
    item["src_id"] = str(document.id)
    return item


def _photo_attachment(photo: object) -> dict:
    """Ф8-5 C1: `width/height` и `size` самого большого размера (его же скачает `download_media`), `src_id` =
    `photo.id`. Уменьшенные копии без размеров (`PhotoStrippedSize`, `PhotoPathSize`) не учитываются."""
    item: dict[str, Any] = {"type": "photo"}
    if not isinstance(photo, types.Photo):
        return item
    best: tuple[int, int, int | None] | None = None
    for size in photo.sizes:
        w, h = getattr(size, "w", None), getattr(size, "h", None)
        if not isinstance(w, int) or not isinstance(h, int):
            continue
        if isinstance(size, types.PhotoSize):
            nbytes: int | None = size.size
        elif isinstance(size, types.PhotoSizeProgressive):
            nbytes = max(size.sizes) if size.sizes else None
        elif isinstance(size, types.PhotoCachedSize):
            nbytes = len(size.bytes)
        else:
            nbytes = None
        if best is None or w * h > best[0] * best[1]:
            best = (w, h, nbytes)
    if best is not None:
        _set_dims(item, best[0], best[1])
        if best[2] is not None:
            item["size"] = best[2]
    item["src_id"] = str(photo.id)
    return item


def convert_attachments(media: object | None) -> list[dict] | None:
    """`None` — нет медиа ИЛИ это превью ссылки (`MessageMediaWebPage`, план 0: медиа не тянем) — тогда
    `kind` сообщения остаётся `text`, не `file` (раздел «Идентификаторы»).

    Ф8-5 П13: одноразовое медиа (`ttl_seconds`) помечено `ttl: true` — ядро строку `media` не заводит, UI
    пишет «одноразовое — смотри в Telegram»."""
    if media is None or isinstance(media, types.MessageMediaEmpty | types.MessageMediaWebPage):
        return None
    if isinstance(media, types.MessageMediaPhoto):
        item = _photo_attachment(media.photo)
    elif isinstance(media, types.MessageMediaGeo | types.MessageMediaGeoLive | types.MessageMediaVenue):
        return [{"type": "location"}]
    elif isinstance(media, types.MessageMediaContact):
        return [{"type": "contact"}]
    elif isinstance(media, types.MessageMediaPoll):
        return [{"type": "poll"}]
    elif isinstance(media, types.MessageMediaDocument):
        item = _document_attachment(media.document)
    else:
        return [{"type": "other"}]
    if getattr(media, "ttl_seconds", None):
        item["ttl"] = True
    return [item]


# --- отправитель, forward, реакции ------------------------------------------------------------------------


def _entity_handle(entity: object) -> str | None:
    username = getattr(entity, "username", None)
    return f"@{username}" if username else None


def effective_is_me(msg: object, me_id: int) -> bool:
    """`out` ИЛИ (`peer_id` — я сам И не форвард) — находка F10 (round 2): именно так Telethon сам решает в
    `custom.Message._finish_init` («Make messages sent to ourselves outgoing unless they're forwarded») —
    ЖИВОЙ путь (апдейты идут через `_finish_init`) правит `out` для «Избранного» (личка с самим собой), а
    сырые объекты из `GetHistoryRequest` этот шаг не проходят вовсе. Без единой формулы одно и то же
    сообщение «Избранного» давало бы `is_me=False` из истории и `is_me=True` из живого апдейта — разъезжались
    бы сверка и live. Форвард в «Избранное» остаётся `is_me=False`, если `out` тоже `False` (переслали, не
    написали сами) — как у Telethon."""
    if bool(getattr(msg, "out", False)):
        return True
    peer_id = getattr(msg, "peer_id", None)
    if (
        isinstance(peer_id, types.PeerUser)
        and peer_id.user_id == me_id
        and not getattr(msg, "fwd_from", None)
    ):
        return True
    return False


def sender_ref(msg: object, *, entities: dict[int, object], me_id: int, channel_kind: str) -> dict | None:
    """Помеченный `from_id`; в личке без `from_id` — собеседник или я по `effective_is_me()` (раздел
    «Идентификаторы», формула — находка F10); пост канала без `from_id` — сам канал."""
    from_id = getattr(msg, "from_id", None)
    if from_id is not None:
        ext_id = peer_ext_id(from_id)
    elif channel_kind == "dm":
        peer_id = getattr(msg, "peer_id", None)
        ext_id = (
            str(me_id)
            if effective_is_me(msg, me_id)
            else (peer_ext_id(peer_id) if peer_id is not None else str(me_id))
        )
    elif channel_kind == "channel" and getattr(msg, "peer_id", None) is not None:
        # Пост канала (broadcast) без `from_id` — от имени самого канала, как в автопересылке поста в группу
        # обсуждения (Ф17-3). Контакт канала — не человек (`people._not_person`: помеченный id с «-»).
        ext_id = peer_ext_id(msg.peer_id)
    else:
        return None

    entity = entities.get(int(ext_id))
    is_me = int(ext_id) == me_id
    ref = {
        "ext_id": ext_id,
        "name": display_title(entity, me_id=me_id) if entity is not None else None,
        "handle": _entity_handle(entity) if entity is not None else None,
        "is_me": is_me,
    }
    # Ф20-1 П5: бот — сущность `User` с `bot=True` (сущность бывает `None`/`Channel`) или служебный 777000.
    # Ключ — только когда истинно: прочие события побайтно те же.
    if not is_me and (getattr(entity, "bot", False) is True or ext_id == TELEGRAM_SERVICE_ID):
        ref["is_bot"] = True
    return ref


def _is_channel_autoforward(fwd: object, from_ext_id: str | None, msg_from_id: object | None) -> bool:
    """Ф17-3 решение 1: автопересылка поста канала в группу обсуждения: `from_id` сообщения =
    `fwd_from.from_id` = `saved_from_peer` (канал) и `saved_from_msg_id == channel_post`. Ручная пересылка
    человеком — нет."""
    post = getattr(fwd, "channel_post", None)
    saved_peer = getattr(fwd, "saved_from_peer", None)
    saved_msg = getattr(fwd, "saved_from_msg_id", None)
    if from_ext_id is None or msg_from_id is None or saved_peer is None or not post:
        return False
    if not isinstance(post, int) or saved_msg != post:
        return False
    return peer_ext_id(msg_from_id) == from_ext_id and peer_ext_id(saved_peer) == from_ext_id


def forward_ref(
    fwd: object | None, msg_from_id: object | None = None, entities: dict | None = None
) -> dict | None:
    if fwd is None:
        return None
    from_ext_id = peer_ext_id(fwd.from_id) if fwd.from_id is not None else None
    out = {
        "from_name": fwd.from_name,
        "from_ext_id": from_ext_id,
        "ts": _ts_ms(fwd.date),
    }
    if _is_channel_autoforward(fwd, from_ext_id, msg_from_id):
        out["channel_post"] = fwd.channel_post
        entity = (entities or {}).get(int(from_ext_id))
        if not out["from_name"] and entity is not None:
            out["from_name"] = display_title(entity, me_id=None)
        out["auto"] = True
    return out


REACTION_BY_MAX = 100  # Ф23-2: сколько имён поставивших хранится в элементе реакции


def _same_reaction(a: object, b: object) -> bool:
    if isinstance(a, types.ReactionEmoji) and isinstance(b, types.ReactionEmoji):
        return a.emoticon == b.emoticon
    if isinstance(a, types.ReactionCustomEmoji) and isinstance(b, types.ReactionCustomEmoji):
        return a.document_id == b.document_id
    return False


def _reaction_names(
    recent: list, reaction: object, entities: dict | None, *, me_id: int | None
) -> tuple[list[str], int]:
    """Имена поставивших эту реакцию из `recent_reactions` (чужие; своя — по `my`/`me_id`) и число без имени
    (сущности нет в кэше). Порядок — как у Telegram."""
    names: list[str] = []
    unnamed = 0
    for rec in recent:
        if not _same_reaction(getattr(rec, "reaction", None), reaction):
            continue
        if getattr(rec, "my", False):
            continue
        try:
            marked = utils.get_peer_id(rec.peer_id)
        except TypeError, ValueError, AttributeError:
            unnamed += 1
            continue
        if me_id is not None and marked == me_id:
            continue
        entity = (entities or {}).get(marked)
        if entity is None:
            unnamed += 1
        else:
            names.append(display_title(entity, me_id=None))
    return names, unnamed


def reactions_list(
    reactions: object | None,
    *,
    entities: dict | None = None,
    me_id: int | None = None,
    dm_name: str | None = None,
) -> list[dict] | None:
    """Агрегат `[{"emoji"|"custom": …, "count", "me"}]` (раздел «Идентификаторы»); `None` — нет объекта
    реакций вовсе ИЛИ `reactions.min=True` — Telegram прислал урезанный набор (без признака "моя реакция",
    мелочь из отчёта исполнителя round 2): `chosen_order`/`me` в этом случае недостоверны, шлём `None`,
    чтобы `_apply_reaction`/no-op в ingest различали «нечего слать».

    Ф23-2: необязательные `by` (имена из `recent_reactions` по сущностям `entities`, до `REACTION_BY_MAX`;
    своя реакция не пишется) и `by_more` (поставившие без имени и сверх лимита). Нет `recent_reactions` у
    реакции: в личке (`dm_name` — собеседник) единственный чужой — собеседник; иначе полей нет. Запросов в
    Telegram нет."""
    if reactions is None or getattr(reactions, "min", False):
        return None
    recent = list(getattr(reactions, "recent_reactions", None) or [])
    out: list[dict] = []
    for item in reactions.results:
        reaction = item.reaction
        entry: dict[str, Any]
        if isinstance(reaction, types.ReactionEmoji):
            entry = {"emoji": reaction.emoticon}
        elif isinstance(reaction, types.ReactionCustomEmoji):
            entry = {"custom": str(reaction.document_id)}
        else:
            continue
        entry["count"] = item.count
        mine = item.chosen_order is not None
        entry["me"] = mine
        others = max(item.count - (1 if mine else 0), 0)
        names, unnamed = _reaction_names(recent, reaction, entities, me_id=me_id) if recent else ([], 0)
        if not names and not unnamed and dm_name and others >= 1:
            names, unnamed = [dm_name], 0
        if names or unnamed:
            more = max(others, len(names) + unnamed) - len(names)
            if len(names) > REACTION_BY_MAX:
                more += len(names) - REACTION_BY_MAX
                names = names[:REACTION_BY_MAX]
            if names:
                entry["by"] = names
            if more:
                entry["by_more"] = more
        out.append(entry)
    return out


def message_reactions_from_updates(updates: object, chat_marked_id: int, msg_id: int) -> object | None:
    """Ф10-3 П4: `MessageReactions` сообщения `msg_id` чата `chat_marked_id` из ответа `SendReactionRequest`/
    `GetMessagesReactionsRequest` (`Updates`/`UpdateShort`) — или `None`, если там такого апдейта нет."""
    if isinstance(updates, types.UpdateShort):
        items = [updates.update]
    else:
        items = list(getattr(updates, "updates", None) or [])
    for update in items:
        if not isinstance(update, types.UpdateMessageReactions) or update.msg_id != msg_id:
            continue
        try:
            if utils.get_peer_id(update.peer) != chat_marked_id:
                continue
        except TypeError, ValueError:
            continue
        return update.reactions
    return None


def message_reactions_all(updates: object, chat_marked_id: int) -> list[tuple[int, int | None, object]]:
    """Ф18-2: все `UpdateMessageReactions` чата `chat_marked_id` из ответа пачечного
    `GetMessagesReactionsRequest` — `[(msg_id, top_msg_id, MessageReactions)]` в порядке ответа."""
    if isinstance(updates, types.UpdateShort):
        items = [updates.update]
    else:
        items = list(getattr(updates, "updates", None) or [])
    out: list[tuple[int, int | None, object]] = []
    for update in items:
        if not isinstance(update, types.UpdateMessageReactions):
            continue
        try:
            if utils.get_peer_id(update.peer) != chat_marked_id:
                continue
        except TypeError, ValueError:
            continue
        out.append((update.msg_id, update.top_msg_id, update.reactions))
    return out


# --- служебные сообщения --------------------------------------------------------------------------------

_SERVICE_TEXTS: tuple[tuple[type, str], ...] = (
    (types.MessageActionChatAddUser, "вход"),
    (types.MessageActionChatJoinedByLink, "вход"),
    (types.MessageActionChatJoinedByRequest, "вход"),
    (types.MessageActionChatDeleteUser, "выход"),
    (types.MessageActionPinMessage, "закрепление"),
    (types.MessageActionChatEditPhoto, "смена фото"),
    (types.MessageActionChatDeletePhoto, "смена фото"),
    (types.MessageActionTopicCreate, "создание темы"),
    # Раздел «Миграция в супергруппу»: последнее сообщение старой группы — эта служебка. `connector.py`
    # эмитит её как обычное `event`-сообщение (находка 6 приёмки куска 1b) — без этого max_ext_sort старой
    # группы никогда не догонял бы `top_message`, и сверка бесконечно «догоняла» бы один и тот же id.
    (types.MessageActionChatMigrateTo, "группа стала супергруппой"),
)
_DEFAULT_SERVICE_TEXT = "служебное событие"


def service_text(action: object) -> str:
    if isinstance(action, types.MessageActionChatEditTitle):
        return "смена названия"
    for cls, text in _SERVICE_TEXTS:
        if isinstance(action, cls):
            return text
    return _DEFAULT_SERVICE_TEXT


def is_migrate_action(action: object) -> bool:
    return isinstance(action, types.MessageActionChatMigrateTo | types.MessageActionChannelMigrateFrom)


def build_channel_migrate_event(msg: object, *, new_channel: dict) -> dict:
    """`msg.action` — `MessageActionChannelMigrateFrom`, встреченный в НОВОЙ супергруппе (она несёт полную
    информацию: старый обычный `chat_id`). Возвращает payload `ChannelMigrateEvent` без ключа `type`."""
    action = msg.action
    old_ext_id = peer_ext_id(types.PeerChat(action.chat_id))
    return {"old_ext_id": old_ext_id, "channel": new_channel}


# --- отправка (кусок 1c, docs/architecture.md) -------------------------------------------------


def split_channel_ext_id(channel_ext_id: str) -> tuple[str, int | None]:
    """`<chat>` или `<chat>:<topic_id>` -> `(chat_ext_id, topic_id|None)` — раздел «Отправка»."""
    if ":" in channel_ext_id:
        chat_ext, topic_str = channel_ext_id.split(":", 1)
        return chat_ext, int(topic_str)
    return channel_ext_id, None


def random_id_from_token(send_token: str) -> int:
    """`random_id = int.from_bytes(sha256(send_token).digest()[:8], "big", signed=True)` — детерминирован,
    одинаков на всех попытках одной строки outbox (решения архитектора куска 1c, раздел «Отправка»)."""
    digest = hashlib.sha256(send_token.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def utf16_len(text: str) -> int:
    """Длина в UTF-16 code units (единица лимита Telegram — 4096, раздел «Отправка»), не кодовые точки
    Python (расходятся на эмодзи вне BMP, та же оговорка, что у `_utf16_slice`)."""
    return len(text.encode("utf-16-le")) // 2


def build_reply_to(*, topic_id: int | None, reply_to_ext_id: str | None) -> object | None:
    """`InputReplyToMessage` для отправки (раздел «Отправка», поправка F6 критика куска 1c). По документации
    `inputReplyToMessage` (core.telegram.org/constructor/inputReplyToMessage): `top_msg_id` заполняется
    ТОЛЬКО когда `reply_to_msg_id != topic_id` И `topic_id != 1` — то есть только у НАСТОЯЩЕГО ответа на
    конкретное сообщение внутри НЕ-General темы. `topic_id` — тема форума (лист — топик) или `None` (обычный
    чат/группа/личка). General без явного ответа -> без `reply_to` вовсе (обычное сообщение, как не в
    форуме). Пост «в тему» без ответа (в т.ч. General) -> `reply_to_msg_id=topic_id`, БЕЗ `top_msg_id`."""
    if topic_id is not None:
        if reply_to_ext_id is None:
            if topic_id == GENERAL_TOPIC_ID:
                return None
            return types.InputReplyToMessage(reply_to_msg_id=topic_id)
        msg_id = int(reply_to_ext_id)
        if msg_id != topic_id and topic_id != GENERAL_TOPIC_ID:
            return types.InputReplyToMessage(reply_to_msg_id=msg_id, top_msg_id=topic_id)
        return types.InputReplyToMessage(reply_to_msg_id=msg_id)
    if reply_to_ext_id is not None:
        return types.InputReplyToMessage(reply_to_msg_id=int(reply_to_ext_id))
    return None


def extract_sent_ext_id(result: object, random_id: int) -> str | None:
    """`ext_id` из ответа `SendMessageRequest` (раздел «Отправка»): `UpdateShortSentMessage.id` (личка) либо
    `UpdateMessageID` с НАШИМ `random_id` внутри `Updates.updates` (группа/канал/форум). Ответ без узнаваемой
    формы -> `None` (вызывающий ищет в истории)."""
    if isinstance(result, types.UpdateShortSentMessage):
        return str(result.id)
    updates = getattr(result, "updates", None)
    if updates:
        for u in updates:
            if isinstance(u, types.UpdateMessageID) and u.random_id == random_id:
                return str(u.id)
    return None


def extract_album_ids(result: object, random_ids: list[int]) -> tuple[list[str | None], str | None]:
    """Ф11-3b B2: ответ `SendMultiMediaRequest` -> (`ext_id` каждого элемента по `UpdateMessageID` его
    `random_id`, в порядке элементов; не нашёлся — `None`), `grouped_id` (строкой) из сообщений ответа
    (`UpdateNewMessage`/`UpdateNewChannelMessage`) с этими id; нет — `None`."""
    by_random: dict[int, str] = {}
    messages: list[object] = []
    for u in getattr(result, "updates", None) or []:
        if isinstance(u, types.UpdateMessageID):
            by_random[u.random_id] = str(u.id)
        elif isinstance(u, types.UpdateNewMessage | types.UpdateNewChannelMessage):
            messages.append(u.message)
    ids = [by_random.get(rid) for rid in random_ids]
    own = {i for i in ids if i is not None}
    grouped_id: str | None = None
    for msg in messages:
        gid = getattr(msg, "grouped_id", None)
        if gid is not None and str(getattr(msg, "id", "")) in own:
            grouped_id = str(gid)
            break
    return ids, grouped_id


# --- сборка события сообщения ---------------------------------------------------------------------------


def _ts_ms(dt: object) -> int:
    return int(dt.timestamp() * 1000)


def edited_ts(msg: object) -> int | None:
    """`edit_date` сообщения; `edit_hide` — источник просит НЕ показывать правку (посты каналов с группой
    обсуждения, служебные правки ботов): официальные клиенты пометку «изменено» не ставят — и хаб тоже."""
    if getattr(msg, "edit_hide", None) is True:
        return None
    edit_date = getattr(msg, "edit_date", None)
    return _ts_ms(edit_date) if edit_date else None


# Ф44-1a (спека 2.1): пределы кнопок — те же числа, что проверяет ядро (`core/buttons.py`; коннектор ядро
# не импортирует). Сверх предела ядро не отклоняет событие, а обнуляет кнопки целиком — поэтому режем здесь.
_BTN_TEXT_MAX = 128
_BTN_URL_MAX = 1024
_BTN_COPY_MAX = 256
_BTN_COUNT_MAX = 100
_BTN_ROWS_MAX = 12
_BTN_JSON_MAX = 8 * 1024
_BTN_DATA_MAX = 192  # байт сырого callback (у Telegram — до 64); в base64 это 256 знаков


def buttons_ts(msg: object) -> int:
    """Отметка времени снимка кнопок: СЫРОЙ `edit_date` (и при `edit_hide` — правку одной клавиатуры
    Telegram помечает именно так, а `edited_ts` её прячет), иначе `date`. Мс."""
    edit_date = getattr(msg, "edit_date", None)
    return _ts_ms(edit_date) if edit_date else _ts_ms(msg.date)


def _convert_button(button: object) -> tuple[dict, bool]:
    """Одна инлайн-кнопка -> `(словарь события, обрезано ли)`."""
    cut = False
    text = getattr(button, "text", None)
    text = text if isinstance(text, str) else ""
    if len(text) > _BTN_TEXT_MAX:
        text, cut = text[:_BTN_TEXT_MAX], True
    btype = getattr(button, "type", None)
    if isinstance(btype, types.InlineButtonTypeCallback):
        data = btype.data if isinstance(btype.data, bytes) else b""
        if len(data) > _BTN_DATA_MAX:
            return {"t": text, "k": "other"}, True
        out = {"t": text, "k": "callback", "d": base64.b64encode(data).decode("ascii")}
        if btype.requires_password:
            out["pw"] = True
        return out, cut
    if isinstance(btype, types.InlineButtonTypeUrl):
        url = btype.url if isinstance(btype.url, str) else ""
        if not url or len(url) > _BTN_URL_MAX:
            # Обрезанный адрес — другой адрес: кнопку показываем, ссылку не отдаём.
            return {"t": text, "k": "other"}, True
        return {"t": text, "k": "url", "u": url}, cut
    if isinstance(btype, types.InlineButtonTypeCopy):
        copy_text = btype.copy_text if isinstance(btype.copy_text, str) else ""
        if not copy_text or len(copy_text) > _BTN_COPY_MAX:
            # Обрезанный текст для копирования (ключ, код) — неверный текст: не отдаём вовсе.
            return {"t": text, "k": "other"}, True
        return {"t": text, "k": "copy", "c": copy_text}, cut
    # url_auth, web_view, switch_inline, buy, game, user_profile, disabled и всё незнакомое: видны, не
    # нажимаются; адрес url_auth и web_view не кладётся — это не обычные ссылки.
    return {"t": text, "k": "other"}, cut


def _buttons_size(buttons: dict) -> int:
    return len(json.dumps(buttons, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def convert_buttons(reply_markup: object) -> dict | None:
    """Ф44-1a: ТОЛЬКО инлайн-кнопки (`ReplyInlineMarkup`) -> поле события `buttons`; reply-клавиатуры,
    `ReplyKeyboardHide`, `ReplyKeyboardForceReply` и отсутствие разметки — `None` (П9: клавиатура чата
    вынесена из Ф44-1). Сверх пределов — обрезать и поставить `cut`."""
    if not isinstance(reply_markup, types.ReplyInlineMarkup):
        return None
    cut = False
    rows: list[list[dict]] = []
    total = 0
    for row in reply_markup.rows or []:
        if len(rows) >= _BTN_ROWS_MAX or total >= _BTN_COUNT_MAX:
            cut = True
            break
        out_row: list[dict] = []
        for button in getattr(row, "buttons", None) or []:
            if total >= _BTN_COUNT_MAX:
                cut = True
                break
            converted, was_cut = _convert_button(button)
            cut = cut or was_cut
            out_row.append(converted)
            total += 1
        if out_row:
            rows.append(out_row)
    if not rows:
        return None
    out: dict = {"kind": "inline", "rows": rows}
    if cut:
        out["cut"] = True
    while len(out["rows"]) > 1 and _buttons_size(out) > _BTN_JSON_MAX:
        out["rows"] = out["rows"][:-1]
        out["cut"] = True
    while out["rows"] and _buttons_size(out) > _BTN_JSON_MAX:
        # Один ряд не влез целиком: убираем кнопки с конца ряда.
        out["rows"] = [out["rows"][0][:-1]] if len(out["rows"][0]) > 1 else []
        out["cut"] = True
    return out


@dataclass(frozen=True)
class ConvertContext:
    """Всё, что `convert_message` знает о канале-получателе (уже разрешённом вызывающим — топик или нет,
    заголовки топиков требуют сети, поэтому решаются в `connector.py`, не здесь)."""

    channel: dict  # ChannelRef payload листового канала (dm/group/channel/topic)
    channel_kind_for_sender: str  # "dm" — как в исходном верхнем диалоге (для sender_ref без from_id)
    me_id: int
    entities: dict[int, object]  # marked id -> User/Chat/Channel
    # `source_url` (раздел «Идентификаторы»): публичный — по `public_username`; приватный канал/супергруппа —
    # по НЕМЕЧЕНОМУ numeric id (`private_channel_num`, `https://t.me/c/<id>/…`); личка и малая группа — ни
    # то, ни другое -> ссылки нет. Ровно один из двух задан (или ни одного).
    public_username: str | None = None
    private_channel_num: int | None = None
    # Мелочь из отчёта исполнителя (round 2): peer ТЕКУЩЕГО канала — сравнить с `reply.reply_to_peer_id`
    # (ответ на сообщение в ДРУГОМ чате не должен получать `reply_to_ext_id` из чужого пространства id).
    chat_peer: object | None = None


def _source_url(ctx: ConvertContext, msg_id: int, *, topic_id: int | None) -> str | None:
    if ctx.public_username:
        return f"https://t.me/{ctx.public_username}/{msg_id}"
    if ctx.private_channel_num is not None:
        if topic_id is not None:
            return f"https://t.me/c/{ctx.private_channel_num}/{topic_id}/{msg_id}"
        return f"https://t.me/c/{ctx.private_channel_num}/{msg_id}"
    return None


def convert_message(msg: object, ctx: ConvertContext) -> dict:
    """Возвращает dict БЕЗ ключа `type` (upsert/edit решает вызывающий — конвертация от режима не зависит,
    раздел «Устройство пакета»). Детерминировано: один и тот же `msg`+`ctx` -> побайтно тот же результат."""
    is_service = isinstance(msg, types.MessageService)
    topic_id = message_topic_id(msg)
    reply_to_ext_id = message_reply_to_ext_id(msg, topic_id=topic_id, chat_peer=ctx.chat_peer)

    if is_service:
        kind = "event"
        text = service_text(msg.action)
        entities: list[dict] = []
        attachments = None
    else:
        media = getattr(msg, "media", None)
        attachments = convert_attachments(media)
        kind = "file" if attachments is not None else "text"
        text = msg.message if msg.message else None
        entities = convert_entities(getattr(msg, "entities", None), text)

    sender = sender_ref(msg, entities=ctx.entities, me_id=ctx.me_id, channel_kind=ctx.channel_kind_for_sender)
    forward = (
        None
        if is_service
        else forward_ref(getattr(msg, "fwd_from", None), getattr(msg, "from_id", None), ctx.entities)
    )
    # Реакции — НЕ поле `upsert`/`edit` (их нет в `events.UpsertEvent`): отдельное событие `reaction`,
    # которое собирает вызывающий (`connector.py`) через `reactions_list()` уже ПОСЛЕ `convert_message`.
    link_urls = entity_link_urls(entities) if entities else []
    refs_list = refs.extract_refs(text, link_urls=link_urls) if not is_service else []

    attachments_out = attachments
    if attachments_out is not None:
        source_url = _source_url(ctx, msg.id, topic_id=topic_id)
        if source_url is not None:
            attachments_out = [{**a, "source_url": source_url} for a in attachments_out]

    out = {
        "channel": ctx.channel,
        "ext_id": str(msg.id),
        "ts": _ts_ms(msg.date),
        "sender": sender,
        "is_me": effective_is_me(msg, ctx.me_id),
        "kind": kind,
        "text": text,
        "entities": entities or None,
        "attachments": attachments_out,
        "forward": forward,
        "mentions_me": bool(getattr(msg, "mentioned", False)),
        "refs": refs_list or None,
        "reply_to_ext_id": reply_to_ext_id,
        "grouped_id": str(msg.grouped_id) if getattr(msg, "grouped_id", None) else None,
        "edited_ts": edited_ts(msg),
        # Ф44-1a: инлайн-кнопки любого отправителя (боты пишут и в группах) и отметка их снимка — всегда:
        # «кнопок больше нет» тоже состояние.
        "buttons": None if is_service else convert_buttons(getattr(msg, "reply_markup", None)),
        "buttons_ts": buttons_ts(msg),
    }
    counters = post_views(msg) if ctx.channel.get("kind") == "channel" and not is_service else None
    if counters is not None:
        # Ф23-3 п.2: просмотры/пересылки поста канала (broadcast) на момент приёма; нет счётчика — полей нет.
        out["views"], out["forwards"] = counters
    return out


def post_views(msg: object) -> tuple[int, int | None] | None:
    """Ф23-3: `(views, forwards)` поста канала; `views` нет (не пост канала, служебное) — `None`."""
    views = getattr(msg, "views", None)
    if not isinstance(views, int) or isinstance(views, bool) or views < 0:
        return None
    forwards = getattr(msg, "forwards", None)
    if not isinstance(forwards, int) or isinstance(forwards, bool) or forwards < 0:
        forwards = None
    return views, forwards


def views_items(messages: list, *, since_ms: int) -> list[dict]:
    """Ф23-3 п.4: элементы события `views` по истории канала — только посты не старше `since_ms` (мс) со
    счётчиком, по возрастанию id. Служебные сообщения и посты без `views` — мимо."""
    items: list[dict] = []
    for msg in sorted(messages, key=lambda m: m.id):
        if isinstance(msg, types.MessageService) or getattr(msg, "date", None) is None:
            continue
        if _ts_ms(msg.date) < since_ms:
            continue
        counters = post_views(msg)
        if counters is None:
            continue
        items.append({"ext_id": str(msg.id), "views": counters[0], "forwards": counters[1]})
    return items
