"""Письмо Gmail (метаданные IMAP + сырое письмо) -> события ядра; письмо-ответ (E2). Спецификация:
`docs/architecture.md` и М3, М4, М7, М14.

Чистые функции — без сети. Детерминизм: одно и то же письмо -> побайтно тот же dict (иначе сверка шумит).
`ext_id` сообщения строится ОДНОЙ функцией (`message_ext_id`) и из полного письма, и из заголовков
метаданных треда — иначе маркер прочтения и родитель ответа не нашли бы строку в ядре.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import format_datetime, getaddresses
from pathlib import Path
from typing import Any

import emoji as emoji_lib

from ... import msgid as msgid_mod
from .. import refs
from . import textproc
from .imapwire import FetchedMsg

KIND = "mail_thread"
MAX_ATTRS_BYTES = 2 * 1024  # М3: attrs ≤ 2 КиБ (лимит ядра 4 КиБ)
MAX_FROM_DOMAINS = 20
MAX_REFERENCES = 20
NO_SUBJECT = "(без темы)"
DEFAULT_EXCLUDE_CATEGORIES = ("promotions", "social")  # C3: вкладки Gmail, которые не берутся

_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|fw|aw|отв|ответ|пересл)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*)+", re.IGNORECASE
)
_RE_PREFIX_RE = re.compile(r"^\s*re\s*(?:\[\d+\]|\(\d+\))?\s*:", re.IGNORECASE)
_MSGID_RE = re.compile(r"<[^<>\s]+>")
_HUB_MSGID_RE = re.compile(r"^<hub-([0-9a-f]{16,64})@[^<>\s]+>$")
# М8: запасной поиск X-GM-RAW rfc822msgid — только для «безопасного» Message-ID.
SAFE_MSGID_RE = re.compile(r"^[A-Za-z0-9._%+=@<>-]+$")

# системные метки Gmail -> имя в attrs.labels (прочие системные — прочь: \Important, \Starred, \Draft…)
_SYSTEM_LABELS = {"\\Inbox": "INBOX", "\\Sent": "SENT"}


# --- заголовки ---------------------------------------------------------------------------------------------


def raw_headers(raw: bytes | None) -> dict[str, str]:
    """Сырые значения заголовков (compat32, без раскодирования) — нижний регистр имени -> ПЕРВОЕ значение.
    Основа `message_ext_id`: одинаково для полного письма и для `BODY[HEADER.FIELDS …]`."""
    if not raw:
        return {}
    try:
        msg = BytesHeaderParser(policy=policy.compat32).parsebytes(raw)
    except Exception:  # noqa: BLE001 — кривые заголовки не роняют пачку
        return {}
    out: dict[str, str] = {}
    for name, value in msg.items():
        key = name.lower()
        if key not in out:
            out[key] = str(value)
    return out


def decoded_header(raw: bytes | None, name: str) -> str | None:
    """Раскодированный заголовок (`=?utf-8?…?=` -> текст) — для темы, имён."""
    if not raw:
        return None
    try:
        msg = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        value = msg.get(name)
    except Exception:  # noqa: BLE001
        return None
    if value is None:
        return None
    return " ".join(str(value).split())


def normalize_msgid(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = "".join(value.split())
    return cleaned or None


def message_ext_id(header_value: str | None, gm_msgid: int | None) -> str:
    """C1: `Message-ID` как в письме (с угловыми скобками); нет заголовка -> `<gm-<X-GM-MSGID>@hub>`."""
    mid = normalize_msgid(header_value)
    if mid:
        return mid
    return f"<gm-{gm_msgid if gm_msgid is not None else 0}@hub>"


def ext_id_from_headers(raw: bytes | None, gm_msgid: int | None) -> str:
    return message_ext_id(raw_headers(raw).get("message-id"), gm_msgid)


def addresses(raw: bytes | None, name: str) -> list[tuple[str, str]]:
    """`[(имя, адрес_в_нижнем_регистре)]` заголовка (From/To/Cc/Reply-To)."""
    if not raw:
        return []
    try:
        msg = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        values = msg.get_all(name) or []
    except Exception:  # noqa: BLE001
        values = raw_headers(raw).get(name.lower())
        values = [values] if values else []
    out: list[tuple[str, str]] = []
    for display, addr in getaddresses([str(v) for v in values]):
        addr = addr.strip().lower()
        if addr:
            out.append((" ".join(display.split()), addr))
    return out


def msgid_list(value: str | None) -> list[str]:
    return _MSGID_RE.findall(value or "")


def send_token_from_msgid(ext_id: str) -> str | None:
    m = _HUB_MSGID_RE.match(ext_id)
    return m.group(1) if m else None


def normalize_subject(subject: str | None) -> str:
    """C1/М14: префиксы `Re`, `RE`, `Re[2]`, `Fwd`, `FW`, `AW`, `Отв`, `Пересл` в любом регистре,
    повторяющиеся — прочь."""
    s = " ".join((subject or "").split())
    s = _SUBJECT_PREFIX_RE.sub("", s).strip()
    return s or NO_SUBJECT


# Ф63-1 П3: тема письма в событии — для правил входящих проекта (ядро режет длиннее как ядовитое событие).
EVENT_SUBJECT_MAX = 300


def event_subject(raw: bytes | None) -> str | None:
    """Ф63-1 П3: тема письма для поля `subject` события — как заголовок беседы (`normalize_subject`: без
    `Re:`/`Fwd:`, пробелы схлопнуты), но без заглушки «(без темы)»: темы нет — `None`. Не длиннее 300."""
    s = " ".join((decoded_header(raw, "Subject") or "").split())
    s = _SUBJECT_PREFIX_RE.sub("", s).strip()
    return s[:EVENT_SUBJECT_MAX].rstrip() or None


def reply_subject(subject: str | None) -> str:
    """E2: `Re: <тема родителя>` без двойного `Re:`."""
    s = " ".join((subject or "").split())
    if _RE_PREFIX_RE.match(s):
        return s
    return f"Re: {s}" if s else "Re:"


# Ф12-1 П4: общая функция с ядром (`hub.msgid`) — ядро само считает Message-ID черновика новой беседы.
domain_of = msgid_mod.domain_of


# --- метки и attrs ------------------------------------------------------------------------------------------


def normalize_labels(labels: list[str]) -> list[str]:
    out: list[str] = []
    for label in labels:
        if label in _SYSTEM_LABELS:
            out.append(_SYSTEM_LABELS[label])
        elif label.startswith("\\"):
            continue
        elif label.startswith(("[Gmail]", "[Google Mail]")):
            continue
        else:
            out.append(label)
    return out


def thread_attrs(thread_msgs: list[FetchedMsg], extra: dict | None = None) -> dict:
    """М3: `labels` — объединение меток ВСЕХ писем треда (кроме черновиков), `from_domains` — не больше 20;
    `attrs` целиком ≤ 2 КиБ (лишнее отрезается с конца отсортированных списков). `extra` — ключи беседы
    трекера (Ф10-5 A5: `tracker`, `key`, `url`, `no_reply`, `board`) — не урезаются, только метки/домены."""
    labels: set[str] = set()
    domains: set[str] = set()
    for m in thread_msgs:
        if m.is_draft:
            continue
        labels.update(normalize_labels(m.labels))
        for _name, addr in addresses(m.header, "From"):
            d = domain_of(addr)
            if d:
                domains.add(d)
    label_list = sorted(labels)
    domain_list = sorted(domains)[:MAX_FROM_DOMAINS]
    attrs = {"labels": label_list, "from_domains": domain_list}
    if extra:
        attrs.update(extra)
    while len(json.dumps(attrs, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_ATTRS_BYTES:
        if attrs["from_domains"]:
            attrs["from_domains"] = attrs["from_domains"][:-1]
        elif attrs["labels"]:
            attrs["labels"] = attrs["labels"][:-1]
        else:
            break
    return attrs


# --- событие письма (C1) -----------------------------------------------------------------------------------


@dataclass
class MeInfo:
    address: str
    aliases: list[str] = field(default_factory=list)

    @property
    def all(self) -> frozenset[str]:
        return frozenset([self.address.lower(), *(a.lower() for a in self.aliases)])


def is_mine(msg: FetchedMsg, from_addr: str | None, me: MeInfo) -> bool:
    """М14: адрес отправителя ∈ адрес + aliases ИЛИ у письма метка `\\Sent`."""
    return "\\Sent" in msg.labels or (from_addr is not None and from_addr in me.all)


# Ф57-1 П2: локальная часть адреса отправителя письма-рассылки — точно или с хвостом после `+`/`-`/`.`
# (`noreply-123@`, `notifications+abc@`).
_BULK_LOCAL_RE = re.compile(
    r"^(?:no-reply|noreply|no_reply|donotreply|do-not-reply|mailer-daemon|notifications|notification)"
    r"(?:[+.\-].*)?$",
    re.IGNORECASE,
)
_BULK_PRECEDENCE = ("bulk", "junk")


def is_bulk(headers: dict[str, str], from_addr: str | None) -> bool:
    """Ф57-1 П1/П2: письмо-рассылка или уведомление сервиса — флаг `bulk` события (в почтовой беседе
    «реагирую» такое письмо не даёт ни карточки, ни уведомления, ни «Нужна реакция»). Признаки: заголовок
    `List-Unsubscribe` без `List-Post`; `Precedence: bulk | junk`; `Auto-Submitted`, отличный от `no`;
    служебная локальная
    часть адреса `From`. `List-Id` и `Precedence: list` сами по себе — НЕ признак: люди пишут через группы
    Google и корпоративные списки. `headers` — `raw_headers()` письма (имена в нижнем регистре)."""
    # Список-обсуждение (группа Google, Mailman) дописывает `List-Unsubscribe` и письмам людей; его отличает
    # `List-Post` — адрес, куда пишут участники. У рассылок сервисов `List-Post` нет.
    if "list-unsubscribe" in headers and "list-post" not in headers:
        return True
    if headers.get("precedence", "").strip().lower() in _BULK_PRECEDENCE:
        return True
    auto = headers.get("auto-submitted", "").split(";", 1)[0].strip().lower()
    if auto and auto != "no":
        return True
    local = (from_addr or "").rsplit("@", 1)[0]
    return bool(local and _BULK_LOCAL_RE.match(local))


def channel_ref(thrid: int, *, title: str | None, attrs: dict | None) -> dict:
    ref: dict = {"ext_id": str(thrid), "kind": KIND, "own_id_space": True}
    if title is not None:
        ref["title"] = title
    if attrs is not None:
        ref["attrs"] = attrs
    return ref


def message_event(
    msg: FetchedMsg,
    *,
    me: MeInfo,
    channel: dict,
    thread_msgids: set[str],
) -> dict:
    """Событие `upsert` письма. `msg.body` — сырое письмо (или его начало); `thread_msgids` — Message-ID
    писем треда в ящике (для `reply_to_ext_id`: только если родитель есть в треде)."""
    raw = msg.body or b""
    headers = raw_headers(raw)
    ext_id = message_ext_id(headers.get("message-id"), msg.msgid)
    from_list = addresses(raw, "From")
    if not from_list and headers.get("from"):
        from_list = [("", headers["from"].strip().lower())]
    from_name, from_addr = from_list[0] if from_list else ("", "")
    mine = is_mine(msg, from_addr or None, me)

    body = textproc.extract_body(raw, partial=msg.body_partial)
    text = body.text
    attachments = body.attachments or None
    kind = "text"
    if body.unparsed:
        text = textproc.UNPARSED_TEXT
        attachments = None
    elif not text and attachments:
        kind = "file"
        text = None

    to_addrs = {addr for _n, addr in addresses(raw, "To")}
    mentions_me = (not mine) and bool(to_addrs & me.all)

    in_reply_to = msgid_list(headers.get("in-reply-to"))
    reply_to = in_reply_to[0] if in_reply_to and in_reply_to[0] in thread_msgids else None
    if reply_to == ext_id:
        reply_to = None

    event: dict = {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": msg.internaldate_ms if msg.internaldate_ms is not None else 0,
        "is_me": mine,
        "kind": kind,
        "text": text,
        "mentions_me": mentions_me,
    }
    # Ф57-1 П1/П3: рассылка — только здесь (не в `tracker_event`), ключ кладётся только когда истинно;
    # своё письмо рассылкой не бывает.
    if not mine and is_bulk(headers, from_addr or None):
        event["bulk"] = True
    subject = event_subject(raw)
    if subject is not None:
        event["subject"] = subject  # Ф63-1 П3: условие `subject` правил входящих проекта
    if from_addr:
        event["sender"] = {
            "ext_id": from_addr,
            "name": from_name or from_addr,
            "handle": from_addr,
            "is_me": mine,
        }
    if attachments:
        event["attachments"] = attachments
    link_refs = refs.extract_refs(text) if text and not body.unparsed else []
    if link_refs:
        event["refs"] = link_refs
    if reply_to:
        event["reply_to_ext_id"] = reply_to
    if mine:
        token = send_token_from_msgid(ext_id)
        if token:
            event["send_token"] = token
        mail_to = recipients(raw)
        if mail_to:
            event["mail_to"] = mail_to
    return event


# Ф11-8: адресатов своего письма в событии не больше 20; адрес длиннее 254 (предел RFC 5321) — мусор.
MAIL_TO_MAX = 20
_MAIL_TO_ADDR_MAX = 254


def recipients(raw: bytes | None) -> list[str]:
    """Ф11-8: адресаты (To, затем Cc) своего письма для поля `mail_to` события — только адреса с `@` длиной
    3..254, без повторов, в порядке заголовков, первые 20 (`addresses()` уже приводит к нижнему регистру).
    Фильтр здесь, а не в ядре: ядро на кривом поле отказывает ВСЕМУ событию — письмо ушло бы в dead_letter."""
    out: list[str] = []
    for name in ("To", "Cc"):
        for _display, addr in addresses(raw, name):
            # Ядро берёт строки 3..320 символов: «a@» и «@» тоже отсеиваем здесь.
            if "@" not in addr or not 3 <= len(addr) <= _MAIL_TO_ADDR_MAX or addr in out:
                continue
            out.append(addr)
            if len(out) >= MAIL_TO_MAX:
                return out
    return out


def tracker_event(msg: FetchedMsg, part: Any, *, me: MeInfo) -> dict:
    """Ф10-5 A5: событие `upsert` письма-уведомления трекера в беседу задачи/карточки (`part` —
    `trackers.TrackerPart`). `ext_id` — тот же Message-ID письма (одно письмо — по событию на каждую
    беседу), отправитель — автор действия (контакт `<трекер>:<имя casefold>`), нет автора — `From`, как у
    обычного письма; `reply_to_ext_id` нет."""
    raw = msg.body or b""
    headers = raw_headers(raw)
    from_list = addresses(raw, "From")
    if not from_list and headers.get("from"):
        from_list = [("", headers["from"].strip().lower())]
    from_name, from_addr = from_list[0] if from_list else ("", "")
    mine = is_mine(msg, from_addr or None, me)
    event: dict = {
        "type": "upsert",
        "channel": part.channel,
        "ext_id": message_ext_id(headers.get("message-id"), msg.msgid),
        "ts": msg.internaldate_ms if msg.internaldate_ms is not None else 0,
        "is_me": mine,
        "kind": "text",
        "text": part.text,
        "mentions_me": bool(part.mentions_me),
    }
    if part.actor:
        event["sender"] = {
            "ext_id": f"{part.tracker}:{part.actor.casefold()}",
            "name": part.actor,
            "is_me": False,
        }
    elif from_addr:
        event["sender"] = {
            "ext_id": from_addr,
            "name": from_name or from_addr,
            "handle": from_addr,
            "is_me": mine,
        }
    if part.attachments:
        event["attachments"] = part.attachments
    if part.refs:
        event["refs"] = part.refs
    return event


def stub_event(msg: FetchedMsg, *, me: MeInfo, channel: dict) -> dict:
    """М14: письмо не превратилось в событие — заглушка: отправитель из сырого `From`, текст «[письмо не
    разобрано]». Только самые простые шаги (сырые заголовки), без разбора тела."""
    headers = raw_headers(msg.body)
    raw_from = (headers.get("from") or "").strip()
    addr = raw_from.rsplit("<", 1)[-1].rstrip(">").strip().lower() if raw_from else ""
    mine = "\\Sent" in msg.labels or addr in me.all
    event: dict = {
        "type": "upsert",
        "channel": channel,
        "ext_id": message_ext_id(headers.get("message-id"), msg.msgid),
        "ts": msg.internaldate_ms if msg.internaldate_ms is not None else 0,
        "is_me": mine,
        "kind": "text",
        "text": textproc.UNPARSED_TEXT,
        "mentions_me": False,
    }
    if addr:
        event["sender"] = {"ext_id": addr, "name": raw_from[:200], "handle": addr, "is_me": mine}
    return event


def thread_title(first_msg_raw: bytes | None) -> str:
    return normalize_subject(decoded_header(first_msg_raw, "Subject"))


# --- прочтение (C4) ----------------------------------------------------------------------------------------


def read_marker(thread_msgs: list[FetchedMsg], me: MeInfo) -> str | None:
    """C4: самое новое письмо треда, до которого ВСЕ входящие (не мои) имеют `\\Seen`. Черновики не в счёт.
    `None` — первое же входящее не прочитано (двигать нечего)."""
    ordered = sorted(
        (m for m in thread_msgs if not m.is_draft),
        key=lambda m: (m.internaldate_ms or 0, m.uid),
    )
    marker: str | None = None
    for m in ordered:
        from_list = addresses(m.header, "From")
        from_addr = from_list[0][1] if from_list else None
        if not is_mine(m, from_addr, me) and "\\Seen" not in m.flags:
            break
        marker = ext_id_from_headers(m.header, m.msgid)
    return marker


# --- ответ (E2) --------------------------------------------------------------------------------------------


@dataclass
class ReplyParent:
    ext_id: str
    subject: str | None
    references: list[str]
    from_addrs: list[str]
    to_addrs: list[str]
    reply_to_addrs: list[str]
    mine: bool


def reply_parent(msg: FetchedMsg, me: MeInfo) -> ReplyParent:
    headers = raw_headers(msg.header)
    from_addrs = [a for _n, a in addresses(msg.header, "From")]
    return ReplyParent(
        ext_id=message_ext_id(headers.get("message-id"), msg.msgid),
        subject=decoded_header(msg.header, "Subject"),
        references=msgid_list(headers.get("references")),
        from_addrs=from_addrs,
        to_addrs=[a for _n, a in addresses(msg.header, "To")],
        reply_to_addrs=[a for _n, a in addresses(msg.header, "Reply-To")],
        mine=is_mine(msg, from_addrs[0] if from_addrs else None, me),
    )


def reply_recipients(parent: ReplyParent) -> list[str]:
    """М14: ВСЕГДА `From` родителя (`Reply-To` не используем); родитель мой -> его `To`."""
    source = parent.to_addrs if parent.mine else parent.from_addrs[:1]
    out: list[str] = []
    for addr in source:
        if addr and addr not in out:
            out.append(addr)
    return out


our_message_id = msgid_mod.our_message_id  # E2/план 1.2 + Ф12-1 П4: одна функция с ядром


def build_reply(
    *,
    parent: ReplyParent,
    recipients: list[str],
    text: str,
    address: str,
    display_name: str,
    message_id: str,
    now_ts: float,
    attachments: list[tuple[Path, str, str]] | None = None,
) -> bytes:
    """Письмо-ответ: текст — `text/plain; charset=utf-8`, без HTML и без цитаты (E2); `attachments`
    (Ф11-3b B4) — вложения, как у `build_letter`."""
    references = [r for r in parent.references if r != parent.ext_id] + [parent.ext_id]
    return build_letter(
        recipients=recipients,
        subject=reply_subject(parent.subject),
        text=text,
        address=address,
        display_name=display_name,
        message_id=message_id,
        now_ts=now_ts,
        in_reply_to=parent.ext_id,
        references=references[-MAX_REFERENCES:],
        attachments=attachments,
    )


_DEFAULT_ATTACHMENT_MIME = ("application", "octet-stream")


def _mime_parts(mime: str) -> tuple[str, str]:
    """`type/subtype` вложения; пусто/кривое — `application/octet-stream`."""
    maintype, _, subtype = (mime or "").strip().lower().partition("/")
    token = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*$")
    if not token.match(maintype) or not token.match(subtype):
        return _DEFAULT_ATTACHMENT_MIME
    return maintype, subtype


def build_letter(
    *,
    recipients: list[str],
    subject: str,
    text: str,
    address: str,
    display_name: str,
    message_id: str,
    now_ts: float,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[tuple[Path, str, str]] | None = None,
) -> bytes:
    """Общая сборка письма (ответ E2 и новое письмо Ф12-1 B3): `text/plain; charset=utf-8`, без HTML; `From` —
    `address` с именем ящика; тема не-ASCII — RFC 2047 (`policy.SMTP`). Без `in_reply_to` — без
    `In-Reply-To`/`References` (новый тред).

    `attachments` (Ф11-3b B4) — `(путь, имя, mime)` в порядке задания: без них — прежнее письмо байт в байт;
    с ними — `multipart/mixed`: первая часть — текст (пустой текст — пустая часть), затем вложения
    (`Content-Disposition: attachment`, не-ASCII имя — RFC 2231). Порядок листьев совпадает с порядком `idx`
    приёма (`textproc.attachment_parts`). Байты читаются целиком (письмо строится в памяти)."""
    from datetime import datetime

    msg = EmailMessage(policy=policy.SMTP)
    local, _, domain = address.partition("@")
    msg["From"] = Address(display_name=display_name or "", username=local, domain=domain)
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to is not None:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg["Date"] = format_datetime(datetime.fromtimestamp(now_ts).astimezone())
    msg["MIME-Version"] = "1.0"
    msg.set_content(text, charset="utf-8", cte="quoted-printable")
    for path, name, mime in attachments or ():
        maintype, subtype = _mime_parts(mime)
        msg.add_attachment(Path(path).read_bytes(), maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes(policy=policy.SMTP)


# --- Ф15-1 C1/C2: реакции в письмах (Gmail и RFC 9078) ------------------------------------------------------

REACTION_MIME = "text/vnd.google.email-reaction+json"
# Ф15-1 ревью 2: рядом с частью реакции допустимы только её замены для чужих клиентов, текст — короткий.
_REACTION_FALLBACK_TYPES = frozenset({"text/plain", "text/html"})
_REACTION_FALLBACK_TEXT_MAX = 2000
REACTION_MAX_EMOJI_LEN = 64
GMAIL_REACTION_MAX_RECIPIENTS = 20  # Gmail: больше 20 разных адресатов в To+Cc — реакций нет


def _single_emoji(value: object) -> str | None:
    """Ровно один эмодзи (как `core/mail_reactions.valid_emoji`) или `None`."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > REACTION_MAX_EMOJI_LEN or not emoji_lib.is_emoji(value):
        return None
    return value


def _reaction_part_emoji(part: Any) -> str | None:
    """Эмодзи части письма-реакции или `None` (часть кривая — тогда письмо обычное)."""
    if part.get_content_type() == REACTION_MIME and part.get_content_disposition() != "attachment":
        try:
            payload = part.get_payload(decode=True) or b""
            data = json.loads(payload.decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        version = data.get("version")
        if type(version) is not int or version != 1:  # `True` и `"1"` — не версия 1
            return None
        return _single_emoji(data.get("emoji"))
    if part.get_content_disposition() == "reaction":  # RFC 9078
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8")
        except LookupError, UnicodeDecodeError:
            return None
        return _single_emoji(text)
    return None


def _is_reaction_part(part: Any) -> bool:
    return part.get_content_type() == REACTION_MIME or part.get_content_disposition() == "reaction"


def reaction_of(raw: bytes | None) -> tuple[str, str] | None:
    """Ф15-1 C1: письмо-реакция -> `(эмодзи, Message-ID цели)` или `None` (обычное письмо). Реакция — часть
    `text/vnd.google.email-reaction+json` (не вложение) с JSON `{"version": 1, "emoji": <один эмодзи>}` или
    часть `Content-Disposition: reaction` (RFC 9078) с одним эмодзи; `In-Reply-To` — ровно один Message-ID.
    Кривая часть реакции — обычное письмо (как у Gmail)."""
    if not raw:
        return None
    targets = msgid_list(raw_headers(raw).get("in-reply-to"))
    if len(targets) != 1:
        return None
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        reaction_parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            # Ф15-1 ревью 2: письмо-реакция состоит ТОЛЬКО из реакции и её короткой замены текстом/HTML —
            # вложенное письмо (`message/*`), вложение или длинный текст -> обычное письмо (иначе текст и
            # файлы письма пропали бы из ленты).
            if part.get_content_maintype() == "message":
                return None
            if part.is_multipart():
                continue
            if _is_reaction_part(part):
                reaction_parts.append(part)
                continue
            if ctype not in _REACTION_FALLBACK_TYPES or part.get_content_disposition() == "attachment":
                return None
            if ctype == "text/plain" and len(part.get_content()) > _REACTION_FALLBACK_TEXT_MAX:
                return None
        if len(reaction_parts) != 1:
            return None
        found = _reaction_part_emoji(reaction_parts[0])
        return (found, targets[0]) if found is not None else None
    except Exception:  # noqa: BLE001 — кривое письмо разбирается как обычное
        return None


def reaction_event(msg: FetchedMsg, *, me: MeInfo, channel: dict) -> dict | None:
    """Ф15-1 C1: событие `mail_reaction` письма-реакции (отправитель и `is_me` — как у письма) или `None`."""
    raw = msg.body or b""
    found = reaction_of(raw)
    if found is None:
        return None
    emoji_value, target = found
    from_list = addresses(raw, "From")
    if not from_list:
        return None
    from_name, from_addr = from_list[0]
    src = message_ext_id(raw_headers(raw).get("message-id"), msg.msgid)
    if src == target:
        return None
    mine = is_mine(msg, from_addr, me)
    return {
        "type": "mail_reaction",
        "channel": channel,
        "target_ext_id": target,
        "src_ext_id": src,
        "emoji": emoji_value,
        "sender": {"ext_id": from_addr, "name": from_name or from_addr, "handle": from_addr, "is_me": mine},
        "ts": msg.internaldate_ms if msg.internaldate_ms is not None else 0,
    }


def letter_event(msg: FetchedMsg, *, me: MeInfo, channel: dict, thread_msgids: set[str]) -> dict:
    """Ф15-1 C1: письмо треда -> событие: письмо-реакция — `mail_reaction`, иначе `upsert`."""
    event = reaction_event(msg, me=me, channel=channel)
    if event is not None:
        return event
    return message_event(msg, me=me, channel=channel, thread_msgids=thread_msgids)


def reaction_blocker(parent: ReplyParent, header: bytes | None, me: MeInfo) -> str | None:
    """Ф15-1 решение 6: ограничения Gmail по заголовкам письма-цели — причина (без адресов) или `None`.
    `List-Id`; больше 20 разных адресатов в To+Cc; меня нет в To/Cc у чужого письма (BCC, рассылка)."""
    if raw_headers(header).get("list-id"):
        return "list"
    to_cc = {a for _n, a in addresses(header, "To")} | {a for _n, a in addresses(header, "Cc")}
    if len(to_cc) > GMAIL_REACTION_MAX_RECIPIENTS:
        return "too_many_recipients"
    if not parent.mine and not (to_cc & me.all):
        return "not_addressed"
    return None


def reaction_token(account_id: int, message_id: int, emoji_value: str) -> str:
    """Ф15-1 П2: токен Message-ID письма-реакции — от (ящик, сообщение хаба, ключ эмодзи без U+FE0F), не от
    `gen`: одна реакция этим эмодзи на письмо — одно письмо навсегда (повтор находит своё письмо)."""
    key = emoji_value.replace("️", "")
    blob = f"hub-reaction:{int(account_id)}:{int(message_id)}:{key}".encode()
    return hashlib.sha256(blob).hexdigest()[:32]


def build_reaction(
    *,
    parent: ReplyParent,
    recipients: list[str],
    emoji_value: str,
    address: str,
    display_name: str,
    message_id: str,
    now_ts: float,
) -> bytes:
    """Ф15-1 C2: письмо-реакция в формате Gmail — `multipart/alternative` из трёх частей: `text/plain`
    (эмодзи), часть реакции `{"emoji": X, "version": 1}` (UTF-8), `text/html` (`<p>X</p>`). `In-Reply-To` —
    один Message-ID цели, `References` — References цели + её Message-ID, тема `Re:`."""
    from datetime import datetime

    references = [r for r in parent.references if r != parent.ext_id] + [parent.ext_id]
    msg = EmailMessage(policy=policy.SMTP)
    local, _, domain = address.partition("@")
    msg["From"] = Address(display_name=display_name or "", username=local, domain=domain)
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = reply_subject(parent.subject)
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = parent.ext_id
    msg["References"] = " ".join(references[-MAX_REFERENCES:])
    msg["Date"] = format_datetime(datetime.fromtimestamp(now_ts).astimezone())
    msg["MIME-Version"] = "1.0"
    msg.set_content(emoji_value, charset="utf-8", cte="quoted-printable")
    body = json.dumps({"emoji": emoji_value, "version": 1}, ensure_ascii=False)
    qp = "quoted-printable"
    msg.add_alternative(body, subtype="vnd.google.email-reaction+json", charset="utf-8", cte=qp)
    msg.add_alternative(f"<p>{emoji_value}</p>", subtype="html", charset="utf-8", cte=qp)
    return msg.as_bytes(policy=policy.SMTP)
