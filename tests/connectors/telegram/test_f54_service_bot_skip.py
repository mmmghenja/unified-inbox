"""Ф54-2 (правка по ревью): личку с ботом хаба ядро отбрасывает на приёме — коннектор Telegram по признаку
`skip` в «головах» бесед её не грузит и не догоняет (раньше — запрос истории впустую на каждой сверке)."""

from hub.connectors.telegram.connector import TelegramConnector
from hub.core import service_bot

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway

_FAKE_NOW_S = 1_700_000_000.0
BOT = 9
PERSON = 77


async def _run(admin_client, internal_transport, db, *, register: bool) -> tuple[list, int, int]:
    account_id, secret = await create_tg_account(admin_client)
    if register:
        db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT, now_ms=1))
    client = make_core_client(internal_transport, secret)
    gw = FakeGateway()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = gw.me.id  # noqa: SLF001
    bot = h.make_user(BOT, first_name="Hub bot")
    person = h.make_user(PERSON, first_name="Алиса")
    for user, top in ((bot, 50), (person, 5)):
        gw.add_dialog(h.make_dialog(user, top_message=top, read_inbox_max_id=top))
        for i in range(1, top + 1):
            gw.add_message(user.id, h.make_message(i, h.peer_of(user), message=f"текст {i}", from_id=None))

    history: list = []
    orig_get = gw.get_messages

    async def get_messages(peer, *, min_id, limit):
        history.append(getattr(peer, "user_id", None))
        return await orig_get(peer, min_id=min_id, limit=limit)

    gw.get_messages = get_messages
    per_sync = []
    for reason in ("start", "scheduled", "scheduled"):
        emitted: list[dict] = []
        connector.emit = lambda ev, emitted=emitted: emitted.append(ev)
        before = len(history)
        await connector.sync(reason)
        if emitted:
            await client.push_events(emitted)
        chats = sorted({e["channel"]["ext_id"] for e in emitted if e.get("type") == "upsert"})
        per_sync.append((history[before:], chats))
    with db.reader() as conn:
        bot_chans = conn.execute(
            "SELECT COUNT(*) FROM main.channel WHERE ext_id = ?", (str(BOT),)
        ).fetchone()[0]
        person_chans = conn.execute(
            "SELECT COUNT(*) FROM main.channel WHERE ext_id = ?", (str(PERSON),)
        ).fetchone()[0]
    return per_sync, bot_chans, person_chans


async def test_hub_bot_dm_is_never_loaded(admin_client, internal_transport, db):
    per_sync, bot_chans, person_chans = await _run(admin_client, internal_transport, db, register=True)
    # Ни одного запроса истории лички с ботом, ни одного её события — на первой сверке и на следующих.
    assert all(BOT not in history and str(BOT) not in chats for history, chats in per_sync), per_sync
    assert bot_chans == 0
    # Обычная личка грузится как раньше: стартовая загрузка на первой сверке, беседа в ядре.
    assert PERSON in per_sync[0][0] and str(PERSON) in per_sync[0][1] and person_chans == 1


async def test_without_registered_bot_the_dialog_loads_as_usual(admin_client, internal_transport, db):
    per_sync, bot_chans, _person = await _run(admin_client, internal_transport, db, register=False)
    assert BOT in per_sync[0][0] and str(BOT) in per_sync[0][1] and bot_chans == 1
