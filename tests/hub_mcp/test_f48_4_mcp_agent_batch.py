"""Ф48-4 (docs/architecture.md, 5): MCP — `hub_subscribe(agent_batch_min=…)`,
действующий интервал в ответе `hub_subscribe` и в `hub_subscriptions`, `hub_agent_send(urgent=True)` и ответ
отправителю «получит пачкой не позже …». Ядро — подменный транспорт."""

from mcp import Client

from hub.mcp_server import server as mcp_server

from .conftest import text_of
from .test_f35_mcp_peer_msgs import TS, Core, _mine
from .test_mcp_chan_subs import ME, SUB, _call, _server

TO = {"project_id": 2, "role": "маркетолог", "role_no": 1, "sub_id": None}


async def test_subscribe_passes_interval_and_names_effective_one(config_dir, make_server):
    sub = {**SUB, "agent_batch_min": 10}
    core = Core({("POST", "/subs"): (200, {"sub": sub, "created": True, "backfilled": 0})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", agent_batch_min=10)
    text = text_of(result)
    assert not result.is_error, text
    assert core.seen[0][3] == {"project_id": None, "session": ME.session_in(), "agent_batch_min": 10}
    assert (
        "Сообщения агентов — пачкой раз в 10 мин (сообщения владельца и срочные — сразу и забирают накопленное); "
        "менять — hub_subscribe(agent_batch_min=…), 0 — сразу."
    ) in text

    # без параметра поле не шлётся; интервал из умолчания проекта всё равно назван
    core.seen.clear()
    text = text_of(await _call(server, "hub_subscribe"))
    assert core.seen[0][3] == {"project_id": None, "session": ME.session_in()}
    assert "пачкой раз в 10 мин" in text

    # 0 и ядро без поля — строки про пачку нет (ответ прежний)
    core.routes[("POST", "/subs")] = (200, {"sub": SUB, "created": True, "backfilled": 0})
    core.seen.clear()
    text = text_of(await _call(server, "hub_subscribe", agent_batch_min=0))
    assert core.seen[0][3]["agent_batch_min"] == 0
    assert "пачкой" not in text


async def test_subscribe_interval_refusals_before_core(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    for bad in (61, -1):
        result = await _call(server, "hub_subscribe", agent_batch_min=bad)
        assert result.is_error and "agent_batch_min: минуты, целое 0–60" in text_of(result)
    result = await _call(server, "hub_subscribe", channel_id=152, agent_batch_min=5)
    assert result.is_error and "только для подписки на проект" in text_of(result)
    assert core.seen == []


async def test_subscriptions_show_effective_interval(config_dir, make_server):
    batched = {**SUB, "agent_batch_min": 15}
    plain = {**SUB, "id": 18, "project_id": 1, "session_pid": 555, "session_name": None}
    core = Core({("GET", "/subs"): (200, {"items": [batched, plain]})})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscriptions")).splitlines()
    mine = next(line for line in lines if "подписка #17" in line)
    theirs = next(line for line in lines if "подписка #18" in line)
    assert "сообщения агентов — пачкой раз в 15 мин" in mine
    assert "пачкой" not in theirs


async def test_send_urgent_flag_and_batch_deadline_in_reply(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)

    async def send(msg, recipient, **args):
        core.routes[("POST", "/peer-msgs")] = (201, {"msg": msg, "recipient": recipient})
        result = await _call(server, "hub_agent_send", text="привет", project_id=2, role="маркетолог", **args)
        assert not result.is_error, text_of(result)
        return text_of(result)

    # адресат копит пачкой — отправителю назван срок
    deadline = TS + 15 * 60_000
    text = await send(_mine(5, to=TO), {"live": True, "waiting": True, "batch_ts": deadline})
    assert text == (
        f"Сообщение #5 агенту «маркетолог» (проект 2): получит пачкой не позже "
        f"{mcp_server.fmt.fmt_ts(deadline)} (ответа раньше не жди; нужно, чтобы начал прямо сейчас, — "
        "urgent=True)."
    )
    assert "urgent" not in core.seen[-1][3]  # по умолчанию пометки срочности в запросе нет

    # срочное: пометка уходит ядру, срока в ответе ядра нет — «сигналом сейчас»
    text = await send(_mine(6, to=TO, urgent=True), {"live": True, "waiting": True}, urgent=True)
    assert core.seen[-1][3]["urgent"] is True
    assert text == "Сообщение #6 агенту «маркетолог» (проект 2): получит сигналом сейчас."

    # без пачки ответы прежние
    text = await send(_mine(7, to=TO), {"live": True, "waiting": False})
    assert text.endswith("в сети — получит, когда перезапустит ожидание.")


async def test_tool_descriptions_mention_batch_and_urgent(config_dir, make_server):
    server, _hub = _server(make_server, config_dir, Core({}))
    async with Client(server) as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}
    assert "agent_batch_min" in tools["hub_subscribe"]
    assert "срочно — только когда адресат должен начать прямо сейчас" in tools["hub_agent_send"]
