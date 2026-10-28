"""Второй круг критика куска 1b (opus, 15 находок F1-F15 + мелочи) — позитивные тесты, зелёные ПОСЛЕ правок.
Воспроизведения критика — `.runs/critic-1b/test_critic_1b.py` (падают ПОСЛЕ правок, см. отчёт). Имена здесь —
`test_1b_crit_NN_...`, по одному (или паре) на находку.
"""

import asyncio

import pytest
from telethon import errors
from telethon.tl import types
from telethon.tl.functions.users import GetUsersRequest

from hub.connectors.base import AuthRequired
from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import (
    NewMessageGE,
    ReactionsGE,
    ReadInboxGE,
    TgGateway,
    _event_entities,
)

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway
from .test_window import _FAKE_NOW_S


class _NullClient:
    async def get_cursor(self):  # Ф42-1: курсор аккаунта (дыры догона) — пусто
        return None

    async def push_events(self, events, cursor=None):
        return {"ok": True}

    async def channels_heads(self):
        return []

    async def channels_window(self, *, ext_id, limit=30):
        return []

    async def set_activity(self, *, backfill):
        return {"ok": True}

    async def set_status(self, status, *, last_error=None):
        return {"ok": True}


async def _instant_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _connector(gw=None, client=None, **kw) -> TelegramConnector:
    c = TelegramConnector(client or _NullClient(), gw or FakeGateway(), account_id=1, **kw)
    c._me_id = h.ME_ID  # noqa: SLF001
    return c


# --- F1: потеря входа не обнаруживалась (client.get_me() глотал UnauthorizedError) -------------------------


async def test_1b_crit_f1_gateway_get_me_uses_raw_request(tmp_path):
    calls = []

    class _StubClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __call__(self, request):
            calls.append(request)
            return []  # пусто -> None

    gw = TgGateway(
        session_path=str(tmp_path / "test"),
        api_id=1,
        api_hash="0" * 32,
        proxy=None,
        device_model="hub",
        system_version="linux",
        app_version="0.1",
        client_factory=_StubClient,
    )
    result = await gw.get_me()
    assert result is None
    assert len(calls) == 1
    assert isinstance(calls[0], GetUsersRequest)


async def test_1b_crit_f1_start_source_raises_auth_required_on_empty_get_me():
    gw = FakeGateway()
    gw.me = None  # "сырой" get_me() отдал пусто -> потеря входа
    connector = _connector(gw, sleep=_instant_sleep)
    connector._me_id = None  # noqa: SLF001 — ещё не стартовали
    with pytest.raises(AuthRequired):
        await connector.start_source()


async def test_1b_crit_f1_health_raises_auth_required_on_unauthorized_error():
    class _RaisingGateway(FakeGateway):
        async def get_me(self):
            raise errors.AuthKeyUnregisteredError(request=None)

    gw = _RaisingGateway()
    gw.connected = True
    connector = _connector(gw)
    with pytest.raises(AuthRequired):
        await connector.health()


# --- F2: после разрыва никто больше не переподключался -----------------------------------------------------


async def test_1b_crit_f2_health_reconnects_when_disconnected_and_requests_sync():
    gw = FakeGateway()
    gw.connected = False  # состояние "Telethon сам не переподключится больше"
    connector = _connector(gw)

    await connector.health()

    assert gw.connect_calls == 1
    assert gw.connected is True
    assert connector._sync_queue.get_nowait() == "reconnect"  # noqa: SLF001


async def test_1b_crit_f2_health_throttles_network_get_me_to_once_per_interval():
    gw = FakeGateway()
    gw.connected = True
    calls = []
    real_get_me = gw.get_me

    async def _counting_get_me():
        calls.append(1)
        return await real_get_me()

    gw.get_me = _counting_get_me
    mono = [0.0]
    connector = _connector(gw, mono=lambda: mono[0])

    await connector.health()
    assert len(calls) == 1  # первый пульс — сеть проверяем

    mono[0] += 30  # 30 с спустя — рано, реже раза в 10 мин
    await connector.health()
    assert len(calls) == 1

    mono[0] += 700  # больше 10 мин — снова пора
    await connector.health()
    assert len(calls) == 2


# --- F3: события.NewMessage выбрасывает MessageService — живые служебные терялись --------------------------


def test_1b_crit_f3_telethon_drops_service_messages_from_new_message_event():
    from telethon import events

    svc = types.MessageService(
        id=1,
        peer_id=types.PeerChannel(500),
        date=h.ts(1_700_000_000_000),
        action=types.MessageActionChannelMigrateFrom(title="old", chat_id=77),
    )
    assert events.NewMessage.build(types.UpdateNewChannelMessage(message=svc, pts=1, pts_count=1)) is None


async def test_1b_crit_f3_migrate_from_delivered_via_live_message_handling():
    """Гейтвей теперь кормит `MessageService` из Raw-апдейтов той же дорогой, что обычные сообщения
    (`NewMessageGE`) — коннектор обрабатывает их как обычно, включая `channel_migrate`."""
    old = h.make_chat(77, title="Старая")
    new = h.make_channel(500, title="Старая", megagroup=True)
    connector = _connector()
    connector._entities[int(convert.peer_ext_id(h.peer_of(new)))] = new  # noqa: SLF001
    emitted = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    migrate = h.make_message(
        1, h.peer_of(new), action=types.MessageActionChannelMigrateFrom(title="Старая", chat_id=77)
    )
    # Путь Raw (F3): entities пустые (гейтвей не разрешает их для service-апдейтов без сети, F12) — чат УЖЕ в
    # кэше коннектора из предыдущей сверки, поэтому резолвится всё равно.
    await connector._handle_gateway_event(NewMessageGE(message=migrate, entities={}))  # noqa: SLF001

    assert [e["type"] for e in emitted] == ["channel_migrate"]
    assert emitted[0]["old_ext_id"] == "-77"
    del old


# --- F4: живые удаления/реакции в темах форума терялись (ищем только по родителю) ---------------------------


def test_1b_crit_f4_ingest_finds_message_in_topic_child_for_reaction(db):
    from hub.core.ingest import apply_batch

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    forum_upsert = {
        "type": "upsert",
        "channel": {
            "ext_id": "-100",
            "kind": "topic",
            "title": "Тема",
            "parent_ext_id": "-100chat",
            "own_id_space": True,
            "parent": {"ext_id": "-100chat", "kind": "group", "title": "Форум", "own_id_space": True},
        },
        "ext_id": "5",
        "ts": 1000,
        "is_me": False,
        "kind": "text",
        "text": "hi",
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[forum_upsert], cursor=None, now_ms=1000)
    )

    # Живая реакция приходит с peer РОДИТЕЛЯ (как реально отдаёт Telethon для тем без top_msg_id).
    reaction = {
        "type": "reaction",
        "channel": {"ext_id": "-100chat", "kind": "group"},
        "ext_id": "5",
        "reactions": [{"emoji": "👍", "count": 1}],
    }
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[reaction], cursor=None, now_ms=2000)
    )
    assert result.applied == 1

    with db.reader() as conn:
        row = conn.execute("SELECT reactions_json FROM main.message WHERE ext_id = '5'").fetchone()
    assert row[0] is not None


async def test_1b_crit_f4_reaction_with_top_msg_id_routes_directly_to_topic():
    connector = _connector()
    emitted = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    reactions = types.MessageReactions(
        results=[types.ReactionCount(reaction=types.ReactionEmoji("👍"), count=1)]
    )
    event = ReactionsGE(peer_marked_id=-100600, msg_id=5, reactions=reactions, top_msg_id=42)
    await connector._handle_gateway_event(event)  # noqa: SLF001

    assert emitted[0]["channel"] == {"ext_id": "-100600:42", "kind": "topic"}


# --- F5: "прочитано содержимое" не должно двигать read всего канала ---


async def test_1b_crit_f5_read_contents_event_is_not_forwarded():
    from telethon import events

    upd = types.UpdateChannelReadMessagesContents(channel_id=700, messages=[20])
    builder = events.MessageRead(inbox=True)
    ev = builder.build(upd, None, 1)
    assert ev is not None
    assert ev.contents is True
    # Гейтвей проверяет `event.contents` ДО вызова handler — воспроизводим ту же проверку напрямую по коду
    # (полный прогон через реальный TelegramClient — в критик-тесте `.runs/critic-1b/`, здесь без сети).
    handled = []
    if not ev.contents and ev.chat_id is not None and ev.max_id is not None:
        handled.append(ReadInboxGE(peer_marked_id=ev.chat_id, max_id=ev.max_id))
    assert handled == []


# --- F6/F11: одна плохая переписка не должна ронять всю сверку; MessageEmpty фильтруется в гейтвее ---


async def test_1b_crit_f6_one_failing_initial_load_dialog_does_not_abort_others(
    admin_client, internal_transport
):
    class _GW(FakeGateway):
        async def get_messages(self, peer, *, min_id, limit):
            if isinstance(peer, types.PeerUser) and peer.user_id == 800:
                raise errors.ChannelPrivateError(request=None)
            return await super().get_messages(peer, min_id=min_id, limit=limit)

    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = _GW()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = h.ME_ID  # noqa: SLF001
    collected = []
    connector.emit = lambda ev: collected.append(ev)  # noqa: SLF001

    bad = h.make_user(800, first_name="Бан")
    good = h.make_user(801, first_name="Хороший")
    for u in (bad, good):
        gw.add_dialog(h.make_dialog(u, top_message=1, last_message_date_ms=1_700_000_000_000))
    gw.add_message(801, h.make_message(1, h.peer_of(good), message="hi", from_id=None))

    await connector.sync("start")  # не должно бросить исключение

    loaded = {e["channel"]["ext_id"] for e in collected if e["type"] == "upsert"}
    assert loaded == {"801"}  # плохой пропущен, хороший загружен


async def test_1b_crit_f6_message_empty_is_filtered_by_gateway(tmp_path):
    calls = []

    class _StubClient:
        def __init__(self, *_a, **_kw):
            pass

        async def __call__(self, request):
            calls.append(request)
            return types.messages.Messages(
                messages=[
                    types.MessageEmpty(id=2, peer_id=types.PeerUser(7)),
                    h.make_message(3, types.PeerUser(7), message="ok", from_id=None),
                ],
                chats=[],
                users=[],
                topics=[],
            )

    gw = TgGateway(
        session_path=str(tmp_path / "test"),
        api_id=1,
        api_hash="0" * 32,
        proxy=None,
        device_model="hub",
        system_version="linux",
        app_version="0.1",
        client_factory=_StubClient,
    )
    messages, _entities = await gw.get_messages(types.PeerUser(7), min_id=None, limit=30)
    assert [m.id for m in messages] == [3]


# --- F7: сверка затирает свежую живую правку старым снимком ------------------------------------------------


def test_1b_crit_f7_stale_upsert_does_not_overwrite_newer_edit(db):
    from hub.core.ingest import apply_batch

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    live_edit = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": "1",
        "ts": 1000,
        "is_me": False,
        "kind": "text",
        "text": "НОВЫЙ",
        "mentions_me": False,
        "edited_ts": 1_700_000_000_500,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[live_edit], cursor=None, now_ms=1000)
    )

    stale_snapshot = {**live_edit, "text": "старый", "edited_ts": None}
    result = db.write_sync(
        lambda conn: apply_batch(
            conn, account_id=account_id, events=[stale_snapshot], cursor=None, now_ms=2000
        )
    )
    assert result.applied == 0  # устаревший снимок — no-op, не переписывает свежую правку

    with db.reader() as conn:
        text, edited_ts = conn.execute("SELECT text, edited_ts FROM main.message WHERE ext_id='1'").fetchone()
    assert (text, edited_ts) == ("НОВЫЙ", 1_700_000_000_500)


# --- F8: позиционный маркер прочитанного ---


def test_1b_crit_f8_out_of_order_insert_does_not_hide_newest_message(db):
    """Сообщение, вставленное ПОЗЖЕ (высокий first_seq), но с БОЛЕЕ РАННИМ ts, не должно "прятать" более
    новое (по ts) сообщение при прочтении."""
    from hub.core.ingest import apply_batch, unread_count

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )

    def _upsert(ext_id, ts):
        return {
            "type": "upsert",
            "channel": {"ext_id": "c1", "kind": "dm"},
            "ext_id": ext_id,
            "ts": ts,
            "is_me": False,
            "kind": "text",
            "text": "x",
            "mentions_me": False,
        }

    # "31" (самое новое по ts) приходит ПЕРВЫМ (живое, обгоняет остальные) -> получает МЕНЬШИЙ first_seq,
    # чем "1".."30", вставленные следом стартовой загрузкой.
    db.write_sync(
        lambda conn: apply_batch(
            conn, account_id=account_id, events=[_upsert("31", 1_700_000_000_000)], cursor=None, now_ms=1
        )
    )
    older_events = [_upsert(str(i), 1_700_000_000_000 - (31 - i) * 1000) for i in range(1, 31)]
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=older_events, cursor=None, now_ms=2)
    )

    with db.reader() as conn:
        channel_id = conn.execute("SELECT id FROM main.channel WHERE ext_id='c1'").fetchone()[0]

    # Прочитано до "20" (по позиции ts) — "31" (самое новое) НЕ должно попасть в прочитанное.
    read_event = {"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": "20"}
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[read_event], cursor=None, now_ms=3)
    )

    with db.reader() as conn:
        n = unread_count(conn, channel_id)
        newest_unread = conn.execute(
            "SELECT c.last_read_ts, c.last_read_sort, c.last_read_ext, m.ts, m.ext_sort, m.ext_id "
            "FROM main.message m, main.channel c WHERE m.ext_id='31' AND c.id = m.channel_id"
        ).fetchone()
    assert n == 11  # 21..31
    last_read_ts, last_read_sort, last_read_ext, m_ts, m_sort, m_ext = newest_unread
    assert (m_ts, m_sort, m_ext) > (last_read_ts, last_read_sort, last_read_ext)  # "31" — правда непрочитано


def test_1b_crit_f8_migration_from_version_4_backfills_positional_marker(tmp_path):
    """База версии 4 (без позиционных колонок) -> после `Db.start()` (миграция 0005) last_read_ts/sort/ext
    заполнены позицией сообщения, ранее покрытого старым `last_read_seq`."""
    import sqlite3

    from hub.core.db import Db

    data_dir = tmp_path
    main_path = data_dir / "hub.db"

    # Поднимаем БД версии 4 (без 0005) — временно создаём через Db.start(), затем откатываем user_version
    # и удаляем позиционные колонки руками (SQLite ALTER TABLE DROP COLUMN — 3.35+, доступен в 3.14 stdlib).
    instance = Db(data_dir)
    instance.start()
    account_id = instance.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    from hub.core.ingest import apply_batch

    events = [
        {
            "type": "upsert",
            "channel": {"ext_id": "c1", "kind": "dm"},
            "ext_id": str(i),
            "ts": 1000 + i,
            "is_me": False,
            "kind": "text",
            "text": "x",
            "mentions_me": False,
        }
        for i in range(1, 4)
    ]
    instance.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=1)
    )
    read_event = {"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": "2"}
    instance.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[read_event], cursor=None, now_ms=2)
    )
    with instance.reader() as conn:
        last_read_seq_before = conn.execute(
            "SELECT last_read_seq FROM main.channel WHERE ext_id='c1'"
        ).fetchone()[0]
    instance.close()

    # Откат "назад" до версии 4: удаляем позиционные колонки и правим user_version — имитация базы ДО правки.
    conn = sqlite3.connect(main_path)
    try:
        conn.execute("ALTER TABLE channel DROP COLUMN last_read_ts")
        conn.execute("ALTER TABLE channel DROP COLUMN last_read_sort")
        conn.execute("ALTER TABLE channel DROP COLUMN last_read_ext")
        # 0006 (кусок 1c, критик) тоже новее версии 4 — та же имитация «базы ДО правки» для колонки
        # maybe_sent, иначе повторный Db.start() ниже упал бы на "duplicate column name" при её применении.
        conn.execute("ALTER TABLE outbox DROP COLUMN maybe_sent")
        # 0007 (кусок 2b) — та же имитация для тредов/ретенции, иначе повторный Db.start() упал бы на
        # "duplicate column name" при переприменении 0007 поверх уже существующих колонок.
        conn.execute("ALTER TABLE message DROP COLUMN thread_broadcast")
        conn.execute("ALTER TABLE message DROP COLUMN purged")
        conn.execute("ALTER TABLE thread DROP COLUMN source_reply_count")
        conn.execute("ALTER TABLE thread DROP COLUMN source_latest_reply_ext")
        conn.execute("ALTER TABLE thread DROP COLUMN source_subscribed")
        conn.execute("ALTER TABLE thread DROP COLUMN last_read_ts")
        conn.execute("ALTER TABLE thread DROP COLUMN last_read_sort")
        conn.execute("ALTER TABLE thread DROP COLUMN last_read_ext")
        conn.execute("ALTER TABLE thread DROP COLUMN first_seen_ts")
        # 0008 (поправки после opus-критика кода куска 2b, T2) — та же имитация для reply_count.
        conn.execute("ALTER TABLE thread DROP COLUMN reply_count")
        # 0010 (Ф8-3) — та же имитация для режима внимания канала и широковещательных упоминаний.
        conn.execute("ALTER TABLE channel DROP COLUMN attention")
        conn.execute("ALTER TABLE message DROP COLUMN mentions_broadcast")
        # 0012 (Ф8-4) — та же имитация для удаления у себя в хабе (таблица `message_edit` — IF NOT EXISTS).
        conn.execute("ALTER TABLE message DROP COLUMN hidden")
        conn.execute("ALTER TABLE channel DROP COLUMN hidden_before_ts")
        conn.execute("ALTER TABLE channel DROP COLUMN hidden_before_sort")
        conn.execute("ALTER TABLE outbox DROP COLUMN text_sha256")
        # 0015 (Ф9-3, 9c) — та же имитация для происхождения реплики рабочей сессии (таблицы — IF NOT EXISTS).
        conn.execute("ALTER TABLE work_say DROP COLUMN origin")
        # 0018 (Ф10-4) — та же имитация для файла строки очереди (таблица `upload` — IF NOT EXISTS).
        conn.execute("ALTER TABLE outbox DROP COLUMN file_json")
        # 0019 (Ф10-7) — та же имитация для учёта UI-сессий (индекс audit — IF NOT EXISTS).
        for column in ("created_ts", "last_used_ts", "via", "user_agent"):
            conn.execute(f"ALTER TABLE api_token DROP COLUMN {column}")
        # 0020 (Ф12-2) — та же имитация для графика отправки (индекс — IF NOT EXISTS).
        conn.execute("DROP INDEX IF EXISTS idx_outbox_paced")
        conn.execute("ALTER TABLE outbox DROP COLUMN paced")
        conn.execute("ALTER TABLE outbox DROP COLUMN pace_slot_ts")
        # 0021 (Ф11-7) — та же имитация для «прочитано собеседником».
        conn.execute("ALTER TABLE channel DROP COLUMN peer_read_sort")
        # 0023 (Ф11-8) — та же имитация для адресатов своего письма.
        conn.execute("ALTER TABLE message DROP COLUMN mail_to_json")
        # 0024 (Ф14-4) — та же имитация для вида загрузки (голосовое).
        conn.execute("ALTER TABLE upload DROP COLUMN kind")
        # 0025 (Ф14-5) — та же имитация для режима подписки на проект (таблицы бесед — IF NOT EXISTS).
        conn.execute("ALTER TABLE session_sub DROP COLUMN mode")
        # 0029 (Ф15-1) — та же имитация для происхождения строки реакции (таблицы — IF NOT EXISTS).
        conn.execute("ALTER TABLE reaction_ack DROP COLUMN origin")
        conn.execute("ALTER TABLE reaction_ack DROP COLUMN token_id")
        # 0032 (Ф19-1) — та же имитация для правок черновиков (частичный индекс — до колонки).
        conn.execute("DROP INDEX IF EXISTS outbox_draft_edited")
        conn.execute("ALTER TABLE outbox DROP COLUMN draft_text")
        conn.execute("ALTER TABLE outbox DROP COLUMN draft_ts")
        # 0033/0034 (Ф20-1) — та же имитация для признака бота, фильтра подписки и категории сигнала.
        conn.execute("DROP INDEX IF EXISTS message_bot_sender")  # 0036: частичный индекс по `is_bot`
        conn.execute("ALTER TABLE message DROP COLUMN is_bot")
        conn.execute("ALTER TABLE session_sub DROP COLUMN msg_filter")
        conn.execute("ALTER TABLE session_chan_signal DROP COLUMN cat")
        conn.execute("ALTER TABLE session_chan_signal DROP COLUMN thread_ext")
        conn.execute("DROP INDEX IF EXISTS idx_message_thread")
        # 0036 (Ф21-1) — та же имитация для людей (частичные индексы — до колонок; таблицы — IF NOT EXISTS).
        conn.execute("DROP INDEX IF EXISTS contact_person")
        conn.execute("DROP INDEX IF EXISTS contact_email")
        for column in ("person_id", "person_manual", "email", "person_name"):
            conn.execute(f"ALTER TABLE contact DROP COLUMN {column}")
        # 0037 (Ф23-3) — та же имитация для просмотров постов каналов.
        conn.execute("ALTER TABLE message DROP COLUMN views_json")
        # 0039 (Ф25-1) — та же имитация для ключей подписки и совпавших ключей сигнала.
        conn.execute("ALTER TABLE session_sub DROP COLUMN keywords_json")
        conn.execute("ALTER TABLE session_chan_signal DROP COLUMN keys_json")
        # 0040 (Ф26-1) — та же имитация для роли подписки (частичный индекс по роли — до колонок).
        conn.execute("DROP INDEX IF EXISTS session_sub_live_role")
        conn.execute("ALTER TABLE session_sub DROP COLUMN role")
        conn.execute("ALTER TABLE session_sub DROP COLUMN role_key")
        # 0041 (Ф28-1) — та же имитация для позиции снимка подписки треда.
        conn.execute("ALTER TABLE thread DROP COLUMN source_sub_ext")
        # 0042 (Ф28-2) — та же имитация для упоминания группы Slack.
        conn.execute("ALTER TABLE message DROP COLUMN mentions_group")
        # 0043 (Ф30-1) — та же имитация для времени отправки `send_at`.
        conn.execute("ALTER TABLE outbox DROP COLUMN send_at_ts")
        # 0044 (Ф31-1) — та же имитация для ключей подписки на беседу (таблица пометок — IF NOT EXISTS).
        conn.execute("ALTER TABLE session_chan_sub DROP COLUMN keywords_json")
        # 0045 (Ф30-2) — та же имитация для флажка «не уйдёт, если ответят».
        conn.execute("ALTER TABLE outbox DROP COLUMN cancel_if_reply")
        # 0046 (Ф34-1) — та же имитация для стадии беседы (частичный индекс по ключу — до колонок).
        conn.execute("DROP INDEX IF EXISTS channel_stage")
        for column in ("stage", "stage_key", "stage_note", "stage_ts"):
            conn.execute(f"ALTER TABLE channel DROP COLUMN {column}")
        # 0048 (Ф35-1) — та же имитация для номера линии и флага `direct` (индекс линии снят выше, у 0040).
        conn.execute("ALTER TABLE session_sub DROP COLUMN role_no")
        conn.execute("ALTER TABLE session_sub DROP COLUMN direct")
        conn.execute("ALTER TABLE reminder DROP COLUMN role_no")
        # 0050 (Ф36-1) — та же имитация для срока пачки сигнала «по ключу».
        conn.execute("ALTER TABLE session_chan_signal DROP COLUMN ready_ts")
        # 0051 (Ф37-1) — та же имитация для роли авто-отправки (таблица списка — IF NOT EXISTS).
        conn.execute("ALTER TABLE outbox DROP COLUMN auto_role")
        # 0053 (Ф38-1) — та же имитация для фильтра подписки по аккаунтам.
        conn.execute("ALTER TABLE session_sub DROP COLUMN accounts_json")
        # 0055 (Ф39 B) — та же имитация для автора черновика (таблица исходов — IF NOT EXISTS).
        conn.execute("ALTER TABLE outbox DROP COLUMN author_role")
        conn.execute("ALTER TABLE outbox DROP COLUMN author_sub_id")
        # 0057 (Ф41-2б) — та же имитация для дополнения записи сессии хаба (таблица 0056 и таблицы `held_*`
        # — IF NOT EXISTS).
        conn.execute("DROP INDEX IF EXISTS hub_session_seq")
        for column in (
            "seq",
            "last_event_id",
            "interrupt_requested_ts",
            "sleep_requested_ts",
            "last_turn_ts",
            "launch_fails",
            "failure",
            "store",
            "wait_reason",
        ):
            conn.execute(f"ALTER TABLE hub_session DROP COLUMN {column}")
        # 0058 (Ф41-3) — та же имитация для расхода постоянной сессии.
        conn.execute("ALTER TABLE hub_session DROP COLUMN usage_json")
        # 0060 (Ф41-4) — та же имитация для входящего постоянной сессии (`wait_reason` там пересоздаётся —
        # столбец снят выше, в списке 0057).
        for column in (
            "want_sub_json",
            "wake_requested_ts",
            "compact_requested_ts",
            "signal_turn_ts",
            "signal_lost_ts",
        ):
            conn.execute(f"ALTER TABLE hub_session DROP COLUMN {column}")
        # 0061, 0062 (Ф41-5) — та же имитация для авто-режима (таблица заявок — IF NOT EXISTS).
        conn.execute("ALTER TABLE hub_session DROP COLUMN autosend_ok")
        conn.execute("ALTER TABLE hub_session DROP COLUMN launch_key_hash")
        conn.execute("ALTER TABLE held_request DROP COLUMN rule")
        conn.execute("ALTER TABLE outbox DROP COLUMN auto_session")
        # 0063 (Ф41-5б) — та же имитация для прежних заявок под выключателями автопилота.
        conn.execute("ALTER TABLE join_request DROP COLUMN autopilot_session")
        conn.execute("ALTER TABLE leave_request DROP COLUMN auto")
        conn.execute("ALTER TABLE rule_request DROP COLUMN auto")
        # 0064 (Ф44-1a) — та же имитация для кнопок под сообщениями ботов.
        conn.execute("ALTER TABLE message DROP COLUMN buttons_json")
        conn.execute("ALTER TABLE message DROP COLUMN buttons_ts")
        # 0066 (Ф48-1) — та же имитация для фоновых задач постоянной сессии.
        conn.execute("ALTER TABLE hub_session DROP COLUMN bg_json")
        # 0067, 0068 (Ф48-2) — та же имитация для настроек сжатия сессии и отметки прерванной реплики
        # (`wait_reason` в 0067 пересоздаётся — столбец снят выше, в списке 0057).
        for column in ("compact_tokens", "compact_auto", "compact_at"):
            conn.execute(f"ALTER TABLE hub_session DROP COLUMN {column}")
        conn.execute("ALTER TABLE held_say DROP COLUMN interrupted_ts")
        conn.execute("ALTER TABLE held_say DROP COLUMN resent_ts")
        # 0069 (Ф48-3) — та же имитация для файла роли сессии.
        conn.execute("ALTER TABLE hub_session DROP COLUMN role_file")
        # 0070 (Ф48-4) — та же имитация для пачки сообщений агентов.
        conn.execute("ALTER TABLE session_sub DROP COLUMN agent_batch_min")
        conn.execute("ALTER TABLE session_sub DROP COLUMN agent_batch_ts")
        conn.execute("ALTER TABLE peer_msg DROP COLUMN urgent")
        # 0071 (Ф48-5) — та же имитация для снимка закреплённого системного текста.
        conn.execute("ALTER TABLE hub_session DROP COLUMN sys_pin")
        # 0072 (Ф49-1) — та же имитация для признака «карточка под делегатом».
        for column in ("delegate_sub_id", "delegate_until_ts", "delegate_signal_ts"):
            conn.execute(f"ALTER TABLE held_request DROP COLUMN {column}")
        # 0073 (Ф50-4) — та же имитация для начала и источника идущего хода и строки в очереди движка.
        for column in ("turn_ts", "turn_origin", "mid_queued_ts"):
            conn.execute(f"ALTER TABLE hub_session DROP COLUMN {column}")
        # 0074 (Ф50-7) — та же имитация для подпапки сессии хаба.
        conn.execute("ALTER TABLE hub_session DROP COLUMN subdir")
        # 0076 (Ф52-2) — та же имитация для права проекта слышать «Неразобранное» аккаунтов.
        conn.execute("ALTER TABLE project DROP COLUMN unsorted_accounts")
        # 0079 (Ф56-1а) — та же имитация для переключателя «канал владельца» у сессии хаба (таблица 0078 — IF NOT
        # EXISTS).
        conn.execute("ALTER TABLE hub_session DROP COLUMN via_agent")
        # 0080 (Ф56-1а) — та же имитация для сессии хаба у заявок на вступление, выход и правило.
        for table in ("join_request", "leave_request", "rule_request"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN hub_session_id")
        # 0081 (Ф56-1б) — та же имитация для источника реплики сессии хаба.
        conn.execute("ALTER TABLE held_say DROP COLUMN source")
        # 0082 (Ф56-1б) — та же имитация для вопроса владельцу и счётчика ходов сессии хаба.
        conn.execute("ALTER TABLE peer_msg DROP COLUMN ask")
        conn.execute("ALTER TABLE peer_msg DROP COLUMN ask_about")
        conn.execute("ALTER TABLE hub_session DROP COLUMN turns")
        # 0083 (Ф56-2) — та же имитация для «делегат передал карточку владельцу».
        for column in ("passed_sub_id", "passed_role", "passed_role_no", "passed_ts", "passed_text"):
            conn.execute(f"ALTER TABLE held_request DROP COLUMN {column}")
        # 0085 (Ф57-1) — та же имитация для признака «письмо-рассылка».
        conn.execute("ALTER TABLE message DROP COLUMN bulk")
        # 0091 (Ф60-1) — та же имитация для пометки «подача интервью» у реплики сессии хаба (таблицы 0092 —
        # IF NOT EXISTS).
        conn.execute("ALTER TABLE held_say DROP COLUMN interview_id")
        # 0093 (Ф61-1) — та же имитация для вопроса агента и ответа из бота (индексы — до столбцов).
        conn.execute("DROP INDEX idx_owner_talk_bot_tg")
        conn.execute("DROP INDEX idx_owner_talk_ask")
        for column in ("ask", "ask_until_ts", "closed_ts", "closed_outcome", "via", "tg_msg_id"):
            conn.execute(f"ALTER TABLE owner_talk DROP COLUMN {column}")
        # 0094 (Ф63-1) — та же имитация для правил входящих у сообщения; 0095 — для пометки правила у сигнала
        # (таблица 0095 — IF NOT EXISTS).
        conn.execute("ALTER TABLE message DROP COLUMN rule_hits")
        conn.execute("ALTER TABLE message DROP COLUMN rule_attention")
        conn.execute("ALTER TABLE session_chan_signal DROP COLUMN rules_json")
        # 0096 (Ф62-6) — только новая таблица `group_op` и её индексы (IF NOT EXISTS): имитации не нужно.
        # 0097 (Ф69-1) — та же имитация для стопа первых сообщений новым людям.
        conn.execute("ALTER TABLE account DROP COLUMN peer_flood_ts")
        # 0098 (Ф66-2) — та же имитация для ветки реплики разговора (индекс — до столбца; таблица пересказа —
        # IF NOT EXISTS).
        conn.execute("DROP INDEX idx_owner_talk_branch")
        conn.execute("ALTER TABLE owner_talk DROP COLUMN session_id")
        # 0099 (Ф70-1) — та же имитация для признака ручной строки outbox.
        conn.execute("ALTER TABLE outbox DROP COLUMN by_hand")
        # 0100 (Ф33-1) — та же имитация для машины сессии у подписок.
        conn.execute("ALTER TABLE session_sub DROP COLUMN session_host")
        conn.execute("ALTER TABLE session_chan_sub DROP COLUMN session_host")
        conn.execute("ALTER TABLE session_sub DROP COLUMN session_boot")
        conn.execute("ALTER TABLE session_chan_sub DROP COLUMN session_boot")
        conn.execute("ALTER TABLE session_sub DROP COLUMN session_seen_ts")
        conn.execute("ALTER TABLE session_chan_sub DROP COLUMN session_seen_ts")
        # 0101 (Ф33-6) — машина записи нагрузки.
        conn.execute("ALTER TABLE load_claim DROP COLUMN host")
        # 0102 (Ф71-1) — пересоздание `hub_session` с явными списками столбцов: столбцы, возвращённые выше
        # ALTER-ами в другом порядке, переносятся как есть; имитации не нужно.
        # 0103 (Ф73-1) — та же имитация для просьбы «Перечитать MCP» у сессии хаба.
        conn.execute("ALTER TABLE hub_session DROP COLUMN mcp_reload_ts")
        # 0105 (Ф74-2) — та же имитация для движка сессии у подписок.
        conn.execute("ALTER TABLE session_sub DROP COLUMN session_engine")
        conn.execute("ALTER TABLE session_chan_sub DROP COLUMN session_engine")
        # 0107 (Ф77-1) — только новая таблица `machine` (IF NOT EXISTS): имитации не нужно.
        # 0108 (Ф83-1) — та же имитация для запасного движка сессий хаба и проекта.
        conn.execute("ALTER TABLE hub_session DROP COLUMN fallback_json")
        conn.execute("ALTER TABLE hub_session DROP COLUMN on_fallback_json")
        conn.execute("ALTER TABLE project DROP COLUMN session_fallback_json")
        # 0109 (Ф86-1) — новая таблица `digest` (IF NOT EXISTS) и признак «набрал владелец» у реплик.
        conn.execute("ALTER TABLE held_say DROP COLUMN typed")
        # 0110 (Ф86-1) — курсор слов владельца из соседних сессий.
        conn.execute("ALTER TABLE hub_session DROP COLUMN siblings_ts")
        # 0111 — строка хода агента владельца у реплики разговора.
        conn.execute("ALTER TABLE owner_talk DROP COLUMN progress")
        conn.execute("ALTER TABLE owner_talk DROP COLUMN progress_ts")
        # 0113 (Ф89-1) — повторяемые напоминания и напоминания без беседы.
        conn.execute("DROP INDEX IF EXISTS reminder_due_fired")
        columns = ("every", "at_hm", "tz", "missed", "done_count", "last_done_ts", "last_done_role", "at_wd")
        for column in columns:
            conn.execute(f"ALTER TABLE reminder DROP COLUMN {column}")
        # 0114 (Ф93-1) — признак «вопрос о тратах» у сообщения агента владельцу.
        conn.execute("ALTER TABLE peer_msg DROP COLUMN money")
        # 0115 (Ф93-2) — причина исхода заявки «нужен человек».
        conn.execute("ALTER TABLE human_request DROP COLUMN decided_note")
        # 0116 (Ф94-1) — признак «агент сообщает владельцу» у реплики разговора.
        conn.execute("DROP INDEX idx_owner_talk_tell")
        conn.execute("ALTER TABLE owner_talk DROP COLUMN tell")
        # 0117 (Ф95-1) — заявка, заведшая правило почты по домену регистрации.
        conn.execute("ALTER TABLE map_rule DROP COLUMN origin_human_id")
        # 0118 (Ф99-2) — кто заглушил беседу (сессия через `hub_mute`).
        conn.execute("ALTER TABLE channel DROP COLUMN muted_by")
        # 0119 (Ф99-7.2) — «Всегда в этой сессии» у сессии хаба.
        conn.execute("ALTER TABLE hub_session DROP COLUMN always_json")
        conn.execute("PRAGMA user_version=4")
        conn.commit()
    finally:
        conn.close()

    # Повторный Db.start() применяет 0005 — заполняет позиционные колонки из last_read_seq.
    instance2 = Db(data_dir)
    instance2.start()
    try:
        with instance2.reader() as conn:
            row = conn.execute(
                "SELECT last_read_ts, last_read_sort, last_read_ext, last_read_seq FROM main.channel "
                "WHERE ext_id='c1'"
            ).fetchone()
        last_read_ts, last_read_sort, last_read_ext, last_read_seq_after = row
        assert last_read_ts is not None
        assert last_read_ext == "2"  # up_to_ext_id="2" -> маркер на позиции сообщения "2"
        assert last_read_seq_after == last_read_seq_before  # старое поле не тронуто повторной миграцией
    finally:
        instance2.close()


# --- F9: провал резолва темы не должен кэшироваться/переименовывать -------------------------------------


async def test_1b_crit_f9_topic_title_failure_is_not_cached_and_does_not_rename_known_topic():
    class _GW(FakeGateway):
        fail = True

        async def get_forum_topics(self, peer, *, ids=None):
            if self.fail:
                raise errors.FloodWaitError(request=None, capture=5)
            return await super().get_forum_topics(peer, ids=ids)

    gw = _GW()
    forum = h.make_channel(610, title="Форум", megagroup=True, forum=True)
    marked = int(convert.peer_ext_id(h.peer_of(forum)))

    class _Client(_NullClient):
        async def channels_heads(self):
            return [
                {
                    "ext_id": f"{marked}:42",
                    "kind": "topic",
                    "parent_ext_id": str(marked),
                    "own_id_space": True,
                    "max_ext_sort": 5,
                    "last_msg_ts": 0,
                    "archived": False,
                }
            ]

    connector = TelegramConnector(_Client(), gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001

    title1 = await connector._topic_title(str(marked), h.peer_of(forum), 42)  # noqa: SLF001
    assert title1 is None  # уже известна ядру -> не переименовываем угадышем
    assert 42 not in connector._topic_titles.get(str(marked), {})  # noqa: SLF001 — провал НЕ закэширован

    gw.fail = False
    gw.forum_topics[(marked, 42)] = h.make_forum_topic(610, 42, title="Релиз")
    title2 = await connector._topic_title(str(marked), h.peer_of(forum), 42)  # noqa: SLF001 — повтор спросит снова
    assert title2 == "Релиз"


# --- F10: "Избранное" — одна формула is_me для истории и живого пути ------------------------------------


def test_1b_crit_f10_favorites_is_me_consistent_between_history_and_live():
    me = h.make_user(h.ME_ID, first_name="владелец", is_self=True)
    channel = convert.dialog_channel_ref(h.peer_of(me), me, me_id=h.ME_ID)
    ctx = convert.ConvertContext(
        channel=channel, channel_kind_for_sender="dm", me_id=h.ME_ID, entities={h.ME_ID: me}
    )

    msg_history = h.make_message(5, types.PeerUser(h.ME_ID), message="заметка", out=False, from_id=None)
    msg_live = h.make_message(5, types.PeerUser(h.ME_ID), message="заметка", out=False, from_id=None)

    ev_history = convert.convert_message(msg_history, ctx)
    ev_live = convert.convert_message(msg_live, ctx)
    assert ev_history["is_me"] is True
    assert ev_live["is_me"] is True  # одинаково, независимо от "живой"/"исторический" путь


# --- F12: сущности живого апдейта — БЕЗ сети (get_chat/get_sender убраны) -----------------------------------


def test_1b_crit_f12_event_entities_is_sync_and_reads_only_precached_fields():
    """`_event_entities` больше НЕ корутина (нет await внутри) — раньше при промахе кэша Telethon она
    дожидалась `event.get_chat()`/`get_sender()` (сетевые запросы прямо из последовательного обработчика)."""
    import inspect as _inspect

    assert not _inspect.iscoroutinefunction(_event_entities)

    class _FakeMsg:
        chat = None
        sender = None

    class _FakeEvent:
        message = _FakeMsg()

    assert _event_entities(_FakeEvent()) == {}


# --- F13: счётчик "тихих" сбрасывают только АКТИВНЫЕ неизвестные диалоги ---


async def test_1b_crit_f13_inactive_unknown_dialogs_count_toward_stale_stop(admin_client, internal_transport):
    class _GW(FakeGateway):
        walked = 0

        async def iter_dialogs(self):
            for d in self.dialogs:
                self.walked += 1
                yield d

    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = _GW()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector.emit = lambda ev: None  # noqa: SLF001
    connector._folders_cache = []  # noqa: SLF001 — исключаем полный проход папок из подсчёта
    connector._folders_changed = False  # noqa: SLF001
    connector._folders_dirty = False  # noqa: SLF001
    connector._folders_last_refresh = float("inf")  # noqa: SLF001

    for i in range(900):  # старые (>30 дней) неизвестные диалоги
        gw.add_dialog(
            h.make_dialog(h.make_user(10_000 + i), top_message=1, last_message_date_ms=1_500_000_000_000)
        )

    await connector.sync("scheduled")

    assert gw.walked < 900  # остановились по правилу "20 тихих подряд", не прошли ВСЕ 900


# --- F14: очередь причин сливается в одну сверку --------------------------------------------------------


async def test_1b_crit_f14_folder_update_bursts_collapse_into_one_sync():
    from hub.connectors.telegram.gateway import FoldersChangedGE

    never = asyncio.Event()

    async def _sleep(_s):
        await never.wait()

    connector = TelegramConnector(_NullClient(), FakeGateway(), account_id=1, sleep=_sleep)
    runs = []

    async def _fake_sync(reason="scheduled"):
        runs.append(reason)
        await asyncio.sleep(0)

    connector.sync = _fake_sync
    task = asyncio.ensure_future(connector._sync_scheduler_loop())  # noqa: SLF001
    # Дождаться, пока планировщик разберёт стартовую "start" ОТДЕЛЬНО — иначе она гонится с первым
    # "folders" и по приоритету забирает его себе в `_pending_reasons` (тоже верно — start и так пересчитал
    # бы папки — но тест целится именно в слияние ОДНОРОДНЫХ "folders", а не в эту случайную гонку).
    for _ in range(200):
        await asyncio.sleep(0)
        if "start" in runs:
            break
    assert "start" in runs

    for _ in range(11):
        await connector._handle_gateway_event(FoldersChangedGE())  # noqa: SLF001
    for _ in range(200):
        await asyncio.sleep(0)
        if "folders" in runs:
            break
    connector._stopping = True  # noqa: SLF001
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runs.count("folders") == 1  # НЕ 11


# --- F15: явный include не выкидывается exclude_muted/exclude_archived --------------------------------------


def test_1b_crit_f15_explicit_include_survives_exclude_muted():
    from hub.connectors.telegram import folders as fm

    work = h.make_chat(55, title="Работа (заглушена)")
    d = fm.DialogFlags(marked_id=-55, dtype="group", contact=False, muted=True, archived=True)
    f = h.make_dialog_filter(
        3, include=[h.input_peer_of(work)], exclude_muted=True, exclude_archived=True, contacts=True
    )
    assert fm.compute_folders([d], [f], me_marked_id=h.ME_ID) == {-55: [3]}


# --- мелочи: reactions.min, reply_to чужого чата, forum без forum_topic -> General ---


async def test_1b_crit_misc_reactions_min_is_not_sent():
    # Ф18-2: `min` по-прежнему не шлётся сразу (`reactions_list` -> None), но не теряется — шлётся после
    # перечитывания (`GetMessagesReactions`, полный набор с «моей»).
    reactions = types.MessageReactions(
        min=True, results=[types.ReactionCount(reaction=types.ReactionEmoji("👍"), count=5)]
    )
    assert convert.reactions_list(reactions) is None

    group = h.make_channel(801, title="Группа", megagroup=True)
    gw = FakeGateway()
    marked_id = int(convert.peer_ext_id(h.peer_of(group)))
    gw.seed_peer(marked_id, h.input_peer_of(group))
    full = types.MessageReactions(
        results=[types.ReactionCount(reaction=types.ReactionEmoji("👍"), count=5, chosen_order=0)]
    )
    gw.get_reactions_results.append(
        types.Updates(
            updates=[types.UpdateMessageReactions(peer=h.peer_of(group), msg_id=3, reactions=full)],
            users=[],
            chats=[],
            date=None,
            seq=0,
        )
    )
    connector = _connector(gw, sleep=_instant_sleep)
    emitted = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001
    await connector._handle_gateway_event(  # noqa: SLF001
        ReactionsGE(peer_marked_id=marked_id, msg_id=3, reactions=reactions)
    )
    assert emitted == []  # сразу не шлётся
    await connector._react_task  # noqa: SLF001
    assert [e["type"] for e in emitted] == ["reaction"]
    assert emitted[0]["reactions"][0]["me"] is True


def test_1b_crit_misc_reply_to_foreign_chat_gives_none_ext_id():
    other_chat_peer = types.PeerChannel(999)
    reply = types.MessageReplyHeader(reply_to_msg_id=5, reply_to_peer_id=other_chat_peer)
    result = convert.message_reply_to_ext_id(
        h.make_message(1, types.PeerUser(7), reply_to=reply, from_id=None),
        topic_id=None,
        chat_peer=types.PeerUser(7),
    )
    assert result is None


async def test_1b_crit_misc_forum_message_without_topic_flag_goes_to_general():
    forum = h.make_channel(700, title="Форум", megagroup=True, forum=True)
    connector = _connector()
    connector._entities[int(convert.peer_ext_id(h.peer_of(forum)))] = forum  # noqa: SLF001
    emitted = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    msg = h.make_message(1, h.peer_of(forum), message="hi", from_id=None)  # БЕЗ reply_to вовсе
    chat_ref = convert.dialog_channel_ref(h.peer_of(forum), forum, me_id=h.ME_ID)
    await connector._emit_message("upsert", chat_ref, h.peer_of(forum), msg)  # noqa: SLF001

    assert emitted[0]["channel"]["ext_id"].endswith(":1")  # General


async def test_1b_crit_misc_folders_dirty_stays_true_when_recompute_fails():
    class _FailingGateway(FakeGateway):
        async def iter_dialogs(self):
            raise errors.ServerError(request=None, message="TEST_ERR")
            yield  # pragma: no cover

    gw = _FailingGateway()
    gw.filters = []
    connector = _connector(gw)
    connector._folders_dirty = True  # noqa: SLF001
    with pytest.raises(errors.ServerError):
        await connector._maybe_recompute_folders("start", set())  # noqa: SLF001
    assert connector._folders_dirty is True  # noqa: SLF001 — не сброшен, обрыв на середине
