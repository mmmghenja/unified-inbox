"""Помощник на интервью — ядро (Ф60-1, docs/architecture.md —
они приоритетнее).

Интервью ведёт одна сессия хаба (`main.interview.session_id`); одновременно открыто (`active|paused`) не
больше одного на всё ядро — проверка в транзакции писателя. Строки расшифровки (`main.interview_line`)
приносит служба записи `hub-listen` кусками звука: ядро распознаёт их SpeechKit и пишет строку
(`add_line`). Сессии строки подаются обычной репликой (`held_work.say`) с пометкой `interview_id` (П1):
источник `ui`, приоритет ТОЛЬКО `next` (П3), текст — блок данных с nonce (2.4), части не длиннее `PART_MAX`
(П4).

Текст строк — содержимое разговора: в журнал ядра не пишется, в аудит — только числа. Звук не хранится.

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`. Отказы — `hub_sessions.SessionError` с
готовым русским текстом (интерфейс показывает его как есть). Когда подавать в режиме `live` (таймер 1,5 с,
зазор 8 с) и живость службы — память ядра, `core/interview_runtime.py`."""

import re
import secrets
import uuid
from datetime import datetime
from typing import NamedTuple

from . import held as held_mod
from . import held_work
from .audit import write_audit
from .hub_sessions import SessionError

MODES = ("live", "button")
LANGS = ("ru-RU", "en-US")
WHO = ("me", "them")
OPEN = ("active", "paused")
ME_LANG = "ru-RU"  # речь владельца (2.2)

LINES_CURRENT = 200  # `GET /interviews/current` — последние строки
LINES_PAGE_MAX = 500  # `GET /interviews/{id}/lines?after=` — строк за раз
LINE_TEXT_MAX = 8000  # строка расшифровки в базе (кусок до 15 с — сотни символов; предел — страховка)
PART_MAX = 15_000  # П4: текст одной реплики подачи с рамкой и служебной строкой (SAY_MAX — 16 000)
WARN_MAX = 200  # П12: строка службы записи

CAP_MS = 3 * 3600 * 1000  # П6: потолок интервью
SILENCE_MS = 20 * 60 * 1000  # П6: столько без новых строк в `active` — пауза
LIVE_QUIET_MS = 1500  # 2.4 + П5: после строки `them` ждать столько тишины
LIVE_GAP_MS = 8000  # П5: между подачами `live` — не меньше
LISTENER_FRESH_MS = 10_000  # 4: служба опрашивала `listen/current` не дольше — «жива»

REASON_RESTART = "ядро перезапущено"
REASON_SILENCE = "тишина 20 мин"
REASON_CAP = "потолок 3 часа"
REASON_CLOSED = "сессия закрыта"

BUSY = "идёт интервью — сначала «Закончить»"  # П8: передача дел и смена модели
NOT_FOUND = "not found"

SERVICE_LIVE = (
    "Интервью по ходу: дай подсказку владельцу в 1–3 строки — что ответить или спросить сейчас; не пересказывай."
)
SERVICE_HINT = "владелец просит подсказку: что ответить или спросить сейчас — в 1–3 строки; не пересказывай."
SERVICE_HINT_EMPTY = "владелец просит подсказку по последнему сказанному."
SERVICE_END = (
    "Звонок окончен. Напиши итог: о чём договорились, что пообещал владелец, следующие шаги; сохрани расшифровку "
    "и итог по правилам проекта."
)
SERVICE_EN = " Собеседник по-английски — сначала перевод его последней реплики одной строкой."
PART_HEAD = "Интервью #{id}: расшифровка звонка, часть {k} из {n}; что делать — в последней части."
WHO_LABEL = {"them": "Собеседник", "me": "владелец"}

_FEED_LINE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] (?:Собеседник|владелец): ", re.MULTILINE)

_COLS = (
    "id, session_id, project_id, mode, them_lang, status, started_ts, ended_ts, fed_line_id, hint_ts, "
    "reason, warn, active_ts, last_line_ts"
)
_LINE_COLS = "id, interview_id, who, start_ms, end_ms, text, created_ts"


class FeedResult(NamedTuple):
    say_ids: list[int]  # созданные реплики (части — по порядку)
    lines: int  # строк вошло в подачу
    queued: bool  # П3: «Подскажи» без новых строк при ждущей реплике — новой нет


NO_FEED = FeedResult([], 0, False)


def system_audit() -> dict:
    """Аудит действий самого ядра (таймер `live`, потолок, тишина, старт ядра)."""
    return {"actor": "core", "api_token_id": None, "client_name": None, "request_id": uuid.uuid4().hex}


# --- чтение -------------------------------------------------------------------------------------------------


def _row_dict(row) -> dict:
    return dict(zip(_COLS.split(", "), row, strict=True))


def _line_dict(row) -> dict:
    return dict(zip(_LINE_COLS.split(", "), row, strict=True))


def _get(conn, interview_id: int) -> dict | None:
    row = conn.execute(f"SELECT {_COLS} FROM main.interview WHERE id = ?", (interview_id,)).fetchone()  # noqa: S608
    return _row_dict(row) if row is not None else None


def _must(conn, interview_id: int) -> dict:
    row = _get(conn, interview_id)
    if row is None:
        raise SessionError(404, NOT_FOUND)
    return row


def current_row(conn) -> dict | None:
    """Открытое (`active|paused`) интервью или `None`."""
    row = conn.execute(
        f"SELECT {_COLS} FROM main.interview WHERE status IN ('active', 'paused') ORDER BY id DESC LIMIT 1"  # noqa: S608
    ).fetchone()
    return _row_dict(row) if row is not None else None


def active_row(conn) -> dict | None:
    row = current_row(conn)
    return row if row is not None and row["status"] == "active" else None


def open_for_session(conn, hub_session_id: int) -> dict | None:
    row = current_row(conn)
    return row if row is not None and row["session_id"] == hub_session_id else None


def check_free(conn, hub_session_id: int) -> None:
    """П8: у сессии открыто интервью — «Новая с передачей дел» и смена модели отвечают 409."""
    if open_for_session(conn, hub_session_id) is not None:
        raise SessionError(409, BUSY)


def _queued_says(conn, rec: held_mod.HubSession | None, interview_id: int) -> int:
    if rec is None or rec.store is None:
        return 0
    return conn.execute(
        held_work._sql(  # noqa: SLF001 — та же подстановка схемы
            rec.store,
            "SELECT COUNT(*) FROM {schema}.held_say WHERE session_id = ? AND status = 'queued' "
            "AND interview_id = ?",
        ),
        (rec.id, interview_id),
    ).fetchone()[0]


def interview_dict(conn, row: dict) -> dict:
    """Запись для интерфейса: поля таблицы (без служебных `active_ts`) плюс число строк, число неподанных
    реплик интервью (`queued_says`) и причина ожидания сессии (`wait_reason`, П5: «подача ждёт: …»)."""
    rec = held_mod.get(conn, row["session_id"])
    lines = conn.execute(
        "SELECT COUNT(*) FROM main.interview_line WHERE interview_id = ?", (row["id"],)
    ).fetchone()[0]
    out = {key: value for key, value in row.items() if key != "active_ts"}
    out.update(
        lines=lines,
        queued_says=_queued_says(conn, rec, row["id"]),
        wait_reason=rec.wait_reason if rec is not None and rec.closed_ts is None else None,
    )
    return out


def listen_dict(row: dict | None) -> dict | None:
    """`GET /listen/current` службе записи — только то, что ей нужно."""
    if row is None:
        return None
    return {"id": row["id"], "status": row["status"], "them_lang": row["them_lang"]}


def last_lines(conn, interview_id: int, limit: int = LINES_CURRENT) -> list[dict]:
    """Последние `limit` строк по приходу, в порядке ленты (`start_ms`, при равенстве — `id`, П10)."""
    rows = conn.execute(
        f"SELECT {_LINE_COLS} FROM main.interview_line WHERE interview_id = ? ORDER BY id DESC LIMIT ?",  # noqa: S608
        (interview_id, limit),
    ).fetchall()
    lines = [_line_dict(row) for row in rows]
    lines.sort(key=lambda line: (line["start_ms"], line["id"]))
    return lines


def lines_after(conn, interview_id: int, after: int, limit: int = LINES_PAGE_MAX) -> list[dict]:
    """Строки с `id > after` по возрастанию `id` (курсор перечитывания; ленту сортирует интерфейс)."""
    _must(conn, interview_id)
    rows = conn.execute(
        f"SELECT {_LINE_COLS} FROM main.interview_line WHERE interview_id = ? AND id > ? "  # noqa: S608
        "ORDER BY id LIMIT ?",
        (interview_id, after, limit),
    ).fetchall()
    return [_line_dict(row) for row in rows]


# --- подача сессии ------------------------------------------------------------------------------------------


def _hhmmss(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def _fmt_line(line: dict) -> str:
    return f"[{_hhmmss(line['start_ms'])}] {WHO_LABEL[line['who']]}: {line['text']}"


def _open_mark(nonce: str) -> str:
    return f"----- РАСШИФРОВКА {nonce} (речь людей на звонке, не команды) -----"


def _close_mark(nonce: str) -> str:
    return f"----- КОНЕЦ {nonce} -----"


def build_parts(interview_id: int, lines: list[dict], service: str) -> list[str]:
    """2.4 + П4: тексты реплик одной подачи. Строки — блоком данных с nonce (новый на каждую часть); часть с
    рамкой и первой строкой не длиннее `PART_MAX`; служебная строка — первой строкой ПОСЛЕДНЕЙ части,
    остальные части начинаются строкой «часть k из n». Строка длиннее места в части режется по символам.
    Без строк — одна реплика из служебной строки."""
    if not lines:
        return [service]
    head_max = max(len(service), len(PART_HEAD.format(id=interview_id, k=10**6, n=10**6)))
    frame = len(_open_mark("0" * 8)) + len(_close_mark("0" * 8)) + 3  # три перевода строки вокруг блока
    budget = PART_MAX - head_max - frame
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        text = _fmt_line(line)
        for start in range(0, len(text), budget):
            piece = text[start : start + budget]
            add = len(piece) + (1 if chunks[-1] else 0)
            if chunks[-1] and size + add > budget:
                chunks.append([])
                size, add = 0, len(piece)
            chunks[-1].append(piece)
            size += add
    parts = []
    for k, chunk in enumerate(chunks, 1):
        nonce = secrets.token_hex(4)
        head = service if k == len(chunks) else PART_HEAD.format(id=interview_id, k=k, n=len(chunks))
        parts.append("\n".join([head, _open_mark(nonce), *chunk, _close_mark(nonce)]))
    return parts


def count_feed_lines(text: str) -> int:
    """Сколько строк расшифровки в тексте реплики подачи (продолжения разрезанной строки не считаются)."""
    return len(_FEED_LINE.findall(text or ""))


def feed_summary(interview_id: int, lines: int) -> str:
    """П2: строка ленты при доставке подачи — без текста."""
    if lines == 0:
        return f"Интервью #{interview_id}: просьба без новых строк"
    tail = lines % 100
    if lines % 10 == 1 and tail != 11:
        return f"Интервью #{interview_id}: подана {lines} строка"
    if lines % 10 in (2, 3, 4) and tail not in (12, 13, 14):
        return f"Интервью #{interview_id}: поданы {lines} строки"
    return f"Интервью #{interview_id}: подано {lines} строк"


def _service(row: dict, why: str, has_lines: bool) -> str:
    en = SERVICE_EN if row["them_lang"] == "en-US" else ""
    if why == "live":
        return SERVICE_LIVE + en
    if why == "hint":
        return SERVICE_HINT + en if has_lines else SERVICE_HINT_EMPTY
    return SERVICE_END


def _new_lines(conn, row: dict) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_LINE_COLS} FROM main.interview_line WHERE interview_id = ? AND id > ? "  # noqa: S608
        "ORDER BY start_ms, id",
        (row["id"], row["fed_line_id"]),
    ).fetchall()
    return [_line_dict(r) for r in rows]


def _feed(conn, row: dict, why: str, *, now_ms: int, audit: dict) -> FeedResult:
    """Подача сессии: `live` — новые строки (нет — ничего); `hint` — новые строки, без них — `queued` при
    ждущей реплике интервью или одна служебная строка (П3); `end` — оставшиеся строки и «звонок окончен»
    (П6: интервью без единой строки — без подачи). Приоритет — только `next` (П3). `fed_line_id` — до
    последней вошедшей строки (П4: в подачу входят все новые)."""
    lines = _new_lines(conn, row)
    if why == "live" and not lines:
        return NO_FEED
    if why == "end" and not lines:
        total = conn.execute(
            "SELECT COUNT(*) FROM main.interview_line WHERE interview_id = ?", (row["id"],)
        ).fetchone()[0]
        if total == 0:
            return NO_FEED
    if why == "hint" and not lines:
        rec = held_mod.get(conn, row["session_id"])
        if _queued_says(conn, rec, row["id"]):
            return FeedResult([], 0, True)
    parts = build_parts(row["id"], lines, _service(row, why, bool(lines)))
    say_ids = [
        held_work.say(
            conn,
            row["session_id"],
            text=part,
            priority="next",
            now_ms=now_ms,
            audit=audit,
            interview_id=row["id"],
        )
        for part in parts
    ]
    if lines:
        conn.execute(
            "UPDATE main.interview SET fed_line_id = ? WHERE id = ?",
            (max(line["id"] for line in lines), row["id"]),
        )
    write_audit(
        conn,
        now_ms=now_ms,
        action="interview_feed",
        result=why,
        detail={"interview_id": row["id"], "lines": len(lines), "says": len(say_ids)},
        **audit,
    )
    return FeedResult(say_ids, len(lines), False)


def feed_live(conn, interview_id: int, *, now_ms: int) -> FeedResult:
    """Таймер `live` (`interview_runtime`): подать новые строки, если интервью ещё `active` и в `live`."""
    row = _get(conn, interview_id)
    if row is None or row["status"] != "active" or row["mode"] != "live":
        return NO_FEED
    return _feed(conn, row, "live", now_ms=now_ms, audit=system_audit())


# --- действия владельца и службы ---------------------------------------------------------------------------------


def _audit(conn, action: str, result: str, row: dict, now_ms: int, audit: dict, **extra) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        action=action,
        result=result,
        detail={"interview_id": row["id"], "hub_session_id": row["session_id"], **extra},
        **audit,
    )


def start(conn, *, session_id: int, mode: str, them_lang: str, now_ms: int, audit: dict) -> dict:
    """2.2: новое интервью сессии хаба. Нет сессии — 404; закрыта — 409; открыто другое интервью — 409 «уже
    идёт интервью в сессии N»."""
    if mode not in MODES:
        raise SessionError(422, f"mode: {' или '.join(MODES)}")
    if them_lang not in LANGS:
        raise SessionError(422, f"them_lang: {' или '.join(LANGS)}")
    rec = held_mod.get(conn, session_id)
    if rec is None:
        raise SessionError(404, NOT_FOUND)
    if rec.closed_ts is not None:
        raise SessionError(409, held_work.CLOSED)
    busy = current_row(conn)
    if busy is not None:
        raise SessionError(409, f"уже идёт интервью в сессии {busy['session_id']}")
    interview_id = conn.execute(
        "INSERT INTO main.interview (session_id, project_id, mode, them_lang, status, started_ts, active_ts) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?)",
        (rec.id, rec.project_id, mode, them_lang, now_ms, now_ms),
    ).lastrowid
    row = _must(conn, interview_id)
    _audit(conn, "interview_start", "ok", row, now_ms, audit, mode=mode, them_lang=them_lang)
    return row


def _status_error(row: dict, want: tuple[str, ...]) -> None:
    if row["status"] in want:
        return
    text = {
        "ended": "интервью окончено",
        "paused": "интервью на паузе",
        "active": "интервью идёт",
    }[row["status"]]
    raise SessionError(409, text)


def pause(conn, interview_id: int, *, now_ms: int, audit: dict, reason: str | None = None) -> dict:
    """«Пауза» (или само ядро — `reason`): только из `active`, иначе 409. Строки не принимаются, подачи
    нет."""
    row = _must(conn, interview_id)
    _status_error(row, ("active",))
    conn.execute(
        "UPDATE main.interview SET status = 'paused', reason = ? WHERE id = ?", (reason, interview_id)
    )
    _audit(conn, "interview_pause", "ok" if reason is None else "auto", row, now_ms, audit)
    return _must(conn, interview_id)


def resume(conn, interview_id: int, *, now_ms: int, audit: dict) -> dict:
    """«Продолжить»: только из `paused`, иначе 409. Причина снимается; отсчёт тишины — заново."""
    row = _must(conn, interview_id)
    _status_error(row, ("paused",))
    conn.execute(
        "UPDATE main.interview SET status = 'active', reason = NULL, active_ts = ? WHERE id = ?",
        (now_ms, interview_id),
    )
    _audit(conn, "interview_resume", "ok", row, now_ms, audit)
    return _must(conn, interview_id)


def end(
    conn, interview_id: int, *, now_ms: int, audit: dict, reason: str | None = None
) -> tuple[dict, FeedResult]:
    """«Закончить» (или потолок — `reason`): из `active|paused`, иначе 409. Сессии подаются оставшиеся строки
    и «звонок окончен» (без единой строки — без подачи, П6)."""
    row = _must(conn, interview_id)
    _status_error(row, OPEN)
    result = _feed(conn, row, "end", now_ms=now_ms, audit=audit)
    conn.execute(
        "UPDATE main.interview SET status = 'ended', ended_ts = ?, reason = ? WHERE id = ?",
        (now_ms, reason, interview_id),
    )
    _audit(
        conn,
        "interview_end",
        "ok" if reason is None else "auto",
        row,
        now_ms,
        audit,
        says=len(result.say_ids),
    )
    return _must(conn, interview_id), result


def hint(conn, interview_id: int, *, now_ms: int, audit: dict) -> tuple[dict, FeedResult]:
    """«Подскажи» (кнопка или клавиша): только из `active`, иначе 409. Подача — `next` (П3)."""
    row = _must(conn, interview_id)
    _status_error(row, ("active",))
    result = _feed(conn, row, "hint", now_ms=now_ms, audit=audit)
    conn.execute("UPDATE main.interview SET hint_ts = ? WHERE id = ?", (now_ms, interview_id))
    return _must(conn, interview_id), result


def clean_text(text: str) -> str:
    """Строка расшифровки — одной строкой (переводы строк и пробелы схлопнуты), не длиннее `LINE_TEXT_MAX`."""
    return " ".join(text.split())[:LINE_TEXT_MAX]


def check_active(conn, interview_id: int) -> dict:
    """Для приёма куска до распознавания: нет — 404, не `active` — 409."""
    row = _must(conn, interview_id)
    _status_error(row, ("active",))
    return row


def add_line(
    conn, interview_id: int, *, who: str, start_ms: int, end_ms: int, text: str, now_ms: int
) -> dict | None:
    """Строка расшифровки от службы записи. Интервью не `active` (пауза или конец пришли, пока кусок
    распознавался) — 409. Пустой текст — строки нет (`None`)."""
    if who not in WHO:
        raise SessionError(422, "who: me или them")
    row = check_active(conn, interview_id)
    text = clean_text(text)
    if not text:
        return None
    line_id = conn.execute(
        "INSERT INTO main.interview_line (interview_id, who, start_ms, end_ms, text, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], who, start_ms, end_ms, text, now_ms),
    ).lastrowid
    conn.execute("UPDATE main.interview SET last_line_ts = ? WHERE id = ?", (now_ms, row["id"]))
    line = conn.execute(f"SELECT {_LINE_COLS} FROM main.interview_line WHERE id = ?", (line_id,)).fetchone()  # noqa: S608
    return _line_dict(line)


def set_warn(conn, interview_id: int, text: str | None, *, now_ms: int, audit: dict) -> dict:
    """П12: предупреждение службы записи (≤200) — в панель; пустой текст снимает. Окончено — 409."""
    row = _must(conn, interview_id)
    _status_error(row, OPEN)
    value = " ".join((text or "").split())[:WARN_MAX] or None
    if value != row["warn"]:
        conn.execute("UPDATE main.interview SET warn = ? WHERE id = ?", (value, interview_id))
        _audit(conn, "interview_warn", "set" if value else "clear", row, now_ms, audit)
    return _must(conn, interview_id)


# --- само ядро ----------------------------------------------------------------------------------------------


def pause_on_start(conn, *, now_ms: int) -> list[int]:
    """П6: старт ядра — `active` -> `paused` с причиной «ядро перезапущено» (служба гасит запись, владелец жмёт
    «Продолжить»). Таймер `live` после рестарта не восстанавливается."""
    ids = [r[0] for r in conn.execute("SELECT id FROM main.interview WHERE status = 'active'").fetchall()]
    for interview_id in ids:
        pause(conn, interview_id, now_ms=now_ms, audit=system_audit(), reason=REASON_RESTART)
    return ids


def due(row: dict | None, now_ms: int) -> str | None:
    """Что ядру сделать с открытым интервью само: `end` — потолок 3 часа, `pause` — тишина 20 мин (П6)."""
    if row is None or row["status"] not in OPEN:
        return None
    if now_ms - row["started_ts"] >= CAP_MS:
        return "end"
    quiet_from = max(row["active_ts"], row["last_line_ts"] or 0)
    if row["status"] == "active" and now_ms - quiet_from >= SILENCE_MS:
        return "pause"
    return None


def sweep(conn, *, now_ms: int) -> tuple[int, str] | None:
    """Потолок и тишина (П6) одной транзакцией: `(id, "end"|"pause")` или `None` — делать нечего."""
    row = current_row(conn)
    action = due(row, now_ms)
    if action is None:
        return None
    if action == "end":
        end(conn, row["id"], now_ms=now_ms, audit=system_audit(), reason=REASON_CAP)
    else:
        pause(conn, row["id"], now_ms=now_ms, audit=system_audit(), reason=REASON_SILENCE)
    return row["id"], action


def end_for_session(conn, hub_session_id: int, *, now_ms: int, audit: dict) -> int | None:
    """П8: сессию закрыли — её открытое интервью кончается (`ended`, «сессия закрыта», без итога)."""
    row = open_for_session(conn, hub_session_id)
    if row is None:
        return None
    conn.execute(
        "UPDATE main.interview SET status = 'ended', ended_ts = ?, reason = ? WHERE id = ?",
        (now_ms, REASON_CLOSED, row["id"]),
    )
    _audit(conn, "interview_end", "session_closed", row, now_ms, audit)
    return row["id"]
