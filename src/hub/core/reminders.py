"""Напоминание по беседе в срок (Ф34-2, docs/architecture.md; «Поправки после plan-critic»
П1–П12 главнее текста выше них). Хаб обещаний не ищет: срок замечает сессия или владелец и ставит напоминание,
хаб хранит его и срабатывает вовремя.

Таблица `main.reminder` (`0047_main.sql`). Статусы: `pending` -> `fired` -> `done`; из `pending` —
`cancelled` (сняли, беседа в архиве) и `answered` (собеседник ответил); `fired` -> `answered` — ответ принят
с опозданием (П6); перенос возвращает `fired` в `pending`.

- Ставит ТОЛЬКО подписанная сессия (П2): проект и роль напоминания — из её живой подписки на проект (беседа
  может лежать в другом проекте или в «Неразобранном»), или владелец в UI (`api_token_id IS NULL`, проект беседы;
  сессиям такие не сигналятся никогда).
- Сигнал сессии — сама строка (П1): `fired AND signaled_ts IS NULL` считает `subs.wait_state`, отдаёт
  `subs.deliver` (`take_signals`); таблицы сигналов не трогаются. В сигнале — только числа.
- Правит и закрывает сессия с живой подпиской той же роли в проекте напоминания (П3; все сессии ходят одним
  токеном — по токену изоляции нет), владелец в UI — любые.
- Заметка — данные: в сигнал и в аудит не попадает; название беседы и заметка отдаются, только если беседа
  видна принципалу по её НЫНЕШНЕМУ проекту (П10).

Ф89-1 (docs/architecture.md П1–П13 главнее A–D): повторяемые напоминания
(`every`) и напоминания без беседы (`channel_id = NO_CHANNEL`, наружу — `null`, в сигнале — `0`).

- У повторяемого одна строка на весь регламент; `due_ts` — ВСЕГДА ближайший будущий срок на сетке
  (`next_due`), `fired_ts` — когда сработал текущий срок. Минутный проход: `pending` и срок наступил ->
  `fired`, срок — на следующий; `fired` и наступил следующий срок -> `missed += 1`, сигнал заново.
- «Сделано» (`update(close=True)` у повторяемого): `pending`, счётчики, `missed = 0`; у `pending` — пропустить
  ближайший срок. `stop=True` — остановить регламент (`done`, `end_reason = 'stopped'`).
- `cancel_if_reply` у повторяемого и без беседы — всегда 0 (явное `true` — 422). Повторяемое по беседе в
  архиве не снимается (П8), беседа удалена — `cancelled` (`channel_gone`).

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`.
"""

import datetime as dt
import re
import sqlite3
import zoneinfo
from typing import NamedTuple

from . import pace as pace_mod
from . import subs as subs_mod
from .audit import write_audit
from .db import db_of, next_seq

# Владелец 23.10: «зачем нам эти лимиты… пять ждущих — почему так мало?» (беседа #20 упёрлась в 5) — пределы
# подняты до предохранителей от сбоя, в живой работе недостижимых.
NOTE_MAX = 1000
MAX_PENDING_PER_PROJECT = 1000
MAX_PENDING_PER_CHANNEL = 100
_MIN_MS = 60 * 1000
DAY_MS = 24 * 60 * _MIN_MS
AT_MIN_AHEAD_MS = _MIN_MS
AT_MAX_AHEAD_MS = 90 * DAY_MS
# Решение 6: напоминание сессии с ролью владельцу на экран — не раньше 20 минут после срабатывания (сначала
# сессия); без роли и свои — сразу.
NOTIFY_ROLE_DELAY_MS = 20 * _MIN_MS
LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200
MARK_MAX = 200

OPEN_STATUSES = ("pending", "fired")
CLOSED_STATUSES = ("done", "cancelled", "answered")

# Ф89-1 (A.1): беседы у напоминания нет — `channel_id = 0` (id бесед начинаются с 1).
NO_CHANNEL = 0
# Ф89-1 (A.2): пояс напоминания без беседы.
DEFAULT_TZ = "Europe/Moscow"
_HOUR_MS = 60 * _MIN_MS
EVERY_GRID = ("day", "weekdays", "week")
_EVERY_H_RE = re.compile(r"^([1-9]|1\d|2[0-4])h$")
EVERY_MAX = 8
STOPPED = "stopped"
_WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# Ф89-1: напоминание «ещё живо» для пределов — ждущее или сработавшее повторяемое (регламент идёт).
_LIVE = "(status = 'pending' OR (status = 'fired' AND every IS NOT NULL))"

_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?$")


class ReminderError(Exception):
    """Отказ ручки напоминаний: `status` — HTTP-код, `detail` — текст ответа (без заметок и названий)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- разбор запроса ---------------------------------------------------------------------------------------


def parse_at(value: object, tz: dt.tzinfo, now_ms: int) -> int:
    """П12: `"YYYY-MM-DD HH:MM"` — время в поясе аккаунта беседы `tz`; ISO со смещением — как есть. Не раньше
    `now + 1 мин`, не позже `now + 90 дней`. -> мс UTC. Отказ — `ReminderError(422)` с текстом «at: …»."""
    text = value.strip() if isinstance(value, str) else ""
    if not _AT_RE.match(text):
        raise ReminderError(
            422, 'at: нужно время вида "2026-10-05 14:00" (время аккаунта беседы) или ISO со смещением'
        )
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReminderError(422, f"at: нет такого времени ({text})") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    ts = int(moment.timestamp() * 1000)
    if ts < now_ms + AT_MIN_AHEAD_MS:
        raise ReminderError(422, "at: время уже прошло или меньше чем через минуту")
    if ts > now_ms + AT_MAX_AHEAD_MS:
        raise ReminderError(422, "at: не позже чем через 90 дней")
    return ts


def note_form(note: str | None) -> str | None:
    """Заметка — одной строкой (пробельные схлопнуты), до `NOTE_MAX`; пустая — `None`."""
    if note is None:
        return None
    text = " ".join(note.split())
    if len(text) > NOTE_MAX:
        raise ReminderError(422, f"note: не длиннее {NOTE_MAX} символов, получено {len(text)}")
    return text or None


def every_form(value: object) -> str | None:
    """Ф89-1 (A.2): `None` — разовое; `"day"`, `"weekdays"`, `"week"`, `"<N>h"` (N = 1…24). Прочее — 422."""
    if value is None:
        return None
    text = value.strip().lower() if isinstance(value, str) else ""
    if text in EVERY_GRID or _EVERY_H_RE.match(text):
        return text
    raise ReminderError(422, 'every: "day", "weekdays", "week" или "<N>h" (N от 1 до 24)')


def zone(name: str | None) -> dt.tzinfo:
    """Пояс по имени IANA; кривое или пустое — `DEFAULT_TZ`."""
    try:
        return zoneinfo.ZoneInfo(name or DEFAULT_TZ)
    except zoneinfo.ZoneInfoNotFoundError, ValueError, OSError:
        return zoneinfo.ZoneInfo(DEFAULT_TZ)


def zone_name(tz: dt.tzinfo) -> str:
    return getattr(tz, "key", None) or DEFAULT_TZ


def _hm(ts_ms: int, tz: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz).strftime("%H:%M")


def _at_hm_of(every: str | None, due_ts: int, tz: dt.tzinfo) -> str | None:
    """`at_hm` сетки: у `day`/`weekdays`/`week` — время срока `due_ts` в поясе; у `<N>h` и разового — нет."""
    return _hm(due_ts, tz) if every in EVERY_GRID else None


def _at_wd_of(every: str | None, due_ts: int, tz: dt.tzinfo) -> int | None:
    """День недели сетки `week` (0 — пн) — из срока `due_ts`; у прочих — нет. Ревью Ф89-1: «Отложить» сдвигает
    `due_ts`, а день недели регламента остаётся прежним — поэтому он хранится, а не берётся из `due_ts`."""
    return dt.datetime.fromtimestamp(due_ts / 1000, tz).weekday() if every == "week" else None


def next_after(
    every: str, at_hm: str | None, tz: dt.tzinfo, anchor_ts: int, after_ms: int, weekday: int | None = None
) -> int:
    """Ф89-1 (A.2, П2): первый срок по правилу строго позже `after_ms`. `anchor_ts` — текущий срок на сетке
    (`due_ts`): у `week` даёт день недели, у `<N>h` — начало шагов по N часов. Даты — в поясе `tz` через
    `zoneinfo` (переход времени честно: стенное время сетки сохраняется)."""
    m = _EVERY_H_RE.match(every)
    if m is not None:
        step = int(m.group(1)) * _HOUR_MS
        if anchor_ts > after_ms:
            return anchor_ts
        return anchor_ts + ((after_ms - anchor_ts) // step + 1) * step
    hour, minute = (int(x) for x in (at_hm or _hm(anchor_ts, tz)).split(":"))
    if weekday is None:
        weekday = dt.datetime.fromtimestamp(anchor_ts / 1000, tz).weekday()
    day = dt.datetime.fromtimestamp(after_ms / 1000, tz).date()
    for _ in range(16):
        moment = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        ts = int(moment.timestamp() * 1000)
        ok = (
            every == "day"
            or (every == "weekdays" and moment.weekday() < 5)
            or (every == "week" and moment.weekday() == weekday)
        )
        if ok and ts > after_ms:
            return ts
        day += dt.timedelta(days=1)
    raise AssertionError(f"next_after: нет срока для {every!r}")  # не бывает: за 16 дней срок есть всегда


def next_due(rec: Rec, after_ms: int) -> int:
    """Следующий срок повторяемого `rec` строго позже `after_ms` (по его `every`, `at_hm`, `tz`, `due_ts`)."""
    assert rec.every is not None  # noqa: S101 — только у повторяемого
    return next_after(rec.every, rec.at_hm, zone(rec.tz), rec.due_ts, after_ms, rec.at_wd)


def every_text(
    every: str | None,
    at_hm: str | None,
    tz: dt.tzinfo | None = None,
    due_ts: int | None = None,
    weekday: int | None = None,
) -> str | None:
    """Период словами: «каждый день в 09:00», «по будням в 10:00», «каждую неделю: пн в 10:00»,
    «каждые 4 ч»."""
    if every is None:
        return None
    if every == "day":
        return f"каждый день в {at_hm}"
    if every == "weekdays":
        return f"по будням в {at_hm}"
    if every == "week":
        day = ""
        if weekday is None and tz is not None and due_ts is not None:
            weekday = dt.datetime.fromtimestamp(due_ts / 1000, tz).weekday()
        if weekday is not None:
            day = _WEEKDAYS_RU[weekday] + " "
        return f"каждую неделю: {day}в {at_hm}"
    return f"каждые {every[:-1]} ч"


# --- строки -----------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "channel_id",
    "project_id",
    "due_ts",
    "note",
    "cancel_if_reply",
    "base_message_ts",
    "role",
    "role_key",
    "api_token_id",
    "status",
    "created_ts",
    "updated_ts",
    "fired_ts",
    "signal_sub_id",
    "signaled_ts",
    "delivered_ts",
    "notified_ts",
    "closed_ts",
    "end_reason",
    "seq",
    "role_no",
    "every",
    "at_hm",
    "tz",
    "missed",
    "done_count",
    "last_done_ts",
    "last_done_role",
    "at_wd",
)
_SELECT = ", ".join(f"r.{c}" for c in _COLUMNS)


class Rec(NamedTuple):
    id: int
    channel_id: int
    project_id: int | None
    due_ts: int
    note: str | None
    cancel_if_reply: int
    base_message_ts: int
    role: str | None
    role_key: str | None
    api_token_id: int | None
    status: str
    created_ts: int
    updated_ts: int
    fired_ts: int | None
    signal_sub_id: int | None
    signaled_ts: int | None
    delivered_ts: int | None
    notified_ts: int | None
    closed_ts: int | None
    end_reason: str | None
    seq: int
    # Ф35-1 (решение 3): номер линии поставившей подписки; значим только при роли (П2).
    role_no: int = 1
    # Ф89-1 (A.1): период и учёт «сделано».
    every: str | None = None
    at_hm: str | None = None
    tz: str | None = None
    missed: int = 0
    done_count: int = 0
    last_done_ts: int | None = None
    last_done_role: str | None = None
    at_wd: int | None = None


def _select(conn: sqlite3.Connection, where: str, params: tuple | list = ()) -> list[Rec]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.reminder r WHERE {where}",  # noqa: S608 — фрагменты заданы кодом
        params,
    ).fetchall()
    return [Rec(*row) for row in rows]


def get(conn: sqlite3.Connection, reminder_id: int) -> Rec | None:
    rows = _select(conn, "r.id = ?", (reminder_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, reminder_id: int, now_ms: int, **sets) -> None:
    """Правка строки: `updated_ts` и новый `seq` (П9: UI узнаёт через `/sync`) — всегда."""
    sets = {**sets, "updated_ts": now_ms, "seq": next_seq(conn)}
    assign = ", ".join(f"{column} = ?" for column in sets)
    conn.execute(
        f"UPDATE main.reminder SET {assign} WHERE id = ?",  # noqa: S608 — имена колонок заданы кодом
        (*sets.values(), reminder_id),
    )


class _Chan(NamedTuple):
    id: int
    account_id: int
    title: str | None
    project_id: int | None
    archived: int


def _chan(conn: sqlite3.Connection, channel_id: int) -> _Chan | None:
    """Беседа напоминания; `NO_CHANNEL` сюда не передаётся (у него беседы нет по замыслу)."""
    assert channel_id != NO_CHANNEL  # noqa: S101 — `db_of(0)` не зовём
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT id, account_id, title, project_id, archived FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    return _Chan(*row) if row is not None else None


def _visible(principal, project_id: int | None) -> bool:
    """Тот же предикат, что `queries.channel_visible`: список проектов токена; без списка — всё."""
    if principal.project_ids is None:
        return True
    return project_id is not None and project_id in principal.project_ids


def _base_ts(conn: sqlite3.Connection, channel_id: int, now_ms: int) -> int:
    """Последнее настоящее сообщение беседы (есть в источнике, не скрыто, не удалено); беседа пуста
    (заготовка первого письма) — «сейчас»."""
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT MAX(ts) FROM {schema}.message "  # noqa: S608 — схема из `db_of`
        "WHERE channel_id = ? AND ext_id IS NOT NULL AND deleted = 0 AND hidden = 0",
        (channel_id,),
    ).fetchone()
    return row[0] if row is not None and row[0] is not None else now_ms


def replied(conn: sqlite3.Connection, channel_id: int, after_ts: int, upto_ts: int | None = None) -> bool:
    """П8: в беседе есть «чужое сообщение» позже `after_ts` (и, если задано, не позже `upto_ts`) — не своё,
    не служебное, не удалённое, не скрытое, дошедшее до источника и не от бота (Ф20-1)."""
    schema = db_of(channel_id)
    upto = "" if upto_ts is None else "AND ts <= ? "
    params = (channel_id, after_ts) if upto_ts is None else (channel_id, after_ts, upto_ts)
    row = conn.execute(
        f"SELECT 1 FROM {schema}.message WHERE channel_id = ? AND ts > ? {upto}"  # noqa: S608
        "AND is_me = 0 AND kind <> 'event' AND deleted = 0 AND hidden = 0 AND ext_id IS NOT NULL "
        "AND is_bot = 0 LIMIT 1",
        params,
    ).fetchone()
    return row is not None


# --- подписка сессии (П2, П3) ------------------------------------------------------------------------------


def session_sub(conn: sqlite3.Connection, principal, session: dict | None) -> subs_mod.SubRec:
    """П2: живая подписка ЭТОЙ сессии (токен, pid + `starttime`, разговор) на проект. Сессии нет или она
    мертва — 409; подписки нет — 409 «сначала hub_subscribe» (так же отсекаются дежурная и оркестратор:
    подписаться они не могут)."""
    if session is None:
        raise ReminderError(422, "session — обязательна bearer-токену сессии")
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    if not subs_mod.is_alive(pid, proc_start, session.get("host")):
        raise ReminderError(409, "сессия не найдена")
    row = conn.execute(
        "SELECT id FROM main.session_sub WHERE api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL ORDER BY id DESC LIMIT 1",
        (principal.token_id, pid, proc_start, session_id),
    ).fetchone()
    sub = subs_mod.get_sub(conn, row[0]) if row is not None else None
    if sub is None:
        raise ReminderError(409, "сначала hub_subscribe() — напоминания ведёт сессия с подпиской на проект")
    if sub.project_id == subs_mod.NO_PROJECT_ID:
        # Ф35-1 (решение 14): у агента вне проектов прав проекта нет.
        raise ReminderError(409, "напоминания — сессии с подпиской на проект")
    return sub


def _role_scope(sub) -> tuple[str, list]:
    """Чьи напоминания слышит подписка `sub` (П2): с ролью — напоминания её ЛИНИИ `(проект, роль, номер)`
    (Ф35-1, решение 3); без роли — безрольные напоминания проекта от того же токена (Ф35-1 П2: как раньше,
    номер безрольного — только адрес). Напоминания владельца из UI не входят никогда."""
    if sub.role_key is not None:
        return "r.api_token_id IS NOT NULL AND r.project_id = ? AND r.role_key = ? AND r.role_no = ?", [
            sub.project_id,
            sub.role_key,
            sub.role_no,
        ]
    return "r.project_id = ? AND r.role_key IS NULL AND r.api_token_id = ?", [
        sub.project_id,
        sub.api_token_id,
    ]


def listeners(conn: sqlite3.Connection, rec: Rec) -> list[subs_mod.SubRec]:
    """Ф66-1, ревью Р2: живые подписки, которым сигнал напоминания `rec` будет отдан (`_role_scope` с другой
    стороны): с ролью — подписки её линии в проекте напоминания, без роли — безрольные подписки проекта от
    того же токена. Напоминание владельца из UI (`api_token_id IS NULL`) не слышит никто."""
    if rec.api_token_id is None or rec.project_id is None:
        return []
    if rec.role_key is not None:
        rows = conn.execute(
            "SELECT id FROM main.session_sub WHERE ended_ts IS NULL AND project_id = ? AND role_key = ? "
            "AND role_no = ? ORDER BY id",
            (rec.project_id, rec.role_key, rec.role_no),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM main.session_sub WHERE ended_ts IS NULL AND project_id = ? AND role_key IS NULL "
            "AND api_token_id = ? ORDER BY id",
            (rec.project_id, rec.api_token_id),
        ).fetchall()
    found = (subs_mod.get_sub(conn, row[0]) for row in rows)
    return [sub for sub in found if sub is not None]


def _may_edit(rec: Rec, sub) -> bool:
    """П3: та же линия (роль и номер — Ф35-1) в проекте напоминания; безрольное — любая подписанная
    безрольная сессия проекта."""
    if rec.api_token_id is None or rec.project_id != sub.project_id or rec.role_key != sub.role_key:
        return False
    return rec.role_key is None or rec.role_no == sub.role_no


# --- постановка, перенос, закрытие -------------------------------------------------------------------------


def _audit(
    conn,
    *,
    principal,
    action: str,
    rec: Rec,
    now_ms: int,
    client_name,
    request_id: str,
    result: str = "ok",
    detail: dict | None = None,
) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=client_name,
        request_id=request_id,
        action=action,
        result=result,
        # Ф89-1 (П11): напоминание без беседы — без беседы и в журнале.
        channel_id=None if rec.channel_id == NO_CHANNEL else rec.channel_id,
        detail={"reminder_id": rec.id, **(detail or {})},  # заметка в аудит не пишется (решение 8)
    )


def _pending_count(conn, where: str, params: tuple) -> int:
    """Ждущие по условию; Ф89-1 (A.3): сработавшее повторяемое — тоже ждущее (регламент идёт)."""
    return conn.execute(
        f"SELECT COUNT(*) FROM main.reminder WHERE {_LIVE} AND {where}",  # noqa: S608
        params,
    ).fetchone()[0]


def _check_limits(conn, channel_id: int, project_id: int | None) -> None:
    """429 — 5 ждущих на беседу (беседа `NO_CHANNEL` в счёт «на беседу» не идёт), 50 на проект."""
    if channel_id != NO_CHANNEL:
        if _pending_count(conn, "channel_id = ?", (channel_id,)) >= MAX_PENDING_PER_CHANNEL:
            raise ReminderError(429, f"на беседу уже {MAX_PENDING_PER_CHANNEL} ждущих напоминаний")
    if _pending_count(conn, "project_id IS ?", (project_id,)) >= MAX_PENDING_PER_PROJECT:
        raise ReminderError(429, f"в проекте уже {MAX_PENDING_PER_PROJECT} ждущих напоминаний")


def _cancel_form(cancel_if_reply: bool | None, *, every: str | None, has_channel: bool) -> int:
    """Ф89-1 (П4): умолчание — `true` у разового по беседе, `false` у повторяемого и без беседы; явное `true`
    у повторяемого или без беседы — 422."""
    if cancel_if_reply is None:
        return int(every is None and has_channel)
    if cancel_if_reply and (every is not None or not has_channel):
        raise ReminderError(
            422, "cancel_if_reply: только у разового напоминания по беседе (повторяемое и без беседы — false)"
        )
    return int(bool(cancel_if_reply))


def _project_exists(conn, project_id: int) -> bool:
    return conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is not None


def create(
    conn: sqlite3.Connection,
    *,
    principal,
    channel_id: int | None,
    at: str,
    note: str | None,
    cancel_if_reply: bool | None,
    session: dict | None,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    every: str | None = None,
    project_id: int | None = None,
) -> Rec:
    """`POST /reminders`. Сессия (bearer): беседа видна токену, проект и роль — из живой подписки сессии на
    проект (П2). Владелец (UI): проект — проект беседы, роли нет. 404 — беседы нет или не видна; 409 — беседа в
    архиве, сессия без подписки; 422 — время или заметка; 429 — лимиты (50 ждущих на проект, 5 на беседу).

    Ф89-1: `channel_id=None` — напоминание без беседы: у сессии проект — из подписки (`project_id` — 422, если
    не он), у владельца `project_id` обязателен (404 — нет проекта). `every` — период (`every_form`); пояс — пояс
    аккаунта беседы, без беседы — `DEFAULT_TZ`. Повторяемое по беседе в архиве ставится (П8: архив его не
    снимает), разовое — 409."""
    text = note_form(note)
    every = every_form(every)
    chan = None
    if channel_id is not None:
        chan = _chan(conn, channel_id)
        if chan is None or not _visible(principal, chan.project_id):
            raise ReminderError(404, "беседа не найдена")
        if project_id is not None:
            raise ReminderError(422, "project_id — только у напоминания без беседы")
    if principal.is_ui:
        if session is not None:
            raise ReminderError(422, "session — только у bearer-токена сессии")
        if chan is None:
            if project_id is None:
                raise ReminderError(422, "project_id — обязателен у напоминания без беседы")
            if not _project_exists(conn, project_id) or not _visible(principal, project_id):
                raise ReminderError(404, "проект не найден")
            reminder_project = project_id
        else:
            reminder_project = chan.project_id
        role, role_key, token_id, role_no = None, None, None, 1
    else:
        sub = session_sub(conn, principal, session)
        if chan is None and project_id is not None and project_id != sub.project_id:
            raise ReminderError(422, "project_id: напоминание сессии — в проекте её подписки")
        reminder_project, role, role_key = sub.project_id, sub.role, sub.role_key
        token_id, role_no = principal.token_id, sub.role_no
    if chan is not None and chan.archived and every is None:
        raise ReminderError(409, "беседа в архиве")
    flag = _cancel_form(cancel_if_reply, every=every, has_channel=chan is not None)
    tz = pace_mod.settings_for(conn, chan.account_id).tz if chan is not None else zone(DEFAULT_TZ)
    due_ts = parse_at(at, tz, now_ms)
    stored_channel = chan.id if chan is not None else NO_CHANNEL
    _check_limits(conn, stored_channel, reminder_project)
    cur = conn.execute(
        "INSERT INTO main.reminder (channel_id, project_id, due_ts, note, cancel_if_reply, base_message_ts, "
        "role, role_key, api_token_id, status, created_ts, updated_ts, seq, role_no, every, at_hm, tz, "
        "at_wd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stored_channel,
            reminder_project,
            due_ts,
            text,
            flag,
            _base_ts(conn, stored_channel, now_ms) if chan is not None else now_ms,
            role,
            role_key,
            token_id,
            now_ms,
            now_ms,
            next_seq(conn),
            role_no,
            every,
            _at_hm_of(every, due_ts, tz),
            zone_name(tz),
            _at_wd_of(every, due_ts, tz),
        ),
    )
    rec = get(conn, cur.lastrowid)
    assert rec is not None  # noqa: S101 — только что вставлена в этой транзакции
    _audit(
        conn,
        principal=principal,
        action="reminder_create",
        rec=rec,
        now_ms=now_ms,
        client_name=client_name,
        request_id=request_id,
        detail={
            "project_id": reminder_project,
            "due_ts": due_ts,
            "role": role_key is not None,
            **({"every": every} if every is not None else {}),
        },
    )
    return rec


UNSET = object()  # `update(every=…)`: поле не передано (`None` — «сделать разовым»)
_SIGNAL_RESET = {"signal_sub_id": None, "signaled_ts": None, "delivered_ts": None, "notified_ts": None}


def _done_sets(rec: Rec, role: str | None, now_ms: int) -> dict:
    """П5: «сделано» у повторяемого. `fired` -> `pending`, срок — ближайший будущий (он уже на сетке; проход
    мог не успеть передвинуть — тогда следующий после now); `pending` сюда не попадает (уже сделано).
    Счётчики, `missed = 0`, сигнальные поля — `NULL`."""
    due = rec.due_ts if rec.due_ts > now_ms else next_due(rec, now_ms)
    return {
        "status": "pending",
        "due_ts": due,
        "fired_ts": None,
        "missed": 0,
        "done_count": rec.done_count + 1,
        "last_done_ts": now_ms,
        "last_done_role": role,
        "base_message_ts": now_ms,
        **_SIGNAL_RESET,
    }


def update(
    conn: sqlite3.Connection,
    reminder_id: int,
    *,
    principal,
    session: dict | None,
    at: str | None,
    note: str | None,
    cancel_if_reply: bool | None,
    close: bool,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    owner: bool = False,
    every=UNSET,
    stop: bool = False,
) -> Rec:
    """`PATCH /reminders/{id}`. `close` — закрыть: `pending` -> `cancelled`, `fired` -> `done` (уже закрытое —
    как есть). Иначе правка `pending`/`fired`: `at` — перенос (`fired` снова `pending`: сработает ещё раз,
    сигнал, доставка и уведомление — заново; точка отсчёта «ответил» — последнее сообщение на момент
    переноса), `note` (`""` — убрать), `cancel_if_reply`. Без `at` сработавшее остаётся сработавшим.
    404 — нет или не видно; 403 — напоминание другой роли (П3); 409 — уже закрыто, беседа в архиве.

    Ф56-1а: `owner` — действует владелец через своего агента (ответ из канала владельца): права — как у кука, хотя
    принципал — токен агента.

    Ф89-1: у повторяемого `close` — «сделано» (`_done_sets`; пределы не проверяются), `stop` — остановить
    регламент (`done`, `end_reason='stopped'`); у разового `stop` — то же, что `close`. `every` (передан —
    даже `None`: «сделать разовым») — сменить период: `at_hm` — из `at` или из текущего срока. `at` у
    повторяемого сдвигает только ближайший срок (`at_hm` не меняется; у `<N>h` сетка переезжает на `at`)."""
    rec = get(conn, reminder_id)
    if rec is None or not _visible(principal, rec.project_id):
        raise ReminderError(404, "напоминание не найдено")
    owner = owner or principal.is_ui
    role = None
    if owner:
        if session is not None:
            raise ReminderError(422, "session — только у bearer-токена сессии")
    else:
        sub = session_sub(conn, principal, session)
        if not _may_edit(rec, sub):
            raise ReminderError(403, "напоминание другой роли — править и закрывать может только она и владелец")
        role = sub.role
    audit = {"now_ms": now_ms, "client_name": client_name, "request_id": request_id}
    if close or stop:
        if at is not None or note is not None or cancel_if_reply is not None or every is not UNSET:
            raise ReminderError(422, "close — без at, note, every и cancel_if_reply")
        if rec.status not in OPEN_STATUSES:
            return rec
        if rec.every is not None and not stop:
            if rec.status == "pending":
                # Ревью Ф89-1: «сделано» по ждущему — уже сделано (повтор вызова, двойной клик, владелец и сессия
                # разом); срок не трогаем, иначе повтор молча съел бы следующий срок.
                return rec
            _update(conn, rec.id, now_ms, **_done_sets(rec, role, now_ms))
            _audit(conn, principal=principal, action="reminder_close", rec=rec, result="repeat_done", **audit)
            return get(conn, rec.id)  # type: ignore[return-value]
        if rec.every is not None:
            status, reason = "done", STOPPED
        else:
            status = "cancelled" if rec.status == "pending" else "done"
            reason = "owner" if owner else "session"
        _update(conn, rec.id, now_ms, status=status, closed_ts=now_ms, end_reason=reason)
        _audit(conn, principal=principal, action="reminder_close", rec=rec, result=status, **audit)
        return get(conn, rec.id)  # type: ignore[return-value]
    if at is None and note is None and cancel_if_reply is None and every is UNSET:
        raise ReminderError(422, "нечего менять: at, note, every, cancel_if_reply или close")
    if rec.status not in OPEN_STATUSES:
        raise ReminderError(409, "напоминание уже закрыто")
    new_every = rec.every if every is UNSET else every_form(every)
    has_channel = rec.channel_id != NO_CHANNEL
    chan = _chan(conn, rec.channel_id) if has_channel else None
    sets: dict = {}
    if note is not None:
        sets["note"] = note_form(note)
    if cancel_if_reply is not None or new_every != rec.every:
        wanted = cancel_if_reply
        if wanted is None:
            # Период сменился — флаг подстраивается сам: повторяемому он не положен, разовому — как был.
            wanted = bool(rec.cancel_if_reply) and new_every is None
        flag = _cancel_form(wanted, every=new_every, has_channel=has_channel)
        if flag != rec.cancel_if_reply:
            sets["cancel_if_reply"] = flag
    if rec.tz:
        tz = zone(rec.tz)
    else:
        tz = pace_mod.settings_for(conn, chan.account_id).tz if chan is not None else zone(None)
    due_ts = rec.due_ts
    if at is not None:
        if has_channel and (chan is None or (chan.archived and new_every is None)):
            raise ReminderError(409, "беседа в архиве")
        if rec.status != "pending" and rec.every is None:
            # Ревью Ф34-2: сработавшее возвращается в ждущие — те же лимиты, что при постановке (сработавшее
            # повторяемое и так в счёте ждущих).
            _check_limits(conn, rec.channel_id, rec.project_id)
        due_ts = parse_at(at, tz, now_ms)
        sets.update(
            due_ts=due_ts,
            status="pending",
            base_message_ts=_base_ts(conn, rec.channel_id, now_ms) if has_channel else now_ms,
            fired_ts=None,
            **_SIGNAL_RESET,
        )
    if new_every != rec.every:
        status = sets.get("status", rec.status)
        if new_every is None:
            # «Сделать разовым»: сработавшее остаётся сработавшим в свой срок; ждущее — к ближайшему сроку.
            if status == "fired":
                sets["due_ts"] = rec.fired_ts or now_ms
            sets.update(at_hm=None, at_wd=None, missed=0)
        else:
            if status == "fired" and due_ts <= now_ms:
                # Сработавшее разовое стало повторяемым: ближайший срок — следующий на новой сетке.
                due_ts = next_after(new_every, _at_hm_of(new_every, due_ts, tz), tz, due_ts, now_ms)
                sets["due_ts"] = due_ts
            sets["at_hm"] = _at_hm_of(new_every, due_ts, tz)
            sets["at_wd"] = _at_wd_of(new_every, due_ts, tz)
        sets.update(every=new_every, tz=zone_name(tz))
    _update(conn, rec.id, now_ms, **sets)
    detail = {"moved": at is not None, **({"due_ts": sets["due_ts"]} if "due_ts" in sets else {})}
    if new_every != rec.every:
        detail["every"] = new_every
    _audit(conn, principal=principal, action="reminder_update", rec=rec, detail=detail, **audit)
    return get(conn, rec.id)  # type: ignore[return-value]


# --- срабатывание (решение 4, П6, П11) ---------------------------------------------------------------------


def _late_answered(conn: sqlite3.Connection, rec: Rec) -> bool:
    """П6: у сработавшего с `cancel_if_reply` нашёлся ответ, написанный ДО срабатывания, но принятый позже
    (после сна ноутбука тик опередил догон коннектора)."""
    if rec.status != "fired" or not rec.cancel_if_reply or rec.fired_ts is None:
        return False
    if rec.channel_id == NO_CHANNEL:
        return False
    return replied(conn, rec.channel_id, rec.base_message_ts, rec.fired_ts)


def _close(conn: sqlite3.Connection, rec: Rec, status: str, reason: str, now_ms: int) -> None:
    _update(conn, rec.id, now_ms, status=status, closed_ts=now_ms, end_reason=reason)


def has_work(conn: sqlite3.Connection, now_ms: int) -> bool:
    """П11, читатель: есть ли что делать тику — должные `pending`, сработавшие повторяемые, у которых наступил
    следующий срок (Ф89-1, П2), или сработавшие с опоздавшим ответом."""
    row = conn.execute(
        f"SELECT 1 FROM main.reminder WHERE {_LIVE} AND due_ts <= ? LIMIT 1",  # noqa: S608
        (now_ms,),
    ).fetchone()
    return row is not None or stale(conn)


def stale(conn: sqlite3.Connection, where: str = "1 = 1", params: tuple | list = ()) -> bool:
    """Читатель: среди сработавших (по условию `where`) есть с опоздавшим ответом — нужен `settle`."""
    rows = _select(conn, f"r.status = 'fired' AND r.cancel_if_reply = 1 AND {where}", params)
    return any(_late_answered(conn, rec) for rec in rows)


def settle(conn: sqlite3.Connection, now_ms: int) -> int:
    """П6: сработавшие с опоздавшим ответом -> `answered`. Возвращает число переведённых."""
    count = 0
    for rec in _select(conn, "r.status = 'fired' AND r.cancel_if_reply = 1 ORDER BY r.id"):
        if _late_answered(conn, rec):
            _close(conn, rec, "answered", "answered_late", now_ms)
            count += 1
    return count


def fire_due(conn: sqlite3.Connection, now_ms: int) -> dict[str, int]:
    """Тик ядра: должные `pending` (в том числе пропущенные за простой — на первом тике, один раз): беседы
    нет или она в архиве -> `cancelled`; `cancel_if_reply` и собеседник ответил -> `answered` (никого не
    будит); иначе `fired` — ждёт сигнала подписке роли и видно на главной. Затем — опоздавшие ответы."""
    counts = {"fired": 0, "answered": 0, "cancelled": 0}
    for rec in _select(conn, "r.status = 'pending' AND r.due_ts <= ? ORDER BY r.due_ts, r.id", (now_ms,)):
        chan = None if rec.channel_id == NO_CHANNEL else _chan(conn, rec.channel_id)
        gone = rec.channel_id != NO_CHANNEL and chan is None
        if gone or (chan is not None and chan.archived and rec.every is None):
            # Ф89-1 (П8): повторяемое по беседе в архиве срабатывает как обычно; удалена — снимается.
            _close(conn, rec, "cancelled", "channel_gone" if gone else "archived", now_ms)
            counts["cancelled"] += 1
        elif rec.cancel_if_reply and replied(conn, rec.channel_id, rec.base_message_ts):
            _close(conn, rec, "answered", "answered", now_ms)
            counts["answered"] += 1
        else:
            sets: dict = {"status": "fired", "fired_ts": now_ms}
            if rec.every is not None:
                sets["due_ts"] = next_due(rec, now_ms)  # П2: `due_ts` — ближайший будущий срок
            _update(conn, rec.id, now_ms, **sets)
            counts["fired"] += 1
    # Ф89-1 (П2): сработавшее повторяемое без «сделано», а следующий срок наступил — пропущен срок.
    missed_where = "r.status = 'fired' AND r.every IS NOT NULL AND r.due_ts <= ? ORDER BY r.due_ts, r.id"
    for rec in _select(conn, missed_where, (now_ms,)):
        if rec.channel_id != NO_CHANNEL and _chan(conn, rec.channel_id) is None:
            _close(conn, rec, "cancelled", "channel_gone", now_ms)
            counts["cancelled"] += 1
            continue
        _update(
            conn,
            rec.id,
            now_ms,
            missed=rec.missed + 1,
            fired_ts=now_ms,
            due_ts=next_due(rec, now_ms),
            **_SIGNAL_RESET,
        )
        counts["missed"] = counts.get("missed", 0) + 1  # ключ — только когда пропуск был
    counts["answered"] += settle(conn, now_ms)
    return counts


# --- сигнал подписке (П1) ----------------------------------------------------------------------------------


def signal_count(conn: sqlite3.Connection, sub) -> int:
    """Читатель, для ожидания: сработавшие напоминания роли подписки, ещё не отданные сигналом."""
    where, params = _role_scope(sub)
    return conn.execute(
        "SELECT COUNT(*) FROM main.reminder r "  # noqa: S608 — фрагмент `_role_scope` задан кодом
        f"WHERE r.status = 'fired' AND r.signaled_ts IS NULL AND {where}",
        params,
    ).fetchone()[0]


def take_signals(
    conn: sqlite3.Connection,
    sub,
    *,
    now_ms: int,
    limit: int,
    api_token_id: int,
    client_name: str | None,
    request_id: str,
) -> list[dict]:
    """П1: отдать подписке сигналы сработавших напоминаний её роли — `{"kind": "reminder", "reminder_id",
    "channel_id"}`, каждому `signaled_ts` (сигнал отдаётся один раз). Проверок сообщений (`answered`,
    `chan_sub`, `moved`) нет; опоздавший ответ (П6) переводит в `answered` без сигнала."""
    where, params = _role_scope(sub)
    rows = _select(
        conn,
        f"r.status = 'fired' AND r.signaled_ts IS NULL AND {where} ORDER BY r.fired_ts, r.id",
        params,
    )
    signals: list[dict] = []
    for rec in rows:
        if _late_answered(conn, rec):
            _close(conn, rec, "answered", "answered_late", now_ms)
            continue
        if len(signals) >= limit:
            continue  # остаются ждать — следующей выдачей
        conn.execute(
            "UPDATE main.reminder SET signal_sub_id = ?, signaled_ts = ? WHERE id = ?",
            (sub.id, now_ms, rec.id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            action="sub_signal",
            result="delivered",
            channel_id=None if rec.channel_id == NO_CHANNEL else rec.channel_id,
            detail={"sub_id": sub.id, "kind": "reminder", "reminder_id": rec.id},
        )
        # Ф89-1 (П3): без беседы — `channel_id: 0` (старый клиент требует целое); `missed` — пропущено сроков
        # повторяемого (поле только при `missed > 0`).
        signal = {"kind": "reminder", "reminder_id": rec.id, "channel_id": rec.channel_id}
        if rec.missed > 0:
            signal["missed"] = rec.missed
        signals.append(signal)
    return signals


def waiting_count(conn: sqlite3.Connection, sub) -> int:
    """Решение 5: сработавшие напоминания роли подписки, которые сессия ещё не прочла (`hub_reminders`)."""
    where, params = _role_scope(sub)
    return conn.execute(
        "SELECT COUNT(*) FROM main.reminder r "  # noqa: S608 — фрагмент `_role_scope` задан кодом
        f"WHERE r.status = 'fired' AND r.delivered_ts IS NULL AND {where}",
        params,
    ).fetchone()[0]


def mark_delivered(conn: sqlite3.Connection, ids: list[int], sub, *, now_ms: int) -> int:
    """`hub_reminders()` прочла заметки: `delivered_ts` сработавшим напоминаниям роли подписки (чужие и уже
    отмеченные — мимо). Возвращает число отмеченных."""
    ids = sorted(set(ids))[:MARK_MAX]
    if not ids:
        return 0
    where, params = _role_scope(sub)
    cur = conn.execute(
        "UPDATE main.reminder AS r SET delivered_ts = ? "  # noqa: S608 — фрагмент `_role_scope` задан кодом
        f"WHERE r.id IN ({','.join('?' for _ in ids)}) AND r.status = 'fired' AND r.delivered_ts IS NULL "
        f"AND {where}",
        (now_ms, *ids, *params),
    )
    return cur.rowcount


# --- выдача (решения 5–6, П9, П10) -------------------------------------------------------------------------


def new_since_done(conn: sqlite3.Connection, rec: Rec) -> int:
    """Ф89-1 (A.4, П10): сообщения беседы после прошлого «сделано» (нет — после постановки): чужие, не
    служебные, не скрытые, не удалённые, дошедшие до источника (как `_base_ts`)."""
    schema = db_of(rec.channel_id)
    since = rec.last_done_ts if rec.last_done_ts is not None else rec.created_ts
    return conn.execute(
        f"SELECT COUNT(*) FROM {schema}.message WHERE channel_id = ? AND ts > ? "  # noqa: S608
        "AND is_me = 0 AND kind <> 'event' AND deleted = 0 AND hidden = 0 AND ext_id IS NOT NULL",
        (rec.channel_id, since),
    ).fetchone()[0]


def reminder_dict(conn: sqlite3.Connection, rec: Rec, principal, *, counts: bool = False) -> dict:
    """Строка API (`/reminders`, `/sync`, главная). П10: название беседы и заметка — только если беседа видна
    принципалу по её НЫНЕШНЕМУ проекту; иначе `channel: null`, `note: null` (номер беседы остаётся).
    `by` — кто поставил: `me` (владелец в UI) или `session` (тогда `role` — роль или `null`). `waiting` —
    сработало, а сессия ещё не прочла.

    Ф89-1: без беседы — `channel_id: null`, `channel: null`, заметка — по видимости проекта напоминания;
    период (`every`, `every_text`, `at_hm`, `tz`) и учёт «сделано» (`missed`, `done_count`, `last_done_ts`,
    `last_done_role`). `counts` (только `GET /reminders?counts=1` — не `/sync`, П10): `new_since_done` —
    сообщений в беседе с прошлого «сделано» (беседа видна принципалу; иначе и без беседы — `null`)."""
    no_chan = rec.channel_id == NO_CHANNEL
    chan = None if no_chan else _chan(conn, rec.channel_id)
    if no_chan:
        seen = _visible(principal, rec.project_id)
    else:
        seen = chan is not None and _visible(principal, chan.project_id)
    by_session = rec.api_token_id is not None
    tz = zone(rec.tz) if rec.tz else None
    out = {
        "id": rec.id,
        "channel_id": None if no_chan else rec.channel_id,
        "project_id": rec.project_id,
        "due_ts": rec.due_ts,
        "note": rec.note if seen else None,
        "cancel_if_reply": bool(rec.cancel_if_reply),
        "status": rec.status,
        "by": "session" if by_session else "me",
        "role": rec.role,
        "role_no": rec.role_no,
        "created_ts": rec.created_ts,
        "updated_ts": rec.updated_ts,
        "fired_ts": rec.fired_ts,
        "delivered_ts": rec.delivered_ts,
        "closed_ts": rec.closed_ts,
        "end_reason": rec.end_reason,
        "waiting": by_session and rec.status == "fired" and rec.delivered_ts is None,
        "seq": rec.seq,
        "channel": (
            {
                "id": chan.id,
                "title": chan.title,
                "project_id": chan.project_id,
                "archived": bool(chan.archived),
            }
            if seen and chan is not None
            else None
        ),
        "every": rec.every,
        "every_text": every_text(rec.every, rec.at_hm, tz, rec.due_ts, rec.at_wd),
        "at_hm": rec.at_hm,
        "tz": rec.tz,
        "missed": rec.missed,
        "done_count": rec.done_count,
        "last_done_ts": rec.last_done_ts,
        "last_done_role": rec.last_done_role,
    }
    if counts:
        out["new_since_done"] = new_since_done(conn, rec) if seen and chan is not None else None
    return out


def _scope(principal, where: list[str], params: list) -> bool:
    """Скоуп проектов принципала в `where` (напоминания без проекта — только без списка проектов); `False`
    — ему не видно ничего."""
    if principal.project_ids is None:
        return True
    ids = sorted(principal.project_ids)
    if not ids:
        return False
    where.append(f"r.project_id IN ({','.join('?' for _ in ids)})")
    params += ids
    return True


_ORDER = "ORDER BY CASE r.status WHEN 'fired' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, r.due_ts, r.id"


def list_reminders(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    project_id: int | None = None,
    channel_id: int | None = None,
    include_closed: bool = False,
    sub=None,
    limit: int = LIST_MAX,
    counts: bool = False,
) -> list[dict]:
    """`GET /reminders`: открытые (`fired`, затем `pending` по сроку); `include_closed` — ещё и закрытые за 7
    дней. Без `sub` — всё видимое принципалу по проекту напоминания; `sub` — только напоминания роли этой
    подписки (`hub_reminders`)."""
    where = ["1 = 1"]
    params: list = []
    if not _scope(principal, where, params):
        return []
    if sub is not None:
        scope, scope_params = _role_scope(sub)
        where.append(scope)
        params += scope_params
    if project_id is not None:
        where.append("r.project_id = ?")
        params.append(project_id)
    if channel_id is not None:
        where.append("r.channel_id = ?")
        params.append(channel_id)
    if include_closed:
        where.append("(r.status IN ('pending', 'fired') OR r.closed_ts > ?)")
        params.append(now_ms - LIST_RECENT_MS)
    else:
        where.append("r.status IN ('pending', 'fired')")
    limit = max(1, min(int(limit), LIST_MAX))
    rows = _select(conn, " AND ".join(where) + f" {_ORDER} LIMIT ?", [*params, limit])
    return [reminder_dict(conn, rec, principal, counts=counts) for rec in rows]


def stale_for(conn: sqlite3.Connection, principal) -> bool:
    """Читатель перед выдачей (П6): среди сработавших, видимых принципалу, есть с опоздавшим ответом."""
    where = ["1 = 1"]
    params: list = []
    if not _scope(principal, where, params):
        return False
    return stale(conn, " AND ".join(where), params)


def home_items(conn: sqlite3.Connection, principal) -> list[dict]:
    """Решение 6: сработавшие и не закрытые напоминания в скоупе принципала — строки «Нужна реакция». Ф89-1
    (головная): повторяемое — только когда сессия пропустила срок (`missed >= 1`), как карточка очереди и
    уведомление; свой срок она ведёт сама."""
    # Ревью Ф89-1: своё повторяемое владельца (из UI, `api_token_id IS NULL`) сигналить некому — видно сразу.
    where = ["r.status = 'fired' AND (r.every IS NULL OR r.missed >= 1 OR r.api_token_id IS NULL)"]
    params: list = []
    if not _scope(principal, where, params):
        return []
    rows = _select(conn, " AND ".join(where) + " ORDER BY r.fired_ts DESC, r.id DESC", params)
    return [reminder_dict(conn, rec, principal) for rec in rows]


def changes(conn: sqlite3.Connection, principal, *, since: int, watermark: int, limit: int) -> list[dict]:
    """П9: напоминания с `since < seq <= watermark` в скоупе принципала, по возрастанию `seq` (`/sync`,
    SSE)."""
    where = ["r.seq > ? AND r.seq <= ?"]
    params: list = [since, watermark]
    if not _scope(principal, where, params):
        return []
    rows = _select(conn, " AND ".join(where) + " ORDER BY r.seq LIMIT ?", [*params, limit])
    return [reminder_dict(conn, rec, principal) for rec in rows]


# --- уведомление владельцу (решение 6, П5) ----------------------------------------------------------------------


def due_notify(conn: sqlite3.Connection, principal, *, now_ms: int) -> list[dict]:
    """`GET /agent/reminders/due`: сработавшие без `notified_ts`, которым пора на экран владельцу, — свои и
    безрольные сразу, с ролью — через `NOTIFY_ROLE_DELAY_MS` после срабатывания (сначала сессия) и только
    пока не закрыты. Заметка и название — по видимости принципала (П10)."""
    # Ф89-1 (П9): повторяемое — только когда сессия пропустила срок (`missed >= 1`), один раз на срок; своё
    # повторяемое владельца (`api_token_id IS NULL`, сигналить некому) — в каждый срок (ревью Ф89-1).
    where = [
        "r.status = 'fired' AND r.notified_ts IS NULL AND (r.role_key IS NULL OR r.fired_ts <= ?) "
        "AND (r.every IS NULL OR r.missed >= 1 OR r.api_token_id IS NULL)"
    ]
    params: list = [now_ms - NOTIFY_ROLE_DELAY_MS]
    if not _scope(principal, where, params):
        return []
    rows = _select(conn, " AND ".join(where) + " ORDER BY r.fired_ts, r.id LIMIT ?", [*params, LIST_MAX])
    return [reminder_dict(conn, rec, principal) for rec in rows]


def mark_notified(conn: sqlite3.Connection, ids: list[int], principal, *, now_ms: int) -> int:
    """Отметка `notified_ts` (уведомление ушло — один раз). Закрытые, уже отмеченные и вне скоупа — мимо."""
    ids = sorted(set(ids))[:MARK_MAX]
    where = [f"r.id IN ({','.join('?' for _ in ids)}) AND r.status = 'fired' AND r.notified_ts IS NULL"]
    params: list = list(ids)
    if not ids or not _scope(principal, where, params):
        return 0
    cur = conn.execute(
        f"UPDATE main.reminder AS r SET notified_ts = ? WHERE {' AND '.join(where)}",  # noqa: S608
        (now_ms, *params),
    )
    return cur.rowcount


# --- слияние и миграция беседы (П7) ------------------------------------------------------------------------


def on_channel_migrate(conn: sqlite3.Connection, old_id: int, new_id: int, *, now_ms: int) -> int:
    """П7: беседа `old_id` вливается в `new_id` (заготовка -> настоящая беседа, `channel_migrate`) — её
    открытые напоминания переезжают на новую (старая уходит в архив, а архив сам по себе — `cancelled`).
    Возвращает число перенесённых."""
    rows = _select(conn, "r.channel_id = ? AND r.status IN ('pending', 'fired') ORDER BY r.id", (old_id,))
    for rec in rows:
        # Ревью Ф34-2: точка отсчёта «ответил» — не раньше последнего сообщения новой беседы на момент
        # переезда, иначе чужое сообщение, лежавшее там до слияния, молча сняло бы напоминание.
        base = max(rec.base_message_ts, _base_ts(conn, new_id, rec.base_message_ts))
        _update(conn, rec.id, now_ms, channel_id=new_id, base_message_ts=base)
    return len(rows)
