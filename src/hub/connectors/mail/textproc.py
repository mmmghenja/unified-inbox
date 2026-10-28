"""Текст письма: разбор MIME, HTML -> текст, отрезание цитаты. Спецификация: `docs/architecture.md` (D1–D3) и М14
(пересылка не режется, блок Outlook — только после текста ответа).

Без новых зависимостей: `email` (`policy.default`) и свой конвертер на `html.parser`. HTML в БД не пишется
(план 4.3) — наружу уходит только текст. Чистые функции, без сети и без журнала с текстом писем.
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from html.parser import HTMLParser

from ...core.media import mail_attachment_kind

MAX_TEXT_LEN = 20_000
TRUNCATED_MARK = "…[обрезано]"
BIG_MESSAGE_NOTE = "[письмо большое, показаны не все вложения]"
UNPARSED_TEXT = "[письмо не разобрано]"

# --- HTML -> текст (D1) -----------------------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "li", "ul", "ol", "tr", "table", "tbody", "thead", "tfoot", "h1", "h2", "h3", "h4",
        "h5", "h6", "blockquote", "pre", "section", "article", "header", "footer", "hr", "dl", "dt", "dd",
        "center", "address", "form", "fieldset", "figure", "figcaption", "main", "nav", "aside",
    }
)  # fmt: skip
_CELL_TAGS = frozenset({"td", "th"})
_SPACE_RE = re.compile(r"[ \t\r\f\v ]+")


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._in_link = False

    # вывод --------------------------------------------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self._in_link:
            self._link_text.append(text)
        else:
            self._out.append(text)

    def _newline(self) -> None:
        if self._in_link:
            self._link_text.append(" ")
        else:
            self._out.append("\n")

    # события парсера ------------------------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._newline()
        elif tag in _BLOCK_TAGS:
            self._newline()
            if tag == "pre":
                self._pre_depth += 1
        elif tag in _CELL_TAGS:
            self._emit(" ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._link_href = href.strip()
            self._link_text = []
            self._in_link = True

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip_depth:
            return
        if tag == "br":
            self._newline()
        elif tag in _BLOCK_TAGS:
            self._newline()
        # img и прочие одиночные теги — ничего

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._in_link:
            self._in_link = False
            text = _SPACE_RE.sub(" ", "".join(self._link_text)).strip()
            href = self._link_href or ""
            shown = text
            if href.lower().startswith(("http://", "https://")):
                if not text:
                    shown = href
                elif href.rstrip("/") != text.rstrip("/"):
                    shown = f"{text} ({href})"
            self._out.append(shown)
            self._link_href = None
            self._link_text = []
        elif tag in _BLOCK_TAGS:
            if tag == "pre":
                self._pre_depth = max(0, self._pre_depth - 1)
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._pre_depth:
            self._emit(data)
        else:
            self._emit(_SPACE_RE.sub(" ", data.replace("\n", " ")))

    def text(self) -> str:
        if self._in_link:
            self.handle_endtag("a")
        return _tidy("".join(self._out))


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError, ValueError:
        pass
    return parser.text()


def _tidy(text: str) -> str:
    """Пробелы по краям строк прочь, подряд идущие пустые строки — в одну, пустые края — прочь."""
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


# --- отрезание цитаты (D2 + М14) -------------------------------------------------------------------------

_FORWARD_RE = re.compile(
    r"^\s*(-{2,}\s*(forwarded message|пересылаемое сообщение|пересланное сообщение)\s*-{2,}"
    r"|begin forwarded message:|-{2,}\s*original message\s*-{2,}\s*forwarded)",
    re.IGNORECASE,
)
_ORIGINAL_SEP_RE = re.compile(r"^\s*-{2,}\s*(original message|исходное сообщение)\s*-{2,}\s*$", re.IGNORECASE)
_UNDERSCORES_RE = re.compile(r"^\s*_{8,}\s*$")
_HEADER_END_RE = re.compile(
    r"(\bwrote:|написал\(а\):|написала:|написал:|пишет:|>:|\)\s*:)\s*$", re.IGNORECASE
)
_HEADER_HINT_RE = re.compile(r"(\d{1,2}:\d{2}|@)")
# mail.ru (живая почта 24.09): «вторник, 22 сентября 2026г., 20:33 +03:00 от Имя a@b.ru :» — адрес без скобок,
# двоеточие через пробел. Узко: время, затем «от»/«from», затем адрес прямо перед двоеточием.
_MAILRU_HEADER_RE = re.compile(r"\d{1,2}:\d{2}.*\b(от|from)\s.*@[\w.-]+\.\w{2,}\s*:\s*$", re.IGNORECASE)
_OUTLOOK_FROM_RE = re.compile(r"^\s*\*?(from|от)\s*:\*?\s*\S", re.IGNORECASE)
_OUTLOOK_SENT_RE = re.compile(r"^\s*\*?(sent|date|отправлено|дата)\s*:\*?", re.IGNORECASE)
_OUTLOOK_TO_RE = re.compile(r"^\s*\*?(to|кому|subject|тема)\s*:\*?", re.IGNORECASE)


def _is_reply_header(line: str) -> bool:
    """Заголовок цитаты: `On … wrote:`, русский Gmail `пн, 23 сент. 2026 г. в 10:00, Имя <a@b>:`, Apple
    `… написал(а):` — строка кончается словом «написал»/«wrote»/`>:` и в ней есть время или адрес."""
    stripped = line.strip()
    if not stripped or stripped.startswith(">"):
        return False
    if _MAILRU_HEADER_RE.search(stripped):
        return True
    if not _HEADER_END_RE.search(stripped):
        return False
    return bool(_HEADER_HINT_RE.search(stripped))


def _has_content(lines: list[str]) -> bool:
    return any(line.strip() for line in lines)


def strip_quote(text: str) -> str:
    """Отрезать цитату перед первой строкой-заголовком цитаты, если после неё остаётся цитата. Пересланное
    письмо не режется (пересланное и есть содержание). Пустой результат -> исходный текст."""
    if not text:
        return text
    lines = text.split("\n")
    forward_at = next((i for i, line in enumerate(lines) if _FORWARD_RE.match(line)), len(lines))
    cut: int | None = None
    i = 0
    while i < forward_at:
        line = lines[i]
        before_nonempty = _has_content(lines[:i])
        # (а) заголовок «On … wrote:» — бывает перенесён на две строки
        if _is_reply_header(line) and _has_content(lines[i + 1 :]):
            cut = i
            break
        if (
            i + 1 < forward_at
            and not line.strip().startswith(">")
            and line.strip()
            and _is_reply_header(line.rstrip() + " " + lines[i + 1].strip())
            and re.match(r"^\s*(on\s|\S.*\d{4})", line, re.IGNORECASE)
            and _has_content(lines[i + 2 :])
        ):
            cut = i
            break
        # (б) Outlook: разделитель «Original Message»/«Исходное сообщение»
        if _ORIGINAL_SEP_RE.match(line) and _has_content(lines[i + 1 :]):
            cut = i
            break
        # (в) Outlook: блок From:/Sent:/To:/Subject: — только после непустого текста ответа
        if before_nonempty and _OUTLOOK_FROM_RE.match(line):
            window = lines[i + 1 : i + 6]
            has_sent = any(_OUTLOOK_SENT_RE.match(w) for w in window)
            if has_sent and any(_OUTLOOK_TO_RE.match(w) for w in window):
                cut = i
                if i > 0 and _UNDERSCORES_RE.match(lines[i - 1]):
                    cut = i - 1
                break
        # (г) хвост из строк «>»: всё до конца (кроме пустых) — цитата
        if line.lstrip().startswith(">"):
            rest = [x for x in lines[i:forward_at] if x.strip()]
            if rest and all(x.lstrip().startswith(">") for x in rest) and forward_at == len(lines):
                cut = i
                break
        i += 1
    if cut is None:
        return text
    head = _tidy("\n".join(lines[:cut]))
    return head if head else text


# --- разбор MIME (D1) ------------------------------------------------------------------------------------


@dataclass
class BodyResult:
    text: str | None
    attachments: list[dict] = field(default_factory=list)
    unparsed: bool = False


def _decode_part(part: Message) -> str:
    try:
        content = part.get_content()
    except LookupError, UnicodeError, ValueError, AssertionError, KeyError:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content if isinstance(content, str) else ""


def _attachment_info(part: Message, payload: bytes | None = None) -> dict:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001 — кривое имя вложения не роняет письмо
        name = None
    mime = part.get_content_type()
    return {
        "type": mail_attachment_kind(mime),  # Ф11-3a A1: вид для загрузчика медиа ядра
        "name": str(name) if name else "без имени",
        "mime": mime,
        "size": len(attachment_bytes(part) if payload is None else payload),
    }


def attachment_bytes(part: Message) -> bytes:
    """Байты вложения: раскодированное тело части; вложенное письмо (`message/*`) — целиком `as_bytes()`."""
    payload = part.get_payload(decode=True)
    if payload is None and part.get_content_maintype() == "message":
        payload = part.as_bytes() if hasattr(part, "as_bytes") else b""
    return payload or b""


def parse_message(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


_strip_quote = strip_quote  # параметр `strip_quote` у `extract_body` заслоняет имя функции


def extract_body(raw: bytes, *, partial: bool = False, strip_quote: bool = True) -> BodyResult:
    """Текст (plain, если он есть и не пуст; иначе HTML -> текст) без цитаты и вложения
    `[{name, mime, size}]`. `partial` — у письма скачано только начало (письмо больше 5 МБ, М11).
    `strip_quote=False` — цитата не режется (Ф10-5 A1: уведомления трекеров — строка «…10:00 владелец написал:»
    в комментарии не должна отрезать остальные уведомления дайджеста)."""
    try:
        msg = parse_message(raw)
        text, body_parts = _pick_text(msg)
        attachments = _collect_attachments(msg, body_parts)
    except Exception:  # noqa: BLE001 — одно кривое письмо не роняет пачку (М14: заглушка)
        return BodyResult(text=UNPARSED_TEXT, unparsed=True)

    if text is not None:
        text = _tidy(text)
        if strip_quote:
            text = _strip_quote(text)
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN] + TRUNCATED_MARK
    if partial:
        text = f"{text}\n\n{BIG_MESSAGE_NOTE}" if text else BIG_MESSAGE_NOTE
    return BodyResult(text=text or None, attachments=attachments)


def extract_html(raw: bytes) -> str | None:
    """HTML-часть письма как строка (Ф11-9b: у письма Jira «обновления + комментарий» plain — только
    комментарий, остальное — в HTML). Нет HTML или письмо не разбирается -> `None`."""
    try:
        part = parse_message(raw).get_body(preferencelist=("html",))
        return _decode_part(part) if part is not None else None
    except Exception:  # noqa: BLE001 — кривое письмо: вызывающий идёт прежним путём
        return None


def _pick_text(msg: EmailMessage) -> tuple[str | None, set[int]]:
    body_parts: set[int] = set()
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        body_parts.add(id(plain))
        text = _decode_part(plain)
        if text.strip():
            # html-альтернатива той же части — тоже «тело», не вложение
            html_alt = msg.get_body(preferencelist=("html",))
            if html_alt is not None:
                body_parts.add(id(html_alt))
            return text, body_parts
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        body_parts.add(id(html))
        return html_to_text(_decode_part(html)), body_parts
    return None, body_parts


def _collect_attachments(msg: EmailMessage, body_parts: set[int]) -> list[dict]:
    return [_attachment_info(part) for part in attachment_parts(msg, body_parts)]


def attachment_parts(msg: EmailMessage, body_parts: set[int]) -> list[Message]:
    """Части-вложения письма в порядке номеров `idx` (Ф11-3a A3: ОДИН обход на конвертер и загрузчик —
    номер вложения в событии и номер части при скачивании обязаны совпасть). Тело письма — не вложение."""
    out: list[Message] = []
    if not msg.is_multipart():
        if id(msg) in body_parts:
            return out
        if msg.get_content_maintype() != "text" or msg.get_content_disposition() == "attachment":
            out.append(msg)
        return out
    for part in _iter_leaves(msg):
        if id(part) in body_parts:
            continue
        disposition = part.get_content_disposition()
        maintype = part.get_content_maintype()
        subtype = part.get_content_subtype()
        if disposition == "attachment":
            out.append(part)
        elif maintype == "text" and subtype in ("plain", "html"):
            continue  # альтернативы/лишние текстовые части — не вложения
        else:
            out.append(part)  # inline-картинки, календарь, message/rfc822 — непоказываемое
    return out


def find_attachment(raw: bytes, idx: int) -> tuple[dict, bytes] | None:
    """Ф11-3a A3: вложение номер `idx` письма `raw` (тот же разбор и обход, что `extract_body`) ->
    (`{type, name, mime, size}`, байты). Нет такого номера -> `None`."""
    msg = parse_message(raw)
    _text, body_parts = _pick_text(msg)
    parts = attachment_parts(msg, body_parts)
    if not 0 <= idx < len(parts):
        return None
    data = attachment_bytes(parts[idx])
    return _attachment_info(parts[idx], data), data


def _iter_leaves(part: Message):
    """Листья дерева MIME; вложенное письмо (`message/*`) — один лист, внутрь не заходим."""
    if part.get_content_maintype() == "message" or not part.is_multipart():
        yield part
        return
    for sub in part.get_payload():
        yield from _iter_leaves(sub)
