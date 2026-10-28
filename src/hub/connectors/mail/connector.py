"""`MailConnector(ConnectorBase)` — почта Gmail: приём (IMAP IDLE + догон по курсору), прочитанность по
`\\Seen`, стартовая загрузка, ответ в тред через SMTP. Спецификация: `docs/architecture.md`, E и поправки М1–М15 — они
главнее текста выше них).

Сеть — только через `GmailGateway` (`gateway.py`); здесь — смысл: что брать (C3), курсор (C2), прочтение
(C4), стартовая загрузка (C5), отправка и её исходы (E1–E5).

Приём идёт ТОЛЬКО через `sync()` (М1): каждая порция (≤ 100 писем) уходит прямым `push_events` вместе с
курсором в той же пачке — ядро пишет события и курсор одной транзакцией; ошибка -> исключение, курсор в ядре
не сдвинут, следующая сверка повторит (повтор — no-op ядра). `emit()` почта не использует: буфер каркаса при
переполнении очищается, курсор ушёл бы вперёд потерянных писем. Слушатель IDLE только заказывает сверку.

`idempotent = False`: у SMTP нет идемпотентности; повтор вслепую не делаем никогда — исход, где письмо МОГЛО
уйти, решает поиск своего Message-ID в ящике (сверка), не повтор.

Ф10-5 (часть A, `docs/architecture.md`): уведомления Jira/Trello (`trackers.py`) уходят в
беседы задач/карточек, а не в беседу-тред Gmail; связь «тред Gmail -> беседы трекера» коннектор помнит в
памяти процесса (`_tracker_threads`) — чтобы тред трекера не грузился целиком и не создавал беседу треда.
Прочитанность бесед трекера из Gmail в хаб НЕ переносится (ревью Ф10-5, находки 2–3: тред Gmail ≠ карточка,
маркер перескакивал бы непрочитанное); из хаба в Gmail — `push_read`. Письма трекеров за 30 дней из
старых бесед-тредов один раз пересортировываются (`_resort_batch`, ключ курсора `trackers`).

Ф11-3a (`docs/architecture.md`): загрузчик медиа — вложения писем по клику владельца. Письмо
ищется по Message-ID в «Вся почта» рабочим соединением (`EXAMINE` + `BODY.PEEK`, флаги не трогаются),
вложение — тем же обходом MIME, что у конвертера (`textproc.attachment_parts`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ... import msgid as msgid_mod
from ..base import (
    _AUTH_EXIT_CODE,
    FILE_SEND_TIMEOUT_S,
    AuthRequired,
    ConnectorBase,
    OutboxFileError,
    SendResult,
    lease_files_meta,
)
from ..media import MediaDownloader, MediaResult, MediaSink
from ..reactions import ReactionResult
from ..read_ack import ReadAckResult
from . import convert, notify_keys, textproc, trackers
from .convert import DEFAULT_EXCLUDE_CATEGORIES, MeInfo
from .gateway import (
    GmailGateway,
    MailAuthError,
    MailConfigError,
    MailError,
    MailUnavailable,
    ServerInfo,
    describe,
)
from .imapwire import FetchedMsg

_logger = logging.getLogger(__name__)

PORTION = 100  # C2: письма догона порциями по 100
PUSH_CHUNK = 400  # < 500 событий на пачку ядра
THREAD_LOAD_LAST = 30  # C5/М5: не больше 30 последних писем треда
STARTUP_THREADS_MAX = 300  # C5/М14: новейшие 300 тредов
STARTUP_BATCH = 20  # C5: порциями по 20 тредов за сверку
STARTUP_DAYS = 14
RESYNC_DAYS = 7
READ_WINDOW_UIDS = 500  # C4 без CONDSTORE
READ_THREADS_PER_SYNC = 50
SYNC_BUDGET_S = 450.0  # < таймаута сверки каркаса (600 с)
LISTENER_DOWN_UNHEALTHY_S = 120.0  # М13
SEND_IMAP_BUDGET_S = 15.0  # М2: IMAP-фаза send()
RECONCILE_BUDGET_S = 40.0  # < 60 с каркаса
SEND_MAX_TEXT_LEN = 100_000
# Ф11-3b B4: письмо с вложениями — `data()` (и копия `APPEND` у `plain`) не дальше срока `send()` минус
# запас на отчёт (срок — `FILE_SEND_TIMEOUT_S` от начала `send`).
SEND_DEADLINE_MARGIN_S = 30.0
_DEFAULT_ATTACHMENT_NAME = "file"
_DEFAULT_ATTACHMENT_MIME = "application/octet-stream"
SEND_GAP_S = 5.0  # лимитер: 1 письмо / 5 с на аккаунт
READ_ACK_IMAP_BUDGET_S = 45.0  # Ф8-7: < 60 с, которые каркас даёт одному `push_read`
RESORT_DAYS = 30  # Ф10-5 A6: пересортировка писем трекеров за 30 дней
RESORT_BATCH = STARTUP_BATCH  # по 20 тредов за сверку, как C5
# Ф12-1 B3 + П6: новое письмо — всё задание `send()` укладывается в 55 с (< 60 с каркаса); поиск копии после
# SMTP — до 3 попыток с паузой 2 с и только в остатке этого времени: не успели — `sent` без `peer_ext_id`
# (привяжет приём).
NEW_SEND_BUDGET_S = 55.0
# Ф12-5 B1: новое письмо с файлами — каркас даёт `FILE_SEND_TIMEOUT_S`; поиск копии — в его остатке (тот же
# запас 5 с на отчёт).
NEW_FILE_SEND_BUDGET_S = FILE_SEND_TIMEOUT_S - 5.0
NEW_COPY_ATTEMPTS = 3
NEW_COPY_PAUSE_S = 2.0
_TEMP_CHANNEL_PREFIX = "new:"  # временный `ext_id` беседы-заготовки ядра
MEDIA_FETCH_IMAP_S = 400.0  # Ф11-3a A3: письмо целиком (до 300 МиБ) — < таймаута загрузки 480 с
_MEDIA_NAME_MAX = 1000  # ядро хранит имя и mime вложения обрезанными до 1000 символов (`media._MAX_TEXT`)


@dataclass
class _ThreadPass:
    """Итог разбора треда: события и что куда ушло (Ф10-5: пересортировке нужны письма, перенесённые в
    беседы трекера, и было ли хоть одно письмо в беседе-треде)."""

    events: list[dict] = field(default_factory=list)
    live: list[FetchedMsg] = field(default_factory=list)
    regular: list[FetchedMsg] = field(default_factory=list)
    moved: dict[str, list[tuple[FetchedMsg, str]]] = field(default_factory=dict)
    newest_ref: dict | None = None


def _is_new_chat(lease: dict) -> bool:
    """Ф12-1: задание беседы-заготовки — по `new_to` или временному `channel_ext_id` (без `new_to` такое
    задание уходит в `permanent` «новая беседа без адреса», а не в разбор X-GM-THRID)."""
    return lease.get("new_to") is not None or str(lease.get("channel_ext_id") or "").startswith(
        _TEMP_CHANNEL_PREFIX
    )


def parse_mail_config(config: dict | None) -> dict:
    """`account.config_json` почты -> нормализованный dict. Нет адреса -> `ValueError`."""
    config = dict(config or {})
    address = (config.get("address") or "").strip().lower()
    if not address or "@" not in address:
        raise ValueError("config.address не задан")
    aliases = [str(a).strip().lower() for a in (config.get("aliases") or []) if str(a).strip()]
    exclude = config.get("exclude_categories")
    if exclude is None:
        exclude = list(DEFAULT_EXCLUDE_CATEGORIES)
    return {
        "address": address,
        "aliases": aliases,
        "display_name": config.get("display_name") or address,
        "proxy": config.get("proxy"),
        "exclude_categories": [str(c) for c in exclude],
        "reply_mode": config.get("reply_mode") or "sender",
        "send_enabled": config.get("send_enabled", True) is not False,
        # Ф84-1: правила склейки писем-уведомлений по ключу (негодные пропущены с WARNING)
        "notify_keys": notify_keys.parse_rules(config.get("notify_keys")),
    }


class MailConnector(ConnectorBase):
    source = "mail"
    sends_enabled = True
    idempotent = False
    lease_ms = 120_000  # send(): IMAP ≤ 15 с + лимитер ≤ 5 с + SMTP ≤ 35 с
    read_ack_enabled = True  # Ф8-7: прочитанное в хабе -> `\\Seen` в Gmail
    sleep_enabled = True  # Ф70-1: спит вместе с днём проекта — слушатель IDLE остановлен, отправка жива

    def __init__(
        self,
        client,
        gateway: GmailGateway,
        *,
        account_id: int,
        config: dict,
        media_tmp_dir: str | Path | None = None,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(client, **base_kwargs)
        self._gw = gateway
        self._account_id = account_id
        # Ф11-3a A3: загрузчик вложений — своя задача, как у Telegram/Slack; `None` — не запускается.
        self._media: MediaDownloader | None = None
        self._media_task: asyncio.Task | None = None
        if media_tmp_dir is not None:
            self._media = MediaDownloader(
                client,
                self.fetch_media,
                tmp_dir=media_tmp_dir,
                mono=lambda: self._mono(),
                sleep=lambda s: self._sleep(s),
                is_blocked=lambda: self.auth_required,
                on_auth_required=self._on_auth_required,
                on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
            )
        cfg = parse_mail_config(config)
        self._me = MeInfo(address=cfg["address"], aliases=cfg["aliases"])
        self._display_name = cfg["display_name"]
        self._exclude = cfg["exclude_categories"]
        self._send_enabled = cfg["send_enabled"]
        self._notify: list[dict] = cfg["notify_keys"]
        self._notify_fp = notify_keys.fingerprint(self._notify)
        # Ф15-1 C2: реакция на письмо — письмо, поэтому только у ящика с отправкой (П5).
        self.reactions_enabled = self._send_enabled

        self._loop: asyncio.AbstractEventLoop | None = None
        self._auth_failed: str | None = None
        self._listener_seen_connect = False

        self._known: set[str] = set()  # ext_id тредов, которые есть в ядре (heads + созданные этой сверкой)
        self._touched: set[int] = set()  # треды, уже разобранные ЭТОЙ сверкой (C4 их не перечитывает)
        self._startup_order: list[int] | None = None
        self._startup_empty: set[int] = set()
        self._flag_cache: dict[int, bool] = {}  # C4 без CONDSTORE: uid -> \Seen
        self._read_dirty: set[int] = set()
        self._last_smtp_mono: float | None = None
        # Ф10-5 A5: тред Gmail -> `ext_id` бесед трекера, куда ушли его письма (только память процесса), и
        # подробно: тред -> Message-ID письма -> беседы трекера (маркер прочтения по письмам каждой беседы)
        self._tracker_threads: dict[int, set[str]] = {}
        self._tracker_letters: dict[int, dict[str, set[str]]] = {}
        self._resort_order: list[int] | None = None
        self._resort_seen: set[int] = set()
        self._resort_count = 0

    # --- жизненный цикл ---------------------------------------------------------------------------------

    async def start_source(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self._gw.work.connect()
        except MailAuthError as exc:
            raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"gmail: ящик не годится ({exc})") from None
        except MailError as exc:
            # сети нет — не исключение: слушатель переподключится сам, сверка — после (как у Slack)
            _logger.warning("mail: старт без связи (%s)", describe(exc))
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error=describe(exc))
        self._gw.start_listener(self._listener_event_threadsafe)
        if self._media is not None:
            self._media_task = asyncio.ensure_future(self._media.run())

    async def stop_source(self) -> None:
        if self._media_task is not None:
            self._media_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._media_task
            self._media_task = None
        with contextlib.suppress(Exception):
            await self._gw.close()

    async def stop_receiving(self) -> None:
        """Ф70-1 C.2: сон вместе с днём проекта — остановить слушатель IDLE и отпустить рабочее соединение
        (переподключится само при следующем задании: прочитано из хаба, вложение по клику). Отправочное
        соединение IMAP и SMTP не трогаются — отправка при сне идёт как обычно."""
        await self._gw.stop_listener()
        self._gw.work.kill()

    async def start_receiving(self) -> None:
        """Ф70-1 C.2: сон снят — слушатель заново. Его первое подключение сверку не заказывает (её закажет
        каркас — `resume`)."""
        self._listener_seen_connect = False
        self._gw.start_listener(self._listener_event_threadsafe)

    async def on_resume(self) -> None:
        """Сон: слушатель и оба рабочих соединения рвутся и поднимаются заново; сверку `resume` закажет
        каркас."""
        self._gw.restart_listener()
        await self._gw.reset_sessions()

    def _listener_event_threadsafe(self, kind: str, detail: str | None) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._on_listener_event, kind, detail)

    def _on_listener_event(self, kind: str, detail: str | None) -> None:
        if kind == "new":
            self._request_sync("live")
        elif kind == "connected":
            # «Догон при КАЖДОМ подключении» (план 1.3); первое подключение покрывает сверка `start`.
            if self._listener_seen_connect:
                self._request_sync("reconnect")
            self._listener_seen_connect = True
        elif kind == "auth":
            self._auth_failed = detail or "other"
            self._heartbeat_now.set()

    async def health(self) -> None:
        """М13: по рабочему соединению не ходит. Слушатель не подключён > 2 мин -> `no_network`."""
        if self._auth_failed is not None:
            raise AuthRequired(f"gmail: вход не принят ({self._auth_failed})")
        down_since = self._gw.listener_down_since()
        if down_since is not None and self._wall() - down_since > LISTENER_DOWN_UNHEALTHY_S:
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error="imap_listener_down")
            raise ConnectionError("gmail: слушатель IMAP не подключён дольше 2 мин")

    # --- курсор и пачки ---------------------------------------------------------------------------------

    @staticmethod
    def _load_cursor(raw: dict | None) -> dict | None:
        if not raw or not raw.get("value"):
            return None
        try:
            value = json.loads(raw["value"])
        except ValueError, TypeError:
            return None
        if not isinstance(value, dict) or "uidvalidity" not in value or "last_uid" not in value:
            return None
        return value

    async def _push(self, events: list[dict], cursor: dict | None = None) -> None:
        """М1: события порции и курсор — одной пачкой (курсор — на ПОСЛЕДНЕМ куске, если событий > 400)."""
        if not events and cursor is None:
            return
        chunks = [events[i : i + PUSH_CHUNK] for i in range(0, len(events), PUSH_CHUNK)] or [[]]
        payload = None
        if cursor is not None:
            payload = {"value": json.dumps(cursor, sort_keys=True), "ts": int(self._wall() * 1000)}
        for idx, chunk in enumerate(chunks):
            last = idx == len(chunks) - 1
            await self._client.push_events(chunk, cursor=payload if last else None)

    # --- сверка --------------------------------------------------------------------------------------------

    async def sync(self, reason: str = "scheduled") -> None:
        deadline = self._mono() + SYNC_BUDGET_S
        self._touched = set()
        try:
            state = await self._gw.work.examine()
            info = self._gw.work.info
            assert info is not None  # noqa: S101 — ставится при подключении
            heads = await self._client.channels_heads()
            self._known = {h["ext_id"] for h in heads}
            cursor = self._load_cursor(await self._client.get_cursor())

            if cursor is None:
                cursor = {
                    "uidvalidity": state.uidvalidity,
                    "last_uid": state.last_uid,
                    "modseq": state.highestmodseq if info.condstore else None,
                    "startup": {"days": STARTUP_DAYS},
                    # Ф10-5 A6: у нового ящика старых бесед-тредов нет — стартовая загрузка уже раскладывает
                    "trackers": self._resort_state(),
                }
                await self._push([], cursor)
                _logger.info("mail: первый запуск — стартовая загрузка за %d дн.", STARTUP_DAYS)
            elif cursor["uidvalidity"] != state.uidvalidity:
                _logger.warning("mail: UIDVALIDITY сменился — пересинхронизация за %d дн.", RESYNC_DAYS)
                cursor = await self._resync(state, cursor, info)
            else:
                cursor = await self._catch_up(cursor, deadline)

            if self._mono() > deadline:
                self._request_sync(reason)
                return
            cursor = await self._sync_flags(cursor, info, state)

            if cursor.get("startup"):
                if self._mono() <= deadline:
                    await self._startup_batch(cursor)
            elif not self._resort_done(cursor) and self._mono() <= deadline:
                await self._resort_batch(cursor, deadline)
        except MailAuthError as exc:
            raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"gmail: ящик не годится ({exc})") from None
        except MailUnavailable as exc:
            _logger.warning("mail: сверка %s прервана — нет связи (%s)", reason, describe(exc))
            with contextlib.suppress(Exception):
                await self._client.set_status("no_network", last_error=describe(exc))

    # --- C2: догон по курсору ----------------------------------------------------------------------------

    async def _catch_up(self, cursor: dict, deadline: float) -> dict:
        uids = await self._gw.work.search_new(int(cursor["last_uid"]))
        for i in range(0, len(uids), PORTION):
            if self._mono() > deadline:
                self._request_sync("scheduled")
                break
            portion = uids[i : i + PORTION]
            events = await self._process_portion(portion)
            new_cursor = dict(cursor, last_uid=max(portion))
            await self._push(events, new_cursor)
            cursor = new_cursor
        return cursor

    async def _resync(self, state, old_cursor: dict, info: ServerInfo) -> dict:
        """UIDVALIDITY сменился: письма за 7 дней проходят тот же отбор, что догон (известные треды — повтор
        upsert = no-op ядра, новые из «Входящих» — как новые треды), затем курсор с новым UIDVALIDITY и
        последним UID, взятым ДО пересинхронизации."""
        found = await self._gw.work.search_raw(f"newer_than:{RESYNC_DAYS}d")
        uids = [u for u in found if u <= state.last_uid]
        for i in range(0, len(uids), PORTION):
            events = await self._process_portion(uids[i : i + PORTION])
            await self._push(events)
        new_cursor = {
            "uidvalidity": state.uidvalidity,
            "last_uid": state.last_uid,
            "modseq": state.highestmodseq if info.condstore else None,
        }
        if old_cursor.get("startup"):
            new_cursor["startup"] = old_cursor["startup"]
        if old_cursor.get("trackers"):
            new_cursor["trackers"] = old_cursor["trackers"]
        await self._push([], new_cursor)
        return new_cursor

    # --- C3: отбор порции ---------------------------------------------------------------------------------

    def _meta_is_mine(self, m: FetchedMsg) -> bool:
        from_list = convert.addresses(m.header, "From")
        return convert.is_mine(m, from_list[0][1] if from_list else None, self._me)

    def _opens_thread(self, m: FetchedMsg, excluded: set[int]) -> bool:
        """C3(б): письмо открывает НОВУЮ беседу. Черновик (М4), не «Входящие», исключённая категория и моё
        письмо (правило (в)) — не открывают."""
        if m.is_draft:
            return False
        if "\\Inbox" not in m.labels:
            return False
        if m.uid in excluded:
            return False
        if self._meta_is_mine(m):  # C3(в): моё письмо в тред, которого в хабе нет, — не берётся
            return False
        return True

    async def _process_portion(self, uids: list[int]) -> list[dict]:
        """C3 + М4 + М5: отбор по тредам порции, догрузка новых тредов целиком, события."""
        if not uids:
            return []
        metas = [m for m in await self._gw.work.fetch_meta(uids) if m.thrid is not None]
        by_thread: dict[int, list[FetchedMsg]] = {}
        for m in metas:
            if m.is_draft:
                continue  # М4: черновик не берётся никогда
            by_thread.setdefault(m.thrid, []).append(m)

        unknown = [t for t in by_thread if not self._thread_tracked(t)]
        excluded: set[int] = set()
        if unknown and any(
            "\\Inbox" in m.labels and not self._meta_is_mine(m) for t in unknown for m in by_thread[t]
        ):
            excluded = await self._gw.work.search_categories(min(uids), max(uids), self._exclude)

        events: list[dict] = []
        for thrid in sorted(by_thread):
            msgs = by_thread[thrid]
            if self._thread_tracked(thrid):
                # Ф10-5 A5 п.4: письмо в треде трекера — не повод грузить тред целиком
                events.extend(await self._thread_events(thrid, portion=msgs, full=False))
            elif any(self._opens_thread(m, excluded) for m in msgs):
                events.extend(await self._thread_events(thrid, portion=msgs, full=True))
        return events

    async def _thread_meta(self, thrid: int) -> list[FetchedMsg]:
        uids = await self._gw.work.search_thread(thrid)
        if not uids:
            return []
        return [m for m in await self._gw.work.fetch_thread_meta(uids) if m.thrid in (None, thrid)]

    def _thread_tracked(self, thrid: int) -> bool:
        """Тред известен: его беседа-тред есть в ядре ИЛИ его письма уже ушли в беседы трекера (Ф10-5)."""
        return str(thrid) in self._known or thrid in self._tracker_threads

    async def _thread_events(self, thrid: int, *, portion: list[FetchedMsg], full: bool) -> list[dict]:
        """События треда: письма порции (и при `full` — последние 30 писем треда), `attrs` по всему треду
        (М3), маркер прочтения (C4)."""
        return (await self._thread_pass(thrid, portion=portion, full=full)).events

    def _tracker_letter_events(self, m: FetchedMsg, thread_msgs: list[FetchedMsg]) -> list[dict] | None:
        """Ф10-5: письмо трекера -> события в беседы задач/карточек; `None` — обычное письмо. Сбой разбора
        уведомления — не потеря: письмо идёт прежним путём (в журнал только тип ошибки)."""
        try:
            parts = trackers.route(m, thread_msgs=thread_msgs, known=self._known)
            if not parts and self._notify:  # Ф84-1: трекеры первыми, затем правила `notify_keys`
                parts = notify_keys.route(m, self._notify, thread_msgs=thread_msgs, known=self._known)
            if not parts:
                return None
            return [convert.tracker_event(m, p, me=self._me) for p in parts]
        except Exception as exc:  # noqa: BLE001 — текст письма в журнал не идёт
            _logger.error(
                "mail: уведомление трекера не разобрано (uid=%s, %s) — как обычное письмо",
                m.uid,
                type(exc).__name__,
            )
            return None

    async def _thread_pass(self, thrid: int, *, portion: list[FetchedMsg], full: bool) -> _ThreadPass:
        meta = await self._thread_meta(thrid)
        meta_by_uid = {m.uid: m for m in meta}
        for m in portion:
            meta_by_uid.setdefault(m.uid, m)
        live = sorted(
            (m for m in meta_by_uid.values() if not m.is_draft), key=lambda m: (m.internaldate_ms or 0, m.uid)
        )
        emit_uids = {m.uid for m in portion if not m.is_draft}
        if full:
            emit_uids |= {m.uid for m in live[-THREAD_LOAD_LAST:]}
        if not emit_uids:
            return _ThreadPass(live=live)

        sizes = [(uid, meta_by_uid[uid].size) for uid in sorted(emit_uids)]
        bodies = {b.uid: b for b in await self._gw.work.fetch_bodies(sizes)}
        merged: list[FetchedMsg] = []
        for uid in sorted(emit_uids):
            body = bodies.get(uid)
            base = meta_by_uid[uid]
            if body is None:
                continue  # письмо исчезло между командами — следующая сверка разберётся
            base.body = body.body
            base.body_partial = body.body_partial
            merged.append(base)
        merged.sort(key=lambda m: (m.internaldate_ms or 0, m.uid))
        if not merged:
            return _ThreadPass(live=live)

        thread_meta = list(meta_by_uid.values())
        routed: dict[int, list[dict]] = {}
        for m in merged:
            tracker_events = self._tracker_letter_events(m, thread_meta)
            if tracker_events:
                routed[m.uid] = tracker_events
        regular = [m for m in merged if m.uid not in routed]

        is_new = str(thrid) not in self._known
        title = convert.thread_title(regular[0].body) if (is_new and regular) else None
        attrs = convert.thread_attrs(thread_meta)
        ref = convert.channel_ref(thrid, title=title, attrs=attrs)
        thread_msgids = {convert.ext_id_from_headers(m.header, m.msgid) for m in live}
        result = _ThreadPass(live=live, regular=regular)
        events = result.events
        for m in merged:
            tracker_events = routed.get(m.uid)
            if tracker_events is not None:
                events.extend(tracker_events)
                mid = tracker_events[0]["ext_id"]
                chans = {e["channel"]["ext_id"] for e in tracker_events}
                self._tracker_letters.setdefault(thrid, {}).setdefault(mid, set()).update(chans)
                self._tracker_threads.setdefault(thrid, set()).update(chans)
                self._known.update(chans)
                for c in chans:
                    result.moved.setdefault(c, []).append((m, mid))
                result.newest_ref = tracker_events[0]["channel"]
                continue
            try:
                events.append(convert.letter_event(m, me=self._me, channel=ref, thread_msgids=thread_msgids))
            except Exception as exc:  # noqa: BLE001 — одно письмо не роняет порцию; текст в журнал не идёт
                # М14: не молчаливая потеря — заглушка; в журнал только тип ошибки (repr несёт байты письма)
                _logger.error("mail: письмо не разобрано (uid=%s, %s) — заглушка", m.uid, type(exc).__name__)
                events.append(convert.stub_event(m, me=self._me, channel=ref))
        if not events:
            return _ThreadPass(live=live)
        # Ф10-5 A5 п.1: беседу-тред и её маркер — только если беседа-тред есть или создаётся сейчас
        if regular or not is_new:
            marker = convert.read_marker(self._thread_side(thrid, live), self._me)
            if marker is not None:
                events.append(self._read_event(thrid, marker))
            self._known.add(str(thrid))
        self._touched.add(thrid)
        return result

    def _thread_side(self, thrid: int, letters: list[FetchedMsg]) -> list[FetchedMsg]:
        """Письма треда, оставшиеся в беседе-треде (без ушедших в беседы трекера) — для её маркера."""
        mapping = self._tracker_letters.get(thrid)
        if not mapping:
            return letters
        return [m for m in letters if convert.ext_id_from_headers(m.header, m.msgid) not in mapping]

    @staticmethod
    def _read_event(thrid: int, marker: str) -> dict:
        return {
            "type": "read",
            "channel": {"ext_id": str(thrid), "kind": convert.KIND, "own_id_space": True},
            "up_to_ext_id": marker,
        }

    # --- C4: прочитанность ------------------------------------------------------------------------------

    async def _sync_flags(self, cursor: dict, info: ServerInfo, state) -> dict:
        """М12: на КАЖДОЙ сверке. CONDSTORE -> изменения флагов с `modseq` курсора; иначе — окно последних
        500 UID против памяти процесса. Для каждого известного треда с изменениями — маркер по C4."""
        modseq = cursor.get("modseq")
        new_modseq = modseq
        if info.condstore and modseq is not None:
            changed = await self._gw.work.fetch_changed_flags(int(modseq))
            seen_modseqs = [m.modseq for m in changed if m.modseq is not None]
            if seen_modseqs:
                new_modseq = max(int(modseq), *seen_modseqs)
            for m in changed:
                if m.thrid is not None and str(m.thrid) in self._known and m.thrid not in self._touched:
                    self._read_dirty.add(m.thrid)
        else:
            last_uid = int(cursor["last_uid"])
            if last_uid > 0:
                window = await self._gw.work.fetch_flags_range(last_uid - READ_WINDOW_UIDS + 1, last_uid)
                current: dict[int, bool] = {}
                for m in window:
                    seen = "\\Seen" in m.flags
                    current[m.uid] = seen
                    if (
                        self._flag_cache.get(m.uid) != seen
                        and m.thrid is not None
                        and str(m.thrid) in self._known
                        and m.thrid not in self._touched
                    ):
                        self._read_dirty.add(m.thrid)
                self._flag_cache = current
            if info.condstore and state.highestmodseq is not None:
                new_modseq = state.highestmodseq

        batch = sorted(self._read_dirty, reverse=True)[:READ_THREADS_PER_SYNC]
        events: list[dict] = []
        for thrid in batch:
            self._read_dirty.discard(thrid)
            meta = await self._thread_meta(thrid)
            # Ф10-5: маркер беседы-треда — без писем, ушедших в беседы трекера
            marker = convert.read_marker(self._thread_side(thrid, meta), self._me)
            if marker is not None:
                events.append(self._read_event(thrid, marker))
        if self._read_dirty:
            self._request_sync("scheduled")

        new_cursor = dict(cursor, modseq=new_modseq)
        if events or new_modseq != modseq:
            await self._push(events, new_cursor)
        return new_cursor

    # --- C5: стартовая загрузка --------------------------------------------------------------------------

    async def _startup_order_threads(self, days: int) -> list[int]:
        if self._startup_order is None:
            query = f"in:inbox newer_than:{days}d" + "".join(f" -category:{c}" for c in self._exclude)
            uids = await self._gw.work.search_raw(query)
            newest: dict[int, tuple[int, int]] = {}
            for m in await self._gw.work.fetch_thrids(uids) if uids else []:
                if m.thrid is None:
                    continue
                key = (m.internaldate_ms or 0, m.uid)
                if m.thrid not in newest or key > newest[m.thrid]:
                    newest[m.thrid] = key
            ordered = sorted(newest, key=lambda t: newest[t], reverse=True)
            self._startup_order = ordered[:STARTUP_THREADS_MAX]
        return self._startup_order

    async def _startup_batch(self, cursor: dict) -> None:
        """C5 + М6: кандидаты — треды поиска, которых нет в ядре (`/channels/heads`), новейшие 300; по 20 за
        сверку с `backfill`. Курсор догона от прогресса C5 не зависит; рестарт посреди — догрузит
        оставшиеся."""
        days = int((cursor.get("startup") or {}).get("days", STARTUP_DAYS))
        order = await self._startup_order_threads(days)
        candidates = [t for t in order if not self._thread_tracked(t) and t not in self._startup_empty]
        if candidates:
            with contextlib.suppress(Exception):
                await self._client.set_activity(backfill=True)
            for thrid in candidates[:STARTUP_BATCH]:
                events = await self._thread_events(thrid, portion=[], full=True)
                if not any(e["type"] == "upsert" for e in events):
                    self._startup_empty.add(thrid)
                    continue
                await self._push(events)
            if len(candidates) > STARTUP_BATCH:
                self._request_sync("initial")
                return
        finished = {k: v for k, v in cursor.items() if k != "startup"}
        await self._push([], finished)
        self._startup_order = None
        with contextlib.suppress(Exception):
            await self._client.set_activity(backfill=False)
        _logger.info("mail: стартовая загрузка закончена")

    # --- Ф10-5 A6: пересортировка писем трекеров (один раз после деплоя) ---------------------------------

    def _resort_state(self) -> dict:
        """Признак «перенос пройден»: `done` и (Ф84-1) отпечаток правил `notify_keys`, если они есть."""
        state: dict = {"done": 1}
        if self._notify_fp:
            state["notify"] = self._notify_fp
        return state

    def _resort_done(self, cursor: dict) -> bool:
        state = cursor.get("trackers")
        if not (isinstance(state, dict) and bool(state.get("done"))):
            return False
        # правила есть, а пройдены другие (или никакие) — пройти заново один раз
        return not self._notify_fp or state.get("notify") == self._notify_fp

    async def _resort_order_threads(self) -> list[int]:
        """Кандидаты: треды «Вся почта» за 30 дней, где есть письмо с домена трекера (A1), новейшие первыми.
        Считается один раз за процесс (как порядок C5)."""
        if self._resort_order is None:
            uids = await self._gw.work.search_raw(f"newer_than:{RESORT_DAYS}d")
            newest: dict[int, tuple[int, int]] = {}
            for m in await self._gw.work.fetch_meta(uids) if uids else []:
                if m.thrid is None or m.is_draft:
                    continue
                if not self._meta_from_tracker(m):
                    continue
                key = (m.internaldate_ms or 0, m.uid)
                if m.thrid not in newest or key > newest[m.thrid]:
                    newest[m.thrid] = key
            self._resort_order = sorted(newest, key=lambda t: newest[t], reverse=True)
        return self._resort_order

    async def _resort_batch(self, cursor: dict, deadline: float) -> dict:
        """A6: старые беседы-треды (есть в `/channels/heads`) с письмами трекеров -> письма в беседы
        трекера, чистые треды (все письма — с домена трекера, ни одного моего) закрываются `channel_migrate`;
        прочитанность перенесённого ядро берёт из старой беседы-треда по тем же письмам (`ingest.
        _apply_channel_migrate`) — письмо, которого в старой беседе не было (пришло во время деплоя), остаётся
        непрочитанным. Тред, в который уже пришло новое письмо трекера (`_tracker_threads`), пересортировке
        тоже нужен — повторы отсекает только `_resort_seen` (ревью Ф10-5, находка 4). По 20 тредов за
        сверку с `backfill`; рестарт посреди — без вреда (повтор upsert — no-op, миграция — no-op по
        `channel_alias`)."""
        order = await self._resort_order_threads()
        candidates = [t for t in order if str(t) in self._known and t not in self._resort_seen]
        if candidates:
            with contextlib.suppress(Exception):
                await self._client.set_activity(backfill=True)
            for thrid in candidates[:RESORT_BATCH]:
                if self._mono() > deadline:
                    self._request_sync("initial")
                    return cursor
                self._resort_seen.add(thrid)
                self._resort_count += 1
                events = await self._resort_thread(thrid)
                if events:
                    await self._push(events)  # upsert + migrate — одной пачкой ядра (одна транзакция)
            if len(candidates) > RESORT_BATCH:
                self._request_sync("initial")
                return cursor
        done = dict(cursor, trackers=self._resort_state())
        await self._push([], done)
        self._resort_order = None
        with contextlib.suppress(Exception):
            await self._client.set_activity(backfill=False)
        _logger.info("mail: пересортировка трекеров закончена (%d тредов)", self._resort_count)
        return done

    def _meta_from_tracker(self, m: FetchedMsg) -> bool:
        from_list = convert.addresses(m.header, "From")
        if not from_list:
            return False
        addr = from_list[0][1]
        return trackers.tracker_of_address(addr) is not None or (
            bool(self._notify) and notify_keys.rule_of_address(addr, self._notify) is not None
        )

    async def _resort_thread(self, thrid: int) -> list[dict]:
        tp = await self._thread_pass(thrid, portion=[], full=True)
        events = list(tp.events)
        if not tp.moved:
            return events
        # миграция: ВСЕ не-черновые письма треда — с домена трекера и ни одно не моё, в беседе-треде этим
        # проходом не осталось ни одного письма, и хоть одно ушло в беседу трекера
        clean = all(not self._meta_is_mine(m) and self._meta_from_tracker(m) for m in tp.live)
        if clean and not tp.regular and tp.newest_ref is not None:
            events.append({"type": "channel_migrate", "old_ext_id": str(thrid), "channel": tp.newest_ref})
        return events

    # --- отправка (E1–E5, М7–М9) ------------------------------------------------------------------------

    def _our_msgid(self, lease: dict) -> str:
        return convert.our_message_id(lease["send_token"], self._me.address)

    @staticmethod
    def _matches(headers: list[FetchedMsg], ext_id: str) -> list[FetchedMsg]:
        return [m for m in headers if convert.ext_id_from_headers(m.header, m.msgid) == ext_id]

    async def _find_own(self, thrid: int, our_mid: str) -> bool:
        """М8: свой Message-ID — через `X-GM-THRID` (число) и сравнение в Python; запасной путь — `X-GM-RAW
        rfc822msgid:` только для безопасного Message-ID (Gmail мог положить копию в другой тред)."""
        uids = await self._gw.sender.search_thread(thrid)
        headers = await self._gw.sender.fetch_parent_headers(uids) if uids else []
        if self._matches(headers, our_mid):
            return True
        if not convert.SAFE_MSGID_RE.match(our_mid):
            return False
        found = await self._gw.sender.search_raw(f"rfc822msgid:{our_mid.strip('<>')}")
        if not found:
            return False
        others = await self._gw.sender.fetch_parent_headers(found)
        return bool(self._matches(others, our_mid))

    # --- Ф8-7: прочитанность из хаба в Gmail --------------------------------------------------------------

    async def push_read(self, item: dict) -> ReadAckResult:
        """Ф8-7 C3: задание ядра -> `\\Seen` письмам треда `channel.ext_id` (X-GM-THRID) с `INTERNALDATE` не
        позже `message.ts` (ядро отдаёт последнее ВХОДЯЩЕЕ письмо, П1). 0 помеченных (всё уже прочитано,
        письмо в Корзине/Спаме) — тоже `done`. Нечисловой тред/нет `ts` -> `drop`; сеть/IMAP -> `retry`;
        вход не принят/ящик не годится -> `AuthRequired`. В журнал — только класс ошибки."""
        channel = item.get("channel") or {}
        message = item.get("message") or {}
        thrid_s = str(channel.get("ext_id") or "")
        ts = message.get("ts")
        if trackers.is_tracker_channel(thrid_s) or notify_keys.is_notify_channel(thrid_s):
            return await self._push_read_tracker(item)
        if not (thrid_s.isascii() and thrid_s.isdigit()):
            return ReadAckResult("drop", reason="bad_thread_id")
        if isinstance(ts, bool) or not isinstance(ts, int):
            return ReadAckResult("drop", reason="bad_ts")
        try:
            await self._gw.mark_thread_seen(int(thrid_s), ts, limit_s=READ_ACK_IMAP_BUDGET_S)
        except MailAuthError as exc:
            raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"gmail: ящик не годится ({exc})") from None
        except MailError as exc:
            _logger.warning(
                "mail: отметка прочтения канала %s не удалась (%s)", item.get("channel_id"), describe(exc)
            )
            return ReadAckResult("retry", reason=describe(exc))
        return ReadAckResult("done")

    async def _push_read_tracker(self, item: dict) -> ReadAckResult:
        """Ф10-5 A7: беседа трекера — Message-ID письма -> `X-GM-RAW rfc822msgid:` -> UID -> `X-GM-THRID`
        (одна выборка) -> `mark_thread_seen(thrid, ts)`. Небезопасный Message-ID -> `drop`; письма нет ->
        `done`. Новых `STORE` нет: единственный — в `mark_thread_seen`."""
        message = item.get("message") or {}
        mid = str(message.get("ext_id") or "")
        ts = message.get("ts")
        if not mid.strip("<>") or not convert.SAFE_MSGID_RE.match(mid):
            return ReadAckResult("drop", reason="bad_message_id")
        if isinstance(ts, bool) or not isinstance(ts, int):
            return ReadAckResult("drop", reason="bad_ts")
        try:
            async with asyncio.timeout(READ_ACK_IMAP_BUDGET_S):
                uids = await self._gw.work.search_raw(f"rfc822msgid:{mid.strip('<>')}")
                metas = await self._gw.work.fetch_thrids(uids) if uids else []
                thrid = next((m.thrid for m in metas if m.thrid is not None), None)
                if thrid is None:
                    return ReadAckResult("done")
                await self._gw.mark_thread_seen(thrid, ts, limit_s=READ_ACK_IMAP_BUDGET_S)
        except TimeoutError:
            return ReadAckResult("retry", reason="timeout")
        except MailAuthError as exc:
            raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"gmail: ящик не годится ({exc})") from None
        except MailError as exc:
            _logger.warning(
                "mail: отметка прочтения канала %s не удалась (%s)", item.get("channel_id"), describe(exc)
            )
            return ReadAckResult("retry", reason=describe(exc))
        return ReadAckResult("done")

    # --- Ф11-3a A3: вложения писем по клику ---------------------------------------------------------------

    async def fetch_media(self, job: dict, sink: MediaSink) -> MediaResult:
        """Задание загрузчика: Message-ID письма (`message.ext_id`) -> `X-GM-RAW rfc822msgid:` в «Вся почта»
        (нет — `gone`, несколько — первое) -> размер письма больше `max_bytes` — `too_big` с `RFC822.SIZE` ->
        `BODY.PEEK[]` целиком -> вложение номер `idx` тем же обходом, что у конвертера -> имя и mime совпали
        с заданием — байты в `sink`, иначе `gone`. Сеть/IMAP — `temporary`; вход не принят — `AuthRequired`.
        В журнал — только класс ошибки (загрузчик пишет его с `media_id`)."""
        mid = str((job.get("message") or {}).get("ext_id") or "")
        idx = job.get("idx")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
            return MediaResult("gone", error="bad_ref")
        if not mid.strip("<>") or not convert.SAFE_MSGID_RE.match(mid):
            return MediaResult("gone", error="bad_message_id")
        max_bytes = job.get("max_bytes")
        try:
            uids = await self._gw.work.search_raw(f"rfc822msgid:{mid.strip('<>')}")
            if not uids:
                return MediaResult("gone", error="no_message")
            uid = uids[0]
            metas = [m for m in await self._gw.work.fetch_meta([uid]) if m.uid == uid]
            size = metas[0].size if metas else None
            if isinstance(max_bytes, int) and isinstance(size, int) and size > max_bytes:
                return MediaResult("too_big", error="message_size", size=size)
            fetched = await self._gw.work.fetch_full(uid, limit_s=MEDIA_FETCH_IMAP_S)
        except MailAuthError as exc:
            raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
        except MailConfigError as exc:
            raise AuthRequired(f"gmail: ящик не годится ({exc})") from None
        except MailError as exc:
            return MediaResult("temporary", error=type(exc).__name__)
        if fetched is None or fetched.body is None:
            return MediaResult("gone", error="no_message")
        try:
            found = await asyncio.to_thread(textproc.find_attachment, fetched.body, idx)
        except Exception as exc:  # noqa: BLE001 — кривое письмо: вложение не достать; текст в журнал не идёт
            return MediaResult("gone", error=type(exc).__name__)
        if found is None:
            return MediaResult("gone", error="no_attachment")
        info, data = found
        if not (_same_field(info["name"], job.get("name")) and _same_field(info["mime"], job.get("mime"))):
            return MediaResult("gone", error="attachment_changed")
        sink.write(data)
        return MediaResult("ready")

    async def _auth_after_report(self, lease: dict, error: str) -> AuthRequired:
        """М9: вход не принят -> отчёт `temporary` (строка не остаётся в `sending`), затем `AuthRequired`
        (каркас после него отчёт не шлёт)."""
        with contextlib.suppress(Exception):
            await self._client.report(
                outbox_id=lease["outbox_id"], lease_gen=lease["lease_gen"], outcome="temporary", error=error
            )
        return AuthRequired(error)

    async def _wait_send_slot(self) -> None:
        now = self._mono()
        if self._last_smtp_mono is not None:
            elapsed = now - self._last_smtp_mono
            if 0 <= elapsed < SEND_GAP_S:
                await self._sleep(SEND_GAP_S - elapsed)
        self._last_smtp_mono = self._mono()

    def _send_deadline(self, started: float, files: list[dict]) -> float | None:
        """Ф11-3b B4: срок `send()` по монотонным часам коннектора — только у задания с файлами (каркас даёт
        ему `FILE_SEND_TIMEOUT_S`); без файлов — `None` (прежние таймауты)."""
        return started + FILE_SEND_TIMEOUT_S if files else None

    def _data_cap(self, deadline: float | None) -> float | None:
        """Потолок таймаута `data()`: остаток срока `send()` минус `SEND_DEADLINE_MARGIN_S`."""
        if deadline is None:
            return None
        return deadline - SEND_DEADLINE_MARGIN_S - self._mono()

    async def _letter(self, lease: dict, files: list[dict], build) -> bytes | SendResult:
        """Ф11-3b B4: письмо `build(attachments)`; с файлами — байты всех файлов задания из ядра
        (`lease_files`, временные файлы удаляются сразу после сборки) -> `(путь, имя, mime)` в порядке
        задания; письмо строится в памяти (≤ 17 МиБ + base64) вне цикла событий. Сбой получения байтов ->
        `temporary` (отправки не было)."""
        if not files:
            return build(None)
        try:
            async with self.lease_files(lease) as paths:
                attachments = [
                    (path, _attachment_name(file), _attachment_mime(file))
                    for path, file in zip(paths, files, strict=True)
                ]
                return await asyncio.to_thread(build, attachments)
        except OutboxFileError as exc:
            return SendResult("temporary", error=exc.reason)

    async def send(self, lease: dict) -> SendResult:
        started = self._mono()
        if not self._send_enabled:
            return SendResult(
                "permanent",
                error=(
                    "ящик подключён только на приём — ответь в Gmail или включи отправку: "
                    f"hub account set {self._account_id} send_enabled=true"
                ),
            )
        files = lease_files_meta(lease)
        text = (lease.get("text") or "").strip()
        if not text and not files:  # Ф11-3b B4: письмо только с вложениями — можно
            return SendResult("permanent", error="пустое сообщение")
        if len(text) > SEND_MAX_TEXT_LEN:
            return SendResult("permanent", error="слишком длинное письмо")
        if _is_new_chat(lease):  # Ф12-1 B3: новое письмо — до разбора `channel_ext_id`
            return await self._send_new(lease, text, started=started)
        thrid_s = str(lease.get("channel_ext_id") or "")
        if not thrid_s.isdigit():
            return SendResult("permanent", error="беседа — не тред Gmail")
        thrid = int(thrid_s)
        our_mid = self._our_msgid(lease)

        # --- IMAP-фаза (М2: бюджет 15 с; не уложились -> temporary, SMTP не начинали) ---
        try:
            async with asyncio.timeout(SEND_IMAP_BUDGET_S):
                if lease.get("maybe_sent") and await self._find_own(thrid, our_mid):
                    return SendResult("sent", ext_id=our_mid)
                uids = await self._gw.sender.search_thread(thrid)
                headers = await self._gw.sender.fetch_parent_headers(uids) if uids else []
        except TimeoutError:
            return SendResult("temporary", error="imap: не уложились в 15 с")
        except MailAuthError as exc:
            raise await self._auth_after_report(lease, f"gmail: вход не принят ({exc.kind})") from None
        except MailError as exc:
            return SendResult("temporary", error=f"imap: {describe(exc)}")

        candidates = [m for m in headers if not m.is_draft]  # М4: родитель — никогда не черновик
        reply_to = lease.get("reply_to_ext_id")
        if reply_to:
            matches = self._matches(candidates, reply_to)
            if len(matches) > 1:
                return SendResult("permanent", error="несколько писем с этим Message-ID")
            parent_msg = matches[0] if matches else None
        else:
            parent_msg = max(candidates, key=lambda m: (m.internaldate_ms or 0, m.uid), default=None)
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

        await self._wait_send_slot()
        result = await self._gw.smtp_send(
            sender=self._me.address,
            recipients=recipients,
            message=message,
            data_cap_s=self._data_cap(self._send_deadline(started, files)),
        )
        if result.outcome == "sent":
            return SendResult("sent", ext_id=our_mid)
        if result.outcome == "auth":
            raise await self._auth_after_report(lease, "smtp: вход не принят")
        if result.outcome == "flood":
            return SendResult("flood", retry_after_ms=result.retry_after_ms, error="gmail: суточная квота")
        if result.outcome == "unknown":
            _logger.warning("mail: обрыв после DATA — исход неизвестен (сверка решит)")
            return SendResult("unknown")
        error = result.error or f"smtp {result.stage}: {result.code or 'сеть'}"
        return SendResult(result.outcome, error=error)

    # --- Ф12-1 B3: новое письмо новому адресату ------------------------------------------------------------

    def _new_letter(self, lease: dict) -> tuple[str, str, str] | SendResult:
        """`(to, subject, from)` задания беседы-заготовки или `permanent`, если адресат не годится (ядро уже
        проверило — здесь вторая защита от заголовков с переводом строки и чужого `from`)."""
        new_to = lease.get("new_to")
        if not isinstance(new_to, dict) or new_to.get("kind") != "email":
            return SendResult("permanent", error="новая беседа без адреса")
        to = new_to.get("to")
        subject = new_to.get("subject")
        sender = new_to.get("from") or self._me.address
        if not isinstance(to, str) or "@" not in to or any(c in to for c in " ,;<>\r\n\t"):
            return SendResult("permanent", error="адрес получателя не годится")
        if not isinstance(subject, str) or not subject.strip():
            return SendResult("permanent", error="тема письма не годится")
        problem = msgid_mod.subject_problem(subject)  # ревью Ф12-1: вторая защита, та же проверка, что в ядре
        if problem is not None:
            return SendResult("permanent", error=problem)
        if not isinstance(sender, str) or sender.lower() not in {self._me.address, *self._me.aliases}:
            return SendResult("permanent", error="from — только адрес ящика или его алиас")
        return to, subject.strip(), sender.lower()

    async def _find_own_anywhere(self, our_mid: str) -> tuple[bool, int | None]:
        """Своё письмо по Message-ID во всём ящике (`X-GM-RAW rfc822msgid:`) -> `(нашли, X-GM-THRID)`.
        Небезопасный Message-ID в команду не идёт (М8)."""
        if not convert.SAFE_MSGID_RE.match(our_mid):
            return False, None
        uids = await self._gw.sender.search_raw(f"rfc822msgid:{our_mid.strip('<>')}")
        if not uids:
            return False, None
        matches = self._matches(await self._gw.sender.fetch_parent_headers(uids), our_mid)
        if not matches:
            return False, None
        return True, next((m.thrid for m in matches if m.thrid is not None), None)

    async def _send_new(self, lease: dict, text: str, *, started: float | None = None) -> SendResult:
        """Ф12-1 B3: новое письмо — `From` (ящик или алиас, с именем ящика), `To`, `Subject` (RFC 2047), свой
        `Message-ID`, без `In-Reply-To`/`References`. `maybe_sent` -> сначала своё письмо по всему ящику ->
        нашли -> `sent`. После SMTP — копия (до 3 попыток, в остатке 55 с) -> `peer_ext_id` = её X-GM-THRID;
        не нашли — `sent` без него (привяжет приём, П4б). Никогда не `TimeoutError` после SMTP (П6).

        Ф12-5 B1: с файлами задания — письмо с вложениями той же сборкой, что ответ (`_letter`: сбой байтов ->
        `temporary`; `SIZE` сервера — до `MAIL FROM`; срок `data()` — в остатке `FILE_SEND_TIMEOUT_S`); поиск
        копии — в остатке срока задания с файлами."""
        if started is None:
            started = self._mono()
        files = lease_files_meta(lease)
        letter = self._new_letter(lease)
        if isinstance(letter, SendResult):
            return letter
        to, subject, sender = letter
        our_mid = self._our_msgid(lease)

        if lease.get("maybe_sent"):
            try:
                async with asyncio.timeout(SEND_IMAP_BUDGET_S):
                    found, thrid = await self._find_own_anywhere(our_mid)
            except TimeoutError:
                return SendResult("temporary", error="imap: не уложились в 15 с")
            except MailAuthError as exc:
                raise await self._auth_after_report(lease, f"gmail: вход не принят ({exc.kind})") from None
            except MailError as exc:
                return SendResult("temporary", error=f"imap: {describe(exc)}")
            if found:
                return SendResult("sent", ext_id=our_mid, peer_ext_id=str(thrid) if thrid else None)

        now_ts = self._wall()
        message = await self._letter(
            lease,
            files,
            lambda attachments: convert.build_letter(
                recipients=[to],
                subject=subject,
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
        await self._wait_send_slot()
        result = await self._gw.smtp_send(
            sender=self._me.address,
            recipients=[to],
            message=message,
            data_cap_s=self._data_cap(self._send_deadline(started, files)),
        )
        if result.outcome == "sent":
            budget = NEW_FILE_SEND_BUDGET_S if files else NEW_SEND_BUDGET_S
            thrid = await self._find_copy_thread(our_mid, deadline=started + budget)
            return SendResult("sent", ext_id=our_mid, peer_ext_id=str(thrid) if thrid is not None else None)
        if result.outcome == "auth":
            raise await self._auth_after_report(lease, "smtp: вход не принят")
        if result.outcome == "flood":
            return SendResult("flood", retry_after_ms=result.retry_after_ms, error="gmail: суточная квота")
        if result.outcome == "unknown":
            _logger.warning("mail: обрыв после DATA — исход неизвестен (сверка решит)")
            return SendResult("unknown")
        error = result.error or f"smtp {result.stage}: {result.code or 'сеть'}"
        return SendResult(result.outcome, error=error)

    async def _find_copy_thread(self, our_mid: str, *, deadline: float) -> int | None:
        """П6: X-GM-THRID копии только что отправленного письма — до `NEW_COPY_ATTEMPTS` попыток с паузой, не
        позже `deadline` (монотонные часы). Любой сбой -> `None`: письмо уже ушло, «отправлено» не теряем."""
        for attempt in range(NEW_COPY_ATTEMPTS):
            remaining = deadline - self._mono()
            if remaining <= 0:
                return None
            try:
                async with asyncio.timeout(remaining):
                    found, thrid = await self._find_own_anywhere(our_mid)
            except Exception as exc:  # noqa: BLE001 — после SMTP любой сбой поиска — «без привязки»
                _logger.info("mail: копия нового письма не найдена (%s)", describe(exc))
                return None
            if found and thrid is not None:
                return thrid
            if attempt + 1 < NEW_COPY_ATTEMPTS:
                if deadline - self._mono() <= NEW_COPY_PAUSE_S:
                    return None
                await self._sleep(NEW_COPY_PAUSE_S)
        return None

    async def reconcile(self, lease: dict) -> SendResult:
        """E5 + М9: один поиск своего Message-ID без паузы — нашли -> `sent`, нет -> `unknown`. Ничего не
        отправляет никогда. Ф12-1 П5: новое письмо — поиск по всему ящику, `sent` с `peer_ext_id` (X-GM-THRID
        найденного)."""
        thrid_s = str(lease.get("channel_ext_id") or "")
        our_mid = self._our_msgid(lease)
        if _is_new_chat(lease):
            try:
                async with asyncio.timeout(RECONCILE_BUDGET_S):
                    found, thrid = await self._find_own_anywhere(our_mid)
            except MailAuthError as exc:
                raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
            except Exception as exc:  # noqa: BLE001 — сверка не уверена -> unknown (решает владелец)
                _logger.warning("mail: сверка нового письма не удалась (%s)", describe(exc))
                return SendResult("unknown")
            if found:
                return SendResult("sent", ext_id=our_mid, peer_ext_id=str(thrid) if thrid else None)
            return SendResult("unknown")
        try:
            async with asyncio.timeout(RECONCILE_BUDGET_S):
                if thrid_s.isdigit() and await self._find_own(int(thrid_s), our_mid):
                    return SendResult("sent", ext_id=our_mid)
        except MailAuthError as exc:
            raise AuthRequired(f"gmail: вход не принят ({exc.kind})") from None
        except Exception as exc:  # noqa: BLE001 — сверка не уверена -> unknown (решает владелец)
            _logger.warning("mail: сверка отправки не удалась (%s)", describe(exc))
        return SendResult("unknown")

    # --- Ф15-1 C2: реакция на письмо (письмо-реакция в формате Gmail) ---------------------------------------

    async def _reaction_target(self, job: dict, target_mid: str) -> FetchedMsg | None:
        """Письмо-цель по Message-ID: письма треда (`X-GM-THRID` беседы) с заголовками для ограничений Gmail
        (`List-Id`, To, Cc); сравнение — в Python. Черновик целью не бывает."""
        thrid_s = str((job.get("channel") or {}).get("ext_id") or "")
        if not thrid_s.isdigit():
            return None
        session = self._gw.sender
        uids = await session.search_thread(int(thrid_s))
        if not uids:
            return None
        info = session.info if session.info is not None else await session.connect()
        headers = await session.fetch_box(info.all_mailbox, uids, REACTION_TARGET_ITEMS)
        matches = [m for m in self._matches(headers, target_mid) if not m.is_draft]
        return matches[0] if matches else None

    async def _reaction_sent(self, our_mid: str) -> bool:
        """П2: письмо-реакция с этим Message-ID уже есть в ящике (Gmail кладёт копию отправленного сам)."""
        found, _thrid = await self._find_own_anywhere(our_mid)
        return found

    async def _reaction_after_smtp(self, message: bytes) -> None:
        """Gmail: копию в «Отправленные» кладёт SMTP; вид `imap` переопределяет (APPEND)."""
        del message

    def _reaction_event(self, job: dict, target_mid: str, our_mid: str, emoji_value: str) -> dict:
        channel = job.get("channel") or {}
        return {
            "type": "mail_reaction",
            "channel": {"ext_id": str(channel.get("ext_id")), "kind": convert.KIND, "own_id_space": True},
            "target_ext_id": target_mid,
            "src_ext_id": our_mid,
            "emoji": emoji_value,
            "sender": {
                "ext_id": self._me.address,
                "name": self._display_name or self._me.address,
                "handle": self._me.address,
                "is_me": True,
            },
            "ts": int(self._wall() * 1000),
        }

    async def push_reaction(self, job: dict) -> ReactionResult:
        """Ф15-1 C2 + П2: каждая операция `on`, чьего эмодзи ещё нет среди моих (`current_mine`), — письмо-
        реакция тем же адресатам, что ответ (`reply_recipients`). Ограничения Gmail по заголовкам цели ->
        `drop` (в журнал — причина, без адресов). Message-ID реакции детерминирован (сообщение, эмодзи): перед
        КАЖДОЙ SMTP-отправкой — поиск своего письма; нашлось -> успех без SMTP. После успеха — событие
        `mail_reaction` от меня (значок сразу; эхо склеится по `src_ext_id`). SMTP 5xx -> `drop`, временный
        сбой и «исход неизвестен» -> `retry` (повтор найдёт своё письмо)."""
        if not self._send_enabled:
            return ReactionResult("drop", reason="receive_only")
        target_mid = str(job.get("message_ext_id") or "")
        message_id = job.get("message_id")
        if not target_mid or not isinstance(message_id, int):
            return ReactionResult("drop", reason="bad_ref")
        mine = {str(e).replace("️", "") for e in (job.get("current_mine") or []) if isinstance(e, str)}
        final: dict[str, tuple[str, bool]] = {}
        for op in job.get("ops") or []:
            if isinstance(op, list | tuple) and len(op) >= 2 and isinstance(op[0], str) and op[0]:
                final[op[0].replace("️", "")] = (op[0], op[1] is True)
        to_send = [emoji for key, (emoji, on) in final.items() if on and key not in mine]
        if not to_send:
            return ReactionResult("done", reason="nothing")
        try:
            async with asyncio.timeout(SEND_IMAP_BUDGET_S):
                target = await self._reaction_target(job, target_mid)
        except TimeoutError:
            return ReactionResult("retry", reason="imap_timeout")
        except MailAuthError as exc:
            raise AuthRequired(f"mail: вход не принят ({exc.kind})") from None
        except MailError as exc:
            return ReactionResult("retry", reason=type(exc).__name__)
        if target is None:
            return ReactionResult("drop", reason="target_not_found")
        parent = convert.reply_parent(target, self._me)
        blocker = convert.reaction_blocker(parent, target.header, self._me)
        if blocker is not None:
            _logger.info("mail: реакция на сообщение %s не ставится (%s)", message_id, blocker)
            return ReactionResult("drop", reason=blocker)
        recipients = convert.reply_recipients(parent)
        if not recipients:
            return ReactionResult("drop", reason="no_recipients")
        for emoji_value in to_send:
            token = convert.reaction_token(self._account_id, message_id, emoji_value)
            our_mid = convert.our_message_id(token, self._me.address)
            try:
                async with asyncio.timeout(SEND_IMAP_BUDGET_S):
                    already = await self._reaction_sent(our_mid)
            except TimeoutError:
                return ReactionResult("retry", reason="imap_timeout")
            except MailAuthError as exc:
                raise AuthRequired(f"mail: вход не принят ({exc.kind})") from None
            except MailError as exc:
                return ReactionResult("retry", reason=type(exc).__name__)
            if not already:
                message = convert.build_reaction(
                    parent=parent,
                    recipients=recipients,
                    emoji_value=emoji_value,
                    address=self._me.address,
                    display_name=self._display_name,
                    message_id=our_mid,
                    now_ts=self._wall(),
                )
                await self._wait_send_slot()
                result = await self._gw.smtp_send(
                    sender=self._me.address, recipients=recipients, message=message
                )
                if result.outcome == "auth":
                    raise AuthRequired("smtp: вход не принят")
                if result.outcome == "flood":
                    retry_s = (result.retry_after_ms or 0) / 1000 or None
                    return ReactionResult("retry", retry_after_s=retry_s, reason="flood")
                if result.outcome == "permanent":
                    _logger.info("mail: реакция на сообщение %s отклонена SMTP (%s)", message_id, result.code)
                    return ReactionResult("drop", reason="smtp_permanent")
                if result.outcome != "sent":
                    return ReactionResult("retry", reason=f"smtp_{result.outcome}")
                await self._reaction_after_smtp(message)
            try:
                await self._client.push_events([self._reaction_event(job, target_mid, our_mid, emoji_value)])
            except Exception as exc:  # noqa: BLE001 — письмо ушло; значок придёт эхом из «Отправленных»
                _logger.info("mail: событие реакции не передано ядру (%s)", type(exc).__name__)
        return ReactionResult("done")


# Ф15-1 C2: заголовки письма-цели реакции — как у родителя ответа, плюс `List-Id` (ограничение Gmail).
REACTION_TARGET_ITEMS = (
    "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE "
    "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES SUBJECT FROM REPLY-TO TO CC LIST-ID)])"
)


def _attachment_name(file: dict) -> str:
    """Ф11-3b B4: имя вложения письма — из задания; пусто/не строка — `file`."""
    name = file.get("name")
    return name if isinstance(name, str) and name.strip() else _DEFAULT_ATTACHMENT_NAME


def _attachment_mime(file: dict) -> str:
    mime = file.get("mime")
    return mime if isinstance(mime, str) and mime else _DEFAULT_ATTACHMENT_MIME


def _same_field(actual: object, expected: object) -> bool:
    """Имя/mime вложения письма против задания ядра (там — обрезанные до 1000 символов)."""
    a = actual[:_MEDIA_NAME_MAX] if isinstance(actual, str) else None
    b = expected[:_MEDIA_NAME_MAX] if isinstance(expected, str) else None
    return a is not None and a == b
