"""Ф69-1 (docs/architecture.md, A–D, поправки П1–П9): спам-ограничение Telegram
(PeerFlood) останавливает только первые сообщения новым людям, а не весь аккаунт.

«Новый человек» (`is_new_peer`, П2) — беседа Telegram-лички (`kind='dm'`), не бот (`bot_guard.chat_is_bot`),
и беседа-заготовка (`new:…`) ИЛИ в ней нет ни
одного сообщения собеседника (`is_me=0 AND kind<>'event'`). Новых признаков беседы не заводится.

Стоп — `main.account.peer_flood_ts` (миграция 0097). Пока стоит, `outbox.lease` и
`outbox_queries.has_leasable` не выдают строки бесед «новый человек» (остаются `queued`), остальная отправка
идёт. Снимает только владелец: `resume_ui` (кнопка «Снять стоп») или `admin.resume_account`
(`hub account resume`).
"""

from . import draft_channels
from .db import db_of, next_seq

PEER_FLOOD_ERROR = "PeerFlood"
# П4: текст причины `failed` строки, поймавшей PeerFlood (`outbox.error`; видят владелец и сессии).
FAILED_TEXT = (
    "Telegram не дал написать: спам-ограничение на первые сообщения новым людям (PeerFlood). "
    "Отправить заново — новым сообщением после снятия стопа."
)
HELD = "peer_flood"  # П7: значение поля `held` строки очереди, которую держит стоп
# Аудит `peer_flood`: исход (B).
SCOPE_NEW = "new_peers"
SCOPE_ACCOUNT = "account"


def _sql(schema: str, template: str) -> str:
    return template.format(schema=schema)


def is_peer_flood_report(outcome: str, error: str | None) -> bool:
    """П3: признак PeerFlood в отчёте коннектора — `error == "PeerFlood"` и у `permanent` (новый коннектор),
    и у `temporary` (старый, окно деплоя)."""
    return error == PEER_FLOOD_ERROR and outcome in ("permanent", "temporary")


def is_new_peer(conn, schema: str, channel_id: int) -> bool:
    """П2: беседа — «новый человек» (см. шапку модуля)."""
    from . import bot_guard  # noqa: PLC0415 — bot_guard -> channels -> outbox -> peer_flood: цикл импорта

    row = conn.execute(
        _sql(
            schema,
            "SELECT a.source, c.kind, c.ext_id, c.meta_json FROM {schema}.channel c "
            "JOIN main.account a ON a.id = c.account_id WHERE c.id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if row is None:
        return False
    source, kind, ext_id, meta_json = row
    if source != "telegram" or kind != "dm":
        return False
    if bot_guard.chat_is_bot(source, kind, ext_id, meta_json):
        return False
    # Ревью Ф69-1: «в моих контактах» (`attrs.contact` = Telethon `User.contact`) не защищает от PeerFlood —
    # Telegram пропускает тех, у кого есть МОЙ номер; признака взаимности в беседе нет, поэтому контакты
    # не исключаются: «старый» — только тот, кто уже писал.
    if draft_channels.is_temp_ext_id(ext_id):
        return True
    inbound = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.message WHERE channel_id = ? AND is_me = 0 AND kind <> 'event' LIMIT 1",
        ),
        (channel_id,),
    ).fetchone()
    return inbound is None


def held_checker(conn, schema: str, peer_flood_ts: int | None):
    """Для выдачи (`lease`/`has_leasable`): функция `channel_id -> bool` «строку беседы держит стоп» с кэшем
    на вызов; без стопа — всегда `False` без запросов."""
    if peer_flood_ts is None:
        return lambda _channel_id: False
    cache: dict[int, bool] = {}

    def _held(channel_id: int) -> bool:
        if channel_id not in cache:
            cache[channel_id] = is_new_peer(conn, schema, channel_id)
        return cache[channel_id]

    return _held


def stop_ts(conn, account_id: int) -> int | None:
    row = conn.execute("SELECT peer_flood_ts FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def stop_for_channel(conn, channel_id: int) -> int | None:
    """П7 (ответ `drafts/new`): `peer_flood_ts` аккаунта беседы, если её строки держит стоп (беседа — «новый
    человек»); иначе `None` (стопа нет или беседа не новая — черновик уйдёт как обычно)."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT a.peer_flood_ts FROM {schema}.channel c JOIN main.account a ON a.id = c.account_id "
            "WHERE c.id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0] if is_new_peer(conn, schema, channel_id) else None


def attach_held(conn, messages: list[dict]) -> None:
    """Дополнение к П7 (головная 16.10): поле `outbox_held` строки ленты — `"peer_flood"`, когда её строка
    очереди `queued` и её держит стоп; у прочих остаётся `None`. Без строк в очереди — ни одного запроса."""
    queued = [m for m in messages if m.get("outbox_status") == "queued" and m.get("outbox_id") is not None]
    if not queued:
        return
    held: dict[int, bool] = {}
    for message in queued:
        channel_id = message["channel_id"]
        if channel_id not in held:
            held[channel_id] = stop_for_channel(conn, channel_id) is not None
        if held[channel_id]:
            message["outbox_held"] = HELD


def held_of(conn, outbox_id: int) -> str | None:
    """П7: `"peer_flood"`, когда строка `queued`, её беседа — «новый человек» и у аккаунта стоит стоп; иначе
    `None`."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT o.status, o.channel_id, a.peer_flood_ts FROM {schema}.outbox o "
            "JOIN main.account a ON a.id = o.account_id WHERE o.id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        return None
    status, channel_id, peer_flood_ts = row
    if status != "queued" or peer_flood_ts is None:
        return None
    return HELD if is_new_peer(conn, schema, channel_id) else None


def held_map(conn, rows) -> dict[int, str | None]:
    """`held` для пачки строк `(outbox_id, status, account_id, channel_id)` (`/sync`): стоп аккаунта и признак
    беседы читаются по разу."""
    stops: dict[int, int | None] = {}
    checkers: dict[tuple[str, int], object] = {}
    out: dict[int, str | None] = {}
    for outbox_id, status, account_id, channel_id in rows:
        if status != "queued":
            out[outbox_id] = None
            continue
        if account_id not in stops:
            stops[account_id] = stop_ts(conn, account_id)
        if stops[account_id] is None:
            out[outbox_id] = None
            continue
        schema = db_of(outbox_id)
        key = (schema, account_id)
        if key not in checkers:
            checkers[key] = held_checker(conn, schema, stops[account_id])
        out[outbox_id] = HELD if checkers[key](channel_id) else None
    return out


def bump_held_rows(conn, account_id: int) -> int:
    """Стоп поставлен или снят: строкам ленты `queued` бесед «новый человек» этого аккаунта — новый `seq`,
    чтобы `/sync` донёс поменявшееся `outbox_held`. Возвращает число сдвинутых строк."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return 0
    schema = row[0]
    rows = conn.execute(
        _sql(
            schema,
            "SELECT m.id, o.channel_id FROM {schema}.outbox o JOIN {schema}.message m ON m.outbox_id = o.id "
            "WHERE o.account_id = ? AND o.status = 'queued' ORDER BY o.id",
        ),
        (account_id,),
    ).fetchall()
    new_peer: dict[int, bool] = {}
    bumped = 0
    for message_id, channel_id in rows:
        if channel_id not in new_peer:
            new_peer[channel_id] = is_new_peer(conn, schema, channel_id)
        if new_peer[channel_id]:
            conn.execute(
                _sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (next_seq(conn), message_id)
            )
            bumped += 1
    return bumped


def bump_held_rows_channel(conn, channel_id: int) -> int:
    """Ф66-3 C: человек написал первым — беседа перестала быть «новым человеком»; строкам ленты `queued` этой
    беседы — новый `seq`, чтобы `/sync` донёс снятый `outbox_held`. Возвращает наибольший выданный `seq`
    (0 — сдвигать было нечего)."""
    schema = db_of(channel_id)
    rows = conn.execute(
        _sql(
            schema,
            "SELECT m.id FROM {schema}.outbox o JOIN {schema}.message m ON m.outbox_id = o.id "
            "WHERE o.channel_id = ? AND o.status = 'queued' ORDER BY o.id",
        ),
        (channel_id,),
    ).fetchall()
    last = 0
    for (message_id,) in rows:
        last = next_seq(conn)
        conn.execute(_sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (last, message_id))
    return last


def apply_report(conn, schema: str, *, account_id: int, channel_id: int, now_ms: int) -> str:
    """B.2/B.3: стоп после PeerFlood на строке беседы `channel_id`. Беседа «новый человек» — `peer_flood_ts`
    (первый стоп не переписывается; статус аккаунта не трогается); иначе весь аккаунт `limited` с
    `last_error='PeerFlood'` (П3). Зовётся в транзакции `outbox.report` ДО `draft_channels.settle` (заготовка
    уходит в архив). Возвращает исход для аудита: `new_peers` | `account`."""
    if is_new_peer(conn, schema, channel_id):
        if stop_ts(conn, account_id) is None:
            conn.execute("UPDATE main.account SET peer_flood_ts = ? WHERE id = ?", (now_ms, account_id))
            bump_held_rows(conn, account_id)
        return SCOPE_NEW
    conn.execute(
        "UPDATE main.account SET status = 'limited', last_error = ? WHERE id = ?",
        (PEER_FLOOD_ERROR, account_id),
    )
    return SCOPE_ACCOUNT


def resume_ui(conn, account_id: int) -> list[str] | None:
    """П6: кнопка «Снять стоп» — снимает только `peer_flood_ts` и `limited` с `last_error='PeerFlood'`;
    `auth_required`, `no_network`, `flood_until` не трогает. `None` — аккаунта нет; `[]` — снимать нечего;
    иначе что снято (`"peer_flood"`, `"limited"`)."""
    row = conn.execute(
        "SELECT status, last_error, peer_flood_ts FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return None
    status, last_error, peer_flood_ts = row
    cleared: list[str] = []
    if peer_flood_ts is not None:
        bump_held_rows(conn, account_id)  # пока стоп стоит — признак беседы тот же, что видела лента
        conn.execute("UPDATE main.account SET peer_flood_ts = NULL WHERE id = ?", (account_id,))
        cleared.append("peer_flood")
    if status == "limited" and last_error == PEER_FLOOD_ERROR:
        conn.execute("UPDATE main.account SET status = 'ok', last_error = NULL WHERE id = ?", (account_id,))
        cleared.append("limited")
    return cleared
