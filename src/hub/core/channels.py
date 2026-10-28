"""Записи каналов для пишущих ручек `/channels/{id}/read` и `PATCH /channels/{id}`. Спецификация:
docs/architecture.mdb. Вызывать только внутри `Db.write()`, время — параметром `now_ms`, как
везде в core. Схема файла — `db_of(channel_id)`. Видимость канала принципалу проверяется ДО вызова этих
функций (в `queries.channel_visible`, в маршруте) — здесь её больше не проверяем.
"""

import json

from . import agent_items, draft_channels, mapping
from . import attention as attention_mod
from . import outbox as outbox_mod
from . import stages as stages_mod
from . import threads as threads_mod
from .db import db_of, next_seq

_UNSET = object()


class ChannelError(Exception):
    """Базовая ошибка записи канала."""


class NotFound(ChannelError):
    """Канал не найден, сообщение не найдено, либо сообщение принадлежит другому каналу; либо указанный
    `project_id` не существует.
    """


class InvalidReplyTarget(ChannelError):
    """Раздел E спеки 2b: канальная `read` с id ответа, который НЕ `thread_broadcast`, — маркер канала не
    должен улетать вперёд позиции сообщения, которого нет в основной ленте. 422."""


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в outbox.py/queries.py: схема и доверенные фрагменты подставляются здесь, а не
    конкатенацией прямо в вызове conn.execute (иначе ruff S608 не отличит это от пользовательского ввода).
    """
    return template.format(schema=schema, **parts)


def bot_meta(source: str | None, kind: str | None, meta_json: str | None) -> bool:
    """Ф63-2 П2: признак беседы с ботом — источник `telegram`, вид `dm`, `attrs.tg_type == "bot"` (ставит
    коннектор Telegram и ядро при привязке заготовки, `draft_channels.bind_after_sent`). Никогда по названию
    беседы или нику."""
    if source != "telegram" or kind != "dm" or not meta_json:
        return False
    try:
        meta = json.loads(meta_json)
    except ValueError, TypeError:
        return False
    attrs = meta.get("attrs") if isinstance(meta, dict) else None
    return isinstance(attrs, dict) and attrs.get("tg_type") == "bot"


def is_bot_channel(conn, channel_id: int) -> bool:
    """Ф63-2 П2: настоящая личка Telegram с ботом (заготовки `new:` — `draft_channels.is_bot_draft`)."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT a.source, c.kind, c.meta_json FROM {schema}.channel c "
            "JOIN main.account a ON a.id = c.account_id WHERE c.id = ?",
        ),
        (channel_id,),
    ).fetchone()
    return row is not None and bot_meta(row[0], row[1], row[2])


def advance_read_marker(
    conn, schema: str, channel_id: int, *, ts: int, ext_sort: int, ext_id: str, first_seq: int
) -> bool:
    """Позиционный маркер прочитанного (находка F8 второго круга критика куска 1b): `last_read_seq =
    MAX(first_seq)` ломался, когда СТАРОЕ по времени сообщение вставлялось ПОЗЖЕ нового (стартовая загрузка +
    живое, догон после сна, окно перечитывания) — новое получало БОЛЬШИЙ `first_seq`, чем уже прочитанное
    более старое, и ошибочно считалось прочитанным. Маркер двигается СТРОГО ВПЕРЁД по позиции `(ts, ext_sort,
    ext_id)` — row-value сравнение SQLite, независимо от порядка вставки строк в БД. Общая точка для
    `mark_read` (UI, по `up_to_message_id`) и события `read` (`ingest._apply_read`).

    `last_read_seq` продолжает писаться (`MAX(старое, first_seq)`) — только ради обратной совместимости
    `/sync`/UI (клиенты, ожидающие его роста); реальные подсчёты непрочитанного (`ingest.unread_count`,
    `queries.list_projects`) на него больше не опираются. `channel.seq` растёт, только если маркер реально
    сдвинулся. Возвращает True, если что-то изменилось.
    """
    row = conn.execute(
        _sql(
            schema,
            "SELECT last_read_ts, last_read_sort, last_read_ext, last_read_seq FROM {schema}.channel "
            "WHERE id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"канал {channel_id} не найден")
    old_ts, old_sort, old_ext, old_seq = row
    # Заготовка outbox (своя, ещё не отправленная строка) — `ext_id IS NULL`: позиция с пустой строкой
    # сравнима с любой другой (иначе две заготовки в одну миллисекунду дают TypeError на None <= None).
    ext_id = ext_id or ""
    old_ext = old_ext or ""

    if old_ts is not None and (ts, ext_sort, ext_id) <= (old_ts, old_sort, old_ext):
        return False

    new_last_read_seq = max(old_seq or 0, first_seq)
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.channel SET last_read_ts = ?, last_read_sort = ?, last_read_ext = ?, "
            "last_read_seq = ? WHERE id = ?",
        ),
        (ts, ext_sort, ext_id, new_last_read_seq, channel_id),
    )
    seq = next_seq(conn)
    conn.execute(_sql(schema, "UPDATE {schema}.channel SET seq = ? WHERE id = ?"), (seq, channel_id))
    return True


def mark_read(conn, channel_id: int, *, up_to_message_id: int, now_ms: int) -> bool:
    """Позиция вперёд по `(ts, ext_sort, ext_id)` (см. `advance_read_marker`). Возвращает True, если
    что-то изменилось. Раздел E спеки 2b: id ответа, который НЕ `thread_broadcast`, — `InvalidReplyTarget`
    (маршрут переводит в 422): иначе маркер канала улетел бы вперёд позиции сообщения, которого нет в
    основной ленте (родитель допустим — он «не ответ»)."""
    del now_ms  # самой отметке время не нужно — параметр ради симметрии с остальными функциями core
    schema = db_of(channel_id)
    msg_row = conn.execute(
        _sql(
            schema,
            "SELECT channel_id, first_seq, ts, ext_sort, ext_id, thread_ext_id, thread_broadcast "
            "FROM {schema}.message WHERE id = ?",
        ),
        (up_to_message_id,),
    ).fetchone()
    if msg_row is None or msg_row[0] != channel_id:
        raise NotFound(f"message {up_to_message_id} не принадлежит каналу {channel_id}")
    _channel_id, first_seq, ts, ext_sort, ext_id, thread_ext_id, thread_broadcast = msg_row
    if threads_mod.is_reply(thread_ext_id, ext_id) and not thread_broadcast:
        raise InvalidReplyTarget(
            f"message {up_to_message_id}: ответ, не показанный в ленте канала (не broadcast)"
        )
    return advance_read_marker(
        conn, schema, channel_id, ts=ts, ext_sort=ext_sort, ext_id=ext_id, first_seq=first_seq
    )


def update_channel(
    conn,
    channel_id: int,
    *,
    project_id=_UNSET,
    muted=_UNSET,
    archived=_UNSET,
    map_source=_UNSET,
    attention=_UNSET,
    stage=_UNSET,
    stage_note=_UNSET,
    now_ms: int,
) -> int:
    """Обновляет только заданные поля. Смена `project_id` -> `map_source='manual', map_reason='manual'`;
    новый `project_id` обязан существовать в `main.project` либо быть `None` («Неразобранное»). `map_source`
    принимает только `"auto"` (раздел B спеки куска 1a: возврат в авто, немедленный пересчёт правилами) —
    любое другое значение здесь бессмысленно, `manual` выставляется только сменой `project_id`. Оба поля
    исключают друг друга по смыслу — если пришли вместе, `project_id` применяется первым, `map_source='auto'`
    его тут же перезатирает пересчётом (последний в запросе побеждает).

    Смена проекта (руками или пересчётом) каскадом раскладывает авто-топики этого канала — он мог быть
    форумом (раздел B, «Пересчёт»). `now_ms` нужен только пересчёту.

    Ф8-3 A6: `attention` — ручной режим внимания (`react|info`) или `None` («авто»); смена
    двигает `channel.seq`, как `muted`.

    Ф11-9c (H1): `archived=True` закрывает открытые карточки разборщика беседы (`new`/`seen` -> `done`) в той
    же транзакции; возврат из архива их не открывает. Возвращает число закрытых карточек.

    Ф34-1 (решение 1, П3, П6): `stage` — стадия беседы (до 40 символов после `strip`, без переводов строк;
    `None`/пустая — снять, вместе с заметкой), `stage_note` — заметка к стадии (до 200; `None`/пустая —
    снять; без стадии — `ChannelError`). `stage_ts` двигается только при смене стадии, `channel.seq` — при
    любом изменении стадии или заметки (запрос с теми же значениями ничего не пишет).
    """
    schema = db_of(channel_id)

    if map_source is not _UNSET and map_source != "auto":
        raise ChannelError(f"map_source: поддерживается только 'auto', получено {map_source!r}")
    if attention is not _UNSET and attention is not None and attention not in attention_mod.MODES:
        raise ChannelError(f"attention: неизвестный режим {attention!r}")

    sets: list[str] = []
    params: list = []
    if project_id is not _UNSET:
        if project_id is not None:
            exists = conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone()
            if exists is None:
                raise NotFound(f"project_id {project_id} не существует")
        sets.append("project_id = ?")
        params.append(project_id)
        sets.append("map_source = 'manual'")
        sets.append("map_reason = 'manual'")
    if muted is not _UNSET:
        # Ф99-2 П5: любой путь, кроме `hub_mute` (`core/mute.py`), снимает пометку «заглушила сессия».
        sets.append("muted = ?, muted_by = NULL")
        params.append(1 if muted else 0)
    if archived is not _UNSET:
        sets.append("archived = ?")
        params.append(1 if archived else 0)
    if attention is not _UNSET:
        sets.append("attention = ?")
        params.append(attention)

    row = conn.execute(
        _sql(schema, "SELECT id, stage, stage_note FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"канал {channel_id} не найден")

    if stage is not _UNSET or stage_note is not _UNSET:
        _id, old_stage, old_note = row
        try:
            new_stage, new_key = stages_mod.stage_form(stage) if stage is not _UNSET else (old_stage, None)
            new_note = stages_mod.note_form(stage_note) if stage_note is not _UNSET else old_note
        except stages_mod.StageError as exc:
            raise ChannelError(str(exc)) from exc
        if new_stage is None:
            if stage_note is not _UNSET and new_note is not None:
                raise ChannelError("stage_note: заметка без стадии — сначала поставь стадию")
            new_note = None  # П6: снятие стадии чистит заметку
        elif (
            stage is not _UNSET
            and stage_note is _UNSET
            and new_key != stages_mod.stage_key(old_stage or "")
        ):
            # Заметка относится к стадии («догон до 07.10»): смена стадии без новой заметки её чистит.
            new_note = None
        if new_stage != old_stage:
            sets.append("stage = ?, stage_key = ?, stage_ts = ?")
            params.extend((new_stage, new_key, now_ms if new_stage is not None else None))
        if new_note != old_note:
            sets.append("stage_note = ?")
            params.append(new_note)

    if sets:
        seq = next_seq(conn)
        sets.append("seq = ?")
        params.append(seq)
        params.append(channel_id)
        query = _sql(schema, "UPDATE {schema}.channel SET {sets} WHERE id = ?", sets=", ".join(sets))
        conn.execute(query, params)

    if map_source is not _UNSET:
        conn.execute(
            _sql(schema, "UPDATE {schema}.channel SET map_source = 'auto' WHERE id = ?"), (channel_id,)
        )
        mapping.recompute_channel(conn, schema, channel_id, now_ms)

    if project_id is not _UNSET or map_source is not _UNSET:
        mapping.recompute_topics(conn, schema, channel_id, now_ms)

    if archived is not _UNSET and archived:
        # Ревью Ф34-3: заготовка по номеру телефона в архив руками — её неподтверждённые и ещё не выдававшиеся
        # строки отменяются, номер стирается (выданная строка доживает, номер сотрёт её исход).
        cancellable = draft_channels.manual_archive_cancellable(conn, schema, channel_id)
        if cancellable is not None:
            for outbox_id in cancellable:
                outbox_mod.cancel(conn, outbox_id, now_ms=now_ms)
            draft_channels.erase_phone_if_idle(conn, schema, channel_id)
        return agent_items.close_open_cards(conn, schema, channel_id)
    return 0
