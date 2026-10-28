"""Расшифровка голосовых: строки `transcript`, очередь службы `hub transcribe`. Спецификация:
docs/architecture.md (14-6a, разделы A–C и «Поправки после plan-critic» П3–П13 — они главнее
текста выше них).

Устройство — как очередь медиа: ядро службе не звонит; служба ждёт и берёт задание лизом (`lease`), файл
читает обычной `GET /media/{id}`, результат шлёт `report`. Тяжёлых моделей в ядре нет.

- Постановка — в той же транзакции, где строка `media` становится `ready` (`media.commit_upload`, заготовка
  своей отправки в `outbox`): `on_media_ready`. Автоматически — только за автозагрузкой (П9: `priority = 0`).
- Ручная — `request` (кнопка UI): любой возраст, аудиофайл тоже.
- Каждая смена статуса — новый `seq` сообщения (`media.bump_message_seq`): `/sync` и курсоры видят.
- Сообщение — всегда через `media.message_id` на момент смены (П3: своей колонки нет).

Все функции, принимающие `conn`, зовутся ВНУТРИ `Db.write()`, кроме помеченных «читатель». Журнал — только
`media_id` и класс ошибки: ни текста расшифровки, ни имён.
"""

import json
import logging
import sqlite3
import time

from . import media as media_mod
from .db import db_of

_logger = logging.getLogger(__name__)

# П8: пороги — константы ядра (`eligible` работает здесь, не в службе).
AUTO_MAX_S = 600  # автоматически — не длиннее 10 мин
MANUAL_MAX_S = 3600  # по кнопке — не длиннее часа
TRANSCRIBE_WAIT_S = 180  # потребители ждут расшифровку не дольше (14-6b), считая от готовности медиа
AUTO_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000  # догон истории не ставит старые голосовые

LEASE_MIN_S = 120  # П10: срок лиза — max(120 с, 3 × длительность)
LEASE_FACTOR = 3
MAX_ATTEMPTS = 3
RETRY_MS = (60 * 1000, 5 * 60 * 1000)  # пауза перед повтором после 1-й и 2-й неудачи
MAX_TEXT = 100_000
MAX_ERROR = 300
MAX_SHORT = 100  # problem, model, device
LANGS = frozenset({"ru", "en"})
OUTCOMES = frozenset({"done", "failed", "skip"})
STATUSES = ("queued", "working", "done", "failed")
TOKEN_NAME = "transcribe:worker"  # noqa: S105 — имя токена службы в ядре, не секрет
STALE_QUEUE_MS = 30 * 60 * 1000  # hub-watch: старейшая выдаваемая строка старше — служба стоит
TEXT_CUT_MARK = "…[обрезано: расшифровка длиннее {limit} символов]"
SKIP_TOO_LONG_AUTO = "длиннее 10 мин — по кнопке"
SKIP_TOO_LONG_MANUAL = "длиннее 60 мин"


class TranscriptError(Exception):
    pass


class NotFound(TranscriptError):
    pass


class StaleLease(TranscriptError):
    pass


class Busy(TranscriptError):
    """`working` — расшифровка идёт, повтор по кнопке нельзя (409 `transcribe_busy`)."""


class NotReady(TranscriptError):
    """Вложение не `ready` (409 `media_not_ready`): сначала «Загрузить»."""


class NotTranscribable(TranscriptError):
    """Вид вложения не голосовой (решение 6) или длиннее часа (422)."""


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


def now_ms() -> int:
    """Часы для `transcript_wait_ms` выдачи: запросы чтения времени параметром не получают, тесты подменяют
    эту функцию."""
    return int(time.time() * 1000)


# --- жива ли служба (14-6b, пункт архитектора 1) ----------------------------------------------------------
# Метка последнего опроса `/transcripts/lease` — только в памяти процесса ядра (как `SubWaiters`): после
# рестарта ядра служба считается «не бравшей», пока не придёт. Не брала дольше `WORKER_SEEN_S` — никто не ждёт
# расшифровку (до `hub transcribe setup` или при остановленной службе голосовое не держит разборщик, сигналы и
# `hub_wait`). Часы — монотонные: часы хаба (`app.state.clock`) в тестах подменены, а метке нужна только
# длительность.
WORKER_SEEN_S = 60.0
_worker_seen: float | None = None


def _mono() -> float:
    return time.monotonic()


def mark_worker_seen() -> None:
    """Служба взяла `/transcripts/lease`: начало каждого долгого опроса (не реже 25 с) и выданное задание."""
    global _worker_seen
    _worker_seen = _mono()


def worker_alive() -> bool:
    return _worker_seen is not None and _mono() - _worker_seen <= WORKER_SEEN_S


def worker_left_ms() -> int:
    """Сколько ещё служба считается живой без нового опроса (0 — уже нет): ожидание не дольше этого."""
    if _worker_seen is None:
        return 0
    return max(0, int((WORKER_SEEN_S - (_mono() - _worker_seen)) * 1000))


def reset_worker_seen() -> None:
    """Для тестов: «служба не приходила» (как после рестарта ядра)."""
    global _worker_seen, _fast_seen
    _worker_seen = None
    _fast_seen = None


# Ф79-1 (docs/architecture.md): голосовые — быстрому работнику (ноутбук, cuda)
# первым. Пока он опрашивал `lease` с `fast=True` не дольше `FAST_SEEN_S` назад, медленный (`fast=False`,
# cpu стационарника) получает только задания, ждущие в очереди дольше `SLOW_AFTER_MS` (быстрый занят длинной
# очередью или завис). Метка — только в памяти процесса ядра, как `_worker_seen`.
FAST_SEEN_S = 60.0
SLOW_AFTER_MS = 180 * 1000
_fast_seen: float | None = None


def mark_fast_seen() -> None:
    global _fast_seen
    _fast_seen = _mono()


def fast_alive() -> bool:
    return _fast_seen is not None and _mono() - _fast_seen <= FAST_SEEN_S


def queued_before_for(*, fast: bool, now_ms: int) -> int | None:
    """Граница `queued_ts` для опроса: быстрому и медленному без живого быстрого — без границы (`None`)."""
    if fast or not fast_alive():
        return None
    return now_ms - SLOW_AFTER_MS


# --- кто расшифровывается (решения 5–6, П8, П9) ------------------------------------------------------------


def attachment_item(attachments_json: str | None, idx: int) -> dict:
    """Вложение сообщения `idx` из `attachments_json` (флаги коннектора `round`/`clip` лежат в нём, в строку
    `media` не переносятся — раздел D) или `{}`."""
    try:
        items = json.loads(attachments_json) if attachments_json else None
    except ValueError:
        return {}
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not 0 <= idx < len(items) or not isinstance(items[idx], dict):
        return {}
    return items[idx]


def voice_like(kind: str | None, item: dict) -> bool:
    """Решение 5: голосовое, видео-кружок Telegram (`round`), аудиоклип Slack (`clip`)."""
    if kind == "voice":
        return True
    if kind == "video" and item.get("round") is True:
        return True
    return item.get("clip") is True


def kind_ok(kind: str | None, item: dict, *, manual: bool) -> bool:
    """Решение 6: по кнопке ещё и обычный аудиофайл. Обычное видео — никогда."""
    if voice_like(kind, item):
        return True
    return manual and kind == "audio"


def eligible(media_row: dict, message_row: dict, *, manual: bool, now_ms: int) -> bool:
    """Решения 5–6 + П8 + П9. `media_row`: `kind`, `status`, `duration_s`, `priority`, `idx`;
    `message_row`: `ts`, `deleted`, `hidden`, `purged`, `attachments_json`. Неизвестная длительность — можно
    (служба посчитает её по wav, П8)."""
    if media_row.get("status") != "ready":
        return False
    if message_row.get("deleted") or message_row.get("hidden") or message_row.get("purged"):
        return False
    item = attachment_item(message_row.get("attachments_json"), media_row.get("idx") or 0)
    if not kind_ok(media_row.get("kind"), item, manual=manual):
        return False
    duration = media_row.get("duration_s")
    if duration is not None and duration > (MANUAL_MAX_S if manual else AUTO_MAX_S):
        return False
    if manual:
        return True
    if media_row.get("priority"):
        return False  # П9: загружено по клику «Загрузить» — расшифровка тоже по кнопке
    ts = message_row.get("ts")
    return isinstance(ts, int) and now_ms - ts <= AUTO_MAX_AGE_MS


_ROW_SQL = (
    "SELECT md.id, md.message_id, md.idx, md.kind, md.status, md.duration_s, md.priority, md.mime, "
    "md.detected, COALESCE(md.size, md.stored_size), md.fetched_ts, md.created_ts, "
    "m.ts, m.deleted, m.hidden, m.purged, m.attachments_json, "
    "t.status, t.manual, t.lease_gen, t.attempts "
    "FROM {schema}.media md JOIN {schema}.message m ON m.id = md.message_id "
    "LEFT JOIN {schema}.transcript t ON t.media_id = md.id "
)
_ROW_KEYS = (
    "id",
    "message_id",
    "idx",
    "kind",
    "status",
    "duration_s",
    "priority",
    "mime",
    "detected",
    "size",
    "fetched_ts",
    "created_ts",
    "ts",
    "deleted",
    "hidden",
    "purged",
    "attachments_json",
    "t_status",
    "t_manual",
    "t_lease_gen",
    "t_attempts",
)


def _load(conn: sqlite3.Connection, media_id: int) -> tuple[str, dict] | None:
    schema = db_of(media_id)
    row = conn.execute(_sql(schema, _ROW_SQL + "WHERE md.id = ?"), (media_id,)).fetchone()
    if row is None:
        return None
    return schema, dict(zip(_ROW_KEYS, row, strict=True))


def message_of(conn: sqlite3.Connection, media_id: int) -> int | None:
    """Читатель: сообщение вложения (для проверки видимости до `request`)."""
    row = conn.execute(
        _sql(db_of(media_id), "SELECT message_id FROM {schema}.media WHERE id = ?"), (media_id,)
    ).fetchone()
    return row[0] if row is not None else None


# --- постановка ----------------------------------------------------------------------------------------


def on_media_ready(conn: sqlite3.Connection, media_id: int, *, now_ms: int) -> bool:
    """Раздел B: строка `media` только что стала `ready` (в этой же транзакции). Кандидат автоматики и строки
    ещё нет — `queued`. `seq` сообщения уже сдвинут сменой статуса медиа. Возвращает, поставлена ли."""
    loaded = _load(conn, media_id)
    if loaded is None:
        return False
    schema, row = loaded
    if row["t_status"] is not None or not eligible(row, row, manual=False, now_ms=now_ms):
        return False
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.transcript (media_id, status, manual, created_ts, queued_ts) "
            "VALUES (?, 'queued', 0, ?, ?)",
        ),
        (media_id, now_ms, now_ms),
    )
    return True


def request(conn: sqlite3.Connection, media_id: int, *, now_ms: int) -> dict:
    """Кнопка «Расшифровать» / «Расшифровать заново» (решение 6). Видимость сообщения проверяет вызывающий.
    Была `done`/`failed` — снова `queued` (`manual=1`, `attempts=0`; прежний текст остаётся до нового),
    `working` — `Busy`. Возвращает `transcript` вложения для выдачи."""
    loaded = _load(conn, media_id)
    if loaded is None:
        raise NotFound("media")
    schema, row = loaded
    if row["status"] != "ready":
        raise NotReady("media")
    if not eligible(row, row, manual=True, now_ms=now_ms):
        raise NotTranscribable("media")
    if row["t_status"] == "working":
        raise Busy("transcript")
    if row["t_status"] is None:
        conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.transcript (media_id, status, manual, created_ts, queued_ts) "
                "VALUES (?, 'queued', 1, ?, ?)",
            ),
            (media_id, now_ms, now_ms),
        )
    else:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.transcript SET status = 'queued', manual = 1, attempts = 0, error = NULL, "
                "not_before_ts = NULL, lease_expires_ts = NULL, queued_ts = ? WHERE media_id = ?",
            ),
            (now_ms, media_id),
        )
    media_mod.bump_message_seq(conn, schema, row["message_id"])
    return for_messages(conn, schema, [row["message_id"]])[media_id]


def drop(conn: sqlite3.Connection, schema: str, media_id: int) -> bool:
    """П4: файл в той же строке `media` заменён — расшифровка прежнего файла прочь."""
    return (
        conn.execute(_sql(schema, "DELETE FROM {schema}.transcript WHERE media_id = ?"), (media_id,)).rowcount
        > 0
    )


def drop_for_message(conn: sqlite3.Connection, schema: str, message_id: int) -> None:
    """Уборка вместе с сообщением (`purged` ретенцией — строки `media` остаются): текст не остаётся в БД."""
    conn.execute(
        _sql(
            schema,
            "DELETE FROM {schema}.transcript WHERE media_id IN "
            "(SELECT id FROM {schema}.media WHERE message_id = ?)",
        ),
        (message_id,),
    )


# --- очередь: lease / extend / report -------------------------------------------------------------------

# П10: лиз берёт только строку, чей файл на месте (`ready`) и чьё сообщение не скрыто и не удалено.
_LEASABLE = (
    "t.status = 'queued' AND (t.not_before_ts IS NULL OR t.not_before_ts <= ?) AND md.status = 'ready' "
    "AND m.hidden = 0 AND m.deleted = 0 AND m.purged = 0"
)
_FROM_QUEUE = (
    "FROM {schema}.transcript t JOIN {schema}.media md ON md.id = t.media_id "
    "JOIN {schema}.message m ON m.id = md.message_id "
)


def lease_ms(duration_s: int | float | None) -> int:
    """П10: `max(120 с, 3 × длительность)`; длительность неизвестна — 120 с (служба продлит по wav, П8)."""
    seconds = LEASE_MIN_S
    if isinstance(duration_s, int | float) and not isinstance(duration_s, bool) and duration_s > 0:
        seconds = max(LEASE_MIN_S, LEASE_FACTOR * duration_s)
    return int(seconds * 1000)


def _expire(conn: sqlite3.Connection, schema: str, now_ms: int) -> None:
    """П10: просроченный `working` — попытка засчитана, дальше `queued` (сразу) или `failed` на третьей."""
    rows = conn.execute(
        _sql(
            schema,
            "SELECT t.media_id, t.attempts, md.message_id FROM {schema}.transcript t "
            "JOIN {schema}.media md ON md.id = t.media_id "
            "WHERE t.status = 'working' AND t.lease_expires_ts <= ?",
        ),
        (now_ms,),
    ).fetchall()
    for media_id, attempts, message_id in rows:
        attempts += 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.transcript SET status = ?, attempts = ?, error = 'lease expired', "
                "lease_expires_ts = NULL, not_before_ts = NULL WHERE media_id = ?",
            ),
            (status, attempts, media_id),
        )
        media_mod.bump_message_seq(conn, schema, message_id)
        _logger.info("transcript %s: лиз истёк (попытка %s)", media_id, attempts)


_AGED = " AND COALESCE(t.queued_ts, 0) <= ?"


def has_leasable(conn: sqlite3.Connection, *, now_ms: int, queued_before: int | None = None) -> bool:
    """Читатель. «Готово» для долгого опроса `lease`: выдалось бы задание или истёк чей-то лиз.
    `queued_before` (Ф79-1) — только задания, поставленные не позже (медленному работнику)."""
    aged = "" if queued_before is None else _AGED
    for schema in ("main", "work"):
        params: tuple = (now_ms,) + (() if queued_before is None else (queued_before,)) + (now_ms,)
        row = conn.execute(
            _sql(
                schema,
                "SELECT 1 " + _FROM_QUEUE + "WHERE (" + _LEASABLE + aged + ") "
                "OR (t.status = 'working' AND t.lease_expires_ts <= ?) LIMIT 1",
            ),
            params,
        ).fetchone()
        if row is not None:
            return True
    return False


def lease(conn: sqlite3.Connection, *, now_ms: int, queued_before: int | None = None) -> dict | None:
    """Одно задание: ручные первыми, затем старейшая в очереди; оба файла БД. `None` — нечего.
    `queued_before` (Ф79-1) — только задания, поставленные не позже (медленному работнику)."""
    best = None
    aged = "" if queued_before is None else _AGED
    for schema in ("main", "work"):
        _expire(conn, schema, now_ms)
        row = conn.execute(
            _sql(
                schema,
                "SELECT t.media_id, t.manual, t.queued_ts " + _FROM_QUEUE + "WHERE " + _LEASABLE + aged + " "
                "ORDER BY t.manual DESC, t.queued_ts, t.media_id LIMIT 1",
            ),
            (now_ms,) if queued_before is None else (now_ms, queued_before),
        ).fetchone()
        if row is None:
            continue
        key = (-row[1], row[2] if row[2] is not None else 0)
        if best is None or key < best[0]:
            best = (key, row[0])
    if best is None:
        return None
    media_id = best[1]
    schema, row = _load(conn, media_id)  # type: ignore[misc] — строка только что найдена
    lease_gen = conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.transcript SET status = 'working', lease_gen = lease_gen + 1, "
            "lease_expires_ts = ? WHERE media_id = ? RETURNING lease_gen",
        ),
        (now_ms + lease_ms(row["duration_s"]), media_id),
    ).fetchone()[0]
    media_mod.bump_message_seq(conn, schema, row["message_id"])
    manual = bool(row["t_manual"])
    return {
        "media_id": media_id,
        "message_id": row["message_id"],
        "lease_gen": lease_gen,
        "kind": row["kind"],
        "mime": row["mime"],
        "detected": row["detected"],
        "duration_s": row["duration_s"],
        "size": row["size"],
        "manual": manual,
        # П8: потолок длительности для службы — она меряет wav и отвечает `skip`, если длиннее.
        "max_s": MANUAL_MAX_S if manual else AUTO_MAX_S,
    }


def _leased(conn: sqlite3.Connection, media_id: int, lease_gen: int) -> tuple[str, dict]:
    loaded = _load(conn, media_id)
    if loaded is None or loaded[1]["t_status"] is None:
        raise NotFound("transcript")
    schema, row = loaded
    if row["t_status"] != "working" or row["t_lease_gen"] != lease_gen:
        raise StaleLease("transcript")
    return schema, row


def extend(conn: sqlite3.Connection, *, media_id: int, lease_gen: int, duration_s: float, now_ms: int) -> int:
    """П8: служба узнала настоящую длительность из wav — срок лиза от неё. Возвращает новый срок."""
    schema, _row = _leased(conn, media_id, lease_gen)
    expires = now_ms + lease_ms(duration_s)
    conn.execute(
        _sql(schema, "UPDATE {schema}.transcript SET lease_expires_ts = ? WHERE media_id = ?"),
        (expires, media_id),
    )
    return expires


def _clip(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def clip_text(text: str) -> str:
    """Потолок текста (раздел C): длиннее 100 000 символов — обрезать с пометкой."""
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + TEXT_CUT_MARK.format(limit=MAX_TEXT)


def report(
    conn: sqlite3.Connection,
    *,
    media_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    text: str | None = None,
    lang: str | None = None,
    lang_prob: float | None = None,
    problem: str | None = None,
    model: str | None = None,
    device: str | None = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
) -> str:
    """Раздел B: `done` — текст, язык, вероятность, `problem`; `failed` — попытка засчитана, до третьей снова
    в очередь с паузой; `skip` (нечего распознавать, длиннее предела, файла нет) — `failed` сразу. Чужой или
    устаревший `lease_gen` — `StaleLease`. Возвращает новый статус."""
    if outcome not in OUTCOMES:
        raise ValueError("unknown outcome")
    if lang is not None and lang not in LANGS:
        raise ValueError("lang")
    schema, row = _leased(conn, media_id, lease_gen)
    error = _clip(error, MAX_ERROR)
    if outcome == "done":
        status = "done"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.transcript SET status = 'done', text = ?, lang = ?, lang_prob = ?, "
                "problem = ?, model = ?, device = ?, elapsed_ms = ?, error = NULL, done_ts = ?, "
                "lease_expires_ts = NULL, not_before_ts = NULL WHERE media_id = ?",
            ),
            (
                clip_text(text if isinstance(text, str) else ""),
                lang,
                lang_prob if isinstance(lang_prob, int | float) and not isinstance(lang_prob, bool) else None,
                _clip(problem, MAX_SHORT),
                _clip(model, MAX_SHORT),
                _clip(device, MAX_SHORT),
                elapsed_ms if isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool) else None,
                now_ms,
                media_id,
            ),
        )
    elif outcome == "skip":
        status = "failed"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.transcript SET status = 'failed', error = ?, lease_expires_ts = NULL, "
                "not_before_ts = NULL WHERE media_id = ?",
            ),
            (error or "skip", media_id),
        )
    else:
        attempts = (row["t_attempts"] or 0) + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        pause = RETRY_MS[min(attempts, len(RETRY_MS)) - 1]
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.transcript SET status = ?, attempts = ?, error = ?, "
                "lease_expires_ts = NULL, not_before_ts = ? WHERE media_id = ?",
            ),
            (status, attempts, error, now_ms + pause if status == "queued" else None, media_id),
        )
    media_mod.bump_message_seq(conn, schema, row["message_id"])
    if outcome == "done":
        # Ф31-1 решение 1: текст расшифровки — повод перепроверить ключи подписок (голосовое без текста при
        # приёме ключ не находило). П11: `chan_subs` импортирует этот модуль — здесь лениво.
        from . import chan_subs

        chan_subs.recheck_keywords(conn, row["message_id"], now_ms=now_ms)
    _logger.info("transcript %s: %s -> %s", media_id, outcome, status)
    return status


# --- выдача ---------------------------------------------------------------------------------------------


def for_messages(conn: sqlite3.Connection, schema: str, message_ids: list[int]) -> dict[int, dict]:
    """Читатель. `{media_id: {status, text, lang, problem, manual}}` вложений этих сообщений (раздел C)."""
    result: dict[int, dict] = {}
    ids = sorted(set(message_ids))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            _sql(
                schema,
                "SELECT t.media_id, t.status, t.text, t.lang, t.problem, t.manual FROM {schema}.transcript t "
                "JOIN {schema}.media md ON md.id = t.media_id WHERE md.message_id IN ({ph})",
                ph=placeholders,
            ),
            chunk,
        ).fetchall()
        for media_id, status, text, lang, problem, manual in rows:
            result[media_id] = {
                "status": status,
                "text": text,
                "lang": lang,
                "problem": problem,
                "manual": bool(manual),
            }
    return result


def _working_lease_left_ms(conn: sqlite3.Connection, now_ms: int) -> int:
    """Читатель. Ревью Ф14-6: служба считает задание дольше `WORKER_SEEN_S` (длинная запись, первая загрузка
    модели, счёт на cpu) и очередь не опрашивает — пока её лиз `working` не истёк, она жива."""
    best = 0
    for schema in ("main", "work"):
        row = conn.execute(
            _sql(schema, "SELECT MAX(lease_expires_ts) FROM {schema}.transcript WHERE status = 'working'")
        ).fetchone()
        if row[0] is not None:
            best = max(best, row[0] - now_ms)
    return max(0, best)


def wait_ms_for_messages(
    conn: sqlite3.Connection, schema: str, message_ids: list[int], *, now_ms: int
) -> dict[int, int]:
    """Читатель. П6: сколько ещё ждать расшифровку сообщения (`> 0` — ждать). Ждёт, если у сообщения есть
    кандидат автоматики (решение 5), чей медиа ещё качается (`pending`/`fetching`, отсчёт — от `ts`
    сообщения), или строка `queued`/`working` (отсчёт — от готовности медиа); окно — `TRANSCRIBE_WAIT_S`.
    Служба не брала задания последние `WORKER_SEEN_S` — никто не ждёт: `{}` (14-6b); ожидание — не дольше,
    чем служба ещё считается живой (замолчала посреди ожидания — пересмотр через остаток, а не через окно)."""
    result: dict[int, int] = {}
    alive_ms = max(worker_left_ms(), _working_lease_left_ms(conn, now_ms))
    if alive_ms <= 0:
        return result
    window = TRANSCRIBE_WAIT_S * 1000
    ids = sorted(set(message_ids))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            _sql(
                schema,
                _ROW_SQL + "WHERE md.message_id IN ({ph}) AND (t.status IN ('queued', 'working') OR "
                "(t.media_id IS NULL AND md.status IN ('pending', 'fetching')))",
                ph=placeholders,
            ),
            chunk,
        ).fetchall()
        for raw in rows:
            row = dict(zip(_ROW_KEYS, raw, strict=True))
            if row["deleted"] or row["hidden"] or row["purged"]:
                continue
            if row["t_status"] is not None:
                since = row["fetched_ts"] if row["fetched_ts"] is not None else row["created_ts"]
            else:
                # Медиа ещё качается: ждём, только если это будет кандидат автоматики.
                candidate = {**row, "status": "ready"}
                if not eligible(candidate, row, manual=False, now_ms=now_ms):
                    continue
                since = row["ts"]
            left = window - (now_ms - since)
            if left > 0:
                result[row["message_id"]] = max(result.get(row["message_id"], 0), min(left, window, alive_ms))
    return result


def pending_for_messages(
    conn: sqlite3.Connection, schema: str, message_ids: list[int], *, now_ms: int
) -> set[int]:
    """Читатель. Сообщения, которые ещё ждут расшифровку (раздел B, для 14-6b)."""
    return set(wait_ms_for_messages(conn, schema, message_ids, now_ms=now_ms))


def signal_hold_ms(conn: sqlite3.Connection, channel_id: int, first_message_id: int, *, now_ms: int) -> int:
    """Читатель. 14-6b П1 + «Мелкое»: сколько ещё держать сигнал сообщений беседы (Ф13-1 `messages`, Ф14-5):
    сообщения сигнала — входящие беседы с `first_message_id` и новее (сигнал копит их, пока `pending`); ждёт
    хоть одно — держать, срок — самое долгое ожидание. `0` — не держать. Только чтение: удержание не пишет."""
    if not worker_alive():
        return 0
    schema = db_of(channel_id)
    ids = [
        row[0]
        for row in conn.execute(
            _sql(
                schema,
                "SELECT DISTINCT md.message_id FROM {schema}.media md "
                "JOIN {schema}.message m ON m.id = md.message_id "
                "WHERE m.channel_id = ? AND m.id >= ? AND m.is_me = 0",
            ),
            (channel_id, first_message_id),
        ).fetchall()
    ]
    if not ids:
        return 0
    return max(wait_ms_for_messages(conn, schema, ids, now_ms=now_ms).values(), default=0)


# --- сводка (hub-watch, `hub transcribe status`) ------------------------------------------------------------


def watch_summary(conn: sqlite3.Connection, now_ms: int) -> dict:
    """Читатель. Заведена ли служба (живой токен `transcribe:worker`), очередь, неудачи за сутки и возраст
    старейшей выдаваемой строки — только числа (текст ошибки службы — в её `state.json`)."""
    configured = (
        conn.execute(
            "SELECT 1 FROM main.api_token WHERE name = ? AND revoked = 0 LIMIT 1", (TOKEN_NAME,)
        ).fetchone()
        is not None
    )
    queued = working = failed_24h = 0
    oldest: int | None = None
    for schema in ("main", "work"):
        q, w = conn.execute(
            _sql(
                schema,
                "SELECT SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'working' THEN 1 ELSE 0 END) FROM {schema}.transcript",
            )
        ).fetchone()
        queued += q or 0
        working += w or 0
        failed_24h += conn.execute(
            _sql(
                schema,
                "SELECT COUNT(*) FROM {schema}.transcript WHERE status = 'failed' AND "
                "COALESCE(queued_ts, created_ts) >= ?",
            ),
            (now_ms - 24 * 60 * 60 * 1000,),
        ).fetchone()[0]
        row = conn.execute(
            _sql(schema, "SELECT MIN(t.queued_ts) " + _FROM_QUEUE + "WHERE " + _LEASABLE),
            (now_ms,),
        ).fetchone()
        if row[0] is not None and (oldest is None or row[0] < oldest):
            oldest = row[0]
    return {
        "configured": configured,
        "queued": queued,
        "working": working,
        "failed_24h": failed_24h,
        "oldest_queued_ts": oldest,
    }
