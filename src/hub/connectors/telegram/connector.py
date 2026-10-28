"""`TelegramConnector(ConnectorBase)` — вход, приём, сверка, отправка. Спецификация:
`docs/architecture.md` (приём), `docs/architecture.md` (отправка, кусок 1c —
`sends_enabled = True`). Сеть — только через `Gateway` (см. `gateway.py`); этот модуль знает про TL-объекты
(через `convert.py`/`folders.py`), но не про `TelegramClient` напрямую — тесты подсовывают `FakeGateway` без
единого сетевого вызова.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import logging
import os
import random
import tempfile
from collections import deque
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from telethon import errors, utils
from telethon.tl import types

from .. import meta_parts
from ..base import (
    _AUTH_EXIT_CODE,
    _SLEEP_THRESHOLD_S,
    STOP_PEER_FLOOD,
    AuthRequired,
    ConnectorBase,
    OutboxFileError,
    SendResult,
    _is_core_unreachable,
    _is_rejected,
    lease_files_meta,
)
from ..join import JoinResult
from ..lookup import LookupResult
from ..media import MediaDownloader, MediaResult, MediaSink
from ..msg_ops import MsgOpResult
from ..press import PressResult
from ..reactions import ReactionResult
from ..read_ack import ReadAckResult
from . import convert
from . import folders as folders_mod
from . import voice as voice_mod
from .gateway import (
    AlbumItem,
    DeletedMessagesGE,
    EditedMessageGE,
    FoldersChangedGE,
    FoldersDirtyGE,
    Gateway,
    MediaUploadError,
    NewMessageGE,
    PeerReadGE,
    PressUnknown,
    ReactionsGE,
    ReadInboxGE,
    message_entities,
)

_logger = logging.getLogger(__name__)

# --- сверка (раздел «Сверка» спеки) ------------------------------------------------------------------------
# Ф42-1 (`docs/architecture.md`): догон — список дыр, страницы по 100 от новых к старым.
_CATCHUP_PAGE = 100  # Telegram за один `GetHistoryRequest` отдаёт не больше 100 при любом `limit`
_CATCHUP_PAGES_PER_SYNC = 30  # страниц на все чаты за заход сверки (≈ 20–40 с при `_SYNC_TIMEOUT_S` 600)
_CATCHUP_PAGE_JITTER_S = (0.3, 1.0)  # пауза между страницами
_CATCHUP_FLUSH_TIMEOUT_S = 30.0  # сколько ждать, пока страница дойдёт до ядра, прежде чем писать курсор
_CATCHUP_STALE_WARN_MS = 60 * 60 * 1000  # дыра живёт дольше -> предупреждение (не чаще раза в час на чат)
_CATCHUP_CURSOR_VERSION = 1
# Ф42-3 (`docs/architecture.md`): добор потерянного — с запуска Telegram в хабе, 23.09.2026
# 00:00 по времени хаба (мс). Другое значение — засев заново.
_HISTORY_REFILL_SINCE_MS = int(
    dt.datetime(2026, 9, 23, tzinfo=dt.timezone(dt.timedelta(hours=3))).timestamp() * 1000
)
_SYNC_STOP_STALE = 20  # подряд незакреплённых известных диалогов без новостей -> стоп прохода
_SYNC_STOP_TOTAL = 1000  # либо столько диалогов всего

# --- стартовая загрузка --------------------------------------------------------------------------------
_INITIAL_LOAD_LIMIT = 30
_INITIAL_LOAD_ACTIVE_MS = 30 * 24 * 60 * 60 * 1000  # активность ≤ 30 дней
_INITIAL_LOAD_JITTER_S = (1.0, 3.0)
# Ф17-1, ревью (находка 4): попыток стартовой загрузки беседы после вступления при сбое чата (не FloodWait).
_JOIN_LOAD_TRIES = 3
_INITIAL_LOAD_BATCH_MAX = 40  # не больше за ОДНУ сверку (находка 4 приёмки куска 1b) — иначе таймаут sync()

# --- перечитывание окна ---------------------------------------------------------------------------------
_WINDOW_LIMIT = 30
# Нагрузка (мелочь из отчёта исполнителя, round 2): было 2 ч/20 чатов на обычной сверке — при активной
# переписке в нескольких чатах сразу окно молотило бы `GetHistoryRequest` почти на каждый; 30 мин/10 чатов
# сужает круг до реально «только что открытых» без потери смысла (окно ловит правки/удаления, пропущенные во
# сне — 30 мин между сверками с запасом покрывают обычный интервал `_SYNC_INTERVAL_S` каркаса).
_WINDOW_ACTIVE_MS = 30 * 60 * 1000  # 30 мин
_WINDOW_ACTIVE_RESUME_MS = 24 * 60 * 60 * 1000  # resume — 24 ч, как было
_WINDOW_MAX_CHATS = 10
_WINDOW_MAX_CHATS_RESUME = 50  # resume — как было

# --- папки ------------------------------------------------------------------------------------------------
_FOLDER_REFRESH_INTERVAL_S = 60 * 60.0  # раз в 60 мин

# --- просмотры постов каналов (Ф23-3, docs/architecture.md) -----------------------------
_VIEWS_FIRST_DELAY_S = 5 * 60.0  # первый проход — через ~5 мин после старта, не сразу
_VIEWS_INTERVAL_S = 60 * 60.0  # дальше раз в час
_VIEWS_RETRY_S = 5 * 60.0  # FloodWait (не меньше его срока), сеть, ядро недоступно — проход откладывается
_VIEWS_MAX_AGE_MS = 72 * 60 * 60 * 1000  # только посты моложе 72 ч
_VIEWS_HISTORY_LIMIT = 100  # одна история канала за проход — до 100 постов (и до 100 элементов события)
_VIEWS_CHANNEL_PAUSE_S = 2.0  # пауза между каналами
_VIEWS_NET_ERRORS = (OSError, errors.ServerError, errors.TimedOutError)

# --- перечитывание реакций (Ф18-2, docs/architecture.md) -----------------------------
REACT_REFRESH_WINDOW_S = 3.0  # окно сбора id одного чата в один запрос
REACT_REFRESH_PER_MIN = 10  # запросов `GetMessagesReactions` в минуту на аккаунт
REACT_REFRESH_CHAT_COOLDOWN_S = 30.0  # один чат — не чаще раза в 30 с (шумная группа не съедает предел)
REACT_REFRESH_QUEUE_MAX = 500  # сообщений в очереди; лишние отбрасываются
_REACT_REFRESH_BATCH = 100  # id в одном запросе
_REACT_REFRESH_MINUTE_S = 60.0

# --- отправка (кусок 1c, docs/architecture.md) -------------------------------------------------
_MAX_MESSAGE_UTF16_LEN = 4096
_ECHO_HISTORY_MARGIN_MS = 5_000  # "date >= send_started_ts - 5 с" (раздел «Отправка», повторная попытка)
_HISTORY_SEARCH_LIMIT = 50  # "последние 50 сообщений чата" — обычный поиск при повторной попытке
_HISTORY_SEARCH_LIMIT_DUP = 100  # "по 100 последним" — поиск после RandomIdDuplicateError
_ACCOUNT_LIMIT_WINDOW_MS = 60_000  # скользящее окно лимитера аккаунта (план 4.1)
_ACCOUNT_LIMIT_MAX = 20  # 20 отправок в минуту на аккаунт
_CHAT_MIN_GAP_MS = 1_000  # не чаще 1 в секунду на чат

# Раздел «Отправка», находки/решения 11.5: имена ошибок -> `permanent`, `error` = имя класса. Смешанные базы
# (`ChatWriteForbiddenError` — `ForbiddenError`, остальные — `BadRequestError`) — намеренно единый список,
# порядок except'ов в `send()` кладёт его ПЕРЕД общим `except errors.BadRequestError`.
_PERMANENT_SEND_ERRORS = (
    errors.ChatWriteForbiddenError,
    errors.UserBannedInChannelError,
    errors.ChannelPrivateError,
    errors.UserIsBlockedError,
    errors.InputUserDeactivatedError,
    errors.PeerIdInvalidError,
    errors.MessageTooLongError,
    errors.TopicDeletedError,
    errors.ChatAdminRequiredError,
)

# --- отправка файла (Ф10-4 B2, П3, П11, `docs/architecture.md`) ------------------------------
_MAX_CAPTION_UTF16_LEN = 1024
# П11: фото не принято как фото -> ОДИН повтор документом (`force_file`); у документа они — `permanent`.
_RETRY_AS_DOCUMENT_ERRORS = (
    errors.FilePartsInvalidError,
    errors.MediaInvalidError,
    errors.PhotoInvalidDimensionsError,
    errors.ImageProcessFailedError,
    errors.PhotoExtInvalidError,
    errors.PhotoSaveFileInvalidError,
)
_DEFAULT_FILE_NAME = "file"
# Ф14-4: собеседник (или права чата) запрещает голосовые — один повтор файлом с новым `random_id`.
_VOICE_FORBIDDEN_ERRORS = (errors.VoiceMessagesForbiddenError, errors.ChatSendVoicesForbiddenError)


class _VoiceForbidden(NamedTuple):
    """Ф14-4: голосовое не принято — запрет голосовых у собеседника/в чате (`error` — имя класса ошибки)."""

    error: str


class _RetryAsDocument(NamedTuple):
    """Итог попытки `SendMedia`: источник не принял медиа этого вида (`error` — имя класса ошибки)."""

    error: str


# --- черновик новому адресату (Ф12-1 B2, `docs/architecture.md`) ------------------------
# Временный `ext_id` беседы-заготовки ядра — в `split_channel_ext_id` не попадает никогда (П8).
_TEMP_CHANNEL_PREFIX = "new:"
_PRIVACY_ERRORS = frozenset(
    {"USER_PRIVACY_RESTRICTED", "PRIVACY_PREMIUM_REQUIRED", "UserPrivacyRestrictedError"}
)
_PRIVACY_TEXT = "человек принимает личку только от контактов/Premium"


def _album_random_ids(send_token: str, n: int) -> list[int]:
    """Ф11-3b B2: `random_id` элементов альбома — детерминированы из `send_token` (повтор безопасен): элемент
    0 — как у одиночной отправки, элемент k ≥ 1 — из `"<send_token>:<k>"`."""
    return [convert.random_id_from_token(send_token)] + [
        convert.random_id_from_token(f"{send_token}:{k}") for k in range(1, n)
    ]


def _album_sent(found: tuple[list[str], str | None]) -> SendResult:
    """Найденный/отправленный альбом -> `sent` с `ext_ids` всех элементов и `grouped_id` (контракт A↔B)."""
    ext_ids, grouped_id = found
    return SendResult("sent", ext_id=ext_ids[0], ext_ids=tuple(ext_ids), grouped_id=grouped_id)


def _is_new_chat(lease: dict) -> bool:
    """П8: задание беседы-заготовки — по `new_to` ИЛИ по временному `channel_ext_id` (второе — страховка: без
    `new_to` такое задание уходит в `permanent`, а не в `split_channel_ext_id`)."""
    return lease.get("new_to") is not None or str(lease.get("channel_ext_id") or "").startswith(
        _TEMP_CHANNEL_PREFIX
    )


def _new_username(lease: dict) -> str | None:
    new_to = lease.get("new_to")
    if not isinstance(new_to, dict) or new_to.get("kind") != "tg_username":
        return None
    username = new_to.get("username")
    return username if isinstance(username, str) and username else None


# Ф14-2 B2: адресат `tg_user` — id пользователя Telegram (как `user_id` ядра: 1..2^53).
_TG_USER_ID_MAX = 2**53
_CONTACT_NOT_FOUND_TEXT = "контакт не найден — напишите по @username"


def _new_user_id(new_to: dict) -> int | None:
    user_id = new_to.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not 1 <= user_id <= _TG_USER_ID_MAX:
        return None
    return user_id


# Ф34-3 (`docs/architecture.md`а): адресат `tg_phone` — номер телефона. Номер — ПДн: в
# журнал, `SendResult.error` и уведомления не попадает (ни сам, ни `repr` запроса/исключения Telethon).
PHONE_DAILY_DEFAULT = 5
PHONE_DAILY_MAX = 20
_PHONE_WINDOW_MS = 24 * 60 * 60 * 1000
_PHONE_STATE_KEY = "phone_resolve"
_PHONE_NOT_FOUND_TEXT = "номер не найден в Telegram или скрыт настройками приватности"
_PHONE_SELF_TEXT = "это ваш номер"
_PHONE_OFF_TEXT = "поиск по номеру телефона выключен (tg_phone_daily=0)"
_PHONE_LIMIT_TEXT = "лимит поиска по номеру телефона в сутки — уйдёт позже"
_PHONE_PAUSE_TEXT = "поиск по номеру телефона на паузе (FloodWait) — уйдёт позже"


def phone_daily_of(config: object) -> int:
    """Настройка аккаунта `tg_phone_daily` (как читает ядро: целое 0…20, больше — 20, кривое — 5): потолок
    настоящих `contacts.ResolvePhone` за 24 ч."""
    value = config.get("tg_phone_daily") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return PHONE_DAILY_DEFAULT
    return min(value, PHONE_DAILY_MAX)


def _new_phone_digits(new_to: dict) -> str | None:
    """Цифры номера адресата `tg_phone` (8–15) или `None`; `User.phone` у Telethon и запрос — без «+»."""
    phone = new_to.get("phone")
    if not isinstance(phone, str):
        return None
    digits = "".join(ch for ch in phone if ch.isascii() and ch.isdigit())
    return digits if 8 <= len(digits) <= 15 else None


# Ф46-1 (`docs/architecture.md` и 10 П7–П8): адресат `tg_msg_author` — автор
# сообщения группы. Имя и id автора в журнал не пишутся — только слово-ветка разрешения.
_AUTHOR_MSG_GONE_TEXT = (
    "сообщение группы недоступно (удалено или нас нет в группе) — написать автору через него нельзя"
)
_AUTHOR_CHAT_GONE_TEXT = "группы больше нет в аккаунте — написать автору через неё нельзя"
_AUTHOR_BASIC_GROUP_TEXT = "в обычной группе написать автору через сообщение нельзя — нужен @ник или номер"
_AUTHOR_DELETED_TEXT = "аккаунт удалён"
_AUTHOR_BOT_TEXT = "автор — бот"
_AUTHOR_SELF_TEXT = "это ваше сообщение"
_AUTHOR_MSG_GONE_ERRORS = (
    errors.MsgIdInvalidError,
    errors.MessageIdInvalidError,
    errors.PeerIdInvalidError,
    errors.ChannelPrivateError,
)
_SUPERGROUP_PREFIX = "-100"


def _new_author_ref(new_to: dict) -> tuple[int, str, int] | None:
    """`(user_id, chat_ext_id, msg_id)` адресата `tg_msg_author` или `None` — ссылка кривая."""
    user_id = _new_user_id(new_to)
    chat_ext = new_to.get("chat_ext_id")
    msg_ext = new_to.get("msg_ext_id")
    if user_id is None or not isinstance(chat_ext, str) or not isinstance(msg_ext, str):
        return None
    if not (chat_ext.startswith("-") and chat_ext[1:].isascii() and chat_ext[1:].isdigit()):
        return None
    if not (msg_ext.isascii() and msg_ext.isdigit()) or not 1 <= int(msg_ext) < 2**31:
        return None
    return user_id, chat_ext, int(msg_ext)


def _check_before_send(lease: dict, files: list[dict]) -> SendResult | None:
    """Проверки задания без запросов (кусок 1c, Ф10-4 П11, Ф11-3b): текст длиннее 4096 UTF-16, подпись к
    файлу длиннее 1024, смешанный альбом -> `permanent`. Общие для обычной и новой беседы (Ф14-2 B3)."""
    text = lease.get("text") or ""
    if not files:
        if convert.utf16_len(text) > _MAX_MESSAGE_UTF16_LEN:
            return SendResult("permanent", error="слишком длинное сообщение")
        return None
    if convert.utf16_len(text) > _MAX_CAPTION_UTF16_LEN:
        return SendResult("permanent", error="подпись длиннее 1024")
    if len(files) >= 2 and len({"photo" if f.get("kind") == "photo" else "document" for f in files}) != 1:
        return SendResult("permanent", error="смешанный альбом: только картинки или только файлы")
    return None


def contacts_items(users: list, *, me_id: int | None) -> list[dict]:
    """Ф14-2 B1: контакты для `tg_contacts` — `{id, first, last, username}` пользователей, без телефонов,
    ботов, удалённых и себя; по возрастанию id (одинаковый список — одинаковая публикация)."""
    items: dict[int, dict] = {}
    for user in users:
        if not isinstance(user, types.User):
            continue
        if user.bot or user.deleted or user.is_self or user.id == me_id:
            continue
        username = user.username
        if not username:
            for item in getattr(user, "usernames", None) or []:
                if getattr(item, "active", False) and getattr(item, "username", None):
                    username = item.username
                    break
        items[user.id] = {
            "id": user.id,
            "first": user.first_name or None,
            "last": user.last_name or None,
            "username": username or None,
        }
    return [items[key] for key in sorted(items)]


def _before_send(result: SendResult) -> SendResult:
    """П3: сбой ДО `SendMediaRequest` (разрешение peer, поиск в истории) — сообщение не уходило: `abandon`
    сети превращается в `temporary`; `flood` остаётся `flood`."""
    if result.outcome == "abandon":
        return SendResult("temporary", error="network")
    return result


# --- алерт о новой сессии (раздел «Алерт о новой сессии Telegram» спеки куска 1c) --------------------------
_SESSION_ALERT_INTERVAL_S = 24 * 60 * 60.0  # раз в сутки (и на старте — отдельный явный вызов)
_STATE_FILE_NAME = "state.json"

# health(): потеря авторизации, не сеть (раздел «Жизненный цикл»). `UnauthorizedError`/`AuthKeyError` —
# БАЗОВЫЕ классы (находка F1, round 2): `AuthKeyUnregisteredError`/`SessionRevokedError`/
# `UserDeactivatedError`/`UserDeactivatedBanError` — все подклассы `UnauthorizedError` (`rpcbaseerrors.py`),
# но раз мы теперь зовём СЫРОЙ `GetUsersRequest` (не `client.get_me()`, который эти ошибки глотал), ловим
# по базовым классам — не пропустить будущий подкласс с тем же смыслом. `AuthKeyError` — соседняя база
# (напр. `AuthKeyDuplicatedError` при параллельном входе с той же сессией) — тоже безусловная потеря входа.
_AUTH_LOST_ERRORS = (errors.UnauthorizedError, errors.AuthKeyError)
# Ф44-1c: кнопки на этом месте уже нет (сообщение удалено, клавиатуру сменили) — `stale_button`.
_PRESS_STALE_ERRORS = (errors.MessageIdInvalidError, errors.DataInvalidError, errors.ButtonDataInvalidError)
# Ожидание ответа на нажатие внутри шлюза — короче шага исполнителя (`connectors/press.PUSH_TIMEOUT_S`).
_PRESS_TIMEOUT_S = 20.0

# Ф42-1: на запрос истории источник ответил «чат недоступен навсегда» (закрытый канал, бан в канале, peer
# недействителен, собеседник удалён) — дыру догона заполнять неоткуда, она снимается с предупреждением.
_CATCHUP_GONE_ERRORS = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChannelBannedError,
    errors.ChannelPublicGroupNaError,
    errors.ChatIdInvalidError,
    errors.ChatForbiddenError,
    errors.PeerIdInvalidError,
    errors.InputUserDeactivatedError,
)


class _GapChatRejectedError(Exception):
    """Ф42-1: ядро отвергло по содержимому (4xx) все сообщения страницы чата — чат пропускается до следующего
    захода, его дыра не двигается, остальные чаты заполняются."""


class _GapPageError(Exception):
    """Ф42-1: страница дыры не получена по причине самого чата (не FloodWait, не потеря входа, не «недоступен
    навсегда») — чат пропускается до следующего захода, остальные заполняются. Причина — в `__cause__`."""

# Ф17-1 C: ошибки вступления -> причина `drop` (по порядку; прочие `RPCError` -> текст ошибки Telegram).
_JOIN_DROP_ERRORS = (
    ((errors.UsernameNotOccupiedError, errors.UsernameInvalidError), "not_found"),
    ((errors.InviteHashExpiredError,), "invite_expired"),
    ((errors.InviteHashInvalidError, errors.InviteHashEmptyError), "invite_invalid"),
    ((errors.ChannelPrivateError, errors.ChannelInvalidError), "private"),
    ((errors.ChannelsTooMuchError, errors.UserChannelsTooMuchError), "too_many_channels"),
    ((errors.UsersTooMuchError,), "group_full"),
)

# Ф17-2 C: ошибки выхода -> причина `drop` (по порядку; прочие `RPCError` -> класс/код ошибки Telegram).
_LEAVE_DROP_ERRORS = (
    ((errors.UserCreatorError,), "creator"),
    ((errors.ChannelInvalidError, errors.ChatIdInvalidError, errors.PeerIdInvalidError), "not_found"),
    ((errors.ChatAdminRequiredError,), "forbidden"),
)

# Ф29-1 C4: ошибки поиска и чтения без вступления -> причина `error` (по порядку; прочие `RPCError` — их
# `message`, код Telegram).
_LOOKUP_ERRORS = (
    ((errors.UsernameNotOccupiedError, errors.UsernameInvalidError), "not_found"),
    ((errors.ChannelPrivateError, errors.ChannelInvalidError), "private"),
    ((errors.QueryTooShortError, errors.SearchQueryEmptyError), "query_too_short"),
)
# Ф29-1 решение 6: кэш ответа поиска по строке — 10 мин; описание и число участников канала — 1 ч.
_LOOKUP_SEARCH_TTL_S = 10 * 60.0
_LOOKUP_FULL_TTL_S = 60 * 60.0
_LOOKUP_CACHE_MAX = 200
_LOOKUP_TEXT_MAX = 2000
_LOOKUP_ABOUT_MAX = 300
_LOOKUP_SEARCH_FETCH_MAX = 40
# Ф32-2 П6: границы `retry_after_s` ответа ядру (`ResultIn.retry_after_s ≤ 86400`).
_LOOKUP_FLOOD_MIN_S = 1
_LOOKUP_FLOOD_MAX_S = 86_400

# Ф14-8 C1: ошибки правки/удаления у всех -> код неудачи (порядок проверки — `_msg_op_error_code`).
_MSG_OP_EXPIRED_ERRORS = (errors.MessageEditTimeExpiredError,)
_MSG_OP_NOT_FOUND_ERRORS = (errors.MessageIdInvalidError,)
_MSG_OP_TOO_LONG_ERRORS = (errors.MessageTooLongError, errors.MediaCaptionTooLongError)
_MSG_OP_FORBIDDEN_ERRORS = (
    errors.MessageAuthorRequiredError,
    errors.MessageDeleteForbiddenError,
    errors.ChatWriteForbiddenError,
    errors.ChatAdminRequiredError,
    errors.ForbiddenError,
)

_CONTACTS_REFRESH_INTERVAL_S = 60 * 60.0  # Ф14-2 B1: контакты — раз в час

_START_BACKOFF_INITIAL_S = 1.0
_START_BACKOFF_CAP_S = 60.0
# Находка F2 (round 2): сетевой `get_me()` в пульсе — не чаще раза в это время (нагрузка); в промежутках
# пульс — только `gateway.is_connected()` (синхронно, без сети) + переподключение, если разъединились.
_HEALTH_NETWORK_CHECK_INTERVAL_S = 10 * 60.0

# Находки F6/F11 (round 2): одна «плохая» переписка (бан, `ChannelPrivateError` и т.п.) не должна ронять
# ВСЮ сверку — наверх из-за одного диалога пробрасываем только то, что касается ВСЕГО цикла: `FloodWaitError`
# (сверка сама ставится на паузу, раздел «Сверка») и потерю входа (`AuthRequired` дальше по каркасу).
_SYNC_ABORT_ERRORS = (errors.FloodWaitError, errors.UnauthorizedError, errors.AuthKeyError)

# Ф8-5 C1: ссылка на файл устарела — перечитать сообщение и повторить один раз.
_FILE_REFERENCE_ERRORS = (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError)
# Ф8-5 C1: доступа к сообщению/файлу больше нет (канал закрыт, бан, файл не найден) — `gone`.
_MEDIA_GONE_ERRORS = (errors.BadRequestError, errors.ForbiddenError)


class ProxyRequired(Exception):
    """`config.proxy` обязателен (план 1.4, раздел «Жизненный цикл» спеки куска 1b) — отсутствует/непонятен
    -> `hub connector` отказывает старту с кодом 78 ДО создания гейтвея (см. `cli.py`)."""


def _msg_op_error_code(exc: BaseException) -> str:
    """Ф14-8 C1: код неудачи правки/удаления по классу ошибки Telegram; прочее — `other`."""
    if isinstance(exc, _MSG_OP_EXPIRED_ERRORS):
        return "expired"
    if isinstance(exc, _MSG_OP_NOT_FOUND_ERRORS):
        return "not_found"
    if isinstance(exc, _MSG_OP_TOO_LONG_ERRORS):
        return "too_long"
    if isinstance(exc, _MSG_OP_FORBIDDEN_ERRORS):
        return "forbidden"
    return "other"


def parse_proxy(config: dict) -> tuple | None:
    """`config["proxy"]`: `"none"` -> без прокси (`None` для `TelegramClient`); `"socks5://host:port"` ->
    кортеж `(тип, host, port)`, как ждёт Telethon/`python-socks` (`_parse_proxy`, `network/connection.py`).
    Ключа нет или он пуст -> `ProxyRequired`."""
    raw = config.get("proxy") if config else None
    if not raw:
        raise ProxyRequired("config.proxy обязателен: 'socks5://host:port' или явное 'none'")
    if raw == "none":
        return None
    parsed = urlsplit(raw)
    if parsed.scheme != "socks5" or not parsed.hostname or not parsed.port:
        raise ProxyRequired(f"config.proxy непонятен: {raw!r} (ожидается socks5://host:port или 'none')")
    return ("socks5", parsed.hostname, parsed.port)


def _missing_in_range(
    core_ids: list[str], fetched_ext_ids: set[str], fetched_ids: list[int], *, limit: int
) -> list[str]:
    """Раздел «Сверка», п.3, находка 1 приёмки куска 1b: ложные удаления. Ядро держит `limit` последних
    ext_id, источник отдал СВОИ последние `limit` — если у источника с момента, когда core в последний раз
    видело канал, появились сообщения новее (живые/догнанные, ещё не отправленные в core), самые старые
    ext_id окна ядра просто не попадают в выборку источника и раньше ошибочно считались удалёнными.

    Правка: удалёнными считаются только ext_id ядра в диапазоне `[min(выборка источника), max(выборка
    источника)]`; если источник вернул МЕНЬШЕ лимита (короче окна — источник отдал всю историю, что у него
    есть) — нижняя граница 0, верхней нет вовсе (правда отдано всё). Нечисловые ext_id не трогаем — диапазон
    для них не определён, дифф пропускает их целиком."""
    if not fetched_ids:
        lo, hi = 0, None
    elif len(fetched_ids) < limit:
        lo, hi = 0, None
    else:
        lo, hi = min(fetched_ids), max(fetched_ids)

    missing = []
    for eid in core_ids:
        if not eid.isdigit():
            continue
        eid_int = int(eid)
        if eid_int < lo:
            continue
        if hi is not None and eid_int > hi:
            continue
        if eid not in fetched_ext_ids:
            missing.append(eid)
    return missing


def _tg_type(entity: object) -> str:
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return "group"
    if isinstance(entity, types.Channel):
        if getattr(entity, "forum", False):
            return "forum"
        return "megagroup" if entity.megagroup else "broadcast"
    return "user"


_VS16 = chr(0xFE0F)


def _telegram_reaction_ops(ops: object) -> list[tuple[str, bool]]:
    """Операции задания `[[emoji, on], …]` -> `[(emoji без U+FE0F, on)]`, мусор отброшен."""
    if not isinstance(ops, list):
        return []
    return [
        (op[0].replace(_VS16, ""), op[1])
        for op in ops
        if isinstance(op, list | tuple)
        and len(op) == 2  # noqa: PLR2004
        and isinstance(op[0], str)
        and op[0].replace(_VS16, "")
        and isinstance(op[1], bool)
    ]


def _owner_reacted(raw_reactions: object | None, current: frozenset[str], job: dict) -> bool:
    """Ф15-1 П1: строка сессии — среди МОИХ реакций сообщения есть не сессионная: эмодзи не из
    `session_emojis` задания (пометки ядра + эмодзи операций `on` строки) или моя кастомная/платная (у неё
    нет эмодзи). Тогда владелец сам занялся сообщением — операции сессии не применяются."""
    marked = job.get("session_emojis")
    session = frozenset(
        e.replace(_VS16, "") for e in (marked if isinstance(marked, list) else []) if isinstance(e, str)
    )
    if current - session:
        return True
    if raw_reactions is None or getattr(raw_reactions, "min", False):
        return False
    for item in getattr(raw_reactions, "results", None) or []:
        if getattr(item, "chosen_order", None) is not None and not isinstance(
            getattr(item, "reaction", None), types.ReactionEmoji
        ):
            return True
    return False


def _telegram_reaction_final(ops: list[tuple[str, bool]], current: frozenset[str]) -> frozenset[str]:
    """Ревью Ф10-3 (заменяет правило П1): операции по порядку от ТЕКУЩЕГО состояния источника — `on X` ->
    {X}; `off X` -> {} только если состояние == {X}, иначе без изменений."""
    state = current
    for emoji, on in ops:
        if on:
            state = frozenset({emoji})
        elif state == frozenset({emoji}):
            state = frozenset()
    return state


class TelegramConnector(ConnectorBase):
    source = "telegram"
    sends_enabled = True  # кусок 1c
    idempotent = True  # random_id — MTProto сам схлопывает повтор (раздел «Отправка», решение 11.5)
    lease_ms = 120_000  # раздел «Отправка»: лиз Telegram — 120 с
    read_ack_enabled = True  # Ф8-7: прочитанное в хабе -> прочитано в Telegram
    reactions_enabled = True  # Ф10-3: реакции владельца из хаба -> SendReaction
    msg_ops_enabled = True  # Ф14-8: правка и «удалить у всех» своих сообщений из хаба
    join_enabled = True  # Ф17-1: вступление в группы и каналы по заявкам сессий
    press_enabled = True  # Ф44-1c: нажатия callback-кнопок ботов
    leave_enabled = True  # Ф17-2: выход из групп и каналов по заявкам (кнопка владельца, просьба сессии)
    lookup_enabled = True  # Ф29-1: поиск и чтение публичных чатов без вступления для сессий

    def __init__(
        self,
        client,
        gateway: Gateway,
        *,
        account_id: int,
        state_path: str | Path | None = None,
        media_tmp_dir: str | Path | None = None,
        views_refresh: bool = False,
        phone_daily: int = PHONE_DAILY_DEFAULT,
        **base_kwargs,
    ) -> None:
        super().__init__(client, **base_kwargs)
        self._gateway = gateway
        self._account_id = account_id
        # Ф8-5 C1/П4: загрузчик медиа — своя задача; временные файлы — `<data_dir>/media/.tmp-conn/<id>/`.
        # `None` — загрузчик не запускается (тесты приёма/отправки, которым он безразличен).
        self._media: MediaDownloader | None = None
        self._media_task: asyncio.Task | None = None
        if media_tmp_dir is not None:
            self._media = MediaDownloader(
                client,
                self.fetch_media,
                tmp_dir=media_tmp_dir,
                mono=lambda: self._mono(),
                sleep=lambda s: self._sleep(s),
                is_blocked=lambda: self.auth_required,
                on_auth_required=self._on_auth_required,
                on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
            )
        # Ф10-4 B1: временные файлы отправки с файлом — тот же каталог, что у загрузчика медиа.
        self.outbox_tmp_dir = Path(media_tmp_dir) if media_tmp_dir is not None else None
        # Алерт о новой сессии (раздел «Алерт»): `<data_dir>/telegram/<id>/state.json`, 0600. `None` —
        # тесты каркаса приёма (1a/1b), которым эта возможность безразлична, не заводят файловую систему.
        self._state_path = Path(state_path) if state_path is not None else None

        self._me_id: int | None = None
        self._entities: dict[int, object] = {}
        self._topic_titles: dict[str, dict[int, str]] = {}
        self._last_attrs: dict[str, dict] = {}
        # Ф11-7: последний отправленный ядру `peer_read` по беседе — сверка шлёт маркер, только если он вырос
        # (после рестарта пусто: первая сверка пришлёт всё, ядро лишнее отбросит как no-op).
        self._peer_read_sent: dict[str, int] = {}

        self._folders_cache: list | None = None
        self._folders_last_refresh: float = float("-inf")
        self._folders_changed = True  # первая сверка всегда пересчитывает
        self._folders_dirty = True

        self._gateway_events: asyncio.Queue = asyncio.Queue()
        self._live_task: asyncio.Task | None = None
        self._last_health_network_check: float = float("-inf")  # находка F2: сетевой get_me — раз в 10 мин

        # Лимитер (план 4.1, раздел «Отправка»): скользящее окно 20/мин на аккаунт (настенные метки, мс),
        # не чаще 1/сек на чат (последняя отправка по базовому chat_ext_id, без учёта топика — тот же peer).
        self._send_times: deque[int] = deque()
        self._chat_last_send_ms: dict[str, int] = {}

        self._last_alert_check_wall: float = float("-inf")  # раздел «Алерт»: раз в сутки + на старте

        # Ф14-2 B1: контакты -> `tg_contacts` (на старте и раз в час, монотонные часы каркаса); `hash`
        # прошлого ответа — `ContactsNotModified` без публикации; публикуется только изменившийся список.
        self._contacts_hash = 0
        self._contacts_next_refresh: float = float("-inf")
        self._contacts_published: list[dict] | None = None

        # Ф17-1 C/П4: беседы, в которые только что вступили (`after_join`), — стартовая загрузка при ближайшей
        # сверке (`_run_join_loads`); ключ — ext_id чата, значение — сущность.
        self._join_loads: dict[str, object] = {}
        self._join_load_tries: dict[str, int] = {}
        # Ф17-2 П1: чаты, где владелец не участник (вышел, выгнали), — одна строка журнала на чат за процесс.
        self._not_member_logged: set[str] = set()

        # Ф42-1: догон без потерь. `_gaps` — дыры по чатам (`lo < id < hi` ещё не запрошены), копия того, что
        # лежит в курсоре аккаунта в ядре (сужается и снимается дыра только ПОСЛЕ записи курсора);
        # `_catchup_snap` — снимок позиций `last` до подключения живого потока, по чатам: запись чата ждёт
        # сверки, которая встретит этот чат (нет записи — позиция из свежих `channels_heads`).
        self._gaps: dict[str, list[dict]] = {}
        self._catchup_snap: dict[str, int] = {}
        self._catchup_snap_epoch = 0
        # Позиции, которые видела последняя сверка, — потолок для новых записей снимка; часы последней
        # проверки «был сон» (`_catchup_wake_check`) и её замок.
        self._catchup_seen_heads: dict[str, int] = {}
        self._catchup_clock: tuple[float, float] | None = None
        self._catchup_wake_lock = asyncio.Lock()
        self._catchup_loaded = False
        self._catchup_saved: str | None = None
        # Ф42-3: отметка засева добора истории (значение `_HISTORY_REFILL_SINCE_MS`, с которым засеяно; `None`
        # — не засевалось) и сколько чатов засеяно; обе лежат в курсоре рядом с дырами.
        self._catchup_hist: int | None = None
        self._catchup_hist_chats = 0
        self._catchup_warned: dict[str, float] = {}

        # Ф23-3: часовой проход просмотров постов каналов — своя задача (`_views_loop`). `False` — не
        # запускается (тесты приёма/отправки с мгновенным `sleep` иначе крутили бы его вхолостую); боевой
        # запуск (`hub connector`) включает.
        self._views_refresh = views_refresh
        self._views_task: asyncio.Task | None = None

        # Ф18-2: очередь перечитывания реакций (живой апдейт с `min=True`): чат -> {msg_id -> top_msg_id}.
        self._react_queue: dict[int, dict[int, int | None]] = {}
        self._react_queued = 0
        self._react_task: asyncio.Task | None = None
        self._react_req_times: deque[float] = deque()  # монотонные метки запросов за последнюю минуту
        self._react_chat_last: dict[int, float] = {}  # чат -> монотонная метка последнего запроса
        self._react_dropped = 0
        self._react_dropped_logged: float = float("-inf")

        # Ф29-1 решение 6: кэши поиска и чтения без вступления (монотонные часы каркаса): ответ поиска по
        # `(строка, limit)` и полная информация канала (число участников, описание) по помеченному id.
        self._lookup_search_cache: dict[tuple[str, int], tuple[float, dict]] = {}
        self._lookup_full_cache: dict[int, tuple[float, int | None, str | None]] = {}

        # Ф34-3 П3а: потолок настоящих `ResolvePhone` за 24 ч (`tg_phone_daily`), их метки и пауза — в
        # `state.json` (переживают рестарт; читаются лениво); кэш «цифры номера -> адресат» — только в
        # памяти процесса: повторная выдача той же строки второго запроса не делает.
        self._phone_daily = phone_daily
        self._phone_calls: list[int] | None = None
        self._phone_pause_until_ms = 0
        self._phone_cache: dict[str, tuple] = {}

    # --- жизненный цикл ---------------------------------------------------------------------------------

    async def start_source(self) -> None:
        backoff = _START_BACKOFF_INITIAL_S
        while True:
            try:
                await self._gateway.connect()
            except Exception as exc:  # noqa: BLE001 — сеть/прокси лежат, это НЕ исключение (раздел «ЖЦ»)
                with contextlib.suppress(Exception):
                    await self._client.set_status("no_network", last_error=str(exc))
                await self._sleep(backoff)
                backoff = min(backoff * 2, _START_BACKOFF_CAP_S)
                continue
            break

        if not await self._gateway.is_authorized():
            raise AuthRequired("сессия Telegram не авторизована")

        try:
            me = await self._gateway.get_me()
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        if me is None:
            # Находка F1 (round 2): раньше `client.get_me()` сам глотал `UnauthorizedError` и возвращал
            # `None` — потеря входа не обнаруживалась вовсе; сырой `gateway.get_me()` либо бросает исключение
            # (поймано строчкой выше), либо (по контракту) отдаёт `None`, только если запрос НИЧЕГО не вернул.
            raise AuthRequired("get_me() вернул пусто — сессия потеряна")
        self._me_id = me.id
        await self._catchup_snapshot()  # Ф42-1 D: позиции — ДО живого потока этого подключения
        self._gateway.subscribe(self._gateway_events.put_nowait)
        self._live_task = asyncio.ensure_future(self._live_updates_loop())
        if self._media is not None:
            self._media_task = asyncio.ensure_future(self._media.run())
        if self._views_refresh and self._views_task is None:
            self._views_task = asyncio.ensure_future(self._views_loop())
        await self._check_session_alert()  # раздел «Алерт»: «раз в сутки И НА СТАРТЕ»

    async def stop_source(self) -> None:
        if self._react_task is not None:
            self._react_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._react_task
            self._react_task = None
        if self._views_task is not None:
            self._views_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._views_task
            self._views_task = None
        if self._media_task is not None:
            self._media_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._media_task
            self._media_task = None
        if self._live_task is not None:
            self._live_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._live_task
            self._live_task = None
        with contextlib.suppress(Exception):
            await self._gateway.disconnect()

    async def on_resume(self) -> None:
        await self._catchup_snapshot()  # Ф42-1 D: до переподключения
        with contextlib.suppress(Exception):
            await self._gateway.disconnect()
        await self._gateway.connect()

    async def health(self) -> None:
        """Находка F2 (round 2): после исчерпания `connection_retries` Telethon остаётся отключённым
        НАВСЕГДА — `connect()` раньше звался только на старте и раз в `on_resume`; сбой ТАМ (сеть ещё не
        поднялась) означал, что никто и никогда больше не попробует снова. Пульс сам проверяет
        `is_connected()` (синхронно, без сети) и переподключается; удачное переподключение просит внеочередную
        сверку (`"reconnect"`) — то, что коннектор пропустил за время разрыва, дальше ловит она же. Сетевой
        `get_me()` (дорогой, дальше — 10 мин между вызовами) сверх этого нужен только чтобы поймать «сессия
        отозвана, а транспорт всё ещё соединён» — иначе такая потеря входа обнаружилась бы только на первой
        неудачной сверке/отправке."""
        if not self._gateway.is_connected():
            await self._catchup_snapshot()  # Ф42-1 D: до `connect()` — живые события ещё не идут
            try:
                await self._gateway.connect()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await self._client.set_status("no_network", last_error=str(exc))
                raise
            self._request_sync("reconnect")
            return

        # Раздел «Алерт»: раз в сутки, по НАСТЕННЫМ часам (не mono — интервал в сутках, не про длительность
        # одного цикла); best-effort — падение здесь не должно портить обычный пульс/переподключение.
        if self._wall() - self._last_alert_check_wall >= _SESSION_ALERT_INTERVAL_S:
            await self._check_session_alert()

        now = self._mono()
        if now - self._last_health_network_check < _HEALTH_NETWORK_CHECK_INTERVAL_S:
            return
        self._last_health_network_check = now

        try:
            me = await self._gateway.get_me()
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error=str(exc))
            raise
        else:
            if me is None:
                raise AuthRequired("get_me() вернул пусто — сессия потеряна")

    # --- отправка (кусок 1c, раздел «Отправка» спеки) -------------------------------------------------

    async def send(self, lease: dict) -> SendResult:
        # Ф12-1 П8: беседа-заготовка — ДО `split_channel_ext_id` и до ветки файла.
        if _is_new_chat(lease):
            return await self._send_new(lease)
        files = lease_files_meta(lease)
        # Длина > 4096 (UTF-16 code units — единица лимита Telegram), подпись > 1024, смешанный альбом ->
        # permanent, БЕЗ запроса.
        invalid = _check_before_send(lease, files)
        if invalid is not None:
            return invalid
        chat_ext, topic_id = convert.split_channel_ext_id(lease["channel_ext_id"])
        peer, pre_err = await self._guarded(self._resolve_peer(int(chat_ext)))
        if pre_err is not None:
            # Ф10-4 П3: у файла сбой до `SendMedia` — `temporary`; текст (идемпотентный) — как было.
            return _before_send(pre_err) if files else pre_err
        if peer is None:
            # Снова промах после прогрева кэша полным проходом диалогов — точно не отправлено.
            return SendResult("temporary", error="peer_unresolved")
        return await self._send_to_peer(lease, files, peer=peer, chat_ext=chat_ext, topic_id=topic_id)

    async def _send_to_peer(
        self, lease: dict, files: list[dict], *, peer: object, chat_ext: str, topic_id: int | None
    ) -> SendResult:
        """Отправка на УЖЕ разрешённый peer (Ф14-2 B3: общий путь обычной и новой беседы): 2–10 файлов —
        альбом (Ф11-3b B2), один — файл (Ф10-4 B2), иначе текст."""
        if len(files) >= 2:
            return await self._send_album(lease, files, peer=peer, chat_ext=chat_ext, topic_id=topic_id)
        if files:
            return await self._send_file(lease, files[0], peer=peer, chat_ext=chat_ext, topic_id=topic_id)
        return await self._send_text(lease, peer=peer, chat_ext=chat_ext, topic_id=topic_id)

    def _peer_flood(self) -> SendResult:
        """Ф69-1 B + П3, П5: спам-ограничение Telegram (PeerFlood). Коннектор сам `limited` НЕ ставит:
        строка — `permanent` с `error="PeerFlood"`, ядро решает, что остановить (первые сообщения новым людям
        или весь аккаунт); остаток пачки — в очередь без паузы аккаунта (`stop_batch(STOP_PEER_FLOOD)`)."""
        _logger.warning("connector: PeerFlood — Telegram не дал написать (спам-ограничение)")
        self.stop_batch(STOP_PEER_FLOOD)
        return SendResult("permanent", error="PeerFlood")

    async def _send_new(self, lease: dict) -> SendResult:
        """Ф12-1 B2 + Ф14-2 B2/B3: первое сообщение новому человеку — по @username (`tg_username`) или по id
        пользователя (`tg_user`, `_resolve_new`). Разрешённый адресат -> тот же путь, что у обычной беседы:
        текст, файл или альбом (тот же `random_id`, `maybe_sent` -> поиск в истории ЭТОГО peer). Нет такого
        адресата -> `permanent`; `FloodWait`/сеть при разрешении -> `temporary` (отправки не было).
        `sent` несёт `peer_ext_id` (id беседы в формате `channel_ext_id` личек) и `peer_title`."""
        files = lease_files_meta(lease)
        invalid = _check_before_send(lease, files)
        if invalid is not None:
            return invalid
        resolved, pre_err = await self._resolve_new(lease)
        new_to = lease.get("new_to")
        by_message = isinstance(new_to, dict) and new_to.get("kind") == "tg_msg_author"
        if pre_err is not None:
            if by_message and lease.get("maybe_sent") and pre_err.outcome == "permanent":
                # Ф46-1 (ревью): прошлая попытка могла дойти, а сообщение группы с тех пор удалили — адресата
                # не разрешить и историю лички не проверить. «Не ушло» объявлять нельзя: исход неясен.
                return SendResult("unknown")
            return pre_err
        peer, peer_ext, peer_title, peer_bot = resolved
        result = await self._send_to_peer(lease, files, peer=peer, chat_ext=peer_ext, topic_id=None)
        if result.outcome == "permanent" and result.error in _PRIVACY_ERRORS:
            return SendResult("permanent", error=_PRIVACY_TEXT)
        if result.outcome == "permanent" and result.error == "PeerIdInvalidError" and by_message:
            # Ф46-1: доступ к автору через сообщение группы не сработал уже на отправке.
            return SendResult("permanent", error=_AUTHOR_MSG_GONE_TEXT)
        if result.outcome == "sent":
            return result._replace(peer_ext_id=peer_ext, peer_title=peer_title, peer_bot=peer_bot or None)
        return result

    async def _resolve_new(self, lease: dict) -> tuple[tuple | None, SendResult | None]:
        """Адресат беседы-заготовки -> `((peer, peer_ext, peer_title, bot), None)` или `(None, SendResult)` —
        отправки не было. `bot` (Ф63-2 П2) — адресат по @username оказался ботом (`entity.bot`).

        Ф63-2 П3: @ник на «bot» (предварительный признак беседы с ботом, по нему ядро могло пропустить
        черновик без владельца) сверяется с `entity.bot` БЕЗУСЛОВНО и на КАЖДОЙ попытке (и после `maybe_sent`, и в
        `reconcile`): не бот -> отправки нет, `permanent` «@… — не бот»."""
        new_to = lease.get("new_to")
        kind = new_to.get("kind") if isinstance(new_to, dict) else None
        if kind in ("tg_user", "tg_phone", "tg_msg_author"):
            if kind == "tg_user":
                resolved, err = await self._resolve_tg_user(new_to)
            elif kind == "tg_phone":
                resolved, err = await self._resolve_tg_phone(new_to)
            else:
                resolved, err = await self._resolve_tg_msg_author(new_to)
            return ((*resolved, False) if resolved is not None else None), err
        username = _new_username(lease)
        if username is None:
            return None, SendResult("permanent", error="новая беседа без @username")
        entity, err = await self._resolve_username(username)
        if err is not None:
            return None, err
        is_bot = bool(getattr(entity, "bot", False))
        if username.lower().endswith("bot") and not is_bot:
            return None, SendResult("permanent", error=f"@{username} — не бот")
        return (
            utils.get_input_peer(entity),
            str(utils.get_peer_id(entity)),
            convert.display_title(entity, me_id=self._me_id),
            is_bot,
        ), None

    async def _resolve_tg_user(self, new_to: dict) -> tuple[tuple | None, SendResult | None]:
        """Ф14-2 B2: id пользователя -> peer. Кэш сущностей сессии (`get_input_entity`) -> промах и есть
        `username` -> `contacts.ResolveUsername` (только человек и только ЭТОТ id — иначе `permanent`, мимо
        выбранного адресата не шлём) -> иначе ОДИН `contacts.GetContacts(hash=0)` (сущности контактов
        попадают в кэш) и снова кэш -> иначе `permanent` «контакт не найден»."""
        user_id = _new_user_id(new_to)
        if user_id is None:
            return None, SendResult("permanent", error="новая беседа без адресата")
        raw_username = new_to.get("username")
        username = raw_username if isinstance(raw_username, str) and raw_username else None
        raw_name = new_to.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        title = name or (f"@{username}" if username else None)
        peer_ext = str(user_id)

        peer, err = await self._cached_user_peer(user_id)
        if err is not None:
            return None, err
        if peer is not None:
            return (peer, peer_ext, title), None
        if username is not None:
            entity, err = await self._resolve_username(username)
            if err is not None:
                return None, err
            if entity.id != user_id:
                return None, SendResult("permanent", error=f"@{username} теперь у другого человека")
            return (utils.get_input_peer(entity), peer_ext, title), None
        _contacts, err = await self._resolve_call(self._gateway.get_contacts(0))
        if err is not None:
            return None, err
        peer, err = await self._cached_user_peer(user_id)
        if err is not None:
            return None, err
        if peer is not None:
            return (peer, peer_ext, title), None
        return None, SendResult("permanent", error=_CONTACT_NOT_FOUND_TEXT)

    async def _resolve_tg_msg_author(self, new_to: dict) -> tuple[tuple | None, SendResult | None]:
        """Ф46-1 (раздел 4 + П7, П8): автор сообщения группы -> peer. Кэш сессии (`get_input_entity`) ->
        иначе доступ через сообщение: peer чата (кэш -> один проход диалогов) и ОДИН
        `users.GetUsers([InputUserFromMessage])`: полный пользователь -> обычный `InputPeerUser`,
        min-пользователь -> `InputPeerUserFromMessage`. Только чтение (и `reconcile` зовёт без счётчиков).
        Обычная группа (не супергруппа) — только кэш. В журнал — одно слово-ветка, без id и имени."""
        ref = _new_author_ref(new_to)
        if ref is None:
            return None, SendResult("permanent", error="bad_ref")
        user_id, chat_ext, msg_id = ref
        raw_name = new_to.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        peer_ext = str(user_id)

        if self._me_id is not None and user_id == self._me_id:
            return None, SendResult("permanent", error=_AUTHOR_SELF_TEXT)
        peer, err = await self._cached_user_peer(user_id)
        if err is not None:
            return None, err
        if peer is not None:
            _logger.info("connector: автор сообщения группы — cache")
            return (peer, peer_ext, name), None
        if not chat_ext.startswith(_SUPERGROUP_PREFIX):
            _logger.info("connector: автор сообщения группы — basic_group")
            return None, SendResult("permanent", error=_AUTHOR_BASIC_GROUP_TEXT)
        chat_peer, err = await self._resolve_call(self._resolve_peer(int(chat_ext)))
        if err is not None:
            return None, err
        if chat_peer is None:
            _logger.info("connector: автор сообщения группы — no_chat")
            return None, SendResult("permanent", error=_AUTHOR_CHAT_GONE_TEXT)
        try:
            user = await self._gateway.get_user_from_message(chat_peer, msg_id, user_id)
        except _AUTHOR_MSG_GONE_ERRORS as exc:
            _logger.info("connector: автор сообщения группы — %s", type(exc).__name__)
            return None, SendResult("permanent", error=_AUTHOR_MSG_GONE_TEXT)
        except errors.FloodWaitError as exc:
            return None, SendResult("temporary", error="FloodWait", retry_after_ms=exc.seconds * 1000)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(type(exc).__name__) from None
        except (OSError, errors.ServerError, errors.TimedOutError):
            return None, SendResult("temporary", error="network")
        except errors.RPCError as exc:
            # П7в: прочий отказ сервера (в том числе любой `BadRequestError`) — не повторять: имя класса.
            _logger.info("connector: автор сообщения группы — %s", type(exc).__name__)
            return None, SendResult("permanent", error=type(exc).__name__)
        if not isinstance(user, types.User) or user.id != user_id:
            # Пустой ответ, `UserEmpty` или не тот человек — как «сообщение недоступно».
            _logger.info("connector: автор сообщения группы — empty")
            return None, SendResult("permanent", error=_AUTHOR_MSG_GONE_TEXT)
        if user.is_self:
            return None, SendResult("permanent", error=_AUTHOR_SELF_TEXT)
        if user.deleted:
            return None, SendResult("permanent", error=_AUTHOR_DELETED_TEXT)
        if user.bot:
            return None, SendResult("permanent", error=_AUTHOR_BOT_TEXT)
        if user.min or user.access_hash is None:
            # min-сущность: её `access_hash` для запросов не годится — доступ остаётся «через сообщение».
            branch = "min"
            peer = types.InputPeerUserFromMessage(peer=chat_peer, msg_id=msg_id, user_id=user_id)
        else:
            # П7г: полный пользователь — ВСЕГДА обычный peer (история и `maybe_sent` идут им).
            branch = "full"
            peer = types.InputPeerUser(user_id=user.id, access_hash=user.access_hash)
        _logger.info("connector: автор сообщения группы — %s", branch)
        title = convert.display_title(user, me_id=self._me_id) or name
        return (peer, peer_ext, title), None

    async def _resolve_tg_phone(self, new_to: dict) -> tuple[tuple | None, SendResult | None]:
        """Ф34-3 решение 2 + П3а, П7: номер телефона -> peer. ОДИН `contacts.GetContacts(hash=0)` (у своих
        контактов телефон виден; сравнение — только цифры) -> кэш процесса -> лимит и пауза (`state.json`) ->
        `contacts.ResolvePhone` (цифры; в контакты никого не добавляет). Свой номер — `permanent`. Тексты
        ошибок и журнал — без номера."""
        digits = _new_phone_digits(new_to)
        if digits is None:
            return None, SendResult("permanent", error="новая беседа без номера телефона")
        contacts, err = await self._resolve_call(self._gateway.get_contacts(0))
        if err is not None:
            return None, err
        for user in getattr(contacts, "users", None) or []:
            if not isinstance(user, types.User) or not user.phone:
                continue
            if "".join(ch for ch in str(user.phone) if ch.isdigit()) == digits:
                return self._phone_peer(user)
        cached = self._phone_cache.get(digits)
        if cached is not None:
            return cached, None
        if self._phone_daily <= 0:  # ревью Ф34-3: выключено — не ждать сутками, сразу отказ
            return None, SendResult("permanent", error=_PHONE_OFF_TEXT)
        wait_ms, text = self._phone_wait()
        if wait_ms is not None:
            # Спека П3а называет `flood`, но `flood` в ядре ставит на паузу ВЕСЬ аккаунт
            # (`account.flood_until`) — обычная переписка встала бы до суток; здесь ждёт только эта строка,
            # и попытка не тратится (`postponed`: запроса не было).
            return None, SendResult("temporary", error=text, retry_after_ms=wait_ms, postponed=True)
        self._phone_note_call()
        try:
            entity = await self._gateway.resolve_phone(digits)
        except (errors.PhoneNotOccupiedError, errors.PhoneNumberInvalidError, ValueError):
            return None, SendResult("permanent", error=_PHONE_NOT_FOUND_TEXT)
        except errors.FloodError as exc:
            seconds = getattr(exc, "seconds", None)
            if isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds > 0:
                self._phone_set_pause(int(seconds * 1000))
                _logger.warning("connector: FloodWait на поиск по номеру телефона, пауза %s с", int(seconds))
                return None, SendResult(
                    "temporary", error="FloodWait", retry_after_ms=int(seconds * 1000), postponed=True
                )
            # Подкласс `FloodError` без `.seconds` — не пауза: отказ с именем класса (как `_lookup_error`).
            return None, SendResult("permanent", error=type(exc).__name__)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(type(exc).__name__) from None
        except (OSError, errors.ServerError, errors.TimedOutError):
            return None, SendResult("temporary", error="network")
        except errors.RPCError as exc:
            # Только имя класса: сам запрос (`exc.request`) несёт номер.
            return None, SendResult("permanent", error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — текст чужого исключения может нести номер: наружу только имя
            _logger.warning("connector: поиск по номеру телефона упал: %s", type(exc).__name__)
            return None, SendResult("temporary", error=type(exc).__name__)
        if not isinstance(entity, types.User):
            return None, SendResult("permanent", error="по номеру найден не человек")
        resolved, err = self._phone_peer(entity)
        if resolved is not None:
            self._phone_cache[digits] = resolved
        return resolved, err

    def _phone_peer(self, user: types.User) -> tuple[tuple | None, SendResult | None]:
        """Найденный по номеру человек -> адресат; П7: сам владелец — `permanent` (иначе — в «Избранное»)."""
        if user.is_self or (self._me_id is not None and user.id == self._me_id):
            return None, SendResult("permanent", error=_PHONE_SELF_TEXT)
        return (
            utils.get_input_peer(user),
            str(utils.get_peer_id(user)),
            convert.display_title(user, me_id=self._me_id),
        ), None

    def _phone_load(self, now_ms: int) -> list[int]:
        """Метки настоящих `ResolvePhone` (мс) за последние 24 ч — из `state.json` при первом обращении.
        Файл есть, но не читается — счёт не обнуляется: считаем лимит выбранным на сутки вперёд."""
        if self._phone_calls is None:
            state, corrupt = self._load_alert_state()
            part = state.get(_PHONE_STATE_KEY) if isinstance(state, dict) else None
            part = part if isinstance(part, dict) else {}
            calls = part.get("calls") if isinstance(part.get("calls"), list) else []
            self._phone_calls = [c for c in calls if isinstance(c, int) and not isinstance(c, bool)]
            pause = part.get("pause_until_ms")
            ok = isinstance(pause, int) and not isinstance(pause, bool)
            self._phone_pause_until_ms = pause if ok else 0
            if corrupt:
                _logger.error("connector: state.json повреждён — поиск по номеру на паузе на сутки")
                self._phone_pause_until_ms = now_ms + _PHONE_WINDOW_MS
        # Метки «из будущего» (часы перевели назад) остаются в счёте — лимит не обнуляется.
        self._phone_calls = [c for c in self._phone_calls if c > now_ms - _PHONE_WINDOW_MS]
        return self._phone_calls

    def _phone_wait(self) -> tuple[int | None, str | None]:
        """`(сколько мс ждать, причина)` — пауза после FloodWait или выбранный лимит окна; `(None, None)` —
        можно делать запрос."""
        now_ms = int(self._wall() * 1000)
        calls = self._phone_load(now_ms)
        if self._phone_pause_until_ms > now_ms:
            return self._phone_pause_until_ms - now_ms, _PHONE_PAUSE_TEXT
        if len(calls) >= self._phone_daily:
            # Место освободится, когда из окна выйдет вызов, лишний сверх `daily - 1` самых свежих.
            leaving = sorted(calls)[len(calls) - self._phone_daily]
            return max(leaving + _PHONE_WINDOW_MS - now_ms, 1000), _PHONE_LIMIT_TEXT
        return None, None

    def _phone_note_call(self) -> None:
        """Запрос считается ДО отправки (и неудачный, и оборванный сетью — тоже вызов)."""
        now_ms = int(self._wall() * 1000)
        self._phone_load(now_ms).append(now_ms)
        self._phone_save()

    def _phone_set_pause(self, wait_ms: int) -> None:
        now_ms = int(self._wall() * 1000)
        self._phone_load(now_ms)
        self._phone_pause_until_ms = now_ms + wait_ms
        self._phone_save()

    def _phone_save(self) -> None:
        part = {"calls": self._phone_calls or [], "pause_until_ms": self._phone_pause_until_ms}
        self._merge_state({_PHONE_STATE_KEY: part})

    async def _cached_user_peer(self, user_id: int) -> tuple[object | None, SendResult | None]:
        """`get_input_entity(user_id)` — пользователь из кэша сессии; промах -> `(None, None)`."""
        try:
            peer = await self._gateway.get_input_entity(user_id)
        except ValueError, TypeError:
            # Ф46-1 П7а: `TypeError` — сервер вернул min-пользователя (без годного `access_hash`): промах.
            return None, None
        except errors.FloodWaitError as exc:
            return None, SendResult("temporary", error="FloodWait", retry_after_ms=exc.seconds * 1000)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError):
            return None, SendResult("temporary", error="network")
        except errors.RPCError:
            return None, None
        if not isinstance(peer, types.InputPeerUser) or peer.user_id != user_id:
            return None, None
        return peer, None

    async def _resolve_call(self, coro) -> tuple[object | None, SendResult | None]:
        """Запрос разрешения адресата (не отправка): ошибки — как у `_resolve_username`, но ответ сервера с
        ошибкой — `temporary` (адресат тут ни при чём)."""
        try:
            return await coro, None
        except errors.FloodWaitError as exc:
            return None, SendResult("temporary", error="FloodWait", retry_after_ms=exc.seconds * 1000)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError):
            return None, SendResult("temporary", error="network")
        except errors.RPCError as exc:
            return None, SendResult("temporary", error=exc.message or type(exc).__name__)

    async def _resolve_username(self, username: str) -> tuple[object | None, SendResult | None]:
        """Ф12-1 B2: @username -> сущность пользователя или `(None, SendResult)` — отправки не было."""
        try:
            entity = await self._gateway.resolve_username(username)
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
            return None, SendResult("permanent", error=f"нет такого @username: @{username}")
        except errors.FloodWaitError as exc:
            return None, SendResult("temporary", error="FloodWait", retry_after_ms=exc.seconds * 1000)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError):
            return None, SendResult("temporary", error="network")
        except errors.RPCError as exc:
            return None, SendResult("permanent", error=f"@{username}: {exc.message or type(exc).__name__}")
        if not isinstance(entity, types.User):
            return None, SendResult("permanent", error=f"@{username} — не человек, а канал/группа")
        return entity, None

    async def _send_text(
        self, lease: dict, *, peer: object, chat_ext: str, topic_id: int | None
    ) -> SendResult:
        """Отправка текста на уже разрешённый peer (кусок 1c; общая для обычной беседы и новой, Ф12-1)."""
        text = lease["text"]
        reply_to_ext_id = lease["reply_to_ext_id"]
        send_token = lease["send_token"]
        send_started_ts = lease["send_started_ts"]
        # Находка F1(а/б) критика куска 1c: `maybe_sent` (не `attempts > 1`!) — единственный сигнал «а мог
        # ли этот лиз реально уйти» (ставится core ТОЛЬКО в ветке истёкшего лиза). После честного
        # temporary/flood запрос точно не уходил — искать в истории незачем и опасно (найдём чужое «+»).
        # `known_own_ext_ids` — уже чьи-то (свои) сообщения этого канала; поиск обязан их пропускать.
        maybe_sent = bool(lease.get("maybe_sent", False))
        exclude_ext_ids = frozenset(lease.get("known_own_ext_ids") or ())

        random_id = convert.random_id_from_token(send_token)
        since_ms = send_started_ts - _ECHO_HISTORY_MARGIN_MS

        if maybe_sent:
            # Повторная попытка ПОСЛЕ истёкшего лиза (падение/abandon/таймаут каркаса) — СНАЧАЛА поиск в
            # истории (решение 11.5: срок памяти Telegram о random_id не документирован — искать надёжнее).
            found, pre_err = await self._guarded(
                self._find_in_history(
                    peer, topic_id, text, since_ms=since_ms, limit=_HISTORY_SEARCH_LIMIT,
                    exclude_ext_ids=exclude_ext_ids,
                )
            )
            if pre_err is not None:
                return pre_err
            if found is not None:
                return SendResult("sent", ext_id=found)

        limited = await self._acquire_send_slot(chat_ext)
        if limited is not None:
            return limited

        reply_to = convert.build_reply_to(topic_id=topic_id, reply_to_ext_id=reply_to_ext_id)

        try:
            result = await self._gateway.send_message(
                peer, message=text, random_id=random_id, reply_to=reply_to
            )
        except errors.RandomIdDuplicateError:
            # Telethon уже сам повторил его 5 раз (11.5) — доказательство «уже ушло»: искать пошире и НЕ
            # повторять отправку вслепую (она ушла точно).
            found, post_err = await self._guarded(
                self._find_in_history(
                    peer, topic_id, text, since_ms=since_ms, limit=_HISTORY_SEARCH_LIMIT_DUP,
                    exclude_ext_ids=exclude_ext_ids,
                )
            )
            if post_err is not None:
                return post_err
            return SendResult("sent", ext_id=found) if found is not None else SendResult("unknown")
        except errors.FloodWaitError as exc:
            return SendResult("flood", retry_after_ms=exc.seconds * 1000)
        except errors.SlowModeWaitError as exc:
            # Откладывается только этот чат (и, по FIFO лиза, его очередь) — не весь аккаунт.
            return SendResult("temporary", retry_after_ms=exc.seconds * 1000)
        except errors.PeerFloodError:
            return self._peer_flood()
        except _PERMANENT_SEND_ERRORS as exc:
            return SendResult("permanent", error=type(exc).__name__)
        except errors.UserPrivacyRestrictedError:
            # Ф12-1 B2: у этой ошибки Telethon `message` — просто «FORBIDDEN»; различаем по классу (новой
            # беседе `_send_new` заменит имя понятным текстом).
            return SendResult("permanent", error="UserPrivacyRestrictedError")
        except errors.ForbiddenError as exc:
            # Находка F4 критика куска 1c: 403 именные (`ChatSendPlainForbiddenError`, `UserRestrictedError`)
            # уже в `_PERMANENT_SEND_ERRORS`; здесь — прочие (напр. `PRIVACY_PREMIUM_REQUIRED`) по `message`.
            return SendResult("permanent", error=exc.message or type(exc).__name__)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except errors.BadRequestError as exc:
            # TOPIC_CLOSED/ALLOW_PAYMENT_REQUIRED* (нет своих классов в 1.45 — проверено) и любой прочий 400:
            # сервер отказал ДО отправки -> permanent с текстом ошибки.
            return SendResult("permanent", error=exc.message or type(exc).__name__)
        except (OSError, errors.ServerError, errors.TimedOutError):
            # Находка F4: `ServerError`/`TimedOutError` (кроме `RandomIdDuplicateError`, пойман раньше) —
            # сеть/сервер лежит посреди запроса, результат неизвестен, но повтор безопасен (идемпотентный
            # источник) -> abandon, как и сетевая ошибка/таймаут (`OSError`, оба builtin-подкласса).
            return SendResult("abandon")
        # Прочее исключение Telegram (RPCError не из перечисленных выше) — ответ сервера, исход не ясен;
        # пропускаем наверх намеренно — каркас (`ConnectorBase._process_lease`) сам превращает необработанное
        # исключение в `unknown` с полной трассировкой в журнал (контракт раздела 4 плана).

        ext_id = convert.extract_sent_ext_id(result, random_id)
        if ext_id is not None:
            return SendResult("sent", ext_id=ext_id)
        # Ответ без узнаваемого id -> поиск в истории -> не нашли -> unknown (раздел «Отправка»).
        found, post_err = await self._guarded(
            self._find_in_history(
                peer, topic_id, text, since_ms=since_ms, limit=_HISTORY_SEARCH_LIMIT,
                exclude_ext_ids=exclude_ext_ids,
            )
        )
        if post_err is not None:
            return post_err
        return SendResult("sent", ext_id=found) if found is not None else SendResult("unknown")

    async def reconcile(self, lease: dict) -> SendResult:
        """Для Telegram ядро эту ветку не вызывает (аккаунт идемпотентный — `outbox.lease()` всегда даёт
        `mode='send'`), но реализуется честно (раздел «Отправка»): поиск в истории -> `sent`/`unknown`
        (строка с файлом — только своё сообщение с медиа, Ф10-4). Ф12-1 П8: беседа-заготовка — peer по
        @username, ДО `split_channel_ext_id`; найдено -> `sent` с `peer_ext_id`/`peer_title`."""
        channel_ext_id = lease["channel_ext_id"]
        text = lease["text"] or ""
        send_started_ts = lease["send_started_ts"]
        exclude_ext_ids = frozenset(lease.get("known_own_ext_ids") or ())
        new_peer: tuple[str, str] | None = None
        if _is_new_chat(lease):
            # Ф14-2 B2: и `tg_user` — тот же разрешитель, что у отправки (только чтение).
            resolved, err = await self._resolve_new(lease)
            if err is not None:
                return SendResult("unknown")
            peer, peer_ext, peer_title, _bot = resolved
            topic_id = None
            new_peer = (peer_ext, peer_title)
        else:
            chat_ext, topic_id = convert.split_channel_ext_id(channel_ext_id)
            try:
                peer = await self._resolve_peer(int(chat_ext))
            except _AUTH_LOST_ERRORS as exc:
                raise AuthRequired(str(exc)) from exc
            except Exception:  # noqa: BLE001 — reconcile принимает только sent|unknown, исход не ясен -> unknown
                return SendResult("unknown")
            if peer is None:
                return SendResult("unknown")
        files = lease_files_meta(lease)
        try:
            if len(files) >= 2:
                # Ф11-3b B2: строка-альбом — тот же поиск альбома, что у `maybe_sent`.
                album = await self._find_album_in_history(
                    peer,
                    topic_id,
                    text,
                    len(files),
                    since_ms=send_started_ts - _ECHO_HISTORY_MARGIN_MS,
                    exclude_ext_ids=exclude_ext_ids,
                )
                return _album_sent(album) if album is not None else SendResult("unknown")
            found = await self._find_in_history(
                peer,
                topic_id,
                text,
                since_ms=send_started_ts - _ECHO_HISTORY_MARGIN_MS,
                exclude_ext_ids=exclude_ext_ids,
                with_media=bool(files),
            )
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except Exception:  # noqa: BLE001
            return SendResult("unknown")
        if found is None:
            return SendResult("unknown")
        if new_peer is not None:
            return SendResult("sent", ext_id=found, peer_ext_id=new_peer[0], peer_title=new_peer[1])
        return SendResult("sent", ext_id=found)

    # --- отправка файла (Ф10-4 B2, П3, П11) -----------------------------------------------------------------

    async def _send_file(
        self, lease: dict, file: dict, *, peer: object, chat_ext: str, topic_id: int | None
    ) -> SendResult:
        """Ф10-4 B2: одно сообщение с одним файлом, текст — подпись (может быть пустой). Подпись длиннее 1024
        UTF-16 -> `permanent` без запросов (запасной путь: ядро отказывает раньше, П11). `maybe_sent` ->
        сначала поиск своего сообщения С МЕДИА и той же подписью. Затем байты из ядра во временный файл
        (`lease_file`, удаляется при любом исходе) -> `send_media` (`upload_file` + `SendMediaRequest` с
        `random_id` токена). П3: любой сбой до `SendMediaRequest` — `temporary` (или `flood`), не `unknown`.
        П11: фото, не принятое как фото, — ОДИН повтор документом (`force_file`).

        Ф14-2 B3: peer уже разрешён вызывающим (`send` — из `channel_ext_id`, `_send_new` — адресат новой
        беседы); подпись проверена `_check_before_send`.

        Ф14-4 B1–B3: `kind == "voice"` — настоящее голосовое: webm переупаковывается в ogg (`voice.prepared`,
        временные файлы удаляются при любом исходе), длительность — `ffprobe`; ремукс не удался / нет ffmpeg —
        файлом (документ с исходным именем) и предупреждение в журнал. `maybe_sent` — как у файла (выше)."""
        text = lease.get("text") or ""
        maybe_sent = bool(lease.get("maybe_sent", False))
        exclude_ext_ids = frozenset(lease.get("known_own_ext_ids") or ())

        random_id = convert.random_id_from_token(lease["send_token"])
        since_ms = lease["send_started_ts"] - _ECHO_HISTORY_MARGIN_MS

        async def find(limit: int) -> tuple[object | None, SendResult | None]:
            return await self._guarded(
                self._find_in_history(
                    peer, topic_id, text, since_ms=since_ms, limit=limit,
                    exclude_ext_ids=exclude_ext_ids, with_media=True,
                )
            )

        if maybe_sent:
            found, pre_err = await find(_HISTORY_SEARCH_LIMIT)
            if pre_err is not None:
                return _before_send(pre_err)
            if found is not None:
                return SendResult("sent", ext_id=found)

        kind = "photo" if file.get("kind") == "photo" else "document"
        raw_name = file.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else _DEFAULT_FILE_NAME
        mime = file.get("mime") if isinstance(file.get("mime"), str) else None
        reply_to = convert.build_reply_to(topic_id=topic_id, reply_to_ext_id=lease.get("reply_to_ext_id"))

        async def attempt(
            path: Path, media_kind: str, force_file: bool, *, duration: int = 0, as_name: str | None = None,
            other_random_id: int | None = None,
        ) -> SendResult | _RetryAsDocument | _VoiceForbidden:
            return await self._send_media_once(
                peer, path, kind=media_kind, name=as_name or name, mime=mime, text=text,
                random_id=random_id if other_random_id is None else other_random_id,
                reply_to=reply_to, force_file=force_file, find=find, duration=duration,
            )

        try:
            async with self.lease_files(lease) as paths:
                path = paths[0]
                if file.get("kind") == "voice":
                    return await self._send_voice(path, mime, chat_ext, attempt, lease["send_token"])
                limited = await self._acquire_send_slot(chat_ext)
                if limited is not None:
                    return limited
                # Картинка документом (> 10 МиБ у ядра) — `force_file`, иначе Telegram может показать её
                # как фото/стикер (webp).
                result = await attempt(path, kind, kind == "document" and (mime or "").startswith("image/"))
                if isinstance(result, _RetryAsDocument) and kind == "photo":
                    _logger.info("connector: фото не принято (%s) — повтор документом", result.error)
                    result = await attempt(path, "document", True)
                if isinstance(result, _RetryAsDocument):
                    return SendResult("permanent", error=result.error)
                return result
        except OutboxFileError as exc:
            return SendResult("temporary", error=exc.reason)

    async def _send_voice(
        self, path: Path, mime: str | None, chat_ext: str, attempt, send_token: str
    ) -> SendResult:
        """Ф14-4 B1–B3: голосовое из файла задания `path` (webm/ogg). Ремукс/длительность — до слота отправки;
        неудача ремукса — документом с исходным именем (`voice.webm`) и `warning` без имён. Собеседник или
        чат запрещает голосовые — ОДИН повтор документом (исходные байты и имя) с НОВЫМ `random_id`
        (`"<send_token>:doc"`: голосовое не ушло); упал и повтор — обычная классификация."""
        tmp_dir = self.outbox_tmp_dir if self.outbox_tmp_dir is not None else Path(tempfile.gettempdir())
        async with voice_mod.prepared(path, mime, tmp_dir) as ready:
            limited = await self._acquire_send_slot(chat_ext)
            if limited is not None:
                return limited
            if ready is not None:
                ogg_path, duration = ready
                result = await attempt(ogg_path, "voice", False, duration=duration, as_name="voice.ogg")
                if isinstance(result, _VoiceForbidden):
                    _logger.warning("connector: голосовое ушло файлом: собеседник запретил голосовые")
                    doc_random_id = convert.random_id_from_token(f"{send_token}:doc")
                    result = await attempt(path, "document", False, other_random_id=doc_random_id)
            else:
                _logger.warning("connector: голосовое ушло файлом: ремукс не удался")
                result = await attempt(path, "document", False)
            if isinstance(result, (_RetryAsDocument, _VoiceForbidden)):
                return SendResult("permanent", error=result.error)
            return result

    async def _send_media_once(
        self,
        peer: object,
        path: Path,
        *,
        kind: str,
        name: str,
        mime: str | None,
        text: str,
        random_id: int,
        reply_to: object | None,
        force_file: bool,
        find,
        duration: int = 0,
    ) -> SendResult | _RetryAsDocument | _VoiceForbidden:
        """Одна попытка `send_media`. Ошибки запроса — как у текстовой отправки; ошибки П11 ->
        `_RetryAsDocument` (решает вызывающий); сбой `upload_file` (`MediaUploadError`) — до
        `SendMediaRequest`: `flood`/потеря входа/`temporary`."""
        voice_forbidden: tuple[type[Exception], ...] = _VOICE_FORBIDDEN_ERRORS if kind == "voice" else ()
        try:
            result = await self._gateway.send_media(
                peer,
                path=str(path),
                kind=kind,
                name=name,
                mime=mime,
                text=text,
                random_id=random_id,
                reply_to=reply_to,
                force_file=force_file,
                duration=duration,
            )
        except MediaUploadError as exc:
            cause = exc.cause
            if isinstance(cause, errors.FloodWaitError):
                return SendResult("flood", retry_after_ms=cause.seconds * 1000)
            if isinstance(cause, _AUTH_LOST_ERRORS):
                raise AuthRequired(str(cause)) from cause
            return SendResult("temporary", error=type(cause).__name__)
        except voice_forbidden as exc:
            # Ф14-4: только у голосового; у прочих видов (пустой кортеж) — обычная классификация ниже.
            return _VoiceForbidden(type(exc).__name__)
        except errors.RandomIdDuplicateError:
            found, post_err = await find(_HISTORY_SEARCH_LIMIT_DUP)
            if post_err is not None:
                return post_err
            return SendResult("sent", ext_id=found) if found is not None else SendResult("unknown")
        except errors.FloodWaitError as exc:
            return SendResult("flood", retry_after_ms=exc.seconds * 1000)
        except errors.SlowModeWaitError as exc:
            return SendResult("temporary", retry_after_ms=exc.seconds * 1000)
        except errors.PeerFloodError:
            return self._peer_flood()
        except _RETRY_AS_DOCUMENT_ERRORS as exc:
            return _RetryAsDocument(type(exc).__name__)
        except _PERMANENT_SEND_ERRORS as exc:
            return SendResult("permanent", error=type(exc).__name__)
        except errors.UserPrivacyRestrictedError:
            # Ф14-2 B3: как у текста — по классу (новой беседе `_send_new` заменит понятным текстом).
            return SendResult("permanent", error="UserPrivacyRestrictedError")
        except errors.ForbiddenError as exc:
            return SendResult("permanent", error=exc.message or type(exc).__name__)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except errors.BadRequestError as exc:
            return SendResult("permanent", error=exc.message or type(exc).__name__)
        except (OSError, errors.ServerError, errors.TimedOutError):
            return SendResult("abandon")

        ext_id = convert.extract_sent_ext_id(result, random_id)
        if ext_id is not None:
            return SendResult("sent", ext_id=ext_id)
        found, post_err = await find(_HISTORY_SEARCH_LIMIT)
        if post_err is not None:
            return post_err
        return SendResult("sent", ext_id=found) if found is not None else SendResult("unknown")

    # --- альбом (Ф11-3b B2, П1, П4) -----------------------------------------------------------------------

    async def _send_album(
        self, lease: dict, files: list[dict], *, peer: object, chat_ext: str, topic_id: int | None
    ) -> SendResult:
        """Ф11-3b B2: 2–10 файлов одним альбомом (`SendMultiMediaRequest`), текст — подпись элемента 0.
        Подпись длиннее 1024 UTF-16 и смешанный альбом (фото + документы) -> `permanent` без запросов
        (запасной путь: ядро отказывает раньше). `maybe_sent` -> сначала поиск альбома в истории. Затем байты
        всех файлов (`lease_files`) -> лимитер (альбом занимает N мест окна аккаунта) -> `send_album` шлюза.
        Сбой загрузки до отправки -> `temporary`/`flood`/потеря входа; фото не принято (`UploadMediaRequest`
        или сама отправка, П1) -> ОДИН повтор всего альбома документами с теми же `random_id`.

        Ф14-2 B3: peer уже разрешён вызывающим; подпись и однородность альбома проверены
        `_check_before_send`."""
        text = lease.get("text") or ""
        kind = "photo" if all(f.get("kind") == "photo" for f in files) else "document"
        n = len(files)
        maybe_sent = bool(lease.get("maybe_sent", False))
        exclude_ext_ids = frozenset(lease.get("known_own_ext_ids") or ())

        random_ids = _album_random_ids(lease["send_token"], n)
        since_ms = lease["send_started_ts"] - _ECHO_HISTORY_MARGIN_MS

        async def find(limit: int) -> tuple[object | None, SendResult | None]:
            return await self._guarded(
                self._find_album_in_history(
                    peer, topic_id, text, n, since_ms=since_ms, limit=limit, exclude_ext_ids=exclude_ext_ids
                )
            )

        if maybe_sent:
            found, pre_err = await find(_HISTORY_SEARCH_LIMIT)
            if pre_err is not None:
                return _before_send(pre_err)
            if found is not None:
                return _album_sent(found)

        reply_to = convert.build_reply_to(topic_id=topic_id, reply_to_ext_id=lease.get("reply_to_ext_id"))

        def items(paths: list[Path], as_document: bool) -> list[AlbumItem]:
            out: list[AlbumItem] = []
            for path, file in zip(paths, files, strict=True):
                raw_name = file.get("name")
                name = raw_name if isinstance(raw_name, str) and raw_name else _DEFAULT_FILE_NAME
                mime = file.get("mime") if isinstance(file.get("mime"), str) else None
                if as_document or kind == "document":
                    # Картинка документом — `force_file`, иначе Telegram может показать её как фото/стикер.
                    force = as_document or (mime or "").startswith("image/")
                    out.append(AlbumItem(str(path), "document", name, mime, force))
                else:
                    out.append(AlbumItem(str(path), "photo", name, mime, False))
            return out

        try:
            async with self.lease_files(lease) as paths:
                limited = await self._acquire_send_slot(chat_ext, slots=n)
                if limited is not None:
                    return limited
                result = await self._send_album_once(
                    peer, items(paths, False), text=text, random_ids=random_ids, reply_to=reply_to, find=find
                )
                if isinstance(result, _RetryAsDocument) and kind == "photo":
                    _logger.info("connector: альбом фото не принят (%s) — повтор документами", result.error)
                    result = await self._send_album_once(
                        peer, items(paths, True), text=text, random_ids=random_ids, reply_to=reply_to,
                        find=find,
                    )
                if isinstance(result, _RetryAsDocument):
                    return SendResult("permanent", error=result.error)
                return result
        except OutboxFileError as exc:
            return SendResult("temporary", error=exc.reason)

    async def _send_album_once(
        self,
        peer: object,
        items: list[AlbumItem],
        *,
        text: str,
        random_ids: list[int],
        reply_to: object | None,
        find,
    ) -> SendResult | _RetryAsDocument:
        """Одна попытка `send_album`. Ошибки `SendMultiMediaRequest` — как у `_send_media_once`; сбой загрузки
        (`MediaUploadError`) — до отправки: фото не принято `UploadMediaRequest` (П1) -> `_RetryAsDocument`,
        иначе `flood`/потеря входа/`temporary`. Ответ: `ext_id` всех элементов по `UpdateMessageID` —
        `sent`; не все -> поиск альбома в истории -> не нашли -> `unknown`."""
        try:
            result = await self._gateway.send_album(
                peer, items=items, text=text, random_ids=random_ids, reply_to=reply_to
            )
        except MediaUploadError as exc:
            cause = exc.cause
            if exc.stage == "upload_media" and isinstance(cause, _RETRY_AS_DOCUMENT_ERRORS):
                return _RetryAsDocument(type(cause).__name__)
            if isinstance(cause, errors.FloodWaitError):
                return SendResult("flood", retry_after_ms=cause.seconds * 1000)
            if isinstance(cause, _AUTH_LOST_ERRORS):
                raise AuthRequired(str(cause)) from cause
            return SendResult("temporary", error=type(cause).__name__)
        except errors.RandomIdDuplicateError:
            found, post_err = await find(_HISTORY_SEARCH_LIMIT_DUP)
            if post_err is not None:
                return post_err
            return _album_sent(found) if found is not None else SendResult("unknown")
        except errors.FloodWaitError as exc:
            return SendResult("flood", retry_after_ms=exc.seconds * 1000)
        except errors.SlowModeWaitError as exc:
            return SendResult("temporary", retry_after_ms=exc.seconds * 1000)
        except errors.PeerFloodError:
            return self._peer_flood()
        except _RETRY_AS_DOCUMENT_ERRORS as exc:
            return _RetryAsDocument(type(exc).__name__)
        except _PERMANENT_SEND_ERRORS as exc:
            return SendResult("permanent", error=type(exc).__name__)
        except errors.UserPrivacyRestrictedError:
            # Ф14-2 B3: как у текста — по классу (новой беседе `_send_new` заменит понятным текстом).
            return SendResult("permanent", error="UserPrivacyRestrictedError")
        except errors.ForbiddenError as exc:
            return SendResult("permanent", error=exc.message or type(exc).__name__)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except errors.BadRequestError as exc:
            return SendResult("permanent", error=exc.message or type(exc).__name__)
        except (OSError, errors.ServerError, errors.TimedOutError):
            return SendResult("abandon")

        ext_ids, grouped_id = convert.extract_album_ids(result, random_ids)
        if all(ext_id is not None for ext_id in ext_ids):
            return _album_sent(([str(e) for e in ext_ids], grouped_id))
        found, post_err = await find(_HISTORY_SEARCH_LIMIT)
        if post_err is not None:
            return post_err
        return _album_sent(found) if found is not None else SendResult("unknown")

    async def _find_album_in_history(
        self,
        peer: object,
        topic_id: int | None,
        text: str,
        n: int,
        *,
        since_ms: int,
        limit: int = _HISTORY_SEARCH_LIMIT,
        exclude_ext_ids: frozenset[str] = frozenset(),
    ) -> tuple[list[str], str] | None:
        """Ф11-3b B2 + П4: свой альбом из `n` файлов в последних `limit` сообщениях чата. Свои (`out`), с
        медиа (фото/документ), `date >= since_ms`, в теме форума — фильтр темы как у `_find_in_history`;
        группировка по `grouped_id`. Группа — кандидат, если в ней ровно `n` сообщений, НИ ОДНО не из
        `exclude_ext_ids` (свои сообщения, уже привязанные к outbox — прежний альбом исключается целиком по
        своему элементу 0), подпись сообщения с наименьшим id совпадает (`strip`), у остальных подписи пусты.
        Самая свежая группа-кандидат ->(`ext_id` по возрастанию id, `grouped_id` строкой); нет -> `None`."""
        text_stripped = text.strip()
        messages, entities = await self._gateway.get_messages(peer, min_id=None, limit=limit)
        self._entities.update(entities)
        groups: dict[int, list] = {}
        for msg in messages:
            if not bool(getattr(msg, "out", False)):
                continue
            if not isinstance(
                getattr(msg, "media", None), types.MessageMediaPhoto | types.MessageMediaDocument
            ):
                continue
            gid = getattr(msg, "grouped_id", None)
            if gid is None:
                continue
            if int(msg.date.timestamp() * 1000) < since_ms:
                continue
            if topic_id is not None:
                msg_topic = convert.message_topic_id(msg) or convert.GENERAL_TOPIC_ID
                if msg_topic != topic_id:
                    continue
            groups.setdefault(gid, []).append(msg)
        best: tuple[int, list[str], str] | None = None
        for gid, group in groups.items():
            if len(group) != n:
                continue
            if any(str(m.id) in exclude_ext_ids for m in group):
                continue
            group.sort(key=lambda m: m.id)
            if (getattr(group[0], "message", None) or "").strip() != text_stripped:
                continue
            if any((getattr(m, "message", None) or "").strip() for m in group[1:]):
                continue
            newest = group[-1].id
            if best is None or newest > best[0]:
                best = (newest, [str(m.id) for m in group], str(gid))
        if best is None:
            return None
        return best[1], best[2]

    # --- отправка: вспомогательное -----------------------------------------------------------------------

    async def _guarded(self, coro) -> tuple[object | None, SendResult | None]:
        """Находка F3 критика куска 1c: вызовы ВНЕ основного запроса отправки (`_resolve_peer()` —
        прогрев кэша сущностей, `_find_in_history()` — поиск в истории до/после запроса) раньше были
        совсем без классификации ошибок -> любой сбой там (сеть легла, FloodWait) каркас видел как
        необработанное исключение -> `unknown`, хотя отправка ТОЧНО не уходила (эти вызовы её не делают).
        Та же классификация, что у самого запроса `send_message()`: `FloodWaitError` -> `flood`, потеря
        входа -> `AuthRequired`, `OSError`/`ServerError`/`TimedOutError` -> `abandon` (никогда `unknown`).
        Возвращает `(значение, None)` при успехе либо `(None, SendResult)` — тогда `send()` должен вернуть
        этот `SendResult` немедленно."""
        try:
            value = await coro
        except errors.FloodWaitError as exc:
            return None, SendResult("flood", retry_after_ms=exc.seconds * 1000)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError):
            return None, SendResult("abandon")
        return value, None

    async def _resolve_peer(self, marked_id: int) -> object | None:
        """`peer = get_input_entity(<chat>)` из кэша сущностей сессии; промах -> ОДИН проход диалогов для
        наполнения кэша и повтор; снова промах -> `None` (вызывающий решает, что это значит для outcome)."""
        try:
            return await self._gateway.get_input_entity(marked_id)
        except ValueError:
            pass
        async for _ in self._gateway.iter_dialogs():
            pass
        try:
            return await self._gateway.get_input_entity(marked_id)
        except ValueError:
            return None

    async def _find_in_history(
        self,
        peer: object,
        topic_id: int | None,
        text: str,
        *,
        since_ms: int,
        limit: int = _HISTORY_SEARCH_LIMIT,
        exclude_ext_ids: frozenset[str] = frozenset(),
        with_media: bool = False,
    ) -> str | None:
        """Последние `limit` сообщений чата (в теме форума — фильтр по вычисленному `topic_id`: форумы не
        делят историю на отдельные id-пространства по темам, раздел «Сверка» куска 1b), свои (`out`),
        `date >= since_ms`, `text.strip()` совпадает -> `ext_id`; не нашли -> `None`. `exclude_ext_ids` —
        находка F1(б) критика куска 1c: ext_id уже известных ядру СВОИХ сообщений этого канала — они уже
        чьи-то (своя строка outbox нашла бы их через эхо/`match_echo`), кандидатом для ЭТОЙ строки не
        считаются, иначе повторная попытка присвоит себе чужую доставку (ложное «найдено» = тихая потеря).
        `with_media` (Ф10-4 B2) — только сообщения с фото/документом (подпись — `message`, пустая у медиа без
        подписи)."""
        text_stripped = text.strip()
        messages, entities = await self._gateway.get_messages(peer, min_id=None, limit=limit)
        self._entities.update(entities)
        for msg in messages:
            if str(msg.id) in exclude_ext_ids:
                continue
            if not bool(getattr(msg, "out", False)):
                continue
            if with_media and not isinstance(
                getattr(msg, "media", None), types.MessageMediaPhoto | types.MessageMediaDocument
            ):
                continue
            body = getattr(msg, "message", None)
            if body is None or body.strip() != text_stripped:
                continue
            msg_ts = int(msg.date.timestamp() * 1000)
            if msg_ts < since_ms:
                continue
            if topic_id is not None:
                msg_topic = convert.message_topic_id(msg) or convert.GENERAL_TOPIC_ID
                if msg_topic != topic_id:
                    continue
            return str(msg.id)
        return None

    async def _acquire_send_slot(self, chat_ext: str, *, slots: int = 1) -> SendResult | None:
        """Лимитер (план 4.1): скользящее окно 20/мин на аккаунт — заполнено -> `temporary` с
        `retry_after_ms`, БЕЗ запроса; не чаще 1/сек на чат — ожидание ВНУТРИ send() (≤ 1 с). Возвращает
        `SendResult`, если отправку нужно остановить (окно аккаунта), иначе `None` — можно слать.
        `slots` (Ф11-3b B2) — альбом занимает N мест окна; не хватает — `flood` до освобождения нужного
        числа мест."""
        slots = max(1, min(slots, _ACCOUNT_LIMIT_MAX))
        now_ms = int(self._wall() * 1000)
        while self._send_times and now_ms - self._send_times[0] >= _ACCOUNT_LIMIT_WINDOW_MS:
            self._send_times.popleft()
        if len(self._send_times) + slots > _ACCOUNT_LIMIT_MAX:
            # Окно АККАУНТА заполнено — это не сбой строки: `flood` (приёмка 1c), а не `temporary`.
            # `temporary` тратит попытку, и пачка из 25+ сообщений (одобренные черновики агента) после пяти
            # отсрочек ушла бы в `failed` без единой ошибки. `flood` не тратит попыток, откладывает весь
            # аккаунт (лимит и есть аккаунтный) и останавливает остаток пачки каркаса.
            # Освободиться должны самые старые `len + slots - MAX` мест — ждём последнего из них.
            oldest_needed = self._send_times[len(self._send_times) + slots - _ACCOUNT_LIMIT_MAX - 1]
            retry_after_ms = max(oldest_needed + _ACCOUNT_LIMIT_WINDOW_MS - now_ms, 1)
            return SendResult("flood", retry_after_ms=retry_after_ms)

        last_chat_ms = self._chat_last_send_ms.get(chat_ext)
        if last_chat_ms is not None:
            gap = now_ms - last_chat_ms
            if gap < _CHAT_MIN_GAP_MS:
                await self._sleep((_CHAT_MIN_GAP_MS - gap) / 1000)
                now_ms = int(self._wall() * 1000)

        self._send_times.extend([now_ms] * slots)
        self._chat_last_send_ms[chat_ext] = now_ms
        return None

    # --- медиа (Ф8-5 C1, П4, П15) --------------------------------------------------------------------------

    async def fetch_media(self, job: dict, sink: MediaSink) -> MediaResult:
        """Одно задание загрузчика: сообщение заново (`get_messages_by_ids` по peer беседы; топик — peer
        форума) -> `download_media(message)` в `sink`. Сообщения/медиа нет или медиа сменилось -> `gone`;
        `FloodWaitError` -> `temporary` с `retry_after_s` (и пауза загрузчика; отправку это не трогает);
        `FileReferenceExpired` -> перечитать сообщение, один повтор. Промах кэша сущностей -> `temporary`
        без полного прохода диалогов (П15: кэш прогреет сверка)."""
        ref = job.get("message") or {}
        try:
            chat_ext, _topic_id = convert.split_channel_ext_id(str(ref["channel_ext_id"]))
            chat_id, msg_id = int(chat_ext), int(ref["ext_id"])
        except KeyError, TypeError, ValueError:
            return MediaResult("gone", error="bad_ref")
        if job.get("idx", 0) != 0:
            return MediaResult("gone", error="no_media")  # у сообщения Telegram одно вложение
        try:
            try:
                peer = await self._gateway.get_input_entity(chat_id)
            except ValueError:
                return MediaResult("temporary", error="peer_unresolved")
            msg = await self._media_message(peer, msg_id, job.get("src_id"))
            if isinstance(msg, MediaResult):
                return msg
            try:
                result = await self._gateway.download_media(msg, sink)
            except _FILE_REFERENCE_ERRORS:
                msg = await self._media_message(peer, msg_id, job.get("src_id"))
                if isinstance(msg, MediaResult):
                    return msg
                sink.reset()
                result = await self._gateway.download_media(msg, sink)
        except errors.FloodWaitError as exc:
            return MediaResult("temporary", error="FloodWait", retry_after_s=float(exc.seconds))
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except _FILE_REFERENCE_ERRORS as exc:
            return MediaResult("temporary", error=type(exc).__name__)  # и повтор не помог
        except _MEDIA_GONE_ERRORS as exc:
            return MediaResult("gone", error=type(exc).__name__)
        except (OSError, errors.ServerError, errors.TimedOutError) as exc:
            return MediaResult("temporary", error=type(exc).__name__)
        if result is None:
            return MediaResult("gone", error="no_media")
        return MediaResult("ready")

    async def _media_message(self, peer: object, msg_id: int, src_id: object) -> object:
        """Сообщение с медиа или готовый итог `gone` (сообщения нет, медиа нет, одноразовое, сменилось)."""
        messages = await self._gateway.get_messages_by_ids(peer, [msg_id])
        msg = messages[0] if messages else None
        media = getattr(msg, "media", None)
        if msg is None or media is None:
            return MediaResult("gone", error="no_message" if msg is None else "no_media")
        if getattr(media, "ttl_seconds", None):
            return MediaResult("gone", error="ttl")  # П13: одноразовое не качается
        attachments = convert.convert_attachments(media) or []
        current = attachments[0].get("src_id") if attachments else None
        if current is None:
            return MediaResult("gone", error="no_media")
        if src_id is not None and str(src_id) != current:
            return MediaResult("gone", error="src_changed")  # П10: медиа заменено правкой
        return msg

    # --- прочитанность из хаба (Ф8-7 C1, П5) -------------------------------------------------------------

    async def push_read(self, item: dict) -> ReadAckResult:
        """Ф8-7 C1 + П5: «прочитано до `message.ext_id`» в Telegram. Тема форума `<chat>:<topic_id>` (включая
        General) — ТОЛЬКО `read_discussion` (`ReadDiscussionRequest`); обычный чат — `read_history`
        (`send_read_acknowledge`). Сам форум (не тема) через `read_history` не отмечается НИКОГДА: id
        сообщений у тем общие — отметило бы все темы (П5) -> `drop`; сущность супергруппы неизвестна
        коннектору (форум ли — не понять без сети) -> `retry`, кэш прогреет сверка/живое сообщение.
        Промах кэша сущностей сессии — как у медиа: `retry` без прохода диалогов. `FloodWaitError` -> `retry`
        с `retry_after_s`; потеря входа -> `AuthRequired`; сеть/`ServerError`/таймаут -> `retry`; прочие
        `RPCError` (`CHANNEL_PRIVATE`, `PEER_ID_INVALID`, …) -> `drop`, reason = имя класса."""
        channel = item.get("channel") or {}
        message = item.get("message") or {}
        try:
            chat_ext, topic_id = convert.split_channel_ext_id(str(channel["ext_id"]))
            chat_id = int(chat_ext)
            max_id = int(str(message["ext_id"]))
        except KeyError, TypeError, ValueError:
            return ReadAckResult("drop", reason="bad_ref")
        if max_id <= 0:
            return ReadAckResult("drop", reason="bad_ref")
        if topic_id is None:
            forum = self._forum_state(chat_id)
            if forum is None:
                return ReadAckResult("retry", reason="entity_unknown")
            if forum:
                return ReadAckResult("drop", reason="forum_parent")
        try:
            try:
                peer = await self._gateway.get_input_entity(chat_id)
            except ValueError:
                return ReadAckResult("retry", reason="peer_unresolved")
            if topic_id is not None:
                await self._gateway.read_discussion(peer, topic_id, max_id)
            else:
                await self._gateway.read_history(peer, max_id)
        except errors.FloodWaitError as exc:
            return ReadAckResult("retry", retry_after_s=float(exc.seconds), reason="FloodWait")
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError) as exc:
            return ReadAckResult("retry", reason=type(exc).__name__)
        except errors.RPCError as exc:
            _logger.info(
                "connector: отметка прочтения канала %s отклонена (%s)",
                item.get("channel_id"),
                type(exc).__name__,
            )
            return ReadAckResult("drop", reason=type(exc).__name__)
        return ReadAckResult("done")

    def _forum_state(self, chat_id: int) -> bool | None:
        """Форум ли чат (не тема): личка/малая группа — никогда (`False`); канал/супергруппа — по сущности в
        кэше коннектора (`None` — сущность неизвестна)."""
        try:
            _real_id, peer_cls = utils.resolve_id(chat_id)
        except TypeError, ValueError:
            return None
        if peer_cls is not types.PeerChannel:
            return False
        entity = self._entities.get(chat_id)
        if entity is None:
            return None
        return bool(getattr(entity, "forum", False))

    # --- нажатия кнопок ботов (Ф44-1c, 4.4 и П1) ------------------------------------------------------------

    async def push_press(self, item: dict) -> PressResult:
        """Ф44-1c: `messages.GetBotCallbackAnswer` — один запрос (`gateway.press_button`), без повторов.

        Ответ бота -> `done` с `message`/`alert`/`url`; `TimedOutError`, `BotResponseTimeoutError` (бот
        получил нажатие и не ответил на запрос — так устроены многие боты) -> `done` с `timeout`;
        `MessageIdInvalidError`, `DataInvalidError`, `ButtonDataInvalidError` -> `failed`, `stale_button`;
        `FloodWaitError` -> `retry` с паузой, `reason="flood"` (успеет ли в срок — решает ядро); потеря входа
        -> `AuthRequired`; запрос не уходил (собеседник не найден в кэше, нет соединения) -> `retry`; ушёл и
        пропал (`PressUnknown`) -> `unknown`; прочие `RPCError` -> `failed` с именем класса. Тема форума:
        `peer` — сам чат."""
        try:
            chat_ext, _topic = convert.split_channel_ext_id(str(item["channel_ext_id"]))
            chat_id = int(chat_ext)
            msg_id = int(str(item["message_ext_id"]))
            data = base64.b64decode(str(item["data_b64"]), validate=True)
        except KeyError, TypeError, ValueError:
            return PressResult("failed", reason="bad_ref")
        if msg_id <= 0 or not data:
            return PressResult("failed", reason="bad_ref")
        try:
            try:
                peer = await self._gateway.get_input_entity(chat_id)
            except ValueError:
                return PressResult("retry", reason="peer_unresolved")
            answer = await self._gateway.press_button(peer, msg_id, data, timeout_s=_PRESS_TIMEOUT_S)
        except PressUnknown as exc:
            return PressResult("unknown", reason=exc.reason)
        except errors.FloodWaitError as exc:
            return PressResult("retry", retry_after_s=float(exc.seconds), reason="flood")
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except errors.TimedOutError, errors.BotResponseTimeoutError:
            return PressResult("done", result={"timeout": True})
        except _PRESS_STALE_ERRORS:
            return PressResult("failed", reason="stale_button")
        except errors.ServerError as exc:
            # 5xx Telegram приходит на уже принятый запрос: дошло ли нажатие до бота — неизвестно.
            return PressResult("unknown", reason=type(exc).__name__)
        except errors.RPCError as exc:
            return PressResult("failed", reason=type(exc).__name__)
        except OSError as exc:
            return PressResult("retry", reason=type(exc).__name__)
        result: dict = {}
        message = getattr(answer, "message", None)
        if isinstance(message, str) and message:
            result["message"] = message
        if getattr(answer, "alert", False):
            result["alert"] = True
        url = getattr(answer, "url", None)
        if isinstance(url, str) and url:
            result["url"] = url
        return PressResult("done", result=result)

    # --- вступление в группы и каналы (Ф17-1 C, П4, П5) -----------------------------------------------------

    async def push_join(self, item: dict) -> JoinResult:
        """Ф17-1 C: шаг заявки. `peek` — `username`: `ResolveUsernameRequest` (человек или бот -> `drop
        not_group`), канал/супергруппа — поля сущности + одна попытка `GetFullChannelRequest` (число
        участников, описание); `invite`: `CheckChatInviteRequest` (`ChatInvite` — без ext_id, платный ->
        `drop paid`; `ChatInviteAlready` -> участник; `ChatInvitePeek` -> ext_id). `join` —
        `JoinChannelRequest` (сущность из кэша, промах -> повторный резолв) / `ImportChatInviteRequest`; чат —
        из `Updates.chats`.
        `UserAlreadyParticipantError` -> `already`, `InviteRequestSentError` -> `requested`. Прочие ошибки —
        `_join_error`."""
        step = item.get("step")
        target = item.get("target")
        kind = item.get("target_kind")
        if step not in ("peek", "join") or kind not in ("username", "invite") or not isinstance(target, str):
            return JoinResult("drop", reason="bad_item")
        try:
            if step == "peek":
                if kind == "username":
                    return await self._join_peek_username(target)
                return await self._join_peek_invite(target)
            return await self._join_do(item, kind, target)
        except errors.UserAlreadyParticipantError:
            ext_id = item.get("ext_id")
            if kind == "invite" and not isinstance(ext_id, str):
                # Ревью Ф17-1 (находка 3): повтор шага после вступления, отчёт о котором не дошёл, — у
                # приглашения ext_id нет; `CheckChatInvite` участнику отдаёт сам чат (`ChatInviteAlready`).
                ext_id = await self._invite_member_ext_id(target)
            return JoinResult("done", result="already", ext_id=ext_id)
        except errors.InviteRequestSentError:
            # П5: чат Telegram не отдаёт — ext_id только если он известен с предпросмотра (цель по имени).
            return JoinResult("done", result="requested", ext_id=item.get("ext_id"))
        except Exception as exc:  # noqa: BLE001 — классификация ниже; `AuthRequired` — наверх
            return self._join_error(exc)

    @staticmethod
    def _join_error(exc: Exception) -> JoinResult:
        """Раздел C: класс ошибки -> исход. Потеря входа — `AuthRequired` (отчёта нет)."""
        if isinstance(exc, AuthRequired):
            raise exc
        if isinstance(exc, errors.FloodWaitError):
            return JoinResult("retry", retry_after_s=float(exc.seconds), reason="FloodWait")
        if isinstance(exc, _AUTH_LOST_ERRORS):
            raise AuthRequired(str(exc)) from exc
        if isinstance(exc, OSError | errors.ServerError | errors.TimedOutError):
            return JoinResult("retry", reason=type(exc).__name__)
        for classes, reason in _JOIN_DROP_ERRORS:
            if isinstance(exc, classes):
                return JoinResult("drop", reason=reason)
        if isinstance(exc, errors.RPCError):
            # У известных Telethon классов `message` — общее «BAD_REQUEST»/«FORBIDDEN»: имя класса точнее; у
            # незнакомых кодов (базовый класс) `message` — сам код Telegram.
            known = type(exc).__module__.endswith("rpcerrorlist")
            reason = type(exc).__name__ if known else (exc.message or type(exc).__name__)
            return JoinResult("drop", reason=reason)
        if isinstance(exc, ValueError):
            # `resolve_username`: сущности нет в ответе.
            return JoinResult("drop", reason="not_found")
        return JoinResult("retry", reason=type(exc).__name__)

    def _remember(self, entity: object) -> str:
        ext_id = str(utils.get_peer_id(entity))
        self._entities[int(ext_id)] = entity
        return ext_id

    @staticmethod
    def _chat_kind(entity: object) -> str:
        if isinstance(entity, types.Channel):
            return "broadcast" if getattr(entity, "broadcast", False) else "megagroup"
        return "group"

    async def _join_peek_username(self, username: str) -> JoinResult:
        entity = await self._gateway.resolve_username(username)
        if isinstance(entity, types.User):
            return JoinResult("drop", reason="not_group")
        if not isinstance(entity, types.Channel | types.Chat):
            return JoinResult("drop", reason="private")
        return JoinResult("done", peek=await self._chat_peek(entity))

    async def _chat_peek(self, entity: object) -> dict:
        """Предпросмотр известного чата: поля сущности; у канала/супергруппы — одна попытка полной
        информации (сбой -> без числа участников и описания; потеря входа — наверх)."""
        ext_id = self._remember(entity)
        peek = {
            "title": getattr(entity, "title", None),
            "kind": self._chat_kind(entity),
            "participants": getattr(entity, "participants_count", None),
            "about": None,
            "request_needed": bool(getattr(entity, "join_request", False)),
            "member": not bool(getattr(entity, "left", False)),
            "forum": bool(getattr(entity, "forum", False)),
            "ext_id": ext_id,
        }
        if isinstance(entity, types.Channel):
            try:
                full = await self._gateway.get_full_channel(utils.get_input_channel(entity))
                full_chat = getattr(full, "full_chat", None)
                count = getattr(full_chat, "participants_count", None)
                if isinstance(count, int):
                    peek["participants"] = count
                about = getattr(full_chat, "about", None)
                peek["about"] = about or None
                linked = self._peek_linked(entity, full)
                if linked is not None:
                    peek["linked"] = linked
            except _AUTH_LOST_ERRORS as exc:
                raise AuthRequired(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 — одна попытка: без числа и описания
                _logger.info(
                    "connector: join: шаг=peek, полная информация не получена (%s)", type(exc).__name__
                )
        return peek

    @staticmethod
    def _peek_linked(entity: object, full: object) -> dict | None:
        """Ф17-3 решение 4: связанный чат: `ChannelFull.linked_chat_id` и сущность в `full.chats`. У канала
        это его группа обсуждения (`discussion`), у группы — её канал (`channel`); нет сущности — поля нет."""
        linked_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
        if not isinstance(linked_id, int) or isinstance(linked_id, bool):
            return None
        for chat in getattr(full, "chats", None) or []:
            if getattr(chat, "id", None) != linked_id:
                continue
            title = getattr(chat, "title", None)
            if not isinstance(title, str) or not title:
                return None
            username = getattr(chat, "username", None)
            return {
                "title": title,
                "username": username if isinstance(username, str) and username else None,
                "kind": "discussion" if getattr(entity, "broadcast", False) else "channel",
            }
        return None

    async def _join_peek_invite(self, invite_hash: str) -> JoinResult:
        invite = await self._gateway.check_invite(invite_hash)
        if isinstance(invite, types.ChatInviteAlready | types.ChatInvitePeek):
            peek = await self._chat_peek(invite.chat)
            peek["member"] = isinstance(invite, types.ChatInviteAlready)
            return JoinResult("done", peek=peek)
        if isinstance(invite, types.ChatInvite):
            if getattr(invite, "subscription_pricing", None) is not None:
                return JoinResult("drop", reason="paid")
            kind = "broadcast" if invite.broadcast else ("megagroup" if invite.megagroup else "group")
            return JoinResult(
                "done",
                peek={
                    "title": invite.title,
                    "kind": kind,
                    "participants": invite.participants_count,
                    "about": getattr(invite, "about", None) or None,
                    "request_needed": bool(invite.request_needed),
                    "member": False,
                    "forum": False,
                    "ext_id": None,  # П5: у приглашения id чата нет
                },
            )
        return JoinResult("drop", reason="invite_invalid")

    async def _invite_member_ext_id(self, invite_hash: str) -> str | None:
        """ext_id чата по приглашению, если владелец уже участник; сбой или не участник — `None` (потеря входа —
        наверх)."""
        try:
            invite = await self._gateway.check_invite(invite_hash)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — без ext_id: беседа придёт по правилам раскладки
            _logger.info("connector: join: шаг=join, чат участника не получен (%s)", type(exc).__name__)
            return None
        if isinstance(invite, types.ChatInviteAlready):
            return self._remember(invite.chat)
        return None

    async def _join_do(self, item: dict, kind: str, target: str) -> JoinResult:
        known = item.get("ext_id")
        if kind == "username":
            entity = None
            if isinstance(known, str):
                with contextlib.suppress(ValueError):
                    entity = self._entities.get(int(known))
            if entity is None:
                entity = await self._gateway.resolve_username(target)
                if isinstance(entity, types.User):
                    return JoinResult("drop", reason="not_group")
            if not isinstance(entity, types.Channel):
                return JoinResult("drop", reason="not_channel")
            self._remember(entity)
            updates = await self._gateway.join_channel(utils.get_input_channel(entity))
            fallback = entity
        else:
            updates = await self._gateway.import_invite(target)
            fallback = None
        chat = self._joined_chat(updates, known) or fallback
        if chat is None:
            return JoinResult("done", result="joined", ext_id=known if isinstance(known, str) else None)
        ext_id = self._remember(chat)
        return JoinResult("done", result="joined", ext_id=ext_id, title=getattr(chat, "title", None))

    @staticmethod
    def _joined_chat(updates: object, known: object) -> object | None:
        """Чат из `Updates.chats`: совпавший с известным ext_id, иначе первый канал/группа, где владелец теперь
        участник (`left` снят): у канала со связанной группой обсуждения в ответе бывают оба чата."""
        raw = getattr(updates, "chats", None) or []
        chats = [c for c in raw if isinstance(c, types.Channel | types.Chat)]
        for chat in chats:
            if isinstance(known, str) and str(utils.get_peer_id(chat)) == known:
                return chat
        joined = [c for c in chats if not getattr(c, "left", False)]
        return (joined or chats)[0] if chats else None

    async def after_join(self, item: dict, result: JoinResult) -> None:
        """Ф17-1 C/П4: отчёт принят — вступили (или уже участник): стартовая загрузка ЭТОЙ беседы при
        ближайшей сверке (`_sync_impl`, без условия «активна за 30 дней»), если её нет в хабе. Здесь — только
        постановка в очередь: шаг исполнителя не держит."""
        if result.outcome != "done":
            return
        if item.get("step") == "join":
            joined = result.result in ("joined", "already")
        else:
            joined = bool((result.peek or {}).get("member"))
        ext_id = result.ext_id or (result.peek or {}).get("ext_id") or item.get("ext_id")
        if not joined or not isinstance(ext_id, str):
            return
        try:
            entity = self._entities.get(int(ext_id))
        except ValueError:
            return
        if entity is None:
            return
        self._join_loads[ext_id] = entity
        self._request_sync("join")

    async def _run_join_loads(self, known_chats: set[str]) -> set[str]:
        """Стартовая загрузка бесед, в которые только что вступили (очередь `after_join`), — тем же путём, что
        у сверки (30 сообщений); беседы, уже известные ядру, пропускаются. Диалога под рукой нет — маркер
        прочтения ставится на самое новое загруженное сообщение (Ф17-3). Возвращает загруженные."""
        loads = {ext: ent for ext, ent in self._join_loads.items() if ext not in known_chats}
        self._join_loads.clear()
        if not loads:
            return set()
        pending = [(utils.get_peer(ent), ent, ext, None, None) for ext, ent in loads.items()]
        failed: list[str] = []
        try:
            # Ф17-3 решение 3: история вступления — прочитанной (маркер на самое новое загруженное).
            await self._run_initial_load(pending, failed=failed, read_newest=True)
        except _SYNC_ABORT_ERRORS:
            # Ревью Ф17-1 (находка 4): FloodWait посреди загрузки — очередь назад, следующая сверка повторит.
            for ext, ent in loads.items():
                self._join_loads.setdefault(ext, ent)
            raise
        for ext in failed:
            # Сбой одного чата (сеть и т.п.) — ещё попытка на следующей сверке, не больше `_JOIN_LOAD_TRIES`.
            tries = self._join_load_tries.get(ext, 0) + 1
            if tries < _JOIN_LOAD_TRIES:
                self._join_load_tries[ext] = tries
                self._join_loads.setdefault(ext, loads[ext])
            else:
                self._join_load_tries.pop(ext, None)
        for ext in set(loads) - set(failed):
            self._join_load_tries.pop(ext, None)
        _logger.info("connector: join: стартовая загрузка бесед: %d", len(pending) - len(failed))
        return set(loads) - set(failed)

    # --- выход из групп и каналов (Ф17-2 C, П1, П4) --------------------------------------------------------

    @staticmethod
    def _entity_not_member(entity: object) -> bool:
        """Ф17-2 П1: владелец не участник чата — `left` у `Channel`/`Chat` или `ChannelForbidden`/
        `ChatForbidden`."""
        if isinstance(entity, types.ChannelForbidden | types.ChatForbidden):
            return True
        return isinstance(entity, types.Channel | types.Chat) and bool(getattr(entity, "left", False))

    def _not_member(self, chat_ext: str, entity: object) -> bool:
        if not self._entity_not_member(entity):
            return False
        if chat_ext not in self._not_member_logged:
            self._not_member_logged.add(chat_ext)
            _logger.info("connector: chat=%s — владелец не участник, сообщения чата не принимаются", chat_ext)
        return True

    async def push_leave(self, item: dict) -> JoinResult:
        """Ф17-2 C + П4: выйти из группы или канала. Сущность — `_resolve_peer` (кэш сессии -> один проход
        диалогов): нет в диалогах -> `done not_member`. Перед вызовом — свежая сущность с сервера: `left` или
        `ChannelForbidden`/`ChatForbidden` -> `done not_member` без вызова (повтор после потерянного отчёта);
        `ChannelPrivateError` на ней -> тоже. `InputPeerChannel` (канал, супергруппа, форум целиком) ->
        `LeaveChannelRequest`, `InputPeerChat` (обычная группа) -> `DeleteChatUserRequest` без чистки истории.
        Ошибки — `_leave_error`. После выхода из канала/супергруппы сущность в кэше помечается `left` (живые
        апдейты не идут в приём сразу, П1; обычную группу кэш не трогает), названия тем форума забываются."""
        if item.get("step") != "leave":
            return JoinResult("drop", reason="bad_item")
        chat_ext = item.get("ext_id")
        try:
            marked = int(str(chat_ext))
        except TypeError, ValueError:
            return JoinResult("drop", reason="bad_item")
        try:
            peer = await self._resolve_peer(marked)
            if peer is None:
                return JoinResult("done", result="not_member")
            if not isinstance(peer, types.InputPeerChannel | types.InputPeerChat):
                return JoinResult("drop", reason="not_group")
            # `ChannelPrivateError` здесь (закрытый канал, где владелец уже не участник) — `not_member` ниже.
            fresh = await self._gateway.get_entity(peer)
            if fresh is not None:
                self._entities[marked] = fresh
            if self._entity_not_member(fresh):
                return JoinResult("done", result="not_member")
            if isinstance(peer, types.InputPeerChannel):
                await self._gateway.leave_channel(utils.get_input_channel(peer))
            else:
                await self._gateway.leave_chat(peer.chat_id)
        except errors.UserNotParticipantError, errors.ChannelPrivateError:
            return JoinResult("done", result="not_member")
        except Exception as exc:  # noqa: BLE001 — классификация ниже; `AuthRequired` — наверх
            return self._leave_error(exc)
        # Только канал/супергруппа: у обычной группы короткий апдейт (`UpdateShortChatMessage`) приходит без
        # сущности чата и решается по кэшу — пометка `left` отбросила бы сообщения, если владельца добавят обратно,
        # до ближайшей сверки. Из обычной группы после выхода Telegram апдейтов и так не шлёт.
        entity = self._entities.get(marked)
        if isinstance(peer, types.InputPeerChannel) and isinstance(entity, types.Channel):
            entity.left = True
        self._topic_titles.pop(str(marked), None)
        return JoinResult("done", result="left")

    @staticmethod
    def _leave_error(exc: Exception) -> JoinResult:
        """Раздел C: класс ошибки выхода -> исход. Потеря входа — `AuthRequired` (отчёта нет)."""
        if isinstance(exc, AuthRequired):
            raise exc
        if isinstance(exc, errors.FloodWaitError):
            return JoinResult("retry", retry_after_s=float(exc.seconds), reason="FloodWait")
        if isinstance(exc, _AUTH_LOST_ERRORS):
            raise AuthRequired(str(exc)) from exc
        if isinstance(exc, OSError | errors.ServerError | errors.TimedOutError):
            return JoinResult("retry", reason=type(exc).__name__)
        for classes, reason in _LEAVE_DROP_ERRORS:
            if isinstance(exc, classes):
                return JoinResult("drop", reason=reason)
        if isinstance(exc, errors.RPCError):
            known = type(exc).__module__.endswith("rpcerrorlist")
            reason = type(exc).__name__ if known else (exc.message or type(exc).__name__)
            return JoinResult("drop", reason=reason)
        return JoinResult("retry", reason=type(exc).__name__)

    # --- поиск и чтение без вступления (Ф29-1 C2–C4) -------------------------------------------------------

    async def run_lookup(self, item: dict) -> LookupResult:
        """Ф29-1 C2/C3: `search` — `contacts.Search`, `peek` — история публичного канала или группы по @имени.
        Ничего не пишет в ядро (ни событий, ни бесед); сущности — в общий кэш `_entities`. Ошибки —
        `_lookup_error` (потеря входа — `AuthRequired` наверх)."""
        kind = item.get("kind")
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        try:
            if kind == "search":
                return await self._lookup_search(params)
            if kind == "peek":
                return await self._lookup_peek(params)
            if kind == "resolve":
                return await self._lookup_resolve(params)
        except Exception as exc:  # noqa: BLE001 — классификация ниже; `AuthRequired` — наверх
            return self._lookup_error(exc)
        return LookupResult("error", reason="bad_item")

    @staticmethod
    def _lookup_error(exc: Exception) -> LookupResult:
        """C4: класс ошибки -> причина. Потеря входа — `AuthRequired` (исполнитель ответит
        `auth_required`)."""
        if isinstance(exc, AuthRequired):
            raise exc
        if isinstance(exc, errors.FloodError):
            # Ф32-2 п.1 + П6: любая пауза Telegram с числом секунд (`FloodWaitError`, `FloodPremiumWaitError`,
            # `SlowModeWaitError`) — `flood`, секунды в границах ручки ядра `[1, 86400]`. Подкласс без
            # `.seconds` (`FrozenMethodInvalidError`) — не пауза: ошибка с именем класса, иначе ядро ставило
            # бы паузы без конца.
            seconds = getattr(exc, "seconds", None)
            if isinstance(seconds, int | float) and not isinstance(seconds, bool):
                wait = float(min(max(seconds, _LOOKUP_FLOOD_MIN_S), _LOOKUP_FLOOD_MAX_S))
                return LookupResult("error", reason="flood", retry_after_s=wait)
            return LookupResult("error", reason=type(exc).__name__)
        if isinstance(exc, _AUTH_LOST_ERRORS):
            raise AuthRequired(str(exc)) from exc
        if isinstance(exc, OSError | errors.ServerError | errors.TimedOutError):
            return LookupResult("error", reason="temporary")
        for classes, reason in _LOOKUP_ERRORS:
            if isinstance(exc, classes):
                return LookupResult("error", reason=reason)
        if isinstance(exc, errors.RPCError):
            return LookupResult("error", reason=exc.message or type(exc).__name__)
        if isinstance(exc, ValueError):
            # `resolve_username`: сущности нет в ответе.
            return LookupResult("error", reason="not_found")
        return LookupResult("error", reason=type(exc).__name__)

    @staticmethod
    def _main_username(entity: object) -> str | None:
        """Основное @имя или первое активное из `usernames` (коллекционные имена)."""
        username = getattr(entity, "username", None)
        if isinstance(username, str) and username:
            return username
        for item in getattr(entity, "usernames", None) or []:
            name = getattr(item, "username", None)
            if getattr(item, "active", False) and isinstance(name, str) and name:
                return name
        return None

    def _lookup_remember(self, entity: object) -> str:
        """В общий кэш — только полные сущности (`min` — без годного access_hash, вызовы по ним не идут)."""
        ext_id = convert.peer_ext_id(entity)
        if not getattr(entity, "min", False):
            self._entities[int(ext_id)] = entity
        return ext_id

    def _lookup_chat(self, entity: object) -> dict:
        """Поля чата для ответа (C2/C3)."""
        count = getattr(entity, "participants_count", None)
        return {
            "ext_id": convert.peer_ext_id(entity),
            "username": self._main_username(entity),
            "title": getattr(entity, "title", None),
            "kind": self._chat_kind(entity),
            "participants": count if isinstance(count, int) and not isinstance(count, bool) else None,
            "member": not bool(getattr(entity, "left", False)),
        }

    def _lookup_cache_put(self, cache: dict, key: object, value: tuple) -> None:
        if len(cache) >= _LOOKUP_CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[key] = value

    async def _lookup_search(self, params: dict) -> LookupResult:
        """C2: из `chats` ответа — только канал или супергруппа (`Channel`) и малая группа (`Chat`, только из
        своих диалогов), без `restricted`; люди и боты — мимо. Сначала `my_results`, потом `results`."""
        query = params.get("query")
        limit = params.get("limit")
        if not isinstance(query, str) or not isinstance(limit, int) or limit < 1:
            return LookupResult("error", reason="bad_item")
        key = (query.lower(), limit)
        now = self._mono()
        cached = self._lookup_search_cache.get(key)
        if cached is not None and now - cached[0] < _LOOKUP_SEARCH_TTL_S:
            return LookupResult("ok", payload=cached[1])
        found = await self._gateway.search_chats(query, limit=min(limit * 2, _LOOKUP_SEARCH_FETCH_MAX))
        chats: dict[int, object] = {}
        for chat in getattr(found, "chats", None) or []:
            with contextlib.suppress(TypeError, ValueError):
                chats[utils.get_peer_id(chat)] = chat
        results: list[dict] = []
        seen: set[int] = set()
        order = [(p, True) for p in getattr(found, "my_results", None) or []]
        order += [(p, False) for p in getattr(found, "results", None) or []]
        for peer, mine in order:
            if len(results) >= limit:
                break
            try:
                marked = utils.get_peer_id(peer)
            except TypeError, ValueError:
                continue
            chat = chats.get(marked)
            if marked in seen or chat is None:
                continue
            if isinstance(chat, types.Chat):
                if not mine:
                    continue
            elif not isinstance(chat, types.Channel):
                continue
            if getattr(chat, "restricted", False):
                continue
            seen.add(marked)
            self._lookup_remember(chat)
            fields = self._lookup_chat(chat)
            fields.update(
                {
                    "verified": bool(getattr(chat, "verified", False)),
                    "scam": bool(getattr(chat, "scam", False)),
                    "fake": bool(getattr(chat, "fake", False)),
                    "join_request": bool(getattr(chat, "join_request", False)),
                }
            )
            results.append(fields)
        payload = {"results": results}
        self._lookup_cache_put(self._lookup_search_cache, key, (now, payload))
        return LookupResult("ok", payload=payload)

    def _entity_by_username(self, username: str) -> object | None:
        """Сущность из общего кэша по @имени (без регистра, и коллекционные имена) — резолв у Telegram
        лимитирован, его бережём."""
        wanted = username.lower()
        for entity in self._entities.values():
            if getattr(entity, "min", False) or not isinstance(
                entity, types.User | types.Chat | types.Channel
            ):
                continue
            names = [getattr(entity, "username", None)]
            names += [getattr(u, "username", None) for u in getattr(entity, "usernames", None) or []]
            if any(isinstance(n, str) and n.lower() == wanted for n in names):
                return entity
        return None

    async def _lookup_full(self, entity: types.Channel) -> tuple[int | None, str | None]:
        """Число участников и описание канала (`GetFullChannel`, кэш 1 ч); сбой — `(None, None)`, потеря
        входа — наверх."""
        marked = utils.get_peer_id(entity)
        now = self._mono()
        cached = self._lookup_full_cache.get(marked)
        if cached is not None and now - cached[0] < _LOOKUP_FULL_TTL_S:
            return cached[1], cached[2]
        try:
            full = await self._gateway.get_full_channel(utils.get_input_channel(entity))
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except errors.FloodError as exc:
            # Ревью Ф29-1: флуд не глотать — ядро ставит паузу аккаунта, иначе следующие чтения снова
            # дёргают GetFullChannel посреди FloodWait. Ревью Ф32-2: любая пауза с числом секунд, как в
            # `_lookup_error`; подкласс без `.seconds` — не пауза, чтение идёт без числа и описания.
            seconds = getattr(exc, "seconds", None)
            if isinstance(seconds, int | float) and not isinstance(seconds, bool):
                raise
            _logger.info("connector: lookup: полная информация не получена (%s)", type(exc).__name__)
            return None, None
        except Exception as exc:  # noqa: BLE001 — без числа и описания
            _logger.info("connector: lookup: полная информация не получена (%s)", type(exc).__name__)
            return None, None
        full_chat = getattr(full, "full_chat", None)
        count = getattr(full_chat, "participants_count", None)
        count = count if isinstance(count, int) and not isinstance(count, bool) else None
        about = getattr(full_chat, "about", None)
        about = about[:_LOOKUP_ABOUT_MAX] if isinstance(about, str) and about else None
        self._lookup_cache_put(self._lookup_full_cache, marked, (now, count, about))
        return count, about

    async def _lookup_resolve(self, params: dict) -> LookupResult:
        """24.10: кто это по @имени (проверка адресата `hub_draft_new`) —
        `{kind: user|bot|broadcast|megagroup|group}`. Кэш сущностей, промах — один `resolve_username`; полная
        сущность — в общий кэш (тот же путь, что у `peek`). Имени нет — `not_found` (ядро тогда не мешает
        созданию заготовки)."""
        username = params.get("username")
        if not isinstance(username, str) or not username:
            return LookupResult("error", reason="bad_item")
        entity = self._entity_by_username(username)
        if entity is None:
            entity = await self._gateway.resolve_username(username)
        if isinstance(entity, types.User):
            self._lookup_remember(entity)
            return LookupResult("ok", payload={"kind": "bot" if getattr(entity, "bot", False) else "user"})
        if isinstance(entity, types.Channel | types.Chat):
            self._lookup_remember(entity)
            return LookupResult("ok", payload={"kind": self._chat_kind(entity)})
        return LookupResult("error", reason="not_found")

    async def _lookup_peek(self, params: dict) -> LookupResult:
        """C3: сущность по @имени (кэш, промах — `resolve_username`), полная информация канала (кэш) и одна
        страница истории `GetHistory(offset_id=before)`. Человек/бот — `not_group`."""
        username = params.get("username")
        limit = params.get("limit")
        before = params.get("before")
        if not isinstance(username, str) or not username or not isinstance(limit, int) or limit < 1:
            return LookupResult("error", reason="bad_item")
        if before is not None and (not isinstance(before, int) or before < 1):
            return LookupResult("error", reason="bad_item")
        entity = self._entity_by_username(username)
        if entity is None:
            entity = await self._gateway.resolve_username(username)
        if isinstance(entity, types.User):
            return LookupResult("error", reason="not_group")
        if not isinstance(entity, types.Channel | types.Chat):
            return LookupResult("error", reason="private")
        self._lookup_remember(entity)
        chat = self._lookup_chat(entity)
        chat["about"] = None
        if isinstance(entity, types.Channel):
            count, about = await self._lookup_full(entity)
            if count is not None:
                chat["participants"] = count
            chat["about"] = about
        raw, entities = await self._gateway.get_history(
            utils.get_input_peer(entity), offset_id=before or 0, limit=limit
        )
        messages = self._lookup_messages(raw, entity, entities)
        ids = [m.id for m in raw if isinstance(getattr(m, "id", None), int)]
        next_before = min(ids) if len(raw) >= limit and ids else None
        return LookupResult("ok", payload={"chat": chat, "messages": messages, "next_before": next_before})

    def _lookup_messages(self, raw: list, chat: object, entities: dict[int, object]) -> list[dict]:
        """C3: сообщения (новые первыми), служебные пропущены; альбом — одним сообщением (`album` — число
        частей, вложения всех частей, подпись — той, у которой она есть)."""
        out: list[dict] = []
        albums: dict[int, dict] = {}
        for msg in raw:
            if not isinstance(msg, types.Message):
                continue
            item = self._lookup_message(msg, chat, entities)
            grouped = getattr(msg, "grouped_id", None)
            if not isinstance(grouped, int):
                out.append(item)
                continue
            head = albums.get(grouped)
            if head is None:
                item["album"] = 1
                albums[grouped] = item
                out.append(item)
                continue
            head["album"] += 1
            head["media"] = [*(head.get("media") or []), *(item.get("media") or [])] or None
            if not head.get("text") and item.get("text"):
                head["text"] = item["text"]
            head["id"] = min(head["id"], item["id"])
            for field in ("views", "forwards", "replies"):
                if isinstance(item.get(field), int):
                    head[field] = max(head.get(field) or 0, item[field])
            if isinstance(item.get("reactions"), int):
                head["reactions"] = (head.get("reactions") or 0) + item["reactions"]
        return out

    def _lookup_author(self, msg: object, chat: object, entities: dict[int, object]) -> str | None:
        """Канал — подпись автора поста или название канала; группа — отображаемое имя отправителя (без
        телефона), отправитель-чат (аноним, от имени канала) — его название."""
        if isinstance(chat, types.Channel) and getattr(chat, "broadcast", False):
            return getattr(msg, "post_author", None) or getattr(chat, "title", None)
        from_id = getattr(msg, "from_id", None)
        if from_id is None:
            return getattr(chat, "title", None)
        entity = entities.get(utils.get_peer_id(from_id))
        if entity is None:
            entity = self._entities.get(utils.get_peer_id(from_id))
        return convert.display_title(entity, me_id=None) if entity is not None else None

    def _lookup_fwd(self, fwd: object | None, entities: dict[int, object]) -> str | None:
        if fwd is None:
            return None
        name = getattr(fwd, "from_name", None)
        from_id = getattr(fwd, "from_id", None)
        if not name and from_id is not None:
            entity = entities.get(utils.get_peer_id(from_id))
            if entity is not None:
                name = convert.display_title(entity, me_id=None)
        return name or getattr(fwd, "post_author", None) or "?"

    def _lookup_message(self, msg: types.Message, chat: object, entities: dict[int, object]) -> dict:
        text = msg.message or ""
        if len(text) > _LOOKUP_TEXT_MAX:
            text = text[:_LOOKUP_TEXT_MAX] + "…"
        media = [a.get("type") for a in convert.convert_attachments(msg.media) or []]
        replies = getattr(getattr(msg, "replies", None), "replies", None)
        reactions = getattr(msg, "reactions", None)
        total = None
        if reactions is not None:
            total = sum(int(getattr(r, "count", 0) or 0) for r in getattr(reactions, "results", None) or [])
        return {
            "id": msg.id,
            "ts": int(msg.date.timestamp() * 1000) if msg.date is not None else None,
            "author": self._lookup_author(msg, chat, entities),
            "fwd": self._lookup_fwd(getattr(msg, "fwd_from", None), entities),
            "text": text,
            "media": media or None,
            "views": msg.views if isinstance(msg.views, int) else None,
            "forwards": msg.forwards if isinstance(msg.forwards, int) else None,
            "replies": replies if isinstance(replies, int) else None,
            "reactions": total,
        }

    # --- реакции из хаба (Ф10-3 C1, П1, П4, П5) -------------------------------------------------------------

    async def push_reaction(self, job: dict) -> ReactionResult:
        """Ф10-3 C1 + ревью: Telegram без Premium держит одну свою реакцию, `SendReactionRequest` ЗАМЕНЯЕТ
        набор. Сначала — мои реакции СЕЙЧАС из источника (`GetMessagesReactionsRequest`, разбор прямо из
        ответа; `min`/нет данных — `current_mine` задания как запасной вариант), затем операции по порядку от
        этого состояния (`_telegram_reaction_final`). Итог совпал с текущим — `done` без отправки (событие
        `reaction` из прочитанного, если оно не `min`); иначе `SendReaction([итог] или [])`. Тема форума —
        peer самого чата. После отправки — событие `reaction` с правдой из ответа (`UpdateMessageReactions`),
        нет её или `min` — повторным `GetMessagesReactionsRequest` (П4).
        Ошибки — как `push_read` (П5): `FloodWaitError` -> `retry` с `retry_after_s`; потеря входа ->
        `AuthRequired`; промах кэша сущностей (`ValueError`) и сеть -> `retry`; прочие `RPCError`
        (`REACTION_INVALID`, `MESSAGE_ID_INVALID`, `CHAT_WRITE_FORBIDDEN`, …) -> `drop`, в журнал — класс."""
        channel = job.get("channel") or {}
        try:
            chat_ext, _topic_id = convert.split_channel_ext_id(str(channel["ext_id"]))
            chat_id = int(chat_ext)
            msg_id = int(str(job["message_ext_id"]))
        except KeyError, TypeError, ValueError:
            return ReactionResult("drop", reason="bad_ref")
        if msg_id <= 0:
            return ReactionResult("drop", reason="bad_ref")
        ops = _telegram_reaction_ops(job.get("ops"))
        if not ops:
            return ReactionResult("done", reason="nothing")
        try:
            try:
                peer = await self._gateway.get_input_entity(chat_id)
            except ValueError:
                return ReactionResult("retry", reason="peer_unresolved")
            raw_reactions = convert.message_reactions_from_updates(
                await self._gateway.get_reactions(peer, msg_id), chat_id, msg_id
            )
            read = convert.reactions_list(raw_reactions)
            if read is not None:
                mine = [r["emoji"] for r in read if r.get("me") and isinstance(r.get("emoji"), str)]
            else:
                hint = job.get("current_mine")
                mine = [c for c in hint if isinstance(c, str)] if isinstance(hint, list) else []
            current = frozenset(c.replace(_VS16, "") for c in mine)
            if job.get("origin") == "session" and _owner_reacted(raw_reactions, current, job):
                # Ф15-1 П1: у сообщения реакция владельца — операции сессии не применяются.
                if read is not None:
                    self._emit_reaction_event(channel, msg_id, read)
                return ReactionResult("done", reason="owner_reacted")
            final = _telegram_reaction_final(ops, current)
            if final == current:
                if read is not None:
                    self._emit_reaction_event(channel, msg_id, read)
                return ReactionResult("done", reason="nothing")
            updates = await self._gateway.send_reaction(peer, msg_id, sorted(final))
        except errors.FloodWaitError as exc:
            return ReactionResult("retry", retry_after_s=float(exc.seconds), reason="FloodWait")
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError) as exc:
            return ReactionResult("retry", reason=type(exc).__name__)
        except errors.RPCError as exc:
            _logger.info(
                "connector: реакция на сообщение %s отклонена (%s)", job.get("message_id"), type(exc).__name__
            )
            return ReactionResult("drop", reason=type(exc).__name__)
        await self._emit_reaction_truth(peer, chat_id, msg_id, channel, updates)
        return ReactionResult("done")

    async def _emit_reaction_truth(
        self, peer: object, chat_id: int, msg_id: int, channel: dict, updates: object
    ) -> None:
        """П4: событие `reaction` с полным списком — из ответа отправки, иначе отдельным запросом. Сбой
        перечитывания реакцию не отменяет (она уже стоит): только строка журнала, правду догонит сверка."""
        raw = convert.message_reactions_from_updates(updates, chat_id, msg_id)
        reactions = self._reactions_named(raw, chat_id)
        if reactions is None:
            try:
                fresh = await self._gateway.get_reactions(peer, msg_id)
            except Exception as exc:  # noqa: BLE001 — реакция уже стоит, событие — ускорение, не правда
                _logger.info("connector: перечитать реакции не удалось (%s)", type(exc).__name__)
                return
            raw = convert.message_reactions_from_updates(fresh, chat_id, msg_id)
            reactions = self._reactions_named(raw, chat_id)
        if reactions is None:
            return
        self._emit_reaction_event(channel, msg_id, reactions)

    def _reactions_named(self, reactions: object | None, chat_id: int | None) -> list[dict] | None:
        """Ф23-2: `reactions_list` с именами поставивших из кэша сущностей (без запросов в Telegram); в личке
        (peer — пользователь, не я) собеседник известен и без `recent_reactions`."""
        entity = self._entities.get(chat_id) if chat_id is not None else None
        dm_name = None
        if isinstance(entity, types.User) and entity.id != self._me_id:
            dm_name = convert.display_title(entity, me_id=None)
        return convert.reactions_list(reactions, entities=self._entities, me_id=self._me_id, dm_name=dm_name)

    def _emit_reaction_event(self, channel: dict, msg_id: int, reactions: list[dict]) -> None:
        self.emit(
            {
                "type": "reaction",
                "channel": {"ext_id": str(channel["ext_id"]), "kind": channel.get("kind") or "dm"},
                "ext_id": str(msg_id),
                "reactions": reactions,
            }
        )

    # --- правка и «удалить у всех» из хаба (Ф14-8 C1, П4, П6) ---------------------------------------------

    async def push_msg_op(self, job: dict) -> MsgOpResult:
        """Ф14-8 C1. Ссылка — как у `push_reaction`: чат — часть `channel.ext_id` до `:` (тема форума — peer
        самого чата), `msg_id = int(message_ext_id)`; плохая ссылка -> `fail`/`other`.

        Правка: длина проверяется до запроса (4096 UTF-16 у текста, 1024 у подписи `file`) -> `too_long`;
        `EditMessageRequest` простым текстом; после — правда перечитыванием (`get_messages_by_ids` ->
        `_handle_live_message("edit", …)`, сбой перечитывания правку не отменяет). `MessageNotModified` ->
        `done`, только если перечитанный текст равен заданному (П6), иначе `fail`/`other` (`not_modified`).

        Удаление: `delete_messages(revoke=True)`; `pts_count > 0` -> событие `delete`, `done`;
        `pts_count == 0` или `MessageIdInvalid` (П4) -> перечитать: сообщения нет -> `delete`, `done`; есть ->
        `forbidden` (`pts_count == 0`) / `not_found` (`MessageIdInvalid`).

        Ошибки: `FloodWait` -> `retry` с `retry_after_s`; потеря входа -> `AuthRequired`; промах кэша
        сущностей и сеть -> `retry`; прочие — код по `_msg_op_error_code`, деталь — класс ошибки."""
        channel = job.get("channel") or {}
        try:
            chat_ext, _topic_id = convert.split_channel_ext_id(str(channel["ext_id"]))
            chat_id = int(chat_ext)
            msg_id = int(str(job["message_ext_id"]))
        except KeyError, TypeError, ValueError:
            return MsgOpResult("fail", code="other", detail="bad_ref")
        if msg_id <= 0:
            return MsgOpResult("fail", code="other", detail="bad_ref")
        op = job.get("op")
        text = job.get("text")
        if op == "edit":
            if not isinstance(text, str) or not text.strip():
                return MsgOpResult("fail", code="other", detail="bad_text")
            limit = _MAX_CAPTION_UTF16_LEN if job.get("message_kind") == "file" else _MAX_MESSAGE_UTF16_LEN
            if convert.utf16_len(text) > limit:
                return MsgOpResult("fail", code="too_long", detail=f"limit_{limit}")
        elif op != "delete":
            return MsgOpResult("fail", code="other", detail="bad_op")
        try:
            try:
                peer = await self._gateway.get_input_entity(chat_id)
            except ValueError:
                return MsgOpResult("retry", detail="peer_unresolved")
            if op == "edit":
                return await self._push_edit(peer, msg_id, text, job)
            return await self._push_revoke(peer, msg_id, channel, job)
        except errors.FloodWaitError as exc:
            return MsgOpResult("retry", retry_after_s=float(exc.seconds), detail="FloodWait")
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except (OSError, errors.ServerError, errors.TimedOutError) as exc:
            return MsgOpResult("retry", detail=type(exc).__name__)
        except errors.RPCError as exc:
            code = _msg_op_error_code(exc)
            _logger.info(
                "connector: %s сообщения %s отклонено (%s)", op, job.get("message_id"), type(exc).__name__
            )
            return MsgOpResult("fail", code=code, detail=type(exc).__name__)

    async def _push_edit(self, peer: object, msg_id: int, text: str, job: dict) -> MsgOpResult:
        try:
            await self._gateway.edit_message(peer, msg_id, text)
        except errors.MessageNotModifiedError:
            # П6: «не изменено» — успех, только если у источника уже ровно этот текст.
            try:
                msg = await self._reread_message(peer, msg_id)
            except (OSError, errors.ServerError, errors.TimedOutError) as exc:
                return MsgOpResult("retry", detail=f"not_modified_reread:{type(exc).__name__}")
            current = getattr(msg, "message", None) if msg is not None else None
            if not isinstance(current, str) or current.strip() != text.strip():
                return MsgOpResult("fail", code="other", detail="not_modified")
            await self._emit_edit_truth(msg, job)
            return MsgOpResult("done", detail="not_modified")
        try:
            msg = await self._reread_message(peer, msg_id)
        except Exception as exc:  # noqa: BLE001 — правка уже у источника; правду догонит эхо/сверка
            _logger.info("connector: перечитать сообщение после правки не удалось (%s)", type(exc).__name__)
            return MsgOpResult("done")
        if msg is None:
            _logger.info("connector: после правки сообщение %s не перечиталось", job.get("message_id"))
            return MsgOpResult("done")
        await self._emit_edit_truth(msg, job)
        return MsgOpResult("done")

    async def _emit_edit_truth(self, msg: object, job: dict) -> None:
        """Правда о правке — тем же путём, что живая правка (`edited_ts` — из `edit_date`). Сбой разбора —
        строка журнала: правка у источника уже есть, эхо/сверка догонят.
        Ревью Ф14-8: чат и отправитель — из самого перечитанного сообщения (`message_entities`, их разрешил
        Telethon из ответа `get_messages`): без них `_handle_live_message` молча выходит, если чата нет в
        кэше `_entities` (его наполняет частичный проход сверки), и в хабе остаётся старый текст."""
        try:
            await self._handle_live_message("edit", msg, message_entities(msg))
        except Exception as exc:  # noqa: BLE001
            mid = job.get("message_id")
            _logger.info("connector: событие правки сообщения %s не собрано (%s)", mid, type(exc).__name__)

    async def _push_revoke(self, peer: object, msg_id: int, channel: dict, job: dict) -> MsgOpResult:
        invalid = False
        try:
            removed = await self._gateway.delete_messages(peer, [msg_id])
        except errors.MessageIdInvalidError:
            invalid, removed = True, 0  # П4: как `pts_count == 0` — проверить, есть ли сообщение
        if removed <= 0:
            msg = await self._reread_message(peer, msg_id)
            if msg is not None:
                code = "not_found" if invalid else "forbidden"
                _logger.info("connector: сообщение %s не удалено у всех (%s)", job.get("message_id"), code)
                return MsgOpResult("fail", code=code, detail="MessageIdInvalid" if invalid else "pts_count_0")
        self.emit(
            {
                "type": "delete",
                "channel": {"ext_id": str(channel["ext_id"]), "kind": channel.get("kind") or "dm"},
                "ext_ids": [str(msg_id)],
            }
        )
        return MsgOpResult("done")

    async def _reread_message(self, peer: object, msg_id: int) -> object | None:
        """Одно сообщение заново (`get_messages_by_ids`); нет его (удалено, `MessageEmpty`) — `None`."""
        messages = await self._gateway.get_messages_by_ids(peer, [msg_id])
        msg = messages[0] if messages else None
        if msg is None or isinstance(msg, types.MessageEmpty):
            return None
        return msg

    # --- алерт о новой сессии (раздел «Алерт о новой сессии Telegram», план 7) -----------------------------

    def _load_alert_state(self) -> tuple[dict, bool]:
        """`(state, corrupt)`. Находка F5в критика куска 1c: файл ОТСУТСТВУЕТ (`{}`, `corrupt=False`) —
        законный первый прогон, только запомнить. Файл ЕСТЬ, но не читается (обрыв записи, порча диска) —
        `corrupt=True`: это НЕ первый прогон, молчать про НОВУЮ сессию в этом случае значило бы потерять
        алерт навсегда (state.json перезапишется с текущим набором и «забудет» об этом эпизоде)."""
        if self._state_path is None or not self._state_path.exists():
            return {}, False
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8")), False
        except (OSError, ValueError):
            return {}, True

    def _save_alert_state(self, state: dict) -> None:
        if self._state_path is None:
            return
        path = self._state_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
                f.flush()
                # Находка F5в критика 1c: fsync ПЕРЕД os.replace — без него содержимое может ещё лежать в
                # буфере ОС на момент внезапного выключения/сбоя (ноутбук, не сервер с UPS), и после
                # перезагрузки файл существует, но пуст/усечён — та же порча, что почини́л `corrupt=True`
                # выше, только предотвращённая заранее там, где это дёшево.
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def _merge_state(self, update: dict) -> None:
        """Ф34-3: `state.json` общий — алерт о сессиях и счётчик поиска по номеру телефона пишут только свои
        ключи поверх прочитанного (без `await` между чтением и записью)."""
        if self._state_path is None:
            return
        state, corrupt = self._load_alert_state()
        if corrupt and _PHONE_STATE_KEY not in update:
            # Битый файл перезаписывается (алерт о сессиях на старте) — счётчик номеров в нём потерян: не
            # обнулять лимит, а поставить поиск по номеру на паузу на сутки.
            pause = int(self._wall() * 1000) + _PHONE_WINDOW_MS
            self._phone_pause_until_ms = max(self._phone_pause_until_ms, pause)
            update = {**update, _PHONE_STATE_KEY: {"calls": [], "pause_until_ms": pause}}
        self._save_alert_state({**(state if isinstance(state, dict) else {}), **update})

    async def _check_session_alert(self) -> None:
        """Раз в сутки и на старте: `GetAuthorizationsRequest`. Известные `hash` — в state.json. Первый
        прогон (файла ЕЩЁ НЕ было) — только запомнить, без алертов. Появилась неизвестная (не текущая) ->
        алерт ядру; хеш запоминается ТОЛЬКО после успешной отправки алерта (находка F5а критика куска 1c —
        иначе отказ ядра «съедал» единственный шанс когда-либо сообщить об этой сессии). Битый state.json —
        НЕ первый прогон (находка F5в): лог-ошибка + алерт `tg_state_corrupt`. Best-effort: падение здесь не
        должно портить пульс/старт."""
        if self._state_path is None:
            return
        try:
            authorizations = await self._gateway.get_authorizations()
        except Exception as exc:  # noqa: BLE001 — не критично для работы коннектора
            _logger.warning("connector: GetAuthorizations упал: %s", type(exc).__name__)
            return  # находка F5г: не трогаем _last_alert_check_wall — health() попробует снова на пульсе
        self._last_alert_check_wall = self._wall()  # F5г: только ПОСЛЕ успешного запроса

        state, corrupt = self._load_alert_state()
        known: set = set(state.get("known_hashes", []))
        first_run = not corrupt and not state.get("initialized", False)

        if corrupt:
            _logger.error("connector: state.json алерта Telegram повреждён — считаем НЕ первым прогоном")
            with contextlib.suppress(Exception):
                await self._client.account_alert(
                    kind="tg_state_corrupt", text="файл состояния алерта Telegram повреждён"
                )

        for auth in authorizations:
            h = getattr(auth, "hash", None)
            if h is None or getattr(auth, "current", False) or h in known:
                continue
            if first_run:
                known.add(h)
                continue
            if await self._send_session_alert(auth):
                known.add(h)

        # Ф34-3: остальные ключи файла (счётчик поиска по номеру телефона) сохраняются.
        self._merge_state({"initialized": True, "known_hashes": sorted(known)})

    async def _send_session_alert(self, auth: object) -> bool:
        """`True` — алерт реально ушёл ядру (только тогда вызывающий вправе запомнить хеш, находка F5а)."""
        date_created = getattr(auth, "date_created", None)
        detail = {
            "device_model": getattr(auth, "device_model", None),
            "platform": getattr(auth, "platform", None),
            "country": getattr(auth, "country", None),
            "date_created": int(date_created.timestamp() * 1000) if date_created is not None else None,
        }
        text = f"новая сессия входа: {detail['platform'] or '?'} / {detail['device_model'] or '?'}"
        try:
            await self._client.account_alert(kind="tg_new_session", text=text, detail=detail)
        except Exception as exc:  # noqa: BLE001 — ядро недоступно/отказало: не критично, повторим на пульсе
            _logger.warning("connector: алерт о новой сессии не ушёл: %s", type(exc).__name__)
            return False
        return True

    # --- живые апдейты: обработчик кладёт «сырое» событие гейтвея в очередь, отдельная задача конвертирует --

    async def _live_updates_loop(self) -> None:
        while True:
            event = await self._gateway_events.get()
            try:
                await self._handle_gateway_event(event)
            except Exception:  # noqa: BLE001 — одно плохое живое событие не должно убивать поток остальных
                _logger.exception("connector: обработка живого события упала")

    async def _handle_gateway_event(self, event: object) -> None:
        await self._catchup_wake_check()  # Ф42-1: после сна — снимок позиций ДО отдачи события ядру
        if isinstance(event, NewMessageGE):
            await self._handle_live_message("upsert", event.message, event.entities)
        elif isinstance(event, EditedMessageGE):
            await self._handle_live_message("edit", event.message, event.entities)
        elif isinstance(event, DeletedMessagesGE):
            channel = None
            if event.channel_marked_id is not None:
                channel = {
                    "ext_id": str(event.channel_marked_id),
                    "kind": self._kind_guess(event.channel_marked_id),
                }
            self.emit({"type": "delete", "ext_ids": [str(i) for i in event.message_ids], "channel": channel})
        elif isinstance(event, ReadInboxGE):
            self.emit(
                {
                    "type": "read",
                    "channel": {
                        "ext_id": str(event.peer_marked_id),
                        "kind": self._kind_guess(event.peer_marked_id),
                    },
                    "up_to_ext_id": str(event.max_id),
                }
            )
        elif isinstance(event, PeerReadGE):
            chat_ext = str(event.peer_marked_id)
            self._emit_peer_read(chat_ext, self._entities.get(event.peer_marked_id), event.max_id, force=True)
        elif isinstance(event, ReactionsGE):
            reactions = self._reactions_named(event.reactions, event.peer_marked_id)
            if reactions is not None:
                self._emit_live_reaction(event.peer_marked_id, event.msg_id, event.top_msg_id, reactions)
            elif event.reactions is not None:
                # Ф18-2: урезанный апдейт (`min=True`, супергруппы и каналы) — «моя» неизвестна: перечитать.
                self._enqueue_reaction_refresh(event.peer_marked_id, event.msg_id, event.top_msg_id)
        elif isinstance(event, FoldersChangedGE):
            self._folders_changed = True
            self._folders_dirty = True
            self._request_sync("folders")
        elif isinstance(event, FoldersDirtyGE):
            self._folders_dirty = True  # пересчёт — в ближайшей сверке, не сейчас (решение 11.5)

    def _emit_live_reaction(
        self, chat_id: int, msg_id: int, top_msg_id: int | None, reactions: list[dict]
    ) -> None:
        """Событие `reaction` живого апдейта и перечитывания (Ф18-2 п.3 — один путь формирования).
        Находка F4 (round 2): реакция в теме форума несёт `top_msg_id` (id корня темы) — адресуем СРАЗУ в тему
        (сообщение лежит там, не в родителе); нет — родитель (`_kind_guess`)."""
        if top_msg_id is not None:
            channel = {"ext_id": f"{chat_id}:{top_msg_id}", "kind": "topic"}
        else:
            channel = {"ext_id": str(chat_id), "kind": self._kind_guess(chat_id)}
        self.emit({"type": "reaction", "channel": channel, "ext_id": str(msg_id), "reactions": reactions})

    # --- перечитывание реакций урезанных апдейтов (Ф18-2) ---------------------------------------------------

    def _enqueue_reaction_refresh(self, chat_id: int, msg_id: int, top_msg_id: int | None) -> None:
        chat = self._react_queue.get(chat_id)
        if chat is not None and msg_id in chat:
            if top_msg_id is not None:
                chat[msg_id] = top_msg_id  # повтор схлопывается
            return
        if self._react_queued >= REACT_REFRESH_QUEUE_MAX:
            self._react_dropped += 1
            return
        self._react_queue.setdefault(chat_id, {})[msg_id] = top_msg_id
        self._react_queued += 1
        _logger.debug("connector: реакции chat=%s msg=%s — в очередь перечитывания", chat_id, msg_id)
        if self._react_task is None or self._react_task.done():
            self._react_task = asyncio.ensure_future(self._reaction_refresh_loop())

    def _log_reaction_drops(self, *, force: bool = False) -> None:
        now = self._mono()
        if self._react_dropped and (force or now - self._react_dropped_logged >= _REACT_REFRESH_MINUTE_S):
            _logger.info(
                "connector: очередь перечитывания реакций полна — сброшено %s сообщ.", self._react_dropped
            )
            self._react_dropped = 0
            self._react_dropped_logged = now

    def _react_done(self, chat_id: int, batch: dict[int, int | None]) -> None:
        items = self._react_queue.get(chat_id)
        if items is None:
            return
        for msg_id in batch:
            if msg_id in items:
                del items[msg_id]
                self._react_queued -= 1
        if not items:
            del self._react_queue[chat_id]

    async def _reaction_refresh_loop(self) -> None:
        """Одна задача-воркер: окно сбора `REACT_REFRESH_WINDOW_S`, затем пачки по чатам (до 100 id одним
        запросом), не больше `REACT_REFRESH_PER_MIN` запросов в минуту и одного запроса на чат за
        `REACT_REFRESH_CHAT_COOLDOWN_S`; пустая очередь — задача завершается
        (новая постановка заведёт её снова). `FloodWaitError` — пауза на `seconds`, пачка остаётся."""
        await self._sleep(REACT_REFRESH_WINDOW_S)
        while self._react_queue:
            self._log_reaction_drops()
            now = self._mono()
            while self._react_req_times and now - self._react_req_times[0] >= _REACT_REFRESH_MINUTE_S:
                self._react_req_times.popleft()
            if len(self._react_req_times) >= REACT_REFRESH_PER_MIN:
                await self._sleep(self._react_req_times[0] + _REACT_REFRESH_MINUTE_S - now)
                continue
            ready = [
                cid
                for cid in self._react_queue
                if now - self._react_chat_last.get(cid, float("-inf")) >= REACT_REFRESH_CHAT_COOLDOWN_S
            ]
            if not ready:
                soonest = min(self._react_chat_last[cid] for cid in self._react_queue)
                await self._sleep(soonest + REACT_REFRESH_CHAT_COOLDOWN_S - now)
                continue
            chat_id = ready[0]
            batch = dict(list(self._react_queue[chat_id].items())[:_REACT_REFRESH_BATCH])
            try:
                peer = await self._gateway.get_input_entity(chat_id)
            except ValueError:
                _logger.debug("connector: реакции chat=%s — сущности нет в кэше, элементы сброшены", chat_id)
                self._react_done(chat_id, batch)
                continue
            self._react_req_times.append(self._mono())
            self._react_chat_last[chat_id] = self._react_req_times[-1]
            if len(self._react_chat_last) > REACT_REFRESH_QUEUE_MAX:
                horizon = self._react_req_times[-1] - REACT_REFRESH_CHAT_COOLDOWN_S
                self._react_chat_last = {c: t for c, t in self._react_chat_last.items() if t > horizon}
            try:
                updates = await self._gateway.get_reactions_many(peer, list(batch))
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as exc:
                _logger.info("connector: перечитывание реакций — FloodWait, пауза %s с", exc.seconds)
                await self._sleep(float(exc.seconds))
                continue
            except Exception as exc:  # noqa: BLE001 — сбой одной пачки не должен убивать воркер
                _logger.info("connector: перечитывание реакций не удалось (%s)", type(exc).__name__)
                self._react_done(chat_id, batch)
                continue
            self._react_done(chat_id, batch)
            sent = 0
            for msg_id, top_msg_id, raw in convert.message_reactions_all(updates, chat_id):
                if msg_id not in batch:
                    continue
                reactions = self._reactions_named(raw, chat_id)
                if reactions is None:  # снова `min` — пропуск
                    continue
                top = top_msg_id if top_msg_id is not None else batch[msg_id]
                self._emit_live_reaction(chat_id, msg_id, top, reactions)
                sent += 1
            _logger.debug("connector: перечитаны реакции chat=%s: %s сообщ.", chat_id, sent)
        self._log_reaction_drops(force=True)

    def _emit_peer_read(self, chat_ext: str, entity: object | None, max_id: int, *, force: bool) -> None:
        """Ф11-7, решение 2: событие `peer_read`. Каналы-ленты (broadcast) и форумы (у тем свой механизм) —
        нет. `force=False` (сверка) — только если маркер больше уже отправленного; живое — всегда."""
        if not max_id or max_id <= 0:
            return
        if isinstance(entity, types.Channel) and (not entity.megagroup or getattr(entity, "forum", False)):
            return
        if not force and max_id <= self._peer_read_sent.get(chat_ext, 0):
            return
        self._peer_read_sent[chat_ext] = max(max_id, self._peer_read_sent.get(chat_ext, 0))
        kind = convert.dialog_kind(entity) if entity is not None else "dm"
        self.emit(
            {"type": "peer_read", "channel": {"ext_id": chat_ext, "kind": kind}, "up_to_ext_id": str(max_id)}
        )

    def _kind_guess(self, marked_id: int) -> str:
        entity = self._entities.get(marked_id)
        return convert.dialog_kind(entity) if entity is not None else "dm"

    @staticmethod
    def _log_dialog_error(action: str, chat_ext: str, exc: Exception) -> None:
        """Находки F6/F11 (round 2): лог — только ext_id и класс ошибки, БЕЗ текста (правило логов спеки:
        ни текстов сообщений, ни названий чатов, ни номеров телефонов)."""
        _logger.warning("connector: %s chat=%s упал: %s", action, chat_ext, type(exc).__name__)

    async def _handle_live_message(
        self, event_type: str, msg: object, entities: dict[int, object] | None = None
    ) -> None:
        if self._me_id is None:
            return
        if entities:
            # Сущности из самого апдейта (гейтвей уже разрешил чат/отправителя, находка 2 приёмки куска 1b)
            # — раньше единственным источником был кэш, наполняемый ЧАСТИЧНЫМ проходом сверки (останов
            # после 20 тихих диалогов), и живое сообщение из любого не увиденного этим проходом чата тихо
            # терялось до следующей сверки (на старте — вообще до конца первой).
            self._entities.update(entities)
        peer = msg.peer_id
        chat_ext = convert.peer_ext_id(peer)
        entity = self._entities.get(int(chat_ext))
        if entity is None:
            # Даже гейтвей не разрешил чат (совсем незнакомый Telethon'у peer) — не ждём общего графика
            # сверки: просим внеочередную СЕЙЧАС ЖЕ, а не теряем сообщение до следующей по расписанию.
            _logger.info("connector: чат живого события не разрешился — просим внеочередную сверку")
            self._request_sync("unknown_chat")
            return
        if self._not_member(chat_ext, entity):
            # Ф17-2 П1: Telethon и после выхода сам тянет разницу канала (раз в 15 мин) — сообщения чата, где
            # владелец не участник, в хаб не идут (сущность — из самого апдейта, она свежее кэша, иначе из кэша).
            return
        chat_ref = convert.dialog_channel_ref(
            peer, entity, me_id=self._me_id, attrs=self._last_attrs.get(chat_ext)
        )
        await self._emit_message(event_type, chat_ref, peer, msg)
        self._note_new_channel(chat_ext)  # новый чат без attrs -> папки «грязные» (находка 2 приёмки 1b)

    # --- сборка и излучение событий сообщений (общее для сверки и живых апдейтов) --------------------------

    async def _leaf_channel_ref(self, chat_ref: dict, chat_peer: object, msg: object) -> dict:
        entity = self._entities.get(int(chat_ref["ext_id"]))
        is_forum = isinstance(entity, types.Channel) and bool(getattr(entity, "forum", False))
        if not is_forum:
            return chat_ref

        action = getattr(msg, "action", None)
        cache = self._topic_titles.setdefault(chat_ref["ext_id"], {})
        if isinstance(action, types.MessageActionTopicCreate):
            topic_id = msg.id
            title = action.title
            cache[topic_id] = title
        else:
            if isinstance(action, types.MessageActionTopicEdit) and action.title:
                topic_id_for_edit = convert.message_topic_id(msg) or msg.id
                cache[topic_id_for_edit] = action.title
            # Мелочь из отчёта исполнителя (round 2): сообщение форума БЕЗ forum_topic (`reply_to` не задан
            # вовсе) -> тема General (`<chat>:1`), а не повисает прямо в родителе — форум ВСЕГДА
            # раскладывается по темам, включая General (спека: «сообщение форума без forum_topic -> тема
            # General <chat>:1»).
            topic_id = convert.message_topic_id(msg) or convert.GENERAL_TOPIC_ID
            title = await self._topic_title(chat_ref["ext_id"], chat_peer, topic_id)

        leaf = convert.topic_channel_ref(chat_ref["ext_id"], topic_id, title)
        leaf["parent"] = chat_ref
        return leaf

    async def _topic_title(self, chat_ext_id: str, chat_peer: object, topic_id: int) -> str | None:
        """Находка F9 (round 2): провал `GetForumTopicsByID` (например `FloodWaitError`) раньше КЭШИРОВАЛСЯ
        как запасное имя `"Тема N"` НАВСЕГДА — и если тема уже существовала в ядре под настоящим именем,
        следующее сообщение переименовывало бы её в этот угадыш. Теперь провал НЕ кэшируется (следующая
        попытка снова спросит источник); если тема уже известна ЯДРУ (проверяем НАПРЯМУЮ через
        `channels_heads` — свой локальный кэш `_topic_titles` мог быть пуст просто потому, что коннектор
        только что запущен и ещё ни разу не сверялся), отдаём `None` (`ChannelRef.title=None` -> ядро
        оставит прежнее имя), а не перезаписываем его угадышем."""
        cache = self._topic_titles.setdefault(chat_ext_id, {})
        if topic_id in cache:
            return cache[topic_id]
        if topic_id == convert.GENERAL_TOPIC_ID:
            cache[topic_id] = convert.GENERAL_TOPIC_TITLE
            return convert.GENERAL_TOPIC_TITLE

        topic_ext = f"{chat_ext_id}:{topic_id}"
        try:
            topics = await self._gateway.get_forum_topics(chat_peer, ids=[topic_id])
            title = topics[0].title if topics and hasattr(topics[0], "title") else None
        except Exception as exc:  # noqa: BLE001 — провал резолва названия темы не должен ронять сверку/приём
            self._log_dialog_error("резолв темы", topic_ext, exc)
            title = None

        if title is not None:
            cache[topic_id] = title
            return title

        with contextlib.suppress(Exception):
            heads = await self._client.channels_heads()
            if any(h["ext_id"] == topic_ext for h in heads):
                return None  # уже известна ядру -> не переименовывать угадышем
        return f"Тема {topic_id}"  # новая тема — какое-то имя нужно прямо сейчас, но НЕ кэшируем его

    def _source_url_info(self, chat_ext_id: str) -> tuple[str | None, int | None]:
        entity = self._entities.get(int(chat_ext_id))
        if not isinstance(entity, types.Channel):
            return None, None  # личка и малая группа — раздел «Идентификаторы»: ссылки нет
        username = getattr(entity, "username", None)
        if username:
            return username, None
        return None, entity.id

    async def _emit_message(
        self, event_type: str, chat_ref: dict, chat_peer: object, msg: object, *, emit=None
    ) -> None:
        """`emit` — куда класть события сообщения (умолчание — буфер каркаса `self.emit`); догон Ф42-1 сначала
        собирает их списком, чтобы знать события каждой страницы."""
        emit = emit or self.emit
        action = getattr(msg, "action", None)
        if isinstance(action, types.MessageActionChannelMigrateFrom):
            payload = convert.build_channel_migrate_event(msg, new_channel=chat_ref)
            emit({"type": "channel_migrate", **payload})
            return
        # MessageActionChatMigrateTo (старая группа) — НЕ return: эмитится ниже как обычное служебное
        # событие (находка 6 приёмки куска 1b). Без этого max_ext_sort старой группы никогда не догонял бы
        # `top_message`, и сверка на КАЖДОМ проходе заново пыталась бы «догнать» тот же самый id, сбрасывая
        # счётчик тишины («20 подряд») и не давая проходу когда-либо остановиться на этом чате.

        leaf_ref = await self._leaf_channel_ref(chat_ref, chat_peer, msg)
        public_username, private_num = self._source_url_info(chat_ref["ext_id"])
        ctx = convert.ConvertContext(
            channel=leaf_ref,
            channel_kind_for_sender=chat_ref["kind"],
            me_id=self._me_id,
            entities=self._entities,
            public_username=public_username,
            private_channel_num=private_num,
            chat_peer=chat_peer,
        )
        payload = convert.convert_message(msg, ctx)
        emit({"type": event_type, **payload})

        reactions = self._reactions_named(getattr(msg, "reactions", None), int(chat_ref["ext_id"]))
        if reactions is not None:
            emit({"type": "reaction", "channel": leaf_ref, "ext_id": str(msg.id), "reactions": reactions})

    # --- сверка -------------------------------------------------------------------------------------------

    async def sync(self, reason: str = "scheduled") -> None:
        try:
            await self._sync_impl(reason)
        except errors.FloodWaitError as exc:
            # Раздел «Сверка»: FloodWaitError наружу как есть — пауза ЦИКЛА СВЕРКИ на e.seconds, не падение.
            _logger.warning("connector: FloodWaitError в sync, пауза %s с", exc.seconds)
            await self._sleep(float(exc.seconds))

    def _heads_index(self, heads: list[dict]) -> tuple[dict[str, int], set[str]]:
        """`last[chat] = max(max_ext_sort родителя, всех его топиков)` — форум делит id-пространство с
        топиками (раздел «Сверка»). `known_chats` — верхнеуровневые ext_id, известные ядру."""
        last_by_chat: dict[str, int] = {}
        known_chats: set[str] = set()
        for h in heads:
            if h["kind"] == "topic" or h.get("skip"):
                continue  # `skip` (Ф54-2): личка с ботом хаба — ядро её отбрасывает, для сверки её нет
            known_chats.add(h["ext_id"])
            if h["max_ext_sort"] is not None:
                last_by_chat[h["ext_id"]] = max(last_by_chat.get(h["ext_id"], 0), h["max_ext_sort"])
        for h in heads:
            if h["kind"] != "topic" or h["parent_ext_id"] is None:
                continue
            known_chats.add(h["parent_ext_id"])
            if h["max_ext_sort"] is not None:
                last_by_chat[h["parent_ext_id"]] = max(
                    last_by_chat.get(h["parent_ext_id"], 0), h["max_ext_sort"]
                )
        return last_by_chat, known_chats

    def _is_recently_active(self, date: object | None) -> bool:
        if date is None:
            return False
        now_ms = int(self._wall() * 1000)
        return now_ms - int(date.timestamp() * 1000) <= _INITIAL_LOAD_ACTIVE_MS

    async def _sync_impl(self, reason: str) -> None:
        await self._catchup_wake_check()  # Ф42-1: после сна — снимок позиций ДО свежих `channels_heads`
        heads = await self._client.channels_heads()
        last_by_chat, known_chats = self._heads_index(heads)
        # Ф54-2: диалоги, которые ядро отбрасывает на приёме (личка с ботом хаба), — не грузить и не догонять.
        skip_chats = {h["ext_id"] for h in heads if h.get("skip")}

        # Ф17-1 C: беседы после вступления — при любой причине сверки (причины сливаются в одну); одна
        # «join» — узкое действие, без полной сверки.
        join_loaded = await self._run_join_loads(known_chats)
        if reason == "join":
            return

        if reason == "folders":
            # Находка F14 (round 2): апдейт папки — узкое дешёвое действие (перезапросить фильтры,
            # пересчитать attrs), НЕ полная сверка с догоном/окном/стартовой загрузкой. Без этого 11 подряд
            # правок папки (владелец добавляет чаты по одному — 11 `UpdateDialogFilter`) запускали бы 11 ПОЛНЫХ
            # сверок (GetDialogs + окно до 20 GetHistory + GetDialogFilters + полный проход диалогов для
            # attrs); `base.py::_sync_scheduler_loop` уже слил очередь причин в одну (см. находку там же), но
            # если единственная причина — "folders", сама сверка тоже должна остаться узкой.
            await self._maybe_recompute_folders(reason, known_chats)
            return

        # Ф42-1: дыры из курсора ядра (раз за процесс; сбой чтения — сверка не идёт: иначе первая же запись
        # курсора затёрла бы сохранённые дыры) и снимок позиций, снятый до живого потока.
        await self._catchup_load()
        snap_epoch = self._catchup_snap_epoch
        self._catchup_seen_heads = last_by_chat
        gap_dialogs: dict[str, object] = {}  # чаты с дырами, встреченные в этом проходе: ext -> диалог

        seen: list = []
        pending_initial: list[tuple] = []
        consecutive_stale = 0
        total = 0

        async for d in self._gateway.iter_dialogs():
            total += 1
            chat_ext = convert.peer_ext_id(d.peer)
            self._entities[int(chat_ext)] = d.entity
            if chat_ext in skip_chats:
                self._gaps.pop(chat_ext, None)
                continue
            if self._not_member(chat_ext, d.entity):
                # Ф17-2 П1/П5: диалог обычной группы, из которой вышли (или выгнали), остаётся в списке — его
                # сообщения не догоняются, окно его не перечитывает, стартовой загрузки нет.
                self._gaps.pop(chat_ext, None)  # Ф42-1 G: дыру не заводить, заведённую — снять
                if self._catchup_snap_epoch == snap_epoch:
                    self._catchup_snap.pop(chat_ext, None)
                consecutive_stale += 1
                if consecutive_stale >= _SYNC_STOP_STALE or total >= _SYNC_STOP_TOTAL:
                    break
                continue
            seen.append(d)

            if chat_ext in known_chats:
                last = last_by_chat.get(chat_ext, 0)
                snap_last = self._catchup_snap.get(chat_ext)
                if snap_last is not None:
                    # Ф42-1 D: живое сообщение этого подключения могло поднять максимум в ядре раньше сверки.
                    last = min(last, snap_last)
                has_news = d.top_message is not None and d.top_message > last
                if has_news:
                    self._gap_note(chat_ext, last, d.top_message + 1)
                if self._catchup_snap_epoch == snap_epoch:
                    # По чату решено (дыра заведена или новостей нет) — его запись снимка израсходована. Не
                    # встреченные проходом чаты остаются в снимке; если за время прохода было новое
                    # подключение, не расходуется ничего (список диалогов мог быть получен до обрыва).
                    self._catchup_snap.pop(chat_ext, None)
                # Ф42-3: дыра добора не в счёт — она позади известных сообщений и идёт часами: ни маркер
                # прочтения из-за неё не откладывается, ни проход диалогов не удлиняется.
                has_gap = bool(self._gaps_of(chat_ext, False))
                if has_gap:
                    # Маркер прочтения — после страниц чата (`_fill_gaps`): ядро двигает его только по
                    # сообщению, которое у него уже есть.
                    gap_dialogs[chat_ext] = d
                else:
                    self._emit_dialog_read(chat_ext, d)
                if d.pinned or has_news or has_gap:
                    consecutive_stale = 0
                else:
                    consecutive_stale += 1
            else:
                # Находка F13 (round 2): счётчик «тихих» сбрасывают ТОЛЬКО неизвестные АКТИВНЫЕ (≤30 дней)
                # диалоги — раньше ЛЮБОЙ неизвестный диалог сбрасывал его (в т.ч. давно молчащий, никогда не
                # попадающий в хаб), и проход при сотнях старых неизвестных диалогов никогда не останавливался
                # раньше `_SYNC_STOP_TOTAL` — полный обход на КАЖДОЙ сверке (раз в ~10 мин) вместо дешёвого
                # раннего стопа.
                is_active = self._is_recently_active(d.last_message_date)
                if is_active and chat_ext in join_loaded:
                    consecutive_stale = 0  # Ф17-1: только что загружена после вступления — не второй раз
                elif is_active:
                    consecutive_stale = 0
                    # Кусок 7b, К1: маркер прочтения диалога — вместе с ним, чтобы история стартовой загрузки
                    # сразу пришла прочитанной (раньше маркер ждал следующей сверки, 10 ± 3 мин).
                    pending_initial.append(
                        (d.peer, d.entity, chat_ext, d.read_inbox_max_id, d.read_outbox_max_id)
                    )
                else:
                    consecutive_stale += 1

            if consecutive_stale >= _SYNC_STOP_STALE or total >= _SYNC_STOP_TOTAL:
                break

        # Дыра снимается только по определённому признаку; здесь — «ядро чата больше не знает» (в проходе
        # диалогов чат мог и не встретиться — это не признак). Новые дыры и остаток снимка — в курсор ДО
        # заполнения.
        for chat_ext in [c for c in self._gaps if c not in known_chats]:
            del self._gaps[chat_ext]
            _logger.warning("connector: дыра chat=%s снята — ядро чата не знает", chat_ext)
        for chat_ext in [c for c in self._catchup_snap if c not in known_chats]:
            del self._catchup_snap[chat_ext]
        await self._catchup_save()
        # Ф42-3: добор потерянного — засев один раз (после прохода диалогов: участие известно), дальше его
        # дыры заполняются после обычных тем же бюджетом. Выключатель аккаунта `history_refill`.
        refill = await self._history_refill_enabled()
        if refill:
            try:
                await self._history_seed()
            except Exception as exc:  # noqa: BLE001 — засев повторится следующей сверкой, сверку не роняет
                _logger.warning("connector: добор не засеян (%s)", type(exc).__name__)
        if await self._fill_gaps(gap_dialogs, history=refill):
            self._request_sync("catchup")  # бюджет страниц исчерпан — следующий заход сразу

        # Находка 4 приёмки куска 1b: стартовая загрузка внутри sync() с таймаутом каркаса 600 с — при
        # сотнях активных неизвестных диалогов (30 сообщений + 1–3 с джиттера на КАЖДЫЙ) сверку отменило бы
        # на середине, а окно/папки на старте не выполнились бы ни разу. Не больше `_INITIAL_LOAD_BATCH_MAX`
        # диалогов за один заход; остаток -> `_request_sync("initial")`, следующая пачка идёт СРАЗУ, а не по
        # общему графику (раздел D куска 1a: два sync() одновременно не идут — станет в очередь честно).
        initial_batch = pending_initial[:_INITIAL_LOAD_BATCH_MAX]
        initial_rest = pending_initial[_INITIAL_LOAD_BATCH_MAX:]
        await self._run_initial_load(initial_batch, has_more=bool(initial_rest))
        if initial_rest:
            self._request_sync("initial")

        # Раскладка по папкам — для каналов, известных к КОНЦУ прохода: диалоги, только что заведённые
        # стартовой загрузкой (несколько строк выше), уже реально существуют в core (их upsert идёт РАНЬШЕ в
        # той же пачке) — без этого их attrs ждали бы следующей сверки. Перечитывание окна, наоборот, не
        # трогает их: только что загруженные 30 сообщений и так предельно свежие, повторный проход по ним
        # был бы чистым дублированием (`_reread_window` — только для диалогов, известных ДО этого прохода).
        known_after_load = known_chats | join_loaded
        known_after_load |= {chat_ext for _peer, _entity, chat_ext, *_read in initial_batch}

        await self._reread_window(reason, known_chats, seen)
        await self._maybe_recompute_folders(reason, known_after_load)
        await self._maybe_refresh_contacts()

    # --- контакты (Ф14-2 B1) -----------------------------------------------------------------------------

    async def _maybe_refresh_contacts(self) -> None:
        """Ф14-2 B1: раз в час (первая сверка — на старте) `contacts.GetContacts(hash=<прошлый>)`;
        `ContactsNotModified` — ничего; список изменился — публикация `tg_contacts` порциями (`meta_parts`:
        порции, затем заголовок). Без телефонов, ботов, удалённых и себя (`contacts_items`). Сбой —
        предупреждение в журнал без имён, повтор через час; сверку он не роняет."""
        now = self._mono()
        if now < self._contacts_next_refresh:
            return
        self._contacts_next_refresh = now + _CONTACTS_REFRESH_INTERVAL_S
        try:
            result = await self._gateway.get_contacts(self._contacts_hash)
        except _AUTH_LOST_ERRORS as exc:
            raise AuthRequired(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — контакты необязательны: без них нет только поиска «Кому»
            _logger.warning("connector: контакты не получены (%s)", type(exc).__name__)
            return
        if result is None:
            return
        self._contacts_hash = result.hash
        items = contacts_items(result.users, me_id=self._me_id)
        if items == self._contacts_published:
            return
        for event in meta_parts.meta_events("tg_contacts", items):
            self.emit(event)
        self._contacts_published = items

    # --- просмотры постов каналов (Ф23-3) ------------------------------------------------------------------

    async def _views_loop(self) -> None:
        """Ф23-3 п.4: первый проход — через `_VIEWS_FIRST_DELAY_S` после старта, дальше раз в час (или раньше
        повтор после FloodWait/сети). Любой сбой прохода — строка журнала (только класс), коннектор живёт."""
        await self._sleep(_VIEWS_FIRST_DELAY_S)
        while not self._stopping:
            try:
                delay = await self._views_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — просмотры необязательны: ядро недоступно и т.п.
                _logger.warning("connector: проход просмотров не удался (%s)", type(exc).__name__)
                delay = _VIEWS_RETRY_S
            await self._sleep(delay)

    async def _views_pass(self) -> float:
        """Один проход: беседы-каналы аккаунта из ядра (`channels_heads`: `kind='channel'`, не в архиве —
        вышли из канала -> архив Ф17-2, последний пост моложе 72 ч) -> история канала (`GetHistory`, до 100,
        счётчики там уже есть; просмотры не накручиваются) -> событие `views` по постам моложе 72 ч. Список
        живёт в БД ядра — после рестарта проход видит те же каналы без прохода диалогов. Возвращает паузу до
        следующего прохода."""
        if self.auth_required:
            return _VIEWS_INTERVAL_S
        heads = await self._client.channels_heads()
        now_ms = int(self._wall() * 1000)
        since_ms = now_ms - _VIEWS_MAX_AGE_MS
        targets = [
            h["ext_id"]
            for h in heads
            if h.get("kind") == "channel"
            and not h.get("archived")
            and isinstance(h.get("last_msg_ts"), int)
            and h["last_msg_ts"] >= since_ms
        ]
        first = True
        for chat_ext in targets:
            if self._stopping or self.auth_required:
                break
            try:
                marked = int(chat_ext)
            except ValueError:
                continue
            entity = self._entities.get(marked)
            if entity is not None and self._not_member(chat_ext, entity):
                continue
            try:
                peer = await self._gateway.get_input_entity(marked)
            except ValueError:
                # Промах кэша сущностей сессии — без прохода диалогов: канал попадёт в следующий проход,
                # когда сверка или живое сообщение его увидят.
                continue
            except Exception as exc:  # noqa: BLE001 — тот же разбор, что у истории
                delay = self._views_error(chat_ext, exc)
                if delay:
                    return delay
                continue
            if not first:
                await self._sleep(_VIEWS_CHANNEL_PAUSE_S)  # пауза между запросами истории каналов
            first = False
            try:
                # Тот же замок, что у сверки: проход не пересекается с ней запросами к Telegram.
                async with self._sync_lock:
                    messages, entities = await self._gateway.get_messages(
                        peer, min_id=None, limit=_VIEWS_HISTORY_LIMIT
                    )
            except Exception as exc:  # noqa: BLE001 — см. `_views_error`
                delay = self._views_error(chat_ext, exc)
                if delay:
                    return delay
                continue
            self._entities.update(entities)
            entity = entities.get(marked, entity)
            if entity is not None and self._not_member(chat_ext, entity):
                continue
            items = convert.views_items(messages, since_ms=since_ms)
            for start in range(0, len(items), _VIEWS_HISTORY_LIMIT):
                self.emit(
                    {
                        "type": "views",
                        "channel": {"ext_id": chat_ext, "kind": "channel"},
                        "ts": now_ms,
                        "items": items[start : start + _VIEWS_HISTORY_LIMIT],
                    }
                )
        return _VIEWS_INTERVAL_S

    @staticmethod
    def _views_error(chat_ext: str, exc: Exception) -> float:
        """Ошибка запроса прохода просмотров: FloodWait, сеть, потеря входа — проход откладывается (пауза до
        следующего, > 0); прочее — только этот канал (0). В журнал — ext_id и класс ошибки, без текстов."""
        name = type(exc).__name__
        if isinstance(exc, errors.FloodWaitError):
            _logger.warning("connector: просмотры chat=%s — %s, проход отложен", chat_ext, name)
            return max(float(exc.seconds), _VIEWS_RETRY_S)
        if isinstance(exc, _VIEWS_NET_ERRORS + _AUTH_LOST_ERRORS):
            # Потерю входа ловят сверка и пульс (`AuthRequired`); здесь — только отложить проход.
            _logger.warning("connector: просмотры chat=%s — %s, проход отложен", chat_ext, name)
            return _VIEWS_RETRY_S
        _logger.warning("connector: просмотры chat=%s — %s", chat_ext, name)
        return 0.0

    # --- догон без потерь (Ф42-1, `docs/architecture.md`) ----------------------------------

    def _emit_dialog_read(self, chat_ext: str, d: object) -> None:
        """Маркеры прочтения диалога известного чата (свой — `read`, собеседника — `peer_read`)."""
        if d.read_inbox_max_id:
            self.emit(
                {
                    "type": "read",
                    "channel": {"ext_id": chat_ext, "kind": convert.dialog_kind(d.entity)},
                    "up_to_ext_id": str(d.read_inbox_max_id),
                }
            )
        self._emit_peer_read(chat_ext, d.entity, d.read_outbox_max_id, force=False)

    def _gap_note(self, chat_ext: str, lo: int, hi: int, *, history: bool = False) -> None:
        """Завести дыру `lo < id < hi` у чата или расширить уже заведённую (пересекающиеся и смежные
        сливаются; раздельные остаются раздельными — уже заполненное между ними заново не запрашивается).
        Память здесь может стать только ШИРЕ записанного в курсоре — это безопасно.

        `history` (Ф42-3) — дыра добора истории (`"h": 1`): её события несут `history=True`. Сливаются только
        дыры одного вида — у слитой обычной и добора пропал бы один из признаков; правило `one` к добору не
        применяется."""
        gaps = self._gaps.get(chat_ext, [])
        now_ms = int(self._wall() * 1000)
        if lo <= 0 and not history:
            # Ядро не знает ни одного сообщения чата — как до Ф42-1: только последняя страница (`one`), а не
            # вся история чата с первого сообщения (догрузка истории — вне объёма).
            regular = [g for g in gaps if not g.get("h")]
            if regular:
                newest = max(regular, key=lambda g: g["hi"])
                newest["hi"] = max(newest["hi"], hi)
            else:
                self._gaps[chat_ext] = [*gaps, {"lo": 0, "hi": hi, "t": now_ms, "p": 0, "m": 0, "one": True}]
            return
        merged = {"lo": lo, "hi": hi, "t": now_ms, "p": 0, "m": 0}
        if history:
            merged["h"] = 1
        rest: list[dict] = []
        for g in gaps:
            same_kind = bool(g.get("h")) == history
            if same_kind and not g.get("one") and g["lo"] < merged["hi"] and merged["lo"] < g["hi"]:
                merged = {
                    **merged,
                    "lo": min(g["lo"], merged["lo"]),
                    "hi": max(g["hi"], merged["hi"]),
                    "t": min(g["t"], merged["t"]),
                    "p": g["p"] + merged["p"],
                    "m": g["m"] + merged["m"],
                }
            else:
                rest.append(g)
        self._gaps[chat_ext] = [*rest, merged]

    def _gaps_of(self, chat_ext: str, history: bool) -> list[dict]:
        """Дыры чата одного вида: обычные (`history=False`) или добора истории."""
        return [g for g in self._gaps.get(chat_ext, []) if bool(g.get("h")) == history]

    @staticmethod
    def _catchup_value(
        gaps: dict[str, list[dict]], snap: dict[str, int], hist: int | None = None, hist_chats: int = 0
    ) -> str:
        body: dict = {
            "v": _CATCHUP_CURSOR_VERSION,
            "gaps": [{"c": chat, **g} for chat in sorted(gaps) for g in gaps[chat]],
        }
        if snap:
            body["snap"] = snap
        if hist is not None:
            # Ф42-3 D: отметка засева добора истории и число засеянных чатов.
            body["hist"] = hist
            body["hn"] = hist_chats
        return json.dumps(body, sort_keys=True)

    @staticmethod
    def _catchup_parse(raw: dict | None) -> tuple[dict[str, list[dict]], dict[str, int], int | None, int]:
        """Курсор аккаунта -> (дыры, снимок позиций, отметка добора истории, число его чатов). Чужой или битый
        формат — пусто с предупреждением; курсор без отметки добора (до Ф42-3) читается."""
        if not raw or not raw.get("value"):
            return {}, {}, None, 0
        try:
            body = json.loads(raw["value"])
            if not isinstance(body, dict) or body.get("v") != _CATCHUP_CURSOR_VERSION:
                raise ValueError("формат")  # noqa: TRY301 — одна ветка разбора на все виды брака
            gaps: dict[str, list[dict]] = {}
            for item in body.get("gaps") or []:
                chat, lo, hi = item["c"], item["lo"], item["hi"]
                if not isinstance(chat, str) or type(lo) is not int or type(hi) is not int:
                    raise TypeError("дыра")  # noqa: TRY301
                int(chat)  # ext_id чата Telegram — помеченный числовой id
                gap = {"lo": lo, "hi": hi, "t": int(item.get("t", 0)), "p": int(item.get("p", 0))}
                gap["m"] = int(item.get("m", 0))
                if item.get("one"):
                    gap["one"] = True
                if item.get("h"):
                    gap["h"] = 1
                gaps.setdefault(chat, []).append(gap)
            snap = {str(k): int(v) for k, v in (body.get("snap") or {}).items()}
            hist = int(body["hist"]) if body.get("hist") is not None else None
            hist_chats = int(body.get("hn") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            _logger.warning("connector: курсор догона не разобран (%s) — считаю пустым", type(exc).__name__)
            return {}, {}, None, 0
        return gaps, snap, hist, hist_chats

    async def _catchup_load(self) -> None:
        """Раз за процесс: дыры и неизрасходованный снимок позиций из курсора аккаунта в ядре. Исключение —
        наружу: без прочитанного курсора писать его нельзя (затёрлись бы дыры прошлого процесса)."""
        if self._catchup_loaded:
            return
        gaps, snap, hist, hist_chats = self._catchup_parse(await self._client.get_cursor())
        self._catchup_saved = self._catchup_value(gaps, snap, hist, hist_chats)
        self._gaps = gaps
        self._catchup_hist, self._catchup_hist_chats = hist, hist_chats
        # Записи снимка прошлого процесса старше нынешних — они главнее.
        self._catchup_snap = {**self._catchup_snap, **snap}
        self._catchup_loaded = True

    async def _catchup_save(self, gaps: dict[str, list[dict]] | None = None) -> None:
        """Записать дыры (и ждущий снимок) в курсор аккаунта: пустая пачка событий с курсором. Без `ts` —
        ядро сверяет монотонность только у курсоров с меткой времени."""
        await self._catchup_load()
        value = self._catchup_value(
            self._gaps if gaps is None else gaps,
            self._catchup_snap,
            self._catchup_hist,
            self._catchup_hist_chats,
        )
        if value == self._catchup_saved:
            return
        await self._client.push_events([], cursor={"value": value})
        self._catchup_saved = value

    async def _catchup_snapshot(self) -> None:
        """Раздел D: позиции `last` — ДО того, как живые события этого подключения могли попасть в ядро.
        Снимок — по чатам: запись чата живёт (и лежит в курсоре), пока сверка не встретит этот чат в проходе и
        не решит по нему; уже ждущая запись не перезаписывается — она старше, значит, ниже. Позиция новой
        записи — не выше той, что видела последняя сверка: живое сообщение, проскочившее в ядро после неё
        (сон, обрыв), границу не поднимает. Сбой ядра подключению не мешает: сверка возьмёт свежие позиции."""
        # Номер подключения: сверка, шедшая в этот момент, записи снимка больше не расходует — список диалогов
        # у неё мог быть получен ещё до обрыва.
        self._catchup_snap_epoch += 1
        try:
            await self._catchup_load()
            fresh, _known = self._heads_index(await self._client.channels_heads())
            for chat_ext, last in fresh.items():
                if chat_ext not in self._catchup_snap:
                    self._catchup_snap[chat_ext] = min(last, self._catchup_seen_heads.get(chat_ext, last))
            await self._catchup_save()
        except Exception as exc:  # noqa: BLE001 — см. докстринг
            _logger.warning("connector: снимок позиций догона не снят (%s)", type(exc).__name__)

    async def _catchup_wake_check(self) -> None:
        """Сон замечается раньше детектора каркаса (тот тикает раз в 5 с): тот же признак — настенные часы
        ушли вперёд монотонных больше порога. Зовётся перед отдачей ядру каждого живого события и перед тем,
        как сверка берёт свежие позиции; обнаружен сон — сначала снимок позиций. Под замком: живое событие не
        уйдёт в ядро, пока снимок, начатый сверкой, не снят."""
        async with self._catchup_wake_lock:
            wall, mono = self._wall(), self._mono()
            last, self._catchup_clock = self._catchup_clock, (wall, mono)
            if last is None or (wall - last[0]) - (mono - last[1]) <= _SLEEP_THRESHOLD_S:
                return
            _logger.info("connector: сон замечен — снимок позиций догона до живых событий")
            await self._catchup_snapshot()

    async def _catchup_delivered(self, lost_before: int) -> str:
        """Дошла ли страница до ядра: `ok` — буфер событий ушёл, ничего не выброшено; `down` — буфер не ушёл
        за отведённое время (ядро недоступно); `lost` — за это время каркас что-то выбросил (отказ пачки,
        переполнение). Курсор двигается только после `ok` — опережение курсора даёт потерю."""
        if self._emit_count != self._gone_count and not await self.flush_events(_CATCHUP_FLUSH_TIMEOUT_S):
            return "down"
        return "ok" if self._lost_count == lost_before else "lost"

    def _warn_stale_gaps(self) -> None:
        now_ms = int(self._wall() * 1000)
        now = self._mono()
        for chat_ext in self._gaps:
            gaps = self._gaps_of(chat_ext, False)  # Ф42-3: добор идёт часами по замыслу — о нём не пишем
            if not gaps:
                continue
            age_ms = now_ms - min(g["t"] for g in gaps)
            if age_ms <= _CATCHUP_STALE_WARN_MS:
                continue
            if now - self._catchup_warned.get(chat_ext, float("-inf")) < _CATCHUP_STALE_WARN_MS / 1000:
                continue
            self._catchup_warned[chat_ext] = now
            _logger.warning("connector: дыра chat=%s не закрыта %d мин", chat_ext, age_ms // 60_000)

    async def _gap_drop(self, chat_ext: str, why: str) -> None:
        """Снять дыры чата по определённому признаку (вышли, чат недоступен навсегда, ядро чата не знает)."""
        if chat_ext not in self._gaps:
            return
        state = {c: g for c, g in self._gaps.items() if c != chat_ext}
        await self._catchup_save(state)
        self._gaps = state
        _logger.warning("connector: дыра chat=%s снята — %s", chat_ext, why)

    async def _fill_gaps(self, dialogs: dict[str, object], *, history: bool = False) -> bool:
        """Заполнитель (разделы B и E): ВСЕ чаты с дырами, встречен их диалог в этом проходе или нет (Telethon
        листает диалоги по дате — чат, получивший сообщение между страницами, в проходе не выдаётся). Чаты —
        от меньшей дыры к большей, внутри чата — от новой дыры к старой, страницы по `_CATCHUP_PAGE`; на все
        чаты — `_CATCHUP_PAGES_PER_SYNC` страниц за заход. Peer — из кэша сущностей сессии (промах — дыра ждёт
        следующего захода). После обычных дыр — маркеры прочтения диалогов (`dialogs`), встреченных проходом.
        `True` — бюджет исчерпан, обычные дыры остались (нужен заход «catchup»). FloodWait и потеря входа —
        наружу: дыры лежат в курсоре. Недоступное ядро прерывает заход; беда одного чата — только его.

        Ф42-3 E: сначала все обычные дыры, потом (при `history`) дыры добора истории — тем же бюджетом. Добор
        немедленного захода «catchup» не просит: его остаток идёт сверками по графику, чтобы не молотить
        Telegram часами подряд."""

        def size(chat_ext: str, kind: bool) -> int:
            return sum(
                _CATCHUP_PAGE if g.get("one") else g["hi"] - g["lo"] for g in self._gaps_of(chat_ext, kind)
            )

        def has_history() -> bool:
            return any(g.get("h") for gaps in self._gaps.values() for g in gaps)

        had_history = has_history()
        budget = _CATCHUP_PAGES_PER_SYNC
        first = True
        core_down = False
        progressed = False
        try:
            for kind in (False, True):
                if kind and not history:
                    break
                chats = [c for c in self._gaps if self._gaps_of(c, kind)]
                for chat_ext in sorted(chats, key=lambda c: (size(c, kind), c)):
                    try:
                        while not core_down and budget > 0 and self._gaps_of(chat_ext, kind):
                            marked = int(chat_ext)
                            entity = self._entities.get(marked)
                            if entity is not None and self._not_member(chat_ext, entity):
                                await self._gap_drop(chat_ext, "владелец не участник")
                                break
                            try:
                                input_peer = await self._gateway.get_input_entity(marked)
                            except ValueError:
                                # Промах кэша сущностей сессии: дыра ждёт захода, когда чат станет известен.
                                _logger.debug("connector: догон chat=%s — нет сущности в кэше", chat_ext)
                                break
                            except _SYNC_ABORT_ERRORS:
                                raise
                            except Exception as exc:
                                raise _GapPageError from exc
                            if not first:
                                await self._sleep(random.uniform(*_CATCHUP_PAGE_JITTER_S))  # noqa: S311
                            first = False
                            budget -= 1
                            page = await self._gap_fetch(chat_ext, marked, input_peer, kind)
                            if page is None:
                                await self._gap_drop(chat_ext, "владелец не участник")
                                break
                            # Запись курсора — вне «беды одного чата»: её сбой прерывает сверку целиком.
                            core_down = not await self._gap_commit(chat_ext, *page)
                            progressed = progressed or (not core_down and not kind)
                    except _SYNC_ABORT_ERRORS:
                        raise
                    except _CATCHUP_GONE_ERRORS as exc:
                        await self._gap_drop(chat_ext, f"чат недоступен ({type(exc).__name__})")
                    except _GapChatRejectedError:
                        _logger.warning(
                            "connector: догон chat=%s — ядро не приняло ни одного сообщения страницы, "
                            "чат пропущен до следующего захода",
                            chat_ext,
                        )
                    except _GapPageError as exc:
                        # Одна плохая переписка не роняет сверку и не запирает остальные чаты.
                        self._log_dialog_error("catch-up", chat_ext, exc.__cause__ or exc)
                if not kind:
                    # Маркеры прочтения — после страниц обычных дыр (ядро двигает маркер только по известному
                    # ему сообщению) и ДО добора: FloodWait посреди добора не должен оставить чаты без них.
                    for chat_ext, d in dialogs.items():
                        self._emit_dialog_read(chat_ext, d)
        finally:
            self._warn_stale_gaps()
            if had_history and not has_history():
                _logger.info("connector: добор закончен, чатов=%d", self._catchup_hist_chats)
        # Без продвижения (все страницы захода упали) немедленный заход не просим — иначе горячий цикл.
        regular_left = any(self._gaps_of(c, False) for c in self._gaps)
        return progressed and not core_down and budget <= 0 and regular_left

    async def _gap_fetch(
        self, chat_ext: str, marked: int, input_peer: object, history: bool = False
    ) -> tuple[dict, list[int], list[tuple[int, dict]], int, int, bool] | None:
        """Одна страница самой новой дыры чата (вида `history`: обычной или добора — у добора события
        сообщений несут `history=True`): запрос истории ниже `hi` (без `min_id` — отсев `id <= lo` на
        своей стороне), эмиссия по возрастанию id. Возвращает (дыра, id страницы ниже `hi`, события страницы
        с id сообщения, сколько сообщений эмитировано, счётчик выброшенных событий до эмиссии, «добор дошёл
        до своей даты»); `None` — ответ показал, что владелец в чате не участник. Беда самого чата (кроме
        «недоступен навсегда», FloodWait и потери входа — они идут как есть) — `_GapPageError`.

        Добор не уходит глубже `_HISTORY_REFILL_SINCE_MS`: нижняя граница его дыры — известное ядру
        сообщение, а оно бывает много старше (родитель ответа, закреп, остаток стартовой загрузки) — без
        остановки по дате добор грузил бы всю историю чата до него, а не потерянное. Сообщения старше даты
        не эмитятся, дыра после такой страницы закрывается."""
        gap = max(self._gaps_of(chat_ext, history), key=lambda g: g["hi"])
        lo, hi = gap["lo"], gap["hi"]
        try:
            messages, entities = await self._gateway.get_history(
                input_peer, offset_id=hi, limit=_CATCHUP_PAGE
            )
        except (*_SYNC_ABORT_ERRORS, *_CATCHUP_GONE_ERRORS):
            raise
        except Exception as exc:
            raise _GapPageError from exc
        self._entities.update(entities)
        entity = self._entities.get(marked)
        if entity is None:
            # Сущности чата нет ни в ответе, ни у прохода диалогов — ссылку на беседу собрать не из чего.
            raise _GapPageError from LookupError("нет сущности чата")
        if self._not_member(chat_ext, entity):
            return None
        peer = utils.get_peer(marked)
        chat_ref = convert.dialog_channel_ref(
            peer, entity, me_id=self._me_id, attrs=self._last_attrs.get(chat_ext)
        )
        lost_before = self._lost_count
        page_events: list[tuple[int, dict]] = []
        emitted = 0
        reached_since = False
        for msg in sorted(messages, key=lambda m: m.id):  # источник отдаёт по убыванию id
            if not lo < msg.id < hi:
                continue
            if history:
                date = getattr(msg, "date", None)
                if date is not None and int(date.timestamp() * 1000) < _HISTORY_REFILL_SINCE_MS:
                    reached_since = True
                    continue
            events: list[dict] = []
            try:
                await self._emit_message("upsert", chat_ref, peer, msg, emit=events.append)
                json.dumps(events)
            except _SYNC_ABORT_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001 — одно негодное сообщение не должно запирать дыру
                _logger.warning(
                    "connector: догон chat=%s сообщение id=%s пропущено — не собрано (%s)",
                    chat_ext,
                    msg.id,
                    type(exc).__name__,
                )
                continue
            for event in events:
                if history and event.get("type") == "upsert":
                    event["history"] = True  # Ф42-3: тихий приём — без сигналов подпискам, медиа по клику
                self.emit(event)
                page_events.append((msg.id, event))
            emitted += 1
        return gap, [m.id for m in messages if m.id < hi], page_events, emitted, lost_before, reached_since

    async def _gap_resend(self, chat_ext: str, page_events: list[tuple[int, dict]]) -> bool:
        """Каркас выбросил что-то, пока страница шла в ядро (пачка отвергнута, буфер переполнен): события
        страницы — ядру по одному, напрямую (приём идемпотентен). Не принятое по содержимому пропускается с
        записью в журнал (без текста сообщения). `False` — ядро недоступно: заход прерывается, дыра не
        двигается. Не принято НИ ОДНО сообщение страницы из нескольких — `_GapChatRejectedError`: беда этого
        чата (например, ядро отвергает его ссылку на беседу), дыра не двигается, чат пропускается до
        следующего захода, остальные заполняются."""
        rejected: set[int] = set()
        for msg_id, event in page_events:
            if msg_id in rejected:
                continue
            try:
                await self._client.push_events([event])
            except Exception as exc:
                if _is_core_unreachable(exc):
                    return False
                if not (_is_rejected(exc) or isinstance(exc, TypeError | ValueError)):
                    raise
                rejected.add(msg_id)
        ids = {msg_id for msg_id, _event in page_events}
        if len(ids) > 1 and rejected == ids:
            raise _GapChatRejectedError
        for msg_id in sorted(rejected):
            _logger.warning(
                "connector: догон chat=%s сообщение id=%s пропущено — не принято ядром", chat_ext, msg_id
            )
        return True

    async def _gap_commit(
        self,
        chat_ext: str,
        gap: dict,
        page_ids: list[int],
        page_events: list[tuple[int, dict]],
        emitted: int,
        lost_before: int,
        reached_since: bool = False,
    ) -> bool:
        """Страница отдана — дождаться её в ядре, ПОТОМ записать курсор с уменьшенным `hi` (или без закрытой
        дыры), и только потом сузить дыру в памяти. `False` — ядро страницу не приняло (недоступно): заход
        прекращается, дыра остаётся как была (страница будет запрошена заново — приём идемпотентен).
        `reached_since` — страница добора дошла до его даты: дыра закрывается."""
        state = await self._catchup_delivered(lost_before)
        if state == "lost" and await self._gap_resend(chat_ext, page_events):
            state = "ok"
        if state != "ok":
            _logger.warning("connector: догон chat=%s — страница не дошла до ядра, заход прерван", chat_ext)
            return False
        pages, total = gap["p"] + 1, gap["m"] + emitted
        # Закрыта: страница пуста или дошла до `lo`. «Короче 100» концом НЕ считается: сервер бывает отдаёт
        # меньше запрошенного при непустом продолжении (см. `telethon/client/messages.py::_MessagesIter`).
        closed = reached_since or not page_ids or min(page_ids) <= gap["lo"] + 1 or bool(gap.get("one"))
        rest = [g for g in self._gaps.get(chat_ext, []) if g is not gap]
        if not closed:
            rest.append({**gap, "hi": min(page_ids), "p": pages, "m": total})
        new_gaps = {c: g for c, g in self._gaps.items() if c != chat_ext}
        if rest:
            new_gaps[chat_ext] = rest
        await self._catchup_save(new_gaps)
        self._gaps = new_gaps
        if closed and gap.get("h"):
            _logger.info("connector: добор chat=%s страниц=%d сообщений=%d", chat_ext, pages, total)
        elif closed:
            # Без текстов сообщений. Пустые закрытия (нечего было догонять) — в отладку, чтобы не шуметь.
            log = _logger.info if total else _logger.debug
            log("connector: догон chat=%s страниц=%d сообщений=%d", chat_ext, pages, total)
        return True

    # --- добор потерянного с 23.09 (Ф42-3) ------------------------------------------------------------------

    async def _history_refill_enabled(self) -> bool:
        """Выключатель аккаунта `history_refill` (JSON `false` — выключено; нет ключа — включено), читается
        из ядра на каждой сверке — перезапуск не нужен. Настройку прочесть не удалось — в этом заходе добора
        нет (засев и заполнение подождут)."""
        try:
            account = await self._client.get_account()
        except Exception as exc:  # noqa: BLE001 — см. докстринг
            _logger.debug("connector: настройка добора не прочитана (%s)", type(exc).__name__)
            return False
        config = account.get("config") if isinstance(account, dict) else None
        return not (isinstance(config, dict) and config.get("history_refill") is False)

    async def _history_seed(self) -> None:
        """Засев добора — один раз на значение `_HISTORY_REFILL_SINCE_MS` (раздел D): границы от ядра
        (`heads` с `since_ts`), дыра `(lo, hi)` на каждый чат проекта — не архивный, где владелец участник; у
        форума — наименьший `lo` и наибольший `hi` по темам, хоть одна тема в проекте. Дыры и отметка —
        ОДНОЙ записью курсора."""
        if self._catchup_hist == _HISTORY_REFILL_SINCE_MS:
            return
        heads = await self._client.channels_heads(since_ts=_HISTORY_REFILL_SINCE_MS)
        chats: dict[str, dict] = {}
        for head in heads:
            is_topic = head["kind"] == "topic"
            chat_ext = head["parent_ext_id"] if is_topic else head["ext_id"]
            if chat_ext is None or head.get("skip"):
                continue
            chat = chats.setdefault(chat_ext, {"lo": None, "hi": None, "project": False, "archived": False})
            if head.get("archived"):
                if not is_topic:
                    chat["archived"] = True
            elif head.get("project_id") is not None:
                chat["project"] = True
            lo, hi = head.get("refill_lo_ext_sort"), head.get("max_ext_sort")
            if lo is not None:
                chat["lo"] = lo if chat["lo"] is None else min(chat["lo"], lo)
            if hi is not None:
                chat["hi"] = hi if chat["hi"] is None else max(chat["hi"], hi)
        seeded = 0
        gaps_before = {c: [dict(g) for g in gaps] for c, gaps in self._gaps.items()}
        for chat_ext, chat in chats.items():
            lo, hi = chat["lo"], chat["hi"]
            if not chat["project"] or chat["archived"] or lo is None or hi is None or hi - lo <= 1:
                continue
            try:
                marked = int(chat_ext)
            except ValueError:
                continue
            entity = self._entities.get(marked)
            if entity is not None and self._not_member(chat_ext, entity):
                continue
            self._gap_note(chat_ext, lo, hi, history=True)
            seeded += 1
        before = (self._catchup_hist, self._catchup_hist_chats)
        self._catchup_hist, self._catchup_hist_chats = _HISTORY_REFILL_SINCE_MS, seeded
        try:
            await self._catchup_save()
        except BaseException:
            # Отметки в курсоре нет — засев повторится; дыры засева из памяти тоже убираем (одна запись).
            self._catchup_hist, self._catchup_hist_chats = before
            self._gaps = gaps_before
            raise
        _logger.info("connector: добор засеян, чатов=%d", seeded)

    async def _run_initial_load(
        self,
        pending: list[tuple],
        *,
        has_more: bool = False,
        failed: list[str] | None = None,
        read_newest: bool = False,
    ) -> None:
        """`pending` — уже ОБРЕЗАННАЯ вызывающим пачка (≤ `_INITIAL_LOAD_BATCH_MAX`, находка 4 приёмки куска
        1b). `has_more=True` — остаток есть, activity(backfill=True) остаётся включённым (не мигает между
        соседними заходами `sync("initial")`; вызывающий уже поставил `_request_sync("initial")`)."""
        if not pending:
            return
        with contextlib.suppress(Exception):
            await self._client.set_activity(backfill=True)
        try:
            first = True
            for peer, entity, chat_ext, read_inbox_max_id, read_outbox_max_id in pending:
                if not first:
                    await self._sleep(random.uniform(*_INITIAL_LOAD_JITTER_S))  # noqa: S311 — джиттер
                first = False
                try:
                    messages, entities = await self._gateway.get_messages(
                        peer, min_id=None, limit=_INITIAL_LOAD_LIMIT
                    )
                    self._entities.update(entities)
                    self._entities[int(chat_ext)] = entity
                    chat_ref = convert.dialog_channel_ref(
                        peer, entity, me_id=self._me_id, attrs=self._last_attrs.get(chat_ext)
                    )
                    for msg in reversed(messages):
                        await self._emit_message("upsert", chat_ref, peer, msg)
                    if not read_inbox_max_id and read_newest and messages:
                        read_inbox_max_id = max(m.id for m in messages)
                    if read_inbox_max_id:
                        # Кусок 7b, К1: маркер прочтения — сразу после сообщений чата (как в сверке известных
                        # чатов): история приходит прочитанной, и дежурный агент её не разбирает.
                        self.emit(
                            {
                                "type": "read",
                                "channel": {"ext_id": chat_ext, "kind": convert.dialog_kind(entity)},
                                "up_to_ext_id": str(read_inbox_max_id),
                            }
                        )
                    self._emit_peer_read(chat_ext, entity, read_outbox_max_id, force=False)
                    self._note_new_channel(chat_ext)  # новый канал -> папки «грязные» (находка 3 приёмки 1b)
                except _SYNC_ABORT_ERRORS:
                    raise
                except Exception as exc:  # noqa: BLE001 — находка F6/F11: один плохой чат не ронял бы всех
                    self._log_dialog_error("стартовая загрузка", chat_ext, exc)
                    if failed is not None:
                        failed.append(chat_ext)
        finally:
            if not has_more:
                with contextlib.suppress(Exception):
                    await self._client.set_activity(backfill=False)

    async def _reread_window(self, reason: str, known_chats: set[str], seen: list) -> None:
        """Раздел «Сверка», п.3: правки/удаления, пропущенные во сне. Форумы (топики) вне окна — ПРИНЯТО
        КАК ДОЛГ ФАЗЫ 1 архитектором на приёмке куска 1b (см. также «per-topic read» ниже): общая нумерация
        id форума и тем усложняет корректный дифф на удаление по каждой отдельной теме; не делаем."""
        active_ms = _WINDOW_ACTIVE_RESUME_MS if reason == "resume" else _WINDOW_ACTIVE_MS
        max_chats = _WINDOW_MAX_CHATS_RESUME if reason == "resume" else _WINDOW_MAX_CHATS
        now_ms = int(self._wall() * 1000)
        done = 0

        for d in seen:
            if done >= max_chats:
                break
            chat_ext = convert.peer_ext_id(d.peer)
            if chat_ext not in known_chats:
                continue
            if isinstance(d.entity, types.Channel) and getattr(d.entity, "forum", False):
                continue
            if d.last_message_date is None:
                continue
            last_ms = int(d.last_message_date.timestamp() * 1000)
            if now_ms - last_ms > active_ms:
                continue
            done += 1

            try:
                messages, entities = await self._gateway.get_messages(
                    d.peer, min_id=None, limit=_WINDOW_LIMIT
                )
                self._entities.update(entities)
                chat_ref = convert.dialog_channel_ref(
                    d.peer, d.entity, me_id=self._me_id, attrs=self._last_attrs.get(chat_ext)
                )
                fetched_ext_ids: set[str] = set()
                fetched_ids: list[int] = []
                for msg in reversed(messages):
                    fetched_ext_ids.add(str(msg.id))
                    fetched_ids.append(msg.id)
                    await self._emit_message("upsert", chat_ref, d.peer, msg)

                core_ids = await self._client.channels_window(ext_id=chat_ext, limit=_WINDOW_LIMIT)
                missing = _missing_in_range(core_ids, fetched_ext_ids, fetched_ids, limit=_WINDOW_LIMIT)
                if missing:
                    self.emit(
                        {
                            "type": "delete",
                            "channel": {"ext_id": chat_ext, "kind": chat_ref["kind"]},
                            "ext_ids": missing,
                        }
                    )
            except _SYNC_ABORT_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001 — находка F6/F11: один плохой чат не ронял бы всё окно
                self._log_dialog_error("окно", chat_ext, exc)

    # --- папки ------------------------------------------------------------------------------------------

    def _is_muted(self, mute_until: object | None) -> bool:
        if mute_until is None:
            return False
        now_ms = int(self._wall() * 1000)
        return int(mute_until.timestamp() * 1000) > now_ms

    def _note_new_channel(self, chat_ext: str) -> None:
        """Новый канал (стартовая загрузка/живое сообщение из ранее неизвестного чата) -> папки «грязные»
        (находки 2 и 3 приёмки куска 1b): без этого `tg_folders` у него не появится НИКОГДА, если он не
        попал в диапазон папочного пересчёта до следующего апдейта фильтра/часа."""
        if chat_ext not in self._last_attrs:
            self._folders_dirty = True

    async def _maybe_recompute_folders(self, reason: str, known_chats: set[str]) -> None:
        """Находка 3 приёмки куска 1b: раньше раскладка папок использовала диалоги, увиденные ЧАСТИЧНЫМ
        проходом основной сверки (стоп после 20 тихих) — у диалогов ПОСЛЕ порога `tg_folders` не появлялся
        никогда. Пересчёт папок теперь делает СВОЙ ПОЛНЫЙ проход `iter_dialogs()`, без правила остановки
        (раздел «Папки -> attrs», решение 11.5: «Пересчёт — полный проход диалогов»)."""
        now = self._mono()
        need_refetch = (
            reason == "start"
            or self._folders_cache is None
            or self._folders_changed
            or (now - self._folders_last_refresh) >= _FOLDER_REFRESH_INTERVAL_S
        )
        if need_refetch:
            self._folders_cache = await self._gateway.get_dialog_filters()
            self._folders_last_refresh = now
            self._folders_changed = False
            self._folders_dirty = True
            self.emit(
                {
                    "type": "account_meta",
                    "key": "tg_folders",
                    "value": folders_mod.folders_account_meta(self._folders_cache),
                }
            )

        if not self._folders_dirty or self._folders_cache is None:
            return

        dialogs = [d async for d in self._gateway.iter_dialogs()]
        for d in dialogs:
            self._entities[int(convert.peer_ext_id(d.peer))] = d.entity

        dialog_flags = [self._dialog_flags(d) for d in dialogs]
        folder_map = folders_mod.compute_folders(dialog_flags, self._folders_cache, me_marked_id=self._me_id)

        for d in dialogs:
            chat_ext = convert.peer_ext_id(d.peer)
            if chat_ext not in known_chats:
                continue
            marked_id = int(chat_ext)
            flags = self._dialog_flags(d)
            attrs = folders_mod.channel_attrs(
                flags, tg_type=_tg_type(d.entity), folders=folder_map.get(marked_id, [])
            )
            self._last_attrs[chat_ext] = attrs
            self.emit(
                {
                    "type": "channel",
                    "channel": {"ext_id": chat_ext, "kind": convert.dialog_kind(d.entity), "attrs": attrs},
                }
            )

        # Мелочь из отчёта исполнителя (round 2): `_folders_dirty = False` — ТОЛЬКО после успешного прохода
        # (раньше стояло до `iter_dialogs()`/цикла ниже — обрыв на середине из-за сетевой ошибки погасил бы
        # «грязный» флаг, и следующая сверка НЕ повторила бы пересчёт, хотя attrs часть каналов так и не
        # обновились).
        self._folders_dirty = False

    def _dialog_flags(self, d: object) -> folders_mod.DialogFlags:
        marked_id = int(convert.peer_ext_id(d.peer))
        dtype = folders_mod.dialog_type(d.entity)
        contact = marked_id == self._me_id or bool(getattr(d.entity, "contact", False))
        return folders_mod.DialogFlags(
            marked_id=marked_id,
            dtype=dtype,
            contact=contact,
            muted=self._is_muted(d.mute_until),
            archived=d.archived,
        )
