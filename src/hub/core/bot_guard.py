"""Страховка переписки сессий с Telegram-ботами (Ф63-2, docs/architecture.md — они главнее).

В беседе с ботом ЛЮБОЙ путь «сразу» (список «без подтверждения», выключатели автопилота `hub_send`,
`hub_send_new`, `hub_press`, `bot_chat`) сначала проходит `risky`: сработала — обычная карточка владельцу
(черновик «ждёт подтверждения» / «Нажать?») с пометкой категории. Решает владелец кликом (или его агент — его
клик, Ф56); ответ канала владельца сюда не ходит.

Категории: `payment` (оплата, покупка, списание, номер карты), `secret` (пароли, коды), `delete` (удаление,
блокировка, отписка), `pdn` (телефон, почта, паспорт), `file` (файл в беседу с ботом — всегда карточка, П6).

`risky` детерминирована: пометку у черновика интерфейс получает вычислением при чтении (`outbox_category`), у
нажатия — в `button_press.reason` (`REASON_PREFIX` + категория). В аудит (`bot_guard`) пишется только
категория — без текста.

Ложные срабатывания осознанны («перевод текста» — `payment`): лишняя карточка дешевле ошибки."""

import json
import re
import unicodedata
from collections.abc import Sequence

from . import draft_channels
from .audit import write_audit
from .channels import bot_meta
from .db import db_of

PAYMENT = "payment"
SECRET = "secret"  # noqa: S105 — имя категории, не секрет
DELETE = "delete"
PDN = "pdn"
FILE = "file"
CATEGORIES = (PAYMENT, SECRET, DELETE, PDN, FILE)

# П8: пометка страховки у нажатия-карточки — `button_press.reason = "bot_guard:<категория>"`.
REASON_PREFIX = "bot_guard:"
# П4: сколько текста сообщения бота (вопроса) смотрим.
CONTEXT_MAX = 2000
# Раздел 8 спеки: контекст черновика — сообщения бота после последнего своего, не больше стольких последних и
# не старше суток; строк беседы просматривается не больше `_CONTEXT_SCAN`.
CONTEXT_MESSAGES = 10
CONTEXT_WINDOW_MS = 24 * 3600 * 1000
_CONTEXT_SCAN = 200

# Начала слов (после `lower()` и `ё` -> `е`). Порядок категорий в проверке: удаление, оплата, секрет, ПДн.
_PAYMENT_STEMS = (
    "оплат",
    "оплач",
    "купи",
    "купл",
    "покуп",
    "заплат",
    "перевод",
    "платеж",
    "подписк",
    "продл",
    "списа",
    "списыв",
    "руб",
    "звезд",
    "pay",
    "buy",
    "purchas",
    "invoice",
    "checkout",
    "subscrib",
    "subscript",
    "renew",
    "stars",
)
_PAYMENT_SIGNS = ("₽", "$", "€", "⭐")
_DELETE_STEMS = ("удал", "блок", "заблок", "отпис", "delete", "remove", "deactivat", "unsubscrib")
_DELETE_COMMANDS = frozenset(
    {"/delete", "/remove", "/stop", "/unsubscribe", "/deactivate", "/cancel_subscription", "/reset"}
)
_SECRET_STEMS = ("парол", "password", "passcode")
_SECRET_WORDS = frozenset({"pin", "cvv", "cvc", "otp"})
_SECRET_PHRASE = re.compile(r"(?<![^\W\d_])(?:код\w*\s+из|code\s+from)(?![^\W\d_])")
_PDN_STEMS = ("паспорт", "снилс")
_PDN_WORDS = frozenset({"инн"})

_WORD_RE = re.compile(r"[^\W\d_]+")
# Группа цифр: пробелы и дефисы между цифрами склеиваются («123 456», «12-34-56», «+7 900 000-00-00»).
_DIGITS_RE = re.compile(r"\+?\d(?:[ \t\u00a0\-\u2013]*\d)*")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_EDGE_PUNCT = ".,;:!?()[]{}\"'«»<>`"


# Раздел 8 спеки: невидимые символы внутри слов (нулевой ширины, мягкий перенос, знаки направления) —
# убираются.
_INVISIBLE_CODES = [0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180E, 0x3164, 0xFEFF, 0xFFA0]
_INVISIBLE_CODES += [
    *range(0x200B, 0x2010),
    *range(0x202A, 0x202F),
    *range(0x2060, 0x2065),
    *range(0x2066, 0x206A),
]
_INVISIBLE = dict.fromkeys(_INVISIBLE_CODES, None)
# Латинские двойники кириллицы (после `lower()`) — только в словах со смешанным письмом.
_LATIN_TWINS = str.maketrans("aeopcxykmthb", "аеорсхукмтнв")
# Одиночные буквы через одиночный пробел или точку («о п л а т и т ь», «о.п.л.а.т.и.т.ь») — склеиваются.
_SPACED_RE = re.compile(r"(?<![^\W\d_])[^\W\d_](?:[ .][^\W\d_]){2,}(?![^\W\d_])")
_SPACED_SEP_RE = re.compile(r"[ .]")


def _unmix(match: re.Match) -> str:
    word = match.group(0)
    has_cyr = any(0x0400 <= ord(ch) <= 0x04FF for ch in word)
    has_lat = any("a" <= ch <= "z" for ch in word)
    return word.translate(_LATIN_TWINS) if has_cyr and has_lat else word


def _norm(text: str) -> str:
    """Раздел 8 спеки: NFKC, нижний регистр, `ё` -> `е`, без невидимых символов; одиночные буквы через пробел
    или точку склеены; латинские двойники в словах со смешанным письмом — кириллицей."""
    low = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).lower().replace("ё", "е")
    low = _SPACED_RE.sub(lambda m: _SPACED_SEP_RE.sub("", m.group(0)), low)
    return _WORD_RE.sub(_unmix, low)


def _words_category(text: str) -> str | None:
    """Проверки по словам и знакам — и для своего текста, и для сообщения бота (П4); текст нормализуется
    (`_norm`, раздел 8 спеки)."""
    low = _norm(text)
    words = _WORD_RE.findall(low)
    if any(w.startswith(_DELETE_STEMS) for w in words):
        return DELETE
    if any(w.startswith(_PAYMENT_STEMS) for w in words) or any(s in low for s in _PAYMENT_SIGNS):
        return PAYMENT
    if any(w.startswith(_SECRET_STEMS) or w in _SECRET_WORDS for w in words) or _SECRET_PHRASE.search(low):
        return SECRET
    if any(w.startswith(_PDN_STEMS) or w in _PDN_WORDS for w in words):
        return PDN
    return None


def _command(text: str) -> str | None:
    head = text.strip().split(maxsplit=1)
    if not head or not head[0].startswith("/"):
        return None
    return head[0].split("@", 1)[0].lower()


def _own_category(text: str) -> str | None:
    """Проверки только своего текста (П5): цифры, код из букв и цифр, CVV, почта."""
    for match in _DIGITS_RE.finditer(text):
        group = match.group(0)
        digits = sum(ch.isdigit() for ch in group)
        if digits < 4:
            continue
        if digits >= 13:
            return PAYMENT  # номер карты
        if group.startswith("+") or digits >= 10:
            return PDN  # телефон
        return SECRET  # код, пин
    if re.fullmatch(r"\d{3}", text.strip()):
        return SECRET  # CVV
    for token in text.split():
        token = token.strip(_EDGE_PUNCT)
        if len(token) >= 6 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            return SECRET
    if _EMAIL_RE.search(text):
        return PDN
    return None


def _is_start_deeplink(text: str, command: str | None) -> bool:
    """Раздел 8 спеки: `/start <параметр>` — одна команда и один токен (диплинк бота)."""
    return command == "/start" and len(text.split()) == 2


def risky(text: str | None, *, context: str | Sequence[str] | None = None) -> str | None:
    """Категория страховки или `None`. `text` — свой текст (черновик сессии или текст кнопки): все проверки;
    `context` — сообщение бота или несколько (вопрос, на который отвечают; сообщения бота после своего
    последнего; сообщение с кнопкой — П4, раздел 8 спеки): только слова и знаки (цифры и коды в чужом тексте
    ничего не значат), категория — по любому из них, каждое — до `CONTEXT_MAX` символов.

    Параметр диплинка `/start <параметр>` категорию `secret` не даёт (раздел 8 спеки)."""
    own = text or ""
    command = _command(own)
    if command in _DELETE_COMMANDS:
        return DELETE
    found = _words_category(own)
    if found is None:
        found = _own_category(own)
        if found == SECRET and _is_start_deeplink(own, command):
            found = None
    if found is not None:
        return found
    for item in [context] if isinstance(context, str) else context or ():
        hit = _words_category(item[:CONTEXT_MAX]) if item else None
        if hit is not None:
            return hit
    return None


# --- беседа с ботом -----------------------------------------------------------------------------------------


def chat_is_bot(source: str | None, kind: str | None, ext_id: str | None, meta_json: str | None) -> bool:
    """Ф63-2 П2: настоящая личка Telegram с ботом (`attrs.tg_type == "bot"`) или заготовка `new:` по нику на
    «bot» (предварительный признак — только выбрать ветку и страховку)."""
    if draft_channels.is_temp_ext_id(ext_id):
        return source == "telegram" and kind == "dm" and draft_channels.is_bot_draft(meta_json)
    return bot_meta(source, kind, meta_json)


def is_bot_chat(conn, channel_id: int) -> bool:
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT a.source, c.kind, c.ext_id, c.meta_json FROM {schema}.channel c "  # noqa: S608
        "JOIN main.account a ON a.id = c.account_id WHERE c.id = ?",
        (channel_id,),
    ).fetchone()
    return row is not None and chat_is_bot(*row)


def bot_context(conn, channel_id: int, reply_to_ext_id: str | None = None, *, now_ms: int) -> list[str]:
    """П4 + раздел 8 спеки: сообщение бота, на которое отвечает черновик (`reply_to`, без предела давности),
    ПЛЮС все входящие сообщения бота после последнего своего отправленного — не больше `CONTEXT_MESSAGES`
    последних и не старше `CONTEXT_WINDOW_MS` от `now_ms`; каждое до `CONTEXT_MAX` символов. Своя строка без
    `ext_id` (неотправленная заготовка, в том числе сам черновик) границей не считается: бот её не видел.
    Скрытое, очищенное и удалённое входящее не берётся."""
    schema = db_of(channel_id)
    alive = "is_me = 0 AND hidden = 0 AND purged = 0 AND deleted = 0 AND text IS NOT NULL AND text <> ''"
    texts: list[str] = []
    if reply_to_ext_id is not None:
        row = conn.execute(
            f"SELECT text FROM {schema}.message WHERE channel_id = ? AND ext_id = ? AND {alive}",  # noqa: S608
            (channel_id, reply_to_ext_id),
        ).fetchone()
        if row is not None:
            texts.append(row[0][:CONTEXT_MAX])
    rows = conn.execute(
        f"SELECT is_me, ext_id, text, hidden, purged, deleted FROM {schema}.message "  # noqa: S608
        "WHERE channel_id = ? AND ts >= ? AND NOT (is_me = 1 AND ext_id IS NULL) "
        "ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT ?",
        (channel_id, now_ms - CONTEXT_WINDOW_MS, _CONTEXT_SCAN),
    ).fetchall()
    taken = 0
    for is_me, ext_id, text, hidden, purged, deleted in rows:
        if is_me:
            break
        if hidden or purged or deleted or not isinstance(text, str) or not text:
            continue
        taken += 1
        if ext_id is None or ext_id != reply_to_ext_id:
            texts.append(text[:CONTEXT_MAX])
        if taken >= CONTEXT_MESSAGES:
            break
    return texts


def message_text(conn, message_id: int) -> str | None:
    """Текст сообщения с кнопкой (П4) — до `CONTEXT_MAX`; скрытое и очищенное не берётся."""
    row = conn.execute(
        f"SELECT text FROM {db_of(message_id)}.message WHERE id = ? AND hidden = 0 AND purged = 0",  # noqa: S608
        (message_id,),
    ).fetchone()
    return row[0][:CONTEXT_MAX] if row is not None and isinstance(row[0], str) else None


def _has_files(file_json: str | None) -> bool:
    if not file_json:
        return False
    try:
        value = json.loads(file_json)
    except ValueError, TypeError:
        return True  # кривое значение — считаем файлом: лишняя карточка дешевле
    return bool(value)


def outbox_category(conn, outbox_id: int, *, now_ms: int | None = None) -> str | None:  # noqa: ARG001
    """Категория страховки строки outbox в беседе с ботом — всегда `None`: владелец 24.10 снял страховку и с
    текстов боту (ложные срабатывания на обычных откликах). Текст и файлы боту держат только выключатели
    (`bot_chat`, `hub_send`), таблица карточек и пределы. Ф63-2 П4/П6 — отменено; `risky` оставлен для возврата по слову
    владельца."""
    return None


def press_reason(category: str) -> str:
    return f"{REASON_PREFIX}{category}"


def reason_category(reason: object) -> str | None:
    """Категория из `button_press.reason` (`bot_guard:<категория>`) или `None`."""
    if isinstance(reason, str) and reason.startswith(REASON_PREFIX):
        category = reason[len(REASON_PREFIX) :]
        return category if category in CATEGORIES else None
    return None


def audit_hit(
    conn,
    *,
    category: str,
    channel_id: int,
    path: str,
    now_ms: int,
    outbox_id: int | None = None,
    press_id: int | None = None,
) -> None:
    """П9: срабатывание страховки — запись `bot_guard` с категорией, без текста. `path` — путь «сразу», с
    которого вернули в карточку (`list` | `bot_chat` | `autopilot`); `kind` — черновик или нажатие."""
    detail: dict = {"category": category, "path": path, "kind": "press" if press_id is not None else "draft"}
    if press_id is not None:
        detail["press_id"] = press_id
    write_audit(
        conn,
        now_ms=now_ms,
        actor="core",
        api_token_id=None,
        client_name=None,
        request_id=f"bot-guard-{press_id if press_id is not None else outbox_id}",
        action="bot_guard",
        result=category,
        channel_id=channel_id,
        outbox_id=outbox_id,
        detail=detail,
    )
