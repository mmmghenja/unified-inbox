"""`PlainMailConnector` — обычная почта по IMAP/SMTP (вид ящика `flavor=imap`, первый — ящик Umbrella на
Timeweb). Спецификация: `docs/architecture.md`–8).

Источник тот же `mail`, что у Gmail (`connector.py`): беседы `mail_thread`, те же разбор письма (`textproc`),
сборка ответа (`convert.build_reply`), детерминированный Message-ID (`convert.our_message_id`), шлюз
`GmailGateway` в виде `imap` (хосты из конфига, без расширений Gmail). Отличия от Gmail:
- **Тред = корень цепочки** (решение 3): ключ треда — первый Message-ID из `References`, нет — `In-Reply-To`,
  нет — свой Message-ID; карта «Message-ID -> ключ» (файл состояния коннектора, до 50 000 записей) поднимает
  цепочки с урезанным `References`. Беседа — `"t:" + ключ без угловых скобок` (длиннее 300 — `"t:" + sha256`).
  Письма треда в ящике ищутся `UID SEARCH OR HEADER Message-ID <ключ> HEADER References <ключ>` в обеих
  папках; совпадение проверяется в Python (поиск по заголовку — подстрока). Сервер без поиска по
  `References` (mail.ru: `NO [CANNOT] Unsupported search criterion`) — письма треда по карте, каждое
  `HEADER Message-ID`; тогда на стартовой загрузке в карту пишутся и «Отправленные» за те же дни.
- **Две папки** (решение 4): `INBOX` и «Отправленные», у каждой свой курсор (`UIDVALIDITY` + последний UID).
  Письма «Отправленных» — свои (`is_me`; внутри им ставится метка `\\Sent`, как у Gmail, — тогда общие
  `is_mine`/`read_marker`/`thread_attrs` работают без правок). Отбор — как C3 Gmail: известный тред берёт
  любое письмо; новый открывает только входящее не моё письмо из `INBOX`; черновики не берутся никогда.
- **Прочитанность** `\\Seen` из `INBOX` -> хаб — окном последних 500 UID на каждой сверке; хаб -> `\\Seen`
  выключено (`read_ack_enabled=False`, в конфиге `read_ack=false`).
- **Отправка** (решение 5): родитель — `HEADER Message-ID` в `INBOX` и «Отправленных»; новое письмо (`new_to`,
  контракт Ф12-1) — без родителя, `peer_ext_id = "t:" + свой Message-ID`; после SMTP — `APPEND` копии в
  «Отправленные» с `\\Seen` (сбой — предупреждение, отправка остаётся `sent`).
- Трекеры, категории и метки Gmail — не для этого вида.

Приём, как у Gmail, — только через `sync()` с курсором в той же пачке (М1).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from ... import msgid as msgid_mod
from ..base import AuthRequired, SendResult, lease_files_meta
from ..media import MediaResult, MediaSink
from ..read_ack import ReadAckResult
from . import convert, notify_keys, textproc
from .connector import (
    LISTENER_DOWN_UNHEALTHY_S,
    MEDIA_FETCH_IMAP_S,
    PORTION,
    RECONCILE_BUDGET_S,
    SEND_IMAP_BUDGET_S,
    SEND_MAX_TEXT_LEN,
    SYNC_BUDGET_S,
    MailConnector,
    _same_field,
    parse_mail_config,
)
from .gateway import (
    FLAVOR_IMAP,
    SMTP_DATA_RATE_BPS,
    GmailGateway,
    ImapSession,
    MailAuthError,
    MailCommandError,
    MailConfigError,
    MailError,
    MailUnavailable,
    ServerInfo,
    describe,
)
from .imapwire import FetchedMsg, quote

_logger = logging.getLogger(__name__)

INBOX_TAG = "\\Inbox"
SENT_TAG = "\\Sent"
ROLES = (("inbox", INBOX_TAG), ("sent", SENT_TAG))

THREAD_MAP_MAX = 50_000
CHANNEL_EXT_MAX = 300
CHANNEL_PREFIX = "t:"
STARTUP_DAYS = 14
STARTUP_THREADS_MAX = 300
STARTUP_BATCH = 20
RESYNC_DAYS = 7
THREAD_LOAD_LAST = 30
THREAD_SEARCH_MAX = 200  # писем треда на папку (новейшие по UID) — метаданные для attrs и маркера
READ_WINDOW_UIDS = 500
READ_THREADS_PER_SYNC = 50
SEND_MAP_FALLBACK = 10  # родитель без `reply_to`: письма треда по карте, не найденные поиском по ключу
THREAD_MIDS_MAX = 50  # сервер без поиска по References: писем треда по карте (новейшие), каждое — поиском
REFS_PROBE = "hub-probe@references.invalid"  # пробный `SEARCH HEADER References` (раз за процесс)
APPEND_BUDGET_S = 20.0
_APPEND_REPORT_SLACK_S = 5.0  # Ф11-3b B4: копия кончается раньше срока `send()` — отчёт успевает уйти
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465

META_ITEMS = (
    "(UID FLAGS INTERNALDATE RFC822.SIZE "
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO FROM SUBJECT DATE)])"
)
PARENT_ITEMS = (
    "(UID FLAGS INTERNALDATE RFC822.SIZE "
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO SUBJECT FROM REPLY-TO TO CC DATE)])"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- конфиг -------------------------------------------------------------------------------------------------


def _port(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("порт — число")
    port = int(value)  # type: ignore[arg-type]
    if not 0 < port < 65536:  # noqa: PLR2004
        raise ValueError("порт вне 1…65535")
    return port


def parse_plain_config(config: dict | None) -> dict:
    """`account.config_json` обычного ящика -> нормализованный dict (поверх `parse_mail_config`): `flavor`
    обязан быть `imap`, хосты заданы. Ошибка -> `ValueError`."""
    cfg = parse_mail_config(config)
    raw = dict(config or {})
    if raw.get("flavor") != FLAVOR_IMAP:
        raise ValueError("config.flavor — не imap")
    imap_host = str(raw.get("imap_host") or "").strip()
    smtp_host = str(raw.get("smtp_host") or "").strip()
    if not imap_host or not smtp_host:
        raise ValueError("config.imap_host/smtp_host не заданы")
    cfg.update(
        flavor=FLAVOR_IMAP,
        imap_host=imap_host,
        imap_port=_port(raw.get("imap_port"), DEFAULT_IMAP_PORT),
        smtp_host=smtp_host,
        smtp_port=_port(raw.get("smtp_port"), DEFAULT_SMTP_PORT),
    )
    return cfg


# --- ключ треда (решение 3) ---------------------------------------------------------------------------------


def bracketed(mid: str) -> str:
    """Message-ID в одном виде — с угловыми скобками (в `References` они есть, в своём заголовке бывают
    без)."""
    return "<" + mid.strip().strip("<>") + ">"


def thread_key(own: str, references: list[str], in_reply_to: list[str], known: Mapping[str, str]) -> str:
    """Ключ треда письма: письмо уже в карте — его ключ; иначе ключ первого ИЗВЕСТНОГО письма из
    `References`/`In-Reply-To` (урезанный `References`); иначе первый Message-ID из `References`, нет —
    `In-Reply-To`, нет — свой. Все Message-ID — в виде `bracketed`."""
    hit = known.get(own)
    if hit:
        return hit
    for mid in (*references, *in_reply_to):
        hit = known.get(mid)
        if hit:
            return hit
    if references:
        return references[0]
    if in_reply_to:
        return in_reply_to[0]
    return own


def channel_ext_for_key(key: str) -> str:
    """`"t:" + ключ без угловых скобок`; длиннее 300 символов — `"t:" + sha256` ключа."""
    bare = key.strip("<>")
    ext = CHANNEL_PREFIX + bare
    if len(ext) > CHANNEL_EXT_MAX:
        ext = CHANNEL_PREFIX + hashlib.sha256(bare.encode("utf-8", errors="replace")).hexdigest()
    return ext


def synthetic_msgid(raw: bytes | None) -> int:
    """Письмо без Message-ID: устойчивое число из `From`/`Date`/`Subject`/`To` (одинаково из
    `BODY[HEADER.FIELDS …]` и из полного письма) — ext_id станет `<gm-N@hub>` (`convert.message_ext_id`)."""
    h = convert.raw_headers(raw)
    blob = "\n".join(" ".join((h.get(k) or "").split()) for k in ("from", "date", "subject", "to"))
    return int(hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:15], 16)


def letter_ext_id(m: FetchedMsg) -> str:
    return convert.ext_id_from_headers(m.header if m.header is not None else m.body, m.msgid)


def _searchable(term: str) -> bool:
    """Строку можно отдать в `SEARCH HEADER` (ASCII без переводов строк; кавычки экранирует `quote`)."""
    return bool(term) and term.isascii() and "\r" not in term and "\n" not in term


def imap_date(ts: float) -> str:
    """Дата для `SEARCH SINCE` (`1-Feb-1994`), месяц — по таблице, не через локаль."""
    d = datetime.fromtimestamp(ts, tz=UTC)
    return f"{d.day}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"


class ThreadMap(Mapping[str, str]):
    """Карта «Message-ID -> ключ треда» (решение 3) — в памяти и в файле состояния коннектора (0600,
    атомарно). Не больше `THREAD_MAP_MAX` записей: старые (по времени записи) вытесняются."""

    def __init__(self, path: Path | None, *, max_size: int = THREAD_MAP_MAX) -> None:
        self._path = path
        self._max = max_size
        self._map: dict[str, str] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            pairs = data.get("threads") or []
            for mid, key in pairs:
                if isinstance(mid, str) and isinstance(key, str):
                    self._map[mid] = key
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # карта — подсказка для урезанных цепочек, не источник истины: битый файл — начать заново
            _logger.warning("mail: файл состояния не прочитан (%s) — карта тредов с нуля", type(exc).__name__)
            self._map = {}
        while len(self._map) > self._max:
            self._map.pop(next(iter(self._map)))

    def __getitem__(self, mid: str) -> str:
        return self._map[mid]

    def __iter__(self):
        return iter(self._map)

    def __len__(self) -> int:
        return len(self._map)

    def put(self, mid: str, key: str) -> None:
        if self._map.get(mid) == key:
            return
        self._map.pop(mid, None)
        self._map[mid] = key
        while len(self._map) > self._max:
            self._map.pop(next(iter(self._map)))
        self.dirty = True

    def mids_of(self, key: str, limit: int) -> list[str]:
        """Последние записанные письма треда `key` (новейшие первыми)."""
        out: list[str] = []
        for mid in reversed(self._map):
            if self._map[mid] == key:
                out.append(mid)
                if len(out) >= limit:
                    break
        return out

    def key_for_channel(self, ext: str) -> str | None:
        """Беседа -> ключ треда: из самого `ext`, а для длинного (`sha256`) — поиском по карте."""
        if not ext.startswith(CHANNEL_PREFIX):
            return None
        bare = ext[len(CHANNEL_PREFIX) :]
        if not bare:
            return None
        if _SHA256_RE.fullmatch(bare):
            for key in reversed(self._map.values()):
                if (
                    channel_ext_for_key(key) == ext
                    and len(CHANNEL_PREFIX + key.strip("<>")) > CHANNEL_EXT_MAX
                ):
                    return key
        return bracketed(bare)

    def save(self) -> None:
        if self._path is None or not self.dirty:
            return
        path = self._path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "threads": list(self._map.items())}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        self.dirty = False


def _lid(m: FetchedMsg) -> tuple[str, int]:
    """UID уникален только внутри папки: письмо — (папка, UID)."""
    return (m.labels[0] if m.labels else "", m.uid)


def _order(m: FetchedMsg) -> tuple[int, str, int]:
    return (m.internaldate_ms or 0, m.labels[0] if m.labels else "", m.uid)


# --- коннектор ----------------------------------------------------------------------------------------------


class PlainMailConnector(MailConnector):
    """Обычный IMAP/SMTP. Общее с Gmail (`MailConnector`): жизненный цикл, слушатель, `_push`, лимитер SMTP,
    загрузчик вложений; своё — сверка, отправка, поиск писем."""

    read_ack_enabled = False  # решение 4: хаб -> `\\Seen` для этого вида выключено

    def __init__(
        self,
        client,
        gateway: GmailGateway,
        *,
        account_id: int,
        config: dict,
        state_path: str | Path | None = None,
        media_tmp_dir: str | Path | None = None,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(
            client, gateway, account_id=account_id, config=config, media_tmp_dir=media_tmp_dir, **base_kwargs
        )
        self._threads = ThreadMap(Path(state_path) if state_path is not None else None)
        self._plain_touched: set[str] = set()
        self._seen_cache: dict[int, bool] = {}  # INBOX: uid -> \Seen (окно последних 500)
        self._uid_keys: dict[int, str] = {}  # INBOX: uid -> ключ треда (для окна прочитанности)
        self._seen_uidvalidity: int | None = None
        self._seen_dirty: set[str] = set()
        self._plan: list[tuple[str, list[FetchedMsg]]] | None = None
        self._plan_empty: set[str] = set()
        # Ф84-1: ключи тредов, письма которых ушли в беседы `notify:…` (память процесса): беседы-треда нет,
        # но тред «известен» — не грузится заново целиком и не зацикливает стартовую загрузку
        self._notify_threads: set[str] = set()
        self._refs_ok: bool | None = None  # сервер ищет по `HEADER References` (None — ещё не проверено)

    # --- жизненный цикл (тексты без «gmail») ------------------------------------------------------------

    async def start_source(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self._gw.work.connect()
        except MailAuthError as exc:
            raise AuthRequired(f"imap: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"imap: ящик не годится ({exc})") from None
        except MailError as exc:
            _logger.warning("mail: старт без связи (%s)", describe(exc))
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error=describe(exc))
        self._gw.start_listener(self._listener_event_threadsafe)
        if self._media is not None:
            self._media_task = asyncio.ensure_future(self._media.run())

    async def health(self) -> None:
        if self._auth_failed is not None:
            raise AuthRequired(f"imap: вход не принят ({self._auth_failed})")
        down_since = self._gw.listener_down_since()
        if down_since is not None and self._wall() - down_since > LISTENER_DOWN_UNHEALTHY_S:
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error="imap_listener_down")
            raise ConnectionError("imap: слушатель не подключён дольше 2 мин")

    async def push_read(self, item: dict) -> ReadAckResult:
        """Решение 4: хаб -> `\\Seen` для этого вида выключено — ящик не трогаем, ошибок нет."""
        return ReadAckResult("drop", reason="read_ack_off")

    # --- папки и письма ---------------------------------------------------------------------------------

    @staticmethod
    async def _info(session: ImapSession) -> ServerInfo:
        return session.info if session.info is not None else await session.connect()

    async def _boxes(self, session: ImapSession) -> list[tuple[str, str]]:
        """[(метка, имя папки на сервере)] — `INBOX`, затем «Отправленные»."""
        info = await self._info(session)
        assert info.sent_mailbox is not None  # noqa: S101 — шлюз вида imap без «Отправленных» не подключается
        return [(INBOX_TAG, info.all_mailbox), (SENT_TAG, info.sent_mailbox)]

    @staticmethod
    def _prepare(msgs: list[FetchedMsg], tag: str) -> list[FetchedMsg]:
        for m in msgs:
            m.labels = [tag]
            if convert.raw_headers(m.header if m.header is not None else m.body).get("message-id") is None:
                m.msgid = synthetic_msgid(m.header if m.header is not None else m.body)
        return msgs

    async def _fetch(
        self, session: ImapSession, box: str, tag: str, uids: list[int], items: str = META_ITEMS
    ) -> list[FetchedMsg]:
        if not uids:
            return []
        wanted = set(uids)
        got = [m for m in await session.fetch_box(box, uids, items) if m.uid in wanted]
        return self._prepare(got, tag)

    def _key_of(self, m: FetchedMsg, *, record: bool = True) -> str:
        raw = m.header if m.header is not None else m.body
        h = convert.raw_headers(raw)
        own = bracketed(letter_ext_id(m))
        refs = [bracketed(x) for x in convert.msgid_list(h.get("references"))]
        irt = [bracketed(x) for x in convert.msgid_list(h.get("in-reply-to"))]
        key = thread_key(own, refs, irt, self._threads)
        if record:
            self._threads.put(own, key)
        return key

    async def _find_by_msgid(
        self, session: ImapSession, mid: str, *, items: str = PARENT_ITEMS
    ) -> list[FetchedMsg]:
        """Письма с этим Message-ID: `UID SEARCH HEADER Message-ID` в `INBOX`, затем в «Отправленных»;
        совпадение — точное, в Python (поиск по заголовку — подстрока)."""
        term = mid.strip().strip("<>")
        if not _searchable(term):
            return []
        want = bracketed(mid)
        out: list[FetchedMsg] = []
        for tag, box in await self._boxes(session):
            uids = await session.search_box(box, "HEADER", "Message-ID", quote(term))
            for m in await self._fetch(session, box, tag, uids[-20:], items):
                if bracketed(letter_ext_id(m)) == want:
                    out.append(m)
        return out

    async def _refs_search(self, session: ImapSession) -> bool:
        """Ищет ли сервер по `HEADER References` (mail.ru — нет). Проверяется раз за процесс пробным поиском;
        отказ посреди работы переключает так же (`_thread_meta`)."""
        if self._refs_ok is None:
            _tag, box = (await self._boxes(session))[0]
            try:
                await session.search_box(box, "HEADER", "References", quote(REFS_PROBE))
            except MailCommandError:
                self._no_refs_search()
            else:
                self._refs_ok = True
        return bool(self._refs_ok)

    def _no_refs_search(self) -> None:
        if self._refs_ok is not False:
            _logger.warning("mail: сервер не ищет по References — письма треда ищутся по карте и Message-ID")
        self._refs_ok = False

    async def _thread_meta(
        self, key: str, *, session: ImapSession | None = None, items: str = META_ITEMS
    ) -> list[FetchedMsg]:
        """Письма треда `key` в обеих папках: `OR HEADER Message-ID <ключ> HEADER References <ключ>` ->
        метаданные -> только те, чей ключ (по карте) и есть `key`. Сервер без поиска по References —
        `_thread_meta_by_map`."""
        session = session or self._gw.work
        term = key.strip("<>")
        if not _searchable(term):
            return []
        if not await self._refs_search(session):
            return await self._thread_meta_by_map(session, key, items)
        out: list[FetchedMsg] = []
        for tag, box in await self._boxes(session):
            try:
                uids = await session.search_box(
                    box, "OR", "HEADER", "Message-ID", quote(term), "HEADER", "References", quote(term)
                )
            except MailCommandError:
                self._no_refs_search()
                return await self._thread_meta_by_map(session, key, items)
            metas = await self._fetch(session, box, tag, uids[-THREAD_SEARCH_MAX:], items)
            out.extend(m for m in metas if self._key_of(m) == key)
        return out

    async def _thread_meta_by_map(self, session: ImapSession, key: str, items: str) -> list[FetchedMsg]:
        """Сервер без поиска по References: письма треда — сам ключ и последние `THREAD_MIDS_MAX` писем треда
        из карты (её пополняет каждое письмо догона и стартовой загрузки), каждое — `HEADER Message-ID` в
        обеих папках. Письма треда старше карты (до первого запуска и вне стартовой загрузки) не видны."""
        found: dict[tuple[str, int], FetchedMsg] = {}
        for mid in dict.fromkeys([key, *self._threads.mids_of(key, THREAD_MIDS_MAX)]):
            for m in await self._find_by_msgid(session, mid, items=items):
                if _lid(m) not in found and self._key_of(m) == key:
                    found[_lid(m)] = m
        return sorted(found.values(), key=_order)

    def _opens(self, m: FetchedMsg) -> bool:
        """Письмо открывает НОВУЮ беседу: входящее из `INBOX`, не черновик, не моё (как C3 Gmail)."""
        return INBOX_TAG in m.labels and not m.is_draft and not self._meta_is_mine(m)

    @staticmethod
    def _read_event(ext: str, marker: str) -> dict:  # type: ignore[override]
        return {
            "type": "read",
            "channel": {"ext_id": ext, "kind": convert.KIND, "own_id_space": True},
            "up_to_ext_id": marker,
        }

    # --- курсор ---------------------------------------------------------------------------------------------

    @staticmethod
    def _load_plain_cursor(raw: dict | None) -> dict | None:
        if not raw or not raw.get("value"):
            return None
        try:
            value = json.loads(raw["value"])
        except ValueError, TypeError:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("boxes"), dict):
            return None
        return value

    @staticmethod
    def _with_box(cursor: dict, role: str, uidvalidity: int, last_uid: int) -> dict:
        boxes = dict(cursor.get("boxes") or {})
        boxes[role] = {"uidvalidity": int(uidvalidity), "last_uid": int(last_uid)}
        return dict(cursor, boxes=boxes)

    # --- сверка ---------------------------------------------------------------------------------------------

    async def sync(self, reason: str = "scheduled") -> None:
        deadline = self._mono() + SYNC_BUDGET_S
        self._plain_touched = set()
        try:
            session = self._gw.work
            boxes = dict(zip(("inbox", "sent"), await self._boxes(session), strict=True))
            states = {role: await session.examine_box(box) for role, (_tag, box) in boxes.items()}
            heads = await self._client.channels_heads()
            self._known = {h["ext_id"] for h in heads}
            cursor = self._load_plain_cursor(await self._client.get_cursor())

            if cursor is None:
                cursor = {"flavor": FLAVOR_IMAP, "boxes": {}, "startup": {"days": STARTUP_DAYS}}
                for role, st in states.items():
                    cursor = self._with_box(cursor, role, st.uidvalidity, st.last_uid)
                await self._push([], cursor)
                _logger.info("mail: первый запуск — стартовая загрузка за %d дн.", STARTUP_DAYS)
            else:
                for role, (tag, box) in boxes.items():
                    if self._mono() > deadline:
                        break
                    st = states[role]
                    box_cur = (cursor.get("boxes") or {}).get(role)
                    if not isinstance(box_cur, dict) or "uidvalidity" not in box_cur:
                        cursor = self._with_box(cursor, role, st.uidvalidity, st.last_uid)
                        await self._push([], cursor)
                    elif int(box_cur["uidvalidity"]) != st.uidvalidity:
                        _logger.warning(
                            "mail: UIDVALIDITY папки %s сменился — пересинхронизация за %d дн.",
                            role,
                            RESYNC_DAYS,
                        )
                        cursor = await self._resync_box(role, tag, box, st, cursor)
                    else:
                        cursor = await self._catch_up_box(role, tag, box, cursor, deadline)

            if self._mono() > deadline:
                self._request_sync(reason)
                return
            await self._sync_seen(cursor, boxes["inbox"][1], states["inbox"].uidvalidity)
            if cursor.get("startup") and self._mono() <= deadline:
                await self._startup_batch_plain(cursor, boxes["inbox"])
        except MailAuthError as exc:
            raise AuthRequired(f"imap: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"imap: ящик не годится ({exc})") from None
        except MailUnavailable as exc:
            _logger.warning("mail: сверка %s прервана — нет связи (%s)", reason, describe(exc))
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error=describe(exc))
        finally:
            try:
                self._threads.save()
            except OSError as exc:
                _logger.warning("mail: файл состояния не записан (%s)", type(exc).__name__)

    async def _catch_up_box(self, role: str, tag: str, box: str, cursor: dict, deadline: float) -> dict:
        box_cur = cursor["boxes"][role]
        uids = await self._gw.work.search_new_box(box, int(box_cur["last_uid"]))
        for i in range(0, len(uids), PORTION):
            if self._mono() > deadline:
                self._request_sync("scheduled")
                break
            portion = uids[i : i + PORTION]
            metas = await self._fetch(self._gw.work, box, tag, portion)
            events = await self._process_letters(metas)
            new_cursor = self._with_box(cursor, role, int(box_cur["uidvalidity"]), max(portion))
            await self._push(events, new_cursor)
            cursor = new_cursor
        return cursor

    async def _resync_box(self, role: str, tag: str, box: str, state, cursor: dict) -> dict:
        """`UIDVALIDITY` папки сменился: письма за 7 дней — тем же отбором, что догон (повтор upsert — no-op
        ядра, дублей нет), затем курсор папки с новым `UIDVALIDITY` и последним UID, взятым ДО
        пересинхронизации."""
        since = imap_date(self._wall() - RESYNC_DAYS * 86400)
        found = await self._gw.work.search_box(box, "SINCE", since)
        uids = [u for u in found if u <= state.last_uid]
        for i in range(0, len(uids), PORTION):
            metas = await self._fetch(self._gw.work, box, tag, uids[i : i + PORTION])
            await self._push(await self._process_letters(metas))
        if role == "inbox":
            self._seen_cache, self._uid_keys = {}, {}
        new_cursor = self._with_box(cursor, role, state.uidvalidity, state.last_uid)
        await self._push([], new_cursor)
        return new_cursor

    async def _process_letters(self, metas: list[FetchedMsg]) -> list[dict]:
        """Отбор по тредам (как C3/М4/М5 Gmail): известный тред — любое письмо; новый — только если в нём
        есть входящее не моё письмо из `INBOX` (тогда тред грузится целиком); черновики — никогда."""
        groups: dict[str, list[FetchedMsg]] = {}
        for m in metas:
            key = self._key_of(m)
            if m.is_draft:
                continue
            groups.setdefault(key, []).append(m)
        events: list[dict] = []
        for key in sorted(groups, key=lambda k: min(_order(m) for m in groups[k])):
            msgs = groups[key]
            if channel_ext_for_key(key) in self._known or key in self._notify_threads:
                events.extend(await self._thread_events(key, portion=msgs, full=False))
            elif any(self._opens(m) for m in msgs):
                events.extend(await self._thread_events(key, portion=msgs, full=True))
        return events

    async def _thread_events(self, key: str, *, portion: list[FetchedMsg], full: bool) -> list[dict]:  # type: ignore[override]
        """События треда: письма порции (при `full` — ещё последние 30 писем треда из обеих папок), `attrs`
        по всему треду, маркер прочтения."""
        ext = channel_ext_for_key(key)
        by_id: dict[tuple[str, int], FetchedMsg] = {_lid(m): m for m in await self._thread_meta(key)}
        for m in portion:
            by_id.setdefault(_lid(m), m)
        live = sorted((m for m in by_id.values() if not m.is_draft), key=_order)
        emit = {_lid(m) for m in portion if not m.is_draft}
        if full:
            emit |= {_lid(m) for m in live[-THREAD_LOAD_LAST:]}
        if not emit:
            return []

        bodies: dict[tuple[str, int], FetchedMsg] = {}
        for tag, box in await self._boxes(self._gw.work):
            sized = [(uid, by_id[(t, uid)].size) for (t, uid) in sorted(emit) if t == tag]
            if sized:
                for b in await self._gw.work.fetch_bodies(sized, box=box):
                    bodies[(tag, b.uid)] = b
        merged: list[FetchedMsg] = []
        for lid in emit:
            body = bodies.get(lid)
            if body is None:
                continue  # письмо исчезло между командами — следующая сверка разберётся
            base = by_id[lid]
            base.body, base.body_partial = body.body, body.body_partial
            merged.append(base)
        merged.sort(key=_order)
        if not merged:
            return []

        # Ф84-1: письма с доменов правил `notify_keys` с найденным ключом — в беседы `notify:<name>:<key>`
        routed: dict[tuple[str, int], list[dict]] = {}
        thread_meta = list(by_id.values())
        for m in merged:
            notify_events = self._notify_letter_events(m, thread_meta)
            if notify_events:
                routed[_lid(m)] = notify_events
        regular = [m for m in merged if _lid(m) not in routed]

        is_new = ext not in self._known
        title = convert.thread_title(regular[0].body) if (is_new and regular) else None
        ref: dict = {"ext_id": ext, "kind": convert.KIND, "own_id_space": True}
        if title is not None:
            ref["title"] = title
        ref["attrs"] = convert.thread_attrs(list(by_id.values()))
        thread_msgids = {letter_ext_id(m) for m in live}
        events: list[dict] = []
        for m in merged:
            notify_events = routed.get(_lid(m))
            if notify_events is not None:
                events.extend(notify_events)
                self._known.update(e["channel"]["ext_id"] for e in notify_events)
                self._notify_threads.add(key)
                m.body = None
                continue
            try:
                events.append(convert.letter_event(m, me=self._me, channel=ref, thread_msgids=thread_msgids))
            except Exception as exc:  # noqa: BLE001 — одно письмо не роняет порцию; текст в журнал не идёт
                _logger.error("mail: письмо не разобрано (uid=%s, %s) — заглушка", m.uid, type(exc).__name__)
                events.append(convert.stub_event(m, me=self._me, channel=ref))
            m.body = None  # тела уже в событиях — не держать в памяти (план стартовой загрузки)
        self._plain_touched.add(key)
        if not regular and is_new:
            return events  # все письма ушли в беседы по ключу — беседу-тред не заводим
        marker = convert.read_marker([m for m in live if _lid(m) not in routed], self._me)
        if marker is not None:
            events.append(self._read_event(ext, marker))
        self._known.add(ext)
        return events

    def _notify_letter_events(self, m: FetchedMsg, thread_msgs: list[FetchedMsg]) -> list[dict] | None:
        """Ф84-1: письмо -> события в беседу по ключу; `None` — обычное письмо (в том числе при сбое разбора:
        текст не теряется, в журнал — только тип ошибки)."""
        if not self._notify:
            return None
        try:
            parts = notify_keys.route(m, self._notify, thread_msgs=thread_msgs, known=self._known)
            if not parts:
                return None
            return [convert.tracker_event(m, p, me=self._me) for p in parts]
        except Exception as exc:  # noqa: BLE001 — текст письма в журнал не идёт
            _logger.error("mail: письмо по ключу не разобрано (uid=%s, %s)", m.uid, type(exc).__name__)
            return None

    # --- прочитанность: \Seen из INBOX -> хаб ---------------------------------------------------------------

    async def _sync_seen(self, cursor: dict, inbox: str, uidvalidity: int) -> None:
        """Решение 4: окно последних 500 UID `INBOX` (без CONDSTORE) против памяти процесса; тред хаба, где
        флаг сменился, — маркер прочтения заново (не больше 50 тредов за сверку)."""
        if self._seen_uidvalidity != uidvalidity:
            self._seen_cache, self._uid_keys = {}, {}
            self._seen_uidvalidity = uidvalidity
        last_uid = int(((cursor.get("boxes") or {}).get("inbox") or {}).get("last_uid") or 0)
        session = self._gw.work
        if last_uid > 0:
            window = await session.fetch_range_box(
                inbox, last_uid - READ_WINDOW_UIDS + 1, last_uid, "(UID FLAGS)"
            )
            current = {m.uid: "\\Seen" in m.flags for m in window if m.uid <= last_uid}
            changed = [u for u, seen in current.items() if self._seen_cache.get(u) != seen]
            self._seen_cache = current
            need = [u for u in changed if u not in self._uid_keys]
            for m in await self._fetch(session, inbox, INBOX_TAG, need):
                self._uid_keys[m.uid] = self._key_of(m)
            self._uid_keys = {u: k for u, k in self._uid_keys.items() if u in current}
            for u in changed:
                key = self._uid_keys.get(u)
                if (
                    key is not None
                    and key not in self._plain_touched
                    and channel_ext_for_key(key) in self._known
                ):
                    self._seen_dirty.add(key)

        batch = sorted(self._seen_dirty)[:READ_THREADS_PER_SYNC]
        events: list[dict] = []
        for key in batch:
            self._seen_dirty.discard(key)
            live = [m for m in await self._thread_meta(key) if not m.is_draft]
            marker = convert.read_marker(live, self._me)
            if marker is not None:
                events.append(self._read_event(channel_ext_for_key(key), marker))
        if self._seen_dirty:
            self._request_sync("scheduled")
        if events:
            await self._push(events)

    # --- стартовая загрузка ---------------------------------------------------------------------------------

    async def _startup_plan(self, days: int, inbox: tuple[str, str]) -> list[tuple[str, list[FetchedMsg]]]:
        """Треды писем `INBOX` за `days` дней, новейшие `STARTUP_THREADS_MAX` (считается раз за процесс;
        рестарт посреди — кандидаты снова те, которых нет в ядре)."""
        if self._plan is None:
            tag, box = inbox
            uids = await self._gw.work.search_box(box, "SINCE", imap_date(self._wall() - days * 86400))
            groups: dict[str, list[FetchedMsg]] = {}
            for m in await self._fetch(self._gw.work, box, tag, uids):
                key = self._key_of(m)
                if not m.is_draft:
                    groups.setdefault(key, []).append(m)
            if not await self._refs_search(self._gw.work):
                # треды ищутся только по карте: мои ответы за те же дни — в карту (бесед не открывают)
                sent_tag, sent_box = (await self._boxes(self._gw.work))[1]
                since = imap_date(self._wall() - days * 86400)
                sent = await self._gw.work.search_box(sent_box, "SINCE", since)
                for m in await self._fetch(self._gw.work, sent_box, sent_tag, sent):
                    self._key_of(m)
            ordered = sorted(groups, key=lambda k: max(_order(m) for m in groups[k]), reverse=True)
            self._plan = [(k, groups[k]) for k in ordered[:STARTUP_THREADS_MAX]]
        return self._plan

    async def _startup_batch_plain(self, cursor: dict, inbox: tuple[str, str]) -> None:
        days = int((cursor.get("startup") or {}).get("days", STARTUP_DAYS))
        plan = await self._startup_plan(days, inbox)
        candidates = [
            (k, ms)
            for k, ms in plan
            if channel_ext_for_key(k) not in self._known
            and k not in self._notify_threads
            and k not in self._plan_empty
            and any(map(self._opens, ms))
        ]
        if candidates:
            with contextlib.suppress(Exception):
                await self._client.set_activity(backfill=True)
            for key, msgs in candidates[:STARTUP_BATCH]:
                events = await self._thread_events(key, portion=msgs, full=True)
                if not any(e["type"] == "upsert" for e in events):
                    self._plan_empty.add(key)
                    continue
                await self._push(events)
            if len(candidates) > STARTUP_BATCH:
                self._request_sync("initial")
                return
        finished = {k: v for k, v in cursor.items() if k != "startup"}
        await self._push([], finished)
        self._plan = None
        with contextlib.suppress(Exception):
            await self._client.set_activity(backfill=False)
        _logger.info("mail: стартовая загрузка закончена")

    # --- отправка -------------------------------------------------------------------------------------------

    async def _find_own_plain(self, our_mid: str) -> bool:
        return bool(await self._find_by_msgid(self._gw.sender, our_mid))

    async def _parent_candidates(self, key: str) -> list[FetchedMsg]:
        """Родитель без `reply_to`: письма треда (поиск по ключу) + по карте — последние записанные письма
        треда, которых поиск по ключу не нашёл (урезанный `References`)."""
        found = await self._thread_meta(key, session=self._gw.sender, items=PARENT_ITEMS)
        if self._refs_ok is False:
            return found  # поиск уже шёл по карте (`_thread_meta_by_map`, новейшие письма треда)
        have = {bracketed(letter_ext_id(m)) for m in found}
        for mid in self._threads.mids_of(key, SEND_MAP_FALLBACK):
            if mid not in have:
                found.extend(await self._find_by_msgid(self._gw.sender, mid))
        return found

    async def _append_copy(self, message: bytes, deadline: float | None = None) -> None:
        """Решение 5: копия в «Отправленные» с `\\Seen`; сбой — предупреждение, отправка остаётся `sent`.
        Ф11-3b B4: бюджет — `APPEND_BUDGET_S + len / SMTP_DATA_RATE_BPS`; у письма с вложениями (`deadline` —
        срок `send()`) — только в остатке срока за вычетом запаса на отчёт; остатка меньше `APPEND_BUDGET_S` —
        копия не кладётся (предупреждение, исход `sent`)."""
        budget = APPEND_BUDGET_S + len(message) / SMTP_DATA_RATE_BPS
        if deadline is not None:
            remaining = deadline - self._mono()
            if remaining < APPEND_BUDGET_S:
                _logger.warning("mail: на копию в «Отправленные» не осталось времени — отправка засчитана")
                return
            budget = min(budget, remaining - _APPEND_REPORT_SLACK_S)
        try:
            async with asyncio.timeout(budget):
                info = await self._info(self._gw.sender)
                assert info.sent_mailbox is not None  # noqa: S101
                await self._gw.sender.append_sent(info.sent_mailbox, message, limit_s=budget)
        except Exception as exc:  # noqa: BLE001 — письмо уже ушло; копия — удобство веб-почты
            _logger.warning("mail: копия в «Отправленные» не легла (%s) — отправка засчитана", describe(exc))

    async def _smtp(
        self,
        lease: dict,
        recipients: list[str],
        message: bytes,
        ok: SendResult,
        *,
        deadline: float | None = None,
    ) -> SendResult:
        await self._wait_send_slot()
        result = await self._gw.smtp_send(
            sender=self._me.address,
            recipients=recipients,
            message=message,
            data_cap_s=self._data_cap(deadline),
        )
        if result.outcome == "sent":
            await self._append_copy(message, deadline)
            return ok
        if result.outcome == "auth":
            raise await self._auth_after_report(lease, "smtp: вход не принят")
        if result.outcome == "flood":
            return SendResult("flood", retry_after_ms=result.retry_after_ms, error="smtp: суточная квота")
        if result.outcome == "unknown":
            _logger.warning("mail: обрыв после DATA — исход неизвестен (сверка решит)")
            return SendResult("unknown")
        error = result.error or f"smtp {result.stage}: {result.code or 'сеть'}"
        return SendResult(result.outcome, error=error)

    def _recipients_of(self, new_to: dict) -> list[str] | None:
        raw = new_to.get("to")
        values = [raw] if isinstance(raw, str) else [str(v) for v in (raw or []) if v]
        out: list[str] = []
        for _name, addr in getaddresses(values):
            addr = addr.strip().lower()
            if not addr or "@" not in addr or " " in addr:
                return None
            if addr not in out:
                out.append(addr)
        return out or None

    async def send(self, lease: dict) -> SendResult:
        started = self._mono()
        if not self._send_enabled:
            return SendResult(
                "permanent",
                error=(
                    "ящик подключён только на приём — включи отправку: "
                    f"hub account set {self._account_id} send_enabled=true"
                ),
            )
        files = lease_files_meta(lease)
        text = (lease.get("text") or "").strip()
        if not text and not files:  # Ф11-3b B4: письмо только с вложениями — можно
            return SendResult("permanent", error="пустое сообщение")
        if len(text) > SEND_MAX_TEXT_LEN:
            return SendResult("permanent", error="слишком длинное письмо")
        our_mid = self._our_msgid(lease)
        new_to = lease.get("new_to")
        if new_to is not None:
            return await self._send_new(lease, new_to, text, our_mid, started=started)

        ext = str(lease.get("channel_ext_id") or "")
        if not ext.startswith(CHANNEL_PREFIX):
            return SendResult("permanent", error="беседа — не тред этого ящика")
        key = self._threads.key_for_channel(ext)
        reply_to = lease.get("reply_to_ext_id")
        try:
            async with asyncio.timeout(SEND_IMAP_BUDGET_S):
                if lease.get("maybe_sent") and await self._find_own_plain(our_mid):
                    return SendResult("sent", ext_id=our_mid)
                if reply_to:
                    candidates = await self._find_by_msgid(self._gw.sender, str(reply_to))
                elif key is not None:
                    candidates = await self._parent_candidates(key)
                else:
                    candidates = []
        except TimeoutError:
            return SendResult("temporary", error="imap: не уложились в 15 с")
        except MailAuthError as exc:
            raise await self._auth_after_report(lease, f"imap: вход не принят ({exc.kind})") from None
        except MailError as exc:
            return SendResult("temporary", error=f"imap: {describe(exc)}")

        candidates = [m for m in candidates if not m.is_draft]  # родитель — никогда не черновик
        parent_msg = max(candidates, key=_order, default=None)
        if parent_msg is None:
            return SendResult("permanent", error="письмо-родитель не найдено в ящике")
        parent = convert.reply_parent(parent_msg, self._me)
        recipients = convert.reply_recipients(parent)
        if not recipients:
            return SendResult("permanent", error="у письма-родителя нет получателя для ответа")
        if parent.reply_to_addrs and parent.from_addrs and parent.reply_to_addrs[0] != parent.from_addrs[0]:
            _logger.info("mail: у родителя Reply-To отличается от From — отвечаем на From (М14)")
        now_ts = self._wall()
        message = await self._letter(
            lease,
            files,
            lambda attachments: convert.build_reply(
                parent=parent,
                recipients=recipients,
                text=text,
                address=self._me.address,
                display_name=self._display_name,
                message_id=our_mid,
                now_ts=now_ts,
                attachments=attachments,
            ),
        )
        if isinstance(message, SendResult):
            return message
        return await self._smtp(
            lease,
            recipients,
            message,
            SendResult("sent", ext_id=our_mid),
            deadline=self._send_deadline(started, files),
        )

    async def _send_new(
        self, lease: dict, new_to: object, text: str, our_mid: str, *, started: float | None = None
    ) -> SendResult:
        """Новое письмо (контракт Ф12-1: `new_to = {"kind": "email", "to", "subject", "from"}`) — без
        родителя; беседа известна сразу: `peer_ext_id = "t:" + свой Message-ID`, поиск копии не нужен.
        Ф12-5 B1: с файлами задания — письмо с вложениями той же сборкой, что ответ (`_letter`; `SIZE`, срок
        `data()` и копия `APPEND` в «Отправленные» — в остатке срока задания, как у ответа Ф11-3b B4)."""
        if started is None:
            started = self._mono()
        files = lease_files_meta(lease)
        if not isinstance(new_to, dict) or new_to.get("kind") != "email":
            return SendResult("permanent", error="новое письмо: адресат не почтовый")
        recipients = self._recipients_of(new_to)
        if recipients is None:
            return SendResult("permanent", error="новое письмо: нет адреса получателя")
        subject = new_to.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            return SendResult("permanent", error="тема письма не годится")
        problem = msgid_mod.subject_problem(subject)  # та же проверка, что в ядре и у Gmail (ревью Ф12-1)
        if problem is not None:
            return SendResult("permanent", error=problem)
        sender = self._me.address
        if new_to.get("from"):
            parsed = [a.strip().lower() for _n, a in getaddresses([str(new_to.get("from"))])]
            if not parsed or parsed[0] not in self._me.all:
                return SendResult("permanent", error="новое письмо: адрес отправителя — не этого ящика")
            sender = parsed[0]
        peer = channel_ext_for_key(bracketed(our_mid))
        ok = SendResult("sent", ext_id=our_mid, peer_ext_id=peer)
        if lease.get("maybe_sent"):
            try:
                async with asyncio.timeout(SEND_IMAP_BUDGET_S):
                    if await self._find_own_plain(our_mid):
                        return ok
            except TimeoutError:
                return SendResult("temporary", error="imap: не уложились в 15 с")
            except MailAuthError as exc:
                raise await self._auth_after_report(lease, f"imap: вход не принят ({exc.kind})") from None
            except MailError as exc:
                return SendResult("temporary", error=f"imap: {describe(exc)}")
        now_ts = self._wall()
        message = await self._letter(
            lease,
            files,
            lambda attachments: convert.build_letter(
                recipients=recipients,
                subject=subject.strip(),
                text=text,
                address=sender,
                display_name=self._display_name,
                message_id=our_mid,
                now_ts=now_ts,
                attachments=attachments,
            ),
        )
        if isinstance(message, SendResult):
            return message
        return await self._smtp(lease, recipients, message, ok, deadline=self._send_deadline(started, files))

    async def reconcile(self, lease: dict) -> SendResult:
        """Свой Message-ID в «Отправленных»/`INBOX` — `sent`, нет — `unknown`. Ничего не отправляет
        никогда."""
        our_mid = self._our_msgid(lease)
        peer = channel_ext_for_key(bracketed(our_mid)) if lease.get("new_to") is not None else None
        try:
            async with asyncio.timeout(RECONCILE_BUDGET_S):
                if await self._find_own_plain(our_mid):
                    return SendResult("sent", ext_id=our_mid, peer_ext_id=peer)
        except MailAuthError as exc:
            raise AuthRequired(f"imap: вход не принят ({exc.kind})") from None
        except Exception as exc:  # noqa: BLE001 — сверка не уверена -> unknown (решает владелец)
            _logger.warning("mail: сверка отправки не удалась (%s)", describe(exc))
        return SendResult("unknown")

    # --- вложения (Ф11-3a, решение 6) -----------------------------------------------------------------------

    async def fetch_media(self, job: dict, sink: MediaSink) -> MediaResult:
        """Как у Gmail, но письмо ищется `HEADER Message-ID` в `INBOX`, затем в «Отправленных»."""
        mid = str((job.get("message") or {}).get("ext_id") or "")
        idx = job.get("idx")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
            return MediaResult("gone", error="bad_ref")
        if not mid.strip("<>") or not convert.SAFE_MSGID_RE.match(mid):
            return MediaResult("gone", error="bad_message_id")
        max_bytes = job.get("max_bytes")
        session = self._gw.work
        try:
            found = await self._find_by_msgid(session, mid, items=META_ITEMS)
            if not found:
                return MediaResult("gone", error="no_message")
            letter = found[0]
            if isinstance(max_bytes, int) and isinstance(letter.size, int) and letter.size > max_bytes:
                return MediaResult("too_big", error="message_size", size=letter.size)
            box = dict(await self._boxes(session))[letter.labels[0]]
            fetched = await session.fetch_full(letter.uid, limit_s=MEDIA_FETCH_IMAP_S, box=box)
        except MailAuthError as exc:
            raise AuthRequired(f"imap: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"imap: ящик не годится ({exc})") from None
        except MailError as exc:
            return MediaResult("temporary", error=type(exc).__name__)
        if fetched is None or fetched.body is None:
            return MediaResult("gone", error="no_message")
        try:
            got = await asyncio.to_thread(textproc.find_attachment, fetched.body, idx)
        except Exception as exc:  # noqa: BLE001 — кривое письмо: вложение не достать; текст в журнал не идёт
            return MediaResult("gone", error=type(exc).__name__)
        if got is None:
            return MediaResult("gone", error="no_attachment")
        info, data = got
        if not (_same_field(info["name"], job.get("name")) and _same_field(info["mime"], job.get("mime"))):
            return MediaResult("gone", error="attachment_changed")
        sink.write(data)
        return MediaResult("ready")

    # --- Ф15-1 C2: реакция на письмо (переопределения поиска и копии) ---------------------------------------

    async def _reaction_target(self, job: dict, target_mid: str) -> FetchedMsg | None:
        """Письмо-цель: `HEADER Message-ID` в `INBOX` и «Отправленных» (как родитель ответа) — с `List-Id`."""
        del job
        found = await self._find_by_msgid(self._gw.sender, target_mid, items=REACTION_TARGET_ITEMS)
        found = [m for m in found if not m.is_draft]
        return found[0] if found else None

    async def _reaction_sent(self, our_mid: str) -> bool:
        """П2: своё письмо-реакция уже есть в «Отправленных» (или во входящих) — SMTP не нужен."""
        return await self._find_own_plain(our_mid)

    async def _reaction_after_smtp(self, message: bytes) -> None:
        """Решение 5 Ф12-4: копия в «Отправленные» (по ней повтор найдёт письмо и не отправит второе)."""
        await self._append_copy(message)


# Ф15-1 C2: заголовки письма-цели реакции — как `PARENT_ITEMS`, плюс `List-Id` (ограничение Gmail).
REACTION_TARGET_ITEMS = (
    "(UID FLAGS INTERNALDATE RFC822.SIZE "
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO SUBJECT FROM REPLY-TO TO CC DATE LIST-ID)])"
)
