"""Единый предикат треда и сводка треда Slack. Спецификация: `docs/architecture.md`,
раздел «Модель тредов» + разделы D/E. ОДИН модуль — SQL-фрагменты и Python-версия предиката больше нигде не
переписываются вручную.

Тред Slack — НЕ отдельный канал: ответы живут в канале родителя с `message.thread_ext_id = <ext_id родителя>`.
`is_reply := thread_ext_id IS NOT NULL AND thread_ext_id IS NOT ext_id` (именно `IS NOT` — у заготовки outbox
`ext_id` NULL, `<>` дал бы NULL, а не истину). `top_level := NOT is_reply OR thread_broadcast = 1`.

Вызывается ТОЛЬКО внутри `Db.write()` (как `ingest.py`/`outbox.py`/`channels.py`) — принимает уже открытое
`conn`, время параметром `now_ms`, схему (`main`/`work`) параметром `schema`.
"""

from . import extsort
from .db import next_seq


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в ingest.py/outbox.py/channels.py/mapping.py — см. комментарий в ingest.py."""
    return template.format(schema=schema, **parts)


# --- Единый предикат (Python + SQL-фрагменты) ---------------------------------------------------------------


def is_reply(thread_ext_id: str | None, ext_id: str | None) -> bool:
    """`is_reply := thread_ext_id IS NOT NULL AND thread_ext_id IS NOT ext_id` — версия на значениях Python
    (`IS NOT` в SQLite -> `!=`, но с NULL-безопасностью: `None != "x"` истинно, `None != None` ложно, что
    Python даёт из коробки через `!=`)."""
    return thread_ext_id is not None and thread_ext_id != ext_id


def is_top_level(thread_ext_id: str | None, ext_id: str | None, thread_broadcast: bool) -> bool:
    """`top_level := NOT is_reply OR thread_broadcast = 1`."""
    return not is_reply(thread_ext_id, ext_id) or thread_broadcast


def is_reply_sql(thread_ext_id_expr: str, ext_id_expr: str) -> str:
    """SQL-фрагмент `is_reply` для произвольных выражений колонок (с алиасом таблицы или без)."""
    return f"({thread_ext_id_expr} IS NOT NULL AND {thread_ext_id_expr} IS NOT {ext_id_expr})"


def top_level_sql(thread_ext_id_expr: str, ext_id_expr: str, thread_broadcast_expr: str) -> str:
    """SQL-фрагмент `top_level` для произвольных выражений колонок."""
    return f"(NOT {is_reply_sql(thread_ext_id_expr, ext_id_expr)} OR {thread_broadcast_expr} = 1)"


def real_reply_sql(thread_ext_id_expr: str, ext_id_expr: str) -> str:
    """Поправки после opus-критика кода (T9): «настоящий» ответ треда — не заготовка outbox (`ext_id IS
    NULL`) и не сам родитель. Строится ИЗ `is_reply_sql` (единственная ручная копия — здесь, остальные места
    обязаны звать эту функцию, а не переписывать условие заново): `is_reply` уже истинно и для заготовки
    (`thread_ext_id IS NOT NULL` при `ext_id IS NULL` даёт `IS NOT` = истина), а для сводки треда (`touch()`)
    заготовки не считаются вовсе — раздел D."""
    return f"({is_reply_sql(thread_ext_id_expr, ext_id_expr)} AND {ext_id_expr} IS NOT NULL)"


_UNREAD_WINDOW_MS = 24 * 60 * 60 * 1000


def direct_mention_sql(message_alias: str = "m") -> str:
    """Баг #product_news (30.09): ПРЯМОЕ упоминание меня (не только `@channel`/`@here`) — единственное место
    правды. Ответ с таким упоминанием непрочитан и «для меня» независимо от подписки на тред (источник мог
    отписать тред при сверке `conversations.replies`)."""
    return f"({message_alias}.mentions_me = 1 AND {message_alias}.mentions_broadcast = 0)"


def thread_sub_mention_sql(message_alias: str = "m") -> str:
    """Ф28-2: упоминание меня, ПОДПИСЫВАЮЩЕЕ тред (`touch()`, `last_msg_ts_eligible_for_new_message`) — прямое
    (`direct_mention_sql`) и не только через мою группу Slack (`mentions_group`): тег группы — упоминание для
    меня, но тред им не подписывается. Пустой `message_alias` — колонки без префикса."""
    p = f"{message_alias}." if message_alias else ""
    return f"({p}mentions_me = 1 AND {p}mentions_broadcast = 0 AND {p}mentions_group = 0)"


def unread_reply_sql(message_alias: str = "m", thread_alias: str = "t") -> str:
    """Поправки после opus-критика кода (T1/T9): ЕДИНЫЙ предикат «непрочитанный ответ треда» — используется
    и для `unread_threads` канала (`ingest.unread_breakdown`), и для сводки конкретного треда (`unread` в
    `message.thread` и в `GET …/threads?unread=1`, `queries._thread_summary_map`/`_channel_threads_page`).
    Раньше эти два места считали по-разному (сводка треда не знала окна `first_seen-24ч` и не требовала
    подписки) — тред с непрочитанным по одному счётчику мог отсутствовать в списке по другому.

    Предполагает, что `thread_alias` УЖЕ присоединён к сообщению условием `t.channel_id = m.channel_id AND
    t.ext_id = m.thread_ext_id` — сам предикат не проверяет «настоящесть» ответа (`real_reply_sql`), это
    отдельное условие, накладываемое вызывающим (JOIN обычно и так работает только по реальным `thread_ext_id`
    строк-ответов, но заготовки и родителя надо исключать явно рядом)."""
    m, t = message_alias, thread_alias
    return (
        f"({m}.is_me = 0 AND {m}.kind <> 'event' AND {m}.deleted = 0 AND {m}.hidden = 0 "
        f"AND {m}.thread_broadcast = 0 "
        f"AND ({t}.subscribed = 1 OR {direct_mention_sql(m)}) "
        f"AND {m}.ts >= {t}.first_seen_ts - {_UNREAD_WINDOW_MS} "
        f"AND ({t}.last_read_ts IS NULL OR ({m}.ts, {m}.ext_sort, {m}.ext_id) > "
        f"({t}.last_read_ts, {t}.last_read_sort, {t}.last_read_ext)))"
    )


def unread_top_sql(message_alias: str = "m", channel_alias: str = "ch") -> str:
    """Спека 7b, К5: ЕДИНЫЙ предикат «непрочитанное сообщение верхнего уровня» — позиция после маркера
    канала (`last_read_*`), не моё, не событие, не удалённое, `top_level` (включая broadcast). Используется и
    счётчиком `unread_top` (`ingest.unread_breakdown`), и полем `unread` сообщения в выдачах
    (`message_unread_sql`) — поле и счётчики не расходятся. `channel_alias` уже присоединён к сообщению
    условием `ch.id = m.channel_id`."""
    m, ch = message_alias, channel_alias
    top_level = top_level_sql(f"{m}.thread_ext_id", f"{m}.ext_id", f"{m}.thread_broadcast")
    return (
        f"({m}.is_me = 0 AND {m}.kind <> 'event' AND {m}.deleted = 0 AND {m}.hidden = 0 AND {top_level} "
        f"AND ({ch}.last_read_ts IS NULL OR ({m}.ts, {m}.ext_sort, {m}.ext_id) > "
        f"({ch}.last_read_ts, {ch}.last_read_sort, {ch}.last_read_ext)))"
    )


def message_unread_sql(message_alias: str = "m", channel_alias: str = "ch", thread_alias: str = "t") -> str:
    """Спека 7b, П1/К5: поле `unread` (0/1) одного сообщения — из тех же построителей, что счётчики канала:
    верхний уровень — `unread_top_sql` (маркер канала), ответ в треде — `real_reply_sql` + `unread_reply_sql`
    (маркер треда, только подписанный тред, окно `first_seen − 24 ч`; ответ в неподписанном треде — 0).
    `thread_alias` присоединён LEFT JOIN-ом по `(channel_id, ext_id = m.thread_ext_id)`: нет строки треда —
    условие ответа даёт NULL, и сообщение считается прочитанным (как в счётчике, где тред без строки не
    участвует)."""
    m, t = message_alias, thread_alias
    reply = f"({real_reply_sql(f'{m}.thread_ext_id', f'{m}.ext_id')} AND {unread_reply_sql(m, t)})"
    return f"(CASE WHEN {unread_top_sql(m, channel_alias)} OR {reply} THEN 1 ELSE 0 END)"


def channel_visible_sql(schema: str, channel_alias: str = "c") -> str:
    """Ф8-4 П2: ЕДИНЫЙ предикат «беседа видна в списках» — не удалена владельцем в хабе (`hidden_before_ts IS
    NULL`) или в ней уже есть хоть одно не скрытое сообщение (пришло новее границы, черновик агента).
    Намеренно не по `last_msg_ts`: его двигают отчёт отправки и `resolve_unknown` по часам хаба. `schema` —
    файл, в котором лежит канал (`main`/`work`)."""
    return _sql(
        schema,
        "({c}.hidden_before_ts IS NULL OR EXISTS (SELECT 1 FROM {schema}.message hvm "
        "WHERE hvm.channel_id = {c}.id AND hvm.hidden = 0))",
        c=channel_alias,
    )


def thread_exists(conn, schema: str, channel_id: int, thread_ext_id: str) -> bool:
    """Есть ли уже строка треда `(channel_id, thread_ext_id)` — раздел D (T6/T7): ReadEvent на неизвестный
    тред не создаёт строку; приём родителя, у которого строка треда уже есть, обязан звать `touch()`."""
    row = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.thread WHERE channel_id = ? AND ext_id = ?"),
        (channel_id, thread_ext_id),
    ).fetchone()
    return row is not None


# --- Сводка треда: подписка, маркер, last_reply_ts ------------------------------------------------------


def _thread_row(conn, schema: str, channel_id: int, thread_ext_id: str):
    return conn.execute(
        _sql(
            schema,
            "SELECT id, last_reply_ts, subscribed, source_subscribed, reply_count FROM {schema}.thread "
            "WHERE channel_id = ? AND ext_id = ?",
        ),
        (channel_id, thread_ext_id),
    ).fetchone()


def _ensure_thread(conn, schema: str, channel_id: int, thread_ext_id: str, now_ms: int) -> int:
    """Найти или создать строку треда (`first_seen_ts = now` при создании). Возвращает `thread.id`."""
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.thread WHERE channel_id = ? AND ext_id = ?"),
        (channel_id, thread_ext_id),
    ).fetchone()
    if row is not None:
        return row[0]
    return conn.execute(
        _sql(schema, "INSERT INTO {schema}.thread (channel_id, ext_id, first_seen_ts) VALUES (?, ?, ?)"),
        (channel_id, thread_ext_id, now_ms),
    ).lastrowid


def is_subscribed(conn, schema: str, channel_id: int, thread_ext_id: str) -> bool:
    """Подписан ли я на этот тред (или тред ещё не существует -> нет)."""
    row = conn.execute(
        _sql(schema, "SELECT subscribed FROM {schema}.thread WHERE channel_id = ? AND ext_id = ?"),
        (channel_id, thread_ext_id),
    ).fetchone()
    return bool(row[0]) if row is not None else False


def _source_subscribed(conn, schema: str, channel_id: int, thread_ext_id: str) -> int | None:
    row = conn.execute(
        _sql(schema, "SELECT source_subscribed FROM {schema}.thread WHERE channel_id = ? AND ext_id = ?"),
        (channel_id, thread_ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def drop_stale_source_sub(
    conn, schema: str, channel_id: int, thread_ext_id: str, *, evidence_ext_id: str | None
) -> bool:
    """Ф28-1: новое свидетельство подписки по правилу хаба (мой ответ, прямое упоминание меня в ответе или
    родителе, правка, добавившая такое упоминание) сбрасывает устаревший снимок источника «не подписан»
    (`source_subscribed = 0` -> NULL) — дальше `touch()` подписывает тред правилом хаба, а следующая сверка с
    `conversations.replies` снова запишет мнение Slack. Зовётся ДО `touch()` (и до
    `last_msg_ts_eligible_for_new_message`, иначе мой ответ не поднял бы канал).

    `evidence_ext_id` — позиция свидетельства у источника. Снимок, уже знавший о нём (`source_sub_ext` —
    позиция, на которой снят именно флаг `subscribed`, не раньше свидетельства: догон сперва кладёт родителя
    с `subscribed`, затем ответы того же `conversations.replies`), — не устаревший, его не трогаем. Сравнение
    — только с `source_sub_ext`: `source_latest_reply_ext` двигают и снимки без мнения о подписке (живое
    обновление родителя, история), они «узнают» тег раньше, чем Slack скажет о подписке. `None` у
    свидетельства (правка — она позже снимка по смыслу) или у снимка (снят до 0041) — сброс без сравнения.
    Снимок «подписан» (1) и отсутствие снимка не трогаются. True — сбросили."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT id, source_subscribed, source_sub_ext FROM {schema}.thread "
            "WHERE channel_id = ? AND ext_id = ?",
        ),
        (channel_id, thread_ext_id),
    ).fetchone()
    if row is None or row[1] != 0:
        return False
    thread_id, _src_sub, src_latest = row
    if (
        evidence_ext_id is not None
        and src_latest is not None
        and extsort.is_slack_ts(evidence_ext_id)
        and extsort.is_slack_ts(src_latest)
        and extsort.ext_sort(evidence_ext_id) <= extsort.ext_sort(src_latest)
    ):
        return False
    conn.execute(
        _sql(
            schema, "UPDATE {schema}.thread SET source_subscribed = NULL, source_sub_ext = NULL WHERE id = ?"
        ),
        (thread_id,),
    )
    return True


def last_msg_ts_eligible_for_new_message(
    conn,
    schema: str,
    channel_id: int,
    *,
    thread_ext_id: str | None,
    ext_id: str | None,
    thread_broadcast: bool,
    is_me: bool,
    mentions_me: bool,
) -> bool:
    """Раздел «Модель»: `last_msg_ts` канала поднимают только top_level-сообщения и ответы в ПОДПИСАННЫХ
    тредах. Мой ответ/упоминание в треде подписывает его тут же (если источник не сказал `subscribed=false`
    явно) — считаем это здесь заранее, не дожидаясь отдельного вызова `touch()`, иначе моё же первое сообщение
    треда не подняло бы канал (гонка порядка операций)."""
    if is_top_level(thread_ext_id, ext_id, thread_broadcast):
        return True
    assert thread_ext_id is not None  # noqa: S101 — is_top_level(..) ложно только когда thread_ext_id задан
    # Прямое упоминание (broadcast вызывающий исключил; тег моей группы — входит, Ф28-2) поднимает канал и в
    # отписанном треде.
    if mentions_me:
        return True
    source_sub = _source_subscribed(conn, schema, channel_id, thread_ext_id)
    if source_sub is not None:
        return bool(source_sub)
    if is_me or mentions_me:
        return True
    # Поправки после opus-критика кода (T4): первый чужой ответ на МОЙ родитель должен поднять last_msg_ts
    # канала уже СЕЙЧАС, до того как `touch()` (позже, в `_finish`) создаст строку треда и пропишет
    # `subscribed=1` по правилу «я автор родителя» — иначе `is_subscribed()` ниже увидит ещё несуществующую
    # строку и ответит «нет», и первый ответ на мой же тред не поднимет канал в списке.
    # Ф8-3 П13: прямое (не широковещательное) упоминание меня в РОДИТЕЛЕ подписывает тред — как мой родитель.
    parent_row = conn.execute(
        _sql(
            schema,
            "SELECT is_me, {sub_mention} FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",
            sub_mention=thread_sub_mention_sql(""),
        ),
        (channel_id, thread_ext_id),
    ).fetchone()
    if parent_row is not None and (parent_row[0] or parent_row[1]):
        return True
    return is_subscribed(conn, schema, channel_id, thread_ext_id)


def _advance_thread_marker(conn, schema: str, thread_id: int, *, ts: int, ext_sort: int, ext_id: str) -> bool:
    """Маркер прочтения ТРЕДА — та же позиционная логика, что `channels.advance_read_marker`, но на таблице
    `thread` (нет колонки `last_read_seq` — тред не участвует в старой seq-совместимости канала)."""
    row = conn.execute(
        _sql(schema, "SELECT last_read_ts, last_read_sort, last_read_ext FROM {schema}.thread WHERE id = ?"),
        (thread_id,),
    ).fetchone()
    old_ts, old_sort, old_ext = row
    ext_id = ext_id or ""
    old_ext = old_ext or ""
    if old_ts is not None and (ts, ext_sort, ext_id) <= (old_ts, old_sort, old_ext):
        return False
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.thread SET last_read_ts = ?, last_read_sort = ?, last_read_ext = ? WHERE id = ?",
        ),
        (ts, ext_sort, ext_id, thread_id),
    )
    return True


def touch(conn, schema: str, channel_id: int, thread_ext_id: str, now_ms: int) -> bool:
    """Пересчитать сводку треда `(channel_id, thread_ext_id)` после изменения любого его ОТВЕТА (раздел D):
    создаёт/обновляет строку треда, пересчитывает `last_reply_ts`/`reply_count` (только `ext_id IS NOT NULL`,
    не удалённые, БЕЗ самого родителя), подписку и маркер по правилам «Модели». Возвращает True, если сводка
    реально изменилась — сигнал вызывающему на бамп seq родителя и канала (см. `touch_and_bump`).

    Заготовка-ответ (`ext_id IS NULL`) не учитывается ни в `last_reply_ts`/`reply_count`, ни в маркере, ни в
    подписке («написал — значит прочитал» ждёт реального ext_id — иначе маркер указывал бы на пустую строку).

    Поправки после opus-критика кода (T2): сравнение ТОЛЬКО по `last_reply_ts` пропускало смену числа живых
    ответов, если `MAX(ts)` не менялся — более ранний ответ, догнавший позже (сверка/сон), не двигает
    максимум, но `reply_count` растёт; удаление НЕ последнего по времени ответа — симметрично. `reply_count`
    закэширован в `thread`, чтобы было с чем сравнивать «было/стало»."""
    row = _thread_row(conn, schema, channel_id, thread_ext_id)
    changed = row is None
    if row is None:
        thread_id = _ensure_thread(conn, schema, channel_id, thread_ext_id, now_ms)
        old_last_reply_ts, old_subscribed, source_subscribed, old_reply_count = None, 0, None, 0
    else:
        thread_id, old_last_reply_ts, old_subscribed, source_subscribed, old_reply_count = row

    real_reply = real_reply_sql("thread_ext_id", "ext_id")
    # Ф8-3 П11: упоминание ТОЛЬКО широковещательное (`@channel`/`@here`) тред не подписывает; Ф28-2 — и только
    # моей группы Slack.
    agg = conn.execute(
        _sql(
            schema,
            "SELECT MAX(ts), MAX(is_me), MAX({sub_mention}), COUNT(*) "
            "FROM {schema}.message "
            "WHERE channel_id = ? AND thread_ext_id = ? AND {real_reply} AND deleted = 0 AND hidden = 0",
            real_reply=real_reply,
            sub_mention=thread_sub_mention_sql(""),
        ),
        (channel_id, thread_ext_id),
    ).fetchone()
    new_last_reply_ts, has_my_reply, has_mention, new_reply_count = agg
    has_my_reply = bool(has_my_reply)
    has_mention = bool(has_mention)

    # Ф8-3 П13: «тред, где меня отметили» — прямое упоминание меня в РОДИТЕЛЕ подписывает тред, как мой
    # родитель (удалённый родитель не подписывает).
    parent_row = conn.execute(
        _sql(
            schema,
            "SELECT is_me, {sub_mention} AND deleted = 0 FROM {schema}.message "
            "WHERE channel_id = ? AND ext_id = ?",
            sub_mention=thread_sub_mention_sql(""),
        ),
        (channel_id, thread_ext_id),
    ).fetchone()
    parent_is_mine = bool(parent_row[0]) if parent_row is not None else False
    parent_mentions_me = bool(parent_row[1]) if parent_row is not None else False

    if new_last_reply_ts != old_last_reply_ts or new_reply_count != old_reply_count:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.thread SET last_reply_ts = ?, reply_count = ? WHERE id = ?",
            ),
            (new_last_reply_ts, new_reply_count, thread_id),
        )
        changed = True

    if source_subscribed is not None:
        new_subscribed = int(bool(source_subscribed))
    elif parent_is_mine or has_my_reply or has_mention or parent_mentions_me:
        new_subscribed = 1
    else:
        new_subscribed = old_subscribed
    if new_subscribed != old_subscribed:
        conn.execute(
            _sql(schema, "UPDATE {schema}.thread SET subscribed = ? WHERE id = ?"),
            (new_subscribed, thread_id),
        )
        changed = True

    if has_my_reply:
        my_pos = conn.execute(
            _sql(
                schema,
                "SELECT ts, ext_sort, ext_id FROM {schema}.message WHERE channel_id = ? "
                "AND thread_ext_id = ? AND {real_reply} AND is_me = 1 "
                "AND deleted = 0 AND hidden = 0 ORDER BY ts DESC, ext_sort DESC, ext_id DESC LIMIT 1",
                real_reply=real_reply,
            ),
            (channel_id, thread_ext_id),
        ).fetchone()
        if my_pos is not None:
            ts, ext_sort_val, ext_id = my_pos
            if _advance_thread_marker(conn, schema, thread_id, ts=ts, ext_sort=ext_sort_val, ext_id=ext_id):
                changed = True

    return changed


def bump_for_summary_change(conn, schema: str, channel_id: int, thread_ext_id: str) -> int:
    """Раздел D.3: сводка треда изменилась -> новый `seq` родителю (если он в ядре) И каналу. Бамп НЕ меняет
    `first_seq`/позицию родителя (родитель не становится непрочитанным, уведомления UI по `first_seq` не
    срабатывают) — просто `UPDATE ... SET seq = ?`, без затрагивания прочих колонок."""
    max_seq = 0
    parent = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?"),
        (channel_id, thread_ext_id),
    ).fetchone()
    if parent is not None:
        seq = next_seq(conn)
        conn.execute(_sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (seq, parent[0]))
        max_seq = seq
    channel_seq = next_seq(conn)
    conn.execute(_sql(schema, "UPDATE {schema}.channel SET seq = ? WHERE id = ?"), (channel_seq, channel_id))
    return max(max_seq, channel_seq)


def touch_and_bump(conn, schema: str, channel_id: int, thread_ext_id: str, now_ms: int) -> int:
    """`touch()` + бамп seq родителя/канала, если сводка реально изменилась. Возвращает итоговый `max_seq`
    (0, если сводка не изменилась — бампа не было; повтор идентичного события/сверка всего треда даёт 0)."""
    if not touch(conn, schema, channel_id, thread_ext_id, now_ms):
        return 0
    return bump_for_summary_change(conn, schema, channel_id, thread_ext_id)


def bump_channel_last_msg_ts(conn, schema: str, channel_id: int, ts: int) -> int:
    """Отдельный (не связанный со сводкой треда) бамп `last_msg_ts` канала — для случая, когда подписка на
    тред появилась только что (моё первое сообщение/упоминание, раздел «Модель»)."""
    seq = next_seq(conn)
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.channel SET last_msg_ts = MAX(COALESCE(last_msg_ts, ?), ?), "
            "seq = ? WHERE id = ?",
        ),
        (ts, ts, seq, channel_id),
    )
    return seq


# --- Сводка источника (ThreadInfo), раздел D.1 -----------------------------------------------------------


def apply_source_info(conn, schema: str, channel_id: int, parent_ext_id: str, info, now_ms: int) -> bool:
    """Раздел D.1: сводка треда со стороны ИСТОЧНИКА (`ThreadInfo`, приходит на родителе), обрабатывается
    ДО ранних выходов `_apply_message` — снимок родителя без правки не должен съесть новый `reply_count`.
    `info.reply_count == 0` без существующей строки треда -> строку НЕ создавать (раздел B событий).
    `last_read_ext_id` источника двигает маркер (только вперёд); `subscribed` источника -> `source_subscribed`
    И `subscribed` (главное — мнение источника). Ф40-1 B: `last_read_ts` — то же временем (Mattermost)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT id, source_reply_count, source_latest_reply_ext, source_subscribed FROM {schema}.thread "
            "WHERE channel_id = ? AND ext_id = ?",
        ),
        (channel_id, parent_ext_id),
    ).fetchone()

    if row is None:
        if info.reply_count == 0:
            return False
        thread_id = _ensure_thread(conn, schema, channel_id, parent_ext_id, now_ms)
        old_src_count, old_src_latest, old_src_sub = None, None, None
        changed = True
    else:
        thread_id, old_src_count, old_src_latest, old_src_sub = row
        changed = False

    if info.reply_count != old_src_count:
        conn.execute(
            _sql(schema, "UPDATE {schema}.thread SET source_reply_count = ? WHERE id = ?"),
            (info.reply_count, thread_id),
        )
        changed = True

    # Поправки после opus-критика кода (T5): `info.latest_reply_ext_id is None`/`info.subscribed is None`
    # значит «источник не знает/не прислал это поле в ЭТОМ снимке» (например, `conversations.history` не
    # несёт `subscribed`, только `conversations.replies` — спека 2c), а НЕ «источник явно сказал NULL». Раньше
    # такой снимок стирал уже известное мнение источника (явную отписку, последний известный ответ) — повтор
    # снимка без этих полей на каждом цикле сверки давал лишний бамп, а более поздний снимок без `subscribed`
    # отменял РАНЕЕ полученную явную отписку. Поле, которого нет в снимке, просто не трогаем.
    if info.latest_reply_ext_id is not None and info.latest_reply_ext_id != old_src_latest:
        conn.execute(
            _sql(schema, "UPDATE {schema}.thread SET source_latest_reply_ext = ? WHERE id = ?"),
            (info.latest_reply_ext_id, thread_id),
        )
        changed = True

    if info.subscribed is not None:
        # Ф28-1: позиция, на которой снят флаг `subscribed`, — последний ответ треда, известный ЭТОМУ снимку
        # (`latest_reply` в том же `conversations.replies`); нет его — последний ответ, уже лежащий в ядре
        # (Slack о нём точно знал). Колонка скрытая — бампа не даёт. Ф40-1 B: «последний» — по времени
        # (`ts`, затем `ext_sort`, `ext_id`): у Mattermost `ext_sort` всегда 0.
        sub_ext = info.latest_reply_ext_id
        if sub_ext is None:
            last = conn.execute(
                _sql(
                    schema,
                    "SELECT ext_id FROM {schema}.message WHERE channel_id = ? AND thread_ext_id = ? "
                    "AND {real_reply} ORDER BY ts DESC, ext_sort DESC, ext_id DESC LIMIT 1",
                    real_reply=real_reply_sql("thread_ext_id", "ext_id"),
                ),
                (channel_id, parent_ext_id),
            ).fetchone()
            sub_ext = last[0] if last is not None else None
        conn.execute(
            _sql(schema, "UPDATE {schema}.thread SET source_sub_ext = ? WHERE id = ?"), (sub_ext, thread_id)
        )
        new_src_sub = int(info.subscribed)
        if new_src_sub != old_src_sub:
            conn.execute(
                _sql(schema, "UPDATE {schema}.thread SET source_subscribed = ? WHERE id = ?"),
                (new_src_sub, thread_id),
            )
            changed = True
            cur_subscribed = conn.execute(
                _sql(schema, "SELECT subscribed FROM {schema}.thread WHERE id = ?"), (thread_id,)
            ).fetchone()[0]
            if cur_subscribed != new_src_sub:
                conn.execute(
                    _sql(schema, "UPDATE {schema}.thread SET subscribed = ? WHERE id = ?"),
                    (new_src_sub, thread_id),
                )

    if info.last_read_ext_id is not None:
        target = conn.execute(
            _sql(
                schema,
                "SELECT ts, ext_sort, ext_id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",
            ),
            (channel_id, info.last_read_ext_id),
        ).fetchone()
        if target is not None:
            ts, ext_sort_val, ext_id = target
        elif extsort.is_slack_ts(info.last_read_ext_id):
            # Приёмка 2b (архитектор): как `ReadEvent` (раздел A) — сообщения в ядре нет (отфильтрованный
            # подтип, ещё не пришло), позиция выводится из самого ts Slack; иначе маркер треда застревал бы.
            ts = extsort.slack_ts_to_ms(info.last_read_ext_id)
            ext_sort_val = extsort.ext_sort(info.last_read_ext_id)
            ext_id = info.last_read_ext_id
        else:
            ts = None
        if ts is not None and _advance_thread_marker(
            conn, schema, thread_id, ts=ts, ext_sort=ext_sort_val, ext_id=ext_id
        ):
            changed = True
    elif getattr(info, "last_read_ts", None) is not None:
        # Ф40-1 B п. 4: позиция прочтения треда ВРЕМЕНЕМ — маркер прямо в `(ts, EXT_SORT_MAX, "")`, сообщение
        # в ядре искать не нужно (сводка приходит раньше родителя и ответов). Своя отправка из хаба —
        # не прочтение (п. 3); при пустом треде правило не срабатывает.
        if not own_hub_send_is_last(
            conn, schema, channel_id, up_to_ts=info.last_read_ts, thread_ext_id=parent_ext_id
        ) and _advance_thread_marker(
            conn, schema, thread_id, ts=info.last_read_ts, ext_sort=extsort.EXT_SORT_MAX, ext_id=""
        ):
            changed = True

    return changed


def own_hub_send_is_last(
    conn, schema: str, channel_id: int, *, up_to_ts: int, thread_ext_id: str | None = None
) -> bool:
    """Ф40-1 B п. 3 (П2 плана фазы): «своя отправка — не прочтение». Источник с позицией прочтения ВРЕМЕНЕМ
    (Mattermost) сам сдвигает её после своего поста — это не значит, что владелец что-то прочитал (авто-отправка
    сессии, отложенная отправка, черновик, подтверждённый с главной). True — последнее по позиции сообщение
    не позже `up_to_ts` своё И отправлено из хаба (`is_me` и `outbox_id` не пуст): такое событие — no-op.

    Смотрим беседу целиком (любое сообщение, включая ответы тредов) либо, при `thread_ext_id`, корень и ответы
    этого треда. Заготовки (`ext_id IS NULL`) не в счёт — у источника их ещё нет, а `ts` у них по часам хаба;
    удалённые и скрытые — в счёт (позицию источник сдвинул из-за них, удаление её не вернуло). Сообщений не
    позже `up_to_ts` нет — False (защищать нечего)."""
    scope = "AND (ext_id = ? OR thread_ext_id = ?) " if thread_ext_id is not None else ""
    params: tuple = (channel_id, thread_ext_id, thread_ext_id) if thread_ext_id is not None else (channel_id,)
    row = conn.execute(
        _sql(
            schema,
            "SELECT is_me, outbox_id FROM {schema}.message WHERE channel_id = ? {scope}"
            "AND ext_id IS NOT NULL AND ts <= ? ORDER BY ts DESC, ext_sort DESC, ext_id DESC LIMIT 1",
            scope=scope,
        ),
        (*params, up_to_ts),
    ).fetchone()
    return row is not None and bool(row[0]) and row[1] is not None


def apply_source_info_and_bump(
    conn, schema: str, channel_id: int, parent_ext_id: str, info, now_ms: int
) -> int:
    if not apply_source_info(conn, schema, channel_id, parent_ext_id, info, now_ms):
        return 0
    return bump_for_summary_change(conn, schema, channel_id, parent_ext_id)


# --- Маркер прочтения ТРЕДА со стороны UI/ReadEvent (раздел «Модель», D.2) -------------------------------


class ThreadError(Exception):
    """Базовая ошибка операций над тредом."""


class NotFound(ThreadError):
    """Сообщение `up_to_message_id` не существует или принадлежит другому каналу — 404."""


class WrongThread(ThreadError):
    """Сообщение существует, но не из ЭТОГО треда (и не его родитель) — 422 (раздел E спеки 2b)."""


def mark_thread_read(
    conn, schema: str, channel_id: int, thread_ext_id: str, *, up_to_message_id: int
) -> bool:
    """Тредовая `read` — как `channels.mark_read`, но по позиции в таблице `thread`, не `channel`. Сообщение
    не из этого треда и не его родитель -> `WrongThread` (маршрут переводит в 422; родитель — допустим, он
    «не ответ», но лежит на позиции `thread_ext_id`).

    Поправки после opus-критика кода:
    - (T7) строки треда ещё нет (ни одного ответа не приходило — раздел B: `reply_count=0` без ответов
      строку не создаёт) -> тоже `WrongThread`/422, а не тихое создание фиктивной сводки `{reply_count: 0}`
      у обычного сообщения (маркер прочтения самим по себе тред не создаёт).
    - (T3) маркер реально сдвинулся -> `bump_for_summary_change`, как у `ReadEvent` (`ingest._apply_read`)
      и `touch_and_bump` — иначе `/sync` других вкладок не узнаёт, что `unread_threads` канала упал."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT channel_id, ts, ext_sort, ext_id, thread_ext_id FROM {schema}.message WHERE id = ?",
        ),
        (up_to_message_id,),
    ).fetchone()
    if row is None or row[0] != channel_id:
        raise NotFound(f"message {up_to_message_id} не принадлежит каналу {channel_id}")
    _channel_id, ts, ext_sort_val, ext_id, msg_thread_ext_id = row
    is_parent = ext_id == thread_ext_id
    is_this_thread_reply = msg_thread_ext_id == thread_ext_id
    if not (is_parent or is_this_thread_reply):
        raise WrongThread(f"message {up_to_message_id} не принадлежит треду {thread_ext_id!r}")
    row = _thread_row(conn, schema, channel_id, thread_ext_id)
    if row is None:
        raise WrongThread(f"тред {thread_ext_id!r} канала {channel_id} ещё не существует (нет ответов)")
    thread_id = row[0]
    changed = _advance_thread_marker(conn, schema, thread_id, ts=ts, ext_sort=ext_sort_val, ext_id=ext_id)
    if changed:
        bump_for_summary_change(conn, schema, channel_id, thread_ext_id)
    return changed


def _is_personal_channel(conn, schema: str, channel_id: int) -> bool:
    """Личка: `kind='dm'` или групповая личка Slack (`kind='group'` с `attrs.slack_type='mpim'`) — то же
    различение, что `attention.effective_mode` и `work.PERSONAL_*` (Ф8-3)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT kind, json_extract(meta_json, '$.attrs.slack_type') FROM {schema}.channel WHERE id = ?",
        ),
        (channel_id,),
    ).fetchone()
    return row is not None and (row[0] == "dm" or row[1] == "mpim")


def read_personal_threads_up_to(
    conn, schema: str, channel_id: int, *, ts: int, ext_sort: int, ext_id: str
) -> int:
    """Ф14-7 A1: моё сообщение в ОСНОВНОЙ ленте лички = прочитал всё до него, включая ответы в тредах. Для
    каждого треда беседы с непрочитанным ответом не позже `(ts, ext_sort, ext_id)` маркер треда сдвигается
    (только вперёд) на последний живой ответ не позже этой позиции, и сводка бампится — как
    `mark_thread_read`. Не личка — ничего. Возвращает итоговый `max_seq` (0 — ничего не сдвинулось)."""
    if not _is_personal_channel(conn, schema, channel_id):
        return 0
    real_reply = real_reply_sql("m.thread_ext_id", "m.ext_id")
    candidates = conn.execute(
        _sql(
            schema,
            "SELECT DISTINCT t.id, t.ext_id FROM {schema}.message m JOIN {schema}.thread t "
            "ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
            "WHERE m.channel_id = ? AND {real_reply} AND {unread_reply} "
            "AND (m.ts, m.ext_sort, m.ext_id) <= (?, ?, ?)",
            real_reply=real_reply,
            unread_reply=unread_reply_sql("m", "t"),
        ),
        (channel_id, ts, ext_sort, ext_id),
    ).fetchall()
    max_seq = 0
    for thread_id, thread_ext_id in candidates:
        target = conn.execute(
            _sql(
                schema,
                "SELECT m.ts, m.ext_sort, m.ext_id FROM {schema}.message m WHERE m.channel_id = ? "
                "AND m.thread_ext_id = ? AND {real_reply} AND m.deleted = 0 AND m.hidden = 0 "
                "AND (m.ts, m.ext_sort, m.ext_id) <= (?, ?, ?) "
                "ORDER BY m.ts DESC, m.ext_sort DESC, m.ext_id DESC LIMIT 1",
                real_reply=real_reply,
            ),
            (channel_id, thread_ext_id, ts, ext_sort, ext_id),
        ).fetchone()
        if target is None:
            continue
        t_ts, t_sort, t_ext = target
        if _advance_thread_marker(conn, schema, thread_id, ts=t_ts, ext_sort=t_sort, ext_id=t_ext):
            max_seq = max(max_seq, bump_for_summary_change(conn, schema, channel_id, thread_ext_id))
    return max_seq


def advance_marker_by_position(
    conn,
    schema: str,
    channel_id: int,
    thread_ext_id: str,
    *,
    ts: int,
    ext_sort: int,
    ext_id: str,
    now_ms: int,
) -> bool:
    """`ReadEvent` с `thread_ext_id`, чья позиция вычислена вызывающим (`ingest._apply_read`) — используется,
    когда самого сообщения в ядре ещё нет (позиция берётся из самой строки ReadEvent/ts Slack — раздел A)."""
    thread_id = _ensure_thread(conn, schema, channel_id, thread_ext_id, now_ms)
    return _advance_thread_marker(conn, schema, thread_id, ts=ts, ext_sort=ext_sort, ext_id=ext_id)
