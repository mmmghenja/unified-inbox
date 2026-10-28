"""Беседа-заготовка черновика новому адресату (Ф12-1, `docs/architecture.md`–4, 6, A3–A7 и поправки П1–П4 — они главнее
текста выше них).

Беседа-заготовка — строка `channel` аккаунта с временным `ext_id = "new:<32 hex>"` и `attrs.new_to` (адресат).
Временный `ext_id` виден коннектору ТОЛЬКО в задании outbox (вместе с `new_to`): списки каналов, окно сверки,
прочитанность, реакции и медиа беседу-заготовку не видят, а события приёма с таким `ext_id` — ядовитые.

После отправки беседа-заготовка привязывается к настоящей беседе (решение 3, П3, П4):
- `rename` — настоящей беседы с этим `ext_id` у аккаунта нет: заготовка получает настоящий `ext_id` на месте;
- `merge` — есть: черновик и его строка ленты переезжают в неё, заготовка уходит в архив + `channel_alias`;
  проект заготовки найденная беседа получает, только если её создало эхо этой же отправки (П3).
Неудача, отмена или «не ушло» — заготовка в архив (решение 6, П2); архивные в лимит и уникальность не входят.

Ф12-5 (`docs/architecture.md`, A2–A5 и П1–П3, П8): беседу-заготовку заводит и сам владелец
(«Новое сообщение», `meta.new_draft.origin = "ui"`) — пустой, без строки outbox. У неё при `failed`/
`cancelled` беседа остаётся открытой (можно написать снова), в архив — «Удалить беседу из хаба» или уборкой
через 24 ч без живой строки. «Живая» строка outbox беседы-заготовки — `LIVE_STATUSES`: пока она есть, второе
сообщение не ставится и поле ввода закрыто.

Вызывать только внутри `Db.write()`; время — параметром `now_ms`, как везде в core. Аудит — только
идентификаторы и виды, без текста и без адресата.
"""

import json
import sqlite3
import uuid

from .. import msgid
from . import mapping
from . import reminders as reminders_mod
from . import stages as stages_mod
from .db import next_seq

TEMP_PREFIX = "new:"
# Ф14-2 A1: виды адресата беседы-заготовки (`attrs.new_to.kind`); прочие — как нет адресата.
# Ф34-3: `tg_phone` — Telegram по номеру телефона (номер — ПДн: `erase_phone`, `public_new_to`).
# Ф46-1: `tg_msg_author` — личка Telegram автору сообщения из группы (без @ника и номера).
NEW_TO_KINDS = ("tg_username", "email", "tg_user", "slack_users", "tg_phone", "mm_users", "tg_msg_author")
PHONE_KIND = "tg_phone"
MSG_AUTHOR_KIND = "tg_msg_author"
# Ф46-1: поля `new_draft` рядом с `to` — откуда взят автор (только для показа и ссылки «к сообщению»).
MSG_AUTHOR_FROM_KEYS = ("from_title", "from_channel_id", "from_message_id")
MAP_REASON = "новая беседа"
PEER_EXT_ID_MAX = 200
PEER_TITLE_MAX = 200
# П4: статусы черновика, при которых письмо могло уйти — его копия привязывает беседу-заготовку.
_BIND_STATUSES = ("sending", "sent", "unknown")
# Ф12-5 A2: «живая» строка outbox беседы-заготовки — первое сообщение ещё не ушло или ушло без привязки.
LIVE_STATUSES = ("pending_approval", "queued", "sending", "unknown", "sent")
# Ф12-5 П2: из них «в пути» — поле ввода закрыто «сначала уйдёт первое сообщение»; `sent` без привязки — своя
# причина (беседа привяжется по копии).
PENDING_STATUSES = ("pending_approval", "queued", "sending", "unknown")
UI_ORIGIN = "ui"
API_ORIGIN = "api"  # Ф14-3 A5: заготовка сессии `drafts/new` с `hold` (без строки outbox)
IDLE_MS = 24 * 60 * 60 * 1000  # Ф12-5 A4: UI-заготовка без живой строки и без событий дольше суток — в архив
# Ф34-3 П5: неподтверждённый черновик сессии по номеру телефона дольше 7 дней — отмена (номер стирается).
PHONE_DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в outbox.py/ingest.py: схема и доверенные фрагменты подставляются здесь."""
    return template.format(schema=schema, **parts)


def is_temp_ext_id(ext_id: object) -> bool:
    return isinstance(ext_id, str) and ext_id.startswith(TEMP_PREFIX)


def temp_ext_sql(alias: str = "c") -> str:
    """Предикат «временный `ext_id`» по индексу `(account_id, ext_id)` — диапазон вместо LIKE (тот без учёта
    регистра). `';'` — следующий после `':'` символ."""
    col = f"{alias}.ext_id" if alias else "ext_id"
    return f"({col} >= 'new:' AND {col} < 'new;')"


def not_temp_sql(alias: str = "c") -> str:
    return f"NOT {temp_ext_sql(alias)}"


def new_temp_ext_id() -> str:
    return TEMP_PREFIX + uuid.uuid4().hex


def _load_meta(meta_json: str | None) -> dict:
    if not meta_json:
        return {}
    try:
        meta = json.loads(meta_json)
    except ValueError, TypeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def new_to_of(meta_json: str | None) -> dict | None:
    """`attrs.new_to` беседы-заготовки — только словарь известного вида, иначе `None`."""
    attrs = _load_meta(meta_json).get("attrs")
    value = attrs.get("new_to") if isinstance(attrs, dict) else None
    if not isinstance(value, dict) or value.get("kind") not in NEW_TO_KINDS:
        return None
    return value


def is_ui_draft(meta_json: str | None) -> bool:
    """Ф12-5: беседу-заготовку завёл сам владелец кнопкой «Новое сообщение» (`meta.new_draft.origin == "ui"`)."""
    info = _load_meta(meta_json).get("new_draft")
    return isinstance(info, dict) and info.get("origin") == UI_ORIGIN


def is_hold_draft(meta_json: str | None) -> bool:
    """Ф14-3 A5: заготовку завела сессия `drafts/new` с `hold: true` (`meta.new_draft.hold is True`)."""
    info = _load_meta(meta_json).get("new_draft")
    return isinstance(info, dict) and info.get("hold") is True


def _in_list(statuses: tuple[str, ...]) -> str:
    return ", ".join(f"'{status}'" for status in statuses)


def draft_state_sql(schema: str, alias: str = "c") -> str:
    """Ф12-5 П2: состояние первого сообщения беседы-заготовки ОДНИМ подзапросом в SELECT выдачи (только для
    временного `ext_id`, у прочих — `NULL`): `'pending'` — живая строка в пути, `'sent'` — ушла без привязки,
    `NULL` — живой строки нет (поле ввода открыто)."""
    return _sql(
        schema,
        "(CASE WHEN {temp} THEN (SELECT CASE "
        "WHEN MAX(o.status IN ({pending})) = 1 THEN 'pending' "
        "WHEN MAX(o.status = 'sent') = 1 THEN 'sent' END "
        "FROM {schema}.outbox o WHERE o.channel_id = {c}.id) END)",
        temp=temp_ext_sql(alias),
        pending=_in_list(PENDING_STATUSES),
        c=alias,
    )


def has_live(conn, schema: str, channel_id: int) -> bool:
    """Ф12-5 A2: у беседы есть живая строка outbox (`LIVE_STATUSES`)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.outbox WHERE channel_id = ? AND status IN ({live}) LIMIT 1",
            live=_in_list(LIVE_STATUSES),
        ),
        (channel_id,),
    ).fetchone()
    return row is not None


def _has_outbox(conn, schema: str, channel_id: int) -> bool:
    """Ф14-3 A6: у беседы есть хоть одна строка outbox (любой статус)."""
    row = conn.execute(
        _sql(schema, "SELECT 1 FROM {schema}.outbox WHERE channel_id = ? LIMIT 1"), (channel_id,)
    ).fetchone()
    return row is not None


def bump(conn, schema: str, channel_id: int) -> None:
    """Ф12-5 П2: новый `seq` беседе-заготовке — `/sync` отдаёт каналы по `seq`, а `can_reply` зависит от её
    строки outbox (постановка и конечные статусы сами `seq` канала не двигают)."""
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET seq = ? WHERE id = ?"), (next_seq(conn), channel_id)
    )


def _audit(conn, *, action: str, result: str, channel_id: int, outbox_id: int | None, now_ms: int,
           detail: dict | None = None) -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO main.audit (ts, actor, action, result, channel_id, outbox_id, detail_json) "
        "VALUES (?, 'outbox', ?, ?, ?, ?, ?)",
        (now_ms, action, result, channel_id, outbox_id, json.dumps(detail) if detail is not None else None),
    )


def _channel(conn, schema: str, channel_id: int):
    return conn.execute(
        _sql(
            schema,
            "SELECT account_id, ext_id, meta_json, project_id, archived, title, title_history_json, "
            "map_source, first_seen_ts FROM {schema}.channel WHERE id = ?",
        ),
        (channel_id,),
    ).fetchone()


def is_draft_channel(conn, schema: str, channel_id: int) -> bool:
    row = conn.execute(
        _sql(schema, "SELECT ext_id FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    return row is not None and is_temp_ext_id(row[0])


def lease_new_to(conn, schema: str, channel_id: int) -> dict | None:
    """A3: поле `new_to` задания — только у беседы с временным `ext_id`; у обычной — `None`."""
    row = conn.execute(
        _sql(schema, "SELECT ext_id, meta_json FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    if row is None or not is_temp_ext_id(row[0]):
        return None
    return new_to_of(row[1])


def _drop_new_to(meta: dict) -> dict:
    attrs = meta.get("attrs")
    if isinstance(attrs, dict) and "new_to" in attrs:
        attrs = {k: v for k, v in attrs.items() if k != "new_to"}
        if attrs:
            meta["attrs"] = attrs
        else:
            meta.pop("attrs", None)
    return meta


def erase_phone(meta: dict) -> dict:
    """Ф34-3 П1: номер телефона адресата лежит в `meta_json` дважды — `attrs.new_to` и копия
    `new_draft.to` (она переживает привязку). Стирает оба места: `attrs.new_to` вида `tg_phone` удаляется,
    `new_draft.to` заменяется на `{"kind": "tg_phone"}` (вид нужен лимиту ядра и повтору по ключу).
    Прочие виды адресата не трогает. Зовут `_rename`, `_merge` и `archive` (все причины)."""
    attrs = meta.get("attrs")
    new_to = attrs.get("new_to") if isinstance(attrs, dict) else None
    if isinstance(new_to, dict) and new_to.get("kind") == PHONE_KIND:
        _drop_new_to(meta)
    info = meta.get("new_draft")
    to = info.get("to") if isinstance(info, dict) else None
    if isinstance(to, dict) and to.get("kind") == PHONE_KIND:
        meta["new_draft"] = {**info, "to": {"kind": PHONE_KIND}}
    return meta


def is_phone_draft(meta_json: str | None) -> bool:
    """Заготовка по номеру телефона: вид адресата в `new_draft.to` (переживает стирание), `attrs.new_to`."""
    meta = _load_meta(meta_json)
    info = meta.get("new_draft")
    to = info.get("to") if isinstance(info, dict) else None
    attrs = meta.get("attrs")
    new_to = attrs.get("new_to") if isinstance(attrs, dict) else None
    return any(isinstance(v, dict) and v.get("kind") == PHONE_KIND for v in (to, new_to))


def erase_phone_of(conn, schema: str, channel_id: int, meta_json: str | None) -> None:
    """`erase_phone` для строки беседы: пишет `meta_json`, только если в нём было что стирать."""
    meta = _load_meta(meta_json)
    erased = erase_phone(json.loads(json.dumps(meta)))
    if erased != meta:
        conn.execute(
            _sql(schema, "UPDATE {schema}.channel SET meta_json = ? WHERE id = ?"),
            (json.dumps(erased, ensure_ascii=False, sort_keys=True), channel_id),
        )


def manual_archive_cancellable(conn, schema: str, channel_id: int) -> list[int] | None:
    """Ревью Ф34-3: заготовку по номеру телефона убирают в архив руками (`PATCH archived`, `hub channel
    archive`). Возвращает id строк outbox, которые вызывающий отменяет (`outbox.cancel`): неподтверждённые
    (`pending_approval`) и подтверждённые, но ещё не выдававшиеся коннектору (`queued` без
    `send_started_ts`). `None` — беседа не заготовка по номеру. Выданная строка не трогается: номер ей
    нужен, сотрёт его `settle` после её исхода."""
    row = _channel(conn, schema, channel_id)
    if row is None or not is_temp_ext_id(row[1]) or not is_phone_draft(row[2]):
        return None
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id FROM {schema}.outbox WHERE channel_id = ? AND (status = 'pending_approval' "
            "OR (status = 'queued' AND send_started_ts IS NULL)) ORDER BY id",
        ),
        (channel_id,),
    ).fetchall()
    return [r[0] for r in rows]


def erase_phone_if_idle(conn, schema: str, channel_id: int) -> None:
    """Архивная заготовка по номеру без живой строки outbox — номер стирается (пустая, `hold`, отменённая)."""
    row = _channel(conn, schema, channel_id)
    if row is None or not is_temp_ext_id(row[1]) or not row[4] or has_live(conn, schema, channel_id):
        return
    erase_phone_of(conn, schema, channel_id, row[2])


def public_new_to(new_to: dict | None, *, full: bool) -> dict | None:
    """Ф34-3 П2: адресат беседы-заготовки в выдаче API. Номер телефона — только UI-сессии владельца (`full`: он
    подтверждает номер, выбранный сессией); bearer получает `{"kind": "tg_phone"}` без номера."""
    if new_to is None or full or new_to.get("kind") != PHONE_KIND:
        return new_to
    return {"kind": PHONE_KIND}


def is_msg_author_draft(meta_json: str | None) -> bool:
    """Ф46-1 П1: заготовка «личка автору сообщения из группы» — по `new_draft.to.kind` или `attrs.new_to`
    (заготовку мог завести и владелец кликом — тогда сессия кладёт в неё обычный `hub_draft`)."""
    meta = _load_meta(meta_json)
    info = meta.get("new_draft")
    to = info.get("to") if isinstance(info, dict) else None
    attrs = meta.get("attrs")
    new_to = attrs.get("new_to") if isinstance(attrs, dict) else None
    return any(isinstance(v, dict) and v.get("kind") == MSG_AUTHOR_KIND for v in (to, new_to))


BOT_USERNAME_SUFFIX = "bot"


def is_bot_username(username: object) -> bool:
    """Ф63-2 П2, П3: ник Telegram оканчивается на «bot» (без учёта регистра) — по правилу Telegram так
    обязаны кончаться ники ботов и не могут — ники людей. Только ПРЕДВАРИТЕЛЬНЫЙ признак: коннектор при
    каждой отправке сверяет `entity.bot`."""
    return isinstance(username, str) and username.lstrip("@").lower().endswith(BOT_USERNAME_SUFFIX)


def is_bot_draft(meta_json: str | None) -> bool:
    """Ф63-2 П2: беседа-заготовка `new:` по @username, оканчивающемуся на «bot» (`attrs.new_to`), — беседа с
    ботом ДО отправки. Только чтобы выбрать ветку `bot_chat` (и страховку); по названию беседы — никогда."""
    new_to = new_to_of(meta_json)
    if new_to is None or new_to.get("kind") != "tg_username":
        return False
    return is_bot_username(new_to.get("username"))


def public_new_to_of(meta_json: str | None, *, full: bool) -> dict | None:
    """Адресат беседы-заготовки в выдаче API по её `meta_json` (`new_to_of` + `public_new_to`).

    Ф46-1: у вида `tg_msg_author` наружу — `kind`, `user_id`, `name` и откуда взят автор (`from_title`,
    `from_channel_id`, `from_message_id` из `new_draft`); служебные `chat_ext_id` / `msg_ext_id` (ими
    коннектор получает доступ к человеку) не отдаются никому."""
    new_to = new_to_of(meta_json)
    if new_to is None or new_to.get("kind") != MSG_AUTHOR_KIND:
        return public_new_to(new_to, full=full)
    out = {"kind": MSG_AUTHOR_KIND, "user_id": new_to.get("user_id"), "name": new_to.get("name")}
    info = _load_meta(meta_json).get("new_draft")
    for key in MSG_AUTHOR_FROM_KEYS:
        out[key] = info.get(key) if isinstance(info, dict) else None
    return out


def _bump_messages(conn, schema: str, channel_id: int) -> None:
    ids = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.message WHERE channel_id = ? ORDER BY id"), (channel_id,)
    ).fetchall()
    for (message_id,) in ids:
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (next_seq(conn), message_id)
        )


def archive(conn, schema: str, channel_id: int, *, reason: str, outbox_id: int | None, now_ms: int) -> bool:
    """Решение 6 + П2: беседа-заготовка в архив (не удаляется). Обычная беседа не трогается. `True` — ушла.

    Ф34-3 П1: у заготовки по номеру телефона номер при этом стирается из `meta_json` (`erase_phone`) — при
    любой причине: `failed`, `cancelled`, `idle`, `hidden`, `sent_unbound`."""
    row = _channel(conn, schema, channel_id)
    if row is None or not is_temp_ext_id(row[1]):
        return False
    # Ревью Ф34-3: и у заготовки, уже убранной в архив руками (`channels.update_channel`), номер стирается.
    erase_phone_of(conn, schema, channel_id, row[2])
    if row[4]:
        return False
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET archived = 1, seq = ? WHERE id = ?"),
        (next_seq(conn), channel_id),
    )
    _audit(
        conn,
        action="draft_channel_archive",
        result=reason,
        channel_id=channel_id,
        outbox_id=outbox_id,
        now_ms=now_ms,
    )
    return True


def settle(conn, schema: str, channel_id: int, *, status: str, outbox_id: int, now_ms: int) -> None:
    """П2: судьба заготовки после конечного статуса её черновика: `failed`/`cancelled` — в архив; `sent` без
    привязки (`resolve_unknown(sent)` — `ext_id` так и не узнали) — в архив, но остаётся кандидатом привязки
    по копии письма (П4б). Прочие статусы (`unknown`, `queued`) беседу не трогают.

    Ф12-5 A3 + П2: беседа-заготовка UI (`new_draft.origin == "ui"`) при `failed`/`cancelled` НЕ уходит в
    архив — владелец пишет снова в неё же; у любой беседы-заготовки здесь двигается `seq` (`can_reply` в
    `/sync`)."""
    row = _channel(conn, schema, channel_id)
    if row is None or not is_temp_ext_id(row[1]):
        return
    if row[4] and status in ("failed", "cancelled", "sent"):
        # Ревью Ф34-3: заготовка уже в архиве (убрана руками) — после исхода её строки номер стирается и у
        # UI-заготовки, которую `failed`/`cancelled` в архив не ведут.
        erase_phone_of(conn, schema, channel_id, row[2])
        row = _channel(conn, schema, channel_id)  # ниже `meta_json` переписывается — уже без номера
    if status in ("failed", "cancelled"):
        if not is_ui_draft(row[2]):
            archive(conn, schema, channel_id, reason=status, outbox_id=outbox_id, now_ms=now_ms)
        else:
            # Ревью Ф12-5: момент неудачи — событие для уборки A4 (`send_started_ts` — первая попытка, а
            # `failed` по «не ушло» или после долгого flood бывает и через сутки).
            meta = _load_meta(row[2])
            draft = meta.get("new_draft") if isinstance(meta.get("new_draft"), dict) else {}
            meta["new_draft"] = {**draft, "settled_ts": now_ms}
            conn.execute(
                _sql(schema, "UPDATE {schema}.channel SET meta_json = ? WHERE id = ?"),
                (json.dumps(meta, ensure_ascii=False, sort_keys=True), channel_id),
            )
    elif status == "sent":
        archive(conn, schema, channel_id, reason="sent_unbound", outbox_id=outbox_id, now_ms=now_ms)
    bump(conn, schema, channel_id)


def archive_hidden(conn, schema: str, channel_id: int, *, now_ms: int) -> bool:
    """Ф12-5 A5 + П3: «Удалить беседу из хаба» у беседы-заготовки — после обычного скрытия (`hide_channel`:
    очистка текста и файлов, отмена невыданного) в архив, если живой строки не осталось. `True` — ушла."""
    row = _channel(conn, schema, channel_id)
    if row is None or not is_temp_ext_id(row[1]) or has_live(conn, schema, channel_id):
        return False
    return archive(conn, schema, channel_id, reason="hidden", outbox_id=None, now_ms=now_ms)


def cleanup_idle(conn, *, now_ms: int) -> int:
    """Ф12-5 A4 + П8: беседа-заготовка UI без живой строки, чьё последнее событие — создание беседы, строка
    outbox (создание или попытка отправки), неудача первого сообщения (`new_draft.settled_ts`) или загрузка
    файла — старше суток, -> архив `idle`. Заготовки
    Claude не трогаются (их судьбу решает `settle`). Возвращает число ушедших в архив.

    Ф14-3 A6: и `hold`-заготовка сессии (`new_draft.hold`), у которой нет НИ ОДНОЙ строки outbox (по тем же
    суткам и без загрузок моложе суток): сессия упала между `drafts/new` и `send`. Со строкой — как раньше."""
    cutoff = now_ms - IDLE_MS
    archived = 0
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT c.id, c.meta_json FROM {schema}.channel c WHERE {temp} AND c.archived = 0 "
                "AND c.first_seen_ts < ? "
                "AND COALESCE(json_extract(c.meta_json, '$.new_draft.settled_ts'), 0) < ? "
                "AND NOT EXISTS (SELECT 1 FROM {schema}.outbox o WHERE o.channel_id = c.id "
                "AND (o.status IN ({live}) OR o.created_ts >= ? OR COALESCE(o.send_started_ts, 0) >= ?)) "
                "AND NOT EXISTS (SELECT 1 FROM {schema}.upload u WHERE u.channel_id = c.id "
                "AND u.created_ts >= ?)",
                temp=temp_ext_sql("c"),
                live=_in_list(LIVE_STATUSES),
            ),
            (cutoff, cutoff, cutoff, cutoff, cutoff),
        ).fetchall()
        for channel_id, meta_json in rows:
            idle = is_ui_draft(meta_json) or (
                is_hold_draft(meta_json) and not _has_outbox(conn, schema, channel_id)
            )
            if idle and archive(
                conn, schema, channel_id, reason="idle", outbox_id=None, now_ms=now_ms
            ):
                archived += 1
    return archived


def expired_phone_drafts(conn, *, now_ms: int) -> list[int]:
    """Ф34-3 П5: черновики (`pending_approval`) открытых бесед-заготовок по номеру телефона старше 7 дней —
    их id. Отменяет вызывающий (`outbox.cancel`, часовой проход `runner`): отмена ведёт заготовку сессии в
    архив (`settle`), а архив стирает номер."""
    cutoff = now_ms - PHONE_DRAFT_TTL_MS
    found: list[int] = []
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT o.id FROM {schema}.outbox o JOIN {schema}.channel c ON c.id = o.channel_id "
                "WHERE {temp} AND o.status = 'pending_approval' AND o.created_ts < ? "
                "AND json_extract(c.meta_json, '$.new_draft.to.kind') = ? ORDER BY o.id",
                temp=temp_ext_sql("c"),
            ),
            (cutoff, PHONE_KIND),
        ).fetchall()
        found.extend(row[0] for row in rows)
    return found


def _rename(
    conn, schema: str, channel_id: int, new_ext_id: str, *, title: str | None, outbox_id: int | None,
    now_ms: int, via: str,
) -> None:  # fmt: skip
    """Решение 3: беседы `(аккаунт, new_ext_id)` нет — заготовка получает настоящий `ext_id` на месте
    (`new_to` уходит из `attrs`, архив снимается — это уже настоящая беседа).

    Ф14-2 A3 (П2): беседа-заготовка «по правилам» (`map_source='auto'`) после переименования
    пересчитывается правилами с топиками — правило по id беседы применяется сразу, по папкам/доменам — со
    следующим событием беседы (его `attrs` пересчитывает приём)."""
    row = _channel(conn, schema, channel_id)
    old_ext_id, meta_json, old_title, history_json = row[1], row[2], row[5], row[6]
    map_source = row[7]
    meta = erase_phone(_load_meta(meta_json))  # Ф34-3 П1: номер — до `_drop_new_to` (вид ещё виден)
    meta = _drop_new_to(meta)
    draft = meta.get("new_draft") if isinstance(meta.get("new_draft"), dict) else {}
    meta["new_draft"] = {**draft, "bound": "rename"}
    history = []
    if history_json:
        try:
            parsed = json.loads(history_json)
            history = parsed if isinstance(parsed, list) else []
        except ValueError, TypeError:
            history = []
    new_title = old_title
    if title and title != old_title:
        if old_title is not None:
            history.append({"title": old_title, "ts": now_ms})
        new_title = title
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.channel SET ext_id = ?, archived = 0, meta_json = ?, title = ?, "
            "title_history_json = ?, seq = ? WHERE id = ?",
        ),
        (
            new_ext_id,
            json.dumps(meta, ensure_ascii=False, sort_keys=True),
            new_title,
            json.dumps(history, ensure_ascii=False) if history else history_json,
            next_seq(conn),
            channel_id,
        ),
    )
    _bump_messages(conn, schema, channel_id)
    if map_source == "auto":
        mapping.recompute_channel_cascade(conn, schema, channel_id, now_ms)
    _audit(
        conn,
        action="draft_bind",
        result="rename",
        channel_id=channel_id,
        outbox_id=outbox_id,
        now_ms=now_ms,
        detail={"via": via, "was_temp": is_temp_ext_id(old_ext_id)},
    )


def _merge(
    conn, schema: str, draft_id: int, target_id: int, *, outbox_id: int | None, now_ms: int, via: str
) -> None:
    """Решение 3 + П3: черновик и строки ленты заготовки -> в найденную беседу; заготовка — в архив +
    `channel_alias`. Строка заготовки, чей `ext_id` уже есть в найденной беседе (эхо пришло раньше), не
    переезжает: `outbox_id` переходит к настоящей строке, заготовка гаснет (как `_report_sent`). Счётчик моих
    сообщений переезжает вместе со строкой, если черновик уже засчитан (`sent`)."""
    draft = _channel(conn, schema, draft_id)
    target = _channel(conn, schema, target_id)
    draft_ext, draft_meta, draft_project, draft_map_source = draft[1], draft[2], draft[3], draft[7]
    target_project, target_map_source, target_first_seen = target[3], target[7], target[8]

    outbox_rows = conn.execute(
        _sql(
            schema,
            "SELECT id, status, send_started_ts FROM {schema}.outbox WHERE channel_id = ? ORDER BY id",
        ),
        (draft_id,),
    ).fetchall()
    status_of = {oid: status for oid, status, _started in outbox_rows}
    # Ревью Ф12-1 (П3): отсчёт — ПЕРВАЯ попытка отправки, не создание черновика: беседа, которую человек начал
    # сам, пока черновик ждал подтверждения, — прошлая переписка, её проект не трогается.
    # Ф12-5 П1: и не попытки ПРОШЛЫХ строк (неудачная отправка UI-заготовки, затем повтор) — отсчёт по строке,
    # которая привязывает (`outbox_id`: отчитавшаяся `sent` / чей Message-ID совпал на приёме).
    started_of = {oid: st for oid, _status, st in outbox_rows}
    if outbox_id is not None and outbox_id in started_of:
        started_ts = started_of[outbox_id]
    else:
        started_ts = min((st for st in started_of.values() if st is not None), default=None)

    messages = conn.execute(
        _sql(
            schema,
            "SELECT id, ext_id, outbox_id, ts, deleted FROM {schema}.message WHERE channel_id = ? "
            "ORDER BY id",
        ),
        (draft_id,),
    ).fetchall()
    moved_counted = 0
    dropped_counted = 0
    max_ts = None
    for message_id, ext_id, m_outbox_id, ts, deleted in messages:
        counted = not deleted and m_outbox_id is not None and status_of.get(m_outbox_id) == "sent"
        real = None
        if ext_id is not None:
            real = conn.execute(
                _sql(schema, "SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?"),
                (target_id, ext_id),
            ).fetchone()
        if real is not None:
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.message SET outbox_id = NULL, deleted = 1, seq = ? WHERE id = ?",
                ),
                (next_seq(conn), message_id),
            )
            if m_outbox_id is not None:
                conn.execute(
                    _sql(schema, "UPDATE {schema}.message SET outbox_id = ?, seq = ? WHERE id = ?"),
                    (m_outbox_id, next_seq(conn), real[0]),
                )
            dropped_counted += int(counted)
            continue
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET channel_id = ?, seq = ? WHERE id = ?"),
            (target_id, next_seq(conn), message_id),
        )
        if counted:
            moved_counted += 1
            max_ts = ts if max_ts is None else max(max_ts, ts)

    conn.execute(
        _sql(schema, "UPDATE {schema}.outbox SET channel_id = ? WHERE channel_id = ?"), (target_id, draft_id)
    )
    if moved_counted:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.channel SET my_msg_count = my_msg_count + ?, "
                "last_msg_ts = MAX(COALESCE(last_msg_ts, ?), ?) WHERE id = ?",
            ),
            (moved_counted, max_ts, max_ts, target_id),
        )
    if moved_counted or dropped_counted:
        conn.execute(
            _sql(schema, "UPDATE {schema}.channel SET my_msg_count = MAX(my_msg_count - ?, 0) WHERE id = ?"),
            (moved_counted + dropped_counted, draft_id),
        )

    # П3: проект заготовки — только беседе, которую создало эхо ЭТОЙ отправки (появилась не раньше первой
    # попытки отправки) и не привязанной руками; прошлая переписка к проектному токену не переезжает.
    # Ревью Ф14-2: переезжает только проект, выбранный руками (`manual`); у заготовки «по правилам» (`auto`)
    # проект — от правил, настоящая беседа уже разложена ими при приёме и остаётся `auto`.
    fresh = target_first_seen is not None and started_ts is not None and target_first_seen >= started_ts
    project_moved = False
    by_hand = draft_map_source != "auto"
    if by_hand and fresh and target_map_source != "manual" and draft_project is not None:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.channel SET project_id = ?, map_source = 'manual', map_reason = ? "
                "WHERE id = ?",
            ),
            (draft_project, MAP_REASON, target_id),
        )
        project_moved = True
    elif by_hand and target_project != draft_project:
        _audit(
            conn,
            action="draft_merge_other_project",
            result="project_kept",
            channel_id=target_id,
            outbox_id=outbox_id,
            now_ms=now_ms,
            detail={"draft_channel_id": draft_id, "fresh": fresh, "manual": target_map_source == "manual"},
        )

    # Ф34-1, решение 4: стадия заготовки переезжает, если у настоящей беседы стадии нет (`seq` настоящей
    # двигается ниже).
    stages_mod.inherit(conn, schema, draft_id, target_id)
    # Ф34-2 П7: открытые напоминания заготовки (на первое письмо) переезжают на настоящую беседу — заготовка
    # ниже уходит в архив, а напоминание беседы в архиве снялось бы.
    reminders_mod.on_channel_migrate(conn, draft_id, target_id, now_ms=now_ms)
    # 24.10: подписки сессий на заготовку (её роль подписалась, положив первое сообщение) — на настоящую
    # беседу: заготовка ниже уходит в архив, а подписку беседы в архиве сняла бы ленивая проверка.
    from . import chan_subs as chan_subs_mod  # noqa: PLC0415 — chan_subs тянет subs и прочее: цикл импорта

    chan_subs_mod.on_channel_migrate(conn, draft_id, target_id, now_ms=now_ms)

    meta = erase_phone(_load_meta(draft_meta))  # Ф34-3 П1: номер — до `_drop_new_to` (вид ещё виден)
    meta = _drop_new_to(meta)
    meta["merged_into"] = target[1]
    info = meta.get("new_draft") if isinstance(meta.get("new_draft"), dict) else {}
    meta["new_draft"] = {**info, "bound": "merge"}
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET archived = 1, meta_json = ?, seq = ? WHERE id = ?"),
        (json.dumps(meta, ensure_ascii=False, sort_keys=True), next_seq(conn), draft_id),
    )
    conn.execute(
        _sql(
            schema,
            "INSERT OR IGNORE INTO {schema}.channel_alias (account_id, old_ext_id, channel_id) "
            "VALUES (?, ?, ?)",
        ),
        (draft[0], draft_ext, target_id),
    )
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET seq = ? WHERE id = ?"), (next_seq(conn), target_id)
    )
    _audit(
        conn,
        action="draft_bind",
        result="merge",
        channel_id=target_id,
        outbox_id=outbox_id,
        now_ms=now_ms,
        detail={"via": via, "draft_channel_id": draft_id, "project_moved": project_moved},
    )


def _find_channel(conn, schema: str, account_id: int, ext_id: str) -> int | None:
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, ext_id),
    ).fetchone()
    return row[0] if row is not None else None


def _account(conn, account_id: int) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT source, config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1]) if row is not None else (None, None)


def valid_peer_ext_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= PEER_EXT_ID_MAX
        and value == value.strip()
        and not is_temp_ext_id(value)
    )


def bind_after_sent(
    conn,
    schema: str,
    *,
    outbox_id: int,
    channel_id: int,
    ext_id: str,
    peer_ext_id: str | None,
    peer_title: str | None,
    now_ms: int,
    peer_bot: bool = False,
) -> int:
    """A4 + решение 3 + П4а: отчёт `sent` по черновику беседы-заготовки. Возвращает беседу, где теперь живёт
    черновик (`_report_sent` пишет `ext_id` уже в ней). Обычная беседа — как есть.

    Почта: своё сообщение с этим `ext_id` (Message-ID) уже есть в ДРУГОЙ беседе аккаунта (копия пришла раньше
    отчёта) — слияние в неё. Иначе `peer_ext_id`: беседа есть — слияние, нет — переименование. Без того и
    другому (почта не нашла копию) беседа остаётся заготовкой — привяжет приём (П4б).

    Ф63-2 П2: `peer_bot` (Telegram, адресат — бот) — беседе, где теперь живёт черновик, ставится
    `attrs.tg_type = "bot"` (прочие `attrs` не затираются): ответ боту сразу после `/start` идёт веткой
    `bot_chat`. Без `peer_ext_id` признак не применяется."""
    row = _channel(conn, schema, channel_id)
    if row is None or not is_temp_ext_id(row[1]):
        return channel_id
    account_id = row[0]
    source, _config = _account(conn, account_id)

    if source == "mail":
        other = conn.execute(
            _sql(
                schema,
                "SELECT m.channel_id FROM {schema}.message m JOIN {schema}.channel c ON c.id = m.channel_id "
                "WHERE m.account_id = ? AND m.ext_id = ? AND m.channel_id <> ? AND {not_temp} LIMIT 1",
                not_temp=not_temp_sql("c"),
            ),
            (account_id, ext_id, channel_id),
        ).fetchone()
        if other is not None:
            _merge(conn, schema, channel_id, other[0], outbox_id=outbox_id, now_ms=now_ms, via="report_echo")
            return other[0]

    if peer_ext_id is None:
        return channel_id
    target = _find_channel(conn, schema, account_id, peer_ext_id)
    if target is None:
        title = peer_title[:PEER_TITLE_MAX] if isinstance(peer_title, str) and peer_title.strip() else None
        _rename(conn, schema, channel_id, peer_ext_id, title=title, outbox_id=outbox_id, now_ms=now_ms,
                via="report")  # fmt: skip
        if peer_bot and source == "telegram":
            _mark_bot(conn, schema, channel_id)
        return channel_id
    _merge(conn, schema, channel_id, target, outbox_id=outbox_id, now_ms=now_ms, via="report")
    if peer_bot and source == "telegram":
        _mark_bot(conn, schema, target)
    return target


def _mark_bot(conn, schema: str, channel_id: int) -> None:
    """Ф63-2 П2: `attrs.tg_type = "bot"` у беседы (остальные `attrs` как были; `seq` — если изменилось)."""
    row = conn.execute(
        _sql(schema, "SELECT meta_json FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    if row is None:
        return
    meta = _load_meta(row[0])
    attrs = meta.get("attrs") if isinstance(meta.get("attrs"), dict) else {}
    if attrs.get("tg_type") == "bot":
        return
    meta["attrs"] = {**attrs, "tg_type": "bot"}
    conn.execute(
        _sql(schema, "UPDATE {schema}.channel SET meta_json = ?, seq = ? WHERE id = ?"),
        (json.dumps(meta, ensure_ascii=False, sort_keys=True), next_seq(conn), channel_id),
    )


def _mail_address(config_json: str | None) -> str | None:
    config = _load_meta(config_json)
    address = config.get("address")
    if not isinstance(address, str) or "@" not in address:
        return None
    return address.strip().lower()


def bind_on_ingest(conn: sqlite3.Connection, schema: str, account_id: int, ev, now_ms: int) -> None:
    """П4б: своё (`is_me`) событие ПОЧТЫ, чьё `ext_id` — Message-ID черновика беседы-заготовки этого аккаунта
    (`sending|sent|unknown`, в т.ч. архивной), привязывает заготовку к треду события: треда в ядре нет —
    переименование, есть — слияние. Дальше событие применяется обычным путём (склейка с заготовкой по
    `send_token` или `(channel, ext_id)`). Для Telegram не применяется (у бесед свои ряды id)."""
    if not ev.is_me or is_temp_ext_id(ev.channel.ext_id):
        return
    source, config_json = _account(conn, account_id)
    if source != "mail":
        return
    address = _mail_address(config_json)
    if address is None:
        return
    placeholders = ",".join("?" for _ in _BIND_STATUSES)
    candidates = conn.execute(
        _sql(
            schema,
            "SELECT c.id, o.id, o.send_token FROM {schema}.outbox o JOIN {schema}.channel c "
            "ON c.id = o.channel_id WHERE c.account_id = ? AND {temp} AND o.status IN ({ph}) "
            "AND o.send_token IS NOT NULL ORDER BY o.id",
            temp=temp_ext_sql("c"),
            ph=placeholders,
        ),
        (account_id, *_BIND_STATUSES),
    ).fetchall()
    for draft_id, outbox_id, send_token in candidates:
        if msgid.our_message_id(send_token, address) != ev.ext_id:
            continue
        target = _find_channel(conn, schema, account_id, ev.channel.ext_id)
        if target is None:
            _rename(conn, schema, draft_id, ev.channel.ext_id, title=None, outbox_id=outbox_id, now_ms=now_ms,
                    via="ingest")  # fmt: skip
        elif target != draft_id:
            _merge(conn, schema, draft_id, target, outbox_id=outbox_id, now_ms=now_ms, via="ingest")
        return
