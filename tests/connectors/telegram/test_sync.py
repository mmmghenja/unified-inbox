"""Тесты 8, 9, 11 куска 1b: сверка `TelegramConnector.sync()` с `FakeGateway` и настоящим ядром в процессе
(`internal_app` через `ASGITransport` — тот же приём, что `tests/e2e/`)."""

from telethon.errors import FloodWaitError

from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway


async def _make_connector(admin_client, internal_transport, *, gateway: FakeGateway | None = None):
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = gateway if gateway is not None else FakeGateway()
    connector = TelegramConnector(client, gw, account_id=account_id)
    connector._me_id = gw.me.id  # noqa: SLF001 — обычно ставит start_source(), тут его не гоняем целиком
    return connector, gw, client


# --- Тест 8: пропущенные сообщения, закреплённый старый диалог, стартовая загрузка, догон ----------------


async def test_1b_08_known_chat_catches_up_missed_messages_in_order(admin_client, internal_transport):
    other = h.make_user(7, first_name="Пётр")
    connector, gw, client = await _make_connector(admin_client, internal_transport)

    # Ядро уже знает канал с сообщением ext_id=1 (последний известный).
    known_event = {
        "type": "upsert",
        "channel": {"ext_id": "7", "kind": "dm", "title": "Пётр"},
        "ext_id": "1",
        "ts": 1000,
        "is_me": False,
        "kind": "text",
        "text": "первое",
        "mentions_me": False,
    }
    r = await client.push_events([known_event])
    assert r["applied"] == 1

    gw.add_dialog(h.make_dialog(other, top_message=4, last_message_date_ms=1_700_000_000_000))
    for i in (2, 3, 4):
        gw.add_message(7, h.make_message(i, h.peer_of(other), message=f"m{i}", from_id=None))

    events_before = []
    connector.emit = lambda ev: events_before.append(ev)  # перехватываем излучение вместо реального буфера

    await connector.sync("start")

    upserts = [e for e in events_before if e["type"] == "upsert" and e["channel"]["ext_id"] == "7"]
    assert [e["ext_id"] for e in upserts] == ["2", "3", "4"]  # по возрастанию id


async def test_1b_08_pinned_old_dialog_does_not_stop_the_pass(admin_client, internal_transport):
    """21 ИЗВЕСТНЫХ ядру диалога без новостей: первый — закреплённый (не считается в «20 подряд»),
    остальные 20 — обычные. Без сброса счётчика закреплённым проход остановился бы на 20-м (пинн +
    19 обычных) и последний диалог не был бы проверен вовсе; со сбросом — проверены все 21."""
    connector, gw, client = await _make_connector(admin_client, internal_transport)

    users = [h.make_user(1000 + i, first_name=f"U{i}") for i in range(21)]
    known_events = [
        {
            "type": "upsert",
            "channel": {"ext_id": str(u.id), "kind": "dm", "title": u.first_name},
            "ext_id": "1",
            "ts": 1000,
            "is_me": False,
            "kind": "text",
            "text": "старое",
            "mentions_me": False,
        }
        for u in users
    ]
    r = await client.push_events(known_events)
    assert r["applied"] == 21

    gw.add_dialog(h.make_dialog(users[0], top_message=1, read_inbox_max_id=1, pinned=True))
    for u in users[1:]:
        gw.add_dialog(h.make_dialog(u, top_message=1, read_inbox_max_id=1))

    reads = []
    connector.emit = lambda ev: reads.append(ev["channel"]["ext_id"]) if ev["type"] == "read" else None
    await connector.sync("start")

    assert sorted(reads, key=int) == sorted((str(u.id) for u in users), key=int)


async def test_1b_08_unknown_active_dialog_gets_initial_load_of_30():
    connector, gw, client = await _make_connector_bare()
    other = h.make_user(9, first_name="Новый")
    gw.add_dialog(h.make_dialog(other, top_message=50, last_message_date_ms=1_700_000_000_000))
    for i in range(1, 51):
        gw.add_message(9, h.make_message(i, h.peer_of(other), message=f"m{i}", from_id=None))

    emitted = []
    connector.emit = lambda ev: emitted.append(ev)

    await connector.sync("start")

    upserts = [e for e in emitted if e.get("type") == "upsert" and e["channel"]["ext_id"] == "9"]
    assert len(upserts) == 30
    assert [int(e["ext_id"]) for e in upserts] == sorted(int(e["ext_id"]) for e in upserts)
    assert client.activity_calls == [{"backfill": True}, {"backfill": False}]


async def test_1b_08_unknown_inactive_dialog_over_30_days_not_created():
    connector, gw, client = await _make_connector_bare()
    del client
    stale = h.make_user(11, first_name="Молчит")
    old_ms = 1_700_000_000_000 - 40 * 24 * 60 * 60 * 1000  # 40 дней назад
    gw.add_dialog(h.make_dialog(stale, top_message=5, last_message_date_ms=old_ms))
    gw.add_message(11, h.make_message(5, h.peer_of(stale), message="старое", from_id=None))

    emitted = []
    connector.emit = lambda ev: emitted.append(ev)
    await connector.sync("start")

    assert [e for e in emitted if e.get("type") == "upsert"] == []


async def test_1b_08_599_missed_are_all_caught_up_in_pages(admin_client, internal_transport, caplog):
    """Ф42-1: раньше 500+ пропущенных давали строку `gap` и потерю; теперь догон идёт страницами по 100."""
    other = h.make_user(7, first_name="Пётр")
    connector, gw, client = await _make_connector(admin_client, internal_transport)
    known_event = {
        "type": "upsert",
        "channel": {"ext_id": "7", "kind": "dm", "title": "Пётр"},
        "ext_id": "1",
        "ts": 1000,
        "is_me": False,
        "kind": "text",
        "text": "первое",
        "mentions_me": False,
    }
    await client.push_events([known_event])
    gw.add_dialog(h.make_dialog(other, top_message=600, last_message_date_ms=1_700_000_000_000))
    for i in range(2, 601):
        gw.add_message(7, h.make_message(i, h.peer_of(other), message="x", from_id=None))

    got: list[int] = []
    connector.emit = lambda ev: got.append(int(ev["ext_id"])) if ev["type"] == "upsert" else None

    async def _no_sleep(_seconds):
        return None

    connector._sleep = _no_sleep  # noqa: SLF001 — паузы между страницами догона
    with caplog.at_level("INFO"):
        await connector.sync("start")
    assert sorted(got) == list(range(2, 601))
    assert any("догон chat=7 страниц=6 сообщений=599" in rec.message for rec in caplog.records)
    assert not any("gap" in rec.message for rec in caplog.records)


# --- Тест 11: backfill on/off и FloodWaitError -------------------------------------------------------------


async def test_1b_11_floodwait_in_sync_pauses_not_crashes():
    connector, gw, client = await _make_connector_bare()
    del gw, client

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    connector._sleep = _fake_sleep  # noqa: SLF001

    async def _boom_iter_dialogs():
        raise FloodWaitError(request=None, capture=30)
        yield  # pragma: no cover — делает функцию генератором, недостижимо

    connector._gateway.iter_dialogs = _boom_iter_dialogs  # noqa: SLF001

    await connector.sync("start")  # не должно бросить исключение наружу
    assert sleeps == [30.0]


_FAKE_NOW_S = 1_700_000_000.0  # совпадает с `last_message_date_ms`/`date_ms` по умолчанию в helpers.py


async def _make_connector_bare():
    """Коннектор без настоящего ядра (не нужен там, где `client` не должен вызываться реально). Часы —
    фиксированные (`_FAKE_NOW_S`), совпадают с умолчаниями `helpers.make_dialog`/`make_message` — иначе
    «активность ≤ 30 дней» считалась бы от РЕАЛЬНОГО текущего времени теста, а не от фикстур 2023 года."""
    gw = FakeGateway()

    class _Client:
        def __init__(self):
            self.activity_calls = []

        async def get_cursor(self):  # Ф42-1: курсор аккаунта (дыры догона) — пусто
            return None

        async def push_events(self, events, cursor=None):
            return {"ok": True}

        async def channels_heads(self):
            return []

        async def channels_window(self, *, ext_id, limit=30):
            return []

        async def set_activity(self, *, backfill):
            self.activity_calls.append({"backfill": backfill})
            return {"ok": True}

        async def set_status(self, status, *, last_error=None):
            return {"ok": True}

    client = _Client()
    connector = TelegramConnector(client, gw, account_id=1, wall=lambda: _FAKE_NOW_S)
    connector._me_id = gw.me.id  # noqa: SLF001
    return connector, gw, client
