"""Приёмка куска 1b архитектором: 8 правок, найденных чтением кода. Один тест на каждую (падает БЕЗ правки),
имена `test_1b_acc_NN_...`. Решения по открытым вопросам исполнителя: переиспользование существующего
telegram-аккаунта в `hub auth tg` — принято (уже было реализовано); форумы в окне перечитывания и `read` по
темам — долг фазы 1, не трогаем (см. комментарии в `connector.py`).
"""

import asyncio
import logging

import pytest
from telethon.tl import types

from hub.connectors.base import ConnectorBase
from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector, _missing_in_range
from hub.connectors.telegram.gateway import NewMessageGE

from . import helpers as h
from .conftest import sync_admin_client
from .helpers import FakeGateway
from .test_window import _FAKE_NOW_S, _wired_connector

# --- находка 1: ложные удаления в _reread_window --------------------------------------------------------


def test_1b_acc_01_missing_in_range_ignores_ids_older_than_fetched_window():
    """Источник вернул РОВНО 30 (лимит) — самых свежих: 6..35. У ядра в окне ещё лежат более старые 1..5
    (сообщения ушли в core позже, чем сверка успела их применить — «живые/догнанные, ещё не отправленные»).
    БЕЗ правки: 1..5 не входят в выборку источника -> считались бы удалёнными. С правкой: они СТАРШЕ
    `min(выборка источника)=6` -> вне диапазона -> не удаляются."""
    fetched_ids = list(range(6, 36))  # 30 штук, 6..35
    fetched_ext_ids = {str(i) for i in fetched_ids}
    core_ids = [str(i) for i in range(1, 31)]  # ядро ещё помнит 1..30

    missing = _missing_in_range(core_ids, fetched_ext_ids, fetched_ids, limit=30)

    assert missing == []


def test_1b_acc_01_missing_in_range_detects_real_deletion_inside_range():
    """Источник снова вернул РОВНО 30 (значит, диапазон ограничен снизу и сверху), но ВНУТРИ диапазона не
    хватает одного id — настоящее удаление, а не просто более старое сообщение вне диапазона."""
    fetched_ids = [i for i in range(20, 51) if i != 40]  # 20..50 без 40 -> 30 штук
    fetched_ext_ids = {str(i) for i in fetched_ids}
    core_ids = [str(i) for i in range(21, 51)]  # ядро видело окно ДО удаления — включая 40

    missing = _missing_in_range(core_ids, fetched_ext_ids, fetched_ids, limit=30)

    assert missing == ["40"]


def test_1b_acc_01_missing_in_range_short_history_checks_from_zero():
    """Источник вернул МЕНЬШЕ лимита (короче окна — отдал всё, что есть) -> нижняя граница 0, а не
    `min(выборка)`: всё, чего нет в выборке, действительно удалено."""
    fetched_ids = [5, 6, 7]
    fetched_ext_ids = {"5", "6", "7"}
    core_ids = ["1", "5", "6", "7"]  # "1" старше самого старого в выборке, но выборка НЕ обрезана лимитом

    missing = _missing_in_range(core_ids, fetched_ext_ids, fetched_ids, limit=30)

    assert missing == ["1"]


def test_1b_acc_01_non_digit_ext_ids_are_never_considered():
    missing = _missing_in_range(["abc", "5"], {"5"}, [5], limit=30)
    assert missing == []


async def test_1b_acc_01_reread_window_end_to_end_no_false_delete(admin_client, internal_transport, db):
    """Сквозной сценарий: ядро знает канал по сообщениям "1".."30"; источник уже успел уйти вперёд (35 —
    самое свежее), но эти новые события ЕЩЁ НЕ ДОШЛИ до ядра (лежат в `collected`, не отправлены). Окно
    перечитывания НЕ должно посчитать старые 1-5 удалёнными."""
    connector, gw, client, collected = await _wired_connector(admin_client, internal_transport)
    other = h.make_user(7, first_name="Пётр")

    seed_events = [
        {
            "type": "upsert",
            "channel": {"ext_id": "7", "kind": "dm", "title": "Пётр"},
            "ext_id": str(i),
            "ts": 1000 + i,
            "is_me": False,
            "kind": "text",
            "text": f"m{i}",
            "mentions_me": False,
        }
        for i in range(1, 31)
    ]
    r = await client.push_events(seed_events)
    assert r["applied"] == 30

    gw.add_dialog(h.make_dialog(other, top_message=35, last_message_date_ms=1_700_000_000_000))
    for i in range(6, 36):
        gw.add_message(7, h.make_message(i, h.peer_of(other), message=f"m{i}", from_id=None))

    await connector.sync("start")
    # Специально НЕ отправляем collected в core — события 31..35 из catch-up остаются "в полёте", как в
    # описании находки; окно уже должно было отработать корректно ВНУТРИ этого же sync().

    deletes = [e for e in collected if e["type"] == "delete"]
    assert deletes == []


# --- находка 2: живое сообщение из чата, которого нет в _entities -----------------------------------------


async def test_1b_acc_02_live_message_uses_entities_from_event_when_chat_unknown():
    other = h.make_user(77, first_name="Незнакомец")
    gw = FakeGateway()
    client = _NullClient()
    connector = TelegramConnector(client, gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    assert 77 not in connector._entities  # noqa: SLF001 — чат НЕ встречался ни в одной сверке

    msg = h.make_message(1, h.peer_of(other), message="привет", from_id=None)
    event = NewMessageGE(message=msg, entities={77: other})

    await connector._handle_gateway_event(event)  # noqa: SLF001

    assert [e["type"] for e in emitted] == ["upsert"]
    assert emitted[0]["channel"]["ext_id"] == "77"
    assert connector._entities[77] is other  # noqa: SLF001 — сущность из события пополнила кэш


async def test_1b_acc_02_completely_unresolved_chat_requests_sync_immediately_not_dropped():
    gw = FakeGateway()
    client = _NullClient()
    connector = TelegramConnector(client, gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector.emit = lambda ev: (_ for _ in ()).throw(AssertionError("не должно ничего излучать"))  # noqa: SLF001

    ghost_peer = types.PeerUser(999999)
    msg = h.make_message(1, ghost_peer, message="привет", from_id=None)
    event = NewMessageGE(message=msg, entities={})  # гейтвей тоже не смог разрешить чат

    await connector._handle_gateway_event(event)  # noqa: SLF001

    assert connector._sync_queue.get_nowait() == "unknown_chat"  # noqa: SLF001 — внеочередная сверка запрошена


async def test_1b_acc_02_newly_resolved_chat_marks_folders_dirty():
    other = h.make_user(78, first_name="Новый")
    gw = FakeGateway()
    client = _NullClient()
    connector = TelegramConnector(client, gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector.emit = lambda ev: None  # noqa: SLF001
    connector._folders_dirty = False  # noqa: SLF001

    msg = h.make_message(1, h.peer_of(other), message="hi", from_id=None)
    await connector._handle_gateway_event(NewMessageGE(message=msg, entities={78: other}))  # noqa: SLF001

    assert connector._folders_dirty is True  # noqa: SLF001


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


# --- находка 3: пересчёт папок должен обходить ВСЕ диалоги, не только частичный проход сверки --------------


async def test_1b_acc_03_folder_recompute_covers_dialogs_beyond_partial_walk_stop():
    """25 известных диалогов без новостей: основной проход сверки остановился бы на 20-м (правило «20
    подряд»), но пересчёт папок делает СВОЙ полный проход — attrs должны прийти на все 25."""
    gw = FakeGateway()
    users = [h.make_user(2000 + i, first_name=f"U{i}") for i in range(25)]
    for u in users:
        gw.add_dialog(h.make_dialog(u, top_message=1, read_inbox_max_id=0, last_message_date_ms=None))

    class _Client:
        async def get_cursor(self):  # Ф42-1: курсор аккаунта (дыры догона) — пусто
            return None

        async def push_events(self, events, cursor=None):
            return {"ok": True}

        async def channels_heads(self):
            return [
                {
                    "ext_id": str(u.id),
                    "kind": "dm",
                    "parent_ext_id": None,
                    "own_id_space": False,
                    "max_ext_sort": 1,
                    "last_msg_ts": 0,
                    "archived": False,
                }
                for u in users
            ]

        async def channels_window(self, *, ext_id, limit=30):
            return []

        async def set_activity(self, *, backfill):
            return {"ok": True}

        async def set_status(self, status, *, last_error=None):
            return {"ok": True}

    connector = TelegramConnector(_Client(), gw, account_id=1, wall=lambda: _FAKE_NOW_S)
    connector._me_id = h.ME_ID  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    await connector.sync("start")

    channel_events = {e["channel"]["ext_id"] for e in emitted if e["type"] == "channel"}
    assert channel_events == {str(u.id) for u in users}


# --- находка 4: стартовая загрузка — не больше 40 диалогов за одну сверку ---------------------------------


async def test_1b_acc_04_initial_load_capped_at_40_per_sync_and_requeues_rest():
    gw = FakeGateway()
    users = [h.make_user(3000 + i, first_name=f"U{i}") for i in range(100)]
    for u in users:
        gw.add_dialog(h.make_dialog(u, top_message=1, last_message_date_ms=1_700_000_000_000))
        gw.add_message(u.id, h.make_message(1, h.peer_of(u), message="hi", from_id=None))

    client = _NullClient()
    connector = TelegramConnector(client, gw, account_id=1, wall=lambda: _FAKE_NOW_S, sleep=_instant_sleep)
    connector._me_id = h.ME_ID  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    await connector.sync("start")

    upsert_chats = {e["channel"]["ext_id"] for e in emitted if e["type"] == "upsert"}
    assert len(upsert_chats) == 40
    assert connector._sync_queue.get_nowait() == "initial"  # noqa: SLF001 — остаток попросился на СЛЕДУЮЩУЮ сверку


async def _instant_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


# --- находка 5: read/reaction/delete-с-каналом по неизвестному каналу -> no-op, канал не создаётся ---------


@pytest.mark.parametrize(
    ("event_type", "extra"),
    [
        ("read", {"up_to_ext_id": "1"}),
        ("reaction", {"ext_id": "1", "reactions": [{"emoji": "👍", "count": 1}]}),
    ],
)
def test_1b_acc_05_read_and_reaction_on_unknown_channel_is_noop(db, event_type, extra):
    from hub.core.ingest import apply_batch

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    event = {"type": event_type, "channel": {"ext_id": "ghost", "kind": "dm", "title": "Призрак"}, **extra}

    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=1000)
    )
    assert (result.applied, result.noop, result.dead) == (0, 1, 0)

    with db.reader() as conn:
        row = conn.execute("SELECT 1 FROM main.channel WHERE ext_id = 'ghost'").fetchone()
    assert row is None


def test_1b_acc_05_delete_with_unknown_channel_is_noop(db):
    from hub.core.ingest import apply_batch

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    event = {
        "type": "delete",
        "ext_ids": ["1", "2"],
        "channel": {"ext_id": "ghost", "kind": "dm", "title": "Призрак"},
    }

    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=1000)
    )
    assert (result.applied, result.noop, result.dead) == (0, 1, 0)

    with db.reader() as conn:
        row = conn.execute("SELECT 1 FROM main.channel WHERE ext_id = 'ghost'").fetchone()
    assert row is None


# --- находка 6: MessageActionChatMigrateTo в старой группе — обычное служебное событие ----------------------


def test_1b_acc_06_chat_migrate_to_in_old_group_is_emitted_as_event():
    old_chat = h.make_chat(123, title="Старая группа")
    action = types.MessageActionChatMigrateTo(channel_id=555)
    msg = h.make_message(50, h.peer_of(old_chat), action=action)

    channel = convert.dialog_channel_ref(h.peer_of(old_chat), old_chat, me_id=h.ME_ID)
    ctx = convert.ConvertContext(channel=channel, channel_kind_for_sender="group", me_id=h.ME_ID, entities={})

    ev = convert.convert_message(msg, ctx)

    assert ev["kind"] == "event"
    assert ev["text"] == "группа стала супергруппой"
    assert ev["ext_id"] == "50"


async def test_1b_acc_06_connector_emits_upsert_for_old_group_migrate_message():
    old_chat = h.make_chat(123, title="Старая группа")
    gw = FakeGateway()
    client = _NullClient()
    connector = TelegramConnector(client, gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector._entities[int(convert.peer_ext_id(h.peer_of(old_chat)))] = old_chat  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001

    action = types.MessageActionChatMigrateTo(channel_id=555)
    msg = h.make_message(50, h.peer_of(old_chat), action=action)
    chat_ref = convert.dialog_channel_ref(h.peer_of(old_chat), old_chat, me_id=h.ME_ID)

    await connector._emit_message("upsert", chat_ref, h.peer_of(old_chat), msg)  # noqa: SLF001

    assert [e["type"] for e in emitted] == ["upsert"]
    assert emitted[0]["ext_id"] == "50"
    assert emitted[0]["text"] == "группа стала супергруппой"


# --- находка 7: health() не пишет полную трассировку на обычную сетевую ошибку -----------------------------


class _HeartbeatConnector(ConnectorBase):
    def __init__(self, client, *, fail_with: Exception) -> None:
        super().__init__(client, sleep=_instant_sleep)
        self._fail_with = fail_with

    async def sync(self, reason: str = "scheduled") -> None:
        return None

    async def send(self, lease):  # pragma: no cover
        raise NotImplementedError

    async def reconcile(self, lease):  # pragma: no cover
        raise NotImplementedError

    async def health(self) -> None:
        raise self._fail_with


async def test_1b_acc_07_heartbeat_logs_warning_without_traceback_on_network_error(caplog):
    client = _NullClientWithStatus()
    connector = _HeartbeatConnector(client, fail_with=ConnectionError("прокси лежит"))

    with caplog.at_level(logging.WARNING, logger="hub.connectors.base"):
        heartbeat_task = asyncio.ensure_future(connector._heartbeat_loop())  # noqa: SLF001
        for _ in range(20):
            await asyncio.sleep(0)
        connector._stopping = True  # noqa: SLF001
        connector._heartbeat_now.set()  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            heartbeat_task.cancel()
            await heartbeat_task

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "ожидали хотя бы один WARNING про health()"
    assert all(r.exc_info is None for r in warning_records)  # НЕТ трассировки
    assert any("health" in r.getMessage() for r in warning_records)


class _NullClientWithStatus:
    async def set_status(self, status, *, last_error=None):
        return {"ok": True}


# --- находка 8: `hub auth tg` без --title -> title="Telegram" ----------------------------------------------


def test_1b_acc_08_auth_tg_without_title_defaults_to_telegram(core_env):
    data_dir, config_dir = core_env
    from types import SimpleNamespace

    from hub.connectors.telegram.auth import cmd_auth_tg
    from hub.connectors.telegram.gateway import TgGateway

    class _FakeTelethonClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def start(self, *, phone=None, code_callback=None, password=None, **_kw):
            for cb in (phone, code_callback, password):
                if callable(cb):
                    cb()
            return self

        async def get_me(self):
            from types import SimpleNamespace as SNS

            return SNS(id=1, first_name="владелец", last_name=None, username=None)

        async def __call__(self, request):
            from telethon.tl.functions.users import GetUsersRequest as _GUR

            if isinstance(request, _GUR):
                return [await self.get_me()]
            raise AssertionError(f"неожиданный запрос в тесте: {request!r}")

    with sync_admin_client(config_dir, data_dir) as admin_client:
        inputs = iter(["1", "+7", "111"])
        secrets = iter(["hash", "pw"])
        code = cmd_auth_tg(
            SimpleNamespace(account=None, title=None, proxy=None),
            admin_client=admin_client,
            config_dir=config_dir,
            data_dir=data_dir,
            gateway_factory=TgGateway,
            client_factory=_FakeTelethonClient,
            input_fn=lambda prompt="": next(inputs),
            getpass_fn=lambda prompt="": next(secrets),
        )
        assert code == 0

        r = admin_client.get("/admin/status")
        accounts = [a for a in r.json()["accounts"] if a["source"] == "telegram"]

    assert len(accounts) == 1
    assert accounts[0]["title"] == "Telegram"
