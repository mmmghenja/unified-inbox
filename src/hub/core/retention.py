"""Ретенция work-БД: тексты старше 90 дней стираются ФИЗИЧЕСКИ. План (раздел 7, 11.7, 11.8), спека
`docs/architecture.md` Только схема `work` — `main` эта функция не трогает вообще.

Цель — через 90 дней текста Globex НЕТ в файле `hub-work.db` (и в его `-wal`), а не только в выдаче API:
`_connect_writer` (db.py) держит `PRAGMA work.secure_delete = ON`, здесь дополнительно — FTS `optimize`,
`incremental_vacuum` (с `.fetchall()` — без него освобождается ровно одна страница) и
`wal_checkpoint(TRUNCATE)`.
"""

import json
import logging
import re
import uuid

from .audit import write_audit
from .db import next_seq

_logger = logging.getLogger(__name__)

# Ф8-4 A1/П9 (план 11.13, владелец 24.09: «вообще ничего не нужно удалять»): ретенция СНЯТА. Периодический прогон
# (`runner._retention_loop`) не запускается, очистка при приёме (`ingest.apply_batch`) не применяется — для
# work тоже; `dead_letter`, `outbox`, `agent_item` по сроку не чистятся. Включение обратно — только правкой
# этой константы (настройки нет намеренно). Код ниже остаётся: `clean_message_content` нужна удалению у себя
# в хабе (`hiding.py`), тесты ретенции включают её через `monkeypatch`.
RETENTION_ENABLED = False

RETENTION_DAYS = 90
_DAY_MS = 24 * 60 * 60 * 1000
_RETENTION_MS = RETENTION_DAYS * _DAY_MS
_BATCH = 2000
_INCREMENTAL_VACUUM_PAGES = 2000
_LAST_RUN_META_KEY = "retention_last_run_ts"
# Поправки после opus-критика кода (R3/R4): опорное время ретенции — отметка времени ИСТОЧНИКОВ, не часы
# хаба и не `channel.last_msg_ts` (тот растёт и от чисто внутренних событий — `resolve_unknown`, — раздел
# ниже, находка R4). main.meta ключ, единый на main и work (Telegram держит его свежим, даже когда Slack
# молчит).
_SOURCE_TIME_HWM_META_KEY = "source_time_hwm"
# Поправки (R1/R6): флаг «физическая уборка work ещё не доведена до конца» — ставят и очистка при приёме
# (ingest.clean_message_content), и периодический прогон, снимает ТОЛЬКО успешный `wal_checkpoint(TRUNCATE)`
# (busy=0). Без него: очистка при приёме, произошедшая МЕЖДУ прогонами (строка сразу подходит под порог), не
# даёт периодическому прогону поводов трогать FTS/vacuum/checkpoint — токен остаётся в файле навсегда.
_DIRTY_META_KEY = "retention_dirty"

# Ключ задачи (тот же формат, что `connectors/refs.py`), БЕЗ `\b` — элементы `refs_json` уже целые токены,
# сравниваем строку целиком (`fullmatch`), не ищем подстроку.
_TASK_KEY_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d{1,6}")


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


# --- Опорное время (Поправки после opus-критика кода, R3/R4) ---------------------------------------------


def get_source_time_hwm(conn) -> int | None:
    """Максимум `ts` сообщений, реально ПРИНЯТЫХ ingest'ом из ЛЮБОГО аккаунта (main и work) — `None`, если
    ни одного сообщения ещё не было."""
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (_SOURCE_TIME_HWM_META_KEY,)).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def bump_source_time_hwm(conn, ts: int, now_ms: int) -> None:
    """Поднять отметку времени источников до `ts`, только если она растёт, и не выше `now_ms + 1 день`
    (раздел «Поправки», R3: кривой `ts` источника — например, наврал коннектор — не уводит опорное время
    ретенции вперёд). Зовётся ТОЛЬКО из `ingest.py` для upsert/edit-событий с настоящим `ts` источника —
    заготовки outbox, `resolve_unknown`, `report` её не двигают (у них нет "времени источника", только часы
    хаба на момент операции)."""
    capped = min(ts, now_ms + _DAY_MS)
    current = get_source_time_hwm(conn)
    if current is not None and capped <= current:
        return
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_SOURCE_TIME_HWM_META_KEY, str(capped)),
    )


def reference_ts(conn, schema: str, now_ms: int) -> int:
    """Опорное время ретенции: `min(now, отметка_времени_источников + 1 день)`. Привязка к самим ДАННЫМ (а
    не к настенным часам хаба и не к `channel.last_msg_ts`, растущему и от чисто внутренних операций вроде
    `resolve_unknown` — находка R4) защищает от скачка часов вперёд (тест: +1 год -> ничего не очищено,
    раздел F.3) и от `resolve_unknown(decision='sent')` при кривых часах (находка R3: тест `test_ret4`).
    Отметки ещё нет (аккаунт вообще не получал сообщений) -> `now_ms` (нечего защищать)."""
    del schema  # раньше отметка бралась ПОФАЙЛОВО (channel.last_msg_ts); теперь она общая, main и work
    hwm = get_source_time_hwm(conn)
    if hwm is None:
        return now_ms
    return min(now_ms, hwm + _DAY_MS)


def cutoff_ts(reference_ts_value: int) -> int:
    return reference_ts_value - _RETENTION_MS


def ingest_cutoff_ts(conn, schema: str, now_ms: int) -> int:
    """Cutoff для очистки ПРИ ПРИЁМЕ (раздел F.1) — та же формула, что периодический прогон."""
    return cutoff_ts(reference_ts(conn, schema, now_ms))


# --- Функция очистки содержимого (ОДНА — раздел F.2) ------------------------------------------------------


def _clean_attachments_json(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None  # dict или иная форма -> NULL (раздел F.2)
    cleaned = [{"type": item["type"]} for item in value if isinstance(item, dict) and item.get("type")]
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None


def _clean_refs_json(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    cleaned = [v for v in value if isinstance(v, str) and _TASK_KEY_RE.fullmatch(v)]
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None


def clean_message_content(content: dict) -> None:
    """Функция очистки (раздел F.2), мутирует словарь колонок сообщения (`ingest.py`) НА МЕСТЕ: `text` = NULL,
    `entities_json` = NULL, `forward_json` = NULL, `attachments_json` -> только `[{"type": …}]`
    (пусто -> NULL), `refs_json` -> только ключи задач (пусто -> NULL). `reactions_json`, отправитель, ts —
    метаданные, не входят в этот словарь/не трогаются."""
    content["attachments_json"] = _clean_attachments_json(content.get("attachments_json"))
    content["refs_json"] = _clean_refs_json(content.get("refs_json"))
    content["text"] = None
    content["entities_json"] = None
    content["forward_json"] = None
    # Ф11-8: адресаты своего письма — ПДн, стираются вместе с содержимым.
    content["mail_to_json"] = None


# --- Периодический прогон (раздел F.3-F.4) ----------------------------------------------------------------


def get_last_run_ts(conn) -> int | None:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (_LAST_RUN_META_KEY,)).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _set_last_run_ts(conn, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_LAST_RUN_META_KEY, str(now_ms)),
    )


# --- Флаг «грязно» (Поправки после opus-критика кода, R1/R6) ------------------------------------------------


def is_dirty(conn) -> bool:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (_DIRTY_META_KEY,)).fetchone()
    return row is not None and row[0] == "1"


def mark_dirty(conn) -> None:
    """Что-то в work физически ещё не убрано (текст обнулён в строке, но не в байтах файла) — ставит и
    очистка ПРИ ПРИЁМЕ (`ingest.py`, когда `clean_message_content` реально сработала), и периодический
    прогон (когда он сам что-то почистил). Снимает ТОЛЬКО успешный физический шаг (`_clear_dirty`)."""
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, '1') ON CONFLICT (key) DO UPDATE SET value = '1'",
        (_DIRTY_META_KEY,),
    )


def _clear_dirty(conn) -> None:
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, '0') ON CONFLICT (key) DO UPDATE SET value = '0'",
        (_DIRTY_META_KEY,),
    )


def _purge_message_portion(conn, *, after_id: int, cutoff: int) -> tuple[int, int, bool]:
    """Одна порция (≤ `_BATCH` строк) `work.message`, курсором по `id` (не пересканом `purged=0 AND
    ts<cutoff` — раздел F.3): продвигаемся по `id` независимо от того, сколько строк реально подошло под
    условие, поэтому полный проход всей таблицы делает ровно `ceil(N/_BATCH)` порций, а не больше при большой
    доле уже очищенных строк. Возвращает `(число_очищенных, последний_просмотренный_id, есть_ли_ещё)`."""
    rows = conn.execute(
        "SELECT id, purged, ts, attachments_json, refs_json FROM work.message "
        "WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, _BATCH),
    ).fetchall()
    if not rows:
        return 0, after_id, False

    cleaned = 0
    for mid, purged, ts, attachments_json, refs_json in rows:
        if purged or ts >= cutoff:
            continue
        new_attachments = _clean_attachments_json(attachments_json)
        new_refs = _clean_refs_json(refs_json)
        conn.execute(
            "UPDATE work.message SET text = NULL, entities_json = NULL, forward_json = NULL, "
            "mail_to_json = NULL, attachments_json = ?, refs_json = ?, "
            # Ф44-1a П5: кнопки бота — чужой текст и сырой callback, стираются вместе с содержимым.
            "buttons_json = NULL, buttons_ts = NULL, purged = 1 WHERE id = ?",
            (new_attachments, new_refs, mid),
        )
        # Ф14-6 B: расшифровка голосового — такой же текст сообщения, при очистке уходит вместе с ним.
        from . import transcripts

        transcripts.drop_for_message(conn, "work", mid)
        # Ф15-1 A1: входящие реакции письма (адреса отправителей) и пометки реакций сессий.
        from . import mail_reactions

        mail_reactions.drop_for_message(conn, "work", mid)
        cleaned += 1

    if cleaned:
        # Поправки (R1/R6): в ТОЙ ЖЕ транзакции, что сама очистка — переживает крах между порцией и
        # физическим шагом (следующий прогон увидит флаг, даже если счётчик "нечего чистить").
        mark_dirty(conn)

    last_id = rows[-1][0]
    return cleaned, last_id, len(rows) == _BATCH


def _purge_outbox_portion(conn, *, after_id: int, cutoff: int) -> tuple[int, int, bool]:
    """`outbox` старше `cutoff`, `text <> ''` -> `text=''`. Раздел F.3, Поправки п.6 (R7): заготовка в
    `pending_approval` — решение человека по строке без текста бессмысленно, поэтому она сразу переводится в
    `cancelled` (error='retention'), а не остаётся висеть с пустым текстом; прочие статусы (конечные,
    `unknown`) — текст стирается, статус не меняется, как раньше."""
    rows = conn.execute(
        "SELECT id, created_ts, text, status, draft_text FROM work.outbox WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, _BATCH),
    ).fetchall()
    if not rows:
        return 0, after_id, False

    cleaned = 0
    for oid, created_ts, text, status, draft_text in rows:
        # Ф19-1 C + П8: исходный текст правленого черновика стирается вместе с текстом — строка с пустым
        # текстом (файлы с пустой подписью), но с `draft_text`, не пропускается.
        if created_ts >= cutoff or (text == "" and draft_text is None):
            continue
        if status == "pending_approval":
            conn.execute(
                "UPDATE work.outbox SET text = '', draft_text = NULL, status = 'cancelled', "
                "error = 'retention' WHERE id = ?",
                (oid,),
            )
            # Заготовка в ленте несёт `outbox_status` производным JOIN'ом (queries.py) — без нового `seq`
            # уже подключённый `/sync` не узнает о смене статуса до следующего изменения самого сообщения.
            placeholder = conn.execute("SELECT id FROM work.message WHERE outbox_id = ?", (oid,)).fetchone()
            if placeholder is not None:
                conn.execute(
                    "UPDATE work.message SET seq = ? WHERE id = ?", (next_seq(conn), placeholder[0])
                )
        else:
            conn.execute("UPDATE work.outbox SET text = '', draft_text = NULL WHERE id = ?", (oid,))
        cleaned += 1

    if cleaned:
        mark_dirty(conn)

    last_id = rows[-1][0]
    return cleaned, last_id, len(rows) == _BATCH


def _purge_agent_item_portion(conn, *, after_id: int, cutoff: int) -> tuple[int, int, bool]:
    """Спека 7b, A4/П4: карточки дежурного в work с `created_ts` старше `cutoff` — `summary`, `action` ->
    NULL, `purged=1`; открытая (`new`) или просмотренная (`seen`) карточка ещё и закрывается (`dismissed`):
    пустая открытая карточка не висит, а новый разбор беседы создаёт новую строку, а не пишет в стёртую. По
    `created_ts`, а не `updated_ts`: пересказ не должен пережить самые ранние свои сообщения. Курсором по
    `id`, как `_purge_message_portion`. Карточки main не трогаются."""
    rows = conn.execute(
        "SELECT id, purged, created_ts, status FROM work.agent_item WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, _BATCH),
    ).fetchall()
    if not rows:
        return 0, after_id, False

    cleaned = 0
    for item_id, purged, created_ts, status in rows:
        if purged or created_ts >= cutoff:
            continue
        new_status = "dismissed" if status in ("new", "seen") else status
        conn.execute(
            "UPDATE work.agent_item SET summary = NULL, action = NULL, purged = 1, status = ? WHERE id = ?",
            (new_status, item_id),
        )
        cleaned += 1

    if cleaned:
        mark_dirty(conn)

    last_id = rows[-1][0]
    return cleaned, last_id, len(rows) == _BATCH


def _purge_digest_portion(conn, *, after_id: int, cutoff: int) -> tuple[int, int, bool]:
    """Ф86-1 (C1.1): сводки дня в work, обновлённые раньше `cutoff`, — тем же сроком, что прочие тексты work
    (строка удаляется: сводка — пересказ ленты, без неё она не нужна). Курсором по `id`, как
    `_purge_agent_item_portion`. Сводки main не трогаются. Ретенция выключена (`RETENTION_ENABLED`)."""
    rows = conn.execute(
        "SELECT id, updated_ts FROM work.digest WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, _BATCH),
    ).fetchall()
    if not rows:
        return 0, after_id, False
    old = [row[0] for row in rows if row[1] < cutoff]
    for digest_id in old:
        conn.execute("DELETE FROM work.digest WHERE id = ?", (digest_id,))
    if old:
        mark_dirty(conn)
    return len(old), rows[-1][0], len(rows) == _BATCH


def _purge_dead_letter_portion(conn, *, cutoff: int) -> int:
    """`dead_letter` старше `cutoff` — удалить. Порциями (id из подзапроса), пока есть что удалять."""
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM work.dead_letter WHERE id IN ("
            "  SELECT id FROM work.dead_letter WHERE ts < ? ORDER BY id LIMIT ?"
            ")",
            (cutoff, _BATCH),
        )
        removed = cur.rowcount
        if removed <= 0:
            return total
        total += removed


def _fts_optimize(conn) -> None:
    """Раздел F.4. Поправки п.7 (R8): ОТДЕЛЬНЫЙ элемент очереди писателя — без `optimize` `incremental_vacuum`
    и `checkpoint` не трогают освободившиеся страницы старых сегментов FTS."""
    conn.execute("INSERT INTO work.message_fts(message_fts) VALUES ('optimize')")


def _incremental_vacuum_step(conn) -> bool:
    """Одна порция `incremental_vacuum` (≤ `_INCREMENTAL_VACUUM_PAGES` страниц) — ОТДЕЛЬНЫЙ элемент очереди
    писателя (раздел F.4, Поправки п.7/R8): на крупной базе полная уборка — много порций, каждая держит
    писатель не дольше своей порции, а не всё время vacuum разом (замер — на синтетической базе 200к
    сообщений). `.fetchall()` — БЕЗ него `incremental_vacuum` как обычный execute() освобождает РОВНО ОДНУ
    страницу (проверено критиком, не документировано). Возвращает True, если было что освобождать —
    вызывающий крутит цикл, пока не False."""
    if conn.execute("PRAGMA work.freelist_count").fetchone()[0] == 0:
        return False
    pragma = f"PRAGMA work.incremental_vacuum({_INCREMENTAL_VACUUM_PAGES})"  # noqa: S608 — константа, не ввод
    conn.execute(pragma).fetchall()
    return True


def _wal_checkpoint_truncate(conn) -> bool:
    """Отдельный элемент очереди писателя (раздел F.4, Поправки п.7/R8). Возвращает True, ТОЛЬКО если
    checkpoint прошёл ДО КОНЦА (`busy=0`) — Поправки п.1 (R1/R9): чекпойнт, заблокированный читателем со
    старым снимком (долгий запрос, бэкап), не переносит все страницы из WAL в основной файл — флаг «грязно»
    снимать нельзя, следующий прогон обязан повторить попытку. `secure_delete` включается один раз для
    writer-соединения в `db.py::_connect_writer`, не здесь."""
    busy, _log_frames, _checkpointed = conn.execute("PRAGMA work.wal_checkpoint(TRUNCATE)").fetchone()
    return busy == 0


def run_once(db, now_ms: int, stop_event=None) -> dict | None:
    """Один полный прогон ретенции work-БД (чистая функция — для тестов и планировщика ядра, раздел F.3).
    Портируется в потоке-писателе `db`, отдельными транзакциями по ≤ `_BATCH` строк на таблицу (не держит
    писатель одной гигантской транзакцией). Возвращает счётчики `{messages, outbox, agent_items, digests,
    dead_letter}`, если реально что-то сделано, иначе `None` (нечего чистить и физически убирать нечего).

    `stop_event` (Поправки п.8/P1) — `threading.Event`, проверяется МЕЖДУ порциями (таблицы message/outbox,
    порции `incremental_vacuum`): планировщик ядра (`runner._retention_loop`) просит остановиться при
    завершении процесса, не дожидаясь конца всего прогона на крупной базе."""
    with db.reader() as conn:
        last_run_ts = get_last_run_ts(conn)
    if last_run_ts is not None and now_ms < last_run_ts:
        # Поправки п.5 (R5): раньше здесь был безусловный пропуск прогона («пришли пораньше — уйдём ни с
        # чем»). После разового скачка часов вперёд (защита которого — опорное время источников, п.3/R3, а
        # НЕ этот теперь ужé лишний барьер) и последующего исправления часов назад ретенция простаивала бы
        # ГОД — до тех пор, пока настенные часы честно не догонят ошибочную отметку прошлого прогона. Прогон
        # теперь считается назревшим в любом случае — в журнал уходит одна строка, не больше.
        _logger.warning(
            "retention: настенные часы (%s) позади прошлого прогона (%s) — прогон всё равно проведён "
            "(защиту от скачка часов держит опорное время источников)",
            now_ms,
            last_run_ts,
        )

    with db.reader() as conn:
        cutoff = cutoff_ts(reference_ts(conn, "work", now_ms))

    counts = {"messages": 0, "outbox": 0, "agent_items": 0, "digests": 0, "dead_letter": 0}

    after_id = 0
    while stop_event is None or not stop_event.is_set():
        cleaned, after_id, has_more = db.write_sync(
            lambda conn, after_id=after_id: _purge_message_portion(conn, after_id=after_id, cutoff=cutoff)
        )
        counts["messages"] += cleaned
        if not has_more:
            break

    after_id = 0
    while stop_event is None or not stop_event.is_set():
        cleaned, after_id, has_more = db.write_sync(
            lambda conn, after_id=after_id: _purge_outbox_portion(conn, after_id=after_id, cutoff=cutoff)
        )
        counts["outbox"] += cleaned
        if not has_more:
            break

    after_id = 0
    while stop_event is None or not stop_event.is_set():
        cleaned, after_id, has_more = db.write_sync(
            lambda conn, after_id=after_id: _purge_agent_item_portion(conn, after_id=after_id, cutoff=cutoff)
        )
        counts["agent_items"] += cleaned
        if not has_more:
            break

    after_id = 0
    while stop_event is None or not stop_event.is_set():
        cleaned, after_id, has_more = db.write_sync(
            lambda conn, after_id=after_id: _purge_digest_portion(conn, after_id=after_id, cutoff=cutoff)
        )
        counts["digests"] += cleaned
        if not has_more:
            break

    if stop_event is None or not stop_event.is_set():
        counts["dead_letter"] = db.write_sync(lambda conn: _purge_dead_letter_portion(conn, cutoff=cutoff))

    any_done = any(counts.values())
    with db.reader() as conn:
        was_dirty = is_dirty(conn)

    if any_done or was_dirty:
        # Поправки п.1 (R1/R6): физический шаг делается, если САМ что-то почистил ИЛИ флаг «грязно» уже
        # стоял (очистка при приёме между прогонами, либо предыдущий физический шаг не довёл дело до конца).
        db.maintenance_sync(_fts_optimize)
        for _ in range(10_000):
            if stop_event is not None and stop_event.is_set():
                break
            if not db.maintenance_sync(_incremental_vacuum_step):
                break
        checkpoint_ok = db.maintenance_sync(_wal_checkpoint_truncate)
        if checkpoint_ok:
            db.write_sync(lambda conn: _clear_dirty(conn))
        # checkpoint_ok=False -> флаг остаётся стоять (он уже был выставлен `_purge_*_portion`/предыдущим
        # прогоном) — следующий прогон обязательно повторит физический шаг, даже если сам ничего не почистит.

    def _finish(conn):
        _set_last_run_ts(conn, now_ms)
        if any_done:
            write_audit(
                conn,
                now_ms=now_ms,
                actor="retention",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="retention_purge",
                result="ok",
                detail=counts,
            )

    db.write_sync(_finish)
    return counts if any_done else None
