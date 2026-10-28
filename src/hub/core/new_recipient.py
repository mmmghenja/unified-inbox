"""`POST /api/v1/drafts/new` — черновик ПЕРВОГО сообщения новому адресату (Ф12-1, A1 + П1, П7): разбор и
проверка адресата, беседа-заготовка и черновик одной транзакцией. Беседа-заготовка и её судьба —
`draft_channels.py`; сам черновик — обычный `outbox.enqueue(origin='api')` (`pending_approval`).

Адресат проверяется только по форме: ядро коннектору не звонит (решение 2) — нет такого @username или
человек закрыл личку — черновик станет `failed` с причиной при отправке.

Ф12-5 A1 (`docs/architecture.md`): `POST /api/v1/channels/new` — пустая беседа-заготовка
для самого владельца (кнопка «Новое сообщение»), без строки outbox; проверки адресата, уникальность, лимит и
вставка беседы — общий код с `create_draft` (`_recipient`, `_open_drafts`, `_insert_channel`).

Ф14-2 A1/A2 (`docs/architecture.md`): у `channels/new` адресат — строка ИЛИ объект
`{"kind": "tg_user", …}` (Telegram по id пользователя) / `{"kind": "slack_users", "users": […]}` (личка или
групповая личка Slack); проект необязателен (`None` — «по правилам»: заготовка `map_source='auto'` и сразу
пересчёт правилами). Сессиям Claude (`drafts/new`) — как было: строка, проект, без Slack.

Вызывать только внутри `Db.write()`; время — параметром `now_ms`.
"""

import json
import re
from typing import NamedTuple

from .. import msgid
from . import draft_channels, mapping
from . import outbox as outbox_mod
from .db import db_of, next_seq

# Ф14-2 A2: `slack` — только для `slack_users` из UI (`create_channel`); сессиям (`create_draft`) — как было.
# Ф40-1b: `mattermost` — так же, только `mm_users` из UI.
# Ф62-1: `whatsapp` — по номеру телефона (вид адресата `tg_phone` — общий «номер телефона»), и сессиям, и UI.
SOURCES = frozenset({"telegram", "mail", "slack", "mattermost", "whatsapp"})
API_SOURCES = frozenset({"telegram", "mail", "whatsapp"})
MAX_OPEN_DRAFTS = 100
TEXT_MAX = 4000  # как у hub_draft
MAIL_TEXT_MAX = 100_000  # лимит ящика — `SEND_MAX_TEXT_LEN` коннектора почты
SUBJECT_MAX = 200
EMAIL_MAX = 254
NAME_MAX = 200
TG_USER_ID_MAX = 2**53
SLACK_USERS_MAX = 8
MM_USERS_MAX = 7  # сервер Mattermost: групповая — до 8 человек вместе со мной
# Ф34-3 (`docs/architecture.md`б): сколько заготовок «по номеру телефона» за 24 ч на аккаунт
# принимается от bearer (сессий); настройка аккаунта `tg_phone_daily` (0…20). Настоящие вызовы Telegram
# считает коннектор — своим счётчиком с тем же потолком.
TG_PHONE_DAILY_DEFAULT = 5
TG_PHONE_DAILY_MAX = 20
TG_PHONE_WINDOW_MS = 24 * 60 * 60 * 1000
# Ф46-1 (`docs/architecture.md`): сколько заготовок «личка автору сообщения из
# группы» за 24 ч на аккаунт принимается от bearer; настройка аккаунта `tg_author_daily` (0…30).
TG_AUTHOR_DAILY_DEFAULT = 10
TG_AUTHOR_DAILY_MAX = 30
MSG_AUTHOR_KIND = draft_channels.MSG_AUTHOR_KIND
MSG_NOT_FOUND_TEXT = "to: сообщение не найдено"

_SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]{2,20}$")
_MM_USER_RE = re.compile(r"^[a-z0-9]{26}$")
_PHONE_SEPARATORS_RE = re.compile(r"[\s()\-\u2010-\u2015]")
_PHONE_RE = re.compile(r"^\+[0-9]{8,15}$")
PHONE_OFF_TEXT = "to: первое сообщение по номеру телефона у этого аккаунта выключено (tg_phone_daily=0)"
WA_PHONE_OFF_TEXT = "to: первое сообщение по номеру телефона у этого аккаунта выключено (wa_phone_daily=0)"
_PHONE_TEXT = "to: номер телефона — в международном формате: «+» и 8–15 цифр (например +7 900 000-00-00)"

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TME_RE = re.compile(r"^(?:https://)?t\.me/(?P<name>[^/?#\s]+)$", re.IGNORECASE)
_EMAIL_RE = re.compile(
    r"^(?P<local>[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+)@(?P<domain>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)$"
)


class DraftNewError(Exception):
    """Базовая ошибка; текст — для человека/агента, без адресата."""


class Invalid(DraftNewError):
    """422: аккаунт, проект, адресат, тема, `from` или текст не годятся."""


class DraftExists(DraftNewError):
    """409: открытая беседа-заготовка этому адресату в этом проекте уже есть (П1). Ф12-5 П4: заготовка без
    строки outbox (пустая, заведённая владельцем) — текст без «#None», черновик кладётся в неё `hub_draft`."""

    def __init__(self, outbox_id: int | None, channel_id: int) -> None:
        if outbox_id is None:
            text = f"беседа #{channel_id} этому адресату уже заведена — черновик кладите в неё (`hub_draft`)"
        else:
            text = f"этому адресату уже есть черновик #{outbox_id} (беседа #{channel_id})"
        super().__init__(text)
        self.outbox_id = outbox_id
        self.channel_id = channel_id


class DraftElsewhere(DraftNewError):
    """409 (ревью Ф34-3): открытая заготовка этому номеру есть в проекте, который токену не виден — без
    номеров строк и бесед."""

    def __init__(self) -> None:
        super().__init__("этому адресату уже готовится сообщение в другом проекте")


class DmExists(DraftNewError):
    """409 `dm_exists` (Ф46-1 П2): с автором сообщения уже есть настоящая личка — сессии черновик в неё этим
    путём не кладётся. Номер беседы — только если она видна токену (`channel_id`, иначе `None`)."""

    def __init__(self, channel_id: int | None) -> None:
        if channel_id is None:
            text = "с этим человеком уже есть беседа в другом проекте — скажи владельцу"
        else:
            text = (
                f"с этим человеком уже есть беседа #{channel_id} — пиши "
                f"`hub_draft(channel_id={channel_id}, …)`"
            )
        super().__init__(text)
        self.channel_id = channel_id


class BotDmExists(DmExists):
    """409 (Ф63-2 П7): с этим ботом (@ник на «bot») у аккаунта уже есть личка — сессия пишет в неё
    `hub_draft`, заготовка не заводится (иначе склейка при отправке молча оставила бы беседу в чужом проекте
    или «Неразобранном»). Номер беседы и проект — только если беседа видна токену."""

    def __init__(self, username: str, channel_id: int | None, project: str | None) -> None:
        if channel_id is None:
            text = f"беседа с @{username} уже есть в другом проекте — скажи владельцу"
        else:
            text = (
                f"беседа с @{username} уже есть: #{channel_id} (проект {project}) — пиши "
                f"`hub_draft(channel_id={channel_id}, …)` в неё"
            )
        DraftNewError.__init__(self, text)
        self.channel_id = channel_id


class TooMany(DraftNewError):
    """429: у аккаунта больше `MAX_OPEN_DRAFTS` открытых бесед-заготовок."""


class IdempotencyConflict(DraftNewError):
    """409: ключ идемпотентности уже занят другим запросом."""


class Recipient(NamedTuple):
    """Разобранный адресат беседы-заготовки (общий шаг `create_draft` и `create_channel`)."""

    source: str
    schema: str
    new_to: dict
    title: str
    kind: str  # вид беседы: `dm` | `mail_thread` | `group` (групповая личка Slack)
    own_id_space: bool
    # Ф46-1: поля `new_draft` рядом с `to` (`from_title`, `from_channel_id`, `from_message_id`) — только у
    # вида `tg_msg_author`.
    extra: dict | None = None


class ChannelCreated(NamedTuple):
    """Ф12-5 A1: беседа-заготовка UI; `created=False` — отдана существующая открытая (решение 5)."""

    channel_id: int
    created: bool
    kind: str  # вид адресата для аудита: `tg_username` | `email` | `tg_user` | `slack_users`


class Created(NamedTuple):
    channel_id: int
    outbox_id: int
    message_id: int
    status: str
    existing: bool
    created: bool
    kind: str  # вид адресата для аудита: `tg_username` | `email`


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


def parse_tg_username(to: str) -> str:
    """`@name`, `name`, `t.me/name`, `https://t.me/name` -> `name` как ввёл (сравнение — без регистра)."""
    value = (to or "").strip()
    match = _TME_RE.match(value)
    if match is not None:
        value = match.group("name")
    elif value.startswith("@"):
        value = value[1:]
    if not _USERNAME_RE.match(value):
        raise Invalid("to: нужен @username Telegram (латиница, цифры, _, 4–32 символа, с буквы)")
    return value


def looks_like_phone(to: object) -> bool:
    """Ф34-3: строка «Кому» аккаунта Telegram — номер, а не @username (тот начинается с буквы)."""
    head = to.strip()[:1] if isinstance(to, str) else ""
    return head in ("+", "(") or head.isdigit()


def parse_tg_phone(to: str) -> str:
    """Ф34-3, решение 1: номер -> `+` и 8–15 цифр. Пробелы, скобки и дефисы убираются; российское
    `8XXXXXXXXXX` -> `+7…`; иначе 422. Текст ошибки — без самого номера (ПДн)."""
    value = _PHONE_SEPARATORS_RE.sub("", to or "")
    if len(value) == 11 and value.startswith("8") and value.isascii() and value.isdigit():
        value = "+7" + value[1:]
    if _PHONE_RE.match(value) is None:
        raise Invalid(_PHONE_TEXT)
    return value


def mask_phone(phone: str) -> str:
    """Ф34-3, решение 4: номер в названии заготовки и везде, кроме UI владельца, — `+7 ••• ••• 12 34` (первая
    цифра кода страны и четыре последние)."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:  # ревью Ф34-3: у короткого номера четыре последние цифры — уже половина
        return f"+{digits[:1]} ••• ••• {digits[-2:]}"
    return f"+{digits[:1]} ••• ••• {digits[-4:-2]} {digits[-2:]}"


def tg_phone_daily(config_json: str | None) -> int:
    """Ф34-3 П3: настройка аккаунта `tg_phone_daily` (`hub account set N tg_phone_daily=…`): целое 0…20,
    больше — 20, кривое или нет ключа — 5."""
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    value = config.get("tg_phone_daily") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return TG_PHONE_DAILY_DEFAULT
    return min(value, TG_PHONE_DAILY_MAX)


def phone_daily(source: str | None, config_json: str | None) -> int:
    """Потолок заготовок «по номеру телефона» аккаунта: Telegram — `tg_phone_daily`, WhatsApp (Ф62-1) —
    `wa_phone_daily` (те же 0…20, умолчание 5)."""
    if source != "whatsapp":
        return tg_phone_daily(config_json)
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    value = config.get("wa_phone_daily") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return TG_PHONE_DAILY_DEFAULT
    return min(value, TG_PHONE_DAILY_MAX)


def tg_author_daily(config_json: str | None) -> int:
    """Ф46-1, раздел 3: настройка аккаунта `tg_author_daily` (`hub account set N tg_author_daily=…`): целое
    0…30, больше — 30, кривое или нет ключа — 10. 0 — сессиям этот способ закрыт (клик владельца не ограничен)."""
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    value = config.get("tg_author_daily") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return TG_AUTHOR_DAILY_DEFAULT
    return min(value, TG_AUTHOR_DAILY_MAX)


def _check_author_limit(conn, schema: str, account_id: int, now_ms: int) -> None:
    """Ф46-1, раздел 3: заготовок вида `tg_msg_author` от bearer за 24 ч на аккаунт — не больше
    `tg_author_daily` (считаются и отменённые, и неудачные, и привязанные: `new_draft.to` переживает
    привязку). Заготовки владельца из UI не считаются и не ограничены."""
    row = conn.execute("SELECT config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
    daily = tg_author_daily(row[0] if row is not None else None)
    (count,) = conn.execute(
        _sql(
            schema,
            "SELECT COUNT(*) FROM {schema}.channel WHERE account_id = ? "
            "AND first_seen_ts > ? AND json_extract(meta_json, '$.new_draft.to.kind') = ? "
            "AND COALESCE(json_extract(meta_json, '$.new_draft.origin'), '') <> ?",
        ),
        (account_id, now_ms - TG_PHONE_WINDOW_MS, MSG_AUTHOR_KIND, draft_channels.UI_ORIGIN),
    ).fetchone()
    if count < daily:
        return
    raise TooMany(
        f"предел первых сообщений авторам из групп на сегодня ({daily}) — остальное завтра или попроси "
        "владельца поднять"
    )


def _wait_text(ms: int) -> str:
    minutes = max(1, -(-ms // 60_000))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def _check_phone_limit(conn, schema: str, account_id: int, now_ms: int) -> None:
    """Ф34-3 П3б: защита от цикла «создал — отменил» — заготовок `tg_phone` от bearer за 24 ч на аккаунт не
    больше `tg_phone_daily` (считаются и отменённые, и неудачные, и привязанные: вид адресата в
    `new_draft.to.kind` переживает стирание номера). Заготовки владельца из UI не считаются и не ограничены."""
    row = conn.execute("SELECT source, config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
    daily = phone_daily(row[0], row[1]) if row is not None else TG_PHONE_DAILY_DEFAULT
    count, oldest = conn.execute(
        _sql(
            schema,
            "SELECT COUNT(*), MIN(first_seen_ts) FROM {schema}.channel WHERE account_id = ? "
            "AND first_seen_ts > ? AND json_extract(meta_json, '$.new_draft.to.kind') = 'tg_phone' "
            "AND COALESCE(json_extract(meta_json, '$.new_draft.origin'), '') <> ?",
        ),
        (account_id, now_ms - TG_PHONE_WINDOW_MS, draft_channels.UI_ORIGIN),
    ).fetchone()
    if count < daily or oldest is None:
        return
    wait = _wait_text(oldest + TG_PHONE_WINDOW_MS - now_ms)
    raise TooMany(f"лимит новых адресатов по номеру телефона: {daily} в сутки; следующий — через {wait}")


def parse_email(to: str) -> str:
    value = (to or "").strip()
    if not value or len(value) > EMAIL_MAX or _EMAIL_RE.match(value) is None:
        raise Invalid("to: нужен ровно один адрес вида local@domain, без имени и запятых")
    return value.lower()


def _parse_subject(subject: str | None) -> str:
    if subject is None:
        raise Invalid("subject: тема письма обязательна")
    problem = msgid.subject_problem(subject)
    if problem is not None:
        raise Invalid(f"subject: {problem}")
    value = subject.strip()
    if not 1 <= len(value) <= SUBJECT_MAX:
        raise Invalid(f"subject: 1–{SUBJECT_MAX} символов")
    return value


def _mail_identity(config_json: str | None) -> tuple[str, list[str]]:
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    address = str(config.get("address") or "").strip().lower()
    aliases = [str(a).strip().lower() for a in (config.get("aliases") or []) if str(a).strip()]
    return address, aliases


def _parse_from(from_address: str | None, config_json: str | None) -> str | None:
    """Решение 7: `from` — только адрес ящика или один из его `aliases` (без регистра), иначе 422."""
    if from_address is None:
        return None
    value = from_address.strip().lower()
    address, aliases = _mail_identity(config_json)
    if not value or value not in {address, *aliases}:
        raise Invalid("from: только адрес ящика или его алиас")
    return value


def mail_send_enabled(config_json: str | None) -> bool:
    """Ф12-5 A1/П5: ящик не «только на приём» — как читает коннектор (`send_enabled` не `false`)."""
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    return not (isinstance(config, dict) and config.get("send_enabled", True) is False)


def mail_address(config_json: str | None) -> str | None:
    """Ф12-5 A6: адрес ящика из конфига аккаунта (подпись «Откуда» в форме UI) или `None`."""
    address, _aliases = _mail_identity(config_json)
    return address if "@" in address else None


def _account(
    conn, account_id: int, sources: frozenset[str] = API_SOURCES
) -> tuple[str, str, str | None]:
    row = conn.execute(
        "SELECT source, db, config_json FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise Invalid("account_id: нет такого аккаунта")
    source, schema, config_json = row
    if source not in sources:
        if sources == API_SOURCES:
            raise Invalid("account_id: новому адресату пишется только из Telegram, WhatsApp или почты")
        raise Invalid(
            "account_id: новому адресату пишется только из Telegram, WhatsApp, Slack, Mattermost или почты"
        )
    return source, schema, config_json


def _recipient(
    source: str, schema: str, config_json: str | None, *, to: str, subject: str | None,
    from_address: str | None,
) -> Recipient:  # fmt: skip
    """Разбор и проверка адресата по виду аккаунта (422 с текстом) — одна функция для обеих ручек."""
    if source == "slack":
        raise Invalid("Slack: выберите людей из списка")
    if source == "mattermost":
        raise Invalid("Mattermost: выберите людей из списка")
    if source == "whatsapp":
        # Ф62-1: WhatsApp — только по номеру; вид `tg_phone` — общий «номер телефона» (стирание номера после
        # привязки, маска в названии, лимит заготовок — те же пути, что у Telegram).
        if subject is not None or from_address is not None:
            raise Invalid("subject/from — только для почты")
        if not looks_like_phone(to):
            raise Invalid("to: WhatsApp — номер телефона в международном формате (например +7 900 000-00-00)")
        phone = parse_tg_phone(to)
        if phone_daily(source, config_json) == 0:
            raise Invalid(WA_PHONE_OFF_TEXT)
        return Recipient(source, schema, {"kind": "tg_phone", "phone": phone}, mask_phone(phone), "dm", False)
    if source == "telegram":
        if subject is not None or from_address is not None:
            raise Invalid("subject/from — только для почты")
        if looks_like_phone(to):
            # Ф34-3: номер телефона — вид `tg_phone`; название — маска, полный номер — только в `new_to`.
            phone = parse_tg_phone(to)
            if tg_phone_daily(config_json) == 0:
                # Ревью Ф34-3: при 0 номер не принимается вовсе (и из формы владельца) — коннектор его не ищет.
                raise Invalid(PHONE_OFF_TEXT)
            return Recipient(
                source, schema, {"kind": "tg_phone", "phone": phone}, mask_phone(phone), "dm", False
            )
        username = parse_tg_username(to)
        return Recipient(
            source, schema, {"kind": "tg_username", "username": username}, f"@{username}", "dm", False
        )
    address = parse_email(to)
    subj = _parse_subject(subject)
    sender = _parse_from(from_address, config_json)
    new_to = {"kind": "email", "to": address, "subject": subj, "from": sender}
    return Recipient(source, schema, new_to, f"{subj} → {address}", "mail_thread", True)


def _open_drafts(conn, schema: str, account_id: int) -> list[tuple]:
    """Открытые (не архивные) беседы-заготовки аккаунта: `(channel_id, project_id, meta_json, первый outbox |
    None, map_source)`."""
    return conn.execute(
        _sql(
            schema,
            "SELECT c.id, c.project_id, c.meta_json, (SELECT MIN(o.id) FROM {schema}.outbox o "
            "WHERE o.channel_id = c.id), c.map_source FROM {schema}.channel c WHERE c.account_id = ? "
            "AND {temp} AND c.archived = 0",
            temp=draft_channels.temp_ext_sql("c"),
        ),
        (account_id,),
    ).fetchall()


def _same_open(
    open_rows: list[tuple], project_id: int | None, key: tuple | None, *, any_project: bool = False
) -> tuple | None:
    """П1: открытая заготовка того же (аккаунт, адресат, ПРОЕКТ) — `(channel_id, outbox_id | None,
    project_id)`.

    Ф14-2 A2: `project_id=None` («по правилам») — совпадает открытая заготовка `map_source='auto'` в любом
    проекте (правило могло её уже переложить); с проектом — ручная заготовка этого проекта.

    Ф34-3 П6: для номера телефона (`tg_phone`) открытая заготовка одна на (аккаунт, номер) БЕЗ проекта —
    один номер в двух проектах не даёт двух резолвов и двух «первых» сообщений.

    Ф46-1 П6: `any_project` — так же без проекта ищет запрос вида `tg_msg_author` (один автор в двух
    проектах — одна заготовка; ключ у него общий с `tg_user`)."""
    by_phone = any_project or (key is not None and key[0] == "tg_phone")
    for channel_id, channel_project, meta_json, outbox_id, map_source in open_rows:
        if by_phone:
            pass
        elif project_id is None:
            if map_source != "auto":
                continue
        elif map_source == "auto" or channel_project != project_id:
            continue
        if _recipient_key(draft_channels.new_to_of(meta_json)) == key:
            return channel_id, outbox_id, channel_project
    return None


def _insert_channel(
    conn, recipient: Recipient, *, account_id: int, project_id: int | None, new_draft: dict, now_ms: int
) -> tuple[int, dict]:
    """Беседа-заготовка: временный `ext_id`, вид, название, ручная привязка к проекту, `attrs.new_to`,
    `meta.new_draft`. Возвращает `(channel_id, meta)`.

    Ф14-2 A2 (П2): `project_id=None` — «по правилам»: `map_source='auto'`, без проекта, и сразу пересчёт
    правилами (`map_reason` пишет пересчёт; заготовка — не форум, топиков нет).

    Ф34-3 П3б: заготовка по номеру телефона не от владельца (`new_draft.origin != "ui"`) — сначала лимит ядра
    (`_check_phone_limit`, 429); все три входа зовут это ПОСЛЕ `_same_open`."""
    schema = recipient.schema
    if recipient.new_to.get("kind") == "tg_phone" and new_draft.get("origin") != draft_channels.UI_ORIGIN:
        _check_phone_limit(conn, schema, account_id, now_ms)
    if (
        recipient.new_to.get("kind") == MSG_AUTHOR_KIND
        and new_draft.get("origin") != draft_channels.UI_ORIGIN
    ):
        _check_author_limit(conn, schema, account_id, now_ms)  # Ф46-1, раздел 3: тоже после `_same_open`
    if recipient.extra:
        new_draft.update(recipient.extra)
    meta = {
        "own_id_space": recipient.own_id_space,
        "attrs": {"new_to": recipient.new_to},
        "new_draft": new_draft,
    }
    auto = project_id is None
    cur = conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.channel (account_id, ext_id, kind, title, project_id, first_seen_ts, "
            "map_source, map_reason, meta_json, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ),
        (
            account_id,
            draft_channels.new_temp_ext_id(),
            recipient.kind,
            recipient.title,
            project_id,
            now_ms,
            "auto" if auto else "manual",
            None if auto else draft_channels.MAP_REASON,
            json.dumps(meta, ensure_ascii=False, sort_keys=True),
            next_seq(conn),
        ),
    )
    channel_id = cur.lastrowid
    assert db_of(channel_id) == schema  # noqa: S101 — id из диапазона файла аккаунта (AUTOINCREMENT засеян)
    if auto:
        mapping.recompute_channel(conn, schema, channel_id, now_ms)
    return channel_id, meta


def create_channel(
    conn,
    *,
    account_id: int,
    project_id: int | None,
    to: str | dict,
    subject: str | None,
    now_ms: int,
    principal=None,
) -> ChannelCreated:
    """Ф12-5 A1: пустая беседа-заготовка UI (`meta.new_draft.origin = "ui"`), без строки outbox. Проверки —
    как у `create_draft` (общий код) плюс ящик «только на приём» -> 422 (П5, только здесь). Открытая заготовка
    того же аккаунта, адресата и проекта (почта — и темы) -> она же, `created=False` (решение 5). Видимость
    проекта проверяет маршрут ДО вызова (в той же транзакции).

    Ф14-2 A2: `to` — строка или объект `tg_user`/`slack_users` (`_object_recipient`); `tg_user`, у которого
    с этим аккаунтом уже есть настоящая личка, -> она, `created=False`, заготовки нет. `project_id=None` —
    «по правилам» (`_same_open`, `_insert_channel`).

    Ф46-1: объект `tg_msg_author` (`{"kind", "message_id"}`) — личка автору сообщения группы; `principal` —
    для видимости сообщения. Настоящая личка с автором уже есть -> она (как у `tg_user`); открытая заготовка
    этому человеку — в любом проекте -> она же."""
    source, schema, config_json = _account(conn, account_id, SOURCES)
    if source == "mail" and not mail_send_enabled(config_json):
        raise Invalid("account_id: ящик подключён только на приём — отправка из хаба выключена")
    if isinstance(to, dict):
        recipient = _object_recipient(
            conn, source, schema, account_id, to, subject=subject, principal=principal
        )
        if recipient.new_to["kind"] in ("tg_user", MSG_AUTHOR_KIND):
            real = _real_channel(conn, schema, account_id, str(recipient.new_to["user_id"]))
            if real is not None:
                return ChannelCreated(real, False, recipient.new_to["kind"])
    else:
        recipient = _recipient(source, schema, config_json, to=to, subject=subject, from_address=None)
    open_rows = _open_drafts(conn, schema, account_id)
    same = _same_open(
        open_rows,
        project_id,
        _recipient_key(recipient.new_to),
        any_project=recipient.new_to["kind"] == MSG_AUTHOR_KIND,
    )
    if same is not None:
        return ChannelCreated(same[0], False, recipient.new_to["kind"])
    if len(open_rows) >= MAX_OPEN_DRAFTS:
        raise TooMany(f"больше {MAX_OPEN_DRAFTS} новых бесед ждут отправки — сначала разберите их")
    channel_id, _meta = _insert_channel(
        conn,
        recipient,
        account_id=account_id,
        project_id=project_id,
        new_draft={"to": recipient.new_to, "origin": draft_channels.UI_ORIGIN},
        now_ms=now_ms,
    )
    return ChannelCreated(channel_id, True, recipient.new_to["kind"])


def _recipient_key(new_to: dict | None) -> tuple | None:
    if new_to is None:
        return None
    kind = new_to.get("kind")
    if kind == "tg_username":
        return ("tg_username", str(new_to.get("username") or "").casefold())
    if kind in ("tg_user", MSG_AUTHOR_KIND):
        # Ф46-1 П6: автор сообщения — тот же человек, что и `tg_user` с этим id: заготовка на него одна.
        return ("tg_user", new_to.get("user_id"))
    if kind == "tg_phone":
        return ("tg_phone", str(new_to.get("phone") or ""))
    if kind == "slack_users":
        users = new_to.get("users")
        return ("slack_users", tuple(sorted(str(u) for u in users)) if isinstance(users, list) else ())
    if kind == "mm_users":
        users = new_to.get("users")
        return ("mm_users", tuple(sorted(str(u) for u in users)) if isinstance(users, list) else ())
    return ("email", str(new_to.get("to") or "").casefold(), str(new_to.get("subject") or "").casefold())


# --- Ф14-2 A1/A2: адресат-объект из поиска «Кому» ----------------------------------------------------------


def _clean_name(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f"to.{field}: строка или null")
    value = " ".join(value.split())
    if len(value) > NAME_MAX:
        raise Invalid(f"to.{field}: не длиннее {NAME_MAX} символов")
    return value or None


def stored_name(value: object) -> str | None:
    """Имя из `contact`/справочника (данные источника, не ввод) — без 422: схлопнутые пробелы, обрезка."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:NAME_MAX] or None


def _real_channel(conn, schema: str, account_id: int, ext_id: str) -> int | None:
    """Настоящая (не заготовка) беседа аккаунта с этим `ext_id` или `None`."""
    if draft_channels.is_temp_ext_id(ext_id):
        return None
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def _contact(conn, schema: str, account_id: int, ext_id: str) -> tuple[str | None, str | None] | None:
    """`(name, handle)` из `contact` аккаунта (не `is_me`) или `None`."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT name, handle FROM {schema}.contact WHERE account_id = ? AND ext_id = ? AND is_me = 0",
        ),
        (account_id, ext_id),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _directory(conn, account_id: int, name: str) -> dict[str, dict]:
    """Справочник аккаунта (`tg_contacts`/`slack_people`, A6) по `id` строкой -> запись."""
    # `queries` сам импортирует этот модуль — импорт здесь, а не наверху (иначе цикл).
    from . import queries

    out: dict[str, dict] = {}
    for item in queries.account_meta_list(conn, account_id, name):
        key = item.get("id")
        if isinstance(key, str | int) and not isinstance(key, bool):
            out[str(key)] = item
    return out


def tg_contact_name(item: dict) -> str | None:
    """Имя записи `tg_contacts` — «имя фамилия»."""
    parts = [str(item.get(k) or "").strip() for k in ("first", "last")]
    return stored_name(" ".join(p for p in parts if p))


def username_or_none(value: object) -> str | None:
    """`@name`/`name` -> `name`, если это допустимый @username Telegram, иначе `None`."""
    if not isinstance(value, str):
        return None
    value = value.strip().removeprefix("@")
    return value if _USERNAME_RE.match(value) else None


def _tg_user_recipient(conn, schema: str, account_id: int, to: dict) -> Recipient:
    """`{"kind": "tg_user", "user_id", "name"?, "username"?}` — id пользователя Telegram (1..2^53); имя и
    username, если не пришли, — из `contact` аккаунта (`ext_id = str(user_id)`) или `tg_contacts`."""
    if set(to) - {"kind", "user_id", "name", "username"}:
        raise Invalid("to: лишние поля адресата")
    user_id = to.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not 1 <= user_id <= TG_USER_ID_MAX:
        raise Invalid("to.user_id: нужен id пользователя Telegram (целое 1..2^53)")
    name = _clean_name(to.get("name"), "name")
    username = None
    if to.get("username") is not None:
        username = username_or_none(to.get("username"))
        if username is None:
            raise Invalid("to.username: @username Telegram (латиница, цифры, _, 4–32 символа, с буквы)")
    if name is None or username is None:
        contact = _contact(conn, schema, account_id, str(user_id))
        if contact is not None:
            name = name or stored_name(contact[0])
            username = username or username_or_none(contact[1])
    if name is None or username is None:
        item = _directory(conn, account_id, "tg_contacts").get(str(user_id))
        if item is not None:
            name = name or tg_contact_name(item)
            username = username or username_or_none(item.get("username"))
    title = name or (f"@{username}" if username else f"id {user_id}")
    new_to = {"kind": "tg_user", "user_id": user_id, "name": name, "username": username}
    return Recipient("telegram", schema, new_to, title, "dm", False)


def slack_person_name(item: dict) -> str | None:
    """Имя записи `slack_people` — `display_name`, иначе `real_name` (как справочник коннектора)."""
    return stored_name(item.get("display_name")) or stored_name(item.get("real_name"))


def _slack_users_recipient(conn, schema: str, account_id: int, to: dict) -> Recipient:
    """`{"kind": "slack_users", "users": [...]}` — 1–8 людей Slack без повторов; имена — из `contact`
    аккаунта, затем `slack_people`, иначе id. Один — личка (`dm`), несколько — групповая личка: у настоящих
    бесед Slack она `group` (`slack/convert.channel_kind`); своё пространство id — как у всех бесед Slack."""
    if set(to) - {"kind", "users", "names"}:  # `names` клиента не берутся — имена ядро находит само
        raise Invalid("to: лишние поля адресата")
    users = to.get("users")
    if (
        not isinstance(users, list)
        or not 1 <= len(users) <= SLACK_USERS_MAX
        or not all(isinstance(u, str) and _SLACK_USER_RE.match(u) for u in users)
    ):
        raise Invalid(f"to.users: 1–{SLACK_USERS_MAX} людей Slack (id вида U…/W…)")
    if len(set(users)) != len(users):
        raise Invalid("to.users: один человек дважды")
    people: dict[str, dict] | None = None
    names = []
    for user in users:
        contact = _contact(conn, schema, account_id, user)
        name = stored_name(contact[0]) if contact is not None else None
        if name is None:
            if people is None:
                people = _directory(conn, account_id, "slack_people")
            item = people.get(user)
            name = slack_person_name(item) if item is not None else None
        names.append(name or user)
    new_to = {"kind": "slack_users", "users": list(users), "names": names}
    kind = "dm" if len(users) == 1 else "group"
    return Recipient("slack", schema, new_to, ", ".join(names), kind, True)


def mm_people(conn, account_id: int) -> dict[str, dict]:
    """Ф40-1b E.1: справочник `mm_people` аккаунта: id человека -> запись `{id, username, name, email, bot,
    deleted}` (только записи с допустимым id). В справочнике все: и я, и боты, и удалённые — флагами."""
    return {
        user_id: item
        for user_id, item in _directory(conn, account_id, "mm_people").items()
        if _MM_USER_RE.match(user_id)
    }


def mm_me_id(conn, account_id: int) -> str | None:
    """Мой id на сервере Mattermost — из `mm_server` аккаунта (публикует коннектор на старте)."""
    from . import queries  # `queries` сам импортирует этот модуль

    meta = queries.account_meta_value(conn, account_id, "mm_server")
    value = meta.get("value") if meta is not None else None
    me = value.get("me") if isinstance(value, dict) else None
    user_id = me.get("id") if isinstance(me, dict) else None
    return user_id if isinstance(user_id, str) and user_id else None


def mm_person_name(item: dict) -> str | None:
    """Имя записи `mm_people` — `name` (коннектор кладёт туда имя и фамилию, иначе ник), иначе `username`."""
    return stored_name(item.get("name")) or stored_name(item.get("username"))


def mm_addressable(conn, account_id: int) -> dict[str, dict]:
    """Люди Mattermost, которым можно писать: из справочника, не боты, не удалённые, не я."""
    me = mm_me_id(conn, account_id)
    return {
        user_id: item
        for user_id, item in mm_people(conn, account_id).items()
        if user_id != me and item.get("bot") is not True and item.get("deleted") is not True
    }


def _mm_users_recipient(conn, schema: str, account_id: int, to: dict) -> Recipient:
    """Ф40-1b E.3: `{"kind": "mm_users", "users": [...]}` — 1–7 людей Mattermost без повторов, только из
    справочника `mm_people` (не боты, не удалённые, не я). Один — личка (`dm`), больше — групповая (`group`).
    Имена — из справочника. Тексты отказов — без имён и почт."""
    if set(to) - {"kind", "users", "names"}:  # `names` клиента не берутся — имена ядро находит само
        raise Invalid("to: лишние поля адресата")
    users = to.get("users")
    if (
        not isinstance(users, list)
        or not 1 <= len(users) <= MM_USERS_MAX
        or not all(isinstance(u, str) and _MM_USER_RE.match(u) for u in users)
    ):
        raise Invalid(f"to.users: 1–{MM_USERS_MAX} людей Mattermost (id из 26 знаков)")
    if len(set(users)) != len(users):
        raise Invalid("to.users: один человек дважды")
    people = mm_addressable(conn, account_id)
    if any(user not in people for user in users):
        raise Invalid("to.users: человека нет в справочнике Mattermost (или это бот, удалённый, вы сами)")
    names = [mm_person_name(people[user]) or user for user in users]
    new_to = {"kind": "mm_users", "users": list(users), "names": names}
    kind = "dm" if len(users) == 1 else "group"
    return Recipient("mattermost", schema, new_to, ", ".join(names), kind, True)


def _positive_int_text(value: object) -> int | None:
    """Строка — запись положительного целого (id сообщения или человека у Telegram), иначе `None`."""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    number = int(value)
    return number if 1 <= number <= TG_USER_ID_MAX else None


def _negative_int_text(value: object) -> bool:
    """Строка — помеченный id чата Telegram (группа или супергруппа): «-» и цифры."""
    return isinstance(value, str) and value.startswith("-") and value[1:].isascii() and value[1:].isdigit()


def _tg_msg_author_recipient(
    conn, principal, source: str, schema: str, account_id: int, to: dict, *, project_id: int | None,
    own_project: bool,
) -> Recipient:  # fmt: skip
    """Ф46-1 (разделы 1, 10 П3–П5): `{"kind": "tg_msg_author", "message_id": N}` — автор сообщения группы
    Telegram. Клиент называет ТОЛЬКО id сообщения хаба; человека, чат и номер сообщения у источника выводит
    ядро. Проверка ключей — первой. `own_project` (bearer): проект беседы-источника должен равняться
    `project_id` запроса (токен `all` проекты не делит). Тексты отказов — без имён."""
    if set(to) != {"kind", "message_id"}:
        raise Invalid("to: у автора сообщения — ровно kind и message_id")
    message_id = to.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise Invalid("to.message_id: нужен id сообщения хаба (целое больше 0)")
    if principal is None or db_of(message_id) != schema:
        raise Invalid(MSG_NOT_FOUND_TEXT)
    from . import queries  # `queries` сам импортирует этот модуль

    try:
        queries.message_visible(conn, principal, message_id)
    except queries.NotFound:
        raise Invalid(MSG_NOT_FOUND_TEXT) from None
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.account_id, m.ext_id, m.kind, m.purged, m.deleted, m.is_bot, m.is_me, "
            "m.sender_contact_id, c.id, c.kind, c.ext_id, c.title, c.project_id, c.parent_channel_id "
            "FROM {schema}.message m JOIN {schema}.channel c ON c.id = m.channel_id WHERE m.id = ?",
        ),
        (message_id,),
    ).fetchone()
    if row is None:
        raise Invalid(MSG_NOT_FOUND_TEXT)
    (m_account, m_ext, m_kind, purged, deleted, is_bot, is_me, contact_id,
     ch_id, ch_kind, ch_ext, ch_title, ch_project, parent_id) = row  # fmt: skip
    if purged or deleted:
        raise Invalid("to: сообщение удалено")
    if m_account != account_id:
        raise Invalid(f"to: сообщение из аккаунта {m_account} — передай account_id={m_account}")
    if source != "telegram":
        raise Invalid("to: автор сообщения — только из группы Telegram")
    if own_project and ch_project != project_id:
        raise Invalid("to: сообщение из другого проекта")
    if m_kind not in ("text", "file"):
        raise Invalid("to: служебное сообщение — у него нет автора, которому можно написать")
    if ch_kind not in ("group", "topic"):
        raise Invalid("to: сообщение не из группы Telegram (личка или канал)")
    msg_ext = _positive_int_text(m_ext)
    if msg_ext is None:
        raise Invalid("to: у сообщения нет номера в Telegram (ещё не отправлено)")
    if is_me:
        raise Invalid("to: это твоё сообщение")
    contact = None
    if contact_id is not None:
        contact = conn.execute(
            _sql(schema, "SELECT ext_id, name, is_me FROM {schema}.contact WHERE id = ?"), (contact_id,)
        ).fetchone()
    if contact is not None and contact[2]:
        raise Invalid("to: это твоё сообщение")
    user_id = _positive_int_text(contact[0]) if contact is not None else None
    if user_id is None:
        raise Invalid("to: у сообщения нет автора-человека (анонимный админ или канал)")
    if is_bot:
        raise Invalid("to: автор — бот")
    # Ревью Ф46-1: у сообщений до 0033 признака бота нет — бот и тот, у кого он есть хоть на одном сообщении.
    known_bot = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.message WHERE sender_contact_id = ? AND is_bot = 1 LIMIT 1"),
        (contact_id,),
    ).fetchone()
    if known_bot is not None:
        raise Invalid("to: автор — бот")
    # П4: чат-родитель темы форума — по `parent_channel_id`; у темы без родителя — часть `ext_id` до «:».
    chat_ext, from_title = ch_ext, ch_title
    if ch_kind == "topic":
        parent = None
        if parent_id is not None:
            parent = conn.execute(
                _sql(schema, "SELECT ext_id, title FROM {schema}.channel WHERE id = ?"), (parent_id,)
            ).fetchone()
        if parent is not None:
            chat_ext, from_title = parent[0], parent[1] or ch_title
        else:
            chat_ext = str(ch_ext).partition(":")[0]
    if not _negative_int_text(chat_ext):
        raise Invalid("to: сообщение не из группы Telegram (личка или канал)")
    name = stored_name(contact[1])
    new_to = {
        "kind": MSG_AUTHOR_KIND,
        "user_id": user_id,
        "name": name,
        "chat_ext_id": chat_ext,
        "msg_ext_id": str(msg_ext),
    }
    extra = {
        "from_title": stored_name(from_title),
        "from_channel_id": ch_id,
        "from_message_id": message_id,
    }
    return Recipient("telegram", schema, new_to, name or f"id {user_id}", "dm", False, extra)


def _bearer_object_recipient(
    conn, principal, source: str, schema: str, account_id: int, to: dict, *, project_id: int,
    subject: str | None, from_address: str | None,
) -> Recipient:  # fmt: skip
    """Ф46-1 П5: адресат-объект от bearer (`create_draft`, `create_hold`) — ТОЛЬКО `tg_msg_author`, ключи
    ровно `{"kind", "message_id"}` (проверка ключей — первой). Прочие объектные виды (`tg_user`,
    `slack_users`, `mm_users`) остаются путём UI: `_object_recipient` отсюда не зовётся."""
    if set(to) != {"kind", "message_id"} or to.get("kind") != MSG_AUTHOR_KIND:
        raise Invalid('to: объектом — только автор сообщения: {"kind": "tg_msg_author", "message_id": N}')
    if subject is not None or from_address is not None:
        raise Invalid("subject/from — только для почты")
    return _tg_msg_author_recipient(
        conn, principal, source, schema, account_id, to, project_id=project_id, own_project=True
    )


def _bearer_dm_exists(conn, principal, recipient: Recipient, account_id: int) -> DmExists | None:
    """Ф46-1 П2: с автором уже есть настоящая личка — сессии этим путём туда не пишут: 409, номер беседы —
    только видимой токену."""
    if recipient.new_to["kind"] != MSG_AUTHOR_KIND:
        return None
    real = _real_channel(conn, recipient.schema, account_id, str(recipient.new_to["user_id"]))
    if real is None:
        return None
    from . import queries  # `queries` сам импортирует этот модуль

    try:
        queries.channel_visible(conn, principal, real)
    except queries.NotFound:
        return DmExists(None)
    return DmExists(real)


def _bot_dm_exists(conn, principal, recipient: Recipient, account_id: int) -> BotDmExists | None:
    """Ф63-2 П7: заготовка по @нику на «bot» — до её создания ищется личка этого аккаунта с контактом
    `handle == @ник` (без учёта регистра). Нашлась — отказ с номером (видимой токену) беседы."""
    new_to = recipient.new_to
    username = new_to.get("username")
    if new_to.get("kind") != "tg_username" or not draft_channels.is_bot_username(username):
        return None
    row = conn.execute(
        _sql(
            recipient.schema,
            "SELECT c.id, c.project_id FROM {schema}.contact ct JOIN {schema}.channel c "
            "ON c.account_id = ct.account_id AND c.ext_id = ct.ext_id "
            "WHERE ct.account_id = ? AND ct.is_me = 0 AND lower(ct.handle) = lower(?) AND c.kind = 'dm' "
            "ORDER BY c.archived, c.id LIMIT 1",
        ),
        (account_id, f"@{username}"),
    ).fetchone()
    if row is None:
        return None
    channel_id, project_id = row
    if principal is not None:
        from . import queries  # `queries` сам импортирует этот модуль

        try:
            queries.channel_visible(conn, principal, channel_id)
        except queries.NotFound:
            return BotDmExists(username, None, None)
    name = "Неразобранное"
    if project_id is not None:
        found = conn.execute("SELECT name FROM main.project WHERE id = ?", (project_id,)).fetchone()
        name = found[0] if found is not None else str(project_id)
    return BotDmExists(username, channel_id, name)


def _object_recipient(
    conn, source: str, schema: str, account_id: int, to: dict, *, subject: str | None, principal=None
) -> Recipient:
    """Ф14-2 A2: адресат-объект: `tg_user` — только аккаунт Telegram, `slack_users` — только Slack,
    `mm_users` (Ф40-1b) — только Mattermost. Ф46-1: `tg_msg_author` — автор сообщения группы Telegram."""
    if subject is not None:
        raise Invalid("subject — только для почты")
    kind = to.get("kind")
    if kind == MSG_AUTHOR_KIND:
        return _tg_msg_author_recipient(
            conn, principal, source, schema, account_id, to, project_id=None, own_project=False
        )
    if kind == "tg_user":
        if source != "telegram":
            raise Invalid("to: человек Telegram — только из аккаунта Telegram")
        return _tg_user_recipient(conn, schema, account_id, to)
    if kind == "slack_users":
        if source != "slack":
            raise Invalid("to: люди Slack — только из аккаунта Slack")
        return _slack_users_recipient(conn, schema, account_id, to)
    if kind == "mm_users":
        if source != "mattermost":
            raise Invalid("to: люди Mattermost — только из аккаунта Mattermost")
        return _mm_users_recipient(conn, schema, account_id, to)
    raise Invalid("to: неизвестный вид адресата")


def _prior(conn, schema_list, *, api_token_id: int, idempotency_key: str):
    """Строка outbox с этим ключом (обе схемы) -> `(schema, outbox_id, channel_id, text, status, message_id,
    account_id)` или `None`."""
    for schema in schema_list:
        row = conn.execute(
            _sql(
                schema,
                "SELECT o.id, o.channel_id, o.text, o.status, m.id, o.account_id FROM {schema}.outbox o "
                "LEFT JOIN {schema}.message m ON m.outbox_id = o.id "
                "WHERE o.api_token_id = ? AND o.idempotency_key = ?",
            ),
            (api_token_id, idempotency_key),
        ).fetchone()
        if row is not None:
            return (schema, *row)
    return None


def _draft_origin_to(conn, schema: str, account_id: int, outbox_id: int) -> dict | None:
    """Адресат черновика по его беседе-заготовке (`meta.new_draft.outbox_id`) — она остаётся и после привязки
    (переименование — та же строка, слияние — архивная)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT meta_json FROM {schema}.channel WHERE account_id = ? "
            "AND json_extract(meta_json, '$.new_draft.outbox_id') = ?",
        ),
        (account_id, outbox_id),
    ).fetchone()
    if row is None:
        return None
    try:
        info = json.loads(row[0]).get("new_draft")
    except ValueError, TypeError, AttributeError:
        return None
    to = info.get("to") if isinstance(info, dict) else None
    return to if isinstance(to, dict) else None


class Held(NamedTuple):
    """Ф14-3 A5: заготовка `hold` (без строки outbox); `created=False` — отдана открытая того же адресата."""

    channel_id: int
    created: bool
    kind: str  # вид адресата для аудита: `tg_username` | `email`


def create_hold(
    conn,
    *,
    account_id: int,
    project_id: int,
    to: str | dict,
    subject: str | None,
    from_address: str | None,
    now_ms: int,
    sees_project=None,
    principal=None,
) -> Held:
    """Ф14-3 A5 + П2, П3: беседа-заготовка сессии БЕЗ строки outbox (`meta.new_draft.origin = "api"`,
    `hold = true`) — в неё потом кладутся загрузки и первое сообщение с файлами (`/messages/send`). Проверки
    адресата и лимит — как у `create_draft`. Открытая заготовка того же аккаунта, адресата и проекта без живой
    строки (`draft_channels.has_live`) — она же, `created=False`, чья бы ни была (в том числе UI-заготовка
    владельца); с живой строкой — `DraftExists`. Настоящую беседу с адресатом не ищет (П2). Видимость проекта
    проверяет маршрут ДО вызова (в той же транзакции)."""
    source, schema, config_json = _account(conn, account_id)
    if isinstance(to, dict):
        # Ф46-1 П5: объектом — только автор сообщения; существующая настоящая личка — 409 (П2).
        recipient = _bearer_object_recipient(
            conn, principal, source, schema, account_id, to, project_id=project_id, subject=subject,
            from_address=from_address,
        )  # fmt: skip
        dm_exists = _bearer_dm_exists(conn, principal, recipient, account_id)
        if dm_exists is not None:
            raise dm_exists
    else:
        recipient = _recipient(
            source, schema, config_json, to=to, subject=subject, from_address=from_address
        )
        bot_dm = _bot_dm_exists(conn, principal, recipient, account_id)  # Ф63-2 П7
        if bot_dm is not None:
            raise bot_dm
    new_to = recipient.new_to
    open_rows = _open_drafts(conn, schema, account_id)
    same = _same_open(
        open_rows, project_id, _recipient_key(new_to), any_project=new_to["kind"] == MSG_AUTHOR_KIND
    )
    if same is not None:
        if sees_project is not None and not sees_project(same[2]):
            raise DraftElsewhere  # ревью Ф34-3: заготовка номера — в невидимом токену проекте
        if draft_channels.has_live(conn, schema, same[0]):
            raise DraftExists(same[1], same[0])
        return Held(same[0], False, new_to["kind"])
    if len(open_rows) >= MAX_OPEN_DRAFTS:
        raise TooMany(f"больше {MAX_OPEN_DRAFTS} новых бесед ждут отправки — сначала разберите их")
    channel_id, _meta = _insert_channel(
        conn,
        recipient,
        account_id=account_id,
        project_id=project_id,
        new_draft={"to": new_to, "origin": draft_channels.API_ORIGIN, "hold": True},
        now_ms=now_ms,
    )
    return Held(channel_id, True, new_to["kind"])


def create_draft(
    conn,
    *,
    account_id: int,
    project_id: int,
    to: str | dict,
    text: str,
    subject: str | None,
    from_address: str | None,
    api_token_id: int,
    idempotency_key: str | None,
    now_ms: int,
    sees_project=None,
    principal=None,
) -> Created:
    """A1 + П1: проверки -> повтор по ключу -> уникальность и лимит -> беседа-заготовка + черновик.
    Видимость проекта токену проверяет маршрут ДО вызова (в той же транзакции)."""
    source, schema, config_json = _account(conn, account_id)

    if text is None or not text.strip():
        raise Invalid("text: пустой текст")
    limit = MAIL_TEXT_MAX if source == "mail" else TEXT_MAX
    if len(text) > limit:
        raise Invalid(f"text: не длиннее {limit} символов")

    if isinstance(to, dict):
        # Ф46-1 П5: объектом — только автор сообщения.
        recipient = _bearer_object_recipient(
            conn, principal, source, schema, account_id, to, project_id=project_id, subject=subject,
            from_address=from_address,
        )  # fmt: skip
    else:
        recipient = _recipient(
            source, schema, config_json, to=to, subject=subject, from_address=from_address
        )
    new_to = recipient.new_to
    key = _recipient_key(new_to)

    if idempotency_key is not None:
        other = "work" if schema == "main" else "main"
        prior = _prior(conn, (schema, other), api_token_id=api_token_id, idempotency_key=idempotency_key)
        if prior is not None:
            p_schema, p_outbox, p_channel, p_text, p_status, p_message, p_account = prior
            p_to = _draft_origin_to(conn, p_schema, p_account, p_outbox) if p_account == account_id else None
            # Ф34-3 П1: номер у заготовки уже стёрт (привязка, архив) — сверяются вид адресата и текст.
            erased = p_to is not None and p_to.get("kind") == "tg_phone" and "phone" not in p_to
            same_to = new_to["kind"] == "tg_phone" if erased else _recipient_key(p_to) == key
            if p_to is None or not same_to or p_text != text:
                raise IdempotencyConflict("idempotency_key уже занят другим запросом")
            return Created(p_channel, p_outbox, p_message, p_status, False, False, new_to["kind"])

    # Ф46-1 П2 + П10: «уже есть настоящая личка» (409) — ПОСЛЕ повтора по ключу: после отправки заготовка
    # сама становится этой личкой, и повтор обязан вернуть прежнюю строку.
    dm_exists = _bearer_dm_exists(conn, principal, recipient, account_id)
    if dm_exists is not None:
        raise dm_exists
    # Ф63-2 П7: личка с этим ботом уже есть — пиши в неё (тоже после повтора по ключу).
    bot_dm = _bot_dm_exists(conn, principal, recipient, account_id)
    if bot_dm is not None:
        raise bot_dm

    # П1: уникальность — (аккаунт, адресат, проект), только открытые (не архивные) заготовки; лимит — на
    # аккаунт.
    open_rows = _open_drafts(conn, schema, account_id)
    same = _same_open(open_rows, project_id, key, any_project=new_to["kind"] == MSG_AUTHOR_KIND)
    if same is not None:
        if sees_project is not None and not sees_project(same[2]):
            raise DraftElsewhere  # ревью Ф34-3: заготовка номера — в невидимом токену проекте
        raise DraftExists(same[1], same[0])
    if len(open_rows) >= MAX_OPEN_DRAFTS:
        raise TooMany(f"больше {MAX_OPEN_DRAFTS} новых бесед ждут отправки — сначала разберите их")

    channel_id, meta = _insert_channel(
        conn, recipient, account_id=account_id, project_id=project_id, new_draft={"to": new_to}, now_ms=now_ms
    )
    result = outbox_mod.enqueue(
        conn,
        channel_id=channel_id,
        text=text,
        origin="api",
        api_token_id=api_token_id,
        idempotency_key=idempotency_key,
        now_ms=now_ms,
        paced=True,  # Ф12-2, решение 1: первое касание новому человеку — всегда по графику
    )
    meta["new_draft"]["outbox_id"] = result.outbox_id
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET meta_json = ? WHERE id = ?"),
        (json.dumps(meta, ensure_ascii=False, sort_keys=True), channel_id),
    )
    return Created(
        channel_id, result.outbox_id, result.message_id, result.status, False, True, new_to["kind"]
    )
