"""Ф93-1 (docs/architecture.md.4): `hub_ask_owner(money=True)` — признак вопроса о тратах
уходит в ядро полем `money`; без него поля нет (старые вызовы — как были). Ядро — подменный транспорт."""

from .conftest import text_of
from .test_f35_mcp_peer_msgs import Core
from .test_mcp_chan_subs import ME, _call, _server


async def test_ask_owner_passes_money_flag(config_dir, make_server):
    core = Core({("POST", "/peer-msgs/ask"): (201, {"id": 7, "seq": 7})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_ask_owner", text="Оплатить тариф Pro?", money=True)
    assert not result.is_error, text_of(result)
    assert text_of(result) == "Вопрос #7 у владельца; ответ — в hub_agent_inbox."
    result = await _call(server, "hub_ask_owner", text="Дашь доступ?")
    assert not result.is_error, text_of(result)
    session = {"pid": ME.pid, "proc_start": ME.proc_start, "session_id": ME.session_id}
    assert [s[3] for s in core.seen] == [
        {"text": "Оплатить тариф Pro?", "session": session, "money": True},
        {"text": "Дашь доступ?", "session": session},
    ]


async def test_ask_owner_description_names_money(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    tool = next(t for t in await server.list_tools() if t.name == "hub_ask_owner")
    assert "money=True" in tool.description and "money" in tool.input_schema["properties"]
