"""М15: настоящий шлюз (`GmailGateway` + НАСТОЯЩИЙ `imaplib`) против локального скриптового IMAP-сервера на
сокете с ответами в форме Gmail: FETCH с литералами, `X-GM-LABELS` с modified UTF-7, кавычками и литералом,
`LIST` с `\\All`, `IDLE`/`DONE`/`BYE`, пустой `UID N:*`. Без TLS (фабрика — `imaplib.IMAP4`)."""

from __future__ import annotations

import asyncio
import imaplib
import re
import socket
import threading
import time

import pytest

from hub.connectors.mail.gateway import GmailGateway, MailAuthError

ADDRESS = "me@gmail.com"
PASSWORD = "abcdefghijklmnop"  # noqa: S105 — тестовый

CAPS = (
    b"IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 UIDPLUS COMPRESS=DEFLATE ENABLE "
    b"MOVE CONDSTORE ESEARCH UTF8=ACCEPT LIST-EXTENDED LIST-STATUS LITERAL- SPECIAL-USE APPENDLIMIT=35651584"
)
ALL_BOX = b"[Gmail]/&BBIEQQRP- &BD8EPgRHBEIEMA-"
RAW_1040 = (
    b"From: =?UTF-8?B?0J/RkdGC0YA=?= <petr@example.com>\r\nTo: me@gmail.com\r\n"
    b"Subject: =?UTF-8?B?0KHQvNC10YLQsA==?=\r\nMessage-ID: <CAP1@mail.gmail.com>\r\n"
    b"Content-Type: text/plain; charset=UTF-8\r\n\r\n\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82 (1)\r\n"
)
FROM_1040 = b"From: =?UTF-8?B?0J/RkdGC0YA=?= <petr@example.com>\r\n\r\n"
FROM_1041 = b"From: Owner <me@gmail.com>\r\n\r\n"


def cmd(pattern: str, *lines: bytes):
    return ("cmd", re.compile(pattern), lines)


def idle(*untagged: bytes, bye: bool = False, hold: bool = False):
    return ("idle", untagged, bye, hold)


def login_steps() -> list:
    ok = b"{tag} OK Success\r\n"
    return [
        cmd(r"CAPABILITY", b"* CAPABILITY " + CAPS + b"\r\n", b"{tag} OK Thats all she wrote!\r\n"),
        cmd(
            rf'LOGIN {re.escape(ADDRESS)} "{PASSWORD}"',
            b"* CAPABILITY " + CAPS + b"\r\n",
            b"{tag} OK me@gmail.com authenticated (Success)\r\n",
        ),
        cmd(r"CAPABILITY", b"* CAPABILITY " + CAPS + b"\r\n", b"{tag} OK Success\r\n"),
        cmd(
            r'LIST "" \*',
            b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n',
            b'* LIST (\\HasChildren \\Noselect) "/" "[Gmail]"\r\n',
            b'* LIST (\\All \\HasNoChildren) "/" "' + ALL_BOX + b'"\r\n',
            b'* LIST (\\Drafts \\HasNoChildren) "/" "[Gmail]/&BCcENQRABD0EPgQyBDgEOgQ4-"\r\n',
            b'* LIST (\\HasNoChildren \\Sent) "/" "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"\r\n',
            b'* LIST (\\HasNoChildren) "/" "&BCAEMAQxBD4EQgQw-"\r\n',
            ok,
        ),
        cmd(
            r'EXAMINE "' + re.escape(ALL_BOX.decode()) + r'" \(CONDSTORE\)',
            b"* FLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen $NotPhishing $Phishing)\r\n",
            b"* OK [PERMANENTFLAGS ()] Flags permitted.\r\n",
            b"* OK [UIDVALIDITY 11] UIDs valid.\r\n",
            b"* 42 EXISTS\r\n",
            b"* 0 RECENT\r\n",
            b"* OK [UIDNEXT 1043] Predicted next UID.\r\n",
            b"* OK [HIGHESTMODSEQ 987654]\r\n",
            b"{tag} OK [READ-ONLY] [Gmail]/All Mail selected. (Success)\r\n",
        ),
    ]


class ScriptedImapServer:
    """Принимает соединения по очереди; у каждого — свой сценарий шагов. Несовпадение команды — в `errors`."""

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)
        self.received: list[str] = []
        self.errors: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stop = threading.Event()
        self.connections = 0

    def start(self) -> ScriptedImapServer:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set() and self._scripts:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            script = self._scripts.pop(0)
            threading.Thread(target=self._handle, args=(conn, script), daemon=True).start()

    def _handle(self, conn: socket.socket, steps: list) -> None:
        rf = conn.makefile("rb")
        try:
            conn.sendall(b"* OK Gimap ready for requests from 127.0.0.1 x1\r\n")
            for step in steps:
                line = rf.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                tag, _, rest = text.partition(" ")
                self.received.append(rest)
                if step[0] == "cmd":
                    _, pattern, lines = step
                    if not pattern.fullmatch(rest):
                        self.errors.append(f"ожидали {pattern.pattern!r}, пришло {rest!r}")
                        conn.sendall(f"{tag} BAD unexpected\r\n".encode())
                        continue
                    for out in lines:
                        conn.sendall(out.replace(b"{tag}", tag.encode()))
                else:
                    _, untagged, bye, hold = step
                    if rest != "IDLE":
                        self.errors.append(f"ожидали IDLE, пришло {rest!r}")
                        return
                    conn.sendall(b"+ idling\r\n")
                    time.sleep(0.05)
                    for u in untagged:
                        conn.sendall(u)
                    if bye:
                        conn.sendall(b"* BYE Temporary System Problem\r\n")
                        return
                    if hold:
                        while rf.readline():
                            pass
                        return
                    done = rf.readline().decode().strip()
                    self.received.append(done)
                    conn.sendall(f"{tag} OK IDLE terminated (Success)\r\n".encode())
            while rf.readline():  # держать соединение, пока клиент не закроет
                pass
        except OSError:
            return
        finally:
            conn.close()


def _gateway(server: ScriptedImapServer, **kw) -> GmailGateway:
    return GmailGateway(
        address=ADDRESS,
        password=PASSWORD,
        imap_factory=lambda timeout: imaplib.IMAP4("127.0.0.1", server.port, timeout=timeout),
        listener_wait=lambda ev, s: ev.wait(0.01),
        **kw,
    )


async def test_m15_real_imaplib_connect_search_fetch_literals_labels():
    meta_fetch = (
        b"* 40 FETCH (X-GM-THRID 1811111111111111111 X-GM-MSGID 1811111111111111112 X-GM-LABELS "
        b'("\\\\Important" "\\\\Inbox" "&BCAEMAQxBD4EQgQw-" "Proj \\"X\\"") UID 1040 FLAGS (\\Seen) '
        b'INTERNALDATE "24-Sep-2026 07:15:02 +0000" RFC822.SIZE 5321 BODY[HEADER.FIELDS (FROM)] {'
        + str(len(FROM_1040)).encode()
        + b"}\r\n"
        + FROM_1040
        + b")\r\n"
        # метка литералом и пустые флаги
        + b"* 41 FETCH (X-GM-THRID 1811111111111111111 X-GM-MSGID 1811111111111111113 X-GM-LABELS ({7}\r\n"
        + b'Lit lab \\Sent) UID 1041 FLAGS () INTERNALDATE " 4-Sep-2026 10:00:00 +0300" RFC822.SIZE 99 '
        + b"BODY[HEADER.FIELDS (FROM)] {"
        + str(len(FROM_1041)).encode()
        + b"}\r\n"
        + FROM_1041
        + b")\r\n"
        # чужой нетегированный FETCH (без UID) — отбросить
        + b"* 7 FETCH (FLAGS (\\Seen))\r\n"
        + b"{tag} OK Success\r\n"
    )
    body_fetch = (
        b"* 40 FETCH (UID 1040 BODY[] {" + str(len(RAW_1040)).encode() + b"}\r\n" + RAW_1040 + b")\r\n"
        b"{tag} OK Success\r\n"
    )
    script = login_steps() + [
        cmd(r"UID SEARCH UID 1043:\*", b"* SEARCH 1042\r\n", b"{tag} OK SEARCH completed (Success)\r\n"),
        cmd(
            r"UID FETCH 1040:1041 \(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE RFC822\.SIZE "
            r"BODY\.PEEK\[HEADER\.FIELDS \(FROM\)\]\)",
            meta_fetch,
        ),
        cmd(r"UID FETCH 1040 \(UID BODY\.PEEK\[\]\)", body_fetch),
        cmd(
            r'UID SEARCH UID 1040:1041 X-GM-RAW "category:promotions OR category:social"',
            b"* SEARCH\r\n",
            b"{tag} OK SEARCH completed (Success)\r\n",
        ),
    ]
    server = ScriptedImapServer([script]).start()
    gw = _gateway(server)
    try:
        info = await gw.work.connect()
        assert info.all_mailbox == ALL_BOX.decode()
        assert info.sent_mailbox is not None and info.condstore
        state = gw.work.state
        assert (state.uidvalidity, state.uidnext, state.highestmodseq, state.exists) == (11, 1043, 987654, 42)
        assert state.last_uid == 1042

        assert await gw.work.search_new(1042) == []  # пустой `UID 1043:*` отдал последнее — отфильтровано

        metas = {m.uid: m for m in await gw.work.fetch_meta([1040, 1041])}
        assert set(metas) == {1040, 1041}
        m = metas[1040]
        assert m.thrid == 1811111111111111111 and m.msgid == 1811111111111111112
        assert m.labels == ["\\Important", "\\Inbox", "Работа", 'Proj "X"']
        assert m.flags == ["\\Seen"]
        assert m.internaldate_ms == 1790234102000
        assert m.size == 5321 and m.header == FROM_1040
        m2 = metas[1041]
        assert m2.labels == ["Lit lab", "\\Sent"]
        assert m2.flags == []
        assert m2.internaldate_ms == 1788505200000

        bodies = await gw.work.fetch_bodies([(1040, 5321)])
        assert bodies[0].uid == 1040 and bodies[0].body == RAW_1040

        assert await gw.work.search_categories(1040, 1041, ["promotions", "social"]) == set()
    finally:
        await gw.close()
        server.close()
    assert server.errors == []


async def test_m15_listener_idle_done_bye_reconnect():
    first = login_steps() + [idle(b"* 43 EXISTS\r\n"), idle(bye=True)]
    second = login_steps() + [idle(hold=True)]
    server = ScriptedImapServer([first, second]).start()
    gw = _gateway(server)
    events: list[str] = []
    try:
        gw.start_listener(lambda kind, _d: events.append(kind))
        deadline = time.monotonic() + 10
        while events.count("connected") < 2 and time.monotonic() < deadline:  # noqa: ASYNC110 — события приходят из потока слушателя
            await asyncio.sleep(0.02)
        assert events[:3] == ["connected", "new", "connected"]
        assert "DONE" in server.received  # вышли из IDLE по EXISTS штатно
        assert server.connections == 2
    finally:
        await gw.close()
        server.close()
    assert server.errors == []


async def test_m15_bad_password_is_auth_error_and_password_not_in_text():
    script = [
        cmd(r"CAPABILITY", b"* CAPABILITY " + CAPS + b"\r\n", b"{tag} OK Thats all she wrote!\r\n"),
        cmd(r"LOGIN .*", b"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)\r\n"),
    ]
    server = ScriptedImapServer([script]).start()
    gw = _gateway(server)
    try:
        with pytest.raises(MailAuthError) as ei:
            await gw.work.connect()
        assert ei.value.kind == "bad_password"
        assert PASSWORD not in str(ei.value) and PASSWORD not in repr(ei.value)
        assert PASSWORD not in repr(gw)
    finally:
        await gw.close()
        server.close()
