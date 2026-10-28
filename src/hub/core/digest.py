"""Сводка дня проекта и слова владельца из соседних сессий (Ф86-1, docs/architecture.md, C3,
C4 с поправками П1, П6, П9, П12–П14, П16).

В ядре LLM нет: сводку пишет внешний клиент (`day-digest.py` сисадмина, скоуп `digest`), ядро хранит, отдаёт
и подкладывает её сессиям хаба после сжатия (хост, `feed_extras`). Выключатели проекта — ключи
`agent_policy_json` `digest_after_compact` и `sibling_says`, по умолчанию оба включены (нет ключа — вкл).

Все функции записи — только внутри `Db.write()`, время — параметром `now_ms`."""

import json
import os
from datetime import datetime, timedelta

from hub import digest_text

from . import agent_runs, say_mask
from .audit import write_audit

DIGEST_KEY = "digest_after_compact"
SIBLINGS_KEY = "sibling_says"
STORES = ("main", "work")

TEXT_MAX = 6000
MODEL_MAX = 60
REASON_MAX = 500
WINDOW_MAX_MS = 48 * 60 * 60 * 1000  # `GET /digest/sources`: окно не длиннее 48 ч
SAYS_MAX = 500  # реплик на проект в `sources` (обрезка — самые старые)
SIBLINGS_BACK_MS = 24 * 60 * 60 * 1000  # П1: старше 24 ч соседние реплики не подаются
SIBLINGS_ROWS_MAX = 200  # строк-кандидатов за одну подачу (дальше — следующей подачей)


class DigestError(Exception):
    """Отказ: `status` — HTTP-код (404/422), `detail` — текст."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- выключатели проекта (C4) -------------------------------------------------------------------------------


def _policy(policy_json: str | None) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def enabled(policy_json: str | None, key: str) -> bool:
    """Нет ключа или кривой — включено; выключено только явным `false`."""
    return _policy(policy_json).get(key) is not False


def fields(policy_json: str | None) -> dict:
    """Поля строки проекта (`GET /projects`, `hub project list`)."""
    return {DIGEST_KEY: enabled(policy_json, DIGEST_KEY), SIBLINGS_KEY: enabled(policy_json, SIBLINGS_KEY)}


def _policy_json(conn, project_id: int) -> tuple[bool, str | None]:
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return (row is not None), (row[0] if row is not None else None)


def project_on(conn, project_id: int, key: str) -> bool:
    found, raw = _policy_json(conn, project_id)
    return found and enabled(raw, key)


def set_project(conn, project_id: int, key: str, on: bool) -> bool | None:
    """Ключ политики проекта (прочие ключи не трогаются). Нет проекта — `None`; иначе — было ли изменение."""
    if key not in (DIGEST_KEY, SIBLINGS_KEY):
        raise ValueError(key)
    found, raw = _policy_json(conn, project_id)
    if not found:
        return None
    policy = _policy(raw)
    before = policy.get(key) is not False
    policy[key] = bool(on)
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    return before != bool(on)


# --- дни ----------------------------------------------------------------------------------------------------


def today(now_ms: int) -> str:
    return digest_text.day_of(now_ms)


def _yesterday(now_ms: int) -> str:
    local = datetime.fromtimestamp(now_ms / 1000, digest_text.TZ) - timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def check_day(day: str) -> str:
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise DigestError(422, "day: ГГГГ-ММ-ДД") from exc
    if parsed.strftime("%Y-%m-%d") != day:
        raise DigestError(422, "day: ГГГГ-ММ-ДД")
    return day


# --- сводка (C1.4, П9, П12) --------------------------------------------------------------------------------

_ROW = "project_id, day, text, model, upto_ts, created_ts, updated_ts"


def _row(raw) -> dict:
    project_id, day, text, model, upto_ts, created_ts, updated_ts = raw
    return {
        "project_id": project_id,
        "day": day,
        "text": text,
        "model": model,
        "upto_ts": upto_ts,
        "created_ts": created_ts,
        "updated_ts": updated_ts,
        "at": digest_text.hhmm(updated_ts),
    }


def get(conn, project_id: int, day: str) -> dict | None:
    """П12: строка сводки из обоих файлов — свежая по `updated_ts`; нет — `None`."""
    best = None
    for schema in STORES:
        raw = conn.execute(
            f"SELECT {_ROW} FROM {schema}.digest WHERE project_id = ? AND day = ?",  # noqa: S608
            (project_id, day),
        ).fetchone()
        if raw is not None and (best is None or raw[6] > best[6]):
            best = raw
    return _row(best) if best is not None else None


def _project_exists(conn, project_id: int) -> bool:
    return conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is not None


def put(
    conn,
    project_id: int,
    day: str,
    *,
    text: str,
    model: str | None,
    upto_ts: int | None,
    now_ms: int,
    audit: dict,
) -> dict:
    """C1.4: upsert сводки. День — только «сегодня» или «вчера» по времени хаба (иначе 422); текст непустой, не
    длиннее 6 000 (иначе 422); теги блоков в тексте обезвреживаются (П9). Пишется в файл
    `schema_for_project`, строка того же `(project_id, day)` из другого файла удаляется (П12). Нет проекта —
    404."""
    check_day(day)
    if day not in (today(now_ms), _yesterday(now_ms)):
        raise DigestError(422, "day: только сегодня или вчера по времени хаба")
    if not _project_exists(conn, project_id):
        raise DigestError(404, "not found")
    if not text.strip():
        raise DigestError(422, "text: пустая сводка")
    if len(text) > TEXT_MAX:
        raise DigestError(422, f"text: не длиннее {TEXT_MAX} символов")
    if model is not None and len(model) > MODEL_MAX:
        raise DigestError(422, f"model: не длиннее {MODEL_MAX} символов")
    text = digest_text.defang(text)
    schema = agent_runs.schema_for_project(conn, project_id)
    other = "work" if schema == "main" else "main"
    old = get(conn, project_id, day)
    conn.execute(f"DELETE FROM {other}.digest WHERE project_id = ? AND day = ?", (project_id, day))  # noqa: S608
    conn.execute(
        f"INSERT INTO {schema}.digest (project_id, day, text, model, upto_ts, created_ts, updated_ts) "  # noqa: S608
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (project_id, day) DO UPDATE SET text = excluded.text, "
        "model = excluded.model, upto_ts = excluded.upto_ts, updated_ts = excluded.updated_ts",
        (project_id, day, text, model, upto_ts, old["created_ts"] if old else now_ms, now_ms),
    )
    write_audit(
        conn,
        now_ms=now_ms,
        action="digest_put",
        result="updated" if old else "created",
        detail={"project_id": project_id, "day": day, "text_len": len(text)},
        **audit,
    )
    row = get(conn, project_id, day)
    assert row is not None  # noqa: S101 — строка этой транзакции
    return row


def turn_off(conn, project_id: int, *, reason: str, now_ms: int, audit: dict) -> bool:
    """C1.4 (B4 сисадмина): выключить «сводку после сжатия» у проекта. -> было ли изменение. Нет проекта —
    404; пустая причина или длиннее 500 — 422. Аудит `digest_off` (причина — в аудите)."""
    reason = " ".join(reason.split())
    if not reason:
        raise DigestError(422, "reason: нужна причина")
    if len(reason) > REASON_MAX:
        raise DigestError(422, f"reason: не длиннее {REASON_MAX} символов")
    changed = set_project(conn, project_id, DIGEST_KEY, False)
    if changed is None:
        raise DigestError(404, "not found")
    write_audit(
        conn,
        now_ms=now_ms,
        action="digest_off",
        result="ok" if changed else "already",
        detail={"project_id": project_id, "reason": reason},
        **audit,
    )
    return changed


def off_notice(conn, project_id: int, reason: str) -> tuple[str, str]:
    """Уведомление владельца о выключении (заголовок, текст)."""
    row = conn.execute("SELECT name FROM main.project WHERE id = ?", (project_id,)).fetchone()
    name = (row[0] if row else None) or f"#{project_id}"
    why = " ".join(reason.split())
    return "Хаб: сводка дня", f"Сводка дня выключена у проекта {project_id} «{name}»: {why}"


# --- реплики владельца (C1.4 sources, C3, П13, П14, П16) -------------------------------------------------------


def _sessions(conn, project_id: int, *, since_ts: int | None = None) -> list[dict]:
    """Сессии хаба проекта: незакрытые и (при `since_ts`) закрытые не раньше него; без `since_ts` — все."""
    where = "project_id = ?"
    params: list = [project_id]
    if since_ts is not None:
        where += " AND (closed_ts IS NULL OR closed_ts >= ?)"
        params.append(since_ts)
    rows = conn.execute(
        "SELECT id, role, engine, subdir, state, closed_ts, store FROM main.hub_session "  # noqa: S608
        f"WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    return [
        {
            "id": r[0],
            "role": r[1],
            "engine": r[2],
            "subdir": r[3],
            "status": "closed" if r[5] is not None else r[4],
            "store": r[6],
        }
        for r in rows
    ]


def _says(
    conn, sessions: list[dict], *, since_ts: int, until_ts: int | None, agent: bool, limit: int
) -> tuple[list[dict], bool]:
    """Реплики владельца (`typed = 1`; при `agent` — и ответы агента владельца `source = 'agent'`) этих сессий, не
    сброшенные, без подач интервью, `since_ts < created_ts <= until_ts`, по файлу сессии (П12). -> (самые
    новые `limit` по времени, обрезано ли)."""
    who = "(typed = 1 OR source = 'agent')" if agent else "typed = 1"
    roles = {s["id"]: s["role"] for s in sessions}
    rows: list[tuple] = []
    for schema in STORES:
        ids = [s["id"] for s in sessions if s["store"] == schema]
        if not ids:
            continue
        marks = ", ".join("?" for _ in ids)
        params: list = [*ids, since_ts]
        upper = ""
        if until_ts is not None:
            upper = " AND created_ts <= ?"
            params.append(until_ts)
        params.append(limit + 1)
        rows += conn.execute(
            f"SELECT id, session_id, created_ts, text, typed, source FROM {schema}.held_say "  # noqa: S608
            f"WHERE session_id IN ({marks}) AND status != 'dropped' AND interview_id IS NULL AND {who} "
            f"AND created_ts > ?{upper} ORDER BY created_ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    rows.sort(key=lambda r: (r[2], r[0]))
    truncated = len(rows) > limit
    rows = rows[-limit:] if truncated else rows
    out = [
        {
            "id": r[0],
            "session_id": r[1],
            "role": roles.get(r[1]),
            "ts": r[2],
            "text": say_mask.mask(r[3]),
            "who": "owner_channel" if r[4] else "agent",
        }
        for r in rows
    ]
    return out, truncated


def sources(conn, *, since_ts: int, until_ts: int, now_ms: int) -> dict:
    """`GET /digest/sources`: проекты с рабочей папкой, их сессии хаба (незакрытые и закрытые за окно) и
    реплики владельца за окно (П16: и ответы агента владельца, поле `who`). Окно — не длиннее 48 ч (иначе 422)."""
    if since_ts < 0 or until_ts < since_ts:
        raise DigestError(422, "окно: since_ts не больше until_ts")
    if until_ts - since_ts > WINDOW_MAX_MS:
        raise DigestError(422, "окно: не длиннее 48 ч")
    projects = []
    rows = conn.execute(
        "SELECT id, name, workdir, agent_policy_json FROM main.project "
        "WHERE workdir IS NOT NULL AND workdir != '' ORDER BY id"
    ).fetchall()
    for pid, name, workdir, policy_json in rows:
        sessions = _sessions(conn, pid, since_ts=since_ts)
        says, truncated = _says(
            conn,
            _sessions(conn, pid),
            since_ts=since_ts - 1,
            until_ts=until_ts,
            agent=True,
            limit=SAYS_MAX,
        )
        says = say_mask.squash(says)
        projects.append(
            {
                "id": pid,
                "name": name,
                "store": agent_runs.schema_for_project(conn, pid),
                "workdir": workdir,
                DIGEST_KEY: enabled(policy_json, DIGEST_KEY),
                "sessions": [
                    {
                        "id": s["id"],
                        "role": s["role"],
                        "engine": s["engine"],
                        "workdir": os.path.join(workdir, s["subdir"]) if s["subdir"] else workdir,
                        "subdir": s["subdir"],
                        "status": s["status"],
                    }
                    for s in sessions
                ],
                "says": says,
                "truncated": truncated,
            }
        )
    return {"now_ts": now_ms, "day": today(now_ms), "projects": projects}


# --- подкладка хосту (C2, C3, П1, П6) ----------------------------------------------------------------------


def feed_extras(conn, hub_session_id: int, *, want_digest: bool, now_ms: int) -> dict:
    """П6: что подать перед репликой сессии хаба. `digest` — сводка проекта за сегодня (только при
    `want_digest` и включённом «сводка после сжатия»), `siblings` — реплики владельца (`typed`) других сессий хаба
    того же проекта после курсора `siblings_ts` (не старше 24 ч), маскированные (П13, П14). `siblings_upto`
    — до какого `created_ts` рассмотрены кандидаты (курсор ack); `null` — двигать нечего. При выключенном
    «соседи» кандидаты не подаются, но курсор всё равно движется (включение не принесёт старого потока).
    Сессии нет — 404."""
    row = conn.execute(
        "SELECT project_id, siblings_ts FROM main.hub_session WHERE id = ?", (hub_session_id,)
    ).fetchone()
    if row is None:
        raise DigestError(404, "not found")
    project_id, cursor = row
    found, policy_json = _policy_json(conn, project_id)
    digest_on = found and enabled(policy_json, DIGEST_KEY)
    siblings_on = found and enabled(policy_json, SIBLINGS_KEY)
    digest = None
    if want_digest and digest_on:
        current = get(conn, project_id, today(now_ms))
        if current is not None:
            digest = {
                "text": digest_text.defang(current["text"]),
                "at": current["at"],
                "day": current["day"],
                "updated_ts": current["updated_ts"],
            }
    since = max(cursor or 0, now_ms - SIBLINGS_BACK_MS)
    others = [s for s in _sessions(conn, project_id) if s["id"] != hub_session_id]
    items, _cut = _says(conn, others, since_ts=since, until_ts=None, agent=False, limit=SIBLINGS_ROWS_MAX)
    upto = max((item["ts"] for item in items), default=None)
    siblings = []
    if siblings_on:
        siblings = [
            {k: i[k] for k in ("id", "ts", "role", "session_id", "text")} for i in say_mask.squash(items)
        ]
    project = conn.execute("SELECT name FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return {
        "project_id": project_id,
        "project": (project[0] if project else None) or f"#{project_id}",
        "digest": digest,
        "digest_on": digest_on,
        "siblings": siblings,
        "siblings_upto": upto,
        "siblings_on": siblings_on,
    }


def ack(conn, hub_session_id: int, siblings_upto: int) -> int | None:
    """П1, П6: хост подал блок — курсор соседей `siblings_ts` двигается, только вперёд. -> курсор после."""
    row = conn.execute("SELECT siblings_ts FROM main.hub_session WHERE id = ?", (hub_session_id,)).fetchone()
    if row is None:
        raise DigestError(404, "not found")
    if row[0] is None or siblings_upto > row[0]:
        conn.execute(
            "UPDATE main.hub_session SET siblings_ts = ? WHERE id = ?", (siblings_upto, hub_session_id)
        )
        return siblings_upto
    return row[0]


# --- проект по папке (C1.6) --------------------------------------------------------------------------------


def project_by_cwd(conn, cwd: str) -> int | None:
    """Проект по самому длинному совпадению рабочей папки: папка равна `workdir` или лежит внутри него.
    Папка `~` не забирает вложенные проекты — их `workdir` длиннее."""
    want = os.path.normpath(cwd)
    best: tuple[int, int] | None = None
    for pid, workdir in conn.execute(
        "SELECT id, workdir FROM main.project WHERE workdir IS NOT NULL AND workdir != ''"
    ):
        root = os.path.normpath(workdir)
        if want == root or want.startswith(root.rstrip("/") + "/"):
            if best is None or len(root) > best[1]:
                best = (pid, len(root))
    return best[0] if best is not None else None
