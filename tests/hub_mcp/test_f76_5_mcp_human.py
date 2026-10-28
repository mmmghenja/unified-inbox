"""Ф76-5: описание `hub_human` велит звать владельца в браузер заявкой с окном, а не текстом в конце хода (по окну
агент владельца проходит ход сессии сам)."""

from mcp import Client

from .test_f54_mcp_human import Core, _server, workdir  # noqa: F401 — фикстура `workdir`


async def test_hub_human_description_asks_for_window(config_dir, make_server, workdir):  # noqa: F811
    server, _hub = _server(make_server, config_dir, Core(str(workdir)))
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    text = tools["hub_human"].description
    assert "зови hub_human с окном (profile, tab, url), а не проси текстом в конце хода" in text
    assert "«Сохранить» или «Изменить»" in text
