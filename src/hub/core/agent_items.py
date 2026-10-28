"""Карточки дежурного агента (`agent_item`): запись. Спека `docs/architecture.md`, A1–A2 и поправки
П2 (слияние открытой карточки), П4 (стёртая ретенцией карточка), П5 (`last_message_id` по позиции), П10/К3
(идемпотентность по позиции).

Вызывается ТОЛЬКО внутри `Db.write()` (как `outbox.py`/`channels.py`) — принимает открытое `conn`, время
параметром `now_ms`. Видимость канала и карточки для принципала проверяет маршрут (`queries.channel_visible`,
`queries.agent_item_channel`) в той же транзакции. Текстов сообщений модуль не читает — только позиции.
"""

import json
from typing import NamedTuple

from . import threads as threads_mod
from .db import db_of

LABELS = ("noise", "fyi", "reply", "urgent")  # по возрастанию важности (П2: метка не понижается)
_LABEL_RANK = {label: rank for rank, label in enumerate(LABELS)}
STATUSES = ("new", "seen", "done", "dismissed")
RULES_PROVIDER = "rules"  # карточка по правилам (Ф57-1: других карточек разборщик не пишет)
MAX_MESSAGE_IDS = 200
# Мелочи поправок: переходы статуса. Из done/dismissed — никуда.
_TRANSITIONS = {"new": frozenset({"seen", "done", "dismissed"}), "seen": frozenset({"done", "dismissed"})}


# Ф11-2 A1: ОДНО определение «отвечено / прочитано» карточки — для выдачи (`queries._AGENT_ITEMS_TEMPLATE`) и
# для записи (`upsert`, `close_answered`). Фрагменты SQL с `{schema}` и алиасами: `a` — карточка, `c` — её
# беседа, `lm` — последнее сообщение карточки, `t` — тред `lm` (LEFT JOIN по `lm.thread_ext_id`).
# П5: «ответил» — в беседе есть МОЁ (не заготовка, не удалённое) сообщение с позицией после последнего
# сообщения карточки; «прочитал» — маркер беседы (для ответа в треде, не broadcast, — маркер треда) не
# ниже последнего сообщения карточки. Оба вычисляются при чтении, в ингесте ничего не пишется.
_REPLY_PRED_SQL = threads_mod.is_reply_sql("lm.thread_ext_id", "lm.ext_id")
# Ф37-1 (ревью): сообщение, ушедшее без подтверждения владельца (`outbox.auto_role`), — ответ сессии одному из
# участников, а не «Владелец ответил»: карточку чужого вопроса к нему самому оно не закрывает и сигналы подписок
# не снимает. Разобранность таких бесед отмечает сама сессия (`hub_handled`, Ф37-3).
ANSWERED_SQL = (
    "EXISTS (SELECT 1 FROM {schema}.message me WHERE me.channel_id = a.channel_id AND me.is_me = 1 "
    "  AND me.ext_id IS NOT NULL AND me.deleted = 0 AND me.hidden = 0 "
    "  AND (me.ts, me.ext_sort, me.id) > (lm.ts, lm.ext_sort, lm.id) "
    "  AND NOT EXISTS (SELECT 1 FROM {schema}.outbox ao WHERE ao.id = me.outbox_id "
    "                  AND ao.auto_role IS NOT NULL))"
)
READ_SQL = (
    "CASE WHEN " + _REPLY_PRED_SQL + " AND lm.thread_broadcast = 0 "
    "  THEN (t.last_read_ts IS NOT NULL AND (t.last_read_ts, t.last_read_sort, t.last_read_ext) >= "
    "        (lm.ts, lm.ext_sort, COALESCE(lm.ext_id, ''))) "
    "  ELSE (c.last_read_ts IS NOT NULL AND (c.last_read_ts, c.last_read_sort, c.last_read_ext) >= "
    "        (lm.ts, lm.ext_sort, COALESCE(lm.ext_id, ''))) END"
)
_STATE_FROM = (
    "FROM {schema}.agent_item a "
    "JOIN {schema}.channel c ON c.id = a.channel_id "
    "JOIN {schema}.message lm ON lm.id = a.last_message_id "
    "LEFT JOIN {schema}.thread t ON t.channel_id = lm.channel_id AND t.ext_id = lm.thread_ext_id "
)


def card_state(conn, schema: str, item_id: int) -> tuple[bool, bool]:
    """Ф11-2 A2: `(answered, read)` карточки — те же выражения, что в выдаче. Нет последнего сообщения
    (строки не нашлось) — `(False, False)`."""
    row = conn.execute(
        _sql(schema, "SELECT " + ANSWERED_SQL + ", " + READ_SQL + " " + _STATE_FROM + "WHERE a.id = ?"),
        (item_id,),
    ).fetchone()
    if row is None:
        return False, False
    return bool(row[0]), bool(row[1])


class AgentItemError(Exception):
    """Базовая ошибка карточки; текст — для журнала и ответа, без текстов сообщений."""


class InvalidMessages(AgentItemError):
    """`message_ids` не сообщения этого канала (или из другого файла) — 422."""


class NotFound(AgentItemError):
    """Карточки нет — 404."""


class InvalidTransition(AgentItemError):
    """Недопустимый переход статуса — 409."""


class UpsertResult(NamedTuple):
    item_id: int
    created: bool
    noop: bool
    closed_item_id: int | None = None  # Ф11-2 A2: открытая карточка, закрытая при разделении
    closed_status: str | None = None  # `done` (отвечена) | `seen` (прочитана)
    status: str = "new"  # Ф11-9 H4: статус итоговой карточки (новая, рождённая отвеченной, — `done`)


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в ingest.py/outbox.py — см. комментарий в ingest.py."""
    return template.format(schema=schema, **parts)


def _messages(conn, schema: str, channel_id: int, message_ids: list[int]) -> dict[int, tuple[tuple, int]]:
    """Сообщения ЭТОГО канала из переданного списка (чужих в словаре нет): `id -> (позиция (ts, ext_sort, id),
    first_seq)`. Позиция — для порядка `message_ids_json` и `last_message_id` (П5), `first_seq` — для
    покрытия (`covered_seq`)."""
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id, ts, ext_sort, first_seq FROM {schema}.message WHERE channel_id = ? AND id IN ({ph})",
            ph=placeholders,
        ),
        [channel_id, *message_ids],
    ).fetchall()
    return {mid: ((ts, ext_sort, mid), first_seq) for mid, ts, ext_sort, first_seq in rows}


def _newest_card_covered(conn, schema: str, channel_id: int) -> tuple[int, int, str] | None:
    """К3 (после приёмки, F1): самая свежая по `covered_seq` карточка беседы (любой статус) — `(id,
    covered_seq, status)`. Покрытие — по порядку поступления в хаб (`first_seq`), а не по времени источника:
    догон после сна, пришедший позже живого сообщения, остаётся непокрытым, хотя его `ts` старше."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT id, covered_seq, status FROM {schema}.agent_item WHERE channel_id = ? "
            "ORDER BY covered_seq DESC, id DESC LIMIT 1",
        ),
        (channel_id,),
    ).fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


def max_label(a: str, b: str) -> str:
    return a if _LABEL_RANK[a] >= _LABEL_RANK[b] else b


def upsert(
    conn,
    *,
    channel_id: int,
    message_ids: list[int],
    label: str,
    summary: str,
    action: str | None,
    provider: str,
    model: str,
    api_token_id: int | None,
    now_ms: int,
) -> UpsertResult:
    """`POST /agent/items`: одна транзакция.

    1. Все `message_ids` — сообщения ЭТОГО канала в ЕГО файле (id из другого файла сюда не попадёт: поиск идёт
       в схеме канала) — иначе `InvalidMessages`, ничего не записано.
    2. К3 (после приёмки, F1): у ВСЕХ `message_ids` `first_seq` ≤ `covered_seq` самой свежей по
       `covered_seq` карточки беседы (любой статус) -> no-op (`noop=True`, id этой карточки).
    3. Открытая карточка (`status='new'`) отвечена -> `done`, прочитана без ответа -> `seen` (Ф11-2 A2;
       `answered`/`read` — те же выражения, что в выдаче) и новая карточка из переданных `message_ids`
       (у прочитанной — метка `max_label(старая, переданная)`); `closed_item_id` — закрытая.
       Иначе открытая карточка есть -> слияние (П2): метка — максимум по важности; содержание, действие
       и провайдер — переданные (Ф57-1 П6: карточка по правилам затирает и пересказ прежней модели);
       `message_ids` — объединение, последние 200 по позиции; `last_message_id` — самое новое по
       позиции; `covered_seq` — максимум старого и нового. Открытая, но стёртая ретенцией (П4, страховка —
       ретенция и так её закрывает) -> закрыть и создать новую.
    4. Иначе — новая карточка. Ф11-9 H4: новая карточка сразу проверяется `card_state` — уже отвечена
       (моё сообщение позже её последнего сообщения: кандидаты копились до ответа, карточка родилась после) ->
       `done`; прочитанная без ответа остаётся `new` (Ф11-2 A2). `status` результата — итоговый статус.
    """
    if label not in _LABEL_RANK:
        raise AgentItemError("неизвестная метка")
    ids = sorted(set(message_ids))
    if not 1 <= len(ids) <= MAX_MESSAGE_IDS:
        raise InvalidMessages(f"message_ids: от 1 до {MAX_MESSAGE_IDS}")
    schema = db_of(channel_id)
    if any(db_of(mid) != schema for mid in ids):
        raise InvalidMessages("message_ids: сообщения другого файла")
    found = _messages(conn, schema, channel_id, ids)
    if len(found) != len(ids):
        raise InvalidMessages("message_ids: не сообщения этого канала")
    positions = {mid: pos for mid, (pos, _first_seq) in found.items()}
    covered_seq = max(first_seq for _pos, first_seq in found.values())

    newest = _newest_card_covered(conn, schema, channel_id)
    if newest is not None and covered_seq <= newest[1]:
        return UpsertResult(newest[0], created=False, noop=True, status=newest[2])

    open_row = conn.execute(
        _sql(
            schema,
            "SELECT id, label, provider, message_ids_json, purged, covered_seq FROM {schema}.agent_item "
            "WHERE channel_id = ? AND status = 'new'",
        ),
        (channel_id,),
    ).fetchone()
    if open_row is not None and open_row[4]:
        conn.execute(
            _sql(schema, "UPDATE {schema}.agent_item SET status = 'dismissed' WHERE id = ?"), (open_row[0],)
        )
        open_row = None

    # Ф11-2 A2: граница карточки — ответ или прочтение владельца. Отвеченная -> `done`, прочитанная без ответа ->
    # `seen` (метка новой — не ниже старой: нужда отвечать от прочтения не пропадает); новые сообщения —
    # новая карточка. Иначе — слияние, как раньше.
    closed_item_id: int | None = None
    closed_status: str | None = None
    if open_row is not None:
        answered, read = card_state(conn, schema, open_row[0])
        if answered or read:
            closed_item_id, closed_status = open_row[0], ("done" if answered else "seen")
            if not answered:
                label = max_label(open_row[1], label)
            conn.execute(
                _sql(schema, "UPDATE {schema}.agent_item SET status = ? WHERE id = ?"),
                (closed_status, closed_item_id),
            )
            open_row = None

    if open_row is None:
        ordered = sorted(positions, key=positions.__getitem__)[-MAX_MESSAGE_IDS:]
        cur = conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.agent_item (channel_id, label, summary, action, message_ids_json, "
                "last_message_id, covered_seq, status, provider, model, api_token_id, created_ts, "
                "updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)",
            ),
            (
                channel_id,
                label,
                summary,
                action,
                json.dumps(ordered),
                ordered[-1],
                covered_seq,
                provider,
                model,
                api_token_id,
                now_ms,
                now_ms,
            ),
        )
        new_id = cur.lastrowid
        status = "new"
        answered, _read = card_state(conn, schema, new_id)
        if answered:
            status = "done"
            conn.execute(
                _sql(schema, "UPDATE {schema}.agent_item SET status = 'done' WHERE id = ?"), (new_id,)
            )
        return UpsertResult(
            new_id,
            created=True,
            noop=False,
            closed_item_id=closed_item_id,
            closed_status=closed_status,
            status=status,
        )

    item_id, old_label, _old_provider, old_ids_json, _purged, old_covered_seq = open_row
    covered_seq = max(covered_seq, old_covered_seq)
    try:
        old_ids = [int(x) for x in json.loads(old_ids_json)]
    except ValueError, TypeError:
        old_ids = []
    merged_ids = sorted(set(old_ids) | set(ids))
    all_positions = {mid: pos for mid, (pos, _fs) in _messages(conn, schema, channel_id, merged_ids).items()}
    ordered = sorted(all_positions, key=all_positions.__getitem__)[-MAX_MESSAGE_IDS:]
    new_label = max_label(old_label, label)

    # Ф57-1 П6: карточка всегда перезаписывает пересказ открытой — карточки ставятся только по правилам, и
    # открытая карточка прежней модели иначе навсегда осталась бы со старым пересказом.
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.agent_item SET label = ?, summary = ?, action = ?, message_ids_json = ?, "
            "last_message_id = ?, covered_seq = ?, provider = ?, model = ?, api_token_id = ?, "
            "updated_ts = ? WHERE id = ?",
        ),
        (
            new_label,
            summary,
            action,
            json.dumps(ordered),
            ordered[-1],
            covered_seq,
            provider,
            model,
            api_token_id,
            now_ms,
            item_id,
        ),
    )
    return UpsertResult(item_id, created=False, noop=False)


def set_status(conn, item_id: int, *, status: str) -> str:
    """`POST /agent/items/{id}/status`: `new -> seen|done|dismissed`, `seen -> done|dismissed`; из
    `done`/`dismissed` — никуда (`InvalidTransition`, 409). `new` целевым статусом не бывает (422 — в модели
    маршрута). Видимость проверяет маршрут в той же транзакции."""
    schema = db_of(item_id)
    row = conn.execute(
        _sql(schema, "SELECT status FROM {schema}.agent_item WHERE id = ?"), (item_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"карточка {item_id} не найдена")
    current = row[0]
    if status not in _TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"{current} -> {status}")
    conn.execute(_sql(schema, "UPDATE {schema}.agent_item SET status = ? WHERE id = ?"), (status, item_id))
    return status


def set_channel_status(conn, channel_id: int, *, status: str) -> list[int]:
    """Ф37-2 B1 (`POST /agent/items/channel-status`): все открытые карточки беседы — одним вызовом, без
    номеров. Переходы те же, что у `set_status`: в `done`/`dismissed` — из `new` и `seen`, в `seen` — только
    из `new`; закрытые (`done`/`dismissed`) не трогаются. Видимость беседы проверяет маршрут в той же
    транзакции. Возвращает id изменённых карточек по возрастанию."""
    schema = db_of(channel_id)
    sources = sorted(current for current, targets in _TRANSITIONS.items() if status in targets)
    if not sources:
        raise InvalidTransition(f"-> {status}")
    marks = ", ".join("?" for _ in sources)
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id FROM {schema}.agent_item WHERE channel_id = ? AND status IN ({marks}) ORDER BY id",
            marks=marks,
        ),
        (channel_id, *sources),
    ).fetchall()
    ids = [row[0] for row in rows]
    for item_id in ids:
        conn.execute(
            _sql(schema, "UPDATE {schema}.agent_item SET status = ? WHERE id = ?"), (status, item_id)
        )
    return ids


def close_open_cards(conn, schema: str, channel_id: int) -> int:
    """Ф11-9c (H1): беседа ушла в архив — её открытые карточки (`new`/`seen`) -> `done` в той же транзакции
    (разборщик архивные беседы не разбирает, карточки устарели). `done`/`dismissed` не трогает. Интерфейс
    узнаёт, как при `close_answered`, — по `Db.on_commit` транзакции записи. Возвращает число закрытых."""
    cur = conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.agent_item SET status = 'done' "
            "WHERE channel_id = ? AND status IN ('new', 'seen')",
        ),
        (channel_id,),
    )
    return cur.rowcount


def close_cards_up_to(conn, schema: str, channel_id: int, *, ts: int, ext_sort: int, message_id: int) -> int:
    """Ф37-3 B2.2 (ревью): «разобрано до сообщения» закрывает только карточки, чьё последнее сообщение не
    позже него (та же позиция, что в `ANSWERED_SQL`); карточка на более новом сообщении остаётся открытой.
    Возвращает число закрытых."""
    cur = conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.agent_item SET status = 'done' "
            "WHERE channel_id = ? AND status IN ('new', 'seen') "
            "AND EXISTS (SELECT 1 FROM {schema}.message lm WHERE lm.id = agent_item.last_message_id "
            "            AND (lm.ts, lm.ext_sort, lm.id) <= (?, ?, ?))",
        ),
        (channel_id, ts, ext_sort, message_id),
    )
    return cur.rowcount


def close_answered(conn, channel_id: int) -> list[int]:
    """Ф11-2 A3 (`POST /agent/items/close-answered`): открытая карточка беседы отвечена (то же выражение, что
    в выдаче) -> `done`; иначе ничего. Прочие статусы не трогает. Видимость беседы проверяет маршрут в той же
    транзакции. Возвращает id закрытых."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.agent_item WHERE channel_id = ? AND status = 'new'"),
        (channel_id,),
    ).fetchone()
    if row is None:
        return []
    answered, _read = card_state(conn, schema, row[0])
    if not answered:
        return []
    conn.execute(_sql(schema, "UPDATE {schema}.agent_item SET status = 'done' WHERE id = ?"), (row[0],))
    return [row[0]]
