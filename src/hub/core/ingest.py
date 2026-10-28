"""Приём событий коннекторов в БД. Спецификация: docs/architecture.md.

`apply_batch` вызывается ТОЛЬКО внутри `Db.write()` (там же уже открыта `BEGIN IMMEDIATE`, conn — писатель
с обоими файлами, main и work, под алиасами). Каждое событие пачки — в своём `SAVEPOINT`: упавшее откатывается
и уходит в `dead_letter`, соседи применяются, курсор двигается в любом случае.
"""

import json
import logging
import sqlite3
from typing import NamedTuple

from . import buttons as buttons_mod
from . import chan_subs, draft_channels, extsort, hiding, mapping, outbox, peer_flood, retention
from . import channels as channels_mod
from . import group_ops as group_ops_mod
from . import handled as handled_mod
from . import inbound_rules as inbound_rules_mod
from . import join as join_mod
from . import leave as leave_mod
from . import mail_reactions as mail_reactions_mod
from . import media as media_mod
from . import reactions as reactions_mod
from . import reminders as reminders_mod
from . import service_bot as service_bot_mod
from . import stages as stages_mod
from . import threads as threads_mod
from .db import db_of, next_seq
from .events import (
    AccountMetaEvent,
    ChannelEvent,
    ChannelMigrateEvent,
    ChannelRef,
    CursorIn,
    DeleteEvent,
    EditEvent,
    MailReactionEvent,
    PeerReadEvent,
    ReactionEvent,
    ReadEvent,
    SenderRef,
    UpsertEvent,
    ViewsEvent,
    parse_event,
)
from .extsort import ext_sort as _shared_ext_sort

_DAY_MS = 24 * 60 * 60 * 1000
_MAX_BATCH = 500
_MAX_ATTRS_BYTES = 4 * 1024  # раздел A.1 спеки куска 1a: attrs в JSON > 4 КиБ -> ядовитое событие
_MAX_ACCOUNT_META_KEY_LEN = 64
_MAX_ACCOUNT_META_VALUE_BYTES = 64 * 1024


def _sql(schema: str, template: str, **parts: str) -> str:
    """Подставить имя схемы (`main`/`work`) и, где нужно, список колонок в шаблон запроса.

    `schema` и `parts` — не пользовательский ввод: schema — одно из двух значений account.db (CHECK в БД),
    parts — построены кодом из фиксированного `_MESSAGE_CONTENT_COLUMNS`. Обёртка нужна только чтобы линтер
    (S608) не путал это с конкатенацией пользовательских данных в SQL.
    """
    return template.format(schema=schema, **parts)


class BatchResult(NamedTuple):
    applied: int
    noop: int
    dead: int
    max_seq: int


_logger = logging.getLogger(__name__)


class _RefillLog:
    """Ф42-3: счёт добора истории за одну пачку `POST /events` — сколько строк вставлено и какие из них
    несут прямое упоминание (id беседы и сообщения в хабе)."""

    def __init__(self) -> None:
        self.inserted = 0
        self.mentions: list[tuple[int, int]] = []


def apply_batch(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    events: list[dict],
    cursor: CursorIn | None,
    now_ms: int,
) -> BatchResult:
    if len(events) > _MAX_BATCH:
        raise ValueError(f"batch too large: {len(events)} > {_MAX_BATCH}")

    row = conn.execute("SELECT db, source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown account_id {account_id}")
    schema = row[0]
    # Ф54-2 (П2): личка аккаунта Telegram с ботом хаба беседой не становится — её события отбрасываются.
    service_bots = (
        service_bot_mod.bot_ids(conn) if row[1] == service_bot_mod.TG_SOURCE else frozenset()
    )

    # Раздел F.1 спеки 2b: cutoff ретенции считается ОДИН раз на пачку (не на событие) — иначе `MAX(work.ts)`
    # пересчитывался бы N раз на N-событийную пачку одного и того же work-аккаунта; на main всегда None.
    # Ф8-4 A1/П9: ретенция снята (`retention.RETENTION_ENABLED`) — очистки при приёме нет и для work.
    retention_cutoff = (
        retention.ingest_cutoff_ts(conn, schema, now_ms)
        if schema == "work" and retention.RETENTION_ENABLED
        else None
    )

    applied = noop = dead = 0
    max_seq = 0
    refill = _RefillLog()  # Ф42-3: что вставил добор истории в этой пачке — для журнала

    for raw in events:
        conn.execute("SAVEPOINT ev")
        try:
            event = parse_event(raw)
            if service_bot_mod.drops_event(event, service_bots):
                status, seq = "noop", 0
            elif isinstance(event, UpsertEvent | EditEvent):
                status, seq = _apply_message(
                    conn, schema, account_id, event, now_ms, retention_cutoff, refill=refill
                )
            elif isinstance(event, DeleteEvent):
                status, seq = _apply_delete(conn, schema, account_id, event, now_ms)
            elif isinstance(event, ReactionEvent):
                status, seq = _apply_reaction(conn, schema, account_id, event, now_ms)
            elif isinstance(event, ViewsEvent):
                status, seq = _apply_views(conn, schema, account_id, event)
            elif isinstance(event, MailReactionEvent):
                status, seq = mail_reactions_mod.apply_event(conn, schema, account_id, event)
            elif isinstance(event, ReadEvent):
                status, seq = _apply_read(conn, schema, account_id, event, now_ms)
            elif isinstance(event, PeerReadEvent):
                status, seq = _apply_peer_read(conn, schema, account_id, event)
            elif isinstance(event, ChannelEvent):
                status, seq = _apply_channel(conn, schema, account_id, event, now_ms)
            elif isinstance(event, ChannelMigrateEvent):
                status, seq = _apply_channel_migrate(conn, schema, account_id, event, now_ms)
            elif isinstance(event, AccountMetaEvent):
                status, seq = _apply_account_meta(conn, schema, account_id, event, now_ms)
            else:  # pragma: no cover — дискриминатор покрывает все ветки
                raise TypeError(f"unhandled event type {type(event)!r}")
        except Exception as exc:  # noqa: BLE001 — ядовитое событие идёт в dead_letter, пачку не роняет
            conn.execute("ROLLBACK TO ev")
            _record_dead_letter(conn, schema, account_id, raw, exc, now_ms)
            dead += 1
        else:
            if status == "applied":
                applied += 1
                max_seq = max(max_seq, seq)
            else:
                noop += 1
        finally:
            conn.execute("RELEASE ev")

    if cursor is not None:
        _apply_cursor(conn, schema, account_id, cursor, now_ms)

    if refill.inserted:
        # Ф42-3 A4: только числа — текстов и имён в журнале нет. Список упоминаний после выкладки собирается
        # по этим строкам. Уровень WARNING намеренно: процесс ядра журнал не настраивает (`basicConfig` зовут
        # только коннекторы), INFO до journald не доходит — только warning и выше через обработчик последней
        # надежды. Строк мало: добор разовый.
        _logger.warning("ingest: добор account=%s новых=%d", account_id, refill.inserted)
        for mention_channel_id, mention_message_id in refill.mentions:
            _logger.warning(
                "ingest: добор упоминание channel=%s message=%s", mention_channel_id, mention_message_id
            )

    return BatchResult(applied=applied, noop=noop, dead=dead, max_seq=max_seq)


def unread_breakdown(conn: sqlite3.Connection, channel_id: int) -> tuple[int, int]:
    """`(unread_top, unread_threads)` — раздел «Непрочитанное» спеки 2b (позиционно, находка F8 критика 1b):
    `unread_top` — как раньше, но только `top_level` (не мои, не удалённые, `kind <> 'event'`, после маркера
    канала); `unread_threads` — сумма по ПОДПИСАННЫМ тредам: ответы после маркера ТРЕДА, не мои, не удалённые,
    `kind <> 'event'`, `thread_broadcast = 0` (broadcast уже в `unread_top` — дважды не считаем), и
    `ts >= thread.first_seen_ts - 24 ч` (свежая загрузка старого треда без `last_read` от источника не
    превращается в сотни непрочитанных)."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.channel WHERE id = ?"),
        (channel_id,),
    ).fetchone()
    if row is None:
        return 0, 0

    # Спека 7b, К5: предикат верхнего уровня — ОДНИМ построителем (`threads_mod.unread_top_sql`), тем же, что
    # поле `unread` сообщения в выдачах (`queries.py`): поле и счётчик не расходятся.
    top_query = _sql(
        schema,
        "SELECT COUNT(*) FROM {schema}.message m JOIN {schema}.channel ch ON ch.id = m.channel_id "
        "WHERE m.channel_id = ? AND {unread_top}",
        unread_top=threads_mod.unread_top_sql("m", "ch"),
    )
    unread_top = conn.execute(top_query, (channel_id,)).fetchone()[0]

    # Поправки после opus-критика кода (T1/T9): предикат «непрочитанный ответ» — ОДНИМ построителем
    # (`threads_mod.unread_reply_sql`), тем же самым, что зовёт сводка треда (`queries._thread_summary_map`,
    # `queries.list_channel_threads`) — иначе цифры «сколько непрочитанных тредов» и «список тредов с
    # непрочитанным» расходятся (находка T1).
    real_reply = threads_mod.real_reply_sql("m.thread_ext_id", "m.ext_id")
    unread_reply = threads_mod.unread_reply_sql("m", "t")
    threads_query = _sql(
        schema,
        "SELECT COALESCE(COUNT(*), 0) FROM {schema}.message m JOIN {schema}.thread t "
        "ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
        "WHERE m.channel_id = ? AND {real_reply} AND {unread_reply}",
        real_reply=real_reply,
        unread_reply=unread_reply,
    )
    unread_threads = conn.execute(threads_query, (channel_id,)).fetchone()[0]
    return unread_top, unread_threads


def unread_count(conn: sqlite3.Connection, channel_id: int) -> int:
    """`unread_top + unread_threads` (раздел «Непрочитанное»: старое поле сохраняет смысл «сколько всего»)."""
    top, threads_unread = unread_breakdown(conn, channel_id)
    return top + threads_unread


_ext_sort = _shared_ext_sort  # раздел A спеки 2b: единый модуль extsort.py, дубль здесь убран


def _dump(value: object) -> str | None:
    return json.dumps(value, sort_keys=True, ensure_ascii=False) if value is not None else None


def _record_dead_letter(
    conn: sqlite3.Connection, schema: str, account_id: int, raw: dict, exc: Exception, now_ms: int
) -> None:
    conn.execute(
        _sql(schema, "INSERT INTO {schema}.dead_letter (account_id, payload, error, ts) VALUES (?, ?, ?, ?)"),
        (account_id, json.dumps(raw, ensure_ascii=False), f"{type(exc).__name__}: {exc}", now_ms),
    )


def _own_id_space(meta_json: str | None) -> bool:
    if not meta_json:
        return False
    try:
        return bool(json.loads(meta_json).get("own_id_space", False))
    except ValueError, TypeError, AttributeError:
        return False


def _reject_temp_ext_id(ext_id: str) -> None:
    """Ф12-1 A7: временный `ext_id` беседы-заготовки (`new:…`) знает только ядро — событие коннектора с ним
    ядовитое (иначе чужое событие попало бы в беседу-заготовку)."""
    if draft_channels.is_temp_ext_id(ext_id):
        raise ValueError("временный ext_id беседы-заготовки в событии коннектора")


def _inbound_rules(
    conn: sqlite3.Connection,
    schema: str,
    account_id: int,
    channel_id: int,
    ev: UpsertEvent | EditEvent,
    now_ms: int,
) -> inbound_rules_mod.Outcome:
    """Ф63-1 П2/П3: контекст сообщения для правил входящих — беседа уже разложена (`_ensure_channel`):
    проект и вид — её; у почты отправитель — адрес (`handle`, иначе `ext_id`), тема — поле `subject` события
    (коннектор старше Ф63-1 его не шлёт — название беседы); у не-почты тема — название беседы."""
    row = conn.execute(
        _sql(schema, "SELECT project_id, kind, title FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    if row is None:
        return inbound_rules_mod.NO_OUTCOME
    project_id, kind, title = row
    mail = kind == "mail_thread"
    sender = None
    if mail and ev.sender is not None:
        address = (ev.sender.handle or ev.sender.ext_id or "").strip().lower()
        sender = address or None
    subject = ev.subject if mail and ev.subject else title
    ctx = inbound_rules_mod.MsgCtx(account_id, kind, sender, subject, ev.text)
    return inbound_rules_mod.evaluate(conn, project_id=project_id, ctx=ctx, now_ms=now_ms)


def _archive_by_rule(conn: sqlite3.Connection, schema: str, channel_id: int, now_ms: int) -> int:
    """Правило входящих «в архив»: беседа ещё не в архиве — в архив; `channel.seq` после правки (0 — уже
    была в архиве)."""
    row = conn.execute(
        _sql(schema, "SELECT archived FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    if row is None or row[0]:
        return 0
    channels_mod.update_channel(conn, channel_id, archived=True, now_ms=now_ms)
    sql = _sql(schema, "SELECT seq FROM {schema}.channel WHERE id = ?")
    return conn.execute(sql, (channel_id,)).fetchone()[0]


def _ensure_channel(
    conn: sqlite3.Connection, schema: str, account_id: int, ref: ChannelRef, now_ms: int
) -> int:
    _reject_temp_ext_id(ref.ext_id)
    row = conn.execute(
        _sql(schema, "SELECT id, title, meta_json FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, ref.ext_id),
    ).fetchone()
    if row is None:
        return _create_channel(conn, schema, account_id, ref, now_ms)

    channel_id, old_title, meta_json = row
    if ref.title is not None and ref.title != old_title:
        _rename_channel(conn, schema, channel_id, old_title, ref.title, now_ms)
    if ref.attrs is not None:
        _update_attrs_if_changed(conn, schema, channel_id, meta_json, ref.attrs, now_ms)
    return channel_id


def _auto_forward_kind(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: UpsertEvent | EditEvent
) -> str:
    """Ф17-3 решение 1: автопересылка поста канала в группу обсуждения (`forward.auto`, помечает коннектор),
    если сам канал есть в хабе и не в архиве, — служебная строка (`event`): не непрочитанное, не будит,
    не разбирается. Канала в хабе нет (или архив) — обычное сообщение: там пост виден только здесь."""
    fwd = ev.forward
    if not isinstance(fwd, dict) or fwd.get("auto") is not True:
        return ev.kind
    from_ext = fwd.get("from_ext_id")
    if not isinstance(from_ext, str) or not from_ext:
        return ev.kind
    row = conn.execute(
        _sql(schema, "SELECT archived FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, from_ext),
    ).fetchone()
    if row is None or row[0]:
        return ev.kind
    return "event"


def _account_source(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def _find_channel_id(conn: sqlite3.Connection, schema: str, account_id: int, ref: ChannelRef) -> int | None:
    """Только поиск, НИКОГДА не создаёт и не трогает title/attrs — для `read`/`reaction`/`delete` (правило
    1a A.2, приёмка архитектора куска 1b, находка 5): живой `ReadInbox`/реакция/удаление по каналу, о котором
    core ещё не знает ни одного сообщения, раньше создавали пустой канал через `_ensure_channel`. Такие
    события ничего не решают о канале (нет title, `attrs` для них не отправляются) — при неизвестном канале
    им попросту нечего делать, `no-op`."""
    if draft_channels.is_temp_ext_id(ref.ext_id):
        return None  # Ф12-1 A7: беседа-заготовка коннектору не видна
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, ref.ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def _find_channel_ids_with_topics(
    conn: sqlite3.Connection, schema: str, account_id: int, ref: ChannelRef
) -> list[int]:
    """Находка F4 (round 2 критика 1b): живые удаления и реакции в темах форума — Telethon отдаёт `peer`
    РОДИТЕЛЯ (форума), не темы (пространство id общее, но строка сообщения в БД лежит в ДОЧЕРНЕМ канале-теме).
    `_find_channel_id` по родителю находил бы форум, а сообщение в нём никогда — реакция/удаление no-op'ились
    молча. Возвращает [канал] + [все его дочерние topic-каналы] (пусто, если канал вообще неизвестен) —
    вызывающий ищет ext_id сообщения СРАЗУ во всех них (пространство id действительно общее для форума и его
    тем, коллизий не бывает)."""
    channel_id = _find_channel_id(conn, schema, account_id, ref)
    if channel_id is None:
        return []
    children = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.channel WHERE parent_channel_id = ? AND kind = 'topic'"),
        (channel_id,),
    ).fetchall()
    return [channel_id, *(c[0] for c in children)]


def _with_wa_alias_channels(
    conn: sqlite3.Connection, schema: str, account_id: int, channel_ids: list[int]
) -> list[int]:
    """Ф62 (ревью, Н2): беседа WhatsApp переехала с номера на LID (`channel_migrate`), сообщения остались в
    старой (архивной) беседе. Удаление и реакция собеседника на старое сообщение приходят с беседой LID — ищем
    и в старых беседах, чьи ключи стали её алиасами. Только WhatsApp: id сообщений там глобально уникальны (у
    Telegram ряды номеров группы и супергруппы разные — совпадение было бы случайным)."""
    if not channel_ids or _account_source(conn, account_id) != "whatsapp":
        return channel_ids
    old = conn.execute(
        _sql(
            schema,
            "SELECT c.id FROM {schema}.channel_alias a JOIN {schema}.channel c "
            "ON c.account_id = a.account_id AND c.ext_id = a.old_ext_id "
            "WHERE a.account_id = ? AND a.channel_id = ?",
        ),
        (account_id, channel_ids[0]),
    ).fetchall()
    return [*channel_ids, *(r[0] for r in old if r[0] not in channel_ids)]


def _validate_attrs_size(attrs: dict) -> None:
    encoded = json.dumps(attrs, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > _MAX_ATTRS_BYTES:
        raise ValueError(f"attrs > {_MAX_ATTRS_BYTES} байт")


def _update_attrs_if_changed(
    conn: sqlite3.Connection,
    schema: str,
    channel_id: int,
    meta_json: str | None,
    new_attrs: dict,
    now_ms: int,
) -> bool:
    """Раздел A.1: совпало -> ничего; отличается -> записать, `channel.seq` растёт, раскладка канала (и его
    авто-топиков) пересчитывается. Возвращает True, если запись реально изменилась."""
    _validate_attrs_size(new_attrs)
    meta = json.loads(meta_json) if meta_json else {}
    if meta.get("attrs") == new_attrs:
        return False
    meta["attrs"] = new_attrs
    seq = next_seq(conn)
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET meta_json = ?, seq = ? WHERE id = ?"),
        (_dump(meta), seq, channel_id),
    )
    mapping.recompute_channel_cascade(conn, schema, channel_id, now_ms)
    return True


def _create_channel(
    conn: sqlite3.Connection, schema: str, account_id: int, ref: ChannelRef, now_ms: int
) -> int:
    if ref.attrs is not None:
        _validate_attrs_size(ref.attrs)

    parent_id = None
    if ref.parent_ext_id is not None:
        _reject_temp_ext_id(ref.parent_ext_id)
        prow = conn.execute(
            _sql(schema, "SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
            (account_id, ref.parent_ext_id),
        ).fetchone()
        if prow is not None:
            parent_id = prow[0]
        elif ref.parent is not None:
            # Родитель (форум) ещё не создан — синтезируем его из вложенной ссылки, В ТОЙ ЖЕ точке
            # сохранения (кусок 1b, раздел «Идентификаторы и маппинг в события»). `parent.ext_id` обязан
            # совпадать с `parent_ext_id`, иначе это ядовитое событие (пачку роняет только это событие,
            # соседи в пачке применяются как обычно — общий механизм SAVEPOINT/dead_letter).
            if ref.parent.ext_id != ref.parent_ext_id:
                raise ValueError(
                    f"ChannelRef.parent.ext_id ({ref.parent.ext_id!r}) не совпадает с "
                    f"parent_ext_id ({ref.parent_ext_id!r})"
                )
            parent_id = _create_channel(conn, schema, account_id, ref.parent, now_ms)
        # иначе родитель остаётся неизвестен — канал создаётся без parent_channel_id (как раньше)

    meta = {"own_id_space": ref.own_id_space}
    if ref.attrs is not None:
        meta["attrs"] = ref.attrs

    channel_seq = next_seq(conn)
    cur = conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.channel "
            "(account_id, ext_id, kind, title, parent_channel_id, first_seen_ts, map_source, meta_json, seq) "
            "VALUES (?, ?, ?, ?, ?, ?, 'auto', ?, ?)",
        ),
        (
            account_id,
            ref.ext_id,
            ref.kind,
            ref.title,
            parent_id,
            now_ms,
            _dump(meta),
            channel_seq,
        ),
    )
    channel_id = cur.lastrowid
    # Событие с attrs (или совпавшее по channel_ext_id правило без attrs) раскладывает канал сразу же, в
    # ТОЙ ЖЕ пачке (раздел A.5 спеки куска 1a) — не дожидаясь отдельного события `channel`.
    mapping.recompute_channel_cascade(conn, schema, channel_id, now_ms)
    # Ф17-1 A3/П1: беседа, появившаяся из-за вступления по заявке сессии, — в проект заявки заглушённой (до
    # вставки первого сообщения: сигнал подписке на проект оно уже не даст).
    join_mod.on_channel_created(conn, schema, account_id, channel_id, now_ms)
    # Ф62-6: беседа группы WhatsApp, созданной по заявке (отчёт пришёл раньше беседы), — в проект заявки.
    group_ops_mod.on_channel_created(conn, schema, account_id, channel_id, now_ms)
    return channel_id


def ensure_group_channel(
    conn: sqlite3.Connection, account_id: int, ext_id: str, title: str | None, now_ms: int
) -> int | None:
    """Ф62-6: беседа группы из ОТЧЁТА операции (группа WhatsApp создана по заявке или владелец в неё вступил) —
    без сообщения: WhatsApp не присылает в новую группу ничего, и беседа иначе появилась бы только с первым
    сообщением. Решение 11.5 («каналы без сообщений не создаём») для приёма не меняется — зовут только
    отчёты `group_ops.report` / `join.report`. Есть беседа `(account_id, ext_id)` — её id без правок; нет —
    тот же `_create_channel`, что у приёма (раскладка, `seq`, `/sync`, хуки привязки заявок). Нет аккаунта —
    `None`."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None
    schema = row[0]
    ref = ChannelRef(ext_id=ext_id, kind="group", title=title or None)
    found = _find_channel_id(conn, schema, account_id, ref)
    if found is not None:
        return found
    _reject_temp_ext_id(ext_id)
    return _create_channel(conn, schema, account_id, ref, now_ms)


def _rename_channel(
    conn: sqlite3.Connection, schema: str, channel_id: int, old_title: str | None, new_title: str, now_ms: int
) -> None:
    hist_row = conn.execute(
        _sql(schema, "SELECT title_history_json FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    history = json.loads(hist_row[0]) if hist_row[0] else []
    # Первое появление названия — не переименование: по истории API решает, доверять ли каналу (план, 5).
    if old_title is not None:
        history.append({"title": old_title, "ts": now_ms})
    channel_seq = next_seq(conn)
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET title = ?, title_history_json = ?, seq = ? WHERE id = ?"),
        (new_title, json.dumps(history, ensure_ascii=False), channel_seq, channel_id),
    )


def _upsert_contact(conn: sqlite3.Connection, schema: str, account_id: int, sender: SenderRef) -> int:
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.contact WHERE account_id = ? AND ext_id = ?"),
        (account_id, sender.ext_id),
    ).fetchone()
    if row is None:
        insert_sql = (
            "INSERT INTO {schema}.contact (account_id, ext_id, name, handle, is_me) VALUES (?, ?, ?, ?, ?)"
        )
        cur = conn.execute(
            _sql(schema, insert_sql),
            (account_id, sender.ext_id, sender.name, sender.handle, int(sender.is_me)),
        )
        return cur.lastrowid
    contact_id = row[0]
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.contact SET name = COALESCE(?, name), handle = COALESCE(?, handle), is_me = ? "
            "WHERE id = ?",
        ),
        (sender.name, sender.handle, int(sender.is_me), contact_id),
    )
    return contact_id


_MESSAGE_CONTENT_COLUMNS = (
    "id",
    "text",
    "entities_json",
    "attachments_json",
    "forward_json",
    "mail_to_json",
    "mentions_me",
    "mentions_broadcast",
    "mentions_group",
    "refs_json",
    "thread_ext_id",
    "thread_broadcast",
    "reply_to_ext_id",
    "grouped_id",
    "edited_ts",
    "sender_contact_id",
    "is_me",
    "kind",
)


def _apply_message(
    conn: sqlite3.Connection,
    schema: str,
    account_id: int,
    ev: UpsertEvent | EditEvent,
    now_ms: int,
    retention_cutoff: int | None = None,
    *,
    refill: _RefillLog | None = None,
) -> tuple[str, int]:
    # Ф12-1 П4б: своё письмо — копия черновика новой беседы: беседа-заготовка привязывается к его треду ДО
    # поиска/создания беседы события (тогда событие ложится на заготовку обычной склейкой).
    draft_channels.bind_on_ingest(conn, schema, account_id, ev, now_ms)
    channel_id = _ensure_channel(conn, schema, account_id, ev.channel, now_ms)

    # Поправки после opus-критика кода (R3/R4): отметка времени источников — опорное время ретенции.
    # Двигается ЛЮБЫМ upsert/edit из ЛЮБОГО аккаунта (main и work — Telegram держит её свежей, даже когда
    # Slack молчит), только вперёд, с потолком `now_ms + 1 день` (кривой ts источника не уводит её вперёд).
    # Заготовки outbox/resolve_unknown/report её не двигают — это не событие ingest'а.
    retention.bump_source_time_hwm(conn, ev.ts, now_ms)

    # Раздел D.1 спеки 2b: сводка ИСТОЧНИКА (ThreadInfo) обрабатывается ОТДЕЛЬНЫМ шагом ДО ранних выходов
    # ниже («устаревший edited_ts», «содержимое совпало») — иначе снимок родителя без правки съел бы новый
    # reply_count. Даёт свой бамп seq родителю/каналу независимо от исхода остального события.
    extra_seq = 0
    if ev.thread is not None:
        extra_seq = threads_mod.apply_source_info_and_bump(
            conn, schema, channel_id, ev.ext_id, ev.thread, now_ms
        )

    is_reply_event = threads_mod.is_reply(ev.thread_ext_id, ev.ext_id)

    sender_contact_id = (
        _upsert_contact(conn, schema, account_id, ev.sender) if ev.sender is not None else None
    )

    new = {
        "text": ev.text,
        "entities_json": _dump(ev.entities),
        # Ф8-5 A2.1: служебная ссылка коннектора (`media_ref`) — только в `media.ref_json`, не в сообщении.
        "attachments_json": _dump(media_mod.strip_media_ref(ev.attachments)),
        "forward_json": _dump(ev.forward),
        # Ф11-8 (поправка 3): как `forward_json` — и в `_MESSAGE_CONTENT_COLUMNS`, иначе повтор с адресатами
        # сравнение `old == new` сочло бы no-op. Пустой список — `NULL`.
        "mail_to_json": _dump(ev.mail_to or None),
        "mentions_me": int(ev.mentions_me),
        # Ф8-3 П11: только широковещательное упоминание — тред им не подписывается (`threads.touch()`).
        "mentions_broadcast": int(ev.mentions_me and ev.mentions_broadcast_only),
        # Ф28-2: только моя группа — упоминание для меня, но тред им не подписывается (`threads.touch()`).
        "mentions_group": int(ev.mentions_me and ev.mentions_group_only and not ev.mentions_broadcast_only),
        "refs_json": _dump(ev.refs),
        "thread_ext_id": ev.thread_ext_id,
        "thread_broadcast": int(ev.thread_broadcast),
        "reply_to_ext_id": ev.reply_to_ext_id,
        "grouped_id": ev.grouped_id,
        "edited_ts": ev.edited_ts,
        "sender_contact_id": sender_contact_id,
        "is_me": int(ev.is_me),
        "kind": _auto_forward_kind(conn, schema, account_id, ev),
    }

    def _finish(status: str, seq: int, *, stale: bool = False) -> tuple[str, int]:
        # Ф8-5 П11: строки `media` — на ВСЕХ ветках (вставка, правка, noop, эхо своей отправки); устаревшее
        # событие только заводит недостающие строки. Любая их перемена — новый `seq` сообщения (П8).
        media_seq = media_mod.sync_for_message(
            conn,
            schema,
            account_id=account_id,
            channel_id=channel_id,
            ext_id=ev.ext_id,
            attachments=ev.attachments,
            now_ms=now_ms,
            create_only=stale,
            defer_new=ev.history,
        )
        if media_seq:
            status, seq = "applied", max(seq, media_seq)
        if is_reply_event:
            bump = threads_mod.touch_and_bump(conn, schema, channel_id, ev.thread_ext_id, now_ms)
            if bump:
                status, seq = "applied", max(seq, bump)
        elif threads_mod.thread_exists(conn, schema, channel_id, ev.ext_id):
            # Поправки после opus-критика кода (T6): это событие — НЕ ответ (родитель, `thread_ext_id`
            # `None`/равен своему `ext_id`), но у него УЖЕ есть строка треда (ответы пришли раньше — инвариант
            # 2c/H: коннектор догружает родителя треда, где есть ответ). Мой родитель подписывает тред
            # («я автор родителя» — правило `touch()`), а до этого touch() на приём родителя не звался вовсе.
            bump = threads_mod.touch_and_bump(conn, schema, channel_id, ev.ext_id, now_ms)
            if bump:
                status, seq = "applied", max(seq, bump)
        if extra_seq:
            status, seq = "applied", max(seq, extra_seq)
        if ev.channel.kind == "mail_thread":
            # Ф15-1 A4: входящие реакции, пришедшие раньше письма-цели, ложатся на него значком.
            reaction_seq = mail_reactions_mod.refresh_target(conn, schema, channel_id, ev.ext_id)
            if reaction_seq:
                status, seq = "applied", max(seq, reaction_seq)
        if ev.views is not None and not stale:
            # Ф23-3 п.2: счётчик поста канала при приёме (ts — время приёма). Нет полей — `views_json` не
            # трогается; устаревший снимок (и заглушка tombstone) — тоже.
            views_seq = _store_views(
                conn, schema, channel_id, ev.ext_id, views=ev.views, forwards=ev.forwards, ts=now_ms
            )
            if views_seq:
                status, seq = "applied", max(seq, views_seq)
        return status, seq

    columns = ", ".join(_MESSAGE_CONTENT_COLUMNS)
    row = conn.execute(
        _sql(
            schema,
            "SELECT {cols}, hidden, buttons_json, buttons_ts, purged FROM {schema}.message "
            "WHERE channel_id = ? AND ext_id = ?",
            cols=columns,
        ),
        (channel_id, ev.ext_id),
    ).fetchone()

    if row is None:
        # Раздел F.1: старое сообщение, впервые пришедшее из истории уже за пределами ретенции, ложится
        # СРАЗУ очищенным.
        purged_value = 1 if (retention_cutoff is not None and ev.ts < retention_cutoff) else 0
        # Ф8-4 П2: беседа удалена владельцем в хабе, а это сообщение не новее её границы (повтор сверкой/окном,
        # догрузка истории) — ложится сразу скрытым и очищенным, беседу не возвращает.
        hidden_value = int(hiding.covered_by_boundary(conn, schema, channel_id, ev.ts, _ext_sort(ev.ext_id)))
        if hidden_value:
            purged_value = 1
        if purged_value:
            retention.clean_message_content(new)

        if not hidden_value and (ev.is_me or _thread_sub_mention(ev)):
            _drop_stale_source_sub(conn, schema, channel_id, ev, is_reply_event, evidence_ext_id=ev.ext_id)

        if ev.is_me:
            matched = outbox.match_echo(conn, schema, channel_id, ev)
            if matched is not None:
                matched_outbox_id, matched_message_id = matched
                seq = outbox.apply_echo(
                    conn,
                    schema,
                    channel_id,
                    matched_outbox_id,
                    matched_message_id,
                    ext_id=ev.ext_id,
                    ts=ev.ts,
                    content=new,
                    now_ms=now_ms,
                    purged=purged_value,
                )
                return _finish("applied", max(seq, _read_personal_threads(conn, schema, channel_id, ev)))
        # Ф63-1 П2: правила проекта для входящих — один раз, при вставке нового чужого входящего (не своего,
        # не скрытого и не очищенного, не служебного, не добора истории); итог ложится на строку сообщения.
        rules = inbound_rules_mod.NO_OUTCOME
        if not (ev.is_me or hidden_value or purged_value or ev.history or new["kind"] == "event"):
            rules = _inbound_rules(conn, schema, account_id, channel_id, ev, now_ms)
        # Ф66-3 C: при стопе PeerFlood входящее от «нового человека» снимает бейдж «ждёт: стоп» со строк
        # очереди; признак «был новым» — ДО вставки. Без стопа — одна проверка `peer_flood_ts`.
        was_new_peer = (
            not (ev.is_me or hidden_value or ev.history or new["kind"] == "event")
            and ev.channel.kind == "dm"
            and peer_flood.stop_ts(conn, account_id) is not None
            and peer_flood.is_new_peer(conn, schema, channel_id)
        )
        status, seq, message_id = _insert_message(
            conn,
            schema,
            account_id,
            channel_id,
            ev,
            new,
            purged=purged_value,
            hidden=hidden_value,
            now_ms=now_ms,
            rules=rules,
        )
        if was_new_peer:
            bumped_seq = peer_flood.bump_held_rows_channel(conn, channel_id)
            if bumped_seq:
                seq = max(seq, bumped_seq)
        if rules.archive:
            # Владелец 22.10: правило «в архив» — беседа уходит в архив до сигналов (`chan_subs` архив не будит,
            # разборщик его пропускает); открытые карточки беседы закрываются тем же `update_channel`.
            seq = max(seq, _archive_by_rule(conn, schema, channel_id, now_ms))
        if not purged_value:
            # Ф44-1a П5: кнопки новой строки — сразу; очищенная или скрытая строка рождается без кнопок.
            _store_new_buttons(conn, schema, message_id, ev)
        if not hidden_value:
            seq = max(seq, _read_personal_threads(conn, schema, channel_id, ev))
        result = _finish(status, seq)
        if ev.history:
            # Ф42-3 A: добор истории — тихо: подписки на беседу и на проект не сигналят. В журнал — число
            # вставленных и новые чужие сообщения с прямым упоминанием (не только-широковещательным, не
            # только-группой).
            if refill is not None:
                refill.inserted += 1
                if not ev.is_me and not hidden_value and _thread_sub_mention(ev):
                    refill.mentions.append((channel_id, message_id))
            return result
        if not ev.is_me and not hidden_value and new["kind"] != "event":
            # Ф14-5 A3/П20: новое чужое входящее — сигнал подписке на беседу или на проект «каждое сообщение»
            # (архив, заглушённость, время подписки проверяет `chan_subs`). Правка и повтор сюда не доходят.
            # Ф20-1 П2: ПОСЛЕ `_finish` — строка треда уже создана и подписка владельца на тред выставлена
            # (`touch_and_bump`): первый чужой ответ на родителя владельца — «ответ в треде», а не «сообщение».
            # Ф63-1 П4: правила с `wake` будят подписки своих ролей мимо пачки, «лично мне» и заглушки беседы.
            chan_subs.signal_for_message(
                conn, channel_id, message_id, ts=ev.ts, now_ms=now_ms, rule_wake=rules.wake
            )
        return result

    old = dict(zip(_MESSAGE_CONTENT_COLUMNS, row[:-4], strict=True))
    msg_id = old.pop("id")
    old_hidden, old_buttons_json, old_buttons_ts, old_purged = row[-4:]

    # Ф44-1a (2.3 + П5): кнопки — ОТДЕЛЬНЫМ шагом до ранних выходов ниже (tombstone, «устаревший
    # edited_ts», «содержимое совпало»): правку одной клавиатуры Telegram помечает `edit_hide` — у события нет
    # `edited_ts`, и после настоящей правки текста оно иначе отбрасывалось бы целиком. Своя отметка
    # `buttons_ts` не даёт старому снимку сверки затереть свежие кнопки. Не для скрытого, очищенного,
    # tombstone и старше порога ретенции. Запись — новый `seq` строке и статус `applied`; истории правок,
    # «(ред.)», сигналов подписчикам и пересчёта ключей не даёт.
    if (
        ev.buttons_ts is not None
        and not ev.tombstone
        and not old_hidden
        and not old_purged
        and not (retention_cutoff is not None and ev.ts < retention_cutoff)
    ):
        extra_seq = max(
            extra_seq,
            buttons_mod.store(
                conn,
                schema,
                msg_id,
                old_json=old_buttons_json,
                old_ts=old_buttons_ts,
                buttons=ev.buttons,
                buttons_ts=ev.buttons_ts,
            ),
        )

    # Ф8-4 П7: заглушка tombstone Slack (родитель треда удалён у источника) содержимое существующей строки
    # НЕ перезаписывает — исходный текст остаётся виден с пометкой «удалено» (её ставит следующее событие
    # `delete`); сводка треда уже применена выше, строки `media` не трогаются.
    if ev.tombstone:
        return _finish("noop", 0, stale=True)

    # Находка F7 (round 2 критика куска 1b): сверка перечитывает окно СТАРЫМ снимком, который может дойти до
    # ingest ПОЗЖЕ живой правки (гонка «сверка запросила историю -> пока ответ в пути, пришла живая правка ->
    # применяется -> сверка применяет то, что успела запросить, и затирает свежий текст старым»). Если у
    # строки УЖЕ есть `edited_ts`, а у события его нет вовсе или он МЕНЬШЕ — событие устарело относительно
    # уже применённой более свежей правки, no-op (её не затираем).
    old_edited_ts = old["edited_ts"]
    if old_edited_ts is not None and (new["edited_ts"] is None or new["edited_ts"] < old_edited_ts):
        return _finish("noop", 0, stale=True)

    # Раздел F.1: строка УЖЕ очищена (purged=1) ИЛИ ts события старше 90 дней -> содержимое события идёт через
    # ТУ ЖЕ функцию очистки перед сравнением — идентичный повтор после очистки даёт no-op без seq.
    purged_value = old_purged
    if old_purged or (retention_cutoff is not None and ev.ts < retention_cutoff):
        retention.clean_message_content(new)
        purged_value = 1
        if not old_purged:
            # Поправки после opus-критика кода (R1/R6): очистка ПРИ ПРИЁМЕ (не периодическим прогоном) —
            # строка только что перестала быть `purged=0`, её старые байты (текст, FTS-токены) ещё физически
            # в файле work. Периодический прогон может в этот раз не найти для себя ни одной строки (эта уже
            # обработана здесь) и молча ничего не почистить физически — флаг «грязно» не даёт ему пропустить
            # физический шаг просто потому, что счётчики нулевые.
            retention.mark_dirty(conn)

    if old == {k: new[k] for k in old} and purged_value == old_purged:
        return _finish("noop", 0)

    if _thread_sub_mention(ev) and not (
        old["mentions_me"] and not old["mentions_broadcast"] and not old["mentions_group"]
    ):
        # Ф28-1: правка добавила прямое упоминание меня — новое свидетельство, позже любого снимка. Ф28-2: тег
        # моей группы свидетельством не считается (тред не подписывает), замена его личным тегом — считается.
        _drop_stale_source_sub(conn, schema, channel_id, ev, is_reply_event, evidence_ext_id=None)

    # Ф8-4 C2 + П7: история правок — прежние `text`/`entities_json` уходят в `message_edit`, только если это
    # настоящая правка у источника (у события `edited_ts` есть и он другой, чем у строки) и прежний текст был.
    # Справочник Slack, смена обработки текста почты, реакции и сводка треда версий не дают.
    if (
        not purged_value
        and ev.edited_ts is not None
        and ev.edited_ts != old_edited_ts
        and old["text"] is not None
        and (new["text"] != old["text"] or new["entities_json"] != old["entities_json"])
    ):
        conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.message_edit (message_id, replaced_ts, text, entities_json) "
                "VALUES (?, ?, ?, ?)",
            ),
            (msg_id, ev.edited_ts, old["text"], old["entities_json"]),
        )

    seq = next_seq(conn)
    assignments = ", ".join(f"{col} = ?" for col in new)
    values = list(new.values())
    if purged_value != old_purged:
        # Ф44-1a П5: строка очищается при приёме — кнопки (чужой текст, сырой callback) уходят вместе с ней.
        assignments += ", purged = ?, buttons_json = NULL, buttons_ts = NULL"
        values.append(purged_value)
    update_sql = "UPDATE {schema}.message SET seq = ?, {set_clause} WHERE id = ?"
    conn.execute(_sql(schema, update_sql, set_clause=assignments), (seq, *values, msg_id))
    result = _finish("applied", seq)
    if (
        not ev.history  # Ф42-3 A2: правка, пришедшая добором истории, ключи подписок не перепроверяет
        and not ev.is_me
        and not purged_value
        and ev.edited_ts is not None
        and ev.edited_ts != old_edited_ts
        and new["text"] != old["text"]
    ):
        # Ф31-1 решение 1 + П4: настоящая правка у источника (то же условие, что история правок) поменяла
        # текст — перепроверить ключи подписок (скрытое, служебное, старое отсекает `chan_subs`). Перерасчёт
        # текста при сверке и новой обработке почты (`edited_ts` тот же) не будит.
        chan_subs.recheck_keywords(conn, msg_id, now_ms=now_ms)
    return result


def _store_new_buttons(
    conn: sqlite3.Connection, schema: str, message_id: int, ev: UpsertEvent | EditEvent
) -> None:
    """Ф44-1a: кнопки только что вставленной строки — без второго `seq`. Эхо своей заготовки сюда не идёт
    (хаб кнопок не шлёт): её кнопки, если появятся, запишет следующее событие шагом существующей строки."""
    if ev.buttons_ts is None:
        return
    buttons_mod.store(
        conn,
        schema,
        message_id,
        old_json=None,
        old_ts=None,
        buttons=ev.buttons,
        buttons_ts=ev.buttons_ts,
        fresh=True,
    )


def _thread_sub_mention(ev: UpsertEvent | EditEvent) -> bool:
    """Упоминание меня, подписывающее тред, — как `threads.thread_sub_mention_sql`: не только
    `@channel`/`@here` и (Ф28-2) не только моя группа Slack."""
    return bool(ev.mentions_me and not ev.mentions_broadcast_only and not ev.mentions_group_only)


def _drop_stale_source_sub(
    conn: sqlite3.Connection,
    schema: str,
    channel_id: int,
    ev: UpsertEvent | EditEvent,
    is_reply_event: bool,
    *,
    evidence_ext_id: str | None,
) -> None:
    """Ф28-1: мой ответ / прямое упоминание меня в ответе или родителе — сбросить устаревший снимок Slack
    «не подписан» у треда (`threads.drop_stale_source_sub`), до `touch()` в `_finish`. Родитель, сам
    несущий `subscribed` в этом событии (`conversations.replies`), — снимок свежий, не трогаем."""
    if is_reply_event:
        thread_ext_id = ev.thread_ext_id
    else:
        if ev.thread is not None and ev.thread.subscribed is not None:
            return
        thread_ext_id = ev.ext_id
    threads_mod.drop_stale_source_sub(
        conn, schema, channel_id, thread_ext_id, evidence_ext_id=evidence_ext_id
    )


def _read_personal_threads(conn: sqlite3.Connection, schema: str, channel_id: int, ev) -> int:
    """Ф14-7 A1: ВСТАВКА моего сообщения основной ленты (новое или эхо заготовки) в личке — ответы в тредах до
    него прочитаны (`threads.read_personal_threads_up_to`). Правка, реакция, повтор сюда не доходят."""
    if not ev.is_me or ev.kind == "event" or threads_mod.is_reply(ev.thread_ext_id, ev.ext_id):
        return 0
    return threads_mod.read_personal_threads_up_to(
        conn, schema, channel_id, ts=ev.ts, ext_sort=_ext_sort(ev.ext_id), ext_id=ev.ext_id
    )


def _insert_message(
    conn: sqlite3.Connection,
    schema: str,
    account_id: int,
    channel_id: int,
    ev: UpsertEvent | EditEvent,
    new: dict,
    *,
    purged: int = 0,
    hidden: int = 0,
    now_ms: int,
    rules: inbound_rules_mod.Outcome = inbound_rules_mod.NO_OUTCOME,
) -> tuple[str, int, int]:
    """Вставка нового сообщения: `(статус, seq, id строки)`. Сигнал подписке зовёт вызывающий после
    `_finish` (Ф20-1 П2). Ф63-1 П2: `rules` — итог правил входящих проекта (`rule_hits`, `rule_attention`)."""
    seq = next_seq(conn)
    # Ф20-1 П8: признак бота — только при вставке, мимо общего словаря `new` (ветка обновления пишет все его
    # ключи: правка и сверка признак не меняют).
    is_bot = int(ev.sender is not None and ev.sender.is_bot and not ev.is_me)
    # Ф57-1 П1: признак рассылки — так же только при вставке; своё письмо рассылкой не бывает.
    bulk = int(ev.bulk and not ev.is_me)
    columns = [
        "seq", "first_seq", "account_id", "channel_id", "ext_id", "ext_sort", "ts", "purged", "hidden",
        "is_bot", "bulk", "rule_hits", "rule_attention", *new.keys(),
    ]
    placeholders = ", ".join("?" for _ in columns)
    cur = conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.message ({cols}) VALUES ({placeholders})",
            cols=", ".join(columns),
            placeholders=placeholders,
        ),
        (
            seq,
            seq,
            account_id,
            channel_id,
            ev.ext_id,
            _ext_sort(ev.ext_id),
            ev.ts,
            purged,
            hidden,
            is_bot,
            bulk,
            inbound_rules_mod.dump_hits(rules.hits),
            rules.attention,
            *new.values(),
        ),
    )
    message_id = cur.lastrowid
    if not hidden:
        # Ф17-2 A6/П3: новое сообщение в беседе (форуме), из которой вышли, позже выхода — владелец снова в ней:
        # архив снимается до сигнала подписке (архивная беседа сигналов не даёт).
        leave_mod.on_message_inserted(conn, schema, channel_id, ev.ts, now_ms)
    # Ф8-4 П2: скрытое сразу (под границей удалённой беседы) беседу в списке не поднимает.
    eligible = not hidden and threads_mod.last_msg_ts_eligible_for_new_message(
        conn,
        schema,
        channel_id,
        thread_ext_id=ev.thread_ext_id,
        ext_id=ev.ext_id,
        thread_broadcast=ev.thread_broadcast,
        is_me=ev.is_me,
        # Ф8-3 П11: широковещательное упоминание тред не подписывает — и `last_msg_ts` канала не поднимает.
        # Ф28-2: тег моей группы канал поднимает (упоминание для меня), хоть тред и не подписывает.
        mentions_me=ev.mentions_me and not ev.mentions_broadcast_only,
    )
    channel_seq = next_seq(conn)
    if eligible:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.channel SET last_msg_ts = MAX(COALESCE(last_msg_ts, ?), ?), "
                "my_msg_count = my_msg_count + ?, seq = ? WHERE id = ?",
            ),
            (ev.ts, ev.ts, new["is_me"], channel_seq, channel_id),
        )
    else:
        conn.execute(
            _sql(schema, "UPDATE {schema}.channel SET my_msg_count = my_msg_count + ?, seq = ? WHERE id = ?"),
            (new["is_me"], channel_seq, channel_id),
        )
    return "applied", max(seq, channel_seq), message_id


def _apply_reaction(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: ReactionEvent, now_ms: int
) -> tuple[str, int]:
    """Находка F4 (round 2): ищем ext_id по родителю И всем его topic-детям — живая реакция в теме форума
    приходит с `peer` РОДИТЕЛЯ (Telethon), а сообщение лежит в дочернем канале-теме."""
    channel_ids = _find_channel_ids_with_topics(conn, schema, account_id, ev.channel)
    if not channel_ids:
        return "noop", 0
    channel_ids = _with_wa_alias_channels(conn, schema, account_id, channel_ids)
    placeholders = ",".join("?" for _ in channel_ids)
    row = conn.execute(
        _sql(
            schema,
            "SELECT id, reactions_json, hidden, channel_id FROM {schema}.message WHERE channel_id IN ({ph}) "
            "AND ext_id = ?",
            ph=placeholders,
        ),
        (*channel_ids, ev.ext_id),
    ).fetchone()
    if row is None:
        return "noop", 0
    msg_id, old_reactions, hidden, msg_channel_id = row
    if hidden:
        return "noop", 0  # Ф8-4 П5: удалённое у себя — реакции из источника не возвращаются, `seq` не растёт
    new_reactions = _dump(ev.reactions)
    if old_reactions == new_reactions:
        return "noop", 0
    seq = next_seq(conn)
    conn.execute(
        _sql(schema, "UPDATE {schema}.message SET reactions_json = ?, seq = ? WHERE id = ?"),
        (new_reactions, seq, msg_id),
    )
    _owner_reacted(conn, schema, msg_channel_id, msg_id, old_reactions, new_reactions, now_ms)
    return "applied", seq


def _owner_reacted(
    conn: sqlite3.Connection,
    schema: str,
    channel_id: int,
    message_id: int,
    old_json: str | None,
    new_json: str,
    now_ms: int,
) -> None:
    """Ф66-4 C: у чужого сообщения появилась МОЯ реакция (`me: true`), которой не было, — владелец поставил её в
    самом источнике: «всё до сообщения разобрано» (`handled.reacted_up_to`; отметку в источник не шлём).
    Эхо реакции сессии (пометка `reaction_by_session` или ждущая/идущая строка сессии) — не реакция владельца."""
    appeared = reactions_mod._mine(new_json)[0] - reactions_mod._mine(old_json)[0]  # noqa: SLF001
    if not appeared:
        return
    if appeared <= reactions_mod._marks(conn, schema, message_id):  # noqa: SLF001
        return
    session_row = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.reaction_ack WHERE message_id = ? AND origin = 'session'"),
        (message_id,),
    ).fetchone()
    if session_row is not None:
        return
    handled_mod.reacted_up_to(conn, channel_id=channel_id, message_id=message_id, now_ms=now_ms)


def _store_views(
    conn: sqlite3.Connection,
    schema: str,
    channel_id: int,
    ext_id: str,
    *,
    views: int,
    forwards: int | None,
    ts: int,
) -> int:
    """Ф23-3 п.3: записать `views_json` поста беседы-канала (`kind='channel'`). Возвращает новый `seq`
    сообщения, если цифры изменились; те же цифры — только свежий `ts` в `views_json`, без `seq` (0). Нет
    сообщения, оно в беседе не-канале, скрыто у себя или снимок старше записанного — ничего (0).
    Непрочитанное, `last_msg_ts`, внимание, карточки и сигналы подписок НЕ трогаются — только эта колонка и
    `seq`."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.id, m.views_json, m.hidden FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id "
            "WHERE m.channel_id = ? AND m.ext_id = ? AND c.kind = 'channel'",
        ),
        (channel_id, ext_id),
    ).fetchone()
    if row is None:
        return 0
    msg_id, old_json, hidden = row
    if hidden:
        return 0  # Ф8-4 П5: удалённое у себя — счётчики из источника не возвращаются
    old: dict = {}
    if old_json:
        try:
            parsed = json.loads(old_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            old = parsed
    old_ts = old.get("ts")
    if isinstance(old_ts, int) and ts < old_ts:
        return 0  # снимок старше записанного (сверка/проход разошлись по времени) — не откатываем цифры
    new_json = json.dumps({"views": views, "forwards": forwards, "ts": ts}, separators=(",", ":"))
    if old and old.get("views") == views and old.get("forwards") == forwards:
        if old_ts != ts:
            conn.execute(
                _sql(schema, "UPDATE {schema}.message SET views_json = ? WHERE id = ?"), (new_json, msg_id)
            )
        return 0
    seq = next_seq(conn)
    conn.execute(
        _sql(schema, "UPDATE {schema}.message SET views_json = ?, seq = ? WHERE id = ?"),
        (new_json, seq, msg_id),
    )
    return seq


def _apply_views(conn: sqlite3.Connection, schema: str, account_id: int, ev: ViewsEvent) -> tuple[str, int]:
    """Ф23-3 п.3: событие `views` — только `views_json` постов (см. `_store_views`). Неизвестная беседа или
    сообщение — no-op (не `dead_letter`), беседа не создаётся."""
    channel_id = _find_channel_id(conn, schema, account_id, ev.channel)
    if channel_id is None:
        return "noop", 0
    max_seq = 0
    for item in ev.items:
        seq = _store_views(
            conn, schema, channel_id, item.ext_id, views=item.views, forwards=item.forwards, ts=ev.ts
        )
        max_seq = max(max_seq, seq)
    return ("applied", max_seq) if max_seq else ("noop", 0)


def _apply_delete(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: DeleteEvent, now_ms: int
) -> tuple[str, int]:
    any_changed = False
    max_seq = 0
    for ext_id in ev.ext_ids:
        candidates = _delete_candidates(conn, schema, account_id, ev.channel, ext_id, now_ms)
        for msg_id, deleted, channel_id, thread_ext_id in candidates:
            # Ф8-4: `deleted` здесь — «удалено отправителем ИЛИ удалено у себя в хабе» (`_delete_candidates`):
            # скрытое удаление отправителем не трогает, `seq` не растёт.
            if deleted:
                continue
            seq = next_seq(conn)
            conn.execute(
                _sql(schema, "UPDATE {schema}.message SET deleted = 1, seq = ? WHERE id = ?"), (seq, msg_id)
            )
            any_changed = True
            max_seq = max(max_seq, seq)
            # Раздел D: удаление ответа тоже меняет сводку треда (last_reply_ts) — touch_and_bump.
            if threads_mod.is_reply(thread_ext_id, ext_id):
                bump = threads_mod.touch_and_bump(conn, schema, channel_id, thread_ext_id, now_ms)
                max_seq = max(max_seq, bump)

    return ("applied" if any_changed else "noop"), max_seq


def _delete_candidates(
    conn: sqlite3.Connection,
    schema: str,
    account_id: int,
    channel_ref: ChannelRef | None,
    ext_id: str,
    now_ms: int,
) -> list[tuple[int, int, int, str | None]]:
    del now_ms  # delete-с-каналом не создаёт канал (находка 5 приёмки 1b) — время ему тут не нужно
    if channel_ref is not None:
        # Находка F4 (round 2): ищем по родителю И всем его topic-детям — живое удаление в теме форума
        # приходит с `peer` РОДИТЕЛЯ (Telethon не отдаёт тему для `UpdateDeleteChannelMessages`), а сообщение
        # лежит в дочернем канале-теме.
        channel_ids = _find_channel_ids_with_topics(conn, schema, account_id, channel_ref)
        if not channel_ids:
            return []
        channel_ids = _with_wa_alias_channels(conn, schema, account_id, channel_ids)
        placeholders = ",".join("?" for _ in channel_ids)
        return conn.execute(
            _sql(
                schema,
                "SELECT id, deleted OR hidden, channel_id, thread_ext_id FROM {schema}.message "
                "WHERE channel_id IN ({ph}) AND ext_id = ?",
                ph=placeholders,
            ),
            (*channel_ids, ext_id),
        ).fetchall()

    joined = conn.execute(
        _sql(
            schema,
            "SELECT m.id, m.deleted OR m.hidden, m.channel_id, m.thread_ext_id, c.meta_json "
            "FROM {schema}.message m "
            "JOIN {schema}.channel c ON m.channel_id = c.id "
            "WHERE m.account_id = ? AND m.ext_id = ?",
        ),
        (account_id, ext_id),
    ).fetchall()
    return [
        (mid, deleted, cid, thread_ext_id)
        for mid, deleted, cid, thread_ext_id, meta in joined
        if not _own_id_space(meta)
    ]


def _apply_read(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: ReadEvent, now_ms: int
) -> tuple[str, int]:
    """Позиционно (находка F8 второго круга критика куска 1b): маркер двигается по `(ts, ext_sort, ext_id)`
    целевого сообщения — общей `channels.advance_read_marker` (маркер канала) или `threads.py` (маркер
    треда, `ev.thread_ext_id` задан — раздел B/D спеки 2b). Раздел A.5: `up_to_ext_id` в формате ts Slack, а
    сообщения в ядре ещё нет (отфильтрованный подтип) — позиция выводится из самой строки, маркер не
    застревает; для прочих форматов — как раньше, no-op. Ф40-1 B: позиция временем (`up_to_ts`) —
    `_apply_read_by_ts`."""
    channel_id = _find_channel_id(conn, schema, account_id, ev.channel)
    if channel_id is None:
        return "noop", 0
    if ev.up_to_ts is not None:
        return _apply_read_by_ts(conn, schema, channel_id, ev, now_ms)
    target = conn.execute(
        _sql(
            schema,
            "SELECT ts, ext_sort, ext_id, first_seq FROM {schema}.message WHERE channel_id = ? "
            "AND ext_id = ?",
        ),
        (channel_id, ev.up_to_ext_id),
    ).fetchone()
    if target is not None:
        target_ts, target_sort, target_ext, target_first_seq = target
    elif extsort.is_slack_ts(ev.up_to_ext_id):
        target_ts = extsort.slack_ts_to_ms(ev.up_to_ext_id)
        target_sort = extsort.ext_sort(ev.up_to_ext_id)
        target_ext = ev.up_to_ext_id
        target_first_seq = 0
    elif (
        reaction_ts := mail_reactions_mod.position(conn, schema, channel_id, ev.up_to_ext_id)
    ) is not None:
        # Ф15-1 П6: маркер прочтения почты указывает на письмо-реакцию (строки ленты у него нет) — позиция
        # по его `ts`, как у Slack-ts без сообщения.
        target_ts = reaction_ts
        target_sort = extsort.ext_sort(ev.up_to_ext_id)
        target_ext = ev.up_to_ext_id
        target_first_seq = 0
    else:
        return "noop", 0

    return _advance_read(
        conn,
        schema,
        channel_id,
        ev.thread_ext_id,
        ts=target_ts,
        ext_sort=target_sort,
        ext_id=target_ext,
        first_seq=target_first_seq,
        now_ms=now_ms,
    )


def _apply_read_by_ts(
    conn: sqlite3.Connection, schema: str, channel_id: int, ev: ReadEvent, now_ms: int
) -> tuple[str, int]:
    """Ф40-1 B: «прочитано до ВРЕМЕНИ» (`up_to_ts`, Mattermost). Маркер ставится прямо в позицию `(up_to_ts,
    EXT_SORT_MAX, "")`: сообщение с `ts == up_to_ts` прочитано, искать сообщение в ядре не нужно, порядок
    доставки не важен (событие может прийти раньше сообщения, которое покрывает). Своя отправка из хаба —
    не прочтение (п. 3, `threads.own_hub_send_is_last`). Дальше — прежний путь: маркер только вперёд,
    прочтение треда без строки треда — no-op."""
    if threads_mod.own_hub_send_is_last(
        conn, schema, channel_id, up_to_ts=ev.up_to_ts, thread_ext_id=ev.thread_ext_id
    ):
        return "noop", 0
    return _advance_read(
        conn,
        schema,
        channel_id,
        ev.thread_ext_id,
        ts=ev.up_to_ts,
        ext_sort=extsort.EXT_SORT_MAX,
        ext_id="",
        first_seq=0,
        now_ms=now_ms,
    )


def _advance_read(
    conn: sqlite3.Connection,
    schema: str,
    channel_id: int,
    thread_ext_id: str | None,
    *,
    ts: int,
    ext_sort: int,
    ext_id: str,
    first_seq: int,
    now_ms: int,
) -> tuple[str, int]:
    """Общий хвост события `read`: сдвиг маркера треда (`thread_ext_id` задан) или канала в готовую
    позицию."""
    if thread_ext_id is not None:
        # Поправки после opus-критика кода (T7): тред ещё не существует (ни одного ответа не приходило) ->
        # no-op, строку треда не создавать — `ReadEvent` сам по себе не «открывает» тред.
        if not threads_mod.thread_exists(conn, schema, channel_id, thread_ext_id):
            return "noop", 0
        changed = threads_mod.advance_marker_by_position(
            conn,
            schema,
            channel_id,
            thread_ext_id,
            ts=ts,
            ext_sort=ext_sort,
            ext_id=ext_id,
            now_ms=now_ms,
        )
        if not changed:
            return "noop", 0
        seq = threads_mod.bump_for_summary_change(conn, schema, channel_id, thread_ext_id)
        return "applied", seq

    changed = channels_mod.advance_read_marker(
        conn,
        schema,
        channel_id,
        ts=ts,
        ext_sort=ext_sort,
        ext_id=ext_id,
        first_seq=first_seq,
    )
    if not changed:
        return "noop", 0
    seq = conn.execute(
        _sql(schema, "SELECT seq FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()[0]
    return "applied", seq


def _apply_peer_read(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: PeerReadEvent
) -> tuple[str, int]:
    """Ф11-7, решение 1: маркер «собеседник прочитал мои до `up_to_ext_id`» — числовая позиция `ext_sort`
    (Telegram: id сообщения), в строке сообщения ядра не нуждается (догон может прийти раньше самого
    сообщения). Только вперёд; `seq` беседы растёт, только если маркер сдвинулся. Неизвестный канал и
    нечисловой `up_to_ext_id` (позиция 0) -> no-op."""
    channel_id = _find_channel_id(conn, schema, account_id, ev.channel)
    if channel_id is None:
        return "noop", 0
    if _account_source(conn, account_id) == "whatsapp":
        # Ф62-1: id сообщения WhatsApp не число — позиция маркера = время (мс) этого сообщения в беседе; его
        # ещё нет в ядре -> no-op (следующее прочтение придёт с более новым сообщением).
        found = conn.execute(
            _sql(schema, "SELECT ts FROM {schema}.message WHERE channel_id = ? AND ext_id = ?"),
            (channel_id, ev.up_to_ext_id),
        ).fetchone()
        sort = int(found[0]) if found is not None and found[0] else 0
    else:
        sort = extsort.ext_sort(ev.up_to_ext_id)
    if sort <= 0:
        return "noop", 0
    old = conn.execute(
        _sql(schema, "SELECT peer_read_sort FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()[0]
    if old is not None and sort <= old:
        return "noop", 0
    seq = next_seq(conn)
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET peer_read_sort = ?, seq = ? WHERE id = ?"),
        (sort, seq, channel_id),
    )
    return "applied", seq


def _apply_channel(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: ChannelEvent, now_ms: int
) -> tuple[str, int]:
    """Раздел A.2: обновить заголовок/атрибуты СУЩЕСТВУЮЩЕГО канала без сообщения. Неизвестный канал ->
    no-op — каналы без сообщений не создаём (решение 11.5)."""
    ref = ev.channel
    _reject_temp_ext_id(ref.ext_id)
    row = conn.execute(
        _sql(schema, "SELECT id, title, meta_json FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, ref.ext_id),
    ).fetchone()
    if row is None:
        return "noop", 0
    channel_id, old_title, meta_json = row

    changed = False
    if ref.title is not None and ref.title != old_title:
        _rename_channel(conn, schema, channel_id, old_title, ref.title, now_ms)
        changed = True
    if ref.attrs is not None and _update_attrs_if_changed(
        conn, schema, channel_id, meta_json, ref.attrs, now_ms
    ):
        changed = True

    if not changed:
        return "noop", 0
    seq = conn.execute(
        _sql(schema, "SELECT seq FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()[0]
    return "applied", seq


def _apply_channel_migrate(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: ChannelMigrateEvent, now_ms: int
) -> tuple[str, int]:
    """Раздел A.3: группа -> супергруппа и т.п. Идемпотентность — по наличию `channel_alias`: если запись
    уже есть, повтор события ничего не делает (эта запись — единственное, что делается один раз и
    необратимо).

    Ф10-5 (ревью, находка 4): беседа-тред почты -> беседа задачи/карточки трекера — новая беседа получает
    прочитанность старой по тем же письмам (`ext_id` = Message-ID): маркер встаёт на самое новое сообщение
    новой беседы, чьё письмо в старой было прочитано. Только для старой беседы вида `mail_thread` — у
    Telegram номера сообщений группы и супергруппы — разные ряды, совпадение `ext_id` там случайно."""
    _reject_temp_ext_id(ev.old_ext_id)
    _reject_temp_ext_id(ev.channel.ext_id)
    alias_row = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.channel_alias WHERE account_id = ? AND old_ext_id = ?"),
        (account_id, ev.old_ext_id),
    ).fetchone()
    if alias_row is not None:
        return "noop", 0

    old_row = conn.execute(
        _sql(
            schema,
            "SELECT id, meta_json, project_id, map_source, map_reason, muted, attention, kind, "
            "last_read_ts, last_read_sort, last_read_ext "
            "FROM {schema}.channel WHERE account_id = ? AND ext_id = ?",
        ),
        (account_id, ev.old_ext_id),
    ).fetchone()
    new_row = conn.execute(
        _sql(
            schema,
            "SELECT id, title, meta_json, map_source FROM {schema}.channel "
            "WHERE account_id = ? AND ext_id = ?",
        ),
        (account_id, ev.channel.ext_id),
    ).fetchone()
    is_new = new_row is None
    # Наследовать привязку — если новый канал не размечен руками сам. Супергруппа могла появиться раньше
    # события миграции (сообщение в ней пришло первым: порядок апдейтов Telegram не гарантирован) — тогда
    # она уже `auto`, и без этого размеченный руками чат уезжал в «Неразобранное» (находка 5 критика 1a).
    inherit = is_new

    if is_new:
        new_channel_id = _create_channel(conn, schema, account_id, ev.channel, now_ms)
    else:
        new_channel_id, new_old_title, new_meta_json, new_map_source = new_row
        inherit = new_map_source != "manual"
        if ev.channel.title is not None and ev.channel.title != new_old_title:
            _rename_channel(conn, schema, new_channel_id, new_old_title, ev.channel.title, now_ms)
        if ev.channel.attrs is not None:
            _update_attrs_if_changed(conn, schema, new_channel_id, new_meta_json, ev.channel.attrs, now_ms)

    if old_row is not None:
        (
            old_id,
            old_meta_json,
            old_project_id,
            old_map_source,
            old_map_reason,
            old_muted,
            old_attention,
            old_kind,
            old_read_ts,
            old_read_sort,
            old_read_ext,
        ) = old_row
        if inherit:
            # Новый канал наследует у старого project_id/map_source/map_reason/muted целиком, независимо
            # от того, auto он был или manual (раздел A.3). Ф8-3 П9: и ручной режим внимания — как `muted`.
            # Ф99-2 П5: пометка «заглушила сессия» не наследуется — `muted_by = NULL`.
            seq = next_seq(conn)
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.channel SET project_id = ?, map_source = ?, map_reason = ?, muted = ?, "
                    "muted_by = NULL, attention = ?, seq = ? WHERE id = ?",
                ),
                (
                    old_project_id,
                    old_map_source,
                    old_map_reason,
                    old_muted,
                    old_attention,
                    seq,
                    new_channel_id,
                ),
            )
        # Ф34-1 П2: стадия старой беседы переезжает, если у новой стадии нет (от `inherit` раскладки не
        # зависит: стадию ставят руками и на размеченную руками беседу).
        if stages_mod.inherit(conn, schema, old_id, new_channel_id):
            conn.execute(
                _sql(schema, "UPDATE {schema}.channel SET seq = ? WHERE id = ?"),
                (next_seq(conn), new_channel_id),
            )
        if old_kind == "mail_thread" and old_read_ts is not None:
            _inherit_read_by_ext_id(
                conn, schema, old_id, new_channel_id, (old_read_ts, old_read_sort or 0, old_read_ext or "")
            )
        # Ф14-5 П11/П18: живая подписка сессии на старую беседу переходит на новую.
        chan_subs.on_channel_migrate(conn, old_id, new_channel_id, now_ms=now_ms)
        # Ф34-2 П7: открытые напоминания старой беседы — тоже на новую (старая ниже уходит в архив).
        reminders_mod.on_channel_migrate(conn, old_id, new_channel_id, now_ms=now_ms)
        # Ф11-9 H2: старая беседа уходит в архив — её открытые карточки разборщика устарели (новые сообщения
        # идут в новую беседу и дают свою карточку). Тот же путь, что `agent_items.close_answered`: смена
        # статуса в транзакции записи, интерфейс узнаёт по `on_commit`.
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.agent_item SET status = 'done' "
                "WHERE channel_id = ? AND status IN ('new', 'seen')",
            ),
            (old_id,),
        )
        old_meta = json.loads(old_meta_json) if old_meta_json else {}
        old_meta["migrated_to"] = ev.channel.ext_id
        old_seq = next_seq(conn)
        conn.execute(
            _sql(schema, "UPDATE {schema}.channel SET archived = 1, meta_json = ?, seq = ? WHERE id = ?"),
            (_dump(old_meta), old_seq, old_id),
        )

    conn.execute(
        _sql(
            schema, "INSERT INTO {schema}.channel_alias (account_id, old_ext_id, channel_id) VALUES (?, ?, ?)"
        ),
        (account_id, ev.old_ext_id, new_channel_id),
    )
    max_seq = conn.execute(
        _sql(schema, "SELECT seq FROM {schema}.channel WHERE id = ?"), (new_channel_id,)
    ).fetchone()[0]
    return "applied", max_seq


def _inherit_read_by_ext_id(
    conn: sqlite3.Connection, schema: str, old_id: int, new_id: int, old_marker: tuple[int, int, str]
) -> None:
    """Ф10-5: маркер новой беседы — на самое новое её сообщение, чьё сообщение с тем же `ext_id` в старой
    беседе не дальше маркера старой. Только вперёд (`advance_read_marker`)."""
    target = conn.execute(
        _sql(
            schema,
            "SELECT n.ts, n.ext_sort, n.ext_id, n.first_seq FROM {schema}.message n "
            "JOIN {schema}.message o ON o.channel_id = ? AND o.ext_id = n.ext_id "
            "WHERE n.channel_id = ? AND n.ext_id IS NOT NULL "
            "AND (o.ts, o.ext_sort, o.ext_id) <= (?, ?, ?) "
            "ORDER BY n.ts DESC, n.ext_sort DESC, n.ext_id DESC LIMIT 1",
        ),
        (old_id, new_id, *old_marker),
    ).fetchone()
    if target is None:
        return
    ts, ext_sort, ext_id, first_seq = target
    channels_mod.advance_read_marker(
        conn, schema, new_id, ts=ts, ext_sort=ext_sort or 0, ext_id=ext_id, first_seq=first_seq or 0
    )


def _apply_account_meta(
    conn: sqlite3.Connection, schema: str, account_id: int, ev: AccountMetaEvent, now_ms: int
) -> tuple[str, int]:
    """Раздел A.4: состояние аккаунта у источника (списки папок и т.п.). Совпало -> no-op. Живёт в файле
    аккаунта — общая схема, но данные только там, где реально пишет этот аккаунт."""
    if len(ev.key) > _MAX_ACCOUNT_META_KEY_LEN:
        raise ValueError(f"account_meta key > {_MAX_ACCOUNT_META_KEY_LEN} символов")
    value_json = json.dumps(ev.value, ensure_ascii=False, sort_keys=True)
    if len(value_json.encode("utf-8")) > _MAX_ACCOUNT_META_VALUE_BYTES:
        raise ValueError(f"account_meta value > {_MAX_ACCOUNT_META_VALUE_BYTES} байт")

    row = conn.execute(
        _sql(schema, "SELECT value_json FROM {schema}.account_meta WHERE account_id = ? AND key = ?"),
        (account_id, ev.key),
    ).fetchone()
    if row is not None and row[0] == value_json:
        return "noop", 0

    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.account_meta (account_id, key, value_json, updated_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (account_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_ts = excluded.updated_ts",
        ),
        (account_id, ev.key, value_json, now_ms),
    )
    # account_meta не носит своего seq (не сущность ленты/канала) — applied считается, max_seq не растёт.
    return "applied", 0


def _apply_cursor(
    conn: sqlite3.Connection, schema: str, account_id: int, cursor: CursorIn, now_ms: int
) -> None:
    row = conn.execute(
        _sql(schema, "SELECT value, ts FROM {schema}.cursor WHERE account_id = ?"), (account_id,)
    ).fetchone()
    old_value, old_ts = row if row is not None else (None, None)

    anomaly = False
    if cursor.ts is not None:
        if cursor.ts > now_ms + _DAY_MS:
            anomaly = True
        if old_ts is not None and (cursor.ts < old_ts or cursor.ts - old_ts > _DAY_MS):
            anomaly = True

    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.cursor (account_id, value, ts, updated_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (account_id) DO UPDATE SET value = excluded.value, ts = excluded.ts, "
            "updated_ts = excluded.updated_ts",
        ),
        (account_id, cursor.value, cursor.ts, now_ms),
    )

    if anomaly:
        detail = {
            "account_id": account_id,
            "old_value": old_value,
            "old_ts": old_ts,
            "new_value": cursor.value,
            "new_ts": cursor.ts,
        }
        conn.execute(
            "INSERT INTO main.audit (ts, actor, action, detail_json) "
            "VALUES (?, 'connector', 'cursor_anomaly', ?)",
            (now_ms, json.dumps(detail, ensure_ascii=False)),
        )
