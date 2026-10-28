"""Разбор ответов IMAP без сети: токенизатор ответов `imaplib` (с литералами), FETCH, LIST, SEARCH, modified
UTF-7 (имена папок и меток Gmail). Спецификация: `docs/architecture.md` и М14/М15.

Чистые функции — `gateway.py` зовёт их на данных, которые вернул `imaplib` (или подделка в тестах). `imaplib`
отдаёт ответ со строковыми литералами кусками: `(заголовок_до_литерала, байты_литерала)`, затем хвост
`bytes` (он может снова кончаться литералом) — токенизатор склеивает это в один поток.
"""

from __future__ import annotations

import base64
import calendar
import re
from dataclasses import dataclass, field

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_INTERNALDATE_RE = re.compile(
    r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})\s*", re.ASCII
)
_LITERAL_TAIL_RE = re.compile(rb"\{(\d+)\+?\}$")


class ImapParseError(ValueError):
    """Ответ сервера не разобран (не та форма). Текст ответа в сообщение не кладём — там бывают данные
    писем."""


# --- modified UTF-7 (RFC 3501, 5.1.3) ---------------------------------------------------------------------


def decode_mutf7(value: str) -> str:
    """`&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-` -> «Отправленные»; `&-` -> `&`. Кривая последовательность
    остаётся как есть (имя метки лучше показать сырым, чем потерять)."""
    if "&" not in value:
        return value
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = value.find("-", i + 1)
        if end == -1:
            out.append(value[i:])
            break
        chunk = value[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64, validate=True).decode("utf-16-be"))
            except ValueError, UnicodeDecodeError:
                out.append(value[i : end + 1])
        i = end + 1
    return "".join(out)


def encode_mutf7(value: str) -> str:
    """Обратное `decode_mutf7` (нужно подделке Gmail и тестам; коннектор имена меток не кодирует)."""
    out: list[str] = []
    buf: list[str] = []

    def _flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in value:
        if 0x20 <= ord(ch) <= 0x7E:
            _flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    _flush()
    return "".join(out)


# --- токенизатор -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Atom:
    value: str


@dataclass(frozen=True)
class Literal:
    """Строка ответа (quoted или literal) — байты как есть."""

    value: bytes


_NIL = object()


class _Stream:
    """Поток сегментов: текст ответа и литералы. Литерал встаёт в поток токеном ровно там, где текст кончился
    `{n}`."""

    def __init__(self, segments: list[bytes | tuple[bytes, bytes]]) -> None:
        self._parts: list[tuple[str, bytes]] = []
        for seg in segments:
            if isinstance(seg, tuple):
                head, lit = seg
                m = _LITERAL_TAIL_RE.search(head)
                if m is None:
                    raise ImapParseError("литерал без {n} в заголовке")
                self._parts.append(("text", head[: m.start()]))
                self._parts.append(("literal", lit))
            elif isinstance(seg, bytes | bytearray):
                self._parts.append(("text", bytes(seg)))
                # граница двух отдельных ответов = пробел (иначе соседние ответы склеились бы в один атом)
                self._parts.append(("text", b" "))
            elif seg is None:
                continue
            else:
                raise ImapParseError("неожиданный тип сегмента ответа")
        self._idx = 0
        self._pos = 0

    def _cur(self) -> tuple[str, bytes] | None:
        while self._idx < len(self._parts):
            kind, data = self._parts[self._idx]
            if kind == "literal":
                return kind, data
            if self._pos < len(data):
                return kind, data
            self._idx += 1
            self._pos = 0
        return None

    def skip_ws(self) -> None:
        while True:
            cur = self._cur()
            if cur is None or cur[0] != "text":
                return
            data = cur[1]
            while self._pos < len(data) and data[self._pos : self._pos + 1] in (b" ", b"\r", b"\n", b"\t"):
                self._pos += 1
            if self._pos < len(data):
                return

    def at_end(self) -> bool:
        self.skip_ws()
        return self._cur() is None

    def next_token(self):
        """Токен: '(' | ')' | Atom | Literal | _NIL. Конец потока -> None."""
        self.skip_ws()
        cur = self._cur()
        if cur is None:
            return None
        kind, data = cur
        if kind == "literal":
            self._idx += 1
            self._pos = 0
            return Literal(data)
        ch = data[self._pos : self._pos + 1]
        if ch in (b"(", b")"):
            self._pos += 1
            return ch.decode()
        if ch == b'"':
            return Literal(self._read_quoted(data))
        return self._read_atom(data)

    def _read_quoted(self, data: bytes) -> bytes:
        self._pos += 1  # открывающая кавычка
        out = bytearray()
        while self._pos < len(data):
            c = data[self._pos]
            if c == 0x5C and self._pos + 1 < len(data):  # обратный слеш
                out.append(data[self._pos + 1])
                self._pos += 2
                continue
            if c == 0x22:
                self._pos += 1
                return bytes(out)
            out.append(c)
            self._pos += 1
        raise ImapParseError("незакрытая строка в ответе")

    def _read_atom(self, data: bytes):
        start = self._pos
        depth = 0
        while self._pos < len(data):
            c = data[self._pos : self._pos + 1]
            if c == b"[":
                depth += 1
            elif c == b"]":
                depth = max(0, depth - 1)
            elif depth == 0 and c in (b" ", b"(", b")", b"\r", b"\n"):
                break
            self._pos += 1
        value = data[start : self._pos].decode("utf-8", errors="replace")
        # `BODY[]<0>`: `<…>` — часть атома, уже прочитано вместе с ним (угловые скобки не разделители).
        if value.upper() == "NIL":
            return _NIL
        return Atom(value)


def _read_value(stream: _Stream):
    tok = stream.next_token()
    if tok is None:
        raise ImapParseError("ответ оборвался")
    if tok == "(":
        return _read_list_body(stream)
    if tok == ")":
        raise ImapParseError("лишняя закрывающая скобка")
    return tok


def _read_list_body(stream: _Stream) -> list:
    items = []
    while True:
        tok = stream.next_token()
        if tok is None:
            raise ImapParseError("незакрытый список")
        if tok == ")":
            return items
        if tok == "(":
            items.append(_read_list_body(stream))
        else:
            items.append(tok)


def parse_values(segments: list) -> list:
    """Весь поток -> список значений верхнего уровня (атомы, строки, списки, None для NIL)."""
    stream = _Stream(segments)
    out = []
    while not stream.at_end():
        out.append(_unwrap(_read_value(stream)))
    return out


def _unwrap(value):
    if value is _NIL:
        return None
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def _as_text(value) -> str:
    if isinstance(value, Atom):
        return value.value
    if isinstance(value, Literal):
        return value.value.decode("utf-8", errors="replace")
    if value is None:
        return ""
    raise ImapParseError("ожидалась строка")


def _as_int(value) -> int:
    try:
        return int(_as_text(value))
    except ValueError as exc:
        raise ImapParseError("ожидалось число") from exc


# --- FETCH -------------------------------------------------------------------------------------------------


@dataclass
class FetchedMsg:
    uid: int
    seq: int | None = None
    msgid: int | None = None
    thrid: int | None = None
    labels: list[str] = field(default_factory=list)  # modified UTF-7 уже раскодирован; системные — `\Inbox`
    flags: list[str] = field(default_factory=list)
    internaldate_ms: int | None = None
    size: int | None = None
    modseq: int | None = None
    body: bytes | None = None  # BODY[] (или его начало BODY[]<0>)
    body_partial: bool = False
    header: bytes | None = None  # BODY[HEADER…]

    @property
    def is_draft(self) -> bool:
        return "\\Draft" in self.flags or "\\Draft" in self.labels


def parse_internaldate(value: str) -> int:
    """`"24-Sep-2026 10:00:00 +0300"` -> миллисекунды UTC. Месяц — по таблице, не через локаль."""
    m = _INTERNALDATE_RE.fullmatch(value)
    if m is None:
        raise ImapParseError("INTERNALDATE неизвестного вида")
    day, mon, year, hh, mm, ss, sign, tzh, tzm = m.groups()
    month = _MONTHS.get(mon.upper())
    if month is None:
        raise ImapParseError("INTERNALDATE: месяц")
    epoch = calendar.timegm((int(year), month, int(day), int(hh), int(mm), int(ss), 0, 0, 0))
    offset = (int(tzh) * 3600 + int(tzm) * 60) * (1 if sign == "+" else -1)
    return (epoch - offset) * 1000


def parse_fetch(data: list) -> list[FetchedMsg]:
    """Ответ `uid('FETCH', …)` -> список писем. Ответы без `UID` (чужие нетегированные FETCH — флаги,
    пришедшие между командами) отбрасываются."""
    if not data or data == [None]:
        return []
    values = parse_values(data)
    out: list[FetchedMsg] = []
    i = 0
    while i < len(values):
        seq_v = values[i]
        if i + 1 >= len(values) or not isinstance(values[i + 1], list):
            raise ImapParseError("FETCH: ожидался список атрибутов")
        attrs = values[i + 1]
        i += 2
        msg = _parse_fetch_attrs(attrs)
        if msg is None:
            continue
        try:
            msg.seq = _as_int(seq_v)
        except ImapParseError:
            msg.seq = None
        out.append(msg)
    return out


def _parse_fetch_attrs(attrs: list) -> FetchedMsg | None:
    fields: dict[str, object] = {}
    j = 0
    while j + 1 < len(attrs):
        key = attrs[j]
        if not isinstance(key, Atom):
            raise ImapParseError("FETCH: имя атрибута не атом")
        fields[key.value.upper()] = attrs[j + 1]
        j += 2
    if "UID" not in fields:
        return None
    msg = FetchedMsg(uid=_as_int(fields["UID"]))
    for key, value in fields.items():
        if key == "X-GM-MSGID":
            msg.msgid = _as_int(value)
        elif key == "X-GM-THRID":
            msg.thrid = _as_int(value)
        elif key == "X-GM-LABELS":
            msg.labels = [decode_mutf7(_as_text(v)) for v in (value or [])]
        elif key == "FLAGS":
            msg.flags = [_as_text(v) for v in (value or [])]
        elif key == "INTERNALDATE":
            msg.internaldate_ms = parse_internaldate(_as_text(value))
        elif key == "RFC822.SIZE":
            msg.size = _as_int(value)
        elif key == "MODSEQ":
            msg.modseq = _as_int(value[0]) if isinstance(value, list) and value else _as_int(value)
        elif key.startswith("BODY[HEADER"):
            msg.header = value.value if isinstance(value, Literal) else None
        elif key.startswith("BODY[]"):
            msg.body = value.value if isinstance(value, Literal) else None
            msg.body_partial = key != "BODY[]" and key.startswith("BODY[]<")
    return msg


# --- LIST / SEARCH / числа ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListEntry:
    flags: tuple[str, ...]
    delimiter: str | None
    name: str  # как пришло с сервера (modified UTF-7) — его и отдавать в команды

    @property
    def display_name(self) -> str:
        return decode_mutf7(self.name)


def parse_list(data: list) -> list[ListEntry]:
    """Ответ `imap.list()`: `(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"` на каждой строке."""
    if not data or data == [None]:
        return []
    out: list[ListEntry] = []
    for item in data:
        if item is None:
            continue
        values = parse_values([item])
        if len(values) < 3 or not isinstance(values[0], list):
            raise ImapParseError("LIST: неизвестная форма")
        flags = tuple(_as_text(f) for f in values[0])
        delim = None if values[1] is None else _as_text(values[1])
        out.append(ListEntry(flags=flags, delimiter=delim, name=_as_text(values[2])))
    return out


def parse_search(data: list) -> list[int]:
    """Ответ `uid('SEARCH', …)`: `[b'1 2 3']` (или `[None]`/`[b'']`) -> список UID."""
    out: list[int] = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            raise ImapParseError("SEARCH: неожиданный литерал")
        for part in bytes(item).split():
            if part.isdigit():
                out.append(int(part))
    return out


def last_int(untagged: dict, key: str) -> int | None:
    """Последнее значение нетегированного ответа `key` (UIDVALIDITY/UIDNEXT/HIGHESTMODSEQ/EXISTS) числом."""
    values = untagged.get(key) or []
    for raw in reversed(values):
        if raw is None:
            continue
        text = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
        head = text.strip().split(" ", 1)[0]
        if head.isdigit():
            return int(head)
    return None


def quote(value: str) -> str:
    """Строка IMAP в кавычках (как `imaplib._quote`): `\\` и `"` экранируются."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uid_set(uids: list[int]) -> str:
    """UID -> компактный набор `1:3,7,9:10`."""
    nums = sorted(set(uids))
    if not nums:
        raise ValueError("пустой набор UID")
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}:{prev}" if start != prev else str(start))
        start = prev = n
    parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)
