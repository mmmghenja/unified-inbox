"""Ф67-1 (docs/architecture.md), MCP: `hub_subscribe(batch_min=…)` без ключей доходит до
ядра (у проекта и у беседы); ответ подписки и `hub_subscriptions` называют действующий интервал из поля строки
`batch_min` — «сигналы — пачкой раз в N мин»; без пачки фразы нет. Ядро — подменный транспорт."""

from .conftest import text_of
from .test_mcp_chan_subs import CHAN, ME, SUB, Core, _call, _server

MESSAGES = {**SUB, "mode": "messages", "msg_filter": "all", "keywords": None}


def _ok(sub: dict, created=True) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": created, "backfilled": 0}


async def test_project_messages_batch_sent_and_named(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**MESSAGES, "batch_min": 10})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="messages", batch_min=10))
    assert core.seen[0][2] == {
        "project_id": None,
        "session": ME.session_in(),
        "mode": "messages",
        "msg_filter": "all",
        "batch_min": 10,
    }
    assert "Сигналы — пачкой раз в 10 мин (личное — сразу и забирает накопленное)" in text
    # без пачки — фразы нет
    core.routes[("POST", "/subs")] = _ok({**MESSAGES, "batch_min": 0}, created=False)
    text = text_of(await _call(server, "hub_subscribe", batch_min=0))
    assert core.seen[1][2] == {"project_id": None, "session": ME.session_in(), "batch_min": 0}
    assert "пачкой" not in text


async def test_cards_batch_named(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "mode": "cards", "keywords": None, "batch_min": 5})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="cards", batch_min=5))
    assert "Сигналы — пачкой раз в 5 мин" in text


async def test_channel_batch_without_keys(config_dir, make_server):
    sub = {**CHAN, "keywords": None, "batch_min": 7}
    core = Core({("POST", "/chan-subs"): (200, {"sub": sub, "created": True, "count": 1})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", channel_id=152, batch_min=7))
    assert core.seen[0][2] == {"channel_id": 152, "session": ME.session_in(), "batch_min": 7}
    assert "Сигнал — на каждое новое входящее этой беседы. Сигналы — пачкой раз в 7 мин" in text


async def test_subscriptions_show_batch_without_keys(config_dir, make_server):
    items = [
        {**MESSAGES, "batch_min": 10},
        {**SUB, "id": 18, "mode": "cards", "keywords": None, "batch_min": 3},
        {**MESSAGES, "id": 19, "batch_min": 0},
    ]
    chans = [
        {**CHAN, "keywords": None, "batch_min": 20},
        {**CHAN, "id": 5, "channel_id": 153, "keywords": None},
    ]
    core = Core({("GET", "/subs"): (200, {"items": items, "channels": chans})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscriptions"))
    assert "каждое сообщение, сигналы — пачкой раз в 10 мин" in text
    assert "карточки, сигналы — пачкой раз в 3 мин" in text
    line19 = next(x for x in text.splitlines() if "подписка #19" in x)
    assert "пачкой" not in line19
    chan_line = next(x for x in text.splitlines() if "беседа #152 " in x)
    assert chan_line.endswith(" · сигналы — пачкой раз в 20 мин")
    chan_line = next(x for x in text.splitlines() if "беседа #153 " in x)
    assert "пачкой" not in chan_line
