"""Работа постоянной сессии хаба: очередь реплик владельца, лента событий, запросы разрешений и вопросы, просьбы
«прервать» и «усыпить», ручки хоста. Ф41-2б, docs/architecture.md и поправки 4а
(П1–П4, П8, П12, П13 — они приоритетнее).

Запись сессии (`main.hub_session`) ведёт `hub_sessions.py`; здесь — её строки в `held_event`, `held_request`,
`held_say`. Таблицы `work_*` (задачи по «Го», убраны в Ф78-1) не трогаются: id сессий хаба и id задач
пересекались, поэтому у постоянной сессии всё своё — таблицы, ручки, пространство id запросов и реплик.

Файл строк сессии (П2) — `hub_session.store`, фиксируется при создании. Запрос и реплика находятся по id
(`db_of(id)`), и этот файл обязан совпасть с `store` их сессии — иначе 404. У записи, созданной до миграции
0057 (41-2а), `store` пуст: он ставится при первой записи (`_store`), читать у неё до тех пор нечего.

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`. Отказы — `hub_sessions.SessionError`
с готовым русским текстом (интерфейс показывает его как есть, П13). Кто что может, проверяют маршруты
(`routes/sessions.py`): действия владельца — только кука UI, хост — только `work_host`."""

import json

from .. import autopilot as catalog
from . import autopilot as autopilot_mod
from . import card_route as card_route_mod
from . import engine as engine_mod
from . import held as held_mod
from . import held_files, hub_sessions
from . import session_always as always_mod
from .audit import write_audit
from .db import db_of, next_seq
from .hub_sessions import SessionError
from .queries import _scope_predicate  # noqa: PLC2701 — та же проверка скоупа, что у всех выдач

EVENT_KINDS = (
    "start",
    "tool",
    "tool_done",
    "text",
    "request",
    "decision",
    "say",
    "turn_end",
    "status",
    "error",
    "warn",
    "interrupt",
    "compact",
)
REQUEST_KINDS = ("permission", "question")
REQUEST_STATUSES = ("pending", "allowed", "denied", "answered", "expired")
DECIDED_STATUSES = ("allowed", "denied", "answered")
SAY_PRIORITIES = ("next", "now")
# Ф56-1б (П5): источник реплики — владелец из интерфейса (`ui`) или его агент из канала (`agent`).
SAY_SOURCES = ("ui", "agent")
# П3б: просьба «прервать» снимается только условно — `clear_interrupt_upto`. Ф41-4 П3г: хост снимает и
# исполненные «поднять», «сжать» и признак оборванного хода по сигналу.
CLEARABLE = ("sleep", "wake", "compact", "signal_lost", "mcp_reload")
TURN_ORIGINS = ("signal",)

SAY_MAX = 16_000
SUMMARY_MAX = 1000
TEXT_SUMMARY_MAX = 8000  # события `text` (текст модели) и `say` (реплика владельца)
TOOL_MAX = 200
REF_MAX = 200
REASON_MAX = 2000
MESSAGE_MAX = 2000
FAILURE_MAX = 1000
EVENTS_BATCH_MAX = 200
INPUT_MAX = 256 * 1024
# Ф41-3 (2.1): подробности события — `brief` (строке ленты) и `detail` (по клику), пределы после сериализации.
BRIEF_MAX = 1000
NAME_BRIEF_MAX = 200
DETAIL_MAX = 60_000
ANSWERS_MAX = 20
# 3.2: столько неудачных запусков подряд — и хост больше не пробует, пока владелец не напишет новую реплику
# (`say` обнуляет счётчик). Решает хост по `launch_fails` записи; ядро только считает.
MAX_LAUNCH_FAILS = 3

NOTE_RECOVER = "ход прерван остановкой хоста или выключением"
NOTE_STALE = "хост не отвечает — ход прерван, сессия спит"
NOTE_SLEEP_DROP = "не отправлено: сессия усыплена"
NOTE_CLOSE_DROP = "не отправлено: сессия закрыта"
NOTE_CLOSED = "сессия закрыта"
# Ф48-2 (раздел A): прерванная реплика — возврат в очередь и два решения владельца.
NOTE_SAY_INTERRUPTED = "прервана — ждёт решения: отправить снова или убрать"
NOTE_SAY_RESENT = "отправлена снова"
NOTE_SAY_REMOVED = "убрана"

CLOSED = "сессия хаба закрыта"


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в work.py/agent_runs.py: схема и доверенные фрагменты подставляются здесь."""
    return template.format(schema=schema, **parts)


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- запись сессии ------------------------------------------------------------------------------------------


def _session(conn, hub_session_id: int) -> held_mod.HubSession:
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    return rec


def _open_ui(conn, hub_session_id: int) -> held_mod.HubSession:
    """Незакрытая сессия для действия владельца; закрытая — 409."""
    rec = _session(conn, hub_session_id)
    if rec.closed_ts is not None:
        raise SessionError(409, CLOSED)
    return rec


def _open_host(conn, hub_session_id: int) -> held_mod.HubSession:
    """Незакрытая сессия для ручки хоста; закрытая — 410 (П8: хост гасит раннер)."""
    rec = _session(conn, hub_session_id)
    if rec.closed_ts is not None:
        raise SessionError(410, CLOSED)
    return rec


def visible(conn, principal, hub_session_id: int) -> held_mod.HubSession:
    """Запись, видимая принципалу (кука UI — все проекты; bearer — проекты токена); иначе 404."""
    rec = _session(conn, hub_session_id)
    if principal.project_ids is not None and rec.project_id not in principal.project_ids:
        raise SessionError(404, "not found")
    return rec


def _store(conn, rec: held_mod.HubSession) -> str:
    """П2: файл строк сессии. У записи 41-2а без `store` — ставится один раз, при первой записи."""
    if rec.store is not None:
        return rec.store
    from . import agent_runs  # тем же правилом, что при создании (`hub_sessions.create`)

    store = agent_runs.schema_for_project(conn, rec.project_id)
    conn.execute("UPDATE main.hub_session SET store = ? WHERE id = ? AND store IS NULL", (store, rec.id))
    return store


# --- события ------------------------------------------------------------------------------------------------


def _fits(value, limit: int) -> bool:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= limit


def pack_detail(brief, detail) -> str | None:
    """Ф41-3 (2.1): `held_event.detail_json` — `{"brief": …, "detail": …}`. Резать обязан хост; здесь —
    страховка: поле, не влезшее в предел (`brief` — 1000 байт, `detail` — 60 000), отбрасывается, событие
    принимается, в `brief` ставится `"cut": true`. Оба поля пусты — `None` (столбец остаётся пустым)."""
    brief = brief if isinstance(brief, dict) else None
    detail = detail if isinstance(detail, dict) else None
    cut = False
    if brief is not None and not _fits(brief, BRIEF_MAX):
        brief, cut = None, True
    if detail is not None and not _fits(detail, DETAIL_MAX):
        detail, cut = None, True
    if cut:
        brief = {**(brief or {}), "cut": True}
        if not _fits(brief, BRIEF_MAX):
            brief = {"cut": True}
    if brief is None and detail is None:
        return None
    return json.dumps({"brief": brief, "detail": detail}, ensure_ascii=False)


def _add_event(
    conn,
    store: str,
    hub_session_id: int,
    *,
    ts: int,
    kind: str,
    summary: str,
    tool: str | None = None,
    is_error: bool = False,
    ref: str | None = None,
    brief: dict | None = None,
    detail: dict | None = None,
) -> int:
    if kind not in EVENT_KINDS:
        raise SessionError(422, f"kind события: одно из {', '.join(EVENT_KINDS)}")
    limit = TEXT_SUMMARY_MAX if kind in ("text", "say") else SUMMARY_MAX
    cur = conn.execute(
        _sql(
            store,
            "INSERT INTO {schema}.held_event (session_id, ts, kind, tool, summary, is_error, ref, "
            "detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ),
        (
            hub_session_id,
            ts,
            kind,
            _clip(tool, TOOL_MAX),
            _clip(summary or "", limit) or "",
            1 if is_error else 0,
            _clip(ref, REF_MAX),
            pack_detail(brief, detail),
        ),
    )
    return cur.lastrowid


def _event_and_touch(
    conn,
    rec: held_mod.HubSession,
    now_ms: int,
    kind: str,
    summary: str,
    *,
    ref: str | None = None,
    brief: dict | None = None,
    **sets,
) -> int:
    """Событие ядра в ленту сессии; запись сессии получает `last_event_id` и новый `seq`."""
    event_id = _add_event(
        conn, _store(conn, rec), rec.id, ts=now_ms, kind=kind, summary=summary, ref=ref, brief=brief
    )
    hub_sessions.touch(conn, rec.id, now_ms, last_event_id=event_id, **sets)
    return event_id


# Ф41-3 (2.1): в списке — `brief` и признак `has_detail`; сам `detail` (до 60 КБ на событие) в список не
# читается — его достаёт SQLite из `detail_json`, не отдавая строку целиком.
_EVENT_LIST_COLS = (
    "id, ts, kind, tool, summary, is_error, ref, json_extract(detail_json, '$.brief'), "
    "json_type(detail_json, '$.detail') = 'object'"
)


def _event_dict(row) -> dict:
    brief = None
    if row[7]:
        try:
            brief = json.loads(row[7])
        except ValueError, TypeError:
            brief = None
    return {
        "id": row[0],
        "ts": row[1],
        "kind": row[2],
        "tool": row[3],
        "summary": row[4],
        "is_error": bool(row[5]),
        "ref": row[6],
        "brief": brief if isinstance(brief, dict) else None,
        "has_detail": bool(row[8]),
    }


def list_events(
    conn, rec: held_mod.HubSession, *, after: int | None, limit: int, before: int | None = None
) -> list[dict]:
    """События сессии по возрастанию `id`. `after` — новые после курсора (первые `limit`); `before` —
    ПОСЛЕДНИЕ `limit` событий с `id < before` («показать раньше» и начальная загрузка хвоста ленты)."""
    if rec.store is None:
        return []
    cols = _EVENT_LIST_COLS
    if before is not None:
        rows = conn.execute(
            _sql(
                rec.store,
                "SELECT {cols} FROM {schema}.held_event WHERE session_id = ? AND id > ? AND id < ? "
                "ORDER BY id DESC LIMIT ?",
                cols=cols,
            ),
            (rec.id, after or 0, before, limit),
        ).fetchall()
        rows.reverse()
    else:
        rows = conn.execute(
            _sql(
                rec.store,
                "SELECT {cols} FROM {schema}.held_event WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                cols=cols,
            ),
            (rec.id, after or 0, limit),
        ).fetchall()
    return [_event_dict(r) for r in rows]


def get_event(conn, rec: held_mod.HubSession, event_id: int) -> dict:
    """Ф41-3 (2.1): событие целиком — с `detail` (по клику в ленте). Нет события у этой сессии — 404."""
    if rec.store is None:
        raise SessionError(404, "not found")
    row = conn.execute(
        _sql(
            rec.store,
            "SELECT {cols}, detail_json FROM {schema}.held_event WHERE session_id = ? AND id = ?",
            cols=_EVENT_LIST_COLS,
        ),
        (rec.id, event_id),
    ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    event = _event_dict(row)
    detail = None
    if row[9]:
        try:
            packed = json.loads(row[9])
        except ValueError:
            packed = None
        if isinstance(packed, dict) and isinstance(packed.get("detail"), dict):
            detail = packed["detail"]
    event["has_detail"] = detail is not None
    event["detail"] = detail
    return event


# --- запросы ------------------------------------------------------------------------------------------------

_REQUEST_COLUMNS = (
    "id",
    "session_id",
    "kind",
    "tool",
    "input_json",
    "input_truncated",
    "reason",
    "status",
    "answer_json",
    "created_ts",
    "decided_ts",
    "delivered_ts",
    "seq",
    "rule",
    # Ф66-1: столбцы делегата Ф49 (`delegate_*`, `passed_*`) остаются в таблице мёртвыми и не читаются.
)
_REQUEST_SELECT = ", ".join(f"r.{c}" for c in _REQUEST_COLUMNS)
# П8: без доставленного решения — висящие и решённые, но не доставленные процессу.
_UNDELIVERED = (
    "(r.status = 'pending' OR (r.status IN ('allowed', 'denied', 'answered') AND r.delivered_ts IS NULL))"
)


def _request(conn, request_id: int) -> tuple[dict, held_mod.HubSession]:
    """(запрос, его сессия). П2: файл запроса (`db_of(id)`) обязан совпасть с `store` сессии — иначе 404
    (у id запросов двух файлов разные пространства)."""
    schema = db_of(request_id)
    row = conn.execute(
        _sql(schema, "SELECT {cols} FROM {schema}.held_request r WHERE r.id = ?", cols=_REQUEST_SELECT),
        (request_id,),
    ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    request = dict(zip(_REQUEST_COLUMNS, row, strict=True))
    rec = held_mod.get(conn, request["session_id"])
    if rec is None or rec.store != schema:
        raise SessionError(404, "not found")
    return request, rec


def _update_request(conn, request_id: int, **sets) -> None:
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        _sql(
            db_of(request_id),
            "UPDATE {schema}.held_request SET {sets} WHERE id = ?",
            sets=", ".join(f"{n} = ?" for n in names),
        ),
        [*(sets[n] for n in names), request_id],
    )


def request_dict(request: dict, *, project_id: int | None = None) -> dict:
    """Запрос сессии для выдачи: поля запроса и `session_id`."""
    try:
        tool_input = json.loads(request["input_json"])
    except ValueError, TypeError:
        tool_input = None
    try:
        answer = json.loads(request["answer_json"]) if request["answer_json"] else None
    except ValueError, TypeError:
        answer = None
    answer = answer if isinstance(answer, dict) else None
    return {
        "id": request["id"],
        "session_id": request["session_id"],
        "project_id": project_id,
        "kind": request["kind"],
        "tool": request["tool"],
        "input": tool_input,
        "input_truncated": bool(request["input_truncated"]),
        "reason": request["reason"],
        "status": request["status"],
        "answer": answer,
        "created_ts": request["created_ts"],
        "decided_ts": request["decided_ts"],
        "delivered": request["delivered_ts"] is not None,
        "seq": request["seq"],
        # Ф41-5 (3.4): ключ выключателя автопилота, под которым пришёл запрос, и его название.
        "rule": request.get("rule"),
        "rule_title": catalog.title(request.get("rule")),
        # Ф99-7.2 (П11): ключ «Всегда в этой сессии» этой карточки (`null` — кнопки нет).
        "always": always_mod.card_key({**request, "input": tool_input}),
        # Кто решил (только владельцу в интерфейсе; хост его не передаёт сессии): `null` — владелец (и его агент, и
        # «сразу да» Ф66-1 — как клик); Ф51-1 B: `{auto: true, role: "автопилот"}` — снятая галочка
        # «Автопилота» (хост ответил сам); `{role, role_no, sub}` — решения делегата Ф49 до Ф66-1.
        "decided_by": answer.get("by") if answer is not None else None,
    }


def _requests_where(conn, rec: held_mod.HubSession, where: str) -> list[dict]:
    if rec.store is None:
        return []
    rows = conn.execute(
        _sql(
            rec.store,
            "SELECT {cols} FROM {schema}.held_request r WHERE r.session_id = ? AND {where} ORDER BY r.id",
            cols=_REQUEST_SELECT,
            where=where,
        ),
        (rec.id,),
    ).fetchall()
    return [dict(zip(_REQUEST_COLUMNS, r, strict=True)) for r in rows]


def pending_requests(conn, rec: held_mod.HubSession) -> list[dict]:
    """Ждущие решения владельца — с входом инструмента (`GET /sessions/{id}`)."""
    return [
        request_dict(r, project_id=rec.project_id) for r in _requests_where(conn, rec, "r.status = 'pending'")
    ]


def _expire_open_requests(conn, rec: held_mod.HubSession, now_ms: int) -> int:
    """Висящие и решённые, но не доставленные процессу запросы -> `expired` (П8)."""
    requests = _requests_where(conn, rec, _UNDELIVERED)
    for request in requests:
        _update_request(conn, request["id"], status="expired", decided_ts=now_ms)
    return len(requests)


# --- реплики ------------------------------------------------------------------------------------------------


def queued_says(conn, rec: held_mod.HubSession) -> list[dict]:
    """Очередь реплик сессии по порядку подачи (П4: хост подаёт по одной, первой — самую старую). Ф41-3в:
    у каждой — `files`, приложенные файлы `[{id, name, mime, size, kind}]` (пусто — без файлов).

    Ф48-2 (раздел A): прерванные реплики — в НАЧАЛЕ очереди, с `status: "interrupted"` и временем
    `interrupted_ts` (у обычных — `"queued"` и `null`). Хост прерванную не подаёт, и пока она в очереди, не
    подаёт несрочные реплики за ней."""
    if rec.store is None:
        return []
    rows = conn.execute(
        _sql(
            rec.store,
            "SELECT id, text, priority, created_ts, interrupted_ts, resent_ts, interview_id, typed "
            "FROM {schema}.held_say "
            "WHERE session_id = ? AND status = 'queued' ORDER BY interrupted_ts IS NULL, id",
        ),
        (rec.id,),
    ).fetchall()
    files = held_files.of_says(conn, rec.store, [r[0] for r in rows])
    return [
        {
            "id": r[0],
            "session_id": rec.id,
            "text": r[1],
            "priority": r[2],
            "created_ts": r[3],
            "files": files.get(r[0], []),
            "status": "queued" if r[4] is None else "interrupted",
            "interrupted_ts": r[4],
            # Когда владелец нажал «Отправить снова» (`null` — не отправлялась снова): хосту — для uuid повтора.
            "resent_ts": r[5],
            # Ф60-1 (П1): подача расшифровки интервью (`null` — обычная реплика владельца).
            "interview_id": r[6],
            # Ф86-1 (C1.2): реплику набрал сам владелец.
            "typed": bool(r[7]),
        }
        for r in rows
    ]


def _drop_queued_says(
    conn, rec: held_mod.HubSession, now_ms: int, note: str, *, keep_interrupted: bool = False
) -> int:
    """Неподанные реплики -> `dropped`, на каждую — событие `say` с причиной (`ref` — id реплики).
    `keep_interrupted` (Ф48-2): прерванные остаются — сессию они не поднимают и ждут решения владельца."""
    says = queued_says(conn, rec)
    if keep_interrupted:
        says = [item for item in says if item["interrupted_ts"] is None]
    if not says:
        return 0
    store = _store(conn, rec)
    last = None
    for item in says:
        conn.execute(
            _sql(store, "UPDATE {schema}.held_say SET status = 'dropped' WHERE id = ? AND status = 'queued'"),
            (item["id"],),
        )
        held_files.release(conn, store, item["id"], now_ms)
        last = _add_event(conn, store, rec.id, ts=now_ms, kind="say", summary=note, ref=str(item["id"]))
    hub_sessions.touch(conn, rec.id, now_ms, last_event_id=last)
    return len(says)


def counts(conn, rec: held_mod.HubSession) -> tuple[int, int, int]:
    """(запросов, ждущих решения ВЛАДЕЛЬЦА; реплик в очереди — вместе с прерванными; из них прерванных, Ф48-2) —
    для строки сессии."""
    if rec.store is None:
        return 0, 0, 0
    pending = conn.execute(
        _sql(
            rec.store,
            "SELECT COUNT(*) FROM {schema}.held_request WHERE session_id = ? AND status = 'pending'",
        ),
        (rec.id,),
    ).fetchone()[0]
    queued, interrupted = conn.execute(
        _sql(
            rec.store,
            "SELECT COUNT(*), COUNT(interrupted_ts) FROM {schema}.held_say "
            "WHERE session_id = ? AND status = 'queued'",
        ),
        (rec.id,),
    ).fetchone()
    return pending, queued, interrupted


def feedable_says(conn, rec: held_mod.HubSession) -> int:
    """Ф48-2 (раздел A): сколько реплик очереди хост может подать сейчас. Прерванная реплика не подаётся и
    держит несрочные за собой (проходят только срочные `now`) — такие реплики сессию не поднимают и сигналы
    не задерживают. То же правило — у хоста (`HeldSessions._feedable`)."""
    says = queued_says(conn, rec)
    if any(item["interrupted_ts"] is not None for item in says):
        return sum(1 for item in says if item["interrupted_ts"] is None and item["priority"] == "now")
    return len(says)


# --- общее для закрытия, восстановления и молчащего хоста ---------------------------------------------------


def on_close(conn, rec: held_mod.HubSession, *, now_ms: int) -> None:
    """3.1: владелец закрывает сессию (`hub_sessions.close`, та же транзакция, ДО отметки `closed_ts`) — очередь
    реплик снимается, запросы без доставленного решения протухают, в ленту — событие `status`."""
    _drop_queued_says(conn, rec, now_ms, NOTE_CLOSE_DROP)
    held_files.purge_session(conn, rec.store, rec.id, now_ms)  # Ф41-3в: байты файлов сессии — сразу
    _expire_open_requests(conn, rec, now_ms)
    _event_and_touch(conn, rec, now_ms, "status", NOTE_CLOSED)


def force_asleep(conn, rec: held_mod.HubSession, *, now_ms: int, note: str) -> bool:
    """П8: процесса сессии больше нет (старт или остановка хоста, хост молчит) — запись в `asleep`, запросы
    без доставленного решения — `expired`, просьбы «прервать» и «усыпить» и причина ожидания снимаются;
    очередь реплик остаётся — она поднимет сессию обычным порядком. У бывшей в ходу — событие `status`.
    Возвращает, изменилось ли что-то."""
    expired = _expire_open_requests(conn, rec, now_ms)
    sets = hub_sessions.state_sets(rec, "asleep")
    if rec.proc_pid is not None or rec.proc_start is not None:
        sets.update(proc_pid=None, proc_start=None)
    if rec.wait_reason is not None:
        sets["wait_reason"] = None
    sets.update(hub_sessions.signal_sets(rec, "asleep", now_ms))  # Ф41-4 П5: ход по сигналу оборван
    from . import owner_queue  # импорт внутри: очередь сама опирается на этот модуль

    # Ф56-1в (5.2): зависший ход кончился сном.
    owner_queue.close_states(conn, rec.id, ("stuck",), by="system", action="asleep", now_ms=now_ms)
    if rec.state == "running" or expired:
        text = note + (f"; запросов снято: {expired}" if expired else "")
        _event_and_touch(conn, rec, now_ms, "status", text, **sets)
        # Ф56-1в (3, А1): ход оборван извне или карточки сняты — карточка `broken` в той же транзакции.
        owner_queue.open_broken(conn, rec, note=note, expired=expired, now_ms=now_ms)
        return True
    if sets:
        hub_sessions.touch(conn, rec.id, now_ms, **sets)
        return True
    return False


# --- действия владельца (маршрут — только кука UI) ---------------------------------------------------------------


def files_brief(files: list[dict]) -> dict:
    """Ф41-3в: `brief` события `say` — `{"type": "files", "files": [{name, size, kind}]}`. Предел `brief` —
    1000 байт (2.1): длинные имена укорачиваются серединой («нач…конец»), пока не влезет."""
    for limit in (NAME_BRIEF_MAX, 60, 40, 24, 12, 6):
        brief = {
            "type": "files",
            "files": [
                {"name": _short_name(f["name"], limit), "size": f["size"], "kind": f["kind"]} for f in files
            ],
        }
        if _fits(brief, BRIEF_MAX):
            break
    return brief


def _short_name(name: str, limit: int) -> str:
    if len(name) <= limit:
        return name
    tail = max(1, limit // 3)
    return name[: limit - tail - 1] + "…" + name[-tail:]


def file_room(conn, hub_session_id: int) -> None:
    """Ф41-3в, читатель: до приёма байтов — сессия открыта (закрытая — 409) и неприложенных файлов меньше
    30 (иначе 409)."""
    rec = _open_ui(conn, hub_session_id)
    held_files.check_room(conn, rec.store, rec.id)


def file_add(conn, hub_session_id: int, *, now_ms: int, audit: dict, **fields) -> dict:
    """Ф41-3в: принятый файл — в склад и строкой сессии (проверки `file_room` — ещё раз, в транзакции)."""
    rec = _open_ui(conn, hub_session_id)
    row = held_files.commit(conn, _store(conn, rec), rec.id, now_ms=now_ms, **fields)
    _file_audit(conn, rec, row["id"], row["size"], row["kind"], "uploaded", now_ms, audit)
    return row


def file_remove(conn, hub_session_id: int, file_id: str, *, now_ms: int, audit: dict) -> None:
    """Ф41-3в: убрать неприложенный файл (приложенный — 409, чужой и несуществующий — 404)."""
    rec = _open_ui(conn, hub_session_id)
    gone = held_files.remove(conn, rec.store, rec.id, file_id)
    _file_audit(conn, rec, file_id, gone["size"], gone["kind"], "removed", now_ms, audit)


def _file_audit(conn, rec, file_id: str, size: int, kind: str, result: str, now_ms: int, audit: dict) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_file",
        result=result,
        detail={"hub_session_id": rec.id, "file_id": file_id, "size": size, "kind": kind},
        **audit,
    )


def say(
    conn,
    hub_session_id: int,
    *,
    text: str,
    priority: str,
    now_ms: int,
    audit: dict,
    files: list[str] | None = None,
    source: str = "ui",
    token_id: int | None = None,
    interview_id: int | None = None,
    typed: bool = False,
) -> int:
    """3.1 + П3а, П4: реплика владельца в очередь сессии (событие `say` — при доставке, Ф55-1). `now`
    («прервать и написать») флага прерывания НЕ ставит: идущий ход прерывает сам хост, у спящей и
    простаивающей `now` = `next`. Новая
    реплика — новая попытка запуска: счётчик неудач и причина последней обнуляются. Закрытая сессия — 409.

    Ф41-3в (4.1): `files` — id загруженных в ЭТУ сессию файлов (до 10, без повторов, в сумме до 30 МиБ, не
    приложенные и не стёртые — иначе 422); с файлами текст может быть пустым. Событие `say` (при
    доставке) получает `brief` со списком `{name, size, kind}`.

    Ф56-1б (3.6, П5): `source` — `ui` (окно сессии, ответ владельца по карточке) или `agent` (агент владельца ответом по
    карточке `turn`), пишется в `held_say.source`; новая реплика закрывает открытую карточку `turn` сессии
    (`closed_by` = источник, `token_id` — токен агента).

    Ф60-1 (П1): `interview_id` — подача расшифровки интервью (`core/interview.py`), источник остаётся `ui`.
    Это не ответ владельца: карточки `turn` и `stall` не закрываются, счётчик неудач запуска не сбрасывается; при
    доставке — событие `status` «Интервью #N: подано K строк» без текста (П2).

    Ф86-1 (C1.2, П3, П4): `typed` — реплику набрал (надиктовал) сам владелец: окно сессии от куки и ответ кукой
    по карточке канала владельца; передача дел переносит признак исходной реплики. Пишется в `held_say.typed`
    (сводка дня и «слова владельца из соседних сессий» берут только такие)."""
    rec = _open_ui(conn, hub_session_id)
    if priority not in SAY_PRIORITIES:
        raise SessionError(422, f"priority: {' или '.join(SAY_PRIORITIES)}")
    if source not in SAY_SOURCES:
        raise SessionError(422, f"source: {' или '.join(SAY_SOURCES)}")
    file_ids = held_files.check_ids(files or [])
    if not text.strip() and not file_ids:
        raise SessionError(422, "text: пустая реплика")
    if len(text) > SAY_MAX:
        raise SessionError(422, f"text: не длиннее {SAY_MAX} символов")
    store = _store(conn, rec)
    attached = held_files.pick(conn, store, rec.id, file_ids) if file_ids else []
    say_id = conn.execute(
        _sql(
            store,
            "INSERT INTO {schema}.held_say (session_id, text, priority, status, created_ts, source, "
            "interview_id, typed) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
        ),
        (rec.id, text, priority, now_ms, source, interview_id, 1 if typed and interview_id is None else 0),
    ).lastrowid
    held_files.attach(conn, store, rec.id, attached, say_id)
    sets: dict = {}
    if interview_id is None:
        from . import owner_queue  # импорт внутри: очередь сама опирается на этот модуль

        owner_queue.close_turns(
            conn, rec.id, by=source, action="reply", now_ms=now_ms, say_id=say_id, token_id=token_id
        )
        # Ф56-1в (2.2): реплика закрывает «стоит без дела» и снимает «Пусть ждёт».
        owner_queue.close_stall_on_say(
            conn, rec.id, by=source, say_id=say_id, now_ms=now_ms, token_id=token_id
        )
        if rec.launch_fails or rec.failure is not None:
            sets = {"launch_fails": 0, "failure": None}
    # Ф55-1 (2.5): событие `say` с текстом пишется, когда реплика доставлена (`say_delivered`); здесь —
    # только новый `seq` сессии (очередь изменилась) и сброс счётчика неудач запуска.
    hub_sessions.touch(conn, rec.id, now_ms, **sets)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_say",
        result="queued",
        detail={
            "hub_session_id": rec.id,
            "say_id": say_id,
            "priority": priority,
            "text_len": len(text),
            **({"source": source} if source != "ui" else {}),
            **({"files": len(attached)} if attached else {}),
            **({"interview_id": interview_id} if interview_id is not None else {}),
        },
        **audit,
    )
    return say_id


def _interrupted_say(conn, rec: held_mod.HubSession, say_id: int) -> str:
    """Ф48-2: прерванная реплика ЭТОЙ сессии или отказ: 404 — нет такой, 409 — она не прервана (обычная в
    очереди, уже подана или снята). Возвращает файл базы сессии."""
    row = None
    if rec.store is not None and db_of(say_id) == rec.store:
        row = conn.execute(
            _sql(
                rec.store,
                "SELECT status, interrupted_ts FROM {schema}.held_say WHERE id = ? AND session_id = ?",
            ),
            (say_id, rec.id),
        ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    if row[0] != "queued" or row[1] is None:
        raise SessionError(409, "реплика не прервана")
    return rec.store


def say_resend(conn, hub_session_id: int, say_id: int, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """Ф48-2 (раздел A): «Отправить снова» — прерванная реплика становится обычной следующей в очереди
    (отметка снята, срочность — `next`: второй раз ход она не прерывает). Событие `say` с `ref`. Закрытая
    сессия — 409, реплика не прервана — 409, чужая или несуществующая — 404."""
    rec = _open_ui(conn, hub_session_id)
    store = _interrupted_say(conn, rec, say_id)
    conn.execute(
        _sql(
            store,
            "UPDATE {schema}.held_say SET interrupted_ts = NULL, priority = 'next', resent_ts = ? "
            "WHERE id = ?",
        ),
        (now_ms, say_id),
    )
    sets = {"launch_fails": 0, "failure": None} if rec.launch_fails or rec.failure is not None else {}
    _event_and_touch(conn, rec, now_ms, "say", NOTE_SAY_RESENT, ref=str(say_id), **sets)
    _say_audit(conn, rec, say_id, "resent", now_ms, audit)
    return _session(conn, rec.id)


def say_remove(conn, hub_session_id: int, say_id: int, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """Ф48-2 (раздел A): «Убрать» — прерванная реплика снимается (`dropped`), очередь за ней идёт дальше.
    Отказы — как у `say_resend`."""
    rec = _open_ui(conn, hub_session_id)
    store = _interrupted_say(conn, rec, say_id)
    conn.execute(_sql(store, "UPDATE {schema}.held_say SET status = 'dropped' WHERE id = ?"), (say_id,))
    held_files.release(conn, store, say_id, now_ms)
    _event_and_touch(conn, rec, now_ms, "say", NOTE_SAY_REMOVED, ref=str(say_id))
    _say_audit(conn, rec, say_id, "removed", now_ms, audit)
    return _session(conn, rec.id)


def _say_audit(conn, rec, say_id: int, result: str, now_ms: int, audit: dict) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_say_interrupted",
        result=result,
        detail={"hub_session_id": rec.id, "say_id": say_id},
        **audit,
    )


def _say_interrupted(conn, rec: held_mod.HubSession, say_id, now_ms: int) -> int | None:
    """Ф48-2 (раздел A): хост закрыл оборванный ход, начатый репликой владельца, — она возвращается в очередь
    прерванной (в той же транзакции, что конец хода). Годится `delivered` и ещё не отмеченная доставленной
    `queued` (отметка хоста могла не дойти — запоздалая получит 409). Реплики нет, она чужая, снята или уже
    прервана — без изменений (ход всё равно закрывается). Файлы реплики снова «не отпущены»: байты живут,
    пока она ждёт. Возвращает id события ленты `say` или `None`."""
    if rec.store is None or not isinstance(say_id, int) or db_of(say_id) != rec.store:
        return None
    # Ф60-1 (ревью, 1): подача расшифровки интервью прерванной не бывает — она возвращается в очередь
    # обычной: иначе следующие подачи встали бы до решения владельца, а сырой текст звонка ушёл бы карточкой.
    conn.execute(
        _sql(
            rec.store,
            "UPDATE {schema}.held_say SET status = 'queued', delivered_ts = NULL "
            "WHERE id = ? AND session_id = ? AND interview_id IS NOT NULL AND interrupted_ts IS NULL "
            "AND status IN ('delivered', 'queued')",
        ),
        (say_id, rec.id),
    )
    if conn.execute(
        _sql(rec.store, "SELECT 1 FROM {schema}.held_say WHERE id = ? AND interview_id IS NOT NULL"),
        (say_id,),
    ).fetchone():
        return None
    done = conn.execute(
        _sql(
            rec.store,
            "UPDATE {schema}.held_say SET status = 'queued', delivered_ts = NULL, interrupted_ts = ? "
            "WHERE id = ? AND session_id = ? AND interrupted_ts IS NULL "
            "AND status IN ('delivered', 'queued')",
        ),
        (now_ms, say_id, rec.id),
    ).rowcount
    if not done:
        return None
    conn.execute(
        _sql(
            rec.store,
            "UPDATE {schema}.held_file SET released_ts = NULL WHERE say_id = ? AND purged_ts IS NULL",
        ),
        (say_id,),
    )
    return _add_event(
        conn, rec.store, rec.id, ts=now_ms, kind="say", summary=NOTE_SAY_INTERRUPTED, ref=str(say_id)
    )


def is_interrupted(conn, rec: held_mod.HubSession, say_id) -> bool:
    """Ф48-2: реплика сессии стоит в очереди прерванной (ответ ручки `turn` хосту)."""
    if rec.store is None or not isinstance(say_id, int) or db_of(say_id) != rec.store:
        return False
    row = conn.execute(
        _sql(
            rec.store,
            "SELECT 1 FROM {schema}.held_say WHERE id = ? AND session_id = ? AND status = 'queued' "
            "AND interrupted_ts IS NOT NULL",
        ),
        (say_id, rec.id),
    ).fetchone()
    return row is not None


def mark_interrupted(conn, rec: held_mod.HubSession, say_id: int, interrupted_ts: int) -> None:
    """Ф48-2: реплика в очереди помечается прерванной (передача дел переносит прерванную как есть)."""
    conn.execute(
        _sql(_store(conn, rec), "UPDATE {schema}.held_say SET interrupted_ts = ? WHERE id = ?"),
        (interrupted_ts, say_id),
    )


def interrupt(conn, hub_session_id: int, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """3.1 + П3б: «Прервать» — просьба прервать идущий ход (`interrupt_requested_ts`); очередь реплик не
    трогается (П4). Хода нет (сессия спит или ждёт входа) — 409; закрытая — 409."""
    rec = _open_ui(conn, hub_session_id)
    if rec.state != "running":
        raise SessionError(409, "хода нет — прерывать нечего")
    hub_sessions.touch(conn, rec.id, now_ms, interrupt_requested_ts=now_ms)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_interrupt",
        result="ok",
        detail={"hub_session_id": rec.id},
        **audit,
    )
    return _session(conn, rec.id)


def sleep(conn, hub_session_id: int, *, now_ms: int, audit: dict) -> tuple[held_mod.HubSession, bool, int]:
    """3.1 + П4: «Усыпить». Неподанные реплики — `dropped` с событием «не отправлено: сессия усыплена» (иначе
    они тут же подняли бы сессию снова) — и у спящей тоже. Процесс есть — просьба хосту
    (`sleep_requested_ts`): прервать ход, закрыть ввод, дождаться выхода. Уже спит и очередь пуста — без
    изменений. Возвращает (запись, изменилось ли что-то, сколько реплик снято). Закрытая — 409."""
    rec = _open_ui(conn, hub_session_id)
    from . import interview  # импорт внутри: интервью опирается на этот модуль

    # Ф60-1 (ревью, 3): сон снял бы подачи расшифровки, а строки уже сочтены поданными — сначала «Закончить».
    interview.check_free(conn, rec.id)
    # Ф48-2 (раздел A): прерванная реплика сон переживает — сессию она не поднимает.
    dropped = _drop_queued_says(conn, rec, now_ms, NOTE_SLEEP_DROP, keep_interrupted=True)
    asked = rec.state != "asleep"
    if asked:
        hub_sessions.touch(conn, rec.id, now_ms, sleep_requested_ts=now_ms)
    changed = asked or dropped > 0
    if changed:
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_sleep",
            result="ok",
            detail={"hub_session_id": rec.id, "dropped_says": dropped, "state": rec.state},
            **audit,
        )
    return _session(conn, rec.id), changed, dropped


def mcp_reload(conn, hub_session_id: int, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """Ф73-1 (1): «Перечитать MCP» — просьба хосту перезапустить процесс сессии с продолжением разговора
    (`mcp_reload_ts`): живой — при простое (идущий ход не обрывается), спящий — подъёмом без хода. Повторное
    нажатие ставит новое значение — строго новее прежнего: исполненное хостом раньше его не снимет.
    Закрытая — 409."""
    rec = _open_ui(conn, hub_session_id)
    value = max(now_ms, (rec.mcp_reload_ts or 0) + 1)
    hub_sessions.touch(conn, rec.id, now_ms, mcp_reload_ts=value)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_mcp_reload",
        result="ok",
        detail={"hub_session_id": rec.id, "state": rec.state},
        **audit,
    )
    return _session(conn, rec.id)


def decide(
    conn,
    request_id: int,
    *,
    decision: str,
    answers: dict | None,
    message: str | None,
    now_ms: int,
    audit: dict,
    always: bool = False,
) -> tuple[str, int]:
    """3.1: решение владельца по карточке сессии:
    разрешить / отказать с текстом / ответы на вопрос. Только из `pending` — иначе 409. Возвращает (статус,
    id сессии). Ф66-1: делегат Ф49 убран — решение всегда от имени владельца.

    Ф99-7.2 (П11): `always` — «Всегда в этой сессии»: разрешить и запомнить ключ карточки в наборе сессии.
    Только разрешение и только у карточки с ключом (`session_always.card_key`) — иначе 422. Передаёт `True`
    ЛИШЬ ручка куки интерфейса; агент владельца и «Сразу да» зовут без него."""
    request, rec = _request(conn, request_id)
    if request["status"] != "pending":
        raise SessionError(409, "запрос уже решён или протух")
    if decision not in ("allow", "deny"):
        raise SessionError(422, "decision: allow или deny")
    always_key = None
    if always:
        if decision != "allow":
            raise SessionError(422, "«Всегда в этой сессии» — только с «разрешить»")
        try:
            parsed = json.loads(request["input_json"]) if request["input_json"] else None
        except ValueError, TypeError:
            parsed = None
        always_key = always_mod.card_key({**request, "input": parsed})
        if always_key is None:
            raise SessionError(422, "у этой карточки «Всегда в этой сессии» нет")
    if message is not None and len(message) > MESSAGE_MAX:
        raise SessionError(422, f"message: не длиннее {MESSAGE_MAX}")
    answer: dict = {"decision": decision}
    if decision == "allow" and request["kind"] == "question":
        if not isinstance(answers, dict) or not answers:
            raise SessionError(422, "answers: ответы на вопросы сессии")
        if len(answers) > ANSWERS_MAX or not all(
            isinstance(k, str) and isinstance(v, str) and len(k) <= 2000 and len(v) <= 4000
            for k, v in answers.items()
        ):
            raise SessionError(422, "answers: {вопрос: ответ} строками")
        answer["answers"] = answers
        status = "answered"
    elif decision == "allow":
        status = "allowed"
    else:
        status = "denied"
    if message:
        answer["message"] = message
    _update_request(
        conn,
        request_id,
        status=status,
        answer_json=json.dumps(answer, ensure_ascii=False),
        decided_ts=now_ms,
    )
    verdict = {"allowed": "разрешил", "denied": "отказал", "answered": "ответил"}[status]
    _event_and_touch(
        conn,
        rec,
        now_ms,
        "decision",
        f"владелец {verdict}: {request['tool'] or request['kind']}",
        ref=str(request_id),
    )
    detail = {"hub_session_id": rec.id, "request_id": request_id, "message_len": len(message or "")}
    if always_key is not None:
        always_mod.add(conn, held_mod.get(conn, rec.id) or rec, always_key, now_ms=now_ms)
        detail["always"] = always_key.split(":", 1)[0]
    write_audit(conn, now_ms=now_ms, action="hub_session_decide", result=status, detail=detail, **audit)
    return status, rec.id


# --- хост (маршрут — только bearer `work_host`) -------------------------------------------------------------


def _seen(conn, hub_session_id: int, now_ms: int) -> None:
    """П1: любая ручка хоста по сессии подтверждает, что хост её ведёт (`seq` при этом не двигается)."""
    conn.execute("UPDATE main.hub_session SET host_seen_ts = ? WHERE id = ?", (now_ms, hub_session_id))


def add_events(conn, hub_session_id: int, events: list[dict], *, now_ms: int) -> int:
    """3.2: пачка событий хоста (до 200). `summary` режется по виду: `text` и `say` — 8000, прочие — 1000.
    Закрытая сессия — 410."""
    rec = _open_host(conn, hub_session_id)
    if len(events) > EVENTS_BATCH_MAX:
        raise SessionError(422, f"событий в пачке не больше {EVENTS_BATCH_MAX}")
    _seen(conn, rec.id, now_ms)
    if not events:
        return 0
    store = _store(conn, rec)
    last = None
    for ev in events:
        last = _add_event(
            conn,
            store,
            rec.id,
            ts=int(ev.get("ts") or now_ms),
            kind=ev["kind"],
            summary=ev.get("summary") or "",
            tool=ev.get("tool"),
            is_error=bool(ev.get("is_error")),
            ref=ev.get("ref"),
            brief=ev.get("brief"),
            detail=ev.get("detail"),
        )
    hub_sessions.touch(conn, rec.id, now_ms, last_event_id=last)
    return len(events)


def create_request(
    conn,
    hub_session_id: int,
    *,
    kind: str,
    tool: str | None,
    tool_input,
    reason: str | None,
    now_ms: int,
    rule: str | None = None,
    waiters=None,  # noqa: ARG001 — Ф66-1: делегата нет; параметр оставлен прежним вызовам
) -> int:
    """3.2: запрос разрешения или вопрос сессии -> `pending`; событие `request`. Вход — целиком (до 256 КБ),
    длиннее — обрезан с пометкой `input_truncated`. Спящая сессия — 409 (у неё нет процесса: хост сначала
    сообщает состояние); закрытая — 410.

    Ф66-1 (C, П3): разрешение при маршруте `auto` группы «Разрешения» (таблица проекта; ручная сессия —
    всегда владельцу) — «сразу да»: строка сразу `allowed` и доставлена (`delivered_ts`), события «запрос» и
    «владелец разрешил» — как после клика, история и аудит `card_route_auto`. Хост узнаёт это по
    `auto_decided(conn, id)` и отвечает движку сам. Вопрос (группа без `auto`) — всегда карточка."""
    if kind not in REQUEST_KINDS:
        raise SessionError(422, f"kind: одно из {', '.join(REQUEST_KINDS)}")
    if rule is not None and rule not in catalog.BY_KEY:
        raise SessionError(422, "rule: нет такого выключателя автопилота")
    rec = _open_host(conn, hub_session_id)
    if rec.state == "asleep":
        raise SessionError(409, "сессия спит — запрос создавать некому")
    _seen(conn, rec.id, now_ms)
    raw = json.dumps(tool_input if tool_input is not None else {}, ensure_ascii=False)
    truncated = len(raw) > INPUT_MAX
    if truncated:
        raw = json.dumps({"_truncated": raw[:INPUT_MAX]}, ensure_ascii=False)
    route_auto = (
        kind == "permission"
        and card_route_mod.for_card(conn, rec.project_id, "permission", rec.id) == card_route_mod.AUTO
        and not _always_me(conn, rec.project_id, tool, tool_input, rule)
    )
    # Ревью Р3: день проекта закрыт — карточка ждёт «Начать день» (пометка `defer`, потом минутный проход).
    deferred = route_auto and card_route_mod.day_closed(conn, rec.project_id)
    auto = route_auto and not deferred
    request_id = conn.execute(
        _sql(
            _store(conn, rec),
            "INSERT INTO {schema}.held_request (session_id, kind, tool, input_json, input_truncated, reason, "
            "status, created_ts, seq, rule, answer_json, decided_ts, delivered_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ),
        (
            rec.id,
            kind,
            _clip(tool, TOOL_MAX),
            raw,
            1 if truncated else 0,
            _clip(reason, REASON_MAX),
            "allowed" if auto else "pending",
            now_ms,
            next_seq(conn),
            rule,
            json.dumps({"decision": "allow"}) if auto else None,
            now_ms if auto else None,
            now_ms if auto else None,
        ),
    ).lastrowid
    what = "вопрос" if kind == "question" else f"просит {tool or '?'}"
    _event_and_touch(conn, rec, now_ms, "request", f"{what} (запрос #{request_id})", ref=str(request_id))
    if deferred:
        card_route_mod.defer(
            conn, f"req:{request_id}", project_id=rec.project_id, session_id=rec.id, now_ms=now_ms
        )
    if auto:
        verdict = f"владелец разрешил: {_clip(tool, TOOL_MAX) or kind}"
        _event_and_touch(conn, rec, now_ms, "decision", verdict, ref=str(request_id))
        card_route_mod.record(
            conn,
            ref=f"req:{request_id}",
            kind="permission",
            project_id=rec.project_id,
            session_id=rec.id,
            now_ms=now_ms,
        )
    return request_id


def _always_me(conn, project_id: int, tool, tool_input, rule: str | None) -> bool:
    """Ф76-1 (П7): разрешение под «всегда меня» проекта — карточкой владельцу, мимо «сразу да» (строки запроса
    ещё нет — отпечаток по входу)."""
    from . import decision_print, decisions  # noqa: PLC0415 — журнал решений опирается на этот модуль

    fingerprint, _cmd = decision_print.permission_print(tool, tool_input, rule)
    return decisions.always_me_print(conn, project_id, fingerprint, None, rule)


def auto_decided(conn, request_id: int) -> bool:
    """Ф66-1 (П3): запрос решён «сразу да» при создании — хост отвечает движку сам, без карточки."""
    row = conn.execute(
        _sql(db_of(request_id), "SELECT status, delivered_ts FROM {schema}.held_request WHERE id = ?"),
        (request_id,),
    ).fetchone()
    return row is not None and row[0] == "allowed" and row[1] is not None


def _host_request(conn, request_id: int) -> tuple[dict, held_mod.HubSession]:
    request, rec = _request(conn, request_id)
    if rec.closed_ts is not None:
        raise SessionError(410, CLOSED)
    return request, rec


def expire_request(conn, request_id: int, *, now_ms: int) -> None:
    """3.2: процесс умер, запрос отменён движком или решение не доставлено -> `expired`; событие `status`."""
    request, rec = _host_request(conn, request_id)
    undelivered = request["status"] in DECIDED_STATUSES and request["delivered_ts"] is None
    if request["status"] != "pending" and not undelivered:
        raise SessionError(409, "запрос уже закрыт")
    _seen(conn, rec.id, now_ms)
    _update_request(conn, request_id, status="expired", decided_ts=now_ms)
    _event_and_touch(conn, rec, now_ms, "status", f"запрос #{request_id} протух", ref=str(request_id))


AUTO_BY = {"auto": True, "role": "автопилот"}  # Ф51-1 B: `decided_by` карточки, закрытой снятой галочкой


def auto_request(conn, request_id: int, *, now_ms: int, audit: dict) -> str:
    """Ф51-1 B: владелец снял галочку «Автопилота», под которой висела карточка, — хост уже ответил процессу
    сам (тем же правилом, что на новый запрос) и закрывает карточку: разрешение — `allowed`, вопрос —
    `denied` (хост отвечает на него «решай сама»), решение сразу доставлено. В `answer_json` — `by` =
    `AUTO_BY` (не владелец и не делегат), признак делегата снят (делегату карточка больше не видна, его
    позднее решение — 409). Только из `pending` — иначе 409 (кто первый, того решение). Вторая проверка
    на стороне ядра: сессия в авто и ключ карточки снят у её проекта — иначе 409."""
    request, rec = _host_request(conn, request_id)
    if request["status"] != "pending":
        raise SessionError(409, "запрос уже решён или протух")
    rule = request.get("rule")
    if rec.mode != "auto" or rule is None or rule not in autopilot_mod.auto_free(conn, rec):
        raise SessionError(409, "выключатель автопилота стоит — карточку решает владелец")
    _seen(conn, rec.id, now_ms)
    question = request["kind"] == "question"
    status = "denied" if question else "allowed"
    answer = {"decision": "deny" if question else "allow", "by": dict(AUTO_BY)}
    _update_request(
        conn,
        request_id,
        status=status,
        answer_json=json.dumps(answer, ensure_ascii=False),
        decided_ts=now_ms,
        delivered_ts=now_ms,
    )
    what = "вопрос решает сама" if question else f"разрешено само: {request['tool'] or '?'}"
    _event_and_touch(
        conn,
        rec,
        now_ms,
        "decision",
        f"галочка снята — {what}",
        ref=str(request_id),
        brief={"auto": True, "verdict": status, "rule": rule},
    )
    detail = {"hub_session_id": rec.id, "request_id": request_id, "auto": True, "rule": rule}
    write_audit(conn, now_ms=now_ms, action="hub_session_decide", result=status, detail=detail, **audit)
    from . import decisions  # noqa: PLC0415

    # Ф76-1 (П1, П5): снятая галочка — строка журнала, образцом не служит.
    decisions.record(
        conn, f"req:{request_id}", decisions.BY_AUTOPILOT, "deny" if question else "allow", now_ms=now_ms
    )
    return status


def request_delivered(conn, request_id: int, *, now_ms: int) -> None:
    """3.2: решение владельца дошло до процесса. Повтор — без изменений; запрос не решён — 409."""
    request, rec = _host_request(conn, request_id)
    if request["status"] not in DECIDED_STATUSES:
        raise SessionError(409, "запрос не решён")
    _seen(conn, rec.id, now_ms)
    if request["delivered_ts"] is None:
        _update_request(conn, request_id, delivered_ts=now_ms)


def say_session(conn, say_id: int) -> int | None:
    """Сессия реплики по её id (`None` — нет такой)."""
    schema = db_of(say_id)
    sql = _sql(schema, "SELECT session_id FROM {schema}.held_say WHERE id = ?")
    row = conn.execute(sql, (say_id,)).fetchone()
    return row[0] if row is not None else None


def say_delivered(conn, say_id: int, *, now_ms: int) -> int:
    """3.2 + П4: реплика записана во ввод процесса (хост отмечает только после записи). Реплика уже не в
    очереди — 409; закрытая сессия — 410. Возвращает id сессии."""
    schema = db_of(say_id)
    row = conn.execute(
        _sql(schema, "SELECT session_id, status, interrupted_ts FROM {schema}.held_say WHERE id = ?"),
        (say_id,),
    ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    rec = held_mod.get(conn, row[0])
    if rec is None or rec.store != schema:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(410, CLOSED)
    if row[1] != "queued":
        raise SessionError(409, "реплика уже не в очереди")
    if row[2] is not None:
        raise SessionError(409, "реплика прервана — ждёт решения владельца")  # Ф48-2 (A)
    conn.execute(
        _sql(schema, "UPDATE {schema}.held_say SET status = 'delivered', delivered_ts = ? WHERE id = ?"),
        (now_ms, say_id),
    )
    # Ф55-1 (2.5): событие ленты `say` («ты написал») — здесь, когда сессия реплику получила, а не при
    # отправке; текст и `brief` файлов — те же. Реплика, поданная снова после прерывания, пишется ещё раз.
    text, priority, interview_id, say_source = conn.execute(
        _sql(schema, "SELECT text, priority, interview_id, source FROM {schema}.held_say WHERE id = ?"),
        (say_id,),
    ).fetchone()
    attached = held_files.of_says(conn, schema, [say_id]).get(say_id)
    if interview_id is not None:
        # Ф60-1 (П2): подача расшифровки — своё событие без текста (`status` с `brief.type =
        # "interview_feed"`; новый вид `held_event` потребовал бы пересоздать таблицу ради CHECK). Выжимка
        # «Новой с передачей дел» берёт только `say` и `text` — расшифровка в неё не едет.
        from . import interview as interview_mod  # импорт внутри: модуль интервью опирается на этот

        lines = interview_mod.count_feed_lines(text)
        event_id = _add_event(
            conn,
            schema,
            rec.id,
            ts=now_ms,
            kind="status",
            summary=interview_mod.feed_summary(interview_id, lines),
            ref=str(say_id),
            brief={"type": "interview_feed", "interview_id": interview_id, "lines": lines},
        )
    else:
        brief = files_brief(attached) if attached else None
        if say_source == "agent":
            # Ф92-1 (A1): реплика агента владельца — не реплика владельца. Префикс `summary` не меняется (его разбирают
            # передача дел и UI), пометка — в `brief.by`.
            brief = {**(brief or {"type": "agent_say"}), "by": "agent"}
        event_id = _add_event(
            conn,
            schema,
            rec.id,
            ts=now_ms,
            kind="say",
            summary=f"владелец ({priority}): {text}",
            ref=str(say_id),
            brief=brief,
        )
    held_files.release(conn, schema, say_id, now_ms)
    _seen(conn, rec.id, now_ms)
    # `queued_says` в строке сессии изменился; лента получила событие.
    hub_sessions.touch(conn, rec.id, now_ms, last_event_id=event_id)
    return rec.id


def turn(conn, hub_session_id: int, changes: dict, *, now_ms: int) -> held_mod.HubSession:
    """3.2 + П3, П13: хост сообщает о ходе — начало, конец, неудачный запуск, причина ожидания. Поля
    `changes`:

    - `state` (обязательно) — как у `…/state`: `asleep` обнуляет настоящий процесс и снимает обе просьбы
      владельца, уход из `running` снимает просьбу «прервать» (`hub_sessions.state_sets`);
    - `last_turn_ts` — конец последнего хода;
    - `clear_interrupt_upto` (П3б) — просьба «прервать» снимается, только если она не новее этого времени
      (более новая остаётся: она про следующий ход);
    - `clear` — `["sleep"]`: просьба «усыпить» исполнена; Ф73-1: `"mcp_reload"` — «Перечитать MCP»
      исполнена значением `mcp_reload_upto` (обязательно рядом): снимается, только если не новее его;
    - `failure` (только с `state: "asleep"`) — запуск не удался: `launch_fails` растёт, причина пишется;
      без `failure` при `running`/`idle` счётчик и причина обнуляются (запуск удался);
    - `wait_reason` (ключ есть — пишется, и `null` тоже) — почему реплика ждёт; начало хода (`running`) без
      ключа причину снимает;
    - `usage` (Ф41-3, 2.3) — расход хода `{"context_tokens", "context_limit", "turn_cost_usd"}`: размер
      контекста и предел пишутся как есть, цена хода прибавляется к накопленной (`cost_usd`; `null`, пока ни
      одной цены не было). `turn` с `usage` двигает `seq` всегда; `keep_signal` — конец хода по сигналу,
      который движок отклонил без вызова модели: `signal_turn_ts` не снимается; `cut` (только с
      `asleep`) — идущий ход оборвал хаб (ограничение автопилота): ставится `signal_lost_ts`.
      Ф41-4б: `context_tokens: null` — после
      сжатия (размер уточнит следующий ход; предел без присланного остаётся прежним); `base_tokens` (П17) —
      база сессии, без ключа остаётся прежней;
    - `bg` (Ф48-1) — снимок фоновых задач процесса `{"items": [...]}`: без ключа и `null` — «не менять»,
      пустой список — «задач нет»; `asleep` столбец обнуляет сам;
    - `interrupted_say` (Ф48-2, A) — id реплики владельца, ход по которой оборван «Прервать» или срочной
      репликой: из `delivered` она возвращается в очередь прерванной (событие `say` с `ref`); не нашлась —
      молча; `interrupted_says` (Ф55-1, 2.4) — то же списком: ход начат пачкой реплик;
    - `compact_at` (Ф48-2, D) — действующий порог авто-сжатия (ключ есть — пишется, и `null`);
    - `source` (Ф50-4, только с `running`) — источник начатого хода: `say`, `signal`, `self`, `compact`;
      начало хода (`turn_ts`) ядро ставит само переходом в `running` и снимает уходом из него;
    - `answer` (Ф56-1б, П7) — последний ответ хода `{"full", "brief", "brief_kind", "cut"}` или `null`;
      `summary` — строка конца хода (`turn_summary` хоста). Конец хода (`last_turn_ts` новый, состояние не
      `running`) увеличивает `hub_session.turns` (ключ карточки) и, если ход не прерван, заводит карточку
      `turn` по правилу 3.3 спеки (`owner_queue.open_turn_card`);
    - `mid_lost` (Ф50-1, П3) — сигнал, поданный посреди хода, сессия не прочитала (ход оборван, строка не
      начата): ставится `signal_lost_ts` при ЛЮБОМ состоянии — и при `idle`, и при сне по просьбе владельца;
    - `stuck` (Ф56-1в, 5.2; только с `running`) — ход завис: `{"since_ts", "step": {"tool"}}` — карточка
      `stuck` идущего хода (завести или обновить), `null` — шаг пришёл, карточка закрывается. Карточки
      состояний по этому вызову (`stall`/`broken` закрываются ходом, `dead` — третьей неудачей запуска,
      `broken` — обрывом хабом) — `owner_queue.on_turn`;
    - `late` (Ф93-1, F3) — отчёт конца хода запоздал (ядро было недоступно; повтор хоста или старт
      следующего хоста): без `last_turn_ts` или с `last_turn_ts` не новее записанного он отбрасывается
      целиком — уже принятый (ответ не дошёл до хоста) или более старый, чем записанный ход, не двигает
      номер хода, расход и состояние и не заводит карточку второй раз; у спящей записи (`recover` прошёл
      раньше) состояние остаётся `asleep`.

    `seq` двигается только при настоящем изменении; `host_seen_ts` — всегда (П1). Закрытая — 410."""
    rec = _open_host(conn, hub_session_id)
    if changes.get("late"):
        late_ts = changes.get("last_turn_ts")
        if late_ts is None or (rec.last_turn_ts is not None and late_ts <= rec.last_turn_ts):
            _seen(conn, rec.id, now_ms)
            return _session(conn, rec.id)
        if rec.state == "asleep":
            # Ревью Ф93-1 (В3): `recover` старта хоста уже усыпил сессию (её конец хода не принимался) —
            # запоздавший конец хода её не будит: номер хода, расход и карточка — да, состояние — нет.
            changes = {**changes, "state": "asleep"}
    state = changes.get("state")
    if state not in held_mod.STATES:
        raise SessionError(422, f"state: {', '.join(held_mod.STATES)}")
    failure = changes.get("failure")
    if failure is not None and not failure.strip():
        failure = None
    if failure is not None and state != "asleep":
        raise SessionError(422, "failure — только вместе с state: asleep")
    unknown = [name for name in changes.get("clear") or () if name not in CLEARABLE]
    if unknown:
        raise SessionError(422, f"clear: {', '.join(CLEARABLE)}")
    wait_reason = changes.get("wait_reason")
    if wait_reason is not None and wait_reason not in held_mod.WAIT_REASONS:
        raise SessionError(422, f"wait_reason: {', '.join(held_mod.WAIT_REASONS)} или null")

    sets = hub_sessions.state_sets(rec, state, now_ms=now_ms)
    # Ф50-4: источник хода называет хост началом хода; повторный `running` без `source` его не трогает.
    source = changes.get("source")
    if source is not None and source not in held_mod.TURN_SOURCES:
        raise SessionError(422, f"source: {', '.join(held_mod.TURN_SOURCES)}")
    if state == "running" and source is not None and source != rec.turn_origin:
        sets["turn_origin"] = source
    if state == "asleep" and (rec.proc_pid is not None or rec.proc_start is not None):
        sets.update(proc_pid=None, proc_start=None)
    upto = changes.get("clear_interrupt_upto")
    if upto is not None and rec.interrupt_requested_ts is not None and rec.interrupt_requested_ts <= upto:
        sets["interrupt_requested_ts"] = None
    clear = changes.get("clear") or ()
    if "sleep" in clear and rec.sleep_requested_ts is not None:
        sets["sleep_requested_ts"] = None
    # Ф41-4 (2.5, П3г, П5): исполненные просьбы «поднять» и «сжать», признак оборванного хода по сигналу.
    if "wake" in clear and rec.wake_requested_ts is not None:
        sets["wake_requested_ts"] = None
    if "compact" in clear and rec.compact_requested_ts is not None:
        sets["compact_requested_ts"] = None
    if "signal_lost" in clear and rec.signal_lost_ts is not None:
        sets["signal_lost_ts"] = None
    # Ф73-1 (1): «Перечитать MCP» исполнена процессом, запущенным под значение `mcp_reload_upto`; владелец нажал
    # ещё раз (значение новее) — просьба остаётся.
    if "mcp_reload" in clear:
        done = changes.get("mcp_reload_upto")
        if done is None:
            raise SessionError(422, "clear: mcp_reload — только вместе с mcp_reload_upto")
        if rec.mcp_reload_ts is not None and rec.mcp_reload_ts <= done:
            sets["mcp_reload_ts"] = None
    origin = changes.get("origin")
    if origin is not None and origin not in TURN_ORIGINS:
        raise SessionError(422, f"origin: {', '.join(TURN_ORIGINS)}")
    sets.update(hub_sessions.signal_sets(rec, state, now_ms))
    if changes.get("cut") and state == "asleep" and rec.signal_lost_ts is None:
        # 10.10: ход (любой — по реплике, по сигналу, свой) оборван хабом, не владельцем: сессия не должна
        # остаться спать с брошенной работой — хост поднимет её и подаст постоянную строку.
        sets.setdefault("signal_lost_ts", now_ms)
    if origin == "signal" and state == "running":
        if rec.signal_turn_ts is None:
            sets["signal_turn_ts"] = now_ms  # П5: ход начат по сигналу — до обычного конца хода
    elif state == "running" and rec.state != "running" and rec.signal_turn_ts is not None:
        # Ревью 41-4 (1): сигналы выданы, а ход начат НЕ по сигналу (реплика владельца, сжатие) — во ввод они не
        # попали (ответ выдачи не дошёл до хоста). Конец этого хода признак бы стёр — он становится
        # `signal_lost`: после хода хост подаст сессии строку «проверь входящее».
        sets.update(signal_turn_ts=None, signal_lost_ts=now_ms)
    elif (
        state == "idle"
        and changes.get("last_turn_ts") is not None
        and rec.signal_turn_ts is not None
        and not changes.get("keep_signal")
    ):
        # Обычный конец хода по сигналу. `keep_signal` (ревью, а): движок ход отклонил, не вызвав модель, —
        # сигнал не обработан, признак остаётся (уснёт или начнёт другой ход — станет `signal_lost`).
        sets["signal_turn_ts"] = None
    if changes.get("mid_lost") and (rec.signal_lost_ts is None or "signal_lost_ts" in sets):
        # Ф50-1 (П3): выдача посреди хода `signal_turn_ts` не ставила — обычные пути `signal_lost` её не
        # видят; хост называет потерю сам. Сильнее снятия признака тем же вызовом.
        sets["signal_lost_ts"] = now_ms
    last_turn_ts = changes.get("last_turn_ts")
    if last_turn_ts is not None and last_turn_ts != rec.last_turn_ts:
        sets["last_turn_ts"] = last_turn_ts
    # Ф56-1б (3.1): конец хода — новый номер хода (прерванный ход — тоже конец хода; повтор той же отметки
    # конца хода номер не двигает).
    ended = state != "running" and "last_turn_ts" in sets
    if ended:
        sets["turns"] = rec.turns + 1
    if failure is not None:
        sets.update(launch_fails=rec.launch_fails + 1, failure=_clip(failure, FAILURE_MAX))
    elif state != "asleep" and (rec.launch_fails or rec.failure is not None):
        sets.update(launch_fails=0, failure=None)
    if "wait_reason" in changes:
        if wait_reason != rec.wait_reason:
            sets["wait_reason"] = wait_reason
        elif "wait_reason" in sets:
            del sets["wait_reason"]  # причина названа явно и не изменилась — `state_sets` её не снимает
    usage = changes.get("usage")
    if usage is not None:
        sets["usage_json"] = _next_usage(rec, usage, now_ms)
    sets.update(hub_sessions.bg_sets(rec, state, changes.get("bg")))  # Ф48-1
    # Ф48-2 (раздел D): действующий порог сжатия, как его посчитал хост.
    if "compact_at" in changes and changes["compact_at"] != rec.compact_at:
        sets["compact_at"] = changes["compact_at"]
    # Ф48-2 (раздел A): реплика оборванного хода — обратно в очередь прерванной; `seq` сессии двигается.
    # Ф55-1 (2.4): ход начат пачкой реплик — `interrupted_says`, прерванными возвращаются все.
    for say_id in [changes.get("interrupted_say"), *(changes.get("interrupted_says") or ())]:
        back = _say_interrupted(conn, rec, say_id, now_ms)
        if back is not None:
            sets["last_event_id"] = back
    _seen(conn, rec.id, now_ms)
    if sets:
        hub_sessions.touch(conn, rec.id, now_ms, **sets)
    # Ревью Ф56 (8): реплики, поданные ходом, которые ядро ещё считает `queued` (отметка «доставлено» не
    # дошла или опоздала: модель ответила раньше) — доставлены ДО разбора конца хода, иначе карточки `turn`
    # «по реплике владельца» не будет. Уже доставленные, прерванные, чужие — пропускаются (409/404 отметки).
    for say_id in changes.get("delivered_says") or ():
        try:
            if say_session(conn, say_id) == rec.id:
                say_delivered(conn, say_id, now_ms=now_ms)
        except SessionError:
            continue
    from . import owner_queue  # импорт внутри: очередь сама опирается на этот модуль

    # Ф56-1в: карточки состояний сессии (`stall`, `broken`, `dead`, `stuck`) по этому сообщению хоста.
    after = _session(conn, rec.id)
    owner_queue.on_turn(conn, rec, after, changes, ended=ended, failure=failure, now_ms=now_ms)
    interrupted = changes.get("interrupted_say") is not None or bool(changes.get("interrupted_says"))
    if ended and not interrupted:
        # 3.3: прерванный ход карточки не заводит — реплика вернётся прерванной (карточка `interrupted`).
        owner_queue.open_turn_card(
            conn,
            rec,
            turns=rec.turns + 1,
            answer=changes.get("answer"),
            summary=_clip(changes.get("summary"), SUMMARY_MAX),
            now_ms=now_ms,
        )
    return _session(conn, rec.id)


def mid_queued(conn, hub_session_id: int, queued: bool, *, now_ms: int) -> held_mod.HubSession:
    """Ф50-4 (П9): хост сообщает, стоит ли в очереди движка строка сигнала, поданная посреди хода: `True` —
    стоит (ждёт конца инструмента или хода; время первой такой строки не перетирается), `False` — вошла в
    ход, выброшена или потеряна. Состояние сессии не трогает; `seq` — только при изменении. Закрытая — 410."""
    rec = _open_host(conn, hub_session_id)
    _seen(conn, rec.id, now_ms)
    if queued and rec.mid_queued_ts is None and rec.state != "asleep":
        hub_sessions.touch(conn, rec.id, now_ms, mid_queued_ts=now_ms)
    elif not queued and rec.mid_queued_ts is not None:
        hub_sessions.touch(conn, rec.id, now_ms, mid_queued_ts=None)
    return _session(conn, rec.id)


def _next_usage(rec: held_mod.HubSession, usage: dict, now_ms: int) -> str:
    """Ф41-3 (2.3): новый `usage_json` записи — контекст и предел из присланного, цена хода — в сумму."""
    previous = hub_sessions.usage_of(rec) or {}
    cost = previous.get("cost_usd")
    cost = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
    turn_cost = usage.get("turn_cost_usd")
    if turn_cost is not None:
        cost = round((cost or 0.0) + float(turn_cost), 6)
    context = usage.get("context_tokens")
    limit = usage.get("context_limit")
    if limit is None and context is None:
        limit = previous.get("context_limit")  # сжатие предел не называет — прежний остаётся
    base = usage.get("base_tokens")
    if base is None:
        base = previous.get("base_tokens")
    value = {
        "context_tokens": None if context is None else int(context),
        "context_limit": None if limit is None else int(limit),
        "cost_usd": cost,
        "updated_ts": now_ms,
    }
    if base is not None:
        value["base_tokens"] = int(base)  # ключ есть, только когда база измерена
    return json.dumps(value)


def recover(conn, *, now_ms: int, bg_out: dict[int, dict] | None = None) -> list[int]:
    """П8: старт (или остановка) хоста — одной транзакцией по всем незакрытым сессиям: `running`/`idle` ->
    `asleep` (у бывших в ходу — событие `status`), запросы висящие И решённые, но не доставленные, —
    `expired`, просьбы «прервать» и «усыпить» и причины ожидания снимаются. Реплики `queued` остаются и
    поднимут сессию обычным порядком. Возвращает id сессий, у которых что-то изменилось.

    Ф89-1 (П12 б): `bg_out` — сюда каждая сессия, у которой был процесс (`running`/`idle` или pid), и её
    снимок фоновых задач (`bg_json`, `{"items": [...]}`; задач нет — пустой список), прочитанный ДО сброса
    (`asleep` его стирает): хост скажет сессии, что процесс перезапущен и что оборвано."""
    touched = []
    for rec in held_mod.list_sessions(conn):
        if bg_out is not None and (rec.state in ("running", "idle") or rec.proc_pid is not None):
            bg = hub_sessions.bg_of(rec)
            bg_out[rec.id] = {"items": list(bg["items"]) if bg is not None else []}
        if force_asleep(conn, rec, now_ms=now_ms, note=NOTE_RECOVER):
            touched.append(rec.id)
    return touched


# --- выдачи хосту: раздел `sessions` в `/work/host/open` и `/work/host/wait` --------------------------------


def _host_dict(conn, rec: held_mod.HubSession) -> dict:
    return hub_sessions.session_json(conn, rec, host=True)


def host_open(conn) -> dict:
    """`/work/host/open`: все незакрытые сессии хаба, их решённые и не доставленные запросы и очереди реплик
    (старт хоста)."""
    recs = held_mod.list_sessions(conn)
    requests: list[dict] = []
    says: list[dict] = []
    for rec in recs:
        requests += [
            request_dict(r, project_id=rec.project_id)
            for r in _requests_where(
                conn, rec, "r.status IN ('allowed', 'denied', 'answered') AND r.delivered_ts IS NULL"
            )
        ]
        says += queued_says(conn, rec)
    return {"items": [_host_dict(conn, rec) for rec in recs], "requests": requests, "says": says}


def _changed_requests(conn, where: str, params: list, *, limit: int | None = None) -> list[tuple[dict, int]]:
    """Запросы обоих файлов с условием на `r`/`s`; файл запроса — `store` его сессии (П2)."""
    found: list[tuple[dict, int]] = []
    for schema in held_mod.STORES:
        rows = conn.execute(
            _sql(
                schema,
                "SELECT {cols}, s.project_id FROM {schema}.held_request r JOIN main.hub_session s "
                "ON s.id = r.session_id AND s.store = '{schema}' WHERE {where} ORDER BY r.seq{limit}",
                cols=_REQUEST_SELECT,
                where=where,
                limit=" LIMIT ?" if limit is not None else "",
            ),
            [*params, *([limit] if limit is not None else [])],
        ).fetchall()
        found += [(dict(zip(_REQUEST_COLUMNS, r[:-1], strict=True)), r[-1]) for r in rows]
    return found


def host_changes(conn, *, since: int, watermark: int) -> tuple[dict, list[int]]:
    """`/work/host/wait`: записи сессий с `since < seq <= watermark` (и закрытые — хост гасит процесс по
    `closed_ts`), запросы с тем же условием на свой `seq` и очереди реплик изменённых незакрытых сессий
    (вставка реплики двигает `seq` сессии). Второе значение — все `seq` раздела (для `max_seq` снимка)."""
    recs = held_mod.select(conn, "seq > ? AND seq <= ?", [since, watermark], order="seq")
    requests = _changed_requests(conn, "r.seq > ? AND r.seq <= ?", [since, watermark])
    says: list[dict] = []
    for rec in recs:
        if rec.closed_ts is None:
            says += queued_says(conn, rec)
    section = {
        "items": [_host_dict(conn, rec) for rec in recs],
        "requests": [request_dict(r, project_id=project_id) for r, project_id in requests],
        "says": says,
    }
    return section, [rec.seq for rec in recs] + [r["seq"] for r, _project_id in requests]


def host_wait_snapshot(conn, *, since: int) -> dict:
    """`/work/host/wait`: изменения сессий хаба после `since` (хост видит все проекты), движок и отметка
    «паузу лимита снял владелец». Водяной знак — как у `/sync`: сначала `main.seq_counter`, наружу — не выше."""
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN")
    try:
        watermark = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
        sessions, seqs = host_changes(conn, since=since, watermark=watermark)
        return {
            "sessions": sessions,
            "max_seq": max(seqs) if seqs else since,
            # Ф16-1 (3.1, П11): движок сессий — ВСЕГДА в снимке. Хост шлёт свой в `?engine=`, и при
            # несовпадении ручка отдаёт ответ сразу (смена движка двигает только `main.meta`, не `seq`:
            # без этого поля хост узнал бы о переключении лишь по таймауту ожидания).
            "engine": engine_mod.get_engine(conn),
            # Ф66-3 F: отметка «паузу лимита снял владелец» — тем же путём, что и движок.
            "pause_cleared_ts": engine_mod.get_pause_cleared(conn),
        }
    finally:
        if own_tx:
            conn.execute("ROLLBACK")


# --- `/sync` и SSE (3.3, П12) -------------------------------------------------------------------------------


def sync_items(conn, principal, *, since: int, watermark: int, limit: int) -> list[tuple[int, str, dict]]:
    """Элементы ленты изменений `(seq, "hub_session"|"session_request", dict)` с `since < seq <= watermark`,
    не больше `limit` из каждого источника (вызывающий передаёт `limit + 1`, как прочим источникам `/sync`).
    Видимость — по проекту сессии, в SQL: bearer видит только проекты токена, запрос — по проекту своей
    сессии. `engine_session_id` — только куке UI; `workdir`, `cache` и `listening` лента не несёт."""
    own_pred, own_params = _scope_predicate("project_id", principal)
    pred, params = _scope_predicate("s.project_id", principal)
    items: list[tuple[int, str, dict]] = []
    for rec in held_mod.select(
        conn,
        f"seq > ? AND seq <= ? AND ({own_pred})",
        [since, watermark, *own_params],
        order="seq",
        limit=limit,
    ):
        items.append(
            (rec.seq, "hub_session", hub_sessions.session_json(conn, rec, ui=principal.is_ui, sync=True))
        )
    for request, project_id in _changed_requests(
        conn, f"r.seq > ? AND r.seq <= ? AND ({pred})", [since, watermark, *params], limit=limit
    ):
        items.append((request["seq"], "session_request", request_dict(request, project_id=project_id)))
    return items
