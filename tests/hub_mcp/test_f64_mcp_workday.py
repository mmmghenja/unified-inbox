"""Ф64-1 (docs/architecture.md): ответ `hub_agent_send`, когда адресат — сессия хаба
проекта с закрытым рабочим днём. Ядро — подменный транспорт."""

from .conftest import text_of
from .test_f35_mcp_peer_msgs import Core, _mine
from .test_f48_4_mcp_agent_batch import TO
from .test_mcp_chan_subs import _call, _server


async def test_send_to_closed_day_says_it_waits_for_morning(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    core.routes[("POST", "/peer-msgs")] = (
        201,
        {"msg": _mine(5, to=TO), "recipient": {"live": True, "waiting": False, "day_closed": True}},
    )
    result = await _call(server, "hub_agent_send", text="привет", project_id=2, role="маркетолог")
    assert not result.is_error, text_of(result)
    assert text_of(result) == (
        "Сообщение #5 агенту «маркетолог» (проект 2): доставлено; день проекта закрыт, прочтёт после "
        "«Начать день»."
    )
