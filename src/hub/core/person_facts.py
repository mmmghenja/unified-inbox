"""Факты о людях (Ф24-1, docs/architecture.md).

К человеку (Ф21-1) хаб сам копит до 50 коротких фактов по переписке владельца с ним. Факты пишет фоновая служба
`hub facts` (обычный клиент API со скоупом `facts`, в ядре LLM нет); владелец смотрит и правит в UI. Единица
работы — (человек, файл): факты из переписки рабочего файла лежат в рабочем файле и уходят вместе с ним.

- `due` — кто «созрел»: последнее сообщение владельца ему (`contact_stats` -> `out.last_ts`) новее курсора, и с
  прошлой попытки прошла пауза (растёт со сбоями, П5). До первого вызова — метка старта `facts_start_ts`
  (`main.meta`): старые обмены не берутся.
- `context` — обмен с человеком за окно (D1 Ф21-1 на годовом окне беседы, П3), прежние и отклонённые факты.
- `apply` — запись службы одной транзакцией: версия (409 при правке владельца между чтением и записью), только свои
  авто-факты, дубли и отклонённые — мимо (сверка в Python, П8), предел 50.
- `add_manual`/`edit`/`reject` — правка владельца (только UI); `card_facts` — для карточки и `hub_person` (граница
  проектов, П2); `move`/`remove_orphans` — хуки людей (объединение, автосклейка по почте П1, уборка B4).

SQL — только здесь (маршруты SQL не пишут). Тексты фактов и переписки — ПДн: в журнал и аудит не пишутся.
"""

import json
import unicodedata

from . import people_stats as stats_mod
from . import queries
from .db import db_of
from .security import Principal

SCHEMAS = ("main", "work")
MIN_MS = 60 * 1000
DAY_MS = 24 * 60 * MIN_MS

FACTS_MAX = 50  # живых фактов на (человек, файл)
FACT_TEXT_MAX = 300
ADD_MAX = 10  # добавлений за одно применение
REJECTED_SHOWN = 20  # отклонённых владельцем — модели в контексте
RETRY_MS = 60 * MIN_MS  # пауза между попытками по человеку
RETRY_POW_MAX = 5  # П5: пауза растёт как RETRY_MS * 2^min(fails, 5)
FAILS_SKIP = 5  # П5: столько сбоев подряд — кусок пропускается (курсор сдвигается)
BOOTSTRAP_MS = 60 * DAY_MS  # первый прогон (человек, файл) — история за 60 дней
CONTEXT_CHARS = 16_000
MESSAGE_CHARS = 1_000
DUE_LIMIT_MAX = 50
TEXT_CHUNK = 200  # тексты сообщений читаются порциями (новейшие первыми, пока влезают в бюджет)
START_KEY = "facts_start_ts"

NotFound = queries.NotFound


class Invalid(Exception):
    """Нарушение правил записи — 422. Текст — без текстов фактов и переписки."""


class Conflict(Exception):
    """Версия фактов устарела (владелец правил между чтением и записью) — 409."""


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `queries._sql`: текст запроса — литерал в вызове, подставляются только доверенные
    фрагменты."""
    return template.format(schema=schema, **parts)


def _in(values) -> str:
    return ",".join("?" for _ in values)


# --- тексты -------------------------------------------------------------------------------------------------


def normalize(text) -> str:
    """Одна строка: управляющие символы и переводы строк -> пробел, пробелы схлопнуты, края обрезаны. Пусто
    или длиннее `FACT_TEXT_MAX` -> `Invalid`."""
    if not isinstance(text, str):
        raise Invalid("текст факта — строка")
    value = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in text)
    value = " ".join(value.split())
    if not value:
        raise Invalid("пустой текст факта")
    if len(value) > FACT_TEXT_MAX:
        raise Invalid(f"текст факта длиннее {FACT_TEXT_MAX} знаков")
    return value


def fold(text: str) -> str:
    """П8: ключ сверки дублей — без регистра (`casefold`, кириллица тоже), `ё` = `е`, пробелы схлопнуты.
    SQLite `lower()` кириллицу не сворачивает — сверка только здесь, в Python."""
    return " ".join(text.casefold().replace("ё", "е").split())


def _project_ids(raw) -> list[int]:
    """`project_ids_json` факта; мусор — `[0]` (виден только UI и токенам на все проекты)."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError, TypeError:
        return [0]
    if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return [0]
    return value


# --- состояние и факты (человек, файл) ----------------------------------------------------------------------


def _person_exists(conn, person_id: int) -> bool:
    return conn.execute("SELECT 1 FROM main.person WHERE id = ?", (person_id,)).fetchone() is not None


def _has_contacts(conn, schema: str, person_id: int) -> bool:
    row = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.contact WHERE person_id = ? LIMIT 1"), (person_id,)
    ).fetchone()
    return row is not None


def has_any_contacts(conn, person_id: int) -> bool:
    """Остались ли у человека контакты хоть в одном файле (П1: автосклейка переносит факты опустевшего)."""
    return any(_has_contacts(conn, schema, person_id) for schema in SCHEMAS)


_STATE_KEYS = ("cursor_ts", "last_try_ts", "version", "fails", "updated_ts")


def _state(conn, schema: str, person_id: int) -> dict | None:
    row = conn.execute(
        _sql(
            schema,
            "SELECT cursor_ts, last_try_ts, version, fails, updated_ts FROM {schema}.person_fact_state "
            "WHERE person_id = ?",
        ),
        (person_id,),
    ).fetchone()
    return dict(zip(_STATE_KEYS, row, strict=True)) if row is not None else None


def _put_state(conn, schema: str, person_id: int, state: dict) -> None:
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.person_fact_state (person_id, cursor_ts, last_try_ts, version, fails, "
            "updated_ts) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (person_id) DO UPDATE SET "
            "cursor_ts = excluded.cursor_ts, last_try_ts = excluded.last_try_ts, version = excluded.version, "
            "fails = excluded.fails, updated_ts = excluded.updated_ts",
        ),
        (person_id, *(state[k] for k in _STATE_KEYS)),
    )


def _bump_version(conn, schema: str, person_id: int) -> None:
    """Правка владельца: `version += 1` в файле факта (строка состояния создаётся, если её нет)."""
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.person_fact_state (person_id, version) VALUES (?, 1) "
            "ON CONFLICT (person_id) DO UPDATE SET version = version + 1",
        ),
        (person_id,),
    )


def _max(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


_FACT_KEYS = ("id", "text", "source", "project_ids_json", "created_ts", "updated_ts")


def _live_facts(conn, schema: str, person_id: int) -> list[dict]:
    """Живые (не отклонённые) факты (человек, файл): новые первыми."""
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id, text, source, project_ids_json, created_ts, updated_ts FROM {schema}.person_fact "
            "WHERE person_id = ? AND rejected_ts IS NULL ORDER BY updated_ts DESC, id DESC",
        ),
        (person_id,),
    ).fetchall()
    return [dict(zip(_FACT_KEYS, row, strict=True)) for row in rows]


def _rejected_texts(
    conn, schema: str, person_id: int, limit: int | None = None, within: set[int] | None = None
) -> list[str]:
    """Тексты отклонённых владельцем фактов, новые первыми. `within` — только факты, все проекты которых в этом
    множестве (контекст модели, после ревью п.1); `None` — все (сверка дублей в `apply`)."""
    sql = _sql(
        schema,
        "SELECT text, project_ids_json FROM {schema}.person_fact "
        "WHERE person_id = ? AND rejected_ts IS NOT NULL ORDER BY rejected_ts DESC, id DESC",
    )
    texts: list[str] = []
    for text, raw in conn.execute(sql, (person_id,)):
        if within is not None and not set(_project_ids(raw)) <= within:
            continue
        texts.append(text)
        if limit is not None and len(texts) >= limit:
            break
    return texts


def _evict(conn, schema: str, person_id: int) -> int:
    """Предел 50: живых больше `FACTS_MAX` — физически удаляются самые старые по `updated_ts` авто-факты;
    ручные не вытесняются (ручных ≥ 50 — авто просто не остаются). Отклонённые не считаются."""
    live = conn.execute(
        _sql(schema, "SELECT COUNT(*) FROM {schema}.person_fact WHERE person_id = ? AND rejected_ts IS NULL"),
        (person_id,),
    ).fetchone()[0]
    excess = live - FACTS_MAX
    if excess <= 0:
        return 0
    ids = [
        row[0]
        for row in conn.execute(
            _sql(
                schema,
                "SELECT id FROM {schema}.person_fact WHERE person_id = ? AND rejected_ts IS NULL "
                "AND source = 'auto' ORDER BY updated_ts, id LIMIT ?",
            ),
            (person_id, excess),
        )
    ]
    if ids:
        conn.execute(_sql(schema, "DELETE FROM {schema}.person_fact WHERE id IN ({m})", m=_in(ids)), ids)
    return len(ids)


# --- созревшие (служба) -----------------------------------------------------------------------------------


def start_ts(conn) -> int | None:
    """Метка старта фактов (`main.meta`): обмены до неё не «созревают» (старт без массового прогона)."""
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (START_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except ValueError, TypeError:
        return None


def ensure_start(conn, now_ms: int) -> int:
    """Записать метку старта, если её ещё нет (в писателе); вернуть действующую."""
    current = start_ts(conn)
    if current is not None:
        return current
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (START_KEY, str(now_ms)),
    )
    return now_ms


def retry_after_ms(fails: int | None) -> int:
    """П5: пауза до следующей попытки по (человек, файл)."""
    return RETRY_MS * 2 ** min(max(int(fails or 0), 0), RETRY_POW_MAX)


def due(conn, now_ms: int, limit: int, start: int) -> list[dict]:
    """Созревшие (человек, файл): `out.last_ts` по контактам человека в файле новее курсора (нет курсора —
    метки старта) и пауза с прошлой попытки прошла. Порядок — по `out_ts` от старых; не больше `limit`."""
    limit = max(1, min(int(limit), DUE_LIMIT_MAX))
    found: list[dict] = []
    for schema in SCHEMAS:
        rows = conn.execute(
            _sql(
                schema,
                "SELECT c.person_id, MAX(json_extract(s.stats_json, '$.out.last_ts')), st.cursor_ts, "
                "st.last_try_ts, st.fails "
                "FROM {schema}.contact c JOIN {schema}.contact_stats s ON s.contact_id = c.id "
                "JOIN main.person p ON p.id = c.person_id "
                "LEFT JOIN {schema}.person_fact_state st ON st.person_id = c.person_id "
                "WHERE c.person_id IS NOT NULL AND c.is_me = 0 GROUP BY c.person_id",
            )
        ).fetchall()
        for person_id, out_ts, cursor_ts, last_try_ts, fails in rows:
            if not isinstance(out_ts, int) or isinstance(out_ts, bool):
                continue
            if out_ts <= (cursor_ts if cursor_ts is not None else start):
                continue
            if last_try_ts is not None and now_ms - last_try_ts < retry_after_ms(fails):
                continue
            found.append({"person_id": person_id, "db": schema, "out_ts": out_ts})
    found.sort(key=lambda item: (item["out_ts"], item["person_id"], item["db"]))
    return found[:limit]


# --- контекст (служба) ------------------------------------------------------------------------------------


def _attachment_line(kind: str, attachments_json) -> str | None:
    """Вложение без текста — «[вложение: <тип>]»; ни текста, ни вложения — `None` (не берётся)."""
    try:
        items = json.loads(attachments_json) if attachments_json else []
    except ValueError, TypeError:
        items = []
    if isinstance(items, dict):
        items = [items]
    types = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                value = item.get("type")
                types.append(" ".join(str(value).split())[:30] if value else "файл")
    if types:
        return f"[вложение: {', '.join(types)}]"
    if kind == "file":
        return "[вложение: файл]"
    return None


def _texts(conn, schema: str, ids: list[int]) -> dict[int, str | None]:
    """Тексты сообщений по id (`substr` до `MESSAGE_CHARS`); очищенные — `None`, пустые — строка вложения."""
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id, substr(text, 1, {chars}), kind, attachments_json, purged FROM {schema}.message "
            "WHERE id IN ({m}) AND hidden = 0 AND deleted = 0",
            chars=str(MESSAGE_CHARS),
            m=_in(ids),
        ),
        ids,
    ).fetchall()
    out: dict[int, str | None] = {}
    for message_id, text, kind, attachments_json, purged in rows:
        if purged:
            out[message_id] = None
            continue
        value = (text or "").strip()
        out[message_id] = value or _attachment_line(kind, attachments_json)
    return out


def context(conn, person_id: int, schema: str, now_ms: int) -> dict:
    """Контекст службы по (человек, файл). Нет человека или у него нет контактов в этом файле — `NotFound`.

    П3: беседы — те, где у контактов человека есть сообщения за год; сообщения беседы грузятся за год, адресат
    (D1) решается на них (`people_stats._File.messages` — тот же разбор, что у статистики), и только потом —
    фильтр окна `(since_ts, until_ts]`. Обмен: чужие «Владельцу» от контактов человека и сообщения владельца ему. Бюджет
    `CONTEXT_CHARS` — новейшие, пока влезают (`truncated`)."""
    from . import people  # people импортирует этот модуль (хуки), здесь — только на вызове

    if schema not in SCHEMAS or not _person_exists(conn, person_id):
        raise NotFound(f"человек {person_id} не найден")
    contacts = people._load_contacts(  # общий загрузчик контактов людей (Ф21-1)
        conn, where="c.person_id = ?", params=(person_id,), schemas=(schema,), with_stats=True
    )
    if not contacts:
        raise NotFound(f"у человека {person_id} нет контактов в этом файле")
    ids = {c["id"] for c in contacts}
    state = _state(conn, schema, person_id) or {}
    since_ts = state.get("cursor_ts")
    if since_ts is None:
        since_ts = now_ms - BOOTSTRAP_MS
    until_ts = now_ms

    year_since = now_ms - stats_mod.WINDOW_MS
    contact_ids = sorted(ids)
    channel_ids = [
        row[0]
        for row in conn.execute(
            _sql(
                schema,
                "SELECT DISTINCT channel_id FROM {schema}.message "
                "WHERE sender_contact_id IN ({m}) AND ts >= ?",
                m=_in(contact_ids),
            ),
            [*contact_ids, year_since],
        )
    ]
    picked: list[tuple[int, int, str, int]] = []  # (ts, message_id, dir, channel_id)
    channels: dict[int, dict] = {}
    if channel_ids:
        rows = conn.execute(
            _sql(
                schema,
                "SELECT ch.id, ch.kind, ch.ext_id, ch.account_id, "
                "json_extract(ch.meta_json, '$.attrs.slack_type'), a.source, ch.title, ch.project_id, "
                "pr.name "
                "FROM {schema}.channel ch LEFT JOIN main.account a ON a.id = ch.account_id "
                "LEFT JOIN main.project pr ON pr.id = ch.project_id WHERE ch.id IN ({m}) ORDER BY ch.id",
                m=_in(channel_ids),
            ),
            channel_ids,
        ).fetchall()
        file = stats_mod._File(conn, schema, now_ms)  # разбор D1 статистики (П3)
        for row in rows:
            channel_id = row[0]
            channels[channel_id] = {"title": row[6], "kind": row[1], "project_id": row[7], "project": row[8]}
            msgs, _personal, _mail = file.messages(row[:6])
            for m in msgs:
                if not since_ts < m["ts"] <= until_ts:
                    continue
                if m["me"]:
                    if ids.isdisjoint(m["targets"]):
                        continue
                    direction = "out"
                elif m["to_me"] and m["sender"] in ids:
                    direction = "in"
                else:
                    continue
                picked.append((m["ts"], m["id"], direction, channel_id))

    picked.sort(key=lambda p: (p[0], p[1]), reverse=True)  # новейшие первыми
    chosen: list[dict] = []
    budget = CONTEXT_CHARS
    truncated = False
    for start in range(0, len(picked), TEXT_CHUNK):
        chunk = picked[start : start + TEXT_CHUNK]
        texts = _texts(conn, schema, [p[1] for p in chunk])
        for ts, message_id, direction, channel_id in chunk:
            text = texts.get(message_id)
            if not text:
                continue
            if len(text) > budget:
                truncated = True
                break
            budget -= len(text)
            channel = channels[channel_id]
            chosen.append(
                {
                    "ts": ts,
                    "dir": direction,
                    "channel_title": channel["title"],
                    "channel_kind": channel["kind"],
                    "project": channel["project"],
                    "text": text,
                    "_project_id": channel["project_id"],
                }
            )
        if truncated:
            break
    chosen.reverse()  # в выдаче — по времени
    project_ids = sorted({m.pop("_project_id") or 0 for m in chosen})
    # После ревью п.1 (П2): модель видит только факты, все проекты которых — среди проектов бесед окна (ручные
    # `[]` — всегда). Иначе факт проекта p3 пересказывался бы новым фактом с проектами окна p1 — токену p1.
    window = set(project_ids)
    shown = [
        f for f in _live_facts(conn, schema, person_id) if set(_project_ids(f["project_ids_json"])) <= window
    ]

    sources: list[dict] = []
    for contact in contacts:
        item = {
            "source": contact["source"],
            "name": contact["name"],
            "handle": contact["handle"],
            "email": contact["email"],
        }
        if item not in sources:
            sources.append(item)
    return {
        "person_id": person_id,
        "db": schema,
        "name": people.display_name(contacts),
        "sources": sources,
        "version": int(state.get("version") or 0),
        "since_ts": since_ts,
        "until_ts": until_ts,
        "facts": [
            {"id": f["id"], "text": f["text"], "source": f["source"], "updated_ts": f["updated_ts"]}
            for f in shown
        ],
        "rejected": _rejected_texts(conn, schema, person_id, REJECTED_SHOWN, within=window),
        "messages": chosen,
        "project_ids": project_ids,
        "truncated": truncated,
    }


# --- запись службы ------------------------------------------------------------------------------------------


def _check_project_ids(conn, values) -> list[int]:
    if not isinstance(values, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in values
    ):
        raise Invalid("project_ids: список целых")
    wanted = sorted({v for v in values if v != 0})
    if wanted:
        found = {
            row[0]
            for row in conn.execute(
                _sql("main", "SELECT id FROM main.project WHERE id IN ({m})", m=_in(wanted)), wanted
            )
        }
        if found != set(wanted):
            raise Invalid("project_ids: неизвестный проект")
    return sorted(set(values))


def apply(conn, person_id: int, schema: str, body: dict, now_ms: int) -> dict:
    """Запись службы одной транзакцией писателя (вызывать внутри `Db.write`). `body`: `version`, `cursor_ts`,
    `add`, `update` (`[{id, text}]`), `delete`, `failed`, `project_ids` (П2; обязателен при `add`/`update`).

    `failed` — только попытка (`last_try_ts`, `fails += 1`); на `FAILS_SKIP`-м сбое подряд курсор уходит на
    `cursor_ts` и `fails` обнуляется (П5, кусок пропущен). Иначе: версия не та — `Conflict` (ничего не
    тронуто); чужой/ручной/отклонённый id — `Invalid` целиком; дубли живых и отклонённых — молча мимо; предел
    50; курсор — вперёд, не дальше `now_ms`; `version += 1`."""
    if (
        schema not in SCHEMAS
        or not _person_exists(conn, person_id)
        or not _has_contacts(conn, schema, person_id)
    ):
        raise NotFound(f"человек {person_id} не найден")
    state = _state(conn, schema, person_id) or {
        "cursor_ts": None,
        "last_try_ts": None,
        "version": 0,
        "fails": 0,
        "updated_ts": None,
    }
    cursor_ts = body.get("cursor_ts")
    if not isinstance(cursor_ts, int) or isinstance(cursor_ts, bool):
        raise Invalid("cursor_ts: целое")
    cursor_new = max(0, min(cursor_ts, now_ms))

    if body.get("failed"):
        fails = int(state["fails"] or 0) + 1
        skipped = False
        if fails >= FAILS_SKIP:
            state["cursor_ts"] = _max(state["cursor_ts"], cursor_new)
            fails = 0
            skipped = True
        state["fails"] = fails
        state["last_try_ts"] = now_ms
        _put_state(conn, schema, person_id, state)
        return {"failed": True, "skipped": skipped, "fails": fails, "version": int(state["version"] or 0)}

    if body.get("version") != state["version"]:
        raise Conflict("версия фактов устарела")

    add = body.get("add") or []
    update = body.get("update") or []
    delete = body.get("delete") or []
    if not isinstance(add, list) or not isinstance(update, list) or not isinstance(delete, list):
        raise Invalid("add/update/delete: списки")
    if len(add) > ADD_MAX:
        raise Invalid(f"add: не больше {ADD_MAX}")
    add_texts = [normalize(text) for text in add]
    updates: dict[int, str] = {}
    for item in update:
        if not isinstance(item, dict):
            raise Invalid("update: [{id, text}]")
        fact_id = item.get("id")
        if not isinstance(fact_id, int) or isinstance(fact_id, bool):
            raise Invalid("update: id — целое")
        if fact_id in updates:
            raise Invalid(f"update: id {fact_id} дважды")
        updates[fact_id] = normalize(item.get("text"))
    deletes: list[int] = []
    for fact_id in delete:
        if not isinstance(fact_id, int) or isinstance(fact_id, bool):
            raise Invalid("delete: целые")
        if fact_id not in deletes:
            deletes.append(fact_id)
    both = set(updates) & set(deletes)
    if both:
        raise Invalid(f"id {min(both)} и в update, и в delete")
    live = _live_facts(conn, schema, person_id)
    live_auto = {f["id"]: f for f in live if f["source"] == "auto"}
    for fact_id in (*updates, *deletes):
        if fact_id not in live_auto:
            raise Invalid(f"факт {fact_id} — не живой авто-факт этого человека")
    project_ids: list[int] = []
    if add_texts or updates:
        if body.get("project_ids") is None:
            raise Invalid("project_ids: обязателен при add/update")
        project_ids = _check_project_ids(conn, body.get("project_ids"))
    elif body.get("project_ids") is not None:
        _check_project_ids(conn, body.get("project_ids"))

    rejected = {fold(text) for text in _rejected_texts(conn, schema, person_id)}
    if deletes:
        conn.execute(
            _sql(schema, "DELETE FROM {schema}.person_fact WHERE id IN ({m})", m=_in(deletes)), deletes
        )
    remaining = {f["id"]: fold(f["text"]) for f in live if f["id"] not in set(deletes)}
    updated = skipped = 0
    for fact_id, text in updates.items():
        key = fold(text)
        others = {v for k, v in remaining.items() if k != fact_id}
        if key in rejected or key in others:
            skipped += 1  # замена на отклонённое владельцем или на дубль другого живого — молча мимо
            continue
        merged = sorted(set(_project_ids(live_auto[fact_id]["project_ids_json"])) | set(project_ids))
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.person_fact SET text = ?, project_ids_json = ?, updated_ts = ? WHERE id = ?",
            ),
            (text, json.dumps(merged), now_ms, fact_id),
        )
        remaining[fact_id] = key
        updated += 1
    added = 0
    present = set(remaining.values())
    for text in add_texts:
        key = fold(text)
        if key in present or key in rejected:
            skipped += 1
            continue
        conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.person_fact (person_id, text, source, project_ids_json, created_ts, "
                "updated_ts) VALUES (?, ?, 'auto', ?, ?, ?)",
            ),
            (person_id, text, json.dumps(project_ids), now_ms, now_ms),
        )
        present.add(key)
        added += 1
    evicted = _evict(conn, schema, person_id)
    state.update(
        cursor_ts=_max(state["cursor_ts"], cursor_new),
        last_try_ts=now_ms,
        updated_ts=now_ms,
        fails=0,
        version=int(state["version"] or 0) + 1,
    )
    _put_state(conn, schema, person_id, state)
    return {
        "added": added,
        "updated": updated,
        "deleted": len(deletes),
        "evicted": evicted,
        "skipped": skipped,
        "version": state["version"],
    }


# --- правка владельца (только UI) ------------------------------------------------------------------------------


def _manual_schema(conn, person_id: int) -> str:
    """Файл ручного факта — файл первого контакта человека (сначала главный)."""
    if not _person_exists(conn, person_id):
        raise NotFound(f"человек {person_id} не найден")
    for schema in SCHEMAS:
        if _has_contacts(conn, schema, person_id):
            return schema
    raise NotFound(f"человек {person_id} не найден")


def add_manual(conn, person_id: int, text, now_ms: int) -> int:
    """Владелец добавил факт: `source = 'manual'`, `project_ids_json = '[]'`, затем предел 50, `version += 1`."""
    value = normalize(text)
    schema = _manual_schema(conn, person_id)
    fact_id = conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.person_fact (person_id, text, source, project_ids_json, created_ts, "
            "updated_ts) VALUES (?, ?, 'manual', '[]', ?, ?)",
        ),
        (person_id, value, now_ms, now_ms),
    ).lastrowid
    _evict(conn, schema, person_id)
    _bump_version(conn, schema, person_id)
    return fact_id


def _own_fact(conn, person_id: int, fact_id: int) -> str:
    """Файл живого факта этого человека; чужой, отклонённый или несуществующий — `NotFound`."""
    schema = db_of(fact_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.person_fact WHERE id = ? AND person_id = ? AND rejected_ts IS NULL",
        ),
        (fact_id, person_id),
    ).fetchone()
    if row is None or not _person_exists(conn, person_id):
        raise NotFound(f"факт {fact_id} не найден")
    return schema


def edit(conn, person_id: int, fact_id: int, text, now_ms: int) -> None:
    """Владелец поправил факт: текст, `source = 'manual'` (служба его больше не трогает), `version += 1`. Проекты
    источника остаются (граница П2 для правленого авто-факта не снимается)."""
    value = normalize(text)
    schema = _own_fact(conn, person_id, fact_id)
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.person_fact SET text = ?, source = 'manual', updated_ts = ? WHERE id = ?",
        ),
        (value, now_ms, fact_id),
    )
    _bump_version(conn, schema, person_id)


def reject(conn, person_id: int, fact_id: int, now_ms: int) -> None:
    """Владелец убрал факт: `rejected_ts` — не показывается, модель видит его как «отклонено», `version += 1`."""
    schema = _own_fact(conn, person_id, fact_id)
    conn.execute(
        _sql(schema, "UPDATE {schema}.person_fact SET rejected_ts = ? WHERE id = ?"), (now_ms, fact_id)
    )
    _bump_version(conn, schema, person_id)


# --- чтение для карточки -----------------------------------------------------------------------------------


def _visible(principal: Principal, project_ids: list[int]) -> bool:
    """П2: UI и токен на все проекты — все факты; токен со списком проектов — только факты, у которых КАЖДЫЙ
    проект из его списка (`0` — беседа без проекта — нет ни в одном списке); ручные (`[]`) — всем."""
    allowed = principal.project_ids
    return allowed is None or all(pid in allowed for pid in project_ids)


def card_facts(conn, principal: Principal, person_id: int) -> dict:
    """`facts` карточки: живые факты обоих файлов (новые первыми), видимые токену; `updated_ts` — последнее
    применение службой (нет — последняя правка факта)."""
    items: list[dict] = []
    updated: list[int] = []
    for schema in SCHEMAS:
        for fact in _live_facts(conn, schema, person_id):
            if not _visible(principal, _project_ids(fact["project_ids_json"])):
                continue
            items.append(
                {
                    "id": fact["id"],
                    "text": fact["text"],
                    "source": fact["source"],
                    "updated_ts": fact["updated_ts"],
                }
            )
        state = _state(conn, schema, person_id)
        if state is not None and state["updated_ts"] is not None:
            updated.append(state["updated_ts"])
    items.sort(key=lambda f: (-f["updated_ts"], -f["id"]))
    stamp = max(updated) if updated else _max(*(f["updated_ts"] for f in items))
    return {"items": items, "count": len(items), "updated_ts": stamp}


# --- хуки людей (в транзакции писателя) ---------------------------------------------------------------------


def move(conn, src: int, dst: int) -> None:
    """Факты и состояние `src` -> `dst` в каждом файле (П1: объединение и автосклейка по почте): курсор — max,
    версия — больше обеих + 1, `fails` — 0, затем предел 50."""
    if src == dst:
        return
    for schema in SCHEMAS:
        moved = conn.execute(
            _sql(schema, "UPDATE {schema}.person_fact SET person_id = ? WHERE person_id = ?"), (dst, src)
        ).rowcount
        source = _state(conn, schema, src)
        if source is None and not moved:
            continue
        target = _state(conn, schema, dst) or {}
        source = source or {}
        merged = {
            "cursor_ts": _max(source.get("cursor_ts"), target.get("cursor_ts")),
            "last_try_ts": _max(source.get("last_try_ts"), target.get("last_try_ts")),
            "version": max(int(source.get("version") or 0), int(target.get("version") or 0)) + 1,
            "fails": 0,
            "updated_ts": _max(source.get("updated_ts"), target.get("updated_ts")),
        }
        conn.execute(_sql(schema, "DELETE FROM {schema}.person_fact_state WHERE person_id = ?"), (src,))
        _put_state(conn, schema, dst, merged)
        _evict(conn, schema, dst)


def remove_orphans(conn) -> int:
    """Уборка B4: факты и состояния людей, которых больше нет (оба файла). Число удалённых фактов."""
    removed = 0
    for schema in SCHEMAS:
        removed += conn.execute(
            _sql(
                schema,
                "DELETE FROM {schema}.person_fact WHERE person_id NOT IN (SELECT id FROM main.person)",
            )
        ).rowcount
        conn.execute(
            _sql(
                schema,
                "DELETE FROM {schema}.person_fact_state WHERE person_id NOT IN (SELECT id FROM main.person)",
            )
        )
    return removed
