"""`FakePlain` — подделка обычного IMAP-сервера (Ф12-4, вид `imap`: Timeweb и т. п.) на уровне соединения
`imaplib`, по образцу `FakeGmail`: без расширений Gmail, две папки (`INBOX` и «Отправленные»), у каждой свои
`UIDVALIDITY` и UID. Понимает ровно то, что шлёт шлюз вида `imap`: `LOGIN`, `CAPABILITY`, `LIST`, `EXAMINE`,
`UID SEARCH` (`UID`, `ALL`, `SINCE`, `HEADER`, `OR`), `UID FETCH`, `APPEND`, `NOOP`, `IDLE`; незнакомое ->
`AssertionError`. `STORE`, `SELECT`, `X-GM-*` — `AssertionError` (этому виду их слать нельзя). SMTP —
`FakeSmtp` из `fakegmail` (копию в «Отправленные» обычный SMTP НЕ кладёт — это делает `APPEND` шлюза).

Как mail.ru: `idle_before_login=False` — IDLE есть только в CAPABILITY после входа (`imaplib` проверяет
IDLE по списку до входа — соединение повторяет эту проверку); `search_headers={"message-id"}` — поиск
по прочим заголовкам -> `NO [CANNOT] Unsupported search criterion`."""

from __future__ import annotations

import email
import imaplib
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy

from hub.connectors.mail.imapwire import encode_mutf7

from .fakegmail import (
    BASE_TS,
    FakeIdler,
    FakeSmtp,
    _header_fields,
    _in_set,
    _internaldate,
    _split_items,
    _unquote,
)

ADDRESS = "e_owner@umbrella.example.com"
PASSWORD = "box-password-1"  # noqa: S105 — подменный пароль тестов
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1
    )
}


@dataclass
class PlainMsg:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)
    date_ts: int = BASE_TS

    def header(self, name: str) -> str:
        m = email.message_from_bytes(self.raw, policy=policy.compat32)
        return " ".join(str(m.get(name, "")).split())

    @property
    def message_id(self) -> str:
        return "".join(self.header("Message-ID").split())


@dataclass
class Folder:
    name: str
    uidvalidity: int
    next_uid: int = 1
    messages: list[PlainMsg] = field(default_factory=list)


class _Sock:
    def __init__(self, conn: FakePlainConn) -> None:
        self._conn = conn

    def shutdown(self, _how: int) -> None:
        self._conn.killed = True


class FakePlainConn:
    def __init__(self, srv: FakePlain) -> None:
        self.gm = srv  # имя поля — как у `FakeImapConn` (его читает `FakeIdler`)
        self.untagged_responses: dict = {}
        self.state = "NONAUTH"
        self.is_readonly = False
        self.killed = False
        self.closed = False
        self.idling = False
        self.selected: Folder | None = None
        self.name = f"conn{len(srv.connections) + 1}"
        # как `imaplib.IMAP4.capabilities`: список до входа (шлюз может заменить его ответом после входа)
        self.capabilities = tuple(c.upper() for c in srv.capabilities_before_login)

    def check_alive(self) -> None:
        if self.killed or self.closed:
            raise imaplib.IMAP4.abort("socket error: EOF")

    def login(self, user: str, password: str):
        self.gm.log(self, f"LOGIN {user} ***")
        self.check_alive()
        if user != self.gm.address or password != self.gm.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        self.state = "AUTH"
        return "OK", [b"Logged in"]

    def capability(self):
        self.gm.log(self, "CAPABILITY")
        self.check_alive()
        return "OK", [" ".join(self.gm.capabilities).encode()]

    def list(self, directory: str = '""', pattern: str = "*"):  # noqa: A003
        self.gm.log(self, "LIST")
        self.check_alive()
        return "OK", list(self.gm.list_lines)

    def noop(self):
        self.gm.log(self, "NOOP")
        self.check_alive()
        return "OK", [b"NOOP completed"]

    def _simple_command(self, name: str, *args: str):
        self.gm.log(self, " ".join([name, *args]))
        self.check_alive()
        if name != "EXAMINE":
            raise AssertionError(f"FakePlain: неожиданная команда {name}")
        assert "(CONDSTORE)" not in args, "вид imap не шлёт CONDSTORE"
        folder = self.gm.folder(_unquote(args[0]))
        with self.gm.lock:
            self.selected = folder
            self.untagged_responses = {
                "EXISTS": [str(len(folder.messages)).encode()],
                "UIDVALIDITY": [str(folder.uidvalidity).encode()],
            }
            if self.gm.send_uidnext:
                self.untagged_responses["UIDNEXT"] = [str(folder.next_uid).encode()]
        return "OK", [b"[READ-ONLY] examined"]

    def append(self, mailbox, flags, date_time, message):
        self.gm.log(self, f"APPEND {mailbox} {flags}")
        self.check_alive()
        if self.gm.append_error is not None:
            err, self.gm.append_error = self.gm.append_error, None
            raise err
        box = _unquote(mailbox)
        assert flags == "(\\Seen)", flags
        assert date_time is None
        self.gm.add(box, message, flags=("\\Seen",))
        return "OK", [b"[APPENDUID 1 1] Append completed."]

    def uid(self, command: str, *args: str):
        self.gm.log(self, " ".join(["UID", command, *args]))
        self.check_alive()
        if self.selected is None:
            raise imaplib.IMAP4.error("command UID illegal in state AUTH")
        if command == "SEARCH":
            try:
                found = self.gm.search(self.selected, list(args))
            except _Unsupported as exc:
                return "NO", [f"[CANNOT] Unsupported search criterion: {exc}".encode()]
            return "OK", [" ".join(str(u) for u in found).encode()]
        if command == "FETCH":
            assert len(args) == 2, args  # без CHANGEDSINCE
            return "OK", self.gm.fetch(self.selected, args[0], args[1])
        raise AssertionError(f"FakePlain: неожиданная команда UID {command}")

    def idle(self, duration: float | None = None) -> FakeIdler:
        assert "IDLE" in self.gm.capabilities, "IDLE без IDLE в CAPABILITY"
        if "IDLE" not in self.capabilities:  # проверка `imaplib.Idler`
            raise imaplib.IMAP4.error("Server does not support IMAP4 IDLE")
        return FakeIdler(self, duration)

    def socket(self) -> _Sock:
        return _Sock(self)

    def shutdown(self) -> None:
        self.closed = True


class _Unsupported(Exception):
    """Критерий поиска, которого сервер не умеет (ответ `NO [CANNOT]`)."""


def _parse_since(value: str) -> int:
    d, mon, y = value.split("-")
    return int(datetime(int(y), _MONTHS[mon.upper()], int(d), tzinfo=UTC).timestamp())


class FakePlain:
    def __init__(
        self,
        *,
        address: str = ADDRESS,
        password: str = PASSWORD,
        idle: bool = True,
        sent_name: str = "Sent",
        sent_special_use: bool = True,
        idle_before_login: bool | None = None,
        search_headers: set[str] | None = None,
    ) -> None:
        self.address = address
        self.password = password
        self.lock = threading.RLock()
        self.capabilities = ["IMAP4rev1", "LITERAL+", "SASL-IR", "LOGIN-REFERRALS", "ID", "ENABLE"]
        if idle:
            self.capabilities.append("IDLE")
        before = idle if idle_before_login is None else idle_before_login
        self.capabilities_before_login = [c for c in self.capabilities if c != "IDLE" or before]
        self.search_headers = search_headers  # None — любой заголовок; иначе только эти (строчными)
        self.folders: dict[str, Folder] = {"INBOX": Folder("INBOX", 11)}
        self.list_lines: list[bytes] = [b'(\\HasNoChildren) "." "INBOX"']
        if sent_name:
            sent_wire = encode_mutf7(sent_name)
            self.folders[sent_wire] = Folder(sent_wire, 22)
            flag = "\\Sent " if sent_special_use else ""
            self.list_lines.append(f'({flag}\\HasNoChildren) "." "{sent_wire}"'.encode())
        self.list_lines += [
            b'(\\HasNoChildren \\Drafts) "." "Drafts"',
            b'(\\HasNoChildren \\Trash) "." "Trash"',
        ]
        self.sent_wire = encode_mutf7(sent_name) if sent_name else None
        self.send_uidnext = True
        self.now_ts = BASE_TS + 86400  # «сейчас» — сутки после BASE_TS; письма по умолчанию — в эти сутки
        self._clock = BASE_TS
        self.commands: list[tuple[str, str]] = []
        self.connections: list[FakePlainConn] = []
        self.connect_errors: list[BaseException] = []
        self.append_error: BaseException | None = None
        # --- IDLE (`FakeIdler`) ---
        self.idle_queue: queue.Queue = queue.Queue()
        self.idle_start_errors: list[BaseException] = []
        self.idle_started = threading.Event()
        self.idle_poll_s = 0.01
        self.idle_real_max_s = 0.3
        self.idle_durations: list[float] = []
        self.on_idle_timeout: Callable[[float], None] | None = None
        # --- SMTP (`FakeSmtp`) ---
        self.smtp_log: list[tuple] = []
        self.smtp_faults: dict[str, BaseException] = {}
        self.smtp_deliver_then_fail: BaseException | None = None
        self.smtp_extensions: dict[str, str] = {}  # Ф11-3b: `EHLO` расширения (`{"size": "1000"}`)
        self.smtp_data_timeouts: list = []  # Ф11-3b П2: `timeout` каждого `data()`
        self.sent: list[bytes] = []

    # --- наполнение ---------------------------------------------------------------------------------------
    def folder(self, name: str) -> Folder:
        if name not in self.folders:
            raise imaplib.IMAP4.error(f"Mailbox doesn't exist: {name}")
        return self.folders[name]

    @property
    def inbox(self) -> Folder:
        return self.folders["INBOX"]

    @property
    def sent_box(self) -> Folder:
        assert self.sent_wire is not None
        return self.folders[self.sent_wire]

    def add(self, box: str, raw: bytes, *, flags=(), date_ts: int | None = None) -> PlainMsg:
        with self.lock:
            f = self.folder(box)
            if date_ts is None:
                self._clock += 60
                date_ts = self._clock
            m = PlainMsg(
                uid=f.next_uid,
                raw=raw if isinstance(raw, bytes) else bytes(raw),
                flags=set(flags),
                date_ts=date_ts,
            )
            f.next_uid += 1
            f.messages.append(m)
            return m

    def renumber(self, box: str, uidvalidity: int) -> None:
        """Сервер пересоздал папку: новый UIDVALIDITY и новые UID тех же писем."""
        with self.lock:
            f = self.folder(box)
            f.uidvalidity = uidvalidity
            f.next_uid = 100
            for m in f.messages:
                m.uid = f.next_uid
                f.next_uid += 1

    def set_flags(self, box: str, uid: int, *, add=(), remove=()) -> None:
        with self.lock:
            m = next(m for m in self.folder(box).messages if m.uid == uid)
            m.flags |= set(add)
            m.flags -= set(remove)

    def log(self, conn, cmd: str) -> None:
        with self.lock:
            self.commands.append((conn.name, cmd))

    def command_texts(self) -> list[str]:
        with self.lock:
            return [c for _n, c in self.commands]

    # --- фабрики для шлюза -----------------------------------------------------------------------------
    def imap_factory(self, timeout: float) -> FakePlainConn:
        assert timeout == 60.0
        with self.lock:
            if self.connect_errors:
                raise self.connect_errors.pop(0)
            conn = FakePlainConn(self)
            self.connections.append(conn)
            return conn

    def smtp_factory(self, **kwargs) -> FakeSmtp:
        return FakeSmtp(self, kwargs)

    def deliver_sent(self, raw: bytes) -> None:
        """Обычный SMTP копию в «Отправленные» не кладёт — только запоминаем отправленное."""
        self.sent.append(raw)

    # --- поиск ---------------------------------------------------------------------------------------------
    def _match(self, m: PlainMsg, args: list[str], i: int, max_uid: int) -> tuple[bool, int]:
        key = args[i].upper()
        if key == "OR":
            a, i = self._match(m, args, i + 1, max_uid)
            b, i = self._match(m, args, i, max_uid)
            return a or b, i
        if key == "ALL":
            return True, i + 1
        if key == "UID":
            return _in_set(m.uid, args[i + 1], max_uid), i + 2
        if key == "SINCE":
            return m.date_ts >= _parse_since(args[i + 1]), i + 2
        if key == "HEADER":
            name, value = args[i + 1], _unquote(args[i + 2])
            return value.lower() in m.header(name).lower(), i + 3
        raise AssertionError(f"FakePlain: неизвестный критерий SEARCH {key}")

    def search(self, folder: Folder, args: list[str]) -> list[int]:
        if self.search_headers is not None:  # отказ — до разбора писем (и в пустой папке)
            for j, a in enumerate(args[:-1]):
                if a.upper() == "HEADER" and args[j + 1].lower() not in self.search_headers:
                    raise _Unsupported(args[j + 1].upper())
        with self.lock:
            max_uid = max((m.uid for m in folder.messages), default=0)
            out = []
            for m in folder.messages:
                ok, i = True, 0
                while i < len(args):
                    res, i = self._match(m, args, i, max_uid)
                    ok = ok and res
                if ok:
                    out.append(m.uid)
            return out

    def fetch(self, folder: Folder, uidset: str, items: str) -> list:
        names = _split_items(items)
        out: list = []
        with self.lock:
            max_uid = max((m.uid for m in folder.messages), default=0)
            for seq, m in enumerate(folder.messages, start=1):
                if _in_set(m.uid, uidset, max_uid):
                    out.extend(self._fetch_one(seq, m, names))
        return out

    def _fetch_one(self, seq: int, m: PlainMsg, names: list[str]) -> list:
        parts: list = []
        cur = f"{seq} (".encode()
        first = True
        for name in names:
            sep = b"" if first else b" "
            first = False
            up = name.upper()
            if up == "UID":
                cur += sep + f"UID {m.uid}".encode()
            elif up == "FLAGS":
                cur += sep + ("FLAGS (" + " ".join(sorted(m.flags)) + ")").encode()
            elif up == "INTERNALDATE":
                cur += sep + f'INTERNALDATE "{_internaldate(m.date_ts)}"'.encode()
            elif up == "RFC822.SIZE":
                cur += sep + f"RFC822.SIZE {len(m.raw)}".encode()
            elif up.startswith("BODY.PEEK["):
                section = name[name.index("[") :]
                partial = re.search(r"<(\d+)\.(\d+)>$", section)
                key = "BODY" + section
                if partial:
                    start, length = int(partial.group(1)), int(partial.group(2))
                    key = "BODY" + section[: partial.start()] + f"<{start}>"
                    literal = m.raw[start : start + length]
                elif section == "[]":
                    literal = m.raw
                elif section.upper().startswith("[HEADER.FIELDS"):
                    fields = section[section.index("(") + 1 : section.index(")")].split()
                    literal = _header_fields(m.raw, fields)
                else:
                    raise AssertionError(f"FakePlain: неизвестная секция {section}")
                cur += sep + f"{key} {{{len(literal)}}}".encode()
                parts.append((cur, literal))
                cur = b""
            else:
                raise AssertionError(f"FakePlain: неизвестный атрибут FETCH {name}")
        parts.append(cur + b")")
        return parts
