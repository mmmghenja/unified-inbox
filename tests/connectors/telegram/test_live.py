"""Тест 10 куска 1b: живые апдейты через `FakeGateway`. `_handle_gateway_event` вызывается напрямую (минуя
очередь `_live_updates_loop`) — то же самое событие, тот же код, без бесконечного цикла в тесте."""

from telethon.tl import types

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import (
    DeletedMessagesGE,
    EditedMessageGE,
    FoldersChangedGE,
    FoldersDirtyGE,
    NewMessageGE,
    ReactionsGE,
    ReadInboxGE,
)

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway


class _Client:
    def __init__(self):
        self.statuses = []

    async def set_status(self, status, *, last_error=None):
        self.statuses.append(status)
        return {"ok": True}


async def _connector_with_entity(entity):
    gw = FakeGateway()
    connector = TelegramConnector(_Client(), gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector._entities[int(convert.peer_ext_id(h.peer_of(entity)))] = entity  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)  # noqa: SLF001
    return connector, gw, emitted


async def test_1b_10_new_message_and_edit_produce_upsert_and_edit_events():
    other = h.make_user(7, first_name="Пётр")
    connector, _gw, emitted = await _connector_with_entity(other)

    msg = h.make_message(1, h.peer_of(other), message="привет", from_id=None)
    await connector._handle_gateway_event(NewMessageGE(message=msg))  # noqa: SLF001
    assert [e["type"] for e in emitted] == ["upsert"]
    assert emitted[0]["ext_id"] == "1"

    edited = h.make_message(1, h.peer_of(other), message="правка", from_id=None)
    await connector._handle_gateway_event(EditedMessageGE(message=edited))  # noqa: SLF001
    assert emitted[1]["type"] == "edit"
    assert emitted[1]["text"] == "правка"


async def test_1b_10_deleted_with_channel_and_without():
    channel = h.make_channel(50, title="Канал", broadcast=True)
    connector, _gw, emitted = await _connector_with_entity(channel)

    marked = int(convert.peer_ext_id(h.peer_of(channel)))
    await connector._handle_gateway_event(  # noqa: SLF001
        DeletedMessagesGE(channel_marked_id=marked, message_ids=[1, 2])
    )
    assert emitted[-1] == {
        "type": "delete",
        "ext_ids": ["1", "2"],
        "channel": {"ext_id": str(marked), "kind": "channel"},
    }

    await connector._handle_gateway_event(DeletedMessagesGE(channel_marked_id=None, message_ids=[3]))  # noqa: SLF001
    assert emitted[-1] == {"type": "delete", "ext_ids": ["3"], "channel": None}


async def test_1b_10_read_inbox_and_reaction():
    other = h.make_user(7, first_name="Пётр")
    connector, _gw, emitted = await _connector_with_entity(other)

    await connector._handle_gateway_event(ReadInboxGE(peer_marked_id=7, max_id=10))  # noqa: SLF001
    assert emitted[-1] == {"type": "read", "channel": {"ext_id": "7", "kind": "dm"}, "up_to_ext_id": "10"}

    reactions = types.MessageReactions(
        results=[types.ReactionCount(reaction=types.ReactionEmoji(emoticon="👍"), count=1, chosen_order=0)]
    )
    await connector._handle_gateway_event(ReactionsGE(peer_marked_id=7, msg_id=1, reactions=reactions))  # noqa: SLF001
    assert emitted[-1]["type"] == "reaction"
    assert emitted[-1]["reactions"] == [{"emoji": "👍", "count": 1, "me": True}]


async def test_1b_10_folders_changed_requests_immediate_sync_dirty_marks_next_sync():
    other = h.make_user(7, first_name="Пётр")
    connector, _gw, _emitted = await _connector_with_entity(other)
    connector._folders_dirty = False  # noqa: SLF001
    connector._folders_changed = False  # noqa: SLF001

    await connector._handle_gateway_event(FoldersChangedGE())  # noqa: SLF001
    assert connector._folders_changed is True  # noqa: SLF001
    assert connector._folders_dirty is True  # noqa: SLF001
    assert connector._sync_queue.get_nowait() == "folders"  # noqa: SLF001

    connector._folders_dirty = False  # noqa: SLF001
    await connector._handle_gateway_event(FoldersDirtyGE())  # noqa: SLF001
    assert connector._folders_dirty is True  # noqa: SLF001
    assert connector._sync_queue.empty()  # noqa: SLF001 — грязно -> пересчёт в БЛИЖАЙШЕЙ сверке, не сейчас


async def test_1b_10_folder_update_refetches_filters_and_relays_project(admin_client, internal_transport, db):
    """Апдейт папки -> перезапрос ВСЕХ фильтров, `account_meta`, `channel` с новыми `attrs`, проект
    меняется."""
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)

    project_a = (await admin_client.post("/internal/v1/admin/projects", json={"name": "A"})).json()[
        "project_id"
    ]
    project_b = (await admin_client.post("/internal/v1/admin/projects", json={"name": "B"})).json()[
        "project_id"
    ]
    await admin_client.post(
        "/internal/v1/admin/map-rules",
        json={"account_id": account_id, "project_id": project_a, "match": {"attr": "tg_folders", "has": 3}},
    )
    await admin_client.post(
        "/internal/v1/admin/map-rules",
        json={"account_id": account_id, "project_id": project_b, "match": {"attr": "tg_folders", "has": 7}},
    )

    gw = FakeGateway()
    other = h.make_user(7, first_name="Пётр")
    gw.add_dialog(h.make_dialog(other, top_message=1, last_message_date_ms=1_700_000_000_000))
    gw.add_message(7, h.make_message(1, h.peer_of(other), message="hi", from_id=None))
    gw.filters = [h.make_dialog_filter(3, include=[h.input_peer_of(other)])]

    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: 1_700_000_000.5)
    connector._me_id = h.ME_ID  # noqa: SLF001
    collected = []
    connector.emit = lambda ev: collected.append(ev)  # noqa: SLF001

    await connector.sync("start")
    await client.push_events(collected)
    collected.clear()

    with_channel_query = "SELECT project_id FROM main.channel WHERE ext_id = '7'"
    with db.reader() as conn:
        project_before = conn.execute(with_channel_query).fetchone()[0]
    assert project_before == project_a

    # Папка сменилась: теперь диалог в фильтре id=7, а не 3.
    gw.filters = [h.make_dialog_filter(7, include=[h.input_peer_of(other)])]
    await connector._handle_gateway_event(FoldersChangedGE())  # noqa: SLF001
    assert connector._sync_queue.get_nowait() == "folders"  # noqa: SLF001

    await connector.sync("folders")
    await client.push_events(collected)

    with db.reader() as conn:
        project_after = conn.execute(with_channel_query).fetchone()[0]
        meta_row = conn.execute(
            "SELECT value_json FROM main.account_meta WHERE account_id = ? AND key = 'tg_folders'",
            (account_id,),
        ).fetchone()
    assert project_after == project_b
    assert meta_row is not None
    assert '"id": 7' in meta_row[0] or '"id":7' in meta_row[0]
