"""Модели входящих событий коннекторов. Раздел «События» docs/architecture.md.

Дискриминатор `type`. Валидация — на входе в `ingest.apply_batch`: невалидное событие не роняет пачку,
становится записью в `dead_letter` (см. ingest.py).
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

# Ф23-3: потолок счётчиков просмотров/пересылок — как у целых вложений (Ф8-5): JSON-числа до 2^53 точны везде.
_MAX_COUNTER = 2**53


class ChannelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ext_id: str
    kind: Literal["dm", "group", "channel", "topic", "mail_thread", "issue", "card", "task"]
    title: str | None = None
    parent_ext_id: str | None = None
    own_id_space: bool = False
    # Атрибуты канала у источника (Telegram: tg_folders/tg_type/contact/…), по которым работает раскладка
    # (`core/mapping.py`). `None` = «не трогать» — событие без мнения об атрибутах канала (раздел A.1 спеки
    # куска 1a). Ядро смысл ключей не знает, только сравнивает и передаёт в правила.
    attrs: dict | None = None
    # Раздел «Идентификаторы и маппинг в события» спеки куска 1b: родитель должен существовать раньше
    # ребёнка (правило 1a A.2 — каналы без сообщений не создаются, форум без своих сообщений не появился бы
    # сам, и топик повис бы без `parent_channel_id`). Если родителя с `parent_ext_id` в ядре ещё нет, ядро
    # создаёт его из ЭТОЙ вложенной ссылки (со своими `attrs`) в той же точке сохранения (`ingest.py`).
    # `parent.ext_id` ОБЯЗАН совпадать с `parent_ext_id`, иначе событие ядовитое (в dead_letter).
    parent: ChannelRef | None = None


class SenderRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ext_id: str
    name: str | None = None
    handle: str | None = None
    is_me: bool = False
    # Ф20-1 A1/П4/П5: отправитель — бот (Slack `bot_message`/Slackbot/бот справочника, Telegram `User.bot` и
    # 777000). Коннектор кладёт ключ только когда истинно. Ядро пишет `message.is_bot` при вставке.
    is_bot: bool = False


class ThreadInfo(BaseModel):
    """Сводка треда со стороны ИСТОЧНИКА, приходит НА РОДИТЕЛЕ (спека 2b, раздел B). Slack отдаёт
    `subscribed`/`last_read` родителя для user-токена в `conversations.replies` (проверит коннектор 2c);
    если отдаёт — это главное (раздел «Модель тредов»). `reply_count == 0` без существующей строки треда —
    строку не создавать (иначе у КАЖДОГО сообщения был бы фиктивный `thread: {0}`).

    Ф40-1 B: `last_read_ts` — позиция прочтения треда ВРЕМЕНЕМ (Mattermost, `last_viewed_at` треда, мс) вместо
    `last_read_ext_id`; заданы оба — событие ядовитое."""

    model_config = ConfigDict(extra="forbid")

    reply_count: int = Field(ge=0)
    latest_reply_ext_id: str | None = None
    subscribed: bool | None = None
    last_read_ext_id: str | None = None
    last_read_ts: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_read_position(self) -> ThreadInfo:
        if self.last_read_ext_id is not None and self.last_read_ts is not None:
            raise ValueError("last_read_ext_id и last_read_ts заданы вместе")
        return self


class _MessageEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: ChannelRef
    ext_id: str
    ts: int
    sender: SenderRef | None = None
    is_me: bool
    kind: Literal["text", "event", "file"]
    text: str | None = None
    entities: list | dict | None = None
    attachments: list | dict | None = None
    forward: dict | None = None
    # Ф11-8: адресаты (To+Cc) СВОЕГО письма — только коннектор почты. Коннектор фильтрует сам (адреса с `@`,
    # ≤ 254, без повторов, первые 20), ядро лишь страхует: отказ здесь — отказ ВСЕГО события (`dead_letter`).
    # Пустой список ядро хранит как `NULL`.
    mail_to: list[Annotated[str, StringConstraints(min_length=3, max_length=320)]] | None = Field(
        default=None, max_length=20
    )
    mentions_me: bool
    # Ф8-3 П11: упоминание меня — ТОЛЬКО широковещательное (`@channel`/`@here`/`@everyone` Slack). В БД
    # `mentions_me` остаётся 1, но тред таким упоминанием не подписывается (`threads.touch()`).
    mentions_broadcast_only: bool = False
    # Ф28-2: упоминание меня — ТОЛЬКО через мою группу пользователей Slack (`<!subteam^…>`). В БД
    # `mentions_me` = 1 и `mentions_group` = 1: сам тег — упоминание для меня, но тред им не подписывается.
    mentions_group_only: bool = False
    # Ф57-1 П1: письмо-рассылка (только коннектор почты, ключ кладётся только когда истинно). Ядро пишет
    # `message.bulk` при вставке; читает его только расчёт внимания (`attention.message_attention`).
    bulk: bool = False
    # Ф63-1 П3: тема письма (только коннектор почты) — нормализованная (без `Re:`/`Fwd:`), до 300 символов.
    # Читают только правила входящих проекта (`core/inbound_rules.py`, условие `subject`); в БД не пишется —
    # заголовок беседы по-прежнему задаёт `channel.title`.
    subject: Annotated[str, StringConstraints(max_length=300)] | None = None
    refs: list | dict | None = None
    thread_ext_id: str | None = None
    # Ответ, показанный и в основной ленте канала (Slack `thread_broadcast`) — раздел «Модель тредов».
    thread_broadcast: bool = False
    # Сводка треда от источника, только на родителе; `None` = «не трогать» (как `attrs` канала).
    thread: ThreadInfo | None = None
    reply_to_ext_id: str | None = None
    grouped_id: str | None = None
    edited_ts: int | None = None
    send_token: str | None = None  # склейка эха со своей заготовкой outbox (см. outbox.match_echo)
    # Ф8-4 П7: заглушка удалённого у источника сообщения (tombstone Slack) — существующую строку не
    # перезаписывает (исходный текст остаётся виден с пометкой «удалено»); строки нет — вставка как обычно.
    tombstone: bool = False
    # Ф23-3: просмотры и пересылки поста канала Telegram (`channel.kind == 'channel'`) на момент приёма. Нет
    # полей — `views_json` сообщения не трогается (правка без счётчика его не стирает).
    views: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    forwards: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    # Ф42-3: добор истории (сообщение потеряно раньше и перечитано из источника). Ставит только заполнитель
    # дыр добора Telegram. Приём тихий: подписки не сигналят (ни новая строка, ни правка известной), новые
    # вложения — по клику; всё остальное — как у обычного события.
    history: bool = False
    # Ф44-1a: инлайн-кнопки под сообщением (только коннектор Telegram) и отметка времени их снимка — сырой
    # `edit_date` сообщения (и при `edit_hide`), иначе `date`, мс. Нет `buttons_ts` (другие источники, старые
    # коннекторы) — кнопки строки не трогаются; `buttons: null` с отметкой — «кнопок больше нет». Форму и
    # пределы здесь НЕ проверяем (П10): не та форма или сверх пределов — событие принимается, кнопки
    # обнуляются с `cut` (`core/buttons.normalize`).
    buttons: dict | None = None
    buttons_ts: int | None = Field(default=None, gt=0)


class UpsertEvent(_MessageEventBase):
    type: Literal["upsert"] = "upsert"


class EditEvent(_MessageEventBase):
    """Тот же набор полей и семантика, что у upsert (см. спеку) — отдельный тип ради контракта коннекторов."""

    type: Literal["edit"] = "edit"


class DeleteEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["delete"] = "delete"
    ext_ids: list[str]
    channel: ChannelRef | None = None


class ReactionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["reaction"] = "reaction"
    channel: ChannelRef
    ext_id: str
    reactions: list | dict


class ViewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ext_id: str = Field(min_length=1, max_length=64)
    views: int = Field(ge=0, le=_MAX_COUNTER)
    forwards: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)


class ViewsEvent(BaseModel):
    """Ф23-3: свежие просмотры/пересылки постов канала Telegram (часовой проход коннектора). Не сообщение и не
    активность: ядро пишет только `message.views_json` (и `seq`, если цифры изменились) — непрочитанное,
    `last_msg_ts`, карточки и сигналы подписок не трогает. Неизвестная беседа или сообщение — no-op."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["views"] = "views"
    channel: ChannelRef
    ts: int  # мс снятия
    items: list[ViewsItem] = Field(max_length=100)


class MailReactionEvent(BaseModel):
    """Ф15-1 A4: реакция в письме (Gmail `text/vnd.google.email-reaction+json` или RFC 9078) — не сообщение, а
    значок на письме-цели `target_ext_id` (Message-ID) той же беседы. `src_ext_id` — Message-ID самого
    письма-реакции (повтор -> no-op). Беседу не создаёт (нет беседы -> no-op)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["mail_reaction"] = "mail_reaction"
    channel: ChannelRef
    target_ext_id: str = Field(min_length=1, max_length=998)
    src_ext_id: str = Field(min_length=1, max_length=998)
    emoji: str = Field(min_length=1, max_length=64)
    sender: SenderRef
    ts: int


class ReadEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["read"] = "read"
    channel: ChannelRef
    # Ф40-1 B: позиция прочтения — сообщением (`up_to_ext_id`) ЛИБО временем (`up_to_ts`, мс: Mattermost,
    # `last_viewed_at`). Задано ровно одно из двух — иначе событие ядовитое (`dead_letter`).
    up_to_ext_id: str | None = None
    up_to_ts: int | None = Field(default=None, gt=0)
    # Прочтение ТРЕДА (маркер треда, не канала) — раздел B спеки 2b. `None` — как раньше, канальная read.
    thread_ext_id: str | None = None

    @model_validator(mode="after")
    def _one_position(self) -> ReadEvent:
        if (self.up_to_ext_id is None) == (self.up_to_ts is None):
            raise ValueError("нужно ровно одно из up_to_ext_id и up_to_ts")
        return self


class PeerReadEvent(BaseModel):
    """Ф11-7: собеседник прочитал МОИ сообщения беседы до `up_to_ext_id` включительно (Telegram
    `read_outbox_max_id`). Ядро хранит числовую позицию `channel.peer_read_sort` и двигает её только вперёд;
    в группе это «прочитал кто-то». Неизвестный канал -> no-op (как у `read`)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["peer_read"] = "peer_read"
    channel: ChannelRef
    up_to_ext_id: str


class ChannelEvent(BaseModel):
    """Обновить заголовок/атрибуты СУЩЕСТВУЮЩЕГО канала без сообщения. Неизвестный канал -> no-op
    (раздел A.2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["channel"] = "channel"
    channel: ChannelRef


class ChannelMigrateEvent(BaseModel):
    """Миграция группы в супергруппу и т.п. (раздел A.3): `old_ext_id` -> новый `channel` (может уже
    существовать)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["channel_migrate"] = "channel_migrate"
    old_ext_id: str
    channel: ChannelRef


class AccountMetaEvent(BaseModel):
    """Состояние аккаунта у источника — список папок Telegram и т.п. (раздел A.4). `key` <= 64 символов,
    `value` — JSON <= 64 КиБ (проверяется в ingest.py, не здесь: размер зависит от сериализации)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["account_meta"] = "account_meta"
    key: str
    value: Any = None


Event = Annotated[
    UpsertEvent
    | EditEvent
    | DeleteEvent
    | ReactionEvent
    | ViewsEvent
    | MailReactionEvent
    | ReadEvent
    | PeerReadEvent
    | ChannelEvent
    | ChannelMigrateEvent
    | AccountMetaEvent,
    Field(discriminator="type"),
]

_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


def parse_event(raw: dict) -> Event:
    """Разобрать одно событие пачки. Исключение pydantic — «ядовитое событие» для вызывающего кода."""
    return _event_adapter.validate_python(raw)


class CursorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    ts: int | None = None
