"""Клиент внутреннего сокета (`CoreClient`) и каркас цикла отправки (`ConnectorBase`). Спецификация:
docs/architecture.md. Настоящие коннекторы фаз 1–5
наследуют `ConnectorBase` и реализуют `sync`/`send`/`reconcile`/`health`.

Контракт каркаса (раздел 4 плана, повторено здесь дословно):
- исключение из `send()` БЕЗ явного результата -> `unknown`, НЕ `temporary`: каркас не знает, ушло ли
  сообщение. `temporary` коннектор возвращает только сам и только когда уверен, что отправки не было;
- `mode='reconcile'` никогда не вызывает `send()`;
- ошибка авторизации источника -> `set_status('auth_required')` и выход из цикла отправки без ретраев;
- core недоступен -> бэкофф с джиттером, настенные часы; события не теряются (буфер коннектора ограничен,
  при переполнении — догон от курсора, а не тихий сброс). Это относится к `sync()`/буферу конкретного
  коннектора — `ConnectorBase` даёт только контракт, реализация буфера и бэкоффа — дело подкласса
  (см. `ConnectorBase.sync`), здесь она не тестируется отдельно (нет протокола источника, о котором можно
  было бы «недоступен»).
"""

import abc
import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

import httpx

from ..diag import install_task_dump

if TYPE_CHECKING:
    from .group_ops import GroupOpResult
    from .join import JoinResult
    from .lookup import LookupResult
    from .msg_ops import MsgOpResult
    from .press import PressResult
    from .reactions import ReactionResult
    from .read_ack import ReadAckResult

_logger = logging.getLogger(__name__)

_BASE_URL = "http://hub-internal/internal/v1"

# Раздел D спеки куска 1a фазы 1 (`docs/architecture.md`) — внешний цикл `ConnectorBase.run()`.
_EVENT_BUFFER_CAP = 5000
_PUSH_BATCH = 500
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BACKOFF_JITTER = 0.2
_SEND_TIMEOUT_S = 60.0
_SYNC_TIMEOUT_S = 600.0
_SYNC_INTERVAL_S = 600.0
_SYNC_JITTER_S = 180.0
# Сверка каркаса упала временной ошибкой (ядро после загрузки отвечает дольше `_CORE_TIMEOUT_S`, сеть ещё не
# поднялась) — повтор той же причины через эти паузы, не больше стольких попыток подряд; успешная сверка
# сбрасывает счётчик. Без повтора догон ждал плановой сверки 600±180 с (16.10: опоздание на 7–13 мин).
_SYNC_REDO_DELAYS_S = (30.0, 60.0, 120.0, 240.0, 300.0)
# Повтор созрел, но очередь сверки не пуста или сверка идёт — ждать столько и смотреть снова.
_SYNC_REDO_POLL_S = 5.0
# Временные ошибки сверки: транспорт до ядра/источника, таймауты (в т.ч. `_SYNC_TIMEOUT_S` каркаса —
# `asyncio.TimeoutError` это `TimeoutError`, а тот — `OSError`). `AuthRequired`, 401 и прочие ответы с кодом
# сюда не входят.
_SYNC_TRANSIENT_ERRORS = (httpx.TransportError, httpx.TimeoutException, TimeoutError, OSError)
_HEALTH_INTERVAL_S = 60.0
_SLEEP_TICK_S = 5.0
_SLEEP_THRESHOLD_S = 30.0
_WAIT_OUTBOX_S = 25.0
_WAIT_SLEEP_S = 25.0  # Ф70-1: long-poll сна аккаунта (ядро режет до 30 с)
_FLUSH_ON_STOP_S = 5.0
_AUTH_EXIT_CODE = 78
_CORE_TIMEOUT_S = 30.0  # ядро на локальном сокете; дольше — считаем недоступным
_STOP_SOURCE_TIMEOUT_S = 10.0
_STOP_BATCH_RETRY_MS = 60_000  # остаток пачки после stop_batch() без срока от источника
STOP_PEER_FLOOD = "peer_flood"  # Ф69-1 П5: режим `stop_batch()` после спам-ограничения (PeerFlood)
_MEDIA_PUT_CHUNK = 256 * 1024  # Ф8-5 П4: кусок тела PUT content
# Ф10-4 П3 (`docs/architecture.md`): отправка с файлом (скачать из ядра, залить в источник) —
# свой таймаут `send()`; ядро держит лиз такой строки не меньше 420 с.
FILE_SEND_TIMEOUT_S = 300.0
_OUTBOX_FILE_TIMEOUT_S = 120.0  # скачивание байтов задания из ядра (до 50 МиБ по локальному сокету)

# Находка F14 (round 2 критика куска 1b фазы 1): приоритет причины сверки при слиянии очереди в одну
# (`_sync_scheduler_loop`) — самая «дорогая»/срочная выигрывает; неизвестная подклассу причина (не из этого
# списка) уходит в конец, но не теряется — берётся первой попавшейся, если список пуст ни разу не совпал.
_SYNC_REASON_PRIORITY = (
    "resume",
    "start",
    "overflow",
    "reconnect",
    "reconnect_quick",
    # Ф42-1: «catchup» (остались незаполненные дыры догона Telegram) — полная сверка; стоит ВЫШЕ узких
    # «folders»/«join», иначе слияние причин отдало бы проход узкой и заполнение ждало бы графика.
    "catchup",
    "folders",
    "initial",
    "unknown_chat",
    "event_error",
    "scheduled",
    # Ф17-1, ревью (находка 5): «clock» (скачок часов) — полная сверка; «join» — узкая, только загрузка бесед
    # после вступления: при слиянии проигрывает любой другой причине.
    "clock",
    "join",
)


def _pick_main_sync_reason(reasons: set[str]) -> str:
    for r in _SYNC_REASON_PRIORITY:
        if r in reasons:
            return r
    return next(iter(reasons))


class SendResult(NamedTuple):
    # "abandon" — кусок 1c фазы 1 (`docs/architecture.md`): результат отправки неизвестен (сеть/
    # таймаут посреди запроса источника), но повтор безопасен у идемпотентного источника — report() НЕ
    # вызывается вовсе, лиз истекает сам (см. `ConnectorBase._process_lease`/`idempotent`).
    outcome: Literal["sent", "unknown", "temporary", "flood", "permanent", "abandon"]
    ext_id: str | None = None
    error: str | None = None
    retry_after_ms: int | None = None
    # Ф12-1 B1: черновик новому адресату — настоящая беседа (Telegram — id пользователя в формате
    # `channel_ext_id` личек, почта — X-GM-THRID своего письма) и её название; только при `sent`.
    peer_ext_id: str | None = None
    peer_title: str | None = None
    # Ф63-2 П2: адресат беседы-заготовки — бот (Telegram, `entity.bot`); только при `sent` с `peer_ext_id`.
    # Ядро ставит новой беседе `attrs.tg_type = "bot"`, чтобы ответ боту сразу шёл веткой `bot_chat`.
    peer_bot: bool | None = None
    # Ф11-3b B1: альбом Telegram (и `fake`) — `ext_id` всех N сообщений отправления (`ext_ids[0] == ext_id`)
    # и общий `grouped_id` источника; только при `sent`, остальным отчётам не передаются.
    ext_ids: tuple[str, ...] | None = None
    grouped_id: str | None = None
    # Ревью Ф34-3: только у `temporary` с `retry_after_ms` — строка отложена ДО любого запроса к источнику
    # (лимит/пауза поиска по номеру телефона): ядро попытку не тратит и аккаунт не останавливает.
    postponed: bool = False
    # Ф40-1 A: время сообщения по часам ИСТОЧНИКА, мс (Mattermost — `create_at` созданного поста); только при
    # `sent`. Ядро ставит его строке-заготовке вместо часов хаба со времени создания черновика. Источник,
    # у которого время сидит в самом `ext_id` (Slack), поле не заполняет.
    ts_ms: int | None = None


def lease_files_meta(lease: dict) -> list[dict]:
    """Ф11-3b B1: файлы задания outbox (контракт A↔B: `files` — `None` или список `{size, mime, name, kind,
    sha256}` в порядке вложений). Не список/пусто -> `[]`; элементы — только словари. Старого поля `file`
    каркас не понимает (ядро и коннекторы деплоятся вместе)."""
    files = lease.get("files")
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, dict)]


_AUTH_REASON_MAX = 200


def _auth_reason(reason: str | None) -> str | None:
    """Ф51-1 A2: причина отказа входа для журнала и `last_error` — одна строка, не длиннее 200 знаков."""
    text = " ".join((reason or "").split())
    if not text:
        return None
    return text if len(text) <= _AUTH_REASON_MAX else text[: _AUTH_REASON_MAX - 1] + "…"


class AuthRequired(Exception):
    """Источник отказал в авторизации. Коннектор поднимает это исключение из `send()`/`reconcile()`/
    `health()`; `ConnectorBase` переводит его в `set_status('auth_required')` и останавливает отправку."""


class OutboxFileError(Exception):
    """Ф10-4 B1: байты файла задания outbox получить не удалось (ядро ответило 404/409 — лиз уже не наш,
    размер не сошёлся, сеть до ядра, диск). Отправки не было — коннектор отвечает `temporary`. `reason` —
    короткий код для отчёта и журнала (без имён файлов)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CoreClient:
    """Асинхронный клиент внутреннего сокета: `httpx.AsyncClient` поверх `AsyncHTTPTransport(uds=...)`.

    `transport` — необязательная правка куска 5b (см. `docs/architecture.md`, раздел
    «Стенд»): тестам нужен `CoreClient` поверх `httpx.ASGITransport`, реального unix-сокета в
    сквозных тестах нет. По умолчанию поведение не меняется — `socket_path` по-прежнему создаёт
    `AsyncHTTPTransport(uds=...)`, как раньше; `transport`, если передан, просто заменяет его целиком.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        secret: str = "",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if transport is None:
            if socket_path is None:
                raise ValueError("CoreClient: нужен либо socket_path, либо transport")
            transport = httpx.AsyncHTTPTransport(uds=socket_path)
        # Таймаут задан явно: умолчание httpx — 5 с, а long-poll `/outbox/wait` держит запрос 25 с — каждый
        # wait падал ReadTimeout, цикл отправки уходил в бэкофф, отправка после простоя ждала до ~35 с
        # (находка 1 критика 1a). `wait_outbox` дополнительно даёт своему запросу запас сверх long-poll.
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=httpx.Timeout(_CORE_TIMEOUT_S),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> CoreClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def push_events(self, events: list[dict], cursor: dict | None = None) -> dict:
        body: dict = {"events": events}
        if cursor is not None:
            body["cursor"] = cursor
        r = await self._client.post("/events", json=body)
        r.raise_for_status()
        return r.json()

    async def get_cursor(self) -> dict | None:
        r = await self._client.get("/cursor")
        r.raise_for_status()
        return r.json()

    async def lease(self, *, limit: int | None = None, lease_ms: int | None = None) -> list[dict]:
        body: dict = {}
        if limit is not None:
            body["limit"] = limit
        if lease_ms is not None:
            body["lease_ms"] = lease_ms
        r = await self._client.post("/outbox/lease", json=body)
        r.raise_for_status()
        return r.json()

    async def report(
        self,
        *,
        outbox_id: int,
        lease_gen: int,
        outcome: str,
        ext_id: str | None = None,
        error: str | None = None,
        retry_after_ms: int | None = None,
        peer_ext_id: str | None = None,
        peer_title: str | None = None,
        peer_bot: bool | None = None,
        ext_ids: list[str] | tuple[str, ...] | None = None,
        grouped_id: str | None = None,
        postponed: bool = False,
        ts_ms: int | None = None,
    ) -> dict:
        body: dict = {
            "outbox_id": outbox_id,
            "lease_gen": lease_gen,
            "outcome": outcome,
            "ext_id": ext_id,
            "error": error,
            "retry_after_ms": retry_after_ms,
        }
        # Ф12-1 B1: поля привязки — только когда есть (обычный отчёт — прежней формы).
        if peer_ext_id is not None:
            body["peer_ext_id"] = peer_ext_id
        if peer_title is not None:
            body["peer_title"] = peer_title
        if peer_bot:  # Ф63-2 П2: только когда адресат — бот (обычный отчёт — прежней формы)
            body["peer_bot"] = True
        # Ф11-3b B1: альбом — только когда есть (контракт A↔B: `ext_ids[0] == ext_id`).
        if ext_ids is not None:
            body["ext_ids"] = list(ext_ids)
        if grouped_id is not None:
            body["grouped_id"] = grouped_id
        if postponed:  # ревью Ф34-3: поле — только когда есть (обычный отчёт — прежней формы)
            body["postponed"] = True
        if ts_ms is not None:  # Ф40-1 A: время источника — только когда есть (обычный отчёт — прежней формы)
            body["ts_ms"] = ts_ms
        r = await self._client.post("/outbox/report", json=body)
        r.raise_for_status()
        return r.json()

    async def wait_outbox(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/outbox/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def fetch_outbox_file(
        self,
        outbox_id: int,
        lease_gen: int,
        idx: int,
        dest_path: str | Path,
        *,
        expected_size: int | None = None,
    ) -> int:
        """Ф10-4 B1 + Ф11-3b A4/B1: байты файла `idx` задания — `GET /outbox/{id}/file?lease_gen=N&idx=K`
        потоком в `dest_path` (файл уже создан вызывающим; перезаписывается). 404/409 (лиз не наш/устарел,
        `idx` вне списка), нет `Content-Length`, он не равен `expected_size` (размер из `files[idx]`
        задания) или тело другой длины -> `OutboxFileError`. Прочие ответы и сеть — исключения httpx как
        есть. Возвращает число записанных байт."""
        async with self._client.stream(
            "GET",
            f"/outbox/{int(outbox_id)}/file",
            params={"lease_gen": lease_gen, "idx": int(idx)},
            timeout=_OUTBOX_FILE_TIMEOUT_S,
        ) as r:
            if r.status_code in (404, 409):
                raise OutboxFileError(f"core_{r.status_code}")
            r.raise_for_status()
            raw_length = r.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else None
            except ValueError:
                length = None
            if length is None or length < 0:
                raise OutboxFileError("no_content_length")
            if expected_size is not None and length != expected_size:
                raise OutboxFileError("size_mismatch")
            written = 0
            with open(dest_path, "wb") as f:  # noqa: ASYNC230 — локальный файл, куски по мере прихода
                async for chunk in r.aiter_raw():
                    written += len(chunk)
                    if written > length:
                        raise OutboxFileError("size_mismatch")
                    f.write(chunk)
            if written != length:
                raise OutboxFileError("short_body")
            return written

    # --- медиа (Ф8-5, `docs/architecture.md`.4 + П3/П4) — по образцу outbox -----------------

    async def wait_media(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/media/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_media(self, *, limit: int = 1) -> list[dict]:
        """Задания `{media_id, lease_gen, max_bytes, message: {channel_ext_id, ext_id, thread_ext_id}, idx,
        kind, mime, size, name, src_id, ref}` (П3)."""
        r = await self._client.post("/media/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def put_media_content(
        self, *, media_id: int, lease_gen: int, path: str, size: int, timeout_s: float
    ) -> dict:
        """П4: тело — временный файл коннектора потоком, `Content-Length` = его размер (ядро отклоняет тело
        другой длины). Файл читается кусками — целиком в памяти его нет."""

        async def _chunks():
            with open(path, "rb") as f:  # noqa: ASYNC230 — локальный файл, куски по 256 КиБ
                while True:
                    chunk = f.read(_MEDIA_PUT_CHUNK)
                    if not chunk:
                        return
                    yield chunk

        r = await self._client.put(
            f"/media/{media_id}/content",
            params={"lease_gen": lease_gen},
            content=_chunks(),
            headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
            timeout=timeout_s,
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    async def report_media(
        self,
        *,
        media_id: int,
        lease_gen: int,
        outcome: str,
        error: str | None = None,
        retry_after_s: float | None = None,
        size: int | None = None,
    ) -> dict:
        """`outcome`: `gone|temporary|too_big|auth` (A2.4). `retry_after_s` — только FloodWait (П15: попыткой
        не считается); `size` — узнанный размер при `too_big` (П6: ядро ставит `deferred` с размером)."""
        body: dict = {"lease_gen": lease_gen, "outcome": outcome, "error": error}
        if retry_after_s is not None:
            body["retry_after_s"] = retry_after_s
        if size is not None:
            body["size"] = size
        r = await self._client.post(f"/media/{media_id}/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get_activity(self) -> dict:
        """Ф20-3: `{active_ms, now_ms, enabled}` — когда владелец последний раз работал в хабе (по часам ядра) и
        включён ли статус «На связи» у этого аккаунта (`slack_status`)."""
        r = await self._client.get("/activity")
        r.raise_for_status()
        return r.json()

    # --- прочитанность в источник (Ф8-7, `docs/architecture.md`) — по образцу медиа --------

    async def wait_read_ack(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/read-ack/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_read_ack(self, *, limit: int = 1) -> list[dict]:
        """Задания `{channel_id, gen, lease_gen, channel: {ext_id, kind, parent_ext_id}, message: {ext_id, ts,
        ext_sort, thread_ext_id, is_me}}` (A3 + П1: `message` — действующая цель)."""
        r = await self._client.post("/read-ack/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_read_ack(
        self, *, channel_id: int, lease_gen: int, outcome: str, retry_after_ms: int | None = None
    ) -> dict:
        """`outcome`: `done|retry|drop` (A4). `retry_after_ms` — пауза, которую велел источник."""
        body: dict = {"channel_id": channel_id, "lease_gen": lease_gen, "outcome": outcome}
        if retry_after_ms is not None:
            body["retry_after_ms"] = retry_after_ms
        r = await self._client.post("/read-ack/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- реакции в источник (Ф10-3, `docs/architecture.md`) — по образцу прочитанности -----

    async def wait_reactions(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/reactions/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_reactions(self, *, limit: int = 1) -> list[dict]:
        """Задания `{message_id, channel_id, gen, lease_gen, channel: {ext_id, kind, parent_ext_id},
        message_ext_id, ops: [[emoji, on], …], current_mine?}` (П6: `current_mine` — только Telegram)."""
        r = await self._client.post("/reactions/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_reaction(
        self,
        *,
        message_id: int,
        lease_gen: int,
        outcome: str,
        retry_after_ms: int | None = None,
        reason: str | None = None,
    ) -> dict:
        """`outcome`: `done|retry|drop`. `retry_after_ms` — пауза, которую велел источник. `reason` (Ф15-1
        П1) — причина `done`: `owner_reacted` — операции сессии не применены."""
        body: dict = {"lease_gen": lease_gen, "outcome": outcome}
        if retry_after_ms is not None:
            body["retry_after_ms"] = retry_after_ms
        if reason is not None:
            body["reason"] = reason[:64]
        r = await self._client.post(f"/reactions/{message_id}/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- правка и «удалить у всех» (Ф14-8, `docs/architecture.md`) — как реакции ----

    async def wait_msg_ops(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/msg-ops/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_msg_ops(self, *, limit: int = 1) -> list[dict]:
        """Задания `{message_id, gen, lease_gen, op: edit|delete, text (edit), channel: {ext_id, kind,
        parent_ext_id}, message_ext_id, thread_ext_id, message_kind, has_files}`."""
        r = await self._client.post("/msg-ops/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_msg_op(
        self,
        *,
        message_id: int,
        lease_gen: int,
        outcome: str,
        code: str | None = None,
        detail: str | None = None,
        retry_after_ms: int | None = None,
    ) -> dict:
        """`outcome`: `done|retry|fail` (`fail` — с `code`). `detail` — класс/код ошибки, без текста."""
        body: dict = {"lease_gen": lease_gen, "outcome": outcome}
        if code is not None:
            body["code"] = code
        if detail is not None:
            body["detail"] = detail
        if retry_after_ms is not None:
            body["retry_after_ms"] = retry_after_ms
        r = await self._client.post(f"/msg-ops/{message_id}/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- вступление в группы и каналы (Ф17-1, `docs/architecture.md`) — как прочитанность ---

    async def wait_joins(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/join/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_joins(self, *, limit: int = 1) -> list[dict]:
        """Задания `{id, lease_gen, step: peek|join, target_kind: username|invite, target, ext_id,
        project_id}`."""
        r = await self._client.post("/join/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_join(self, body: dict) -> dict:
        """`{id, lease_gen, outcome: done|retry|drop, retry_after_ms?, reason?, peek? (шаг peek), result?,
        ext_id?, title? (шаг join)}` — тело собирает `connectors/join.Joiner`."""
        r = await self._client.post("/join/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- выход из групп и каналов (Ф17-2, `docs/architecture.md`) — как вступление ----------

    async def wait_leaves(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/leave/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_leaves(self, *, limit: int = 1) -> list[dict]:
        """Задания `{id, lease_gen, step: "leave", ext_id, chat_kind: group|channel, project_id}`."""
        r = await self._client.post("/leave/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_leave(self, body: dict) -> dict:
        """`{id, lease_gen, outcome: done|retry|drop, retry_after_ms?, reason?, result?: left|not_member}` —
        тело собирает `connectors/join.Joiner`."""
        r = await self._client.post("/leave/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- операции над группами (Ф62-6, `docs/architecture.md`) — как выход ----------------

    async def wait_group_ops(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/group-op/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_group_ops(self, *, limit: int = 1) -> list[dict]:
        """Задания `{id, lease_gen, kind: create|add|remove|link|link_reset, ext_id, title, participants:
        [{i, jid, phone?}], project_id, first_leased_ts, attempts, exclude? (create)}`."""
        r = await self._client.post("/group-op/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_group_op(self, body: dict) -> dict:
        """`{id, lease_gen, outcome: done|retry|drop, retry_after_ms?, postpone_ms?, reason?, result?}` — тело
        собирает `connectors/group_ops.GroupOpWorker`."""
        r = await self._client.post("/group-op/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- нажатия кнопок ботов (Ф44-1c, `docs/architecture.md`) ---------------------

    async def wait_presses(self, timeout_s: float) -> bool:
        r = await self._client.get(
            "/button-presses/wait", params={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        return bool(r.json()["ready"])

    async def lease_presses(self, *, limit: int = 1) -> list[dict]:
        """Нажатия `{id, lease_gen, channel_ext_id, message_ext_id, data_b64}` — единственное место, где
        сырой callback кнопки выходит из ядра."""
        r = await self._client.post("/button-presses/lease", json={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def report_press(self, press_id: int, body: dict) -> dict:
        """`{lease_gen, outcome: done|failed|unknown|retry, result? (done), reason?, retry_after_ms?
        (retry)}` — тело собирает `connectors/press.Presser`."""
        r = await self._client.post(f"/button-presses/{press_id}/report", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- поиск и чтение Telegram без вступления (Ф29-1, `docs/architecture.md`) -----

    async def lookup_next(self, timeout_s: float) -> dict | None:
        """Long-poll: заявка `{id, kind: search|peek, params}` своего аккаунта или `None` (204)."""
        r = await self._client.post(
            "/lookup/next", json={"timeout_s": timeout_s}, timeout=timeout_s + _CORE_TIMEOUT_S
        )
        r.raise_for_status()
        if r.status_code == 204 or not r.content:  # noqa: PLR2004
            return None
        return r.json()

    async def lookup_result(self, body: dict) -> dict:
        """`{id, outcome: ok|error, payload? (ok), reason?, retry_after_s? (error)}` — тело собирает
        `connectors/lookup.Lookuper`."""
        r = await self._client.post("/lookup/result", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        r = await self._client.post("/account/status", json={"status": status, "last_error": last_error})
        r.raise_for_status()
        return r.json()

    async def get_account(self) -> dict:
        r = await self._client.get("/account")
        r.raise_for_status()
        return r.json()

    async def wait_sleep(self, *, asleep: bool, timeout_s: float) -> dict | None:
        """Ф70-1 A.4: `GET /sleep/wait` — сон аккаунта (`{project_id, since_ts}` или `None`). Отвечает сразу,
        если сон уже не `asleep`, иначе — при смене или по сроку."""
        r = await self._client.get(
            "/sleep/wait",
            params={"asleep": "true" if asleep else "false", "timeout_s": timeout_s},
            timeout=timeout_s + _CORE_TIMEOUT_S,
        )
        r.raise_for_status()
        sleep = r.json().get("sleep")
        return sleep if isinstance(sleep, dict) else None

    async def channels_heads(self, *, since_ts: int | None = None) -> list[dict]:
        """Ф42-3 B: с `since_ts` (мс) строки несут ещё `project_id` и `refill_lo_ext_sort` (границы добора
        истории); без него запрос и ответ прежние."""
        if since_ts is not None:
            r = await self._client.get("/channels/heads", params={"since_ts": since_ts})
            r.raise_for_status()
            return r.json()
        r = await self._client.get("/channels/heads")
        r.raise_for_status()
        return r.json()

    async def channels_window(
        self, *, ext_id: str, limit: int = 30, thread_ext_id: str | None = None
    ) -> list[str]:
        params: dict[str, str | int] = {"ext_id": ext_id, "limit": limit}
        if thread_ext_id is not None:
            params["thread_ext_id"] = thread_ext_id
        r = await self._client.get("/channels/window", params=params)
        r.raise_for_status()
        return r.json()

    async def threads_heads(self, *, since_ms: int) -> list[dict]:
        """Раздел H спеки 2b / «Решения архитектора» спеки 2c: `GET /threads/heads?since_ms=` — треды
        своего аккаунта, опора коннектора Slack для `conversations.replies`."""
        r = await self._client.get("/threads/heads", params={"since_ms": since_ms})
        r.raise_for_status()
        return r.json()

    async def set_activity(self, *, backfill: bool) -> dict:
        r = await self._client.post("/account/activity", json={"backfill": backfill})
        r.raise_for_status()
        return r.json()

    async def account_alert(self, *, kind: str, text: str, detail: dict | None = None) -> dict:
        """Кусок 1c: `POST /account/alert` — алерт о новой сессии источника (раздел «Алерт о новой сессии
        Telegram» спеки). `detail` — устройство/платформа/страна/дата, БЕЗ IP (правило логов)."""
        r = await self._client.post(
            "/account/alert", json={"kind": kind, "text": text, "detail": detail}
        )
        r.raise_for_status()
        return r.json()


def _is_unauthorized(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401


def _is_core_unreachable(exc: BaseException) -> bool:
    """Ошибка соединения, 5xx, таймаут — раздел D: «core недоступен» (в отличие от 401, который не про
    доступность, а про отозванный секрет)."""
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500  # noqa: PLR2004


def _is_rejected(exc: BaseException) -> bool:
    """4xx кроме 401 — core понял запрос и отказал; повтор того же запроса бессмыслен."""
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and 400 <= exc.response.status_code < 500
        and (  # noqa: PLR2004
            exc.response.status_code != 401  # noqa: PLR2004
        )
    )


def _own_task_cancelling() -> bool:
    """Находка F2 критика куска 1c: `True`, если ТЕКУЩУЮ задачу реально попросили остановиться
    (`task.cancel()` на неё саму — штатный SIGTERM/`_trigger_exit`), а не что-то ниже по стеку. Источник
    проблемы: Telethon (`gateway.disconnect()`, в т.ч. из `on_resume()`) отменяет СВОИ внутренние
    pending-future при разрыве соединения — это всплывает как `asyncio.CancelledError` из середины
    `send()`/`sync()`, хотя НАШУ задачу никто не просил останавливаться; раньше это убивало
    `_outbox_loop()`/`_sync_scheduler_loop()` насмерть (строки/сверка повисали, процесс жил, никто не
    заметил бы). `Task.cancelling()` (3.11+, счётчик необработанных запросов на отмену) различает одно от
    другого; вне задачи (не должно случаться в проде) считаем «не наша», чтобы не заглушить реальный сигнал
    молча."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class ConnectorBase(abc.ABC):
    """Каркас цикла отправки. `step_outbox()` — один шаг (взять лизы, обработать, отчитаться) как отдельный
    метод, а не часть бесконечного цикла — чтобы тесты могли прогнать шаг без реального ожидания.

    `run()` — внешний цикл коннектора (раздел D спеки куска 1a): буфер `emit()`/толкатель, детектор сна,
    планировщик `sync()`, пульс `health()`, цикл `step_outbox()`, дослать буфер и выйти по SIGTERM/SIGINT.
    Всё время — через инжектируемые `wall`/`mono`/`sleep` (по умолчанию настоящие часы и `asyncio.sleep`) —
    тесты подменяют их, чтобы не ждать реальных секунд.

    `sends_enabled: bool = True` (атрибут класса, кусок 1b фазы 1) — при `False` цикл отправки в `run()` НЕ
    запускается вовсе: `wait_outbox`/`lease` ни разу не вызываются. `TelegramConnector` в 1b ставит `False`
    (отправка — кусок 1c), `FakeConnector` и остальные коннекторы 1a — не переопределяют, остаются `True`.

    `idempotent: bool = False` (атрибут класса, кусок 1c) — источник сам схлопывает повтор одной и той же
    отправки (Telegram: `random_id`). Только такой коннектор может вернуть `SendResult("abandon")` из
    `send()`/`reconcile()`; у неидемпотентного `abandon` превращается в `unknown` (см. `_process_lease`).

    `lease_ms: int = 60_000` (атрибут класса, кусок 1c) — срок лиза `step_outbox()`; Telegram ставит 120_000
    (решения архитектора куска 1c: сеть+ретраи Telethon внутри `send()` могут занять больше умолчания).

    `read_ack_enabled: bool = False` (атрибут класса, Ф8-7 B3) — `run()` запускает толкатель прочитанности
    (`connectors/read_ack.ReadAcker`) отдельной задачей рядом с циклом отправки; задания выполняет
    `push_read(item)` подкласса.

    `reactions_enabled: bool = False` (атрибут класса, Ф10-3 B) — `run()` запускает толкатель реакций
    (`connectors/reactions.Reactor`) отдельной задачей; задания выполняет `push_reaction(job)` подкласса.

    `msg_ops_enabled: bool = False` (атрибут класса, Ф14-8 B) — `run()` запускает толкатель правок и
    «удалить у всех» (`connectors/msg_ops.MsgOpWorker`) отдельной задачей; задания выполняет
    `push_msg_op(job)` подкласса.

    `join_enabled: bool = False` (атрибут класса, Ф17-1 C) — `run()` запускает исполнителя заявок на
    вступление (`connectors/join.Joiner`) отдельной задачей; шаги выполняет `push_join(item)` подкласса, после
    принятого ядром `done` — `after_join(item, result)`.

    `leave_enabled: bool = False` (атрибут класса, Ф17-2 C) — `run()` запускает исполнителя заявок на выход
    (тот же `connectors/join.Joiner` со своими вызовами ядра) отдельной задачей; шаг выполняет
    `push_leave(item)` подкласса.

    `group_ops_enabled: bool = False` (атрибут класса, Ф62-6 D) — `run()` запускает исполнителя операций над
    группами (`connectors/group_ops.GroupOpWorker`) отдельной задачей; операцию выполняет
    `push_group_op(item)` подкласса.

    `lookup_enabled: bool = False` (атрибут класса, Ф29-1 C1) — `run()` запускает исполнителя поиска и чтения
    без вступления (`connectors/lookup.Lookuper`) отдельной задачей; заявки выполняет `run_lookup(item)`
    подкласса.

    `outbox_tmp_dir` (Ф10-4 B1) — каталог временных файлов отправки с файлами (`lease_files()`); подкласс
    ставит его в `media_tmp_dir` (`<data_dir>/media/.tmp-conn/<id>/`). `None` — системный временный каталог.

    `sleep_enabled: bool = False` (атрибут класса, Ф70-1 C) — источник умеет спать вместе с днём проекта:
    ядро говорит «аккаунт спит» (`GET /sleep/wait`), каркас зовёт `stop_receiving()`, не запускает сверку,
    не зовёт `health()` (пульс — просто `ok`) и `on_resume()`; сон снят — `start_receiving()` и сверка
    `resume`. Отправка и действия владельца (прочитано, реакции, правки) при сне идут как обычно — что выдать,
    решает ядро (`outbox.lease`). Без `sleep_enabled` каркас только пишет в журнал «сон не поддерживается».
    """

    sends_enabled: bool = True
    idempotent: bool = False
    lease_ms: int = 60_000
    read_ack_enabled: bool = False
    reactions_enabled: bool = False
    msg_ops_enabled: bool = False
    join_enabled: bool = False
    # Ф44-1c: `run()` запускает исполнителя нажатий кнопок ботов (`connectors/press.Presser`) отдельной
    # задачей; нажатие выполняет `push_press(item)` подкласса.
    press_enabled: bool = False
    leave_enabled: bool = False
    group_ops_enabled: bool = False
    lookup_enabled: bool = False
    sleep_enabled: bool = False
    outbox_tmp_dir: Path | None = None

    def __init__(
        self,
        client: CoreClient,
        *,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self.auth_required = False
        self._wall = wall
        self._mono = mono
        self._sleep = sleep

        self._event_buffer: list[dict] = []
        self._buffer_gen = 0  # растёт при каждой очистке буфера переполнением
        # Ф14-8 П7 (`flush_events`): сколько событий легло в буфер за всё время и сколько из него ушло
        # (отправлено в ядро или выброшено — переполнением/отказом пачки).
        # Всегда `_emit_count - _gone_count == len(буфер)`.
        self._emit_count = 0
        self._gone_count = 0
        # Ф42-1: сколько раз события были ВЫБРОШЕНЫ, не дойдя до ядра (переполнение буфера, отказ пачки,
        # несериализуемое событие). `flush_events` считает выброшенное «ушедшим» — кому важно, что события
        # именно ДОШЛИ (догон Telegram двигает курсор только после этого), сверяет счётчик до и после.
        self._lost_count = 0
        self._gone_event = asyncio.Event()
        self._stop_batch = False
        self._stop_mode: str | None = None  # Ф69-1 П5: `"peer_flood"` — остаток пачки без паузы аккаунта
        self._heartbeat_now = asyncio.Event()
        self._buffer_event = asyncio.Event()
        self._need_resync = False

        self._sync_queue: asyncio.Queue[str] = asyncio.Queue()
        self._sync_lock = asyncio.Lock()
        # Будится `_request_sync()` — НЕ `queue.get()` наперегонки со сном (находка при правке F14: та гонка
        # вместе с `task.cancel()` на «проигравшем» `queue.get()` — классическая ловушка `asyncio.Queue`,
        # изредка теряющая элемент, если `put()` и отмена пересекаются; `Event` ничего не «держит», драним
        # очередь отдельно, только через `get_nowait()`, который не бывает отменён на середине).
        self._sync_wake = asyncio.Event()
        # Находка F14 (round 2 критика 1b куска 1b): причины сверки, слитые в текущий проход, но НЕ ставшие
        # главной (см. `_sync_scheduler_loop`) — подкласс может заглянуть сюда, если разным причинам нужна
        # разная разовая реакция сверх той, что уже делает главная причина.
        self._pending_reasons: frozenset[str] = frozenset()
        # Повтор упавшей сверки (`_schedule_sync_redo`): одна задача за раз, её причина и сколько повторов
        # подряд уже заказано. Имена не `_sync_retry_*` — те заняты собственным повтором Slack/Mattermost.
        self._sync_redo_task: asyncio.Task | None = None
        self._sync_redo_reason: str | None = None
        self._sync_redo_count = 0

        self._stopping = False
        self._stop_event = asyncio.Event()
        self._exit_code = 0

        # Ф70-1 C: сон вместе с днём проекта. `_sleep_known` — что ядро сказало последним (для long-poll, в
        # том числе у источника без поддержки сна); `_sleeping` — каркас и правда спит (`sleep_enabled`).
        self._sleep_known = False
        self._sleeping = False
        self._awake = asyncio.Event()
        self._awake.set()
        # Ноутбук спал, пока спал канал: `on_resume` (у почты — сброс рабочих соединений отправки) — при
        # выходе канала из сна (ревью Ф70-1, п.4).
        self._resume_pending = False

    @property
    def sleeping(self) -> bool:
        """Ф70-1: аккаунт спит вместе с днём проекта (приём остановлен); статус «На связи» при сне не
        ставится."""
        return self._sleeping

    # --- хуки подкласса: обязательные -----------------------------------------------------------------

    @abc.abstractmethod
    async def sync(self, reason: str = "scheduled") -> None:
        """Забрать новые события источника и передать их в core. Настоящие коннекторы кладут их через
        `emit()` (буфер `run()`), а не зовут `CoreClient.push_events` напрямую — `reason` объясняет, почему
        сверка происходит именно сейчас (`"start"`/`"scheduled"`/`"resume"`/`"overflow"`)."""

    @abc.abstractmethod
    async def send(self, lease: dict) -> SendResult:
        """Отправить сообщение источнику. НИКОГДА не вызывается для лизов с `mode == 'reconcile'`."""

    @abc.abstractmethod
    async def reconcile(self, lease: dict) -> SendResult:
        """Сверить с источником, ушло ли сообщение (`mode == 'reconcile'`). Должен вернуть только
        `sent`/`unknown` — outbox.report() отклонит любой другой исход для reconcile-лиза (409)."""

    @abc.abstractmethod
    async def health(self) -> None:
        """Проверить, что источник доступен и авторизация в силе. Бросает `AuthRequired` при её потере."""

    # --- хуки подкласса: необязательные (по умолчанию — no-op) ----------------------------------------

    async def start_source(self) -> None:
        """Поднять соединение с источником — до запуска фоновых задач `run()`."""
        return None  # noqa: B027 — намеренно НЕ abstractmethod: подкласс переопределяет по желанию

    async def stop_source(self) -> None:
        """Закрыть соединение с источником — после остановки фоновых задач `run()`."""
        return None  # noqa: B027

    async def on_resume(self) -> None:
        """Ноутбук проснулся (детектор сна, раздел D): рвать и поднимать соединение с источником заново."""
        return None  # noqa: B027

    async def stop_receiving(self) -> None:
        """Ф70-1 C.2: аккаунт уснул вместе с днём проекта — перестать получать (живой поток закрыт). По
        умолчанию — `stop_source()`; источник, которому для отправки нужны клиенты из `start_source()`,
        переопределяет и закрывает только приём."""
        await self.stop_source()

    async def start_receiving(self) -> None:
        """Ф70-1 C.2: сон снят — снова получать. По умолчанию — `start_source()`; сверку `resume` (догон за
        время сна) каркас закажет сам."""
        await self.start_source()

    async def push_read(self, item: dict) -> ReadAckResult:
        """Ф8-7 B3: отметить в источнике прочитанным до `item["message"]` (формат —
        `CoreClient.lease_read_ack`). Вызывается только при `read_ack_enabled`; умолчание — `drop`
        (источник не умеет)."""
        from .read_ack import ReadAckResult  # noqa: PLC0415 — read_ack импортирует base

        del item
        return ReadAckResult("drop", reason="unsupported")

    def _make_read_acker(self):
        from .read_ack import ReadAcker  # noqa: PLC0415 — read_ack импортирует base

        return ReadAcker(
            self._client,
            self.push_read,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
        )

    async def push_reaction(self, job: dict) -> ReactionResult:
        """Ф10-3 B: привести реакции владельца на сообщении `job` к операциям `job["ops"]` (формат —
        `CoreClient.lease_reactions`) и после успеха прислать событие `reaction` с правдой источника.
        Вызывается только при `reactions_enabled`; умолчание — `drop` (источник не умеет)."""
        from .reactions import ReactionResult  # noqa: PLC0415 — reactions импортирует base

        del job
        return ReactionResult("drop", reason="unsupported")

    def _make_reactor(self):
        from .reactions import Reactor  # noqa: PLC0415 — reactions импортирует base

        return Reactor(
            self._client,
            self.push_reaction,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
        )

    async def push_msg_op(self, job: dict) -> MsgOpResult:
        """Ф14-8 B: выполнить у источника правку (`job["op"] == "edit"`, текст `job["text"]`) или «удалить у
        всех» (`delete`) своего сообщения (формат — `CoreClient.lease_msg_ops`) и после успеха выпустить
        событие `edit`/`delete` с правдой источника. Вызывается только при `msg_ops_enabled`; умолчание —
        `fail`/`other` (источник не умеет)."""
        from .msg_ops import MsgOpResult  # noqa: PLC0415 — msg_ops импортирует base

        del job
        return MsgOpResult("fail", code="other", detail="unsupported")

    def _make_msg_op_worker(self):
        from .msg_ops import MsgOpWorker  # noqa: PLC0415 — msg_ops импортирует base

        return MsgOpWorker(
            self._client,
            self.push_msg_op,
            flush=self.flush_events,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
        )

    async def push_join(self, item: dict) -> JoinResult:
        """Ф17-1 C: шаг заявки на вступление (формат — `CoreClient.lease_joins`): `peek` — посмотреть цель,
        `join` — вступить. Вызывается только при `join_enabled`; умолчание — `drop` (источник не умеет)."""
        from .join import JoinResult  # noqa: PLC0415 — join импортирует base

        del item
        return JoinResult("drop", reason="unsupported")

    async def after_join(self, item: dict, result: JoinResult) -> None:
        """Ф17-1 C/П4: после принятого ядром `done` — например, стартовая загрузка беседы, в которую вступили.
        Не дольше мгновения: долгую работу подкласс ставит своей задачей. Умолчание — ничего."""
        del item, result

    def _make_joiner(self):
        from .join import Joiner  # noqa: PLC0415 — join импортирует base

        return Joiner(
            self._client,
            self.push_join,
            after=self.after_join,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
        )

    async def push_press(self, item: dict) -> PressResult:
        """Ф44-1c: нажать callback-кнопку (формат — `CoreClient.lease_presses`). РОВНО один запрос к
        источнику, без повторов. Вызывается только при `press_enabled`; умолчание — `failed` (не умеет)."""
        from .press import PressResult  # noqa: PLC0415 — press импортирует base

        del item
        return PressResult("failed", reason="unsupported")

    def _make_presser(self):
        from .press import Presser  # noqa: PLC0415 — press импортирует base

        return Presser(
            self._client,
            self.push_press,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
        )

    async def push_leave(self, item: dict) -> JoinResult:
        """Ф17-2 C: шаг заявки на выход (формат — `CoreClient.lease_leaves`): выйти из группы или канала.
        `done` — с `result` `left|not_member`. Вызывается только при `leave_enabled`; умолчание — `drop`
        (источник не умеет)."""
        from .join import JoinResult  # noqa: PLC0415 — join импортирует base

        del item
        return JoinResult("drop", reason="unsupported")

    def _make_leaver(self):
        from .join import Joiner  # noqa: PLC0415 — join импортирует base

        return Joiner(
            self._client,
            self.push_leave,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
            wait=lambda timeout_s: self._client.wait_leaves(timeout_s),
            lease=lambda *, limit: self._client.lease_leaves(limit=limit),
            report=lambda body: self._client.report_leave(body),
            name="leave",
        )

    async def push_group_op(self, item: dict) -> GroupOpResult:
        """Ф62-6 D: операция над группой (формат — `CoreClient.lease_group_ops`). Вызывается только при
        `group_ops_enabled`; умолчание — `drop unsupported` (источник не умеет)."""
        from .group_ops import GroupOpResult  # noqa: PLC0415 — group_ops импортирует base

        del item
        return GroupOpResult("drop", reason="unsupported")

    def _make_group_op_worker(self):
        from .group_ops import GroupOpWorker  # noqa: PLC0415 — group_ops импортирует base

        return GroupOpWorker(
            self._client,
            self.push_group_op,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
            wait=lambda timeout_s: self._client.wait_group_ops(timeout_s),
            lease=lambda *, limit: self._client.lease_group_ops(limit=limit),
            report=lambda body: self._client.report_group_op(body),
            name="group_op",
        )

    async def run_lookup(self, item: dict) -> LookupResult:
        """Ф29-1 C1: заявка поиска/чтения (формат — `CoreClient.lookup_next`). Вызывается только при
        `lookup_enabled`; умолчание — `error unsupported` (источник не умеет)."""
        from .lookup import LookupResult  # noqa: PLC0415 — lookup импортирует base

        del item
        return LookupResult("error", reason="unsupported")

    def _make_lookuper(self):
        from .lookup import Lookuper  # noqa: PLC0415 — lookup импортирует base

        return Lookuper(
            self._client,
            self.run_lookup,
            mono=lambda: self._mono(),
            sleep=lambda s: self._sleep(s),
            is_blocked=lambda: self.auth_required,
            on_auth_required=self._on_auth_required,
            on_unauthorized=lambda: self._trigger_exit(_AUTH_EXIT_CODE),
        )

    # --- буфер событий («толкатель») -------------------------------------------------------------------

    def _mark_gone(self, n: int) -> None:
        """Ф14-8 П7: `n` событий покинули буфер (отправлены или выброшены) — будит `flush_events`."""
        if n > 0:
            self._gone_count += n
            self._gone_event.set()

    async def flush_events(self, timeout_s: float) -> bool:
        """Ф14-8 П7: дождаться, пока все события, положенные в буфер ДО вызова, отправлены в ядро или
        выброшены (переполнение/отказ пачки считаются «ушли»). `False` — не дождались за `timeout_s`
        (толкатель завис или ядро недоступно) — вызывающий идёт дальше всё равно."""
        target = self._emit_count
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        while self._gone_count < target:
            self._gone_event.clear()
            left = deadline - loop.time()
            if left <= 0:
                return False
            try:
                await asyncio.wait_for(self._gone_event.wait(), timeout=left)
            except TimeoutError:
                return self._gone_count >= target
        return True

    def emit(self, event: dict) -> None:
        """Положить событие в ограниченную очередь (ёмкость `_EVENT_BUFFER_CAP`). Переполнение -> буфер
        целиком очищается, выставляется `need_resync` — тихой потери нет, догон отвечает сверка/курсор
        подкласса (раздел D). Порядок событий внутри буфера сохраняется (обычный список, FIFO).

        Несериализуемое в JSON событие (datetime/bytes из сырого объекта источника — ошибка коннектора) сюда
        не попадает: иначе толкатель повторял бы ту же пачку вечно, а всё, что за ней, стояло бы (находка 3
        критика 1a). Оно выбрасывается с ошибкой в журнал и `need_resync`."""
        try:
            json.dumps(event)
        except TypeError, ValueError:
            _logger.error("connector: несериализуемое событие — выброшено, будет sync")
            self._lost_count += 1
            self._need_resync = True
            self._buffer_event.set()
            return
        if len(self._event_buffer) >= _EVENT_BUFFER_CAP:
            self._mark_gone(len(self._event_buffer))
            self._event_buffer.clear()
            self._buffer_gen += 1
            self._lost_count += 1
            self._need_resync = True
        else:
            self._event_buffer.append(event)
            self._emit_count += 1
        self._buffer_event.set()

    async def _apply_backoff(self, backoff_s: float) -> float:
        jitter = backoff_s * _BACKOFF_JITTER * (2 * random.random() - 1)  # noqa: S311 — джиттер, не крипто
        await self._sleep(max(0.0, backoff_s + jitter))
        return min(backoff_s * 2, _BACKOFF_CAP_S)

    async def _pusher_loop(self) -> None:
        """Толкатель буфера. Когда буфер пуст, но `need_resync` висит (переполнение только что очистило
        буфер целиком, толкать нечего) — шлёт ПУСТУЮ пачку как пробу связи: core принимает `{"events": []}`
        как валидный no-op, поэтому её успех — надёжный признак «связь восстановлена», без чего
        `sync(reason="overflow")` было бы не от чего оттолкнуть (раздел D: «после восстановления связи»)."""
        backoff = _BACKOFF_BASE_S
        while True:
            probing = not self._event_buffer and self._need_resync
            if not self._event_buffer and not probing:
                if self._stopping:
                    return
                self._buffer_event.clear()
                await self._buffer_event.wait()
                continue
            batch = self._event_buffer[:_PUSH_BATCH]
            # Поколение буфера: если, пока пачка в полёте, буфер переполнился и был очищен, удалять по длине
            # пачки уже нечего — там лежат НОВЫЕ, ещё не отправленные события (находка 7 критика 1a).
            gen = self._buffer_gen
            try:
                await self._client.push_events(batch)
            except Exception as exc:  # noqa: BLE001 — маршрутизируем по виду ошибки ниже
                if _is_unauthorized(exc):
                    self._trigger_exit(_AUTH_EXIT_CODE)
                    return
                if _is_core_unreachable(exc):
                    backoff = await self._apply_backoff(backoff)
                    continue
                # 4xx кроме 401 (core отверг саму пачку) или ошибка до отправки (кодирование): повтор той
                # же пачки даст то же самое — толкатель встал бы навсегда. Пачку выбрасываем, дыру закроет
                # сверка.
                if _is_rejected(exc):
                    _logger.error(
                        "connector: core отверг пачку (HTTP %s, %d событий) — выброшена, будет sync",
                        exc.response.status_code,
                        len(batch),
                    )
                else:
                    _logger.exception("connector: push_events() упал (%d событий), выброшено", len(batch))
                if gen == self._buffer_gen:
                    del self._event_buffer[: len(batch)]
                    self._mark_gone(len(batch))
                self._lost_count += 1
                self._need_resync = True
                continue
            else:
                if batch and gen == self._buffer_gen:
                    del self._event_buffer[: len(batch)]
                    self._mark_gone(len(batch))
                backoff = _BACKOFF_BASE_S
                if self._need_resync:
                    self._need_resync = False
                    self._request_sync("overflow")

    # --- планировщик sync() ----------------------------------------------------------------------------

    def _request_sync(self, reason: str) -> None:
        self._sync_queue.put_nowait(reason)
        self._sync_wake.set()

    def _sync_interval_jittered(self) -> float:
        return _SYNC_INTERVAL_S + random.uniform(-_SYNC_JITTER_S, _SYNC_JITTER_S)  # noqa: S311

    async def _run_one_sync(self, reason: str) -> None:
        async with self._sync_lock:
            covered = {reason} | self._pending_reasons
            if self._sync_redo_reason in covered:
                # Этот проход и есть заказанный повтор (или вобрал его причину): старая задача повтора не
                # нужна — упадёт проход, новый повтор закажется ниже со следующей паузой.
                self._cancel_sync_redo()
            try:
                await asyncio.wait_for(self.sync(reason), timeout=_SYNC_TIMEOUT_S)
            except AuthRequired as exc:
                # Потеря входа в источник посреди сверки — то же правило, что в пульсе (раздел D).
                await self._on_auth_required(str(exc))
            except asyncio.CancelledError:
                # Находка F2 критика куска 1c: чужая отмена (Telethon `disconnect()` изнутри `sync()`,
                # например из `on_resume()`) НЕ должна убивать планировщик сверки насмерть — настоящую
                # отмену (SIGTERM/остановку) пропускаем как есть.
                if _own_task_cancelling():
                    raise
                _logger.warning("connector: sync(%s) поймал чужую отмену (не свою задачу)", reason)
            except Exception as exc:  # noqa: BLE001 — сверка не должна ронять планировщик
                if _is_unauthorized(exc):
                    self._trigger_exit(_AUTH_EXIT_CODE)
                    return
                _logger.exception("connector: sync(%s) упал", reason)
                if isinstance(exc, _SYNC_TRANSIENT_ERRORS):
                    self._schedule_sync_redo(reason)
            else:
                self._sync_redo_count = 0

    def _cancel_sync_redo(self) -> None:
        task = self._sync_redo_task
        if task is not None and not task.done():
            task.cancel()
        self._sync_redo_task = None
        self._sync_redo_reason = None

    def _schedule_sync_redo(self, reason: str) -> None:
        """Сверка `reason` упала временной ошибкой — заказать повтор ТОЙ ЖЕ причины (Slack, Mattermost и
        Telegram различают полный и узкий проход по причине) через `_SYNC_REDO_DELAYS_S`. Одна задача за раз;
        не при сне (после него `resume` — полная сверка) и не при остановке; не больше 5 повторов подряд."""
        if self._stopping or self._sleeping:
            return
        if self._sync_redo_task is not None and not self._sync_redo_task.done():
            return
        if self._sync_redo_count >= len(_SYNC_REDO_DELAYS_S):
            _logger.warning(
                "connector: sync(%s) повторы исчерпаны (%d подряд) — до плановой сверки",
                reason,
                self._sync_redo_count,
            )
            return
        delay = _SYNC_REDO_DELAYS_S[self._sync_redo_count]
        self._sync_redo_count += 1
        _logger.info(
            "connector: sync(%s) повтор через %d с (попытка %d)", reason, int(delay), self._sync_redo_count
        )
        self._sync_redo_reason = reason
        self._sync_redo_task = asyncio.ensure_future(self._sync_redo(reason, delay))

    async def _sync_redo(self, reason: str, delay: float) -> None:
        await self._sleep(delay)
        # Очередь не пуста или сверка идёт — свой повтор не ставить, а дождаться: проход, вобравший эту
        # причину, сам снимет задачу (`_run_one_sync`); чужой узкий проход (`join`, `folders`) её не
        # закрывает.
        while not self._stopping and not self._sleeping:
            if self._sync_queue.empty() and not self._sync_lock.locked():
                self._request_sync(reason)
                return
            await self._sleep(_SYNC_REDO_POLL_S)

    async def _wait_awake(self) -> None:
        """Ф70-1 C.1: ждать снятия сна (или остановки процесса)."""
        awake_task = asyncio.ensure_future(self._awake.wait())
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        _, pending = await asyncio.wait({awake_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _sync_scheduler_loop(self) -> None:
        self._request_sync("start")
        while not self._stopping:
            if self._sleeping:
                # Ф70-1 C.1: при сне сверки нет — ни плановой, ни заказанной; причины копятся в очереди и
                # сливаются с `resume` после пробуждения.
                await self._wait_awake()
                continue
            deadline_s = self._sync_interval_jittered()
            # Ждать есть смысл, только если очередь ПУСТА прямо сейчас: если в ней уже что-то лежит (как
            # "start" на первом же обороте — `_request_sync` его туда положил ДО входа в цикл), `clear()`
            # ниже потёр бы флаг уже готового к разбору события, и обе задачи (сон + пробуждение) ждали бы
            # неопределённо долго нового сигнала — тихий deadlock, если `_sleep` не запланирован на самозавод
            # (например, у настоящего детектора сна `_sleep` действительно не возвращается, пока часы не
            # сдвинутся). Между проверкой `empty()` и `clear()` ниже нет ни одного `await` — переключиться
            # на другую задачу и положить что-то в очередь между ними Python не может (кооп. планировщик).
            if self._sync_queue.empty():
                self._sync_wake.clear()
                wake_task = asyncio.ensure_future(self._sync_wake.wait())
                sleep_task = asyncio.ensure_future(self._sleep(deadline_s))
                done, pending = await asyncio.wait(
                    {wake_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
                )
                del done  # не нужен — причина решается только по содержимому очереди ниже
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                if self._stopping:
                    return
                if self._sleeping:
                    continue  # Ф70-1: уснули, пока ждали, — причины остаются в очереди до пробуждения

            # Находка F14 (round 2): слить ВСЁ, что успело накопиться в очереди, в ОДНУ сверку — без этого
            # серия быстрых апдейтов (например, 11 подряд `UpdateDialogFilter` — владелец добавляет чаты в папку
            # по одному) запускала бы столько же ПОЛНЫХ сверок подряд, хотя актуальна только последняя.
            # `get_nowait()` (не `get()` наперегонки со сном) — не отменяется на середине, элемент не
            # теряется.
            reasons: set[str] = set()
            while True:
                try:
                    reasons.add(self._sync_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not reasons:
                reasons = {"scheduled"}
            main_reason = _pick_main_sync_reason(reasons)
            self._pending_reasons = frozenset(reasons - {main_reason})
            await self._run_one_sync(main_reason)

    # --- детектор сна ------------------------------------------------------------------------------------

    async def _sleep_detector_loop(self) -> None:
        last_wall = self._wall()
        last_mono = self._mono()
        while not self._stopping:
            await self._sleep(_SLEEP_TICK_S)
            if self._stopping:
                return
            wall, mono = self._wall(), self._mono()
            wall_delta, mono_delta = wall - last_wall, mono - last_mono
            drift = wall_delta - mono_delta
            if drift < -_SLEEP_THRESHOLD_S:
                # Настенные часы ушли назад (ручная правка, большой шаг NTP): соединение с источником цело,
                # переподключаться незачем — только сверка. Мелкие шаги NTP ниже порога не трогаем вовсе.
                self._request_sync("clock")
            elif drift > _SLEEP_THRESHOLD_S and self._sleeping:
                # Ф70-1: аккаунт спит вместе с днём проекта — соединения приёма нет, рвать нечего; сверка и
                # `on_resume` — после снятия сна (`start_receiving` поднимет приём заново).
                self._resume_pending = True
                self._request_sync("resume")
                self._heartbeat_now.set()
            elif drift > _SLEEP_THRESHOLD_S:
                # Переподключение после сна может само повиснуть (сеть ещё не поднялась) — детектор при
                # этом не должен замереть навсегда; сверка после пробуждения запрашивается в любом случае.
                try:
                    await asyncio.wait_for(self.on_resume(), timeout=_SEND_TIMEOUT_S)
                except AuthRequired as exc:
                    await self._on_auth_required(str(exc))
                    return
                except Exception:  # noqa: BLE001
                    _logger.exception("connector: on_resume() упал")
                self._request_sync("resume")
                self._heartbeat_now.set()
            last_wall, last_mono = wall, mono

    # --- пульс ---------------------------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            try:
                # Ф70-1 C.1: при сне в источник не ходим — пульс просто `ok` (сторож не видит молчания).
                if not self._sleeping:
                    await asyncio.wait_for(self.health(), timeout=_SEND_TIMEOUT_S)
            except AuthRequired as exc:
                await self._on_auth_required(str(exc))
                return
            except Exception as exc:  # noqa: BLE001 — пульс не должен ронять процесс
                if _is_unauthorized(exc):
                    self._trigger_exit(_AUTH_EXIT_CODE)
                    return
                # Обычная сетевая ошибка (прокси/сеть лежат) — раз в минуту, пока сети нет, ПОЛНАЯ
                # трассировка в журнал каждую минуту — чистый шум (находка 7 приёмки куска 1b); одна строка
                # без стека достаточно для диагностики. `AuthRequired`/401 — отдельными ветками выше, там
                # трассировка не нужна (код 78, процесс и так завершается).
                _logger.warning("connector: health() не прошёл: %s", type(exc).__name__)
            else:
                if not self.auth_required:
                    with contextlib.suppress(Exception):
                        await self._client.set_status("ok")
            if self._stopping:
                return
            # Пауза до следующего пульса, но детектор сна будит раньше: после пробуждения пульс должен уйти
            # сразу, а не через 60 с monotonic — иначе hub-watch почти каждое пробуждение кричал «коннектор
            # молчит» (находка 4б критика 1a).
            self._heartbeat_now.clear()
            sleep_task = asyncio.ensure_future(self._sleep(_HEALTH_INTERVAL_S))
            wake_task = asyncio.ensure_future(self._heartbeat_now.wait())
            _, pending = await asyncio.wait({sleep_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    # --- сон вместе с днём проекта (Ф70-1) ---------------------------------------------------------------

    async def _apply_sleep(self, info: dict | None) -> None:
        """Ф70-1 C.1: принять ответ ядра о сне. Переход — один раз на смену; ошибки хуков — в журнал (приём
        после неудачного `start_receiving` поднимет следующая смена или перезапуск процесса)."""
        want = info is not None
        changed = want != self._sleep_known
        self._sleep_known = want
        if not self.sleep_enabled:
            if want and changed:
                _logger.info(
                    "connector: сон: день проекта %s закрыт — сон не поддерживается", info["project_id"]
                )
            return
        if want == self._sleeping:
            return
        if want:
            self._sleeping = True
            self._awake.clear()
            _logger.info("connector: сон: день проекта %s закрыт", info["project_id"])
            try:
                await self.stop_receiving()
            except Exception:  # noqa: BLE001 — сон всё равно наступил: сверки и пульса в источник нет
                _logger.exception("connector: stop_receiving() упал")
            return
        try:
            await self.start_receiving()
        except AuthRequired as exc:
            await self._on_auth_required(str(exc))
            return
        except Exception:  # noqa: BLE001 — сверка `resume` всё равно пойдёт; источник поднимет её сбой
            _logger.exception("connector: start_receiving() упал")
        if self._resume_pending:
            self._resume_pending = False
            try:
                await asyncio.wait_for(self.on_resume(), timeout=_SEND_TIMEOUT_S)
            except AuthRequired as exc:
                await self._on_auth_required(str(exc))
                return
            except Exception:  # noqa: BLE001 — как в детекторе сна: сверка `resume` пойдёт всё равно
                _logger.exception("connector: on_resume() упал")
        self._sleeping = False
        self._awake.set()
        _logger.info("connector: сон снят, догон")
        self._request_sync("resume")
        self._heartbeat_now.set()

    async def _check_sleep_once(self) -> None:
        """Ф70-1 C.1: прочитать сон при старте (до задач `run()`). Ядро недоступно — решит цикл ожидания."""
        wait = getattr(self._client, "wait_sleep", None)
        if wait is None:
            return
        try:
            info = await wait(asleep=False, timeout_s=0)
        except Exception as exc:  # noqa: BLE001
            if _is_unauthorized(exc):
                self._trigger_exit(_AUTH_EXIT_CODE)
            return
        await self._apply_sleep(info)

    async def _sleep_watch_loop(self) -> None:
        """Ф70-1 A.4/C.1: long-poll `GET /sleep/wait` — ядро отвечает, как только сон сменился (не позже пары
        секунд после «Закончить/Начать день», правила раскладки или переноса беседы). Двойник клиента без
        `wait_sleep` (тесты каркаса) — цикла нет."""
        wait = getattr(self._client, "wait_sleep", None)
        if wait is None:
            return
        backoff = _BACKOFF_BASE_S
        while not self._stopping:
            try:
                info = await wait(asleep=self._sleep_known, timeout_s=_WAIT_SLEEP_S)
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                continue
            except Exception as exc:  # noqa: BLE001
                if _is_unauthorized(exc):
                    self._trigger_exit(_AUTH_EXIT_CODE)
                    return
                backoff = await self._apply_backoff(backoff)
                continue
            backoff = _BACKOFF_BASE_S
            if self._stopping:
                return
            await self._apply_sleep(info)

    # --- цикл отправки ---------------------------------------------------------------------------------

    async def _outbox_loop(self) -> None:
        backoff = _BACKOFF_BASE_S
        while not self._stopping:
            if self.auth_required:
                await self._sleep(_HEALTH_INTERVAL_S)
                continue
            try:
                ready = await self._client.wait_outbox(_WAIT_OUTBOX_S)
            except asyncio.CancelledError:
                # Находка F2 критика куска 1c: см. докстринг `_own_task_cancelling()` — цикл отправки не
                # должен умирать ни от чего, кроме настоящей отмены СВОЕЙ задачи.
                if _own_task_cancelling():
                    raise
                _logger.warning("connector: wait_outbox() поймал чужую отмену (не свою задачу) — повтор")
                continue
            except Exception as exc:  # noqa: BLE001
                if _is_unauthorized(exc):
                    self._trigger_exit(_AUTH_EXIT_CODE)
                    return
                backoff = await self._apply_backoff(backoff)
                continue
            backoff = _BACKOFF_BASE_S
            if self._stopping:
                return
            if not ready:
                continue
            try:
                await self.step_outbox()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("connector: step_outbox() поймал чужую отмену (не свою задачу) — продолжаем")
            except Exception as exc:  # noqa: BLE001 — step_outbox() сам гасит send()/reconcile(), это про lease/report
                if _is_unauthorized(exc):
                    self._trigger_exit(_AUTH_EXIT_CODE)
                    return
                _logger.exception("connector: step_outbox() упал целиком")

    async def _on_auth_required(self, reason: str | None = None) -> None:
        """Источник отказал во входе — откуда бы это ни пришло (старт, пульс, сверка, отправка, пробуждение):
        `auth_required` в core и выход 78 (юнит его не перезапускает — нужен `hub auth`).

        Ф51-1 A2: `reason` — текст исключения `AuthRequired` (вид отказа, без логина/адреса/пароля): в журнал
        и в `last_error` аккаунта. Задачи-работники (`on_auth_required()` без причины) — как раньше."""
        self.auth_required = True
        # причина первого отказа не затирается поздним вызовом без неё (работники зовут без причины)
        reason = self._auth_reason = _auth_reason(reason) or getattr(self, "_auth_reason", None)
        _logger.warning("connector: источник отказал во входе: %s", reason or "причина не названа")
        with contextlib.suppress(Exception):
            await self._client.set_status("auth_required", last_error=reason)
        self._trigger_exit(_AUTH_EXIT_CODE)

    def _trigger_exit(self, code: int) -> None:
        self._exit_code = code
        self._stopping = True
        self._stop_event.set()
        self._buffer_event.set()

    async def step_outbox(self) -> int:
        """Один шаг цикла отправки. Возвращает число обработанных лизов (0, если `auth_required` уже
        установлен — тогда лизы не берутся вовсе, раздел «Клиент коннектора» спеки)."""
        if self.auth_required:
            return 0

        leases = await self._client.lease(lease_ms=self.lease_ms)
        self._stop_batch = False
        self._stop_mode = None
        processed = 0
        for index, lease in enumerate(leases):
            if self.auth_required:
                break
            result = await self._process_lease(lease)
            processed += 1
            if (result is not None and result.outcome == "flood") or self._stop_batch:
                # Источник велел ждать (FLOOD_WAIT) или коннектор остановил отправку (`stop_batch()`, 1c:
                # PeerFlood -> limited). Остальные лизы пачки НЕ отправляем (находка 8 критика 1a):
                # send-лизы возвращаем как `flood` — не ошибка и не расход попыток; reconcile-лизы (они
                # ничего не шлют, `flood` для них ядро не примет) просто истекут и выдадутся снова.
                retry_ms = (result.retry_after_ms if result is not None else None) or _STOP_BATCH_RETRY_MS
                # Ф69-1 П5: после PeerFlood остаток — `temporary` + `postponed` (попытка не тратится,
                # аккаунт не встаёт: `flood` поставил бы `flood_until` всему аккаунту); FLOOD_WAIT — как было.
                peer_flood = self._stop_mode == STOP_PEER_FLOOD and not (
                    result is not None and result.outcome == "flood"
                )
                for rest in leases[index + 1 :]:
                    if rest["mode"] == "reconcile":
                        continue
                    with contextlib.suppress(Exception):
                        if peer_flood:
                            await self._client.report(
                                outbox_id=rest["outbox_id"],
                                lease_gen=rest["lease_gen"],
                                outcome="temporary",
                                retry_after_ms=_STOP_BATCH_RETRY_MS,
                                postponed=True,
                            )
                        else:
                            await self._client.report(
                                outbox_id=rest["outbox_id"],
                                lease_gen=rest["lease_gen"],
                                outcome="flood",
                                retry_after_ms=retry_ms,
                            )
                break
        return processed

    @contextlib.asynccontextmanager
    async def lease_files(self, lease: dict) -> AsyncIterator[list[Path]]:
        """Ф11-3b B1: файлы задания outbox (`lease["files"]`) — байты ВСЕХ файлов по очереди из ядра
        (`idx = 0..N-1`) во временные файлы коннектора (`outbox_tmp_dir`, 0600); отдаёт список путей в порядке
        `files`. При выходе из блока удаляются все при ЛЮБОМ исходе (успех, исключение, отмена по таймауту
        каркаса). Любой сбой получения байтов (любого из файлов) -> `OutboxFileError`: отправки не было,
        коннектор отвечает `temporary`."""
        files = lease_files_meta(lease)
        if not files:
            raise OutboxFileError("no_file")
        tmp_dir = self.outbox_tmp_dir if self.outbox_tmp_dir is not None else Path(tempfile.gettempdir())
        paths: list[Path] = []
        try:
            try:
                tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                for idx, file in enumerate(files):
                    size = file.get("size")
                    expected = size if isinstance(size, int) and not isinstance(size, bool) else None
                    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix="outbox-", suffix=".part")
                    os.close(fd)
                    path = Path(name)
                    paths.append(path)
                    await self._client.fetch_outbox_file(
                        lease["outbox_id"], lease["lease_gen"], idx, path, expected_size=expected
                    )
            except OutboxFileError:
                raise
            except Exception as exc:
                # Сеть до ядра, 401/5xx, диск — отправки не было. 401 всплывёт снова на `report` (выход 78).
                raise OutboxFileError(type(exc).__name__) from exc
            yield list(paths)
        finally:
            for path in paths:
                with contextlib.suppress(OSError):
                    path.unlink()

    def stop_batch(self, mode: str | None = None) -> None:
        """Для подкласса: не отправлять остаток текущей пачки лизов (иная причина «стоп»). Ф69-1 П5: `mode=
        STOP_PEER_FLOOD` — остаток пачки уходит в очередь `temporary` + `postponed` (без паузы аккаунта)."""
        self._stop_batch = True
        self._stop_mode = mode

    async def mark_limited(self, reason: str) -> None:
        """Источник наложил спам-ограничение (Telegram PeerFlood): `limited` в core (снимает только
        `hub account resume`) и остаток пачки не отправляется. Единственный правильный путь для подкласса —
        прямой `set_status('limited')` пачку не останавливал бы (находка 8 критика 1a)."""
        self.stop_batch()
        await self._client.set_status("limited", last_error=reason)

    async def _process_lease(self, lease: dict) -> SendResult | None:
        """Отправить/сверить один лиз и отчитаться. `None` — отчёта не было (потерян вход ИЛИ `abandon` у
        идемпотентного коннектора — кусок 1c: лиз истекает сам, ядро выдаёт строку снова)."""
        mode = lease["mode"]
        # Ф10-4 П3 / Ф11-3b B1: отправка с файлами (скачать из ядра + залить в источник) — длинный таймаут.
        timeout_s = FILE_SEND_TIMEOUT_S if mode == "send" and lease_files_meta(lease) else _SEND_TIMEOUT_S
        try:
            coro = self.reconcile(lease) if mode == "reconcile" else self.send(lease)
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        except AuthRequired as exc:
            # Строка остаётся в `sending`: её подхватит ветка истёкшего лиза после починки входа (5b).
            await self._on_auth_required(str(exc))
            return None
        except TimeoutError:
            # Таймаут каркаса: исход неизвестен. У идемпотентного источника повтор безопасен (Telegram: тот же
            # random_id + сначала поиск в истории) — `abandon`, лиз истечёт сам; `unknown` отдал бы решение
            # человеку без нужды (приёмка 1c). Неидемпотентный — `unknown`, как раньше.
            _logger.warning("ConnectorBase: send()/reconcile() не уложился в %s с", timeout_s)
            result = SendResult("abandon" if self.idempotent and mode == "send" else "unknown")
        except asyncio.CancelledError:
            # Находка F2 критика куска 1c: Telethon (`gateway.disconnect()`, в т.ч. из `on_resume()`)
            # отменяет СВОИ pending-future при разрыве соединения — это всплывает как `CancelledError`
            # прямо из середины `send()`/`reconcile()`, хотя НАШУ задачу никто не просил остановиться.
            # Раньше это убивало `_outbox_loop()` насмерть: процесс жил, строка висела в `sending` вечно.
            # Настоящую отмену (SIGTERM/остановка) пропускаем как есть — её обязан увидеть `run()`.
            if _own_task_cancelling():
                raise
            _logger.warning("connector: send()/reconcile() поймал чужую отмену (не свою задачу)")
            result = SendResult("abandon" if self.idempotent and mode == "send" else "unknown")
        except Exception:  # noqa: BLE001 — исключение send()/reconcile() без результата -> unknown, не temporary
            _logger.exception("ConnectorBase: send()/reconcile() упал без результата")
            result = SendResult("unknown")

        if result.outcome == "abandon":
            if self.idempotent:
                # Раздел «Решения архитектора» куска 1c: report НЕ вызывается вовсе — лиз истекает сам,
                # ядро выдаёт строку снова в режиме `send` (идемпотентный источник — `idempotent_send`),
                # и следующая попытка сама разбирается (для Telegram — сначала поиском в истории).
                return result
            _logger.error(
                "connector: 'abandon' от неидемпотентного коннектора (idempotent=False) — считаем unknown"
            )
            result = SendResult("unknown")

        # Ф12-1 B1: поля привязки новой беседы — только если коннектор их дал (подменные клиенты тестов и
        # отчёты обычных бесед — прежней формы).
        peer = {
            key: value
            for key, value in (("peer_ext_id", result.peer_ext_id), ("peer_title", result.peer_title))
            if value is not None
        }
        # Ф11-3b B1: альбом — только при `sent` и только если коннектор дал.
        if result.outcome == "sent":
            if result.peer_bot and result.peer_ext_id is not None:
                peer["peer_bot"] = True  # Ф63-2 П2
            if result.ext_ids is not None:
                peer["ext_ids"] = list(result.ext_ids)
            if result.grouped_id is not None:
                peer["grouped_id"] = result.grouped_id
            if result.ts_ms is not None:  # Ф40-1 A: время источника — только при `sent`
                peer["ts_ms"] = result.ts_ms
        if result.outcome == "temporary" and result.postponed and result.retry_after_ms is not None:
            peer["postponed"] = True
        try:
            await self._client.report(
                outbox_id=lease["outbox_id"],
                lease_gen=lease["lease_gen"],
                outcome=result.outcome,
                ext_id=result.ext_id,
                error=result.error,
                retry_after_ms=result.retry_after_ms,
                **peer,
            )
        except Exception as exc:
            # Поправки после opus-критика кода 2c+2d (`docs/architecture.md`), п. 11 (С5): core
            # отказал в отчёте ПО ЭТОМУ лизу (409 StaleLease — строку уже закрыло склеенное эхо, прочие 4xx) —
            # строка в журнал, остальные лизы пачки обрабатываются дальше (раньше исключение роняло
            # `step_outbox`, лизы пачки сиротели и через срок лиза уходили в reconcile -> ложный unknown у
            # неотправленного). 401 (секрет отозван) и недоступность core — как раньше, наверх.
            if not _is_rejected(exc):
                raise
            _logger.warning(
                "connector: core отверг отчёт по лизу outbox_id=%s (HTTP %s) — дальше по пачке",
                lease["outbox_id"],
                exc.response.status_code,
            )
        return result

    # --- внешний цикл ----------------------------------------------------------------------------------

    async def run(self, *, source: str, account_id: int) -> int:
        """Точка входа `hub connector ACCOUNT_ID` (раздел D). Возвращает код выхода процесса: 0 — обычная
        остановка (SIGTERM/SIGINT, буфер дослан), 78 — секрет отозван (401) или потеряна авторизация
        источника (`AuthRequired` из `health()`/цикла отправки/толкателя буфера)."""
        loop = asyncio.get_running_loop()
        install_task_dump(loop, name=f"connector:{source}:{account_id}")

        def _on_signal() -> None:
            self._stopping = True
            self._stop_event.set()
            self._buffer_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _on_signal)

        # Старт источника гонится с сигналом: зависшее подключение (прокси лежит) не должно пережить SIGTERM
        # до SIGKILL systemd. Потеря входа при старте — код 78, как везде (юнит его не перезапускает).
        start_task = asyncio.ensure_future(self.start_source())
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        stop_task.cancel()
        if not start_task.done():
            start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await start_task
            with contextlib.suppress(Exception):
                await self.stop_source()
            with contextlib.suppress(Exception):
                await self._client.aclose()
            return self._exit_code
        try:
            start_task.result()
        except AuthRequired as exc:
            await self._on_auth_required(str(exc))
            with contextlib.suppress(Exception):
                await self._client.aclose()
            return self._exit_code

        # Ф70-1 C.1: сон — до задач: спящий аккаунт сразу закрывает приём и не гоняет сверку `start`.
        await self._check_sleep_once()

        pusher_task = asyncio.ensure_future(self._pusher_loop())
        other_tasks = [
            asyncio.ensure_future(self._sleep_detector_loop()),
            asyncio.ensure_future(self._heartbeat_loop()),
            asyncio.ensure_future(self._sync_scheduler_loop()),
            asyncio.ensure_future(self._sleep_watch_loop()),
        ]
        if self.sends_enabled:
            other_tasks.append(asyncio.ensure_future(self._outbox_loop()))
        if self.read_ack_enabled:
            # Ф8-7 B3: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_read_acker().run()))
        if self.reactions_enabled:
            # Ф10-3 B: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_reactor().run()))
        if self.msg_ops_enabled:
            # Ф14-8 B: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_msg_op_worker().run()))
        if self.join_enabled:
            # Ф17-1 C: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_joiner().run()))
        if self.leave_enabled:
            # Ф17-2 C: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_leaver().run()))
        if self.group_ops_enabled:
            # Ф62-6 D: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_group_op_worker().run()))
        if self.press_enabled:
            # Ф44-1c: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_presser().run()))
        if self.lookup_enabled:
            # Ф29-1 C1: своя задача — останавливается вместе с остальными (SIGTERM/выход 78).
            other_tasks.append(asyncio.ensure_future(self._make_lookuper().run()))

        try:
            await self._stop_event.wait()
        finally:
            self._stopping = True
            self._buffer_event.set()

            if self._sync_redo_task is not None:
                other_tasks.append(self._sync_redo_task)
            for t in other_tasks:
                t.cancel()
            for t in other_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

            # Дослать буфер (до 5 с) — только если останавливаемся штатно (SIGTERM/SIGINT), не после 401/
            # AuthRequired: секрет отозван или источник не авторизован, слать уже некуда/незачем.
            if self._exit_code == 0:
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(pusher_task, timeout=_FLUSH_ON_STOP_S)
            if not pusher_task.done():
                pusher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pusher_task

            # Зависший disconnect источника не должен держать процесс до SIGKILL systemd.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.stop_source(), timeout=_STOP_SOURCE_TIMEOUT_S)
            with contextlib.suppress(Exception):
                await self._client.aclose()

        return self._exit_code
