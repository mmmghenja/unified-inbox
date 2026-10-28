"""Ф41-6 (docs/architecture.md, пункты 2–4): расход сессий хаба за сегодня, линии роли
«не в сети» («Забрать в хаб» убрано в Ф33-0).

Линия — `(проект, роль, номер)` настоящего проекта; подписка без роли и агент вне проектов линии не дают
(как `peer_msgs._is_line`). Модуль ничего не отправляет и подписок не трогает: ждущее линии переедет новой
подписке обычным порядком (Ф32-1, Ф36-1), когда сессия хаба подпишется сама."""

import time

from . import engine as engine_mod
from . import held as held_mod
from . import peer_msgs
from . import subs as subs_mod

OFFLINE_MS = 7 * 24 * 60 * 60 * 1000  # линия «не в сети» видна, пока последняя подписка кончилась не раньше
TODAY_CACHE_MS = 30_000


# --- расход за сегодня (п.2) ------------------------------------------------------------------------------


def day_start_ms(now_ms: int) -> int:
    """Местная полночь суток, в которые попадает `now_ms`."""
    local = time.localtime(now_ms / 1000)
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))) * 1000


def today(conn, *, now_ms: int) -> dict:
    """`{"cost_usd", "turns"}` — ходы сессий хаба (и закрытых) с местной полуночи: события `turn_end` обоих
    хранилищ, цена хода — `brief.cost_usd` события (41-3а); ход без цены считается в `turns`. Смотрятся
    только сессии, менявшиеся сегодня (конец хода двигает `updated_ts`)."""
    since = day_start_ms(now_ms)
    ids = [row[0] for row in conn.execute("SELECT id FROM main.hub_session WHERE updated_ts >= ?", (since,))]
    cost, turns = 0.0, 0
    if ids:
        marks = ",".join("?" for _ in ids)
        for schema in held_mod.STORES:
            row = conn.execute(
                f"SELECT COUNT(*), SUM(json_extract(detail_json, '$.brief.cost_usd')) "  # noqa: S608
                f"FROM {schema}.held_event WHERE session_id IN ({marks}) AND kind = 'turn_end' AND ts >= ?",
                [*ids, since],
            ).fetchone()
            turns += row[0] or 0
            cost += float(row[1] or 0)
    return {"cost_usd": round(cost, 6), "turns": turns}


class TodayCache:
    """Кэш `today` в памяти ядра: считается не чаще раза в `TODAY_CACHE_MS` и заново — со сменой суток."""

    def __init__(self) -> None:
        self._at: int | None = None
        self._day: int | None = None
        self._value: dict | None = None

    def get(self, now_ms: int) -> dict | None:
        if self._value is None or self._at is None or self._day != day_start_ms(now_ms):
            return None
        if not 0 <= now_ms - self._at < TODAY_CACHE_MS:
            return None
        return dict(self._value)

    def put(self, now_ms: int, value: dict) -> None:
        self._at, self._day, self._value = now_ms, day_start_ms(now_ms), dict(value)


# --- линии «не в сети» (п.3) ------------------------------------------------------------------------------


def line_key(project_id: int, role_key: str, role_no: int) -> str:
    """Ключ линии — тот же, что у диалога агента в интерфейсе (`lineKey`): `p<проект>:<роль>:<номер>`."""
    return f"p{project_id}:{role_key}:{role_no}"


def _last_subs(conn, where: str, params: list) -> list[subs_mod.SubRec]:
    """Последняя (по номеру) подписка каждой линии роли настоящего проекта."""
    rows = conn.execute(
        f"SELECT {subs_mod._SUB_COLUMNS} FROM main.session_sub WHERE id IN ("  # noqa: S608 — фрагменты кода
        "SELECT MAX(id) FROM main.session_sub WHERE role_key IS NOT NULL AND role_no IS NOT NULL "
        f"AND project_id != ? AND {where} GROUP BY project_id, role_key, role_no) ORDER BY id DESC",
        [subs_mod.NO_PROJECT_ID, *params],
    ).fetchall()
    return [subs_mod.SubRec(*row) for row in rows]


def _line_open(conn, rec: subs_mod.SubRec) -> bool:
    """На линии подписки есть незавершённая подписка (мёртвую снимает чистка ядра раз в минуту)."""
    row = conn.execute(
        "SELECT 1 FROM main.session_sub WHERE project_id = ? AND role_key = ? AND role_no = ? "
        "AND ended_ts IS NULL LIMIT 1",
        (rec.project_id, rec.role_key, rec.role_no),
    ).fetchone()
    return row is not None


def _engine(conn, rec: subs_mod.SubRec) -> str:
    hub = held_mod.of_rec(conn, rec)
    return hub.engine if hub is not None else engine_mod.engine_of_session(None, rec.session_id)


def _pending(conn, rec: subs_mod.SubRec) -> int:
    """Ждущее линии: сигналы последней подписки и непрочитанные сообщения агентов, адресованные линии."""
    signals = subs_mod._pending_counts(conn, [rec.id]).get(rec.id, 0)
    return signals + peer_msgs.unread_count(conn, rec)


def offline_lines(conn, *, project_ids: frozenset[int] | None, now_ms: int) -> list[dict]:
    """`GET /subs?offline=1`: по строке на линию роли без незавершённой подписки и без незакрытой сессии
    хаба, чья последняя подписка кончилась не раньше `OFFLINE_MS` назад. Новые первыми. Ничего не пишет."""
    where, params = "1=1", []
    if project_ids is not None:
        ids = sorted(project_ids)
        if not ids:
            return []
        where = f"project_id IN ({','.join('?' for _ in ids)})"
        params = ids
    held_lines: dict[int, dict] = {}
    lines: list[dict] = []
    for rec in _last_subs(conn, where, params):
        if rec.ended_ts is None or rec.ended_ts < now_ms - OFFLINE_MS or _line_open(conn, rec):
            continue
        if rec.end_reason == "ended_by_owner" and not held_mod.is_held(rec.session_pid):
            # Владелец 14.10: «Отписать от хаба» у внешней сессии убирает её из списка совсем, а не в «не в сети»
            # (у закрытой сессии хаба та же причина — её линия «не в сети» остаётся).
            continue
        if rec.project_id not in held_lines:
            held_lines[rec.project_id] = held_mod.role_lines(conn, rec.project_id)
        if (rec.role_key, rec.role_no) in held_lines[rec.project_id]:
            continue
        lines.append(
            {
                "key": line_key(rec.project_id, rec.role_key, rec.role_no),
                "project_id": rec.project_id,
                "role": rec.role,
                "role_no": rec.role_no,
                "engine": _engine(conn, rec),
                "session_name": rec.session_name,
                "cwd": rec.session_cwd,
                "ended_ts": rec.ended_ts,
                "end_reason": rec.end_reason,
                "pending": _pending(conn, rec),
                "sub_id": rec.id,
            }
        )
    return lines
