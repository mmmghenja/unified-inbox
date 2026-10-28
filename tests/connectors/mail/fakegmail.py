"""`FakeGmail` — подделка Gmail на уровне СОЕДИНЕНИЯ `imaplib` (и `aiosmtplib.SMTP`): настоящий `GmailGateway`
строит те же команды и разбирает ответы той же формы, что отдаёт `imaplib` (кортежи с литералами) — весь код
шлюза (команды, `PEEK`, разбор FETCH/LIST/SEARCH) идёт под тестом. Сети нет вообще.

Понимает ровно тот набор команд, который шлёт шлюз (`LOGIN`, `CAPABILITY`, `LIST`, `EXAMINE`, `UID SEARCH`
с `UID`/`X-GM-THRID`/`X-GM-RAW`, `UID FETCH` с `CHANGEDSINCE`, `IDLE`; Ф8-7 — `SELECT` и ТОЛЬКО
`UID STORE <uid> +FLAGS.SILENT (\\Seen)`, и только в `SELECT`, иначе `NO`); незнакомое -> `AssertionError`
(тест сразу видит, что шлюз прислал что-то новое). `no_once` — ответить `NO` на команду один раз (Ф8-7).
`BODY[` без `PEEK` честно ставит `\\Seen` (как настоящий сервер). Все команды пишутся в `commands` — по
ним тест 4 проверяет `PEEK` и отсутствие `STORE`.
"""

from __future__ import annotations

import email
import imaplib
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.utils import format_datetime, make_msgid, parseaddr

import aiosmtplib

from hub.connectors.mail.imapwire import encode_mutf7, quote

ALL_BOX = "[Gmail]/&BBIEQQRP- &BD8EPgRHBEIEMA-"  # «Вся почта» в modified UTF-7
SENT_BOX = "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_SYSTEM = {"\\Inbox", "\\Sent", "\\Draft", "\\Important", "\\Starred", "\\Trash", "\\Spam"}

BASE_TS = 1_790_000_000  # ~сентябрь 2026, секунды


def make_email(
    *,
    msgid: str | None,
    from_: str = "Пётр Петров <petr@example.com>",
    to: str = "me@gmail.com",
    cc: str | None = None,
    subject: str = "Проект",
    body: str = "Привет!",
    in_reply_to: str | None = None,
    references: str | None = None,
    reply_to: str | None = None,
    date_ts: int = BASE_TS,
    html: str | None = None,
) -> bytes:
    msg = email.message.EmailMessage(policy=policy.SMTP)
    msg["From"] = from_
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    if msgid is not None:
        msg["Message-ID"] = msgid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg["Date"] = format_datetime(datetime.fromtimestamp(date_ts, tz=UTC))
    if html is None:
        msg.set_content(body)
    else:
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


@dataclass
class FakeMsg:
    uid: int
    msgid: int
    thrid: int
    raw: bytes
    labels: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    date_ts: int = BASE_TS
    categories: set[str] = field(default_factory=set)
    modseq: int = 1

    @property
    def message_id(self) -> str | None:
        m = email.message_from_bytes(self.raw, policy=policy.compat32)
        v = m.get("Message-ID")
        return "".join(str(v).split()) if v else None


class _FakeSock:
    def __init__(self, conn: FakeImapConn) -> None:
        self._conn = conn

    def shutdown(self, _how: int) -> None:
        self._conn.killed = True


class FakeIdler:
    def __init__(self, conn: FakeImapConn, duration: float | None) -> None:
        self._conn = conn
        self._duration = duration

    def __enter__(self) -> FakeIdler:
        gm = self._conn.gm
        gm.log(self._conn, "IDLE")
        gm.idle_started.set()
        self._conn.check_alive()
        if gm.idle_start_errors:
            raise gm.idle_start_errors.pop(0)
        self._conn.idling = True
        return self

    def __exit__(self, *exc) -> bool:
        self._conn.idling = False
        self._conn.gm.log(self._conn, "DONE")
        return False

    def __iter__(self) -> FakeIdler:
        return self

    def __next__(self):
        gm = self._conn.gm
        deadline = time.monotonic() + gm.idle_real_max_s
        while True:
            self._conn.check_alive()
            try:
                item = gm.idle_queue.get(timeout=gm.idle_poll_s)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    if gm.on_idle_timeout is not None and self._duration is not None:
                        gm.idle_durations.append(self._duration)
                        gm.on_idle_timeout(self._duration)
                    raise StopIteration from None
                continue
            if isinstance(item, BaseException):
                raise item
            return item


class FakeImapConn:
    """Подмена объекта `imaplib.IMAP4_SSL` — тот же набор методов/атрибутов, которым пользуется шлюз."""

    def __init__(self, gm: FakeGmail) -> None:
        self.gm = gm
        self.untagged_responses: dict = {}
        self.state = "NONAUTH"
        self.is_readonly = False
        self.killed = False
        self.closed = False
        self.idling = False
        self.read_write = False  # Ф8-7: ящик открыт `SELECT` (иначе `EXAMINE` — STORE отвергается)
        self.name = f"conn{len(gm.connections) + 1}"

    def check_alive(self) -> None:
        if self.killed or self.closed:
            raise imaplib.IMAP4.abort("socket error: EOF")

    # --- команды ---------------------------------------------------------------------------------------
    def login(self, user: str, password: str):
        self.gm.log(self, f"LOGIN {user} ***")
        self.check_alive()
        if self.gm.login_error is not None:
            raise imaplib.IMAP4.error(self.gm.login_error)
        if user != self.gm.address or password != self.gm.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.state = "AUTH"
        return "OK", [b"me authenticated (Success)"]

    def capability(self):
        self.gm.log(self, "CAPABILITY")
        self.check_alive()
        return "OK", [" ".join(self.gm.capabilities).encode()]

    def list(self, directory: str = '""', pattern: str = "*"):  # noqa: A003
        self.gm.log(self, "LIST")
        self.check_alive()
        return "OK", list(self.gm.list_lines)

    def _simple_command(self, name: str, *args: str):
        self.gm.log(self, " ".join([name, *args]))
        self.check_alive()
        if name not in ("EXAMINE", "SELECT"):
            raise AssertionError(f"FakeGmail: неожиданная команда {name}")
        gm = self.gm
        if gm.take_no(name):
            return "NO", [f"[FAKE] {name} failed".encode()]
        self.read_write = name == "SELECT"
        with gm.lock:
            self.untagged_responses = {
                "EXISTS": [str(len(gm.messages)).encode()],
                "UIDVALIDITY": [str(gm.uidvalidity).encode()],
                "UIDNEXT": [str(gm.next_uid).encode()],
            }
            if "(CONDSTORE)" in args and gm.condstore:
                self.untagged_responses["HIGHESTMODSEQ"] = [str(gm.modseq).encode()]
        return "OK", [b"[READ-WRITE] selected" if self.read_write else b"[READ-ONLY] examined"]

    def uid(self, command: str, *args: str):
        self.gm.log(self, " ".join(["UID", command, *args]))
        self.check_alive()
        if self.state != "SELECTED":
            raise imaplib.IMAP4.error(f"command UID illegal in state {self.state}")
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in self.gm.search(list(args))).encode()]
        if command == "FETCH":
            # imaplib 3.14.8: у UID FETCH ровно два аргумента, модификатор — хвостом второго.
            assert len(args) == 2, args
            m = re.fullmatch(r"(\(.*?\))(?: (\(CHANGEDSINCE \d+\)))?", args[1])
            assert m, args[1]
            if self.gm.fetch_gate is not None and "BODY.PEEK[]" in m.group(1):
                while not self.gm.fetch_gate.wait(0.02):
                    self.check_alive()
            return "OK", self.gm.fetch(args[0], m.group(1), m.group(2))
        if command == "STORE":
            # Ф8-7: единственная допустимая форма — добавить `\Seen` без ответа FETCH
            if len(args) != 3 or args[1] != "+FLAGS.SILENT" or args[2] != "(\\Seen)":
                raise AssertionError(f"FakeGmail: недопустимый STORE {args!r}")
            if self.gm.take_no("STORE"):
                return "NO", [b"[FAKE] STORE failed"]
            if not self.read_write:
                return "NO", [b"STORE attempt on READ-ONLY folder (Failure)"]
            self.gm.store_seen(args[0])
            return "OK", [b"Success"]
        raise AssertionError(f"FakeGmail: неожиданная команда UID {command}")

    def idle(self, duration: float | None = None) -> FakeIdler:
        return FakeIdler(self, duration)

    def socket(self) -> _FakeSock:
        return _FakeSock(self)

    def shutdown(self) -> None:
        self.closed = True


# --- поиск и выдача -----------------------------------------------------------------------------------------


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _in_set(uid: int, spec: str, max_uid: int) -> bool:
    for part in spec.split(","):
        if ":" in part:
            a, b = part.split(":")
            lo = max_uid if a == "*" else int(a)
            hi = max_uid if b == "*" else int(b)
            lo, hi = min(lo, hi), max(lo, hi)
            if lo <= uid <= hi:
                return True
        elif (max_uid if part == "*" else int(part)) == uid:
            return True
    return False


def _split_items(items: str) -> list[str]:
    body = items.strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    out: list[str] = []
    cur = ""
    depth = 0
    for ch in body:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == " " and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _internaldate(ts: int) -> str:
    d = datetime.fromtimestamp(ts, tz=UTC)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year} {d.hour:02d}:{d.minute:02d}:{d.second:02d} +0000"


def _label_atom(label: str) -> str:
    if label in _SYSTEM:
        return quote(label)
    return quote(encode_mutf7(label))


def _header_fields(raw: bytes, names: list[str]) -> bytes:
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    out = b""
    wanted = {n.lower() for n in names}
    for k, v in msg.items():
        if k.lower() in wanted:
            out += f"{k}: {v}\r\n".encode("utf-8", errors="replace")
    return out + b"\r\n"


class FakeGmail:
    def __init__(
        self,
        *,
        address: str = "me@gmail.com",
        password: str = "abcd efgh ijkl mnop".replace(" ", ""),
        condstore: bool = True,
        uidvalidity: int = 7,
    ) -> None:
        self.address = address
        self.password = password
        self.condstore = condstore
        self.uidvalidity = uidvalidity
        self.lock = threading.RLock()
        self.messages: list[FakeMsg] = []
        self.next_uid = 1
        self.next_msgid = 5_000_000_000
        self.next_thrid = 9_000_000_000
        self.modseq = 100
        self.now_ts = BASE_TS + 3600
        self.capabilities = ["IMAP4rev1", "IDLE", "X-GM-EXT-1", "UIDPLUS", "ENABLE", "MOVE"]
        if condstore:
            self.capabilities.append("CONDSTORE")
        self.list_lines: list[bytes] = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
            f'(\\All \\HasNoChildren) "/" "{ALL_BOX}"'.encode(),
            f'(\\HasNoChildren \\Sent) "/" "{SENT_BOX}"'.encode(),
            b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"',
        ]
        self.commands: list[tuple[str, str]] = []
        self.connections: list[FakeImapConn] = []
        self.login_error: bytes | None = None
        self.connect_errors: list[BaseException] = []
        self.idle_queue: queue.Queue = queue.Queue()
        self.idle_start_errors: list[BaseException] = []
        self.idle_started = threading.Event()
        self.idle_poll_s = 0.01
        self.idle_real_max_s = 0.3
        self.idle_durations: list[float] = []
        self.on_idle_timeout: Callable[[float], None] | None = None
        self.fetch_gate: threading.Event | None = None
        self.no_once: list[str] = []  # Ф8-7: имена команд (`SELECT`/`EXAMINE`/`STORE`) — один `NO` на каждое
        # --- SMTP ---
        self.smtp_log: list[tuple] = []
        self.smtp_faults: dict[str, BaseException] = {}
        self.smtp_deliver_then_fail: BaseException | None = None
        self.smtp_extensions: dict[str, str] = {}  # Ф11-3b: `EHLO` расширения (`{"size": "1000"}`)
        self.smtp_data_timeouts: list = []  # Ф11-3b П2: `timeout` каждого `data()`
        self.sent: list[bytes] = []
        self.sent_copy_mode = "same_thread"  # | "none" | "other_thread"

    # --- наполнение ---------------------------------------------------------------------------------------
    def add(
        self,
        raw: bytes,
        *,
        thrid: int | None = None,
        labels: tuple[str, ...] | set[str] = ("\\Inbox",),
        flags: tuple[str, ...] | set[str] = (),
        date_ts: int | None = None,
        categories: tuple[str, ...] = (),
    ) -> FakeMsg:
        with self.lock:
            if thrid is None:
                thrid = self.next_thrid
                self.next_thrid += 1
            self.modseq += 1
            m = FakeMsg(
                uid=self.next_uid,
                msgid=self.next_msgid,
                thrid=thrid,
                raw=raw,
                labels=set(labels),
                flags=set(flags),
                date_ts=date_ts if date_ts is not None else BASE_TS + self.next_uid * 60,
                categories=set(categories),
                modseq=self.modseq,
            )
            self.next_uid += 1
            self.next_msgid += 1
            self.messages.append(m)
            return m

    def set_flags(self, uid: int, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        with self.lock:
            m = self.by_uid(uid)
            m.flags |= set(add)
            m.flags -= set(remove)
            self.modseq += 1
            m.modseq = self.modseq

    def take_no(self, name: str) -> bool:
        with self.lock:
            if name in self.no_once:
                self.no_once.remove(name)
                return True
            return False

    def store_seen(self, uidset: str) -> None:
        with self.lock:
            max_uid = max((m.uid for m in self.messages), default=0)
            for m in self.messages:
                if _in_set(m.uid, uidset, max_uid) and "\\Seen" not in m.flags:
                    m.flags.add("\\Seen")
                    self.modseq += 1
                    m.modseq = self.modseq

    def by_uid(self, uid: int) -> FakeMsg:
        return next(m for m in self.messages if m.uid == uid)

    def by_message_id(self, mid: str) -> list[FakeMsg]:
        return [m for m in self.messages if m.message_id == mid]

    def log(self, conn: FakeImapConn, cmd: str) -> None:
        with self.lock:
            self.commands.append((conn.name, cmd))

    def command_texts(self) -> list[str]:
        with self.lock:
            return [c for _n, c in self.commands]

    # --- фабрики для шлюза -----------------------------------------------------------------------------
    def imap_factory(self, timeout: float) -> FakeImapConn:
        assert timeout == 60.0  # М2: таймаут сокета 60 с
        with self.lock:
            if self.connect_errors:
                raise self.connect_errors.pop(0)
            conn = FakeImapConn(self)
            self.connections.append(conn)
            return conn

    def smtp_factory(self, **kwargs) -> FakeSmtp:
        return FakeSmtp(self, kwargs)

    # --- поиск ---------------------------------------------------------------------------------------------
    def _raw_match(self, m: FakeMsg, query: str) -> bool:
        tokens = query.split()
        groups: list[list[str]] = []
        i = 0
        while i < len(tokens):
            group = [tokens[i]]
            while i + 2 < len(tokens) and tokens[i + 1] == "OR":
                group.append(tokens[i + 2])
                i += 2
            groups.append(group)
            i += 1
        return all(any(self._term(m, t) for t in group) for group in groups)

    def _term(self, m: FakeMsg, term: str) -> bool:
        neg = term.startswith("-")
        term = term.lstrip("-")
        key, _, value = term.partition(":")
        if key == "category":
            res = value in m.categories
        elif key == "in" and value == "inbox":
            res = "\\Inbox" in m.labels
        elif key == "in" and value == "sent":
            res = "\\Sent" in m.labels
        elif key == "newer_than" and value.endswith("d"):
            res = m.date_ts >= self.now_ts - int(value[:-1]) * 86400
        elif key == "rfc822msgid":
            res = (m.message_id or "").strip("<>") == value
        else:
            raise AssertionError(f"FakeGmail: неизвестный оператор X-GM-RAW {term!r}")
        return not res if neg else res

    def search(self, args: list[str]) -> list[int]:
        with self.lock:
            max_uid = max((m.uid for m in self.messages), default=0)
            result = []
            for m in self.messages:
                ok = True
                i = 0
                while i < len(args):
                    key = args[i].upper()
                    value = args[i + 1] if i + 1 < len(args) else ""
                    if key == "UID":
                        ok = ok and _in_set(m.uid, value, max_uid)
                    elif key == "X-GM-THRID":
                        ok = ok and m.thrid == int(value)
                    elif key == "X-GM-RAW":
                        ok = ok and self._raw_match(m, _unquote(value))
                    else:
                        raise AssertionError(f"FakeGmail: неизвестный критерий SEARCH {key}")
                    i += 2
                if ok:
                    result.append(m.uid)
            return result

    # --- выдача FETCH ------------------------------------------------------------------------------------
    def fetch(self, uidset: str, items: str, modifier: str | None) -> list:
        changedsince = None
        if modifier is not None:
            mm = re.fullmatch(r"\(CHANGEDSINCE (\d+)\)", modifier)
            assert mm, modifier
            changedsince = int(mm.group(1))
        names = _split_items(items)
        out: list = []
        with self.lock:
            max_uid = max((m.uid for m in self.messages), default=0)
            for seq, m in enumerate(self.messages, start=1):
                if not _in_set(m.uid, uidset, max_uid):
                    continue
                if changedsince is not None and m.modseq <= changedsince:
                    continue
                out.extend(self._fetch_one(seq, m, names))
        return out

    def _fetch_one(self, seq: int, m: FakeMsg, names: list[str]) -> list:
        parts: list = []
        cur = f"{seq} (".encode()
        first = True
        for name in names:
            sep = b"" if first else b" "
            first = False
            up = name.upper()
            literal: bytes | None = None
            if up == "UID":
                cur += sep + f"UID {m.uid}".encode()
            elif up == "X-GM-MSGID":
                cur += sep + f"X-GM-MSGID {m.msgid}".encode()
            elif up == "X-GM-THRID":
                cur += sep + f"X-GM-THRID {m.thrid}".encode()
            elif up == "X-GM-LABELS":
                labels = " ".join(_label_atom(label) for label in sorted(m.labels))
                cur += sep + f"X-GM-LABELS ({labels})".encode()
            elif up == "FLAGS":
                cur += sep + ("FLAGS (" + " ".join(sorted(m.flags)) + ")").encode()
            elif up == "INTERNALDATE":
                cur += sep + f'INTERNALDATE "{_internaldate(m.date_ts)}"'.encode()
            elif up == "RFC822.SIZE":
                cur += sep + f"RFC822.SIZE {len(m.raw)}".encode()
            elif up == "MODSEQ":
                cur += sep + f"MODSEQ ({m.modseq})".encode()
            elif up.startswith(("BODY.PEEK[", "BODY[")):
                peek = up.startswith("BODY.PEEK[")
                section = name[name.index("[") :]
                key = "BODY" + section
                partial = re.search(r"<(\d+)\.(\d+)>$", section)
                if partial:
                    start, length = int(partial.group(1)), int(partial.group(2))
                    key = "BODY" + section[: partial.start()] + f"<{start}>"
                    literal = m.raw[start : start + length]
                elif section == "[]":
                    literal = m.raw
                elif section.upper().startswith("[HEADER.FIELDS"):
                    fields = section[section.index("(") + 1 : section.index(")")].split()
                    literal = _header_fields(m.raw, fields)
                elif section.upper() == "[HEADER]":
                    literal = m.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                else:
                    raise AssertionError(f"FakeGmail: неизвестная секция {section}")
                if not peek:
                    # как настоящий сервер: BODY[] без PEEK ставит \Seen
                    m.flags.add("\\Seen")
                    self.modseq += 1
                    m.modseq = self.modseq
                cur += sep + f"{key} {{{len(literal)}}}".encode()
                parts.append((cur, literal))
                cur = b""
            else:
                raise AssertionError(f"FakeGmail: неизвестный атрибут FETCH {name}")
        parts.append(cur + b")")
        return parts

    # --- доставка отправленного (SMTP -> копия в «Вся почта») ----------------------------------------------
    def deliver_sent(self, raw: bytes) -> FakeMsg | None:
        self.sent.append(raw)
        if self.sent_copy_mode == "none":
            return None
        msg = email.message_from_bytes(raw, policy=policy.compat32)
        mid = "".join(str(msg.get("Message-ID", "")).split())
        if self.by_message_id(mid):
            return None  # Gmail схлопывает повтор с тем же Message-ID
        parent_id = "".join(str(msg.get("In-Reply-To", "")).split())
        parents = self.by_message_id(parent_id) if parent_id else []
        thrid = parents[0].thrid if parents and self.sent_copy_mode == "same_thread" else None
        to_self = parseaddr(str(msg.get("To", "")))[1].lower() == self.address
        labels = {"\\Sent"} | ({"\\Inbox"} if to_self else set())
        return self.add(raw, thrid=thrid, labels=labels, flags=("\\Seen",))


class FakeSmtp:
    """Подмена `aiosmtplib.SMTP`: шаги по одному, сбой по шагу — `smtp_faults[шаг]` (исключение
    `aiosmtplib`). `data` при успехе кладёт копию письма в «Вся почта» (как SMTP Gmail)."""

    def __init__(self, gm: FakeGmail, kwargs: dict) -> None:
        self.gm = gm
        self.kwargs = kwargs
        gm.smtp_log.append(("new", dict(kwargs)))

    def _step(self, name: str, *args) -> None:
        self.gm.smtp_log.append((name, *args))
        fault = self.gm.smtp_faults.get(name)
        if fault is not None:
            raise fault

    async def connect(self):
        self._step("connect")

    async def ehlo(self):
        self._step("ehlo")

    async def login(self, user, password):
        self.gm.smtp_log.append(("login_user", user))
        if password != self.gm.password:
            raise aiosmtplib.SMTPAuthenticationError(535, "5.7.8 Username and Password not accepted")
        self._step("login")

    async def mail(self, sender):
        self._step("mail", sender)

    async def rcpt(self, rcpt):
        self._step("rcpt", rcpt)

    @property
    def esmtp_extensions(self) -> dict:
        """Ф11-3b B4: расширения из ответа `EHLO` (как у aiosmtplib) — задаёт тест (`smtp_extensions`)."""
        return dict(getattr(self.gm, "smtp_extensions", {}) or {})

    async def data(self, message: bytes, timeout=None):  # noqa: ASYNC109 — подпись aiosmtplib `data()`
        if hasattr(self.gm, "smtp_data_timeouts"):
            self.gm.smtp_data_timeouts.append(timeout)
        self._step("data")
        if self.gm.smtp_deliver_then_fail is not None:
            self.gm.deliver_sent(message)
            raise self.gm.smtp_deliver_then_fail
        self.gm.deliver_sent(message)

    async def quit(self):
        self.gm.smtp_log.append(("quit",))

    def close(self):
        pass


def new_msgid(tag: str = "x") -> str:
    return make_msgid(idstring=tag, domain="example.com")
