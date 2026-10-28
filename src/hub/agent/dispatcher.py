"""Диспетчер разборщика: лента `/sync` -> буферы бесед -> карточка по правилам в ядре -> уведомление. Спека
7b, B3–B9 и поправки П1 (берётся НЕПРОЧИТАННОЕ на момент карточки), П7 (смена токена ядра), П10/К2/К3 (курсор
и рестарт без потерь), П13 (служебное), П14 (без SSE).

Ф57-1 (docs/architecture.md): модели нет. Разборщик не ходит никуда, кроме ядра: режим
«разобрать» убран, карточки ставятся только по правилам.

Ф8-3 (docs/architecture.md): кандидат берётся, только
если внимание сообщения `react` (`info`/`muted` не буферизуются); уведомление — СРАЗУ при приёме,
карточка по правилам — после тишины в беседе. Беседу, которую владелец видит в окне хаба (`GET /presence`),
прочитал или на которую ответил, не трогаем.

Журнал (B9, П8) — без текстов, содержаний и названий: id бесед, числа, метки, класс ошибки и HTTP-код. Токен
ядра здесь не виден вовсе (его держит `CoreApi`).
"""

import asyncio
import contextlib
import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from hub.core.extsort import ext_sort
from hub.core.presence import is_visible
from hub.mcp_server import format as fmt

from . import files
from .core_api import CoreApi, CoreError, CoreUnauthorized, CoreUnavailable

_logger = logging.getLogger("hub.agent")

DAY_MS = 24 * 60 * 60 * 1000
SYNC_PAGE_LIMIT = 500
PROJECTS_REFRESH_S = 600
# Ф64-1: есть проекты «по времени» — их рабочий день перечитывается чаще (уведомления молчат после
# «Закончить день» и возвращаются после «Начать день» не позже чем через минуту).
DAY_REFRESH_S = 60
BUFFER_CAP = 500  # B7: потолок кандидатов на беседу, старые выкидываются со счётчиком
FETCH_PAGE_MAX = 100  # К2: страница разбора <= 100, плюс ещё одна через `before=` — итого <= 200
FETCH_PAGE_SPARE = 5  # запас страницы свежего состояния сверх числа кандидатов (`_fetch_list`)
STATE_SAVE_EVERY_S = 60.0
ERRORS_KEEP = 20
EXIT_UNAUTHORIZED = 78
# П13: служебное не уходит наружу. Признака бота в `attrs` канала коннекторы не отдают (и API `attrs` не
# выдаёт) — по умолчанию только служебный чат Telegram и личка со Slackbot (долг: лички с ботами).
TELEGRAM_SERVICE_CHAT = "777000"
SLACKBOT_ID = "USLACKBOT"
SLACKBOT_TITLE = "Slackbot"  # коннектор Slack так называет личку со Slackbot (`convert.dm_title`)

# L4: поля долларового бюджета 7b в state.json и (Ф57-1) поля времён модели — вызовы за час, остановка
# разбора, последний ответ LLM — выкидываются при старте (файл битым не считается).
LEGACY_STATE_KEYS = (
    "day",
    "spent_usd",
    "budget_notified_day",
    "calls",
    "stopped",
    "last_llm_ok_ts",
    "calls_notified_ts",
)

# Ф8-3 B: внимание сообщения, которое разборщик берёт; прочие (`info`, `muted`) — мимо.
TAKEN_ATTENTION = ("react",)
PRESENCE_CACHE_S = 5.0  # B3: `GET /presence` — не чаще раза в 5 с
REACT_SUMMARY_MAX = 300  # B4: пересказ карточки по правилам — одна строка
REACT_NOTIFY_TEXT_MAX = 120  # B5: текст сообщения в уведомлении «реагирую»
PEER_NOTIFY_TEXT_MAX = 200  # Ф35-2: начало текста сообщения агентов в уведомлении
PEER_REPLY_NOTIFY_PER_HOUR = 6  # Ф35-2 Н8: уведомлений об ответах агентов владельцу — не больше в час
REACT_NOTIFIED_KEEP_MS = 24 * 60 * 60 * 1000
VOICE_LABEL_MAX = 200  # Ф14-6b: «[голосовое] <начало расшифровки>» в пересказе по правилам


def message_attention(message: dict) -> str | None:
    """Внимание сообщения от ядра. Ф57-1: сообщение без поля `attention` — `None` (не брать): режима
    «разобрать», которым оно считалось раньше, больше нет."""
    return message.get("attention") or None


def personal_mention(message: dict) -> bool:
    """Ф20-1 B2: личное упоминание — `mentions_me` без `mentions_broadcast` (`@channel`/`@here` — не
    личное)."""
    return bool(message.get("mentions_me")) and not message.get("mentions_broadcast")


def is_priority(message: dict, channel: dict) -> bool:
    """Ф20-1 A5: `priority` — личка или личное упоминание НЕ от бота."""
    if message.get("is_bot"):
        return False
    return channel.get("kind") == "dm" or personal_mention(message)


def voice_text(message: dict) -> str | None:
    """Ф14-6b: начало готовой непустой расшифровки голосового сообщения (одной строкой) или `None`."""
    for entry in message.get("media") or []:
        text = fmt.shown_transcript_text(entry)
        if text:
            return fmt.one_line(text.strip(), VOICE_LABEL_MAX)
    return None


def attachment_label(message: dict) -> str:
    """B4: чем заменить пустой текст в пересказе по правилам (без самого вложения). Ф14-6b: голосовое с
    готовой расшифровкой — «[голосовое] <начало расшифровки>»."""
    if message.get("deleted"):
        return "[удалено]"
    voice = voice_text(message)
    if voice:
        return f"[голосовое] {voice}"
    return "[вложение]" if message.get("attachments_json") else "[без текста]"


def transcript_wait_s(message: dict) -> float:
    """Ф14-6b П6: сколько ещё ждать расшифровку сообщения (поле ядра `transcript_wait_ms`; нет — 0)."""
    value = message.get("transcript_wait_ms")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value / 1000
    return 0.0


@dataclass
class Candidate:
    id: int
    first_seq: int
    ts: int
    ext_sort: int
    thread: str | None  # ext родителя, если это ответ в треде (не broadcast); иначе None — верхний уровень
    mentions_me: bool
    attention: str = "react"  # Ф8-3 B2: внимание сообщения при приёме (Ф57-1: берётся только `react`)

    @property
    def pos(self) -> tuple[int, int, int]:
        return (self.ts, self.ext_sort, self.id)


@dataclass
class Buffer:
    first_seen: float  # monotonic — время первого появления кандидата
    candidates: dict[int, Candidate] = field(default_factory=dict)
    priority: bool = False
    # Ф8-3 B2: буфер зреет по тишине: `last_added` — последний НОВЫЙ кандидат беседы.
    last_added: float = 0.0
    # Ф14-6b П6: беседа ждёт расшифровку голосового до этого момента (monotonic) — не зреет; бамп готовой
    # расшифровки (сообщение без `transcript_wait_ms`) снимает ожидание раньше.
    hold_until: float = 0.0


def message_pos(message: dict) -> tuple[int, int, int]:
    return (message["ts"], ext_sort(message.get("ext_id")), message["id"])


def _is_reply(message: dict) -> bool:
    thread = message.get("thread_ext_id")
    return thread is not None and thread != message.get("ext_id")


def _is_my_sent(message: dict) -> bool:
    """Ф11-2 B2: моё ОТПРАВЛЕННОЕ сообщение (есть `ext_id` — не заготовка), не удалённое и не скрытое."""
    return (
        bool(message.get("is_me"))
        and message.get("ext_id") is not None
        and not message.get("deleted")
        and not message.get("hidden")
        and isinstance(message.get("channel_id"), int)
    )


def _title_key(title: str) -> str:
    """`skip_channel_titles`: без учёта регистра, с начальным `#` или без («youtrack» == «#YouTrack»)."""
    return title.strip().removeprefix("#").strip().casefold()


def _peer_name(sender: object) -> str:
    """Ф35-2: имя агента в заголовке уведомления — роль (номер, если он не первый) или «без роли N»."""
    sender = sender if isinstance(sender, dict) else {}
    role, number = sender.get("role"), sender.get("role_no")
    number = number if isinstance(number, int) else 1
    if isinstance(role, str) and role:
        return role if number < 2 else f"{role} {number}"
    return f"без роли {number}"


def escape_markup(text: str) -> str:
    """notify-send понимает разметку в ТЕЛЕ уведомления: `& < >` экранируются (заголовок — простой текст)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_hhmm(value: str) -> int:
    hours, minutes = value.strip().split(":")
    return int(hours) * 60 + int(minutes)


def in_window(window: str | None, minute_of_day: int) -> bool:
    """`"23:00-08:00"` — окно может переходить через полночь. Пустое/битое — вне окна."""
    if not window:
        return False
    try:
        start_s, end_s = window.split("-")
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    except ValueError:
        return False
    if start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


Notifier = Callable[[str, str, str], None]  # (заголовок, текст, urgency)


class Agent:
    def __init__(
        self,
        *,
        core: CoreApi,
        config: dict,
        data_dir,
        notifier: Notifier,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.core = core
        self.config = config
        self.data_dir = data_dir
        self._notifier = notifier
        self._wall = wall
        self._mono = mono

        self.buffers: dict[int, Buffer] = {}
        self.channels: dict[int, dict] = {}
        self.projects: dict[int, dict] = {}
        self._projects_at: float | None = None
        self.sync_pos = 0
        self.state: dict = {}
        self._saved_at: float | None = None
        self._saved_seq: int | None = None

        # Ф34-2: уведомления о напоминаниях, уже показанные этим процессом, — `(id, fired_ts)`: сбой отметки в
        # ядре не повторяет уведомление каждый тик.
        self._reminders_sent: set[tuple[int, int | None]] = set()
        # Ф35-2: уведомления о строках переписки агентов, уже показанные этим процессом (сбой отметки не
        # повторяет уведомление), и время показанных уведомлений об ответах (Н8: не больше 6 в час).
        self._peer_sent: set[int] = set()
        self._peer_reply_at: list[float] = []
        self._presence_items: list[dict] = []
        self._presence_at: float | None = None
        self._react_backlog: set[int] = set()  # П7: беседы со старыми react-кандидатами за этот опрос

    # --- время, состояние, счётчики -----------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self._wall() * 1000)

    def _count(self, key: str, n: int = 1) -> None:
        counters = self.state.setdefault("counters", {})
        counters[key] = counters.get(key, 0) + n

    def _error(self, where: str, cls: str, code: int | None = None) -> None:
        errors = self.state.setdefault("errors", [])
        errors.append({"ts": self._now_ms(), "where": where, "cls": cls, "code": code})
        del errors[:-ERRORS_KEEP]

    def persisted_cursor(self) -> int:
        """П10: `min(first_seq кандидатов в буферах) − 1`; буферы пусты — конец последней прочитанной
        страницы. После рестарта `/sync` отдаст кандидатов заново (уже разобранных отсечёт К3)."""
        firsts = [c.first_seq for b in self.buffers.values() for c in b.candidates.values()]
        return min(firsts) - 1 if firsts else self.sync_pos

    def _save(self, *, force: bool = False) -> None:
        self.state["seq"] = self.persisted_cursor()
        mono = self._mono()
        if (
            not force
            and self._saved_seq == self.state["seq"]
            and self._saved_at is not None
            and mono - self._saved_at < STATE_SAVE_EVERY_S
        ):
            return
        try:
            files.save_state(self.data_dir, self.state)
        except OSError as exc:
            _logger.warning("agent: состояние не сохранено (%s)", exc.__class__.__name__)
            return
        self._saved_at = mono
        self._saved_seq = self.state["seq"]

    # --- старт и лента -------------------------------------------------------------------------------------

    async def start(self) -> None:
        """B3 + мелочи: первый старт без state — курсор = текущий `max_seq` (прошлое не разбираем); курсор
        больше `max_seq` (восстановление из бэкапа) или битый файл состояния — тоже `max_seq`, строка в
        журнал."""
        status = await self.core.get("/status")
        max_seq = int(status["max_seq"])
        state = files.load_state(self.data_dir)
        if state is None:
            if files.state_path(self.data_dir).exists():
                _logger.warning("agent: state.json битый — курсор = max_seq (%d)", max_seq)
            else:
                _logger.info("agent: первый старт — курсор = max_seq (%d), прошлое не разбирается", max_seq)
            state = {}
            cursor = max_seq
        else:
            cursor = state.get("seq")
            if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or cursor > max_seq:
                _logger.warning("agent: курсор состояния вне диапазона — курсор = max_seq (%d)", max_seq)
                cursor = max_seq
            else:
                _logger.info("agent: продолжаю с курсора %d (max_seq %d)", cursor, max_seq)
        for key in LEGACY_STATE_KEYS:
            state.pop(key, None)
        self.state = state
        self.sync_pos = cursor
        self.buffers.clear()
        await self._refresh_projects(force=True)
        self._save(force=True)

    async def _refresh_projects(self, *, force: bool = False, every: float = PROJECTS_REFRESH_S) -> None:
        mono = self._mono()
        if not force and self._projects_at is not None and mono - self._projects_at < every:
            return
        rows = await self.core.get("/projects")
        self.projects = {p["id"]: p for p in rows if isinstance(p.get("id"), int) and p["id"] >= 0}
        self._projects_at = mono

    async def _channel(self, channel_id: int) -> dict | None:
        channel = self.channels.get(channel_id)
        if channel is not None:
            return channel
        try:
            channel = await self.core.get(f"/channels/{channel_id}")
        except CoreError:
            return None
        self.channels[channel_id] = channel
        return channel

    def skip_reason(self, channel: dict) -> str | None:
        """B4 + П13: что вообще не разбирается."""
        if channel.get("muted"):
            return "muted"
        if channel.get("archived"):
            return "archived"
        project_id = channel.get("project_id")
        if project_id is not None:
            project = self.projects.get(project_id)
            if project is not None and project.get("muted"):
                return "project_muted"
            if project_id in (self.config.get("skip_projects") or []):
                return "skip_projects"
        if channel.get("id") in (self.config.get("skip_channels") or []):
            return "skip_channels"
        titles = self.config.get("skip_channel_titles") or []
        skip_titles = {_title_key(t) for t in titles if isinstance(t, str)}
        if channel.get("title") and _title_key(channel["title"]) in skip_titles:
            return "skip_channel_titles"
        ext_id = channel.get("ext_id")
        if channel.get("source") == "telegram" and ext_id == TELEGRAM_SERVICE_CHAT:
            return "service"
        if ext_id == SLACKBOT_ID or (
            channel.get("source") == "slack"
            and channel.get("kind") == "dm"
            and channel.get("title") == SLACKBOT_TITLE
        ):
            return "service"
        return None

    async def poll_once(self) -> int:
        """Страницы `/sync?since=<курсор>&limit=500` до `more=false` (П14: без SSE). `/sync` — только сигнал
        (П1): «в беседе X новые входящие». Новое — `first_seq` больше курсора на начало опроса: правка
        сообщения между страницами (`more=true`) не делает его новым второй раз, а сообщение, созданное и
        поправленное до опроса, на второй странице не теряется."""
        poll_since = self.sync_pos
        added = 0
        answered: set[int] = set()  # Ф11-2 B2: беседы с моим отправленным сообщением за этот опрос
        while True:
            page = await self.core.get("/sync", {"since": self.sync_pos, "limit": SYNC_PAGE_LIMIT})
            for channel in page.get("channels") or []:
                self.channels[channel["id"]] = channel
            for message in page.get("messages") or []:
                if _is_my_sent(message):
                    answered.add(message["channel_id"])
                if await self._consider(message, poll_since):
                    added += 1
            self.sync_pos = max(self.sync_pos, int(page["max_seq"]))
            if not page.get("more"):
                break
        for channel_id in sorted(answered):
            await self._close_answered(channel_id)
        self._flush_react_backlog()
        await self._refresh_projects()
        self.state["last_ok_ts"] = self._now_ms()
        self._save()
        return added

    async def _consider(self, message: dict, poll_since: int) -> bool:
        if message.get("hidden"):
            # Ф8-4 П8: удалено владельцем у себя в хабе — не разбирается; уже ждущий разбора кандидат снимается
            # (в `/sync` у скрытого только заглушка `{id, channel_id, hidden}` — без `first_seq`).
            buf = self.buffers.get(message.get("channel_id"))
            if buf is not None and buf.candidates.pop(message.get("id"), None) is not None:
                self._count("skipped_hidden")
                if not buf.candidates:
                    self._drop(message["channel_id"])
            return False
        if message.get("first_seq", 0) <= poll_since:
            # Ф14-6b П6: бамп `seq` по готовности расшифровки — не новый кандидат (второй карточки нет), но
            # беседа, ждавшая его, пересматривается сразу (остальных ждущих `_card` проверит заново).
            held = self.buffers.get(message.get("channel_id"))
            if (
                held is not None
                and held.hold_until
                and message.get("id") in held.candidates
                and not transcript_wait_s(message)
            ):
                held.hold_until = 0.0
            return False  # правка, реакция, удаление, прочтение — не повод для разбора
        if message.get("is_me") or message.get("kind") not in ("text", "file"):
            return False
        if message.get("deleted") or message.get("outbox_id") is not None:
            return False
        channel_id = message["channel_id"]
        channel = await self._channel(channel_id)
        if channel is None or self.skip_reason(channel) is not None:
            return False
        # Ф8-3 B1: «к сведению», сводка и заглушённое не буферизуются вовсе.
        attention = message_attention(message)
        if attention not in TAKEN_ATTENTION:
            self._count("skipped_attention")
            return False
        buf = self.buffers.get(channel_id)
        if buf is None:
            buf = self.buffers[channel_id] = Buffer(first_seen=self._mono())
        if message["id"] in buf.candidates:
            return False
        thread = (
            message["thread_ext_id"] if _is_reply(message) and not message.get("thread_broadcast") else None
        )
        cand = Candidate(
            id=message["id"],
            first_seq=message["first_seq"],
            ts=message["ts"],
            ext_sort=ext_sort(message.get("ext_id")),
            thread=thread,
            mentions_me=personal_mention(message),
            attention=attention,
        )
        buf.candidates[cand.id] = cand
        buf.last_added = self._mono()
        wait_s = transcript_wait_s(message)
        if wait_s:
            buf.hold_until = max(buf.hold_until, buf.last_added + wait_s)
        await self._notify_react(channel, message, thread)
        if is_priority(message, channel):
            buf.priority = True
        if len(buf.candidates) > BUFFER_CAP:
            oldest = sorted(buf.candidates.values(), key=lambda c: c.pos)[: len(buf.candidates) - BUFFER_CAP]
            for c in oldest:
                del buf.candidates[c.id]
            self._count("dropped_cap", len(oldest))
            _logger.warning(
                "agent: беседа #%d — буфер переполнен, выкинуто %d старых", channel_id, len(oldest)
            )
        return True

    async def _close_answered(self, channel_id: int) -> None:
        """Ф11-2 B2: в беседе моё отправленное сообщение — ядро само проверяет «отвечено» открытой карточки и
        закрывает её. Отказ — warning в журнал; опрос не падает и курсор не держит."""
        try:
            body = await self.core.post("/agent/items/close-answered", json={"channel_id": channel_id})
        except (CoreError, CoreUnavailable) as exc:
            self._error("core", "close_answered", getattr(exc, "status", None))
            _logger.warning(
                "agent: беседа #%d — закрыть отвеченную карточку не удалось (%s)",
                channel_id,
                exc.__class__.__name__,
            )
            return
        closed = body.get("closed") if isinstance(body, dict) else None
        for item_id in closed or []:
            self._count("closed_answered")
            _logger.info("agent: карточка #%s закрыта: отвечено (беседа #%d)", item_id, channel_id)

    # --- тик: созревшие беседы -----------------------------------------------------------------------------

    def _mature(self, buf: Buffer, mono: float) -> bool:
        if buf.hold_until > mono:
            return False  # Ф14-6b П6: ждёт расшифровку голосового (не дольше срока ядра)
        # Ф8-3 B2: люди пишут в несколько сообщений — карточка после тишины в беседе (без НОВЫХ
        # кандидатов), но не позже `react_max_wait_s` от первого.
        age = mono - buf.first_seen
        return mono - buf.last_added >= float(self.config.get("react_silence_s", 15)) or age >= float(
            self.config.get("react_max_wait_s", 120)
        )

    def _day_closed(self, project_id) -> bool:
        """Ф64-1 (П10): рабочий день проекта закрыт (по кэшу `/projects`) — уведомления по нему молчат."""
        project = self.projects.get(project_id) if isinstance(project_id, int) else None
        return project is not None and project.get("day_open") is False

    async def tick(self, *, only_mature: bool = True) -> int:
        """Созревшие беседы по одной (П9); лички и упоминания — первыми (П12). Возвращает число бесед, по
        которым поставлена карточка. `CoreUnavailable` — наружу: тик прерван, буферы и курсор целы."""
        if any(p.get("workday") for p in self.projects.values()):
            await self._refresh_projects(every=DAY_REFRESH_S)  # Ф64-1: смена дня — не позже минуты
        await self._notify_reminders()
        await self._notify_peer_msgs()
        mono = self._mono()
        ready = [cid for cid, buf in self.buffers.items() if not only_mature or self._mature(buf, mono)]
        ready.sort(key=lambda cid: (not self.buffers[cid].priority, self.buffers[cid].first_seen))
        attempted = 0
        try:
            for channel_id in ready:
                if channel_id in self.buffers and await self._card(channel_id):
                    attempted += 1
        finally:
            self._save()
        return attempted

    def _drop(self, channel_id: int) -> None:
        """К2: после попытки карточки ВСЕ кандидаты беседы снимаются с буфера — курсор П10 не залипает."""
        self.buffers.pop(channel_id, None)

    async def _card(self, channel_id: int) -> bool:
        """Карточка по правилам созревшей беседы: свежее состояние кандидатов, отсев прочитанного,
        виденного, отвеченного и покрытого прежней карточкой (К3), затем `_react_card`. `True` — карточка
        отправлена в ядро."""
        buf = self.buffers[channel_id]
        try:
            channel = await self.core.get(f"/channels/{channel_id}")
        except CoreError as exc:
            _logger.info("agent: беседа #%d недоступна (HTTP %d) — кандидаты сняты", channel_id, exc.status)
            self._count("channel_gone", len(buf.candidates))
            self._drop(channel_id)
            return False
        self.channels[channel_id] = channel
        if self.skip_reason(channel) is not None:
            self._count("skipped", len(buf.candidates))
            self._drop(channel_id)
            return False

        fresh = await self._fetch(channel_id, buf)
        now_ms = self._now_ms()
        max_age_ms = int(self.config.get("max_age_days", 3)) * DAY_MS
        to_triage: list[dict] = []
        overflow = read_or_gone = too_old = 0
        for cand in buf.candidates.values():
            message = fresh.get(cand.id)
            if message is None:
                overflow += 1
                continue
            if message.get("deleted") or message.get("is_me") or not message.get("unread"):
                read_or_gone += 1  # П1: прочитал на телефоне между сигналом и разбором — не разбирается
                continue
            if message["ts"] < now_ms - max_age_ms:
                too_old += 1  # П1: предохранитель для аккаунта без маркеров прочтения
                continue
            to_triage.append(message)
        if overflow:
            self._count("overflow", overflow)
            _logger.warning(
                "agent: беседа #%d — %d кандидатов не вошли в выборку разбора", channel_id, overflow
            )
        if read_or_gone:
            self._count("read_or_deleted", read_or_gone)
        if too_old:
            self._count("too_old", too_old)

        # Ф14-6b П6: хоть одно сообщение ждёт расшифровку — буфер остаётся (не `_drop`), беседа
        # пересматривается после срока ядра или по бампу готовой расшифровки. Одна карточка на всё.
        wait_s = max((transcript_wait_s(m) for m in to_triage), default=0.0)
        if wait_s:
            buf.hold_until = self._mono() + wait_s
            self._count("transcript_wait")
            return False
        buf.hold_until = 0.0

        to_triage = await self._seen_by_me(channel, to_triage)

        covered_seq = await self._covered_seq(channel_id)
        if covered_seq is not None and to_triage:
            # К3 (после приёмки, F1): покрытие — по порядку поступления в хаб (`first_seq`), а не по позиции:
            # ночной догон со старым `ts`, пришедший после живого сообщения, покрытым не считается.
            kept = [m for m in to_triage if m["first_seq"] > covered_seq]
            if len(kept) != len(to_triage):
                self._count("covered", len(to_triage) - len(kept))
            to_triage = kept

        # Ф8-3 П17: по СВЕЖЕМУ вниманию сообщений на момент карточки: не `react` (режим поменяли, пока
        # беседа зрела) — сняты.
        react_msgs = [m for m in to_triage if message_attention(m) == "react"]
        if len(react_msgs) != len(to_triage):
            self._count("skipped_attention", len(to_triage) - len(react_msgs))
        if react_msgs:
            await self._react_card(channel, sorted(react_msgs, key=message_pos))
        self._drop(channel_id)
        return bool(react_msgs)

    async def _presence(self) -> list[dict]:
        """B3: `GET /presence` с кэшем <= 5 с. Ядро старее Ф8-3 (404) или отказ — «никого нет» (разбор не
        останавливается); ядро недоступно — наружу, как любой запрос тика."""
        mono = self._mono()
        if self._presence_at is not None and mono - self._presence_at < PRESENCE_CACHE_S:
            return self._presence_items
        try:
            body = await self.core.get("/presence")
            items = [i for i in body.get("items") or [] if isinstance(i, dict)]
        except CoreError:
            items = []
        self._presence_items, self._presence_at = items, mono
        return items

    @staticmethod
    def _thread_of(message: dict) -> str | None:
        if _is_reply(message) and not message.get("thread_broadcast"):
            return message["thread_ext_id"]
        return None

    async def _seen_by_me(self, channel: dict, to_triage: list[dict]) -> list[dict]:
        """Ф8-3 B3 + П8 + П17: беседа в присутствии (верхний уровень — окно беседы, ответ — открытая панель
        этого треда) и отвеченное (МОЁ сообщение в беседе, включая треды, позже кандидата — та же позиция, что
        `answered` карточки) — сняты без карточки."""
        if not to_triage:
            return to_triage
        items = await self._presence()
        kept = [m for m in to_triage if not is_visible(items, channel["id"], self._thread_of(m))]
        if len(kept) != len(to_triage):
            self._count("present", len(to_triage) - len(kept))
        mine = channel.get("last_my_message")
        if kept and isinstance(mine, dict) and isinstance(mine.get("ts"), int):
            my_pos = (mine["ts"], mine.get("ext_sort") or 0, mine.get("id") or 0)
            answered = [m for m in kept if message_pos(m) < my_pos]
            if answered:
                self._count("answered", len(answered))
                kept = [m for m in kept if message_pos(m) >= my_pos]
        return kept

    async def _react_card(self, channel: dict, messages: list[dict]) -> None:
        """Ф8-3 B4 + П10: карточка «реагирую» по правилам: `reply` от `rules`, пересказ — «отправитель: текст
        последнего сообщения», `action` — почему реакция. Уведомление уже было при приёме (B5)."""
        channel_id = channel["id"]
        last = messages[-1]
        text = fmt.one_line(last.get("text"), REACT_SUMMARY_MAX) if not last.get("deleted") else ""
        summary = fmt.one_line(
            f"{fmt.sender_text(last)}: {text or attachment_label(last)}", REACT_SUMMARY_MAX
        )
        if any(personal_mention(m) for m in messages):
            action = "упоминание"
        elif channel.get("kind") == "dm":
            action = "личное"
        elif any(m.get("for_me") and self._thread_of(m) for m in messages):
            action = "тред"
        else:
            action = "беседа «реагирую»"
        response = await self._post_card(
            channel_id,
            label="reply",
            summary=summary,
            action=action,
            message_ids=[m["id"] for m in messages][-200:],
            provider="rules",
            model="-",
        )
        self._count("react_cards")
        _logger.info(
            "agent: беседа #%d — карточка по правилам (%s), сообщений %d%s",
            channel_id,
            action,
            len(messages),
            " (без изменений)" if response is not None and response.get("noop") else "",
        )

    async def _notify_react(self, channel: dict, message: dict, thread: str | None) -> None:
        """Ф8-3 B5 + П7: уведомление «реагирую» СРАЗУ при приёме — только непрочитанное, не в присутствии,
        не в тихие часы и не чаще раза в `react_notify_every_s` на беседу (метка — в state: переживает рестарт
        и откат курсора). Сообщение старше `react_notify_max_age_s` (догон после сна) — в одну сводную строку
        за опрос."""
        if not message.get("unread"):
            return
        if is_visible(await self._presence(), channel["id"], thread):
            self._count("notify_present")
            return
        if self._quiet_now():
            self._count("notify_quiet")
            return
        if self._day_closed(channel.get("project_id")):
            # Ф64-1 (П10): день проекта закрыт — как тихие часы (карточка по правилам заводится как обычно).
            self._count("notify_day_closed")
            return
        now_ms = self._now_ms()
        notified = self.state.setdefault("react_notified", {})
        stale = [
            k for k, t in notified.items() if not isinstance(t, int) or now_ms - t > REACT_NOTIFIED_KEEP_MS
        ]
        for key in stale:
            del notified[key]
        last = notified.get(str(channel["id"]))
        if isinstance(last, int) and now_ms - last < int(self.config.get("react_notify_every_s", 180)) * 1000:
            return
        notified[str(channel["id"])] = now_ms
        if message.get("ts", now_ms) < now_ms - int(self.config.get("react_notify_max_age_s", 600)) * 1000:
            self._react_backlog.add(channel["id"])
            return
        project_id = channel.get("project_id")
        project = "Неразобранное" if project_id is None else (self.projects.get(project_id) or {}).get("name")
        title = f"Хаб · {project or f'проект {project_id}'}: {channel.get('title') or 'беседа'}"
        text = fmt.one_line(message.get("text"), REACT_NOTIFY_TEXT_MAX) if not message.get("deleted") else ""
        self._count("notify_react")
        self._send(title, f"{fmt.sender_text(message)}: {text or attachment_label(message)}")

    def _flush_react_backlog(self) -> None:
        """П7: после сна — одна строка «N бесед ждут реакции» вместо пачки уведомлений."""
        if not self._react_backlog:
            return
        count, self._react_backlog = len(self._react_backlog), set()
        self._count("notify_react_backlog")
        self._send("Хаб: ждут реакции", f"{count} бесед ждут реакции")

    async def _covered_seq(self, channel_id: int) -> int | None:
        """К3: `covered_seq` самой свежей по нему карточки беседы (любой статус). Берутся последние карточки
        по `updated_ts`: обновляется только открытая карточка, а новая создаётся лишь с `covered_seq` выше
        прежних — самая свежая по `covered_seq` среди них."""
        rows = await self.core.get("/agent/items", {"channel": channel_id, "status": "any", "limit": 20})
        covered = [r["covered_seq"] for r in rows if isinstance(r.get("covered_seq"), int)]
        return max(covered) if covered else None

    async def _fetch_list(self, path: str, cands: list[dict]) -> list[dict]:
        limit = min(len(cands) + FETCH_PAGE_SPARE, FETCH_PAGE_MAX)
        page = await self.core.get(path, {"limit": limit})
        messages = list(page.get("messages") or [])
        oldest = min(c["pos"] for c in cands)
        if page.get("next_before") and messages and message_pos(messages[0]) > oldest:
            older = await self.core.get(path, {"limit": FETCH_PAGE_MAX, "before": page["next_before"]})
            messages = list(older.get("messages") or []) + messages
        return messages

    async def _fetch(self, channel_id: int, buf: Buffer) -> dict[int, dict]:
        """П1: СВЕЖЕЕ состояние кандидатов беседы на момент карточки (`id -> сообщение`) — страница канала
        (кандидаты верхнего уровня) и страницы тредов (ответы). Самый старый кандидат не попал — ещё одна
        страница через `before=` (К2)."""
        fresh: dict[int, dict] = {}
        top = [{"pos": c.pos} for c in buf.candidates.values() if c.thread is None]
        by_thread: dict[str, list[dict]] = {}
        for c in buf.candidates.values():
            if c.thread is not None:
                by_thread.setdefault(c.thread, []).append({"pos": c.pos})
        if top:
            for message in await self._fetch_list(f"/channels/{channel_id}/messages", top):
                fresh[message["id"]] = message
        for thread, cands in by_thread.items():
            path = f"/channels/{channel_id}/threads/{quote(thread, safe='')}/messages"
            for message in await self._fetch_list(path, cands):
                fresh.setdefault(message["id"], message)
        return fresh

    # --- карточки, уведомления ------------------------------------------------------------------------------

    async def _post_card(self, channel_id: int, **fields) -> dict | None:
        body = {"channel_id": channel_id, **fields}
        try:
            response = await self.core.post("/agent/items", json=body)
        except CoreError as exc:
            # Тело 422 не читаем и не пишем (П8): только код и имена полей запроса.
            self._count("core_rejected")
            self._error("core", "agent_items", exc.status)
            _logger.warning(
                "agent: ядро отвергло карточку беседы #%d (HTTP %d; поля %s)",
                channel_id,
                exc.status,
                ",".join(sorted(body)),
            )
            return None
        if isinstance(response, dict) and response.get("closed_item_id") is not None:
            # Ф11-2 B2: ядро разделило карточку по ответу/прочтению владельца.
            why = "отвечено" if response.get("closed_status") == "done" else "прочитано"
            self._count("card_split")
            _logger.info(
                "agent: карточка #%s закрыта (%s), новая #%s",
                response["closed_item_id"],
                why,
                response.get("id"),
            )
        return response

    def _quiet_now(self) -> bool:
        moment = datetime.fromtimestamp(self._wall())
        return in_window(self.config.get("quiet_hours"), moment.hour * 60 + moment.minute)

    def _send(self, title: str, text: str, urgency: str = "critical") -> None:
        try:
            self._notifier(fmt.one_line(title), escape_markup(fmt.one_line(text)), urgency)
        except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять разбор
            _logger.warning("agent: уведомление не отправлено (%s)", exc.__class__.__name__)

    async def _notify_reminders(self) -> None:
        """Ф34-2 решение 6 + П5: сработавшие напоминания, которым пора на экран владельцу (ядро само держит 20
        минут для напоминаний сессий с ролью), — уведомление и отметка `notified_ts`. В тихие часы ничего не
        шлётся и НЕ отмечается: уведомление уйдёт после окна. Сбой ручки ловится здесь — разбору он не
        мешает и паузу цикла не удваивает; 401 — наружу (токен отозван — как у всего цикла)."""
        if self._quiet_now():
            return
        try:
            due = await self.core.get("/agent/reminders/due")
            rows = due.get("items") if isinstance(due, dict) else None
            items = [i for i in rows or [] if isinstance(i, dict)]
            sent: list[int] = []
            for item in items:
                if not isinstance(item.get("id"), int):
                    continue
                if self._day_closed(item.get("project_id")):
                    continue  # Ф64-1 (П10): не шлётся и НЕ отмечается — уведомление после «Начать день»
                key = (item["id"], item.get("fired_ts"))
                if key not in self._reminders_sent:
                    channel = item.get("channel") if isinstance(item.get("channel"), dict) else {}
                    if item.get("channel_id") is None and "channel_id" in item:
                        # Ф89-1 (П9): без беседы — «Хаб: напоминание <роль>».
                        role = item.get("role") if isinstance(item.get("role"), str) else None
                        title = f"Хаб: напоминание {role}" if role else "Хаб: напоминание"
                    else:
                        title = f"Хаб: напоминание — {channel.get('title') or 'беседа'}"
                    self._send(title, item.get("note") or "срок напоминания наступил")
                    self._reminders_sent.add(key)
                sent.append(item["id"])
            if sent:
                await self.core.post("/agent/reminders/notified", json={"ids": sent})
                self._count("notify_reminders", len(sent))
        except CoreUnauthorized:
            raise
        except (CoreUnavailable, CoreError) as exc:
            self._error("reminders", exc.__class__.__name__)
            _logger.warning("agent: напоминания не проверены (%s)", exc)
        except Exception as exc:  # noqa: BLE001 — уведомления не должны ронять разбор
            self._error("reminders", exc.__class__.__name__)
            _logger.warning("agent: напоминания не проверены (%s)", exc.__class__.__name__)

    async def _notify_peer_msgs(self) -> None:
        """Ф35-2 (П7, Н8): ответы агентов владельцу — уведомление и отметка `notified_ts`, один раз на строку.
        В тихие часы ничего не шлётся и НЕ отмечается. Уведомлений об ответах — не больше
        `PEER_REPLY_NOTIFY_PER_HOUR` в час: лишние отмечаются без показа (остаётся счётчик на вкладке).
        Сбой ручки ловится здесь, как у напоминаний; 401 — наружу."""
        if self._quiet_now():
            return
        try:
            got = await self.core.get("/agent/peer-msgs/alerts")
            rows = got.get("items") if isinstance(got, dict) else None
            done: list[int] = []
            for item in rows or []:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                    continue
                sender = item.get("from") if isinstance(item.get("from"), dict) else {}
                if self._day_closed(sender.get("project_id")):
                    continue  # Ф64-1 (П10): агент проекта с закрытым днём — без уведомления и без отметки
                if item["id"] not in self._peer_sent:
                    text = fmt.one_line(item.get("text") or "", PEER_NOTIFY_TEXT_MAX)
                    mono = self._mono()
                    self._peer_reply_at = [t for t in self._peer_reply_at if mono - t < 3600]
                    if len(self._peer_reply_at) < PEER_REPLY_NOTIFY_PER_HOUR:
                        self._peer_reply_at.append(mono)
                        self._send(f"Хаб: ответ агента «{_peer_name(item.get('from'))}»", text)
                    else:
                        self._count("notify_peer_suppressed")
                    self._peer_sent.add(item["id"])
                done.append(item["id"])
            if done:
                await self.core.post("/agent/peer-msgs/notified", json={"ids": done})
                self._count("notify_peer_msgs", len(done))
        except CoreUnauthorized:
            raise
        except (CoreUnavailable, CoreError) as exc:
            self._error("peer_msgs", exc.__class__.__name__)
            _logger.warning("agent: переписка агентов не проверена (%s)", exc)
        except Exception as exc:  # noqa: BLE001 — уведомления не должны ронять разбор
            self._error("peer_msgs", exc.__class__.__name__)
            _logger.warning("agent: переписка агентов не проверена (%s)", exc.__class__.__name__)

    # --- процесс --------------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> int:
        """Цикл процесса. Ядро недоступно — пауза и повтор, курсор не двигается; 401 ядра и после
        перечитывания токена — выход 78. По SIGTERM — попытка поставить карточки созревших буферов за 20 с
        (П10)."""
        backoff = float(self.config.get("poll_s", 5))
        while not stop.is_set():
            try:
                await self.start()
                break
            except CoreUnauthorized:
                _logger.error("agent: ядро не принимает токен агента — hub agent setup; выход 78")
                return EXIT_UNAUTHORIZED
            except CoreError as exc:
                # 403 и т.п.: у токена нет нужного скоупа — рестарт не поможет, нужен `hub agent setup`.
                _logger.error(
                    "agent: ядро отказало на старте (HTTP %d) — hub agent setup; выход 78", exc.status
                )
                return EXIT_UNAUTHORIZED
            except CoreUnavailable as exc:
                _logger.warning("agent: ядро недоступно на старте (%s)", exc)
                await _wait(stop, backoff)
                backoff = min(backoff * 2, 60.0)
        poll_s = float(self.config.get("poll_s", 5))
        tick_s = float(self.config.get("tick_s", 15))
        next_tick = self._mono()
        delay = poll_s
        while not stop.is_set():
            try:
                await self.poll_once()
                if self._mono() >= next_tick:
                    await self.tick()
                    next_tick = self._mono() + tick_s
                delay = poll_s
            except CoreUnauthorized:
                _logger.error("agent: ядро не принимает токен агента — hub agent setup; выход 78")
                self._save(force=True)
                return EXIT_UNAUTHORIZED
            except CoreUnavailable as exc:
                self._error("core", "unavailable")
                _logger.warning("agent: ядро недоступно (%s) — пауза", exc)
                delay = min(max(delay, poll_s) * 2, 60.0)
            except Exception as exc:  # noqa: BLE001 — непредвиденное: журнал без текстов, цикл жив
                frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
                where = f"{frame.filename}:{frame.lineno}" if frame else "?"
                self._error("agent", exc.__class__.__name__)
                _logger.error("agent: непредвиденная ошибка %s в %s", exc.__class__.__name__, where)
                delay = min(max(delay, poll_s) * 2, 60.0)
            await _wait(stop, delay)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.tick(), timeout=20)
        self._save(force=True)
        return 0


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
