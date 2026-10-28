"""`GmailGateway` — ЕДИНСТВЕННОЕ место с `imaplib`/`aiosmtplib` (сеть почты). Спецификация:
`docs/architecture.md`, М9, М10, М11, М13, М15.

Три соединения IMAP (М2), каждое в своём daemon-потоке и строго последовательное:
- «слушатель» (`_Listener`) — `EXAMINE \\All` и цикл IDLE; на `EXISTS`/`FETCH`/`EXPUNGE` выходит из IDLE и
  сигналит «есть новое» (коннектор заказывает сверку). Переподключение с бэкоффом 1…60 с (джиттер), раз в 23 ч
  — принудительно; «слишком много соединений»/блокировка трафика — пауза 30 мин;
- «рабочее» (`ImapSession("work")`) — сверка, догон, стартовая загрузка;
- «отправочное» (`ImapSession("send")`) — поиск родителя и сверка отправки.
Везде `EXAMINE` (только чтение, флаги не тронуть даже по ошибке), тела — только `BODY.PEEK` (не ставить
`\\Seen`). ЕДИНСТВЕННОЕ исключение — `mark_thread_seen` (Ф8-7, `docs/architecture.md` + П3):
одним заданием рабочего соединения `SELECT` «Вся почта» -> `UID SEARCH X-GM-THRID` -> `UID FETCH` ->
`UID STORE <uid> +FLAGS.SILENT (\\Seen)` -> снова `EXAMINE`; любая ошибка внутри — соединение сбрасывается.
Больше `STORE` нет нигде; `CLOSE`, `EXPUNGE`, `-FLAGS`, `FLAGS` без `+`, любой флаг кроме `\\Seen`, `STORE`
без `UID`, `COPY`/`MOVE` — запрещены во всём шлюзе; `APPEND` — только `append_sent` вида `imap` (Ф12-4,
`docs/architecture.md`: копия отправленного в «Отправленные» с флагом `\\Seen`).
Таймаут сокета 60 с; таймаут/отмена посреди команды -> соединение испорчено, закрывается и пересоздаётся при
следующей команде.

Ф12-4: вид ящика `flavor` — `gmail` (умолчание, всё как было) | `imap` (обычный IMAP/SMTP: хосты и порты из
конфига, без `X-GM-EXT-1` и `\\All`; основной ящик — `INBOX`, «Отправленные» — SPECIAL-USE `\\Sent` или по
имени; сервер без IDLE — слушатель опрашивает раз в 2 мин). Команды вида `imap` ходят с явным ящиком
(`box=`): задание само делает `EXAMINE` нужной папки, если соединение сейчас в другой.

SMTP — `aiosmtplib` вручную по шагам (М9): `connect`, `ehlo`, `login`, `mail`, `rcpt`, `data`; исход
классифицируется здесь (`SmtpResult`), в `SendResult` его переводит коннектор.

Пароль приложения живёт только в памяти этого объекта: в журнал, исключения и конфиг не попадает.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import imaplib
import logging
import queue
import random
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import aiosmtplib

from .imapwire import (
    FetchedMsg,
    ImapParseError,
    last_int,
    parse_fetch,
    parse_list,
    parse_search,
    quote,
    uid_set,
)

_logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

SOCKET_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 90.0  # > таймаута сокета: команда, повисшая дольше, — испорченное соединение
RECONNECT_EVERY_S = 23 * 60 * 60.0
IDLE_SESSION_RECONNECT_S = 20 * 60.0  # рабочее/отправочное соединение простаивало — Gmail мог его закрыть
# Ф99-3: таймер простоя прокси на пути до почты (xray `connIdle` 300 с, проверка раз в 300 с от начала
# соединения) закрывает соединение, по которому 300 с не прошло ни байта. Поэтому заход IDLE (DONE+IDLE —
# одна команда на том же соединении, без нового входа) у ВСЕХ ящиков не длиннее 240 с, а рабочее/отправочное
# соединение в простое получает `NOOP` раз в 240 с.
IDLE_DURATION_S = 240.0
PLAIN_IDLE_DURATION_S = 240.0
KEEPALIVE_S = 240.0
LOGINS_WINDOW_S = 24 * 3600.0
LOGINS_REPORT_EVERY_S = 3600.0
# Ф51-1 A3: невнятный отказ входа (`other`) ПОСЛЕ удачного входа в этом процессе — до трёх повторов с этими
# паузами, и только потом `auth` (выход 78). Предохранитель от блокировки учётки: не больше трёх повторов,
# счётчик сбрасывает удачный вход; `bad_password`/`imap_disabled`/`web_login` и первый вход — сразу.
LOGIN_RETRY_PAUSES_S = (30.0, 120.0, 300.0)
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.2
THROTTLE_BACKOFF_S = 30 * 60.0
LISTENER_JOIN_S = 5.0

BODY_FULL_MAX = 5 * 1024 * 1024  # М11: больше — только первые 2 МБ
BODY_PARTIAL_BYTES = 2 * 1024 * 1024
BODY_BATCH_MSGS = 20
BODY_BATCH_BYTES = 20 * 1024 * 1024
META_BATCH = 200

SMTP_PRE_DATA_TIMEOUT_S = 20.0
SMTP_DATA_TIMEOUT_S = 15.0
# Ф11-3b B4: письмо с вложениями — таймаут `data()` растёт с размером: 17 МиБ -> base64 ~24 МБ -> ~+190 с.
SMTP_DATA_RATE_BPS = 128 * 1024
SMTP_LOCAL_HOSTNAME = "hub.localdomain"
SMTP_QUOTA_RETRY_MS = 24 * 60 * 60 * 1000
POLL_INTERVAL_S = 120.0
SMTP_STARTTLS_PORTS = (25, 587)  # Ф12-4: сервер без IDLE — опрос раз в 2 мин

FLAVOR_GMAIL = "gmail"
FLAVOR_IMAP = "imap"
FLAVORS = (FLAVOR_GMAIL, FLAVOR_IMAP)
# Ф12-4, решение 2: «Отправленные» без SPECIAL-USE — по имени (без учёта регистра)
SENT_NAMES = ("sent", "отправленные", "inbox.sent", "inbox/sent", "sent items", "sent messages")

_IDLE_SIGNAL_TYPES = frozenset({"EXISTS", "FETCH", "EXPUNGE", "VANISHED"})
_THROTTLE_MARKERS = (
    "too many simultaneous connections",
    "overquota",
    "bandwidth",
    "account exceeded command or bandwidth limits",
    "[throttled]",
)
_AUTH_BAD_PASSWORD = ("authenticationfailed", "invalid credentials", "application-specific password")
_AUTH_IMAP_DISABLED = ("imap access is disabled", "not enabled for imap", "imap is disabled", "enable imap")
_AUTH_WEB = ("webalert", "web browser", "web login required")


# --- ошибки ------------------------------------------------------------------------------------------------


class MailError(Exception):
    """База ошибок шлюза. Текст — без пароля и без содержимого писем."""


class MailAuthError(MailError):
    """Gmail отказал во входе. `kind`: `bad_password` | `imap_disabled` | `web_login` | `other`."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"вход не принят ({kind})")
        self.kind = kind
        self.detail = detail


class MailUnavailable(MailError):
    """Сеть/соединение/таймаут — источник сейчас недоступен."""


class MailThrottled(MailUnavailable):
    """«Слишком много соединений» / блокировка трафика Gmail — пауза 30 мин (М10)."""


class MailLoginRetry(MailUnavailable):
    """Ф51-1 A3: сервер невнятно отказал во входе после обрыва — вход повторится через `wait_s` секунд.
    Для вызывающих это «источник недоступен», не потеря входа."""

    def __init__(self, wait_s: float, attempt: int) -> None:
        total = len(LOGIN_RETRY_PAUSES_S)
        super().__init__(f"imap: вход не принят после обрыва — повтор {attempt} из {total}")
        self.wait_s = wait_s
        self.attempt = attempt


class MailCommandError(MailError):
    """Сервер ответил NO/BAD на команду или ответ не разобран."""


class MailConfigError(MailError):
    """Ящик не годится: нет `X-GM-EXT-1`/`IDLE` или папки `\\All`."""


def describe(exc: BaseException) -> str:
    """Короткое описание для журнала/статуса — без текста сервера (там бывает адрес) и без пароля."""
    if isinstance(exc, MailAuthError):
        return f"auth:{exc.kind}"
    return type(exc).__name__


def _short_text(exc: BaseException) -> str:
    """Короткий текст исходного исключения для журнала: без ответа сервера, до 80 символов."""
    if isinstance(exc, imaplib.IMAP4.error):  # abort — его подкласс; текст — ответ сервера
        return "(ответ сервера не показан)"
    return " ".join(str(exc).split())[:80]


def _server_text(exc: BaseException) -> str:
    return " ".join(str(a) for a in getattr(exc, "args", ()) if a is not None).lower()


def _classify_login_error(exc: BaseException) -> MailError:
    text = _server_text(exc)
    if any(m in text for m in _THROTTLE_MARKERS):
        return MailThrottled("gmail: слишком много соединений / лимит трафика")
    if "[unavailable]" in text or "temporary" in text:
        return MailUnavailable("gmail: временно недоступен")
    if any(m in text for m in _AUTH_IMAP_DISABLED):
        return MailAuthError("imap_disabled")
    if any(m in text for m in _AUTH_WEB):
        return MailAuthError("web_login")
    if any(m in text for m in _AUTH_BAD_PASSWORD):
        return MailAuthError("bad_password")
    return MailAuthError("other")


def _translate(exc: BaseException) -> BaseException:
    """Исключение потока IMAP -> ошибка шлюза. `MailError` — как есть."""
    if isinstance(exc, MailError):
        return exc
    if isinstance(exc, imaplib.IMAP4.abort):
        return MailUnavailable("imap: соединение оборвано")
    if isinstance(exc, imaplib.IMAP4.error):
        text = _server_text(exc)
        if any(m in text for m in _THROTTLE_MARKERS):
            return MailThrottled("gmail: лимит")
        return MailCommandError("imap: сервер отказал в команде")
    if isinstance(exc, ImapParseError):
        return MailCommandError("imap: ответ не разобран")
    if isinstance(exc, OSError | TimeoutError | ssl.SSLError):
        return MailUnavailable(f"imap: сеть ({type(exc).__name__})")
    if type(exc).__module__.startswith("python_socks"):
        return MailUnavailable(f"imap: прокси ({type(exc).__name__})")
    return exc


def _spoils_connection(exc: BaseException) -> bool:
    """Команда отвергнута сервером (NO/BAD) или ответ не понят — соединение цело; прочее — испорчено."""
    return not isinstance(exc, MailCommandError | MailConfigError)


def _shutdown_socket(imap: Any) -> None:
    """Разбудить поток, висящий в чтении (IDLE блокирует до конца `duration`), — `shutdown(SHUT_RDWR)`
    на самом дескрипторе, мимо `SSLSocket.shutdown` (тот трогает SSL-объект, которым пользуется другой
    поток). Закрывает сокет уже сам поток."""
    with contextlib.suppress(Exception):
        sock = imap.socket()
        if isinstance(sock, socket.socket):
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        else:
            sock.shutdown(socket.SHUT_RDWR)


def _close_imap(imap: Any) -> None:
    with contextlib.suppress(Exception):
        imap.shutdown()


def normalize_proxy(value: str | None) -> str | None:
    """`None`/''/'none' -> без прокси (по умолчанию — напрямую, спека «Факты»). `socks5h://` -> `socks5://`:
    `python-socks` схему `socks5h` не понимает, имена разрешает прокси (`rdns=True`)."""
    if not value or value == "none":
        return None
    if value.startswith("socks5h://"):
        return "socks5://" + value[len("socks5h://") :]
    return value


class _GmailImap(imaplib.IMAP4_SSL):
    """`IMAP4_SSL` со своим сокетом через SOCKS5 (`_create_socket` — точка расширения `imaplib`)."""

    def __init__(self, host: str, port: int, *, proxy: str | None, timeout: float) -> None:
        self._hub_proxy = proxy
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout):  # noqa: ANN001, ANN202 — сигнатура imaplib
        if self._hub_proxy is None:
            return super()._create_socket(timeout)
        from python_socks.sync import Proxy

        raw = Proxy.from_url(self._hub_proxy, rdns=True).connect(
            dest_host=self.host, dest_port=self.port, timeout=timeout
        )
        raw.settimeout(timeout)
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)


# --- состояние ящика ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerInfo:
    capabilities: frozenset[str]
    all_mailbox: str  # имя как с сервера (modified UTF-7) — в команды идёт оно
    sent_mailbox: str | None

    @property
    def condstore(self) -> bool:
        return "CONDSTORE" in self.capabilities

    @property
    def idle(self) -> bool:
        return "IDLE" in self.capabilities


@dataclass(frozen=True)
class MailboxState:
    uidvalidity: int
    uidnext: int | None
    highestmodseq: int | None
    exists: int | None

    @property
    def last_uid(self) -> int:
        return max(0, (self.uidnext or 1) - 1)


def _parse_capabilities(data: list) -> frozenset[str]:
    caps: set[str] = set()
    for item in data or []:
        if isinstance(item, bytes):
            caps.update(p.decode("ascii", errors="replace").upper() for p in item.split())
    return frozenset(caps)


def _check_ok(typ: str, what: str) -> None:
    if typ != "OK":
        raise MailCommandError(f"imap: {what} -> {typ}")


def _examine(imap: Any, mailbox: str, *, condstore: bool) -> MailboxState:
    """`EXAMINE` (только чтение), с `(CONDSTORE)`, если сервер умеет. `imaplib.select()` параметров не
    принимает — команда шлётся напрямую, состояние `SELECTED` ставится как у `select(readonly=True)`."""
    imap.untagged_responses = {}
    imap.is_readonly = True
    args = [quote(mailbox)]
    if condstore:
        args.append("(CONDSTORE)")
    typ, _dat = imap._simple_command("EXAMINE", *args)  # noqa: SLF001
    _check_ok(typ, "EXAMINE")
    imap.state = "SELECTED"
    untagged = imap.untagged_responses
    uidvalidity = last_int(untagged, "UIDVALIDITY")
    if uidvalidity is None:
        raise MailCommandError("imap: EXAMINE без UIDVALIDITY")
    state = MailboxState(
        uidvalidity=uidvalidity,
        uidnext=last_int(untagged, "UIDNEXT"),
        highestmodseq=last_int(untagged, "HIGHESTMODSEQ"),
        exists=last_int(untagged, "EXISTS"),
    )
    imap.untagged_responses = {}
    return state


def _select_for_seen(imap: Any, mailbox: str) -> None:
    """Ф8-7: `SELECT` (чтение-запись) того же ящика «Вся почта» — только внутри `mark_thread_seen`.
    Ответы `SELECT` (UIDVALIDITY и т. п.) не нужны: состояние ящика снимает возвратный `EXAMINE`."""
    imap.untagged_responses = {}
    imap.is_readonly = False
    typ, _dat = imap._simple_command("SELECT", quote(mailbox))  # noqa: SLF001
    _check_ok(typ, "SELECT")
    imap.state = "SELECTED"
    imap.untagged_responses = {}


def _uid_store_seen(imap: Any, uids: list[int]) -> None:
    """Ф8-7: ЕДИНСТВЕННЫЙ `STORE` шлюза — `UID STORE <uid> +FLAGS.SILENT (\\Seen)`: только добавить,
    только `\\Seen`, только по UID."""
    imap.untagged_responses.pop("FETCH", None)
    typ, _dat = imap.uid("STORE", uid_set(uids), "+FLAGS.SILENT", "(\\Seen)")
    _check_ok(typ, "UID STORE")


def _uid_append_seen(imap: Any, mailbox: str, message: bytes) -> None:
    """Ф12-4, решение 5: ЕДИНСТВЕННЫЙ `APPEND` шлюза — копия отправленного письма в «Отправленные» вида
    `imap` с флагом `\\Seen` (дата — время сервера)."""
    typ, _dat = imap.append(quote(mailbox), "(\\Seen)", None, message)
    _check_ok(typ, "APPEND")


def _uid_search(imap: Any, *criteria: str) -> list[int]:
    imap.untagged_responses.pop("SEARCH", None)
    typ, data = imap.uid("SEARCH", *criteria)
    if typ != "OK":
        # текст отказа — в исключение (трассировка журнала): `[CANNOT] Unsupported search criterion` и т. п.
        reply = next((d for d in data or [] if isinstance(d, bytes)), b"").decode("utf-8", errors="replace")
        raise MailCommandError(f"imap: UID SEARCH -> {typ}: {reply[:120]}")
    return parse_search(data)


def _uid_fetch(imap: Any, uids: list[int] | str, items: str, modifier: str | None = None) -> list[FetchedMsg]:
    imap.untagged_responses.pop("FETCH", None)
    target = uids if isinstance(uids, str) else uid_set(uids)
    # Модификатор (CHANGEDSINCE) — в той же строке, что атрибуты: imaplib с 3.14.8 принимает у UID FETCH ровно
    # два аргумента (третий -> ValueError), а строку «(…) (…)» передаёт как есть.
    parts = f"{items} {modifier}" if modifier else items
    typ, data = imap.uid("FETCH", target, parts)
    _check_ok(typ, "UID FETCH")
    wanted = None if isinstance(uids, str) else set(uids)
    return [m for m in parse_fetch(data) if wanted is None or m.uid in wanted]


# --- сессия IMAP в своём потоке ----------------------------------------------------------------------------


class _Job:
    __slots__ = ("cancelled", "fn", "fut", "loop")

    def __init__(
        self, fn: Callable[[Any], Any], loop: asyncio.AbstractEventLoop, fut: asyncio.Future
    ) -> None:
        self.fn = fn
        self.loop = loop
        self.fut = fut
        self.cancelled = False


def _deliver(job: _Job, result: Any = None, exc: BaseException | None = None) -> None:
    def _set() -> None:
        if job.fut.done():
            return
        if exc is not None:
            job.fut.set_exception(exc)
        else:
            job.fut.set_result(result)

    with contextlib.suppress(RuntimeError):  # цикл уже закрыт — отдавать некому
        job.loop.call_soon_threadsafe(_set)


class ImapSession:
    """Одно соединение IMAP в своём daemon-потоке (М2, М10): задания выполняются строго по одному;
    подключение (вход + `LIST` + `EXAMINE \\All`) — лениво перед первым заданием и заново после порчи/23 ч."""

    def __init__(self, gw: GmailGateway, name: str) -> None:
        self._gw = gw
        self.name = name
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._imap: Any = None
        self._connected_mono: float | None = None
        self._used_mono: float | None = None
        self._broken = False
        self._closed = False
        self._lock = asyncio.Lock()
        self.info: ServerInfo | None = None
        self.state: MailboxState | None = None
        # Ф12-4: папка, открытая сейчас (`EXAMINE`), и состояние папок вида `imap` по имени
        self._selected: str | None = None
        self.box_states: dict[str, MailboxState] = {}

    # --- поток ---------------------------------------------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name=f"hub-imap-{self.name}", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=self._gw.keepalive_s)
            except queue.Empty:
                self._keepalive()
                continue
            if job is None:
                self._drop()
                return
            if job.cancelled:
                continue
            try:
                result = self._run_job(job)
            except BaseException as raw_exc:  # noqa: BLE001 — всё уходит вызывающему
                exc = _translate(raw_exc)
                if _spoils_connection(exc):
                    self._drop()
                _deliver(job, exc=exc)
            else:
                _deliver(job, result)

    def _keepalive(self) -> None:
        """Ф99-3: соединение простояло `keepalive_s` без заданий — `NOOP`, чтобы таймер простоя прокси (300 с)
        его не закрыл. Не открывает новый вход (входов при обычной нагрузке меньше, чем при `LOGOUT` после
        каждой работы); оборвалось — тихо закрыть, следующее задание откроет новое. 23 ч — закрыть."""
        imap = self._imap
        if imap is None or self._closed:
            return
        if self._broken or (
            self._connected_mono is not None and self._gw.mono() - self._connected_mono >= RECONNECT_EVERY_S
        ):
            self._drop()
            return
        try:
            typ, _dat = imap.noop()
            _check_ok(typ, "NOOP")
        except BaseException as raw_exc:  # noqa: BLE001
            _logger.info(
                "mail: NOOP соединения %s не прошёл (%s) — закрыто", self.name, describe(_translate(raw_exc))
            )
            self._drop()
            return
        self._used_mono = self._gw.mono()

    def _run_job(self, job: _Job) -> Any:
        """Команды сессии — чтение (SEARCH/FETCH/EXAMINE) и идемпотентный `mark_thread_seen`
        (`+FLAGS \\Seen`), повтор безопасен: если соединение было старым (не только что открытым) и
        оборвалось — один повтор на новом (Gmail закрывает простаивающее соединение; первая команда после
        долгой паузы иначе падала бы всегда)."""
        imap, fresh = self._ensure_connected()
        try:
            return job.fn(imap)
        except BaseException as raw_exc:
            exc = _translate(raw_exc)
            if (
                fresh
                or job.cancelled
                or not isinstance(exc, MailUnavailable)
                or isinstance(exc, MailThrottled)
            ):
                raise
            _logger.info("mail: соединение %s оборвалось — повтор команды на новом", self.name)
            self._drop()
            imap, _fresh = self._ensure_connected()
            return job.fn(imap)

    def _ensure_connected(self) -> tuple[Any, bool]:
        now = self._gw.mono()
        if self._imap is not None and (
            self._broken
            or (self._connected_mono is not None and now - self._connected_mono >= RECONNECT_EVERY_S)
            or (self._used_mono is not None and now - self._used_mono >= IDLE_SESSION_RECONNECT_S)
        ):
            self._drop()
        fresh = False
        if self._imap is None:
            imap, info, state = self._gw.open_connection(self.name)
            self._imap = imap
            self.info = info
            self.state = state
            self._selected = info.all_mailbox
            self.box_states[info.all_mailbox] = state
            self._connected_mono = self._gw.mono()
            self._broken = False
            fresh = True
        self._used_mono = self._gw.mono()
        return self._imap, fresh

    def _drop(self) -> None:
        imap, self._imap = self._imap, None
        self._broken = False
        self._selected = None
        if imap is not None:
            _close_imap(imap)

    # --- асинхронная сторона -----------------------------------------------------------------------------
    def _examine_box(self, imap: Any, box: str) -> MailboxState:
        """Ф12-4: `EXAMINE` папки вида `imap` (без CONDSTORE), состояние — в `box_states`. Сервер без
        `UIDNEXT` в ответе — последний UID поиском (иначе курсор встал бы на 0 и догон взял бы весь ящик)."""
        state = _examine(imap, box, condstore=False)
        self._selected = box
        if state.uidnext is None:
            uids = _uid_search(imap, "ALL") if state.exists else []
            state = MailboxState(
                uidvalidity=state.uidvalidity,
                uidnext=(max(uids) + 1) if uids else 1,
                highestmodseq=None,
                exists=state.exists,
            )
        self.box_states[box] = state
        return state

    def _in_box(self, box: str, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def _wrapped(imap: Any) -> Any:
            if self._selected != box:
                self._examine_box(imap, box)
            return fn(imap)

        return _wrapped

    async def call(
        self, fn: Callable[[Any], Any], *, limit_s: float = CALL_TIMEOUT_S, box: str | None = None
    ) -> Any:
        """`box` (Ф12-4, вид `imap`) — задание само открывает эту папку, если соединение сейчас в другой
        (между заданиями сверки могло вклиниться чужое — загрузчик вложений, другой ящик)."""
        if box is not None:
            fn = self._in_box(box, fn)
        if self._closed:
            raise MailUnavailable("imap: сессия закрыта")
        self._ensure_thread()
        loop = asyncio.get_running_loop()
        async with self._lock:
            fut = loop.create_future()
            job = _Job(fn, loop, fut)
            self._jobs.put(job)
            try:
                return await asyncio.wait_for(asyncio.shield(fut), limit_s)
            except TimeoutError:
                job.cancelled = True
                fut.cancel()
                self.kill()
                raise MailUnavailable(f"imap: таймаут команды ({self.name})") from None
            except asyncio.CancelledError:
                job.cancelled = True
                fut.cancel()
                self.kill()
                raise

    def kill(self) -> None:
        """Соединение испорчено (таймаут/отмена/сон): разбудить поток и пересоздать соединение при следующем
        задании."""
        self._broken = True
        imap = self._imap
        if imap is not None:
            _shutdown_socket(imap)

    async def close(self) -> None:
        self._closed = True
        self.kill()
        self._jobs.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, LISTENER_JOIN_S)

    # --- команды высокого уровня ---------------------------------------------------------------------------
    async def connect(self) -> ServerInfo:
        await self.call(lambda imap: None)
        assert self.info is not None  # noqa: S101 — ставится в потоке до ответа
        return self.info

    async def examine(self) -> MailboxState:
        """Свежий `EXAMINE \\All` (UIDVALIDITY/UIDNEXT/HIGHESTMODSEQ) — заодно и `NOOP`
        рабочего соединения."""

        def _fn(imap: Any) -> MailboxState:
            assert self.info is not None  # noqa: S101
            state = _examine(imap, self.info.all_mailbox, condstore=self.info.condstore)
            self.state = state
            self._selected = self.info.all_mailbox
            return state

        return await self.call(_fn)

    # --- Ф12-4: команды вида `imap` (папка явно) ---------------------------------------------------------
    async def examine_box(self, box: str) -> MailboxState:
        """Свежий `EXAMINE` папки (UIDVALIDITY/UIDNEXT) — заодно и `NOOP` соединения."""
        return await self.call(lambda imap: self._examine_box(imap, box))

    async def search_box(self, box: str, *criteria: str) -> list[int]:
        uids = await self.call(lambda imap: _uid_search(imap, *criteria), box=box)
        return sorted(set(uids))

    async def search_new_box(self, box: str, last_uid: int) -> list[int]:
        """Догон папки: `UID SEARCH UID <last+1>:*` + фильтр `uid > last` (без новых `N:*` отдаёт
        последнее)."""
        uids = await self.search_box(box, "UID", f"{last_uid + 1}:*")
        return [u for u in uids if u > last_uid]

    async def fetch_box(
        self, box: str, uids: list[int], items: str, *, chunk: int = META_BATCH
    ) -> list[FetchedMsg]:
        return await self._fetch_chunks(uids, items, chunk=chunk, box=box)

    async def fetch_range_box(self, box: str, lo: int, hi: int, items: str) -> list[FetchedMsg]:
        spec = f"{max(1, lo)}:{max(1, hi)}"
        return await self.call(lambda imap: _uid_fetch(imap, spec, items), box=box)

    async def append_sent(self, box: str, message: bytes, *, limit_s: float = CALL_TIMEOUT_S) -> None:
        """Ф12-4, решение 5: копия отправленного в «Отправленные» с `\\Seen`. Только вид `imap` — у Gmail
        копию кладёт сам SMTP."""
        if self._gw.flavor != FLAVOR_IMAP:
            raise MailConfigError("APPEND — только для обычного IMAP")
        await self.call(lambda imap: _uid_append_seen(imap, box, message), limit_s=limit_s)

    async def search_new(self, last_uid: int) -> list[int]:
        """C2: `UID SEARCH UID <last+1>:*` + фильтр `uid > last` (М11: без новых писем `N:*`
        отдаёт последнее)."""
        uids = await self.call(lambda imap: _uid_search(imap, "UID", f"{last_uid + 1}:*"))
        return sorted(u for u in uids if u > last_uid)

    async def search_categories(self, lo: int, hi: int, categories: list[str]) -> set[int]:
        """C3(б): UID порции, попавшие в исключённые категории (`X-GM-RAW "category:a OR category:b"`)."""
        if not categories:
            return set()
        query = " OR ".join(f"category:{c}" for c in categories)
        uids = await self.call(lambda imap: _uid_search(imap, "UID", f"{lo}:{hi}", "X-GM-RAW", quote(query)))
        return set(uids)

    async def search_thread(self, thrid: int) -> list[int]:
        """М8: письма треда по числовому `X-GM-THRID` (сырой Message-ID в команду не идёт)."""
        uids = await self.call(lambda imap: _uid_search(imap, "X-GM-THRID", str(int(thrid))))
        return sorted(uids)

    async def search_raw(self, query: str) -> list[int]:
        uids = await self.call(lambda imap: _uid_search(imap, "X-GM-RAW", quote(query)))
        return sorted(uids)

    async def _fetch_chunks(
        self, uids: list[int], items: str, *, chunk: int = META_BATCH, box: str | None = None
    ) -> list[FetchedMsg]:
        out: list[FetchedMsg] = []
        ordered = sorted(set(uids))
        for i in range(0, len(ordered), chunk):
            part = ordered[i : i + chunk]
            out.extend(await self.call(lambda imap, part=part: _uid_fetch(imap, part, items), box=box))
        return out

    async def fetch_meta(self, uids: list[int]) -> list[FetchedMsg]:
        """М11, фаза 1: метаданные порции (+ `From` — отличить своё письмо для правила (в) C3)."""
        items = (
            "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE RFC822.SIZE "
            "BODY.PEEK[HEADER.FIELDS (FROM)])"
        )
        return await self._fetch_chunks(uids, items)

    async def fetch_thread_meta(self, uids: list[int]) -> list[FetchedMsg]:
        """М3/C4: метки, флаги, дата и Message-ID всех писем треда — `attrs` и маркер прочтения."""
        items = (
            "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE RFC822.SIZE "
            "BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID)])"
        )
        return await self._fetch_chunks(uids, items)

    async def fetch_parent_headers(self, uids: list[int]) -> list[FetchedMsg]:
        """E1: заголовки для ответа (без тел)."""
        items = (
            "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE "
            "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES SUBJECT FROM REPLY-TO TO CC)])"
        )
        return await self._fetch_chunks(uids, items)

    async def fetch_thrids(self, uids: list[int]) -> list[FetchedMsg]:
        return await self._fetch_chunks(uids, "(UID X-GM-THRID INTERNALDATE)", chunk=500)

    async def fetch_changed_flags(self, modseq: int) -> list[FetchedMsg]:
        """C4 при CONDSTORE: `UID FETCH 1:* (FLAGS …) (CHANGEDSINCE <modseq>)`."""
        items = "(UID FLAGS X-GM-THRID MODSEQ)"
        return await self.call(lambda imap: _uid_fetch(imap, "1:*", items, f"(CHANGEDSINCE {int(modseq)})"))

    async def fetch_flags_range(self, lo: int, hi: int) -> list[FetchedMsg]:
        """C4 без CONDSTORE: флаги окна UID."""
        items = "(UID FLAGS X-GM-THRID)"
        return await self.call(lambda imap: _uid_fetch(imap, f"{max(1, lo)}:{max(1, hi)}", items))

    async def fetch_bodies(
        self, sized: list[tuple[int, int | None]], *, box: str | None = None
    ) -> list[FetchedMsg]:
        """М11, фаза 2: тела ТОЛЬКО отобранных — `BODY.PEEK[]` целиком (≤ 5 МБ) или первые 2 МБ; не больше
        20 писем или 20 МБ на один FETCH."""
        full = [(u, s or 0) for u, s in sized if (s or 0) <= BODY_FULL_MAX]
        partial = [u for u, s in sized if (s or 0) > BODY_FULL_MAX]
        out: list[FetchedMsg] = []
        batch: list[int] = []
        batch_bytes = 0
        for uid, size in sorted(full):
            if batch and (len(batch) >= BODY_BATCH_MSGS or batch_bytes + size > BODY_BATCH_BYTES):
                out.extend(await self._fetch_body_batch(batch, "(UID BODY.PEEK[])", box=box))
                batch, batch_bytes = [], 0
            batch.append(uid)
            batch_bytes += size
        if batch:
            out.extend(await self._fetch_body_batch(batch, "(UID BODY.PEEK[])", box=box))
        per_batch = max(1, BODY_BATCH_BYTES // BODY_PARTIAL_BYTES)
        for i in range(0, len(partial), per_batch):
            out.extend(
                await self._fetch_body_batch(
                    sorted(partial)[i : i + per_batch], f"(UID BODY.PEEK[]<0.{BODY_PARTIAL_BYTES}>)", box=box
                )
            )
        return out

    async def _fetch_body_batch(
        self, uids: list[int], items: str, *, box: str | None = None
    ) -> list[FetchedMsg]:
        return await self.call(lambda imap: _uid_fetch(imap, uids, items), box=box)

    async def fetch_full(
        self, uid: int, *, limit_s: float = CALL_TIMEOUT_S, box: str | None = None
    ) -> FetchedMsg | None:
        """Ф11-3a A3: одно письмо целиком (`BODY.PEEK[]`, без предела М11) — загрузчик вложений. Флаги не
        трогает. Письма нет -> `None`."""
        uid = int(uid)
        got = await self.call(
            lambda imap: _uid_fetch(imap, [uid], "(UID BODY.PEEK[])"), limit_s=limit_s, box=box
        )
        return next((m for m in got if m.uid == uid and m.body is not None), None)

    async def mark_thread_seen(self, thrid: int, up_to_ms: int, *, limit_s: float = CALL_TIMEOUT_S) -> int:
        """Ф8-7 C3 + П3: письма треда `thrid` с `INTERNALDATE <= up_to_ms` без `\\Seen` и не черновики ->
        `\\Seen`. ОДНО задание сессии (между командами не вклинится сверка): `SELECT` «Вся почта» ->
        `UID SEARCH X-GM-THRID` -> `UID FETCH (UID INTERNALDATE FLAGS X-GM-LABELS)` ->
        `UID STORE … +FLAGS.SILENT (\\Seen)` -> `EXAMINE` того же ящика (кэш `state` обновляется). ЛЮБАЯ
        ошибка внутри (сеть, `NO` на `STORE` или на возвратном `EXAMINE`) — соединение сбрасывается, в
        `SELECT` не остаётся: следующее задание откроет новое (вход + `EXAMINE`). Повтор задания (после
        обрыва старого соединения) безопасен: `+FLAGS \\Seen` идемпотентен. Возвращает число помеченных."""
        thrid = int(thrid)
        up_to_ms = int(up_to_ms)

        def _fn(imap: Any) -> int:
            info = self.info
            assert info is not None  # noqa: S101 — ставится при подключении
            try:
                _select_for_seen(imap, info.all_mailbox)
                marked = 0
                uids = _uid_search(imap, "X-GM-THRID", str(thrid))
                if uids:
                    metas = _uid_fetch(imap, sorted(set(uids)), "(UID INTERNALDATE FLAGS X-GM-LABELS)")
                    targets = sorted(
                        m.uid
                        for m in metas
                        if m.internaldate_ms is not None
                        and m.internaldate_ms <= up_to_ms
                        and "\\Seen" not in m.flags
                        and not m.is_draft
                    )
                    if targets:
                        _uid_store_seen(imap, targets)
                        marked = len(targets)
                self.state = _examine(imap, info.all_mailbox, condstore=info.condstore)
                self._selected = info.all_mailbox
            except BaseException:
                # П3: соединение могло остаться в `SELECT` — считаем испорченным при ЛЮБОЙ ошибке.
                self._drop()
                raise
            return marked

        return await self.call(_fn, limit_s=limit_s)


# --- слушатель (IDLE) ---------------------------------------------------------------------------------------


class _Listener:
    def __init__(self, gw: GmailGateway, on_event: Callable[[str, str | None], None]) -> None:
        self._gw = gw
        self._on_event = on_event
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._imap: Any = None
        self._thread = threading.Thread(target=self._run, name="hub-imap-listener", daemon=True)
        self.down_since: float | None = gw.wall()
        self.connected = False
        self._phase = "команда"

    def start(self) -> None:
        self._thread.start()

    def _wait(self, seconds: float) -> None:
        self._gw.listener_wait(self._wake, seconds)
        self._wake.clear()

    def _jittered(self, base: float) -> float:
        return base * (1 + BACKOFF_JITTER * (2 * self._gw.rng() - 1))

    def _mark_down(self) -> None:
        self.connected = False
        if self.down_since is None:
            self.down_since = self._gw.wall()

    def _run(self) -> None:
        backoff = self._gw.backoff_base_s
        while not self._stop.is_set():
            try:
                imap, info, _state = self._gw.open_connection("listener")
            except MailAuthError as exc:
                self._mark_down()
                self._on_event("auth", exc.kind)
                return
            except MailLoginRetry as exc:
                # Ф51-1 A3: пауза до повтора входа; её рвут `stop` и `restart` (тогда `open_connection`
                # сам вернёт остаток паузы, в сеть раньше срока не пойдёт).
                self._mark_down()
                self._wait(exc.wait_s)
                continue
            except MailThrottled:
                self._mark_down()
                _logger.warning("mail: Gmail ограничил соединения — пауза %.0f мин", THROTTLE_BACKOFF_S / 60)
                self._wait(self._gw.throttle_backoff_s)
                continue
            except BaseException as exc:  # noqa: BLE001 — сеть/прокси: бэкофф, не падение потока
                self._mark_down()
                _logger.warning("mail: слушатель не подключился (%s)", describe(_translate(exc)))
                self._wait(self._jittered(backoff))
                backoff = min(backoff * 2, self._gw.backoff_cap_s)
                continue

            if self._stop.is_set():
                _close_imap(imap)
                return
            self._imap = imap
            self.connected = True
            self.down_since = None
            self._on_event("connected", None)
            connected_at = self._gw.mono()
            failed = False
            self._phase = "команда"
            try:
                while not self._stop.is_set():
                    self._gw.report_logins()
                    remaining = RECONNECT_EVERY_S - (self._gw.mono() - connected_at)
                    if remaining <= 0:
                        _logger.info("mail: плановое переподключение слушателя (23 ч)")
                        break
                    if info.idle:
                        got = self._idle_once(imap, min(self._gw.idle_duration_s, remaining))
                    else:
                        got = self._poll_once(imap, min(self._gw.poll_interval_s, remaining))
                    backoff = self._gw.backoff_base_s
                    if got:
                        self._on_event("new", None)
            except BaseException as exc:  # noqa: BLE001
                failed = True
                if not self._stop.is_set():
                    _logger.warning(
                        "mail: слушатель оборвался (возраст соединения %.0f с, фаза: %s, %s: %s)",
                        self._gw.mono() - connected_at,
                        self._phase,
                        type(exc).__name__,
                        _short_text(exc),
                    )
            finally:
                self._imap = None
                _close_imap(imap)
                self._mark_down()
            if failed and not self._stop.is_set():
                self._wait(self._jittered(backoff))
                backoff = min(backoff * 2, self._gw.backoff_cap_s)

    def _idle_once(self, imap: Any, duration: float) -> bool:
        """Один заход IDLE. `True` — сервер сообщил о новом/изменённом письме (вышли из IDLE досрочно)."""
        self._phase = "команда"
        with imap.idle(duration=duration) as idler:
            self._phase = "ждали в IDLE"
            for typ, _data in idler:
                if typ == "BYE":
                    raise MailUnavailable("imap: сервер закрыл соединение (BYE)")
                if typ in _IDLE_SIGNAL_TYPES:
                    self._phase = "ждали ответ на DONE"
                    return True
            self._phase = "ждали ответ на DONE"
        self._phase = "команда"
        return False

    def _poll_once(self, imap: Any, interval: float) -> bool:
        self._phase = "пауза опроса"
        """Ф12-4: сервер без IDLE — пауза `interval` (её рвут `stop`/`restart`), затем `NOOP` (жив ли сокет) и
        сигнал «есть новое»: сверку заказывает коннектор, как на `EXISTS`."""
        self._wait(interval)
        if self._stop.is_set():
            return False
        self._phase = "команда"
        typ, _dat = imap.noop()
        _check_ok(typ, "NOOP")
        return True

    def restart(self) -> None:
        """После сна: порвать текущее соединение (поток переподключится сам) и сбросить паузу."""
        imap = self._imap
        if imap is not None:
            _shutdown_socket(imap)
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        imap = self._imap
        if imap is not None:
            _shutdown_socket(imap)
        if self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, LISTENER_JOIN_S)


# --- SMTP --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SmtpResult:
    outcome: Literal["sent", "temporary", "permanent", "unknown", "auth", "flood"]
    stage: str
    code: int | None = None
    retry_after_ms: int | None = None
    # Ф11-3b B4: готовый текст ошибки для отчёта (письмо больше объявленного `SIZE` сервера); иначе — `None`.
    error: str | None = None


def smtp_data_timeout(size: int, cap_s: float | None = None) -> float:
    """Ф11-3b B4: таймаут `data()` — `SMTP_DATA_TIMEOUT_S + size / SMTP_DATA_RATE_BPS`, но не дальше `cap_s`
    (остаток срока `send()` минус запас; `None` — без потолка). Не меньше 1 с."""
    timeout = SMTP_DATA_TIMEOUT_S + size / SMTP_DATA_RATE_BPS
    if cap_s is not None:
        timeout = min(timeout, cap_s)
    return max(timeout, 1.0)


def _declared_size(smtp: Any) -> int | None:
    """RFC 1870: `SIZE` из ответа `EHLO` (`esmtp_extensions` aiosmtplib); нет, кривой или `0` (без
    ограничения) -> `None`."""
    extensions = getattr(smtp, "esmtp_extensions", None)
    raw = extensions.get("size") if isinstance(extensions, dict) else None
    if not isinstance(raw, str):
        return None
    head = raw.strip().split(" ")[0]
    if not head.isdigit():
        return None
    value = int(head)
    return value or None


def _is_quota(code: int | None, message: str | None) -> bool:
    text = (message or "").lower()
    return code == 550 and ("5.4.5" in text or "quota" in text)  # noqa: PLR2004


def _code_outcome(code: int | None) -> Literal["temporary", "permanent"]:
    return "permanent" if code is not None and 500 <= code < 600 else "temporary"  # noqa: PLR2004


# --- шлюз --------------------------------------------------------------------------------------------------


def _default_wait(event: threading.Event, seconds: float) -> None:
    event.wait(max(0.0, seconds))


class GmailGateway:
    """Сеть почты — только здесь. `imap_factory(timeout)` и `smtp_factory(**kw)` подменяются в тестах
    (`FakeGmail`, скриптовый сервер на сокете); по умолчанию — `imaplib` (TLS 993, свой сокет для SOCKS5) и
    `aiosmtplib.SMTP` (TLS 465, `sock=` для SOCKS5)."""

    def __init__(
        self,
        *,
        address: str,
        password: str,
        proxy: str | None = None,
        imap_factory: Callable[[float], Any] | None = None,
        smtp_factory: Callable[..., Any] | None = None,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        listener_wait: Callable[[threading.Event, float], None] = _default_wait,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
        throttle_backoff_s: float = THROTTLE_BACKOFF_S,
        idle_duration_s: float | None = None,
        flavor: str = FLAVOR_GMAIL,
        imap_host: str = IMAP_HOST,
        imap_port: int = IMAP_PORT,
        smtp_host: str = SMTP_HOST,
        smtp_port: int = SMTP_PORT,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        if flavor not in FLAVORS:
            raise ValueError(f"неизвестный вид ящика {flavor!r}")
        self.address = address
        self._password = password
        self._proxy = normalize_proxy(proxy)
        self.flavor = flavor
        self.imap_host = imap_host
        self.imap_port = int(imap_port)
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.poll_interval_s = poll_interval_s
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or aiosmtplib.SMTP
        self.mono = mono
        self.wall = wall
        self.rng = rng
        self.listener_wait = listener_wait
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.throttle_backoff_s = throttle_backoff_s
        if idle_duration_s is None:
            idle_duration_s = PLAIN_IDLE_DURATION_S if flavor == FLAVOR_IMAP else IDLE_DURATION_S
        self.idle_duration_s = idle_duration_s
        self.keepalive_s = KEEPALIVE_S
        # Ф99-3: моменты удачных LOGIN (wall) за последние сутки — счётчик в журнале раз в час.
        self._login_times: collections.deque[float] = collections.deque()
        self._logins_reported: float | None = None
        # Ф51-1 A3: вход в этом процессе уже удавался; сколько повторов после невнятного отказа назначено и
        # до какого момента (монотонные часы) в сеть со входом не ходим. Под `_cooldown_lock`: соединений три.
        self._login_ok = False
        self._login_retries = 0
        self._login_retry_until: float | None = None
        self._cooldown_until: float | None = None
        self._cooldown_lock = threading.Lock()
        self.work = ImapSession(self, "work")
        self.sender = ImapSession(self, "send")
        self._listener: _Listener | None = None
        self.server_info: ServerInfo | None = None

    def __repr__(self) -> str:  # пароль не показывать даже в отладке
        return f"GmailGateway(address={self.address!r}, flavor={self.flavor!r})"

    # --- подключение (зовётся из потоков) -------------------------------------------------------------------
    def _default_imap(self, timeout: float) -> Any:
        return _GmailImap(self.imap_host, self.imap_port, proxy=self._proxy, timeout=timeout)

    def _set_cooldown(self) -> None:
        with self._cooldown_lock:
            self._cooldown_until = self.mono() + self.throttle_backoff_s

    def _check_login_pause(self) -> None:
        """Ф51-1 A3: идёт пауза перед повтором входа — в сеть не ходим (три соединения делят один счётчик)."""
        with self._cooldown_lock:
            until, attempt = self._login_retry_until, self._login_retries
        if until is not None:
            left = until - self.mono()
            if left > 0:
                raise MailLoginRetry(left, attempt)

    def _count_login(self) -> None:
        with self._cooldown_lock:
            self._login_times.append(self.wall())

    def logins_24h(self) -> int:
        """Ф99-3: входов (LOGIN) на этот ящик за последние 24 ч."""
        with self._cooldown_lock:
            cutoff = self.wall() - LOGINS_WINDOW_S
            while self._login_times and self._login_times[0] < cutoff:
                self._login_times.popleft()
            return len(self._login_times)

    def report_logins(self) -> None:
        """Ф99-3: раз в час строка info «входов за сутки: N» (зовёт цикл слушателя)."""
        now = self.mono()
        if self._logins_reported is not None and now - self._logins_reported < LOGINS_REPORT_EVERY_S:
            return
        self._logins_reported = now
        _logger.info("mail: входов за сутки: %d (%s)", self.logins_24h(), self.address)

    def _login_accepted(self) -> None:
        with self._cooldown_lock:
            self._login_ok = True
            self._login_retries = 0
            self._login_retry_until = None

    def _login_refused(self, err: MailAuthError) -> MailError:
        """Ф51-1 A3: что поднять на отказ входа. Невнятный (`other`) после удачного входа в этом процессе —
        `MailLoginRetry` с паузой, не больше `len(LOGIN_RETRY_PAUSES_S)` раз подряд; всё прочее и четвёртый
        отказ — сам `MailAuthError` (прежний путь: `auth`, выход 78)."""
        with self._cooldown_lock:
            if err.kind != "other" or not self._login_ok:
                return err
            until = self._login_retry_until
            now = self.mono()
            if until is not None and until > now:
                # Пауза уже идёт: второе соединение стояло на LOGIN одновременно с первым и получило тот
                # же отказ — это один сбой сервера, повтор не сжигаем.
                return MailLoginRetry(until - now, self._login_retries)
            if self._login_retries >= len(LOGIN_RETRY_PAUSES_S):
                return err
            pause = LOGIN_RETRY_PAUSES_S[self._login_retries]
            self._login_retries += 1
            self._login_retry_until = self.mono() + pause
            attempt = self._login_retries
        _logger.warning(
            "mail: вход не принят после обрыва (other) — повтор %d из %d через %.0f с",
            attempt,
            len(LOGIN_RETRY_PAUSES_S),
            pause,
        )
        return MailLoginRetry(pause, attempt)

    def open_connection(self, name: str) -> tuple[Any, ServerInfo, MailboxState]:
        """Подключиться, войти, найти `\\All`/`\\Sent` по SPECIAL-USE, `EXAMINE \\All`. Поток вызывающего."""
        with self._cooldown_lock:
            cooldown = self._cooldown_until
        if cooldown is not None and self.mono() < cooldown:
            raise MailThrottled("gmail: пауза после ограничения соединений")
        self._check_login_pause()
        try:
            imap = self._imap_factory(SOCKET_TIMEOUT_S)
        except BaseException as exc:
            raise _translate(exc) from None
        try:
            try:
                typ, _ = imap.login(self.address, self._password)
            except imaplib.IMAP4.abort as exc:
                raise MailUnavailable("imap: соединение оборвано при входе") from exc
            except imaplib.IMAP4.error as exc:
                err = _classify_login_error(exc)
                if isinstance(err, MailThrottled):
                    self._set_cooldown()
                if isinstance(err, MailAuthError):
                    err = self._login_refused(err)
                raise err from None
            _check_ok(typ, "LOGIN")
            self._login_accepted()
            self._count_login()
            typ, cap_data = imap.capability()
            _check_ok(typ, "CAPABILITY")
            caps = _parse_capabilities(cap_data)
            # `imaplib` помнит CAPABILITY до входа и по нему решает, можно ли IDLE; mail.ru объявляет IDLE
            # только после входа — без этой строки `idle()` падает «Server does not support IMAP4 IDLE».
            imap.capabilities = tuple(sorted(caps))
            if self.flavor == FLAVOR_IMAP:
                info = self._plain_info(imap, caps)
                state = _examine(imap, info.all_mailbox, condstore=False)
            else:
                info, state = self._gmail_info(imap, caps)
        except BaseException as exc:
            _close_imap(imap)
            translated = _translate(exc)
            if isinstance(translated, MailThrottled):
                self._set_cooldown()
            if translated is exc:
                raise
            raise translated from None
        self.server_info = info
        _logger.debug("mail: соединение %s открыто", name)
        return imap, info, state

    @staticmethod
    def _gmail_info(imap: Any, caps: frozenset[str]) -> tuple[ServerInfo, MailboxState]:
        """Gmail: обязательны `X-GM-EXT-1`, `IDLE` и папка `\\All`; открывается «Вся почта»."""
        if "X-GM-EXT-1" not in caps:
            raise MailConfigError("нет X-GM-EXT-1 — это не Gmail")
        if "IDLE" not in caps:
            raise MailConfigError("сервер не умеет IDLE")
        typ, list_data = imap.list()
        _check_ok(typ, "LIST")
        entries = parse_list(list_data)
        all_box = next((e.name for e in entries if "\\All" in e.flags), None)
        sent_box = next((e.name for e in entries if "\\Sent" in e.flags), None)
        if all_box is None:
            raise MailConfigError("нет папки \\All («Вся почта») — включите её показ в IMAP")
        info = ServerInfo(capabilities=caps, all_mailbox=all_box, sent_mailbox=sent_box)
        state = _examine(imap, all_box, condstore=info.condstore)
        return info, state

    @staticmethod
    def _plain_info(imap: Any, caps: frozenset[str]) -> ServerInfo:
        """Ф12-4, решение 2: обычный IMAP — основной ящик `INBOX`, «Отправленные» по SPECIAL-USE `\\Sent`,
        иначе по имени (`Sent`/`Отправленные`/`INBOX.Sent`…); не нашли — понятная ошибка подключения."""
        typ, list_data = imap.list()
        _check_ok(typ, "LIST")
        entries = [e for e in parse_list(list_data) if "\\Noselect" not in e.flags]
        sent_box = next((e.name for e in entries if "\\Sent" in e.flags), None)
        if sent_box is None:
            by_name = {e.display_name.casefold(): e.name for e in entries}
            sent_box = next((by_name[n] for n in SENT_NAMES if n in by_name), None)
        if sent_box is None:
            raise MailConfigError(
                "на сервере не найдена папка «Отправленные» (ни по атрибуту \\Sent, ни по имени Sent/"
                "Отправленные/INBOX.Sent)"
            )
        return ServerInfo(capabilities=caps, all_mailbox="INBOX", sent_mailbox=sent_box)

    # --- слушатель ----------------------------------------------------------------------------------------
    def start_listener(self, on_event: Callable[[str, str | None], None]) -> None:
        """`on_event(kind, detail)` зовётся ИЗ ПОТОКА слушателя: `connected` | `new` | `auth` — вызывающий
        сам переносит его в свой цикл (`call_soon_threadsafe`)."""
        if self._listener is not None:
            return
        self._listener = _Listener(self, on_event)
        self._listener.start()

    def restart_listener(self) -> None:
        if self._listener is not None:
            self._listener.restart()

    def listener_down_since(self) -> float | None:
        """Настенное время, с которого слушатель не подключён (`None` — подключён)."""
        if self._listener is None:
            return None
        return self._listener.down_since

    async def stop_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            await listener.stop()

    async def mark_thread_seen(self, thrid: int, up_to_ms: int, *, limit_s: float = CALL_TIMEOUT_S) -> int:
        """Ф8-7 C3: `\\Seen` письмам треда до `up_to_ms` — на РАБОЧЕМ соединении (не на слушателе IDLE), под
        его очередью заданий. Подробности — `ImapSession.mark_thread_seen`."""
        return await self.work.mark_thread_seen(thrid, up_to_ms, limit_s=limit_s)

    async def reset_sessions(self) -> None:
        """После сна: рабочее и отправочное соединения испорчены — пересоздать при следующей команде."""
        self.work.kill()
        self.sender.kill()

    async def close(self) -> None:
        """Все три соединения — разом (каркас даёт `stop_source()` 10 с; потоки — демоны)."""
        await asyncio.gather(
            self.stop_listener(), self.work.close(), self.sender.close(), return_exceptions=True
        )

    # --- SMTP ----------------------------------------------------------------------------------------------
    async def _smtp_connect(self) -> Any:
        sock = None
        if self._proxy is not None:
            from python_socks.async_.asyncio import Proxy

            sock = await Proxy.from_url(self._proxy, rdns=True).connect(
                dest_host=self.smtp_host, dest_port=self.smtp_port, timeout=SMTP_PRE_DATA_TIMEOUT_S
            )
        kwargs: dict[str, Any] = {
            "hostname": self.smtp_host,
            "use_tls": True,
            "timeout": SMTP_PRE_DATA_TIMEOUT_S,
            "local_hostname": SMTP_LOCAL_HOSTNAME,
        }
        if self.smtp_port in SMTP_STARTTLS_PORTS:
            # Ф12-4: 587/25 — STARTTLS после EHLO (465 — TLS сразу, как у Gmail)
            kwargs["use_tls"] = False
            kwargs["start_tls"] = True
        if sock is not None:
            kwargs["sock"] = sock
        else:
            kwargs["port"] = self.smtp_port
        smtp = self._smtp_factory(**kwargs)
        await smtp.connect()
        return smtp

    async def _smtp_quit(self, smtp: Any) -> None:
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5.0):
                await smtp.quit()
        with contextlib.suppress(Exception):
            smtp.close()

    async def check_smtp_login(self) -> None:
        """`hub auth mail`: EHLO + AUTH без отправки. `MailAuthError` / `MailUnavailable`."""
        smtp = None
        try:
            async with asyncio.timeout(SMTP_PRE_DATA_TIMEOUT_S):
                smtp = await self._smtp_connect()
                await smtp.ehlo()
                await smtp.login(self.address, self._password)
        except aiosmtplib.SMTPAuthenticationError as exc:
            raise MailAuthError("bad_password" if exc.code in (534, 535) else "other") from None
        except Exception as exc:  # noqa: BLE001
            raise MailUnavailable(f"smtp: {type(exc).__name__}") from None
        finally:
            if smtp is not None:
                await self._smtp_quit(smtp)

    async def smtp_send(
        self, *, sender: str, recipients: list[str], message: bytes, data_cap_s: float | None = None
    ) -> SmtpResult:
        """М9: по шагам. До `data`: сеть/таймаут -> `temporary`; 535/534 -> `auth`; `rcpt` 5xx -> `permanent`.
        `data`: 4xx -> `temporary`, 5xx -> `permanent`, суточная квота -> `flood`; ЛЮБОЕ исключение из
        `data()` без кода ответа сервера -> `unknown` (тело могло дойти).

        Ф11-3b B4: после `EHLO` длина письма сверяется с объявленным `SIZE` сервера — больше -> `permanent`
        до `MAIL FROM`. Таймаут `data()` — `smtp_data_timeout(len, data_cap_s)`; тот же срок уходит и в сам
        `smtp.data(timeout=…)` (П2: иначе действует таймаут соединения aiosmtplib, 20 с)."""
        smtp = None
        stage = "connect"
        try:
            try:
                async with asyncio.timeout(SMTP_PRE_DATA_TIMEOUT_S):
                    smtp = await self._smtp_connect()
                    stage = "ehlo"
                    await smtp.ehlo()
                    limit = _declared_size(smtp)
                    if limit is not None and len(message) > limit:
                        return SmtpResult(
                            "permanent",
                            "size",
                            error=(
                                "письмо больше, чем принимает сервер "
                                f"({max(1, round(limit / 1_000_000))} МБ)"
                            ),
                        )
                    stage = "login"
                    await smtp.login(self.address, self._password)
                    stage = "mail"
                    await smtp.mail(sender)
                    stage = "rcpt"
                    for rcpt in recipients:
                        await smtp.rcpt(rcpt)
            except aiosmtplib.SMTPAuthenticationError as exc:
                if exc.code in (534, 535):
                    return SmtpResult("auth", stage, exc.code)
                return SmtpResult("temporary", stage, exc.code)
            except aiosmtplib.SMTPResponseException as exc:
                if _is_quota(exc.code, exc.message):
                    return SmtpResult("flood", stage, exc.code, SMTP_QUOTA_RETRY_MS)
                if stage == "rcpt":
                    return SmtpResult(_code_outcome(exc.code), stage, exc.code)
                return SmtpResult("temporary", stage, exc.code)
            except Exception:  # noqa: BLE001 — до DATA тело не ушло: сеть/таймаут/прочее -> temporary
                return SmtpResult("temporary", stage)

            stage = "data"
            data_timeout = smtp_data_timeout(len(message), data_cap_s)
            try:
                async with asyncio.timeout(data_timeout):
                    await smtp.data(message, timeout=data_timeout)
            except aiosmtplib.SMTPDataError as exc:
                if _is_quota(exc.code, exc.message):
                    return SmtpResult("flood", stage, exc.code, SMTP_QUOTA_RETRY_MS)
                return SmtpResult(_code_outcome(exc.code), stage, exc.code)
            except Exception:  # noqa: BLE001 — обрыв/таймаут после начала DATA: исход неизвестен
                return SmtpResult("unknown", stage)
            return SmtpResult("sent", stage, 250)
        finally:
            if smtp is not None:
                await self._smtp_quit(smtp)
