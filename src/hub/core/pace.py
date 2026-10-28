"""График отправки черновиков «по графику» (Ф12-2, `docs/architecture.md`–3 и
поправки П1, П2 — они главнее текста выше них).

Холодные первые сообщения и повторные касания не должны уходить пачкой: Telegram даёт личному аккаунту
спам-ограничение, Gmail — подозрение в рассылке. Подтверждение владельца ставит такой черновик в СЛОТ аккаунта, а
`outbox.lease` перед выдачей ещё раз сверяет график (П2): после сна ноутбука, выключенного коннектора или
`flood` просроченное уходит по одному в окне, а не пачкой ночью.

Слот (всё — по данным в БД, переживает рестарт; случайность — параметром `rng`, тесты её подменяют):
- окно — локальное время `pace_tz` (умолчание `Europe/Moscow`), `pace_window` (умолчание `10:00-19:00`);
- промежуток после предыдущего слота аккаунта — случайный в `[pace_gap_min_s, pace_gap_max_s]` (240–720 с);
  предыдущий — максимум слота (`pace_slot_ts`) и `sent_ts` строк графика аккаунта в `queued|sending|sent|
  unknown`; строка, которую коннектор отправляет прямо сейчас (`sending`), считается «сейчас»; предыдущего
  нет или он давно — случайная пауза `[0, pace_gap_min_s]` от «сейчас», не мгновенно;
- суточный лимит `pace_daily` (Telegram 20, почта 15) — строки графика аккаунта в том же локальном дне:
  отправленные — по `sent_ts`, прочие (`queued|sending|unknown`) — по слоту; исчерпан -> следующий день;
- вне окна -> начало следующего окна + случайно `[0, pace_gap_max_s]`.
Настройки — ключи `config_json` аккаунта, читаются при каждом обращении (перезапуск не нужен); кривое
значение -> умолчание + предупреждение в журнал (один раз на значение).

Прогрев лимита (Ф30-1, решение 6 и П9, `docs/architecture.md`): ключи `pace_warmup_from`
(`YYYY-MM-DD`), `pace_warmup_start` (1…`pace_daily`), `pace_warmup_days` (1…120) — лимит дня растёт от `start`
до `pace_daily` за `days` дней (`Settings.daily_for`). Без `pace_warmup_from` — `pace_daily` как раньше.

Вызывать только внутри транзакции (`Db.write()`/`db.reader()`); время — параметром `now_ms`.
"""

import datetime as dt
import json
import logging
import re
import zoneinfo
from typing import NamedTuple, Protocol

_logger = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"
DEFAULT_WINDOW = "10:00-19:00"
DEFAULT_GAP_MIN_S = 240
DEFAULT_GAP_MAX_S = 720
DEFAULT_DAILY = {"telegram": 20, "mail": 15}
DEFAULT_DAILY_OTHER = 15
GAP_MAX_LIMIT_S = 24 * 60 * 60
DAILY_MAX = 1000
# Ф30-1 П9: прогрев с кривым значением — в осторожную сторону (меньше лимит, дольше разгон).
WARMUP_START_DEFAULT = 5
WARMUP_DAYS_DEFAULT = 14
WARMUP_DAYS_MAX = 120
# Статусы, которые занимают слот графика (решение 3): подтверждённые и уже ушедшие/уходящие.
SLOT_STATUSES = ("queued", "sending", "sent", "unknown")
_MAX_DAYS_AHEAD = 400  # страховка цикла подбора дня: лимит ≥ 1, так что свободный день находится сразу

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
_DATE_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")
_warned: set[tuple] = set()


class Rng(Protocol):
    def randint(self, a: int, b: int) -> int: ...


class Warmup(NamedTuple):
    """Ф30-1 решение 6: прогрев суточного лимита — с `start` в день `since` до `daily` через `days` дней."""

    since: dt.date
    start: int
    days: int


class Settings(NamedTuple):
    tz: dt.tzinfo
    start_min: int  # начало окна, минуты от полуночи
    end_min: int  # конец окна (не включительно), минуты от полуночи; 1440 — до полуночи
    gap_min_s: int
    gap_max_s: int
    daily: int  # `pace_daily` — целевой лимит; лимит конкретного дня — `daily_for(day)` (прогрев)
    warmup: Warmup | None = None

    def daily_for(self, day: dt.date) -> int:
        """Лимит локального дня `day`: до `since` — `start`; дальше `start + floor((daily − start) ×
        min(дней, days) / days)`; без прогрева — `daily`."""
        w = self.warmup
        if w is None:
            return self.daily
        if day < w.since:
            return w.start
        passed = min((day - w.since).days, w.days)
        return w.start + (self.daily - w.start) * passed // w.days

    def warmup_active(self, day: dt.date) -> bool:
        """Прогрев задан и в день `day` ещё не дошёл до `daily`."""
        return self.warmup is not None and self.daily_for(day) < self.daily


def _warn(account_id: int, key: str, value: object) -> None:
    marker = (account_id, key, repr(value))
    if marker in _warned:
        return
    _warned.add(marker)
    _logger.warning("аккаунт %s: %s=%r не годится — беру умолчание графика отправки", account_id, key, value)


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_window(value: object) -> tuple[int, int] | None:
    """`"ЧЧ:ММ-ЧЧ:ММ"` -> `(начало, конец)` в минутах; конец позже начала, не позже `24:00`. Иначе `None`."""
    if not isinstance(value, str):
        return None
    match = _WINDOW_RE.match(value)
    if match is None:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if m1 > 59 or m2 > 59 or h1 > 23 or h2 > 24 or (h2 == 24 and m2 != 0):
        return None
    start, end = h1 * 60 + m1, h2 * 60 + m2
    return (start, end) if start < end else None


def _zone(name: object) -> dt.tzinfo | None:
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return zoneinfo.ZoneInfo(name.strip())
    except zoneinfo.ZoneInfoNotFoundError, ValueError, OSError:
        return None


def _config(conn, account_id: int) -> tuple[str | None, dict]:
    row = conn.execute("SELECT source, config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None, {}
    source, config_json = row
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    return source, config if isinstance(config, dict) else {}


def settings_for(conn, account_id: int) -> Settings:
    """Настройки графика аккаунта: ключи `pace_*` его `config_json` поверх умолчаний."""
    source, config = _config(conn, account_id)

    tz_name = config.get("pace_tz", DEFAULT_TZ)
    tz = _zone(tz_name)
    if tz is None:
        _warn(account_id, "pace_tz", tz_name)
        tz = zoneinfo.ZoneInfo(DEFAULT_TZ)

    window = parse_window(config.get("pace_window", DEFAULT_WINDOW))
    if window is None:
        _warn(account_id, "pace_window", config.get("pace_window"))
        window = parse_window(DEFAULT_WINDOW)
    assert window is not None  # noqa: S101 — умолчание разбирается всегда

    gap_min = _int_value(config.get("pace_gap_min_s", DEFAULT_GAP_MIN_S))
    gap_max = _int_value(config.get("pace_gap_max_s", DEFAULT_GAP_MAX_S))
    # Промежуток не нулевой: иначе новый слот после отказа в выдаче совпал бы с «сейчас», строка крутилась.
    if gap_min is None or gap_max is None or not 1 <= gap_min <= gap_max <= GAP_MAX_LIMIT_S:
        if "pace_gap_min_s" in config or "pace_gap_max_s" in config:
            _warn(account_id, "pace_gap_min_s/pace_gap_max_s", (config.get("pace_gap_min_s"),
                                                                 config.get("pace_gap_max_s")))  # fmt: skip
        gap_min, gap_max = DEFAULT_GAP_MIN_S, DEFAULT_GAP_MAX_S

    default_daily = DEFAULT_DAILY.get(source or "", DEFAULT_DAILY_OTHER)
    daily = _int_value(config.get("pace_daily", default_daily))
    if daily is None or not 1 <= daily <= DAILY_MAX:
        _warn(account_id, "pace_daily", config.get("pace_daily"))
        daily = default_daily

    return Settings(tz, window[0], window[1], gap_min, gap_max, daily, _warmup(account_id, config, daily))


def _warmup(account_id: int, config: dict, daily: int) -> Warmup | None:
    """Ф30-1 решение 6 + П9: ключи прогрева. Кривой `from` — прогрев выключен (без даты отсчёта не посчитать);
    кривой `start` — `min(5, daily)`, больше `daily` — обрезается до `daily`; кривой `days` — 14."""
    if "pace_warmup_from" not in config:
        return None
    raw_from = config.get("pace_warmup_from")
    since = None
    if isinstance(raw_from, str) and _DATE_RE.match(raw_from):
        try:
            since = dt.date.fromisoformat(raw_from.strip())
        except ValueError:
            since = None
    if since is None:
        _warn(account_id, "pace_warmup_from", raw_from)
        return None

    start = _int_value(config.get("pace_warmup_start", WARMUP_START_DEFAULT))
    if start is None or start < 1:
        _warn(account_id, "pace_warmup_start", config.get("pace_warmup_start"))
        start = min(WARMUP_START_DEFAULT, daily)
    start = min(start, daily)

    days = _int_value(config.get("pace_warmup_days", WARMUP_DAYS_DEFAULT))
    if days is None or not 1 <= days <= WARMUP_DAYS_MAX:
        _warn(account_id, "pace_warmup_days", config.get("pace_warmup_days"))
        days = WARMUP_DAYS_DEFAULT
    return Warmup(since, start, days)


def _sql(schema: str, template: str) -> str:
    return template.format(schema=schema)


def _schema_of(conn, account_id: int) -> str:
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else "main"


def _local(ts_ms: int, s: Settings) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts_ms / 1000, s.tz)


def _at(day: dt.date, minutes: int, s: Settings) -> int:
    """Локальные `day` + `minutes` от полуночи -> мс эпохи (`1440` — полночь следующего дня)."""
    # Настенное время дня + минуты, затем пояс: переход часов внутри суток не сдвигает границу окна.
    naive = dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(minutes=minutes)
    moment = naive.replace(tzinfo=s.tz)
    return int(moment.timestamp() * 1000)


def _day_bounds(day: dt.date, s: Settings) -> tuple[int, int]:
    return _at(day, 0, s), _at(day + dt.timedelta(days=1), 0, s)


def in_window(ts_ms: int, s: Settings) -> bool:
    day = _local(ts_ms, s).date()
    return _at(day, s.start_min, s) <= ts_ms < _at(day, s.end_min, s)


def day_count(
    conn, schema: str, account_id: int, day: dt.date, s: Settings, *, exclude_id: int | None
) -> int:
    """Строки графика аккаунта в локальном дне `day`: отправленные — по `sent_ts`, прочие — по слоту (П2)."""
    start, end = _day_bounds(day, s)
    return conn.execute(
        _sql(
            schema,
            "SELECT COUNT(*) FROM {schema}.outbox WHERE account_id = ? AND paced = 1 AND id <> ? AND ("
            "(status = 'sent' AND sent_ts >= ? AND sent_ts < ?) OR "
            "(status IN ('queued', 'sending', 'unknown') AND pace_slot_ts >= ? AND pace_slot_ts < ?))",
        ),
        (account_id, exclude_id if exclude_id is not None else -1, start, end, start, end),
    ).fetchone()[0]


def _previous(conn, schema: str, account_id: int, now_ms: int, exclude_id: int | None) -> int | None:
    """Предыдущий слот аккаунта: максимум слота и `sent_ts` строк графика; `sending` — «сейчас»."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT MAX(CASE WHEN status = 'sending' THEN MAX(?, COALESCE(pace_slot_ts, 0)) "
            "ELSE MAX(COALESCE(pace_slot_ts, 0), COALESCE(sent_ts, 0)) END) "
            "FROM {schema}.outbox WHERE account_id = ? AND paced = 1 AND id <> ? "
            "AND status IN ('queued', 'sending', 'sent', 'unknown')",
        ),
        (now_ms, account_id, exclude_id if exclude_id is not None else -1),
    ).fetchone()
    value = row[0] if row is not None else None
    return value if value else None


def _window_start(day: dt.date, s: Settings, rng: Rng) -> int:
    """Начало окна дня + случайно `[0, gap_max]`, но внутри окна."""
    start, end = _at(day, s.start_min, s), _at(day, s.end_min, s)
    offset_ms = rng.randint(0, s.gap_max_s) * 1000
    return start + min(offset_ms, max(end - start - 1000, 0))


def next_slot(
    conn,
    account_id: int,
    now_ms: int,
    rng: Rng,
    *,
    exclude_id: int | None = None,
    settings: Settings | None = None,
    after_ms: int | None = None,
) -> int:
    """Решение 3: следующий слот аккаунта не раньше `now_ms`. `exclude_id` — строка, которой слот ищется
    заново (`lease`, П2): её собственный прежний слот не считается ни предыдущим, ни занятым. `after_ms` —
    отсчёт промежутка от этого момента, а не от самого позднего слота аккаунта (ревью Ф12-2, находка 2:
    строка, упёршаяся в «промежуток» или «уже отправляется», не уезжает за чужие слоты — даже завтрашние)."""
    s = settings or settings_for(conn, account_id)
    schema = _schema_of(conn, account_id)
    prev = after_ms if after_ms is not None else _previous(conn, schema, account_id, now_ms, exclude_id)
    candidate = None if prev is None else prev + rng.randint(s.gap_min_s, s.gap_max_s) * 1000
    if candidate is None or candidate <= now_ms:
        # Слова владельца («паузы недетерминированные должны быть везде»): предыдущего слота нет или он
        # давно — не мгновенно, а через случайную паузу `[0, gap_min]` от «сейчас» (дальше — окно и лимит).
        candidate = now_ms + rng.randint(0, s.gap_min_s) * 1000
    for _ in range(_MAX_DAYS_AHEAD):
        day = _local(candidate, s).date()
        window_start, window_end = _at(day, s.start_min, s), _at(day, s.end_min, s)
        if candidate < window_start:
            candidate = _window_start(day, s, rng)
        if candidate >= window_end:
            candidate = _window_start(day + dt.timedelta(days=1), s, rng)
            continue
        if day_count(conn, schema, account_id, day, s, exclude_id=exclude_id) >= s.daily_for(day):
            candidate = _window_start(day + dt.timedelta(days=1), s, rng)
            continue
        return candidate
    return candidate


def blocked_reason(
    conn, account_id: int, outbox_id: int, now_ms: int, *, settings: Settings | None = None
) -> str | None:
    """П2: можно ли выдать строку графика СЕЙЧАС. `None` — можно; иначе причина (для аудита): вне окна, другая
    строка графика аккаунта уже отправляется, с последней отправки прошло меньше `pace_gap_min_s`, суточный
    лимит этого дня исчерпан."""
    s = settings or settings_for(conn, account_id)
    schema = _schema_of(conn, account_id)
    if not in_window(now_ms, s):
        return "window"
    sending = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.outbox WHERE account_id = ? AND paced = 1 AND id <> ? "
            "AND status = 'sending' LIMIT 1",
        ),
        (account_id, outbox_id),
    ).fetchone()
    if sending is not None:
        return "in_flight"
    last_sent = conn.execute(
        _sql(
            schema,
            "SELECT MAX(sent_ts) FROM {schema}.outbox WHERE account_id = ? AND paced = 1 AND id <> ? "
            "AND status = 'sent'",
        ),
        (account_id, outbox_id),
    ).fetchone()[0]
    if last_sent is not None and now_ms - last_sent < s.gap_min_s * 1000:
        return "gap"
    return _daily_reason(conn, schema, account_id, outbox_id, now_ms, s)


def last_sent_ts(conn, account_id: int, outbox_id: int) -> int | None:
    """Последняя отправка строки графика аккаунта (кроме `outbox_id`)."""
    schema = _schema_of(conn, account_id)
    return conn.execute(
        _sql(
            schema,
            "SELECT MAX(sent_ts) FROM {schema}.outbox WHERE account_id = ? AND paced = 1 AND id <> ? "
            "AND status = 'sent'",
        ),
        (account_id, outbox_id),
    ).fetchone()[0]


def window_or_daily_reason(
    conn, account_id: int, outbox_id: int, now_ms: int, *, settings: Settings | None = None
) -> str | None:
    """Ревью Ф12-2, находка 3: для повторной выдачи строки с истёкшим лизом — только окно и лимит дня."""
    s = settings or settings_for(conn, account_id)
    if not in_window(now_ms, s):
        return "window"
    return _daily_reason(conn, _schema_of(conn, account_id), account_id, outbox_id, now_ms, s)


def _daily_reason(conn, schema: str, account_id: int, outbox_id: int, now_ms: int, s: Settings) -> str | None:
    day = _local(now_ms, s).date()
    if day_count(conn, schema, account_id, day, s, exclude_id=outbox_id) >= s.daily_for(day):
        return "daily"
    return None


def today(conn, account_id: int, now_ms: int, *, settings: Settings | None = None) -> tuple[int, int]:
    """A5: `(paced_today, pace_daily)` — строк графика аккаунта на сегодня (локальный день) и лимит
    сегодняшнего дня (Ф30-1 A5: с прогревом — его значение на сегодня)."""
    s = settings or settings_for(conn, account_id)
    schema = _schema_of(conn, account_id)
    day = _local(now_ms, s).date()
    return day_count(conn, schema, account_id, day, s, exclude_id=None), s.daily_for(day)


def warmup_status(conn, account_id: int, now_ms: int, *, settings: Settings | None = None) -> dict | None:
    """Ф30-1 A5: при активном прогреве (сегодняшний лимит ещё ниже `pace_daily`) — `{from, start, days,
    target}` для `/status`; иначе `None`."""
    s = settings or settings_for(conn, account_id)
    if s.warmup is None or not s.warmup_active(_local(now_ms, s).date()):
        return None
    w = s.warmup
    return {"from": w.since.isoformat(), "start": w.start, "days": w.days, "target": s.daily}


SEND_AT_MIN_AHEAD_MS = 60 * 1000
SEND_AT_MAX_AHEAD_MS = 30 * 24 * 60 * 60 * 1000
_SEND_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?$")


class SendAtInvalid(ValueError):
    """Ф30-1 решение 5: время `send_at` не разобрать или оно вне `[now + 1 мин, now + 30 дней]`; текст — для
    ответа 422."""


def parse_send_at(value: object, tz: dt.tzinfo, now_ms: int) -> int:
    """Ф30-1 решение 5: `"YYYY-MM-DD HH:MM"` — время в поясе аккаунта `tz` (`pace_tz`); ISO со смещением
    (`…T10:00+03:00`, `…Z`) — как есть. Не раньше `now + 1 мин`, не позже `now + 30 дней`. -> мс UTC."""
    text = value.strip() if isinstance(value, str) else ""
    if not _SEND_AT_RE.match(text):
        raise SendAtInvalid(
            'send_at: нужно время вида "2026-10-05 10:00" (время аккаунта) или ISO со смещением'
        )
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise SendAtInvalid(f"send_at: нет такого времени ({text})") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    ts = int(moment.timestamp() * 1000)
    if ts < now_ms + SEND_AT_MIN_AHEAD_MS:
        raise SendAtInvalid("send_at: время уже прошло или меньше чем через минуту")
    if ts > now_ms + SEND_AT_MAX_AHEAD_MS:
        raise SendAtInvalid("send_at: не позже чем через 30 дней")
    return ts
