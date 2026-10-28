"""Ф20-1 C4, П1, П11, A3 (docs/architecture.md): `hub_subscribe(mode=…)`,
`every_message` как синоним, конфликт, смена режима на месте, режим по-русски в `hub_subscriptions`, «(бот)» у
отправителя в выдаче сообщений. Ядро — подменный транспорт (как test_mcp_chan_subs.py)."""

from hub.mcp_server import format as fmt

from .conftest import text_of
from .test_mcp_chan_subs import ME, SUB, Core, _call, _server


def _ok(sub: dict, created=True) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": created, "backfilled": 0}


async def test_mode_values_map_to_core_body(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "mode": "messages", "msg_filter": "personal"})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="personal"))
    assert core.seen[0][2] == {
        "project_id": None,
        "session": ME.session_in(),
        "mode": "messages",
        "msg_filter": "personal",
    }
    # П11: свой текст «лично мне», без «карточек уже ждёт»
    assert "режим «лично мне»" in text and "боты и объявления на всех" in text
    assert "Карточек уже ждёт" not in text
    assert "sub wait 17 --profile all" in text

    await _call(server, "hub_subscribe", mode="messages")
    assert core.seen[1][2]["mode"] == "messages" and core.seen[1][2]["msg_filter"] == "all"
    await _call(server, "hub_subscribe", mode="cards")
    assert core.seen[2][2]["mode"] == "cards" and core.seen[2][2]["msg_filter"] == "all"
    # синоним прежнего вызова — без фильтра (ядро ставит `all`)
    await _call(server, "hub_subscribe", every_message=True)
    assert core.seen[3][2] == {"project_id": None, "session": ME.session_in(), "mode": "messages"}
    await _call(server, "hub_subscribe")
    assert "mode" not in core.seen[4][2] and "msg_filter" not in core.seen[4][2]


async def test_mode_conflicts_refused_without_request(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", mode="personal", every_message=True)
    assert result.is_error and "mode ИЛИ every_message" in text_of(result)
    result = await _call(server, "hub_subscribe", mode="junk")
    assert result.is_error and "personal" in text_of(result)
    result = await _call(server, "hub_subscribe", channel_id=152, mode="personal")
    assert result.is_error and "только для подписки на проект" in text_of(result)
    assert core.seen == []


async def test_messages_mode_text_and_change_in_place(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "mode": "messages", "msg_filter": "all"}, created=False)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="messages"))
    assert "уже была у этой сессии" in text and "каждое новое чужое сообщение" in text
    assert 'hub_subscribe(mode="personal")' in text


async def test_subscriptions_names_mode_in_russian(config_dir, make_server):
    rows = [
        {**SUB, "id": 17, "mode": "cards", "msg_filter": "all"},
        {**SUB, "id": 18, "project_id": 4, "mode": "messages", "msg_filter": "personal"},
        {**SUB, "id": 19, "project_id": 5, "mode": "messages", "msg_filter": "all"},
        {**SUB, "id": 20, "project_id": 6, "mode": "messages"},  # ядро старее Ф20-1
    ]
    core = Core({("GET", "/subs"): (200, {"items": rows, "channels": []})})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscriptions")).splitlines()

    def line(sub_id):
        return next(x for x in lines if f"подписка #{sub_id} " in x)

    assert line(17).endswith(" · карточки")
    assert line(18).endswith(" · лично мне")
    assert line(19).endswith(" · каждое сообщение")
    assert line(20).endswith(" · каждое сообщение")


def test_bot_sender_marked_in_message_lines():
    base = {"id": 1, "ext_id": "1.0", "ts": 1_700_000_000_000, "text": "деплой готов", "is_me": False}
    bot = fmt.message_lines({**base, "sender": {"name": "deploy-bot"}, "is_bot": True})
    assert "deploy-bot (бот): деплой готов" in bot[0]
    human = fmt.message_lines({**base, "sender": {"name": "Пётр"}})
    assert "Пётр: деплой готов" in human[0] and "(бот)" not in human[0]
