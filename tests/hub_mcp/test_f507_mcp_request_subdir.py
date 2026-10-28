"""Ф50-7 (docs/architecture.md, Р9): заявка на напарника несёт папку и файл роли —
`hub_request_session(subdir=…, role_file=…)`; ответ и список своих заявок называют их и замечания ядра о
диске внутри блока данных. Ядро — подменный транспорт."""

from .conftest import text_of
from .test_f41_mcp_autopilot import HELD, KEY, _bodies
from .test_f41_mcp_held import ME, SUB, Core, _call, _server

PLACE = {"role": "проект", "task": "веди заказ", "subdir": "proekty/zakaz-17", "role_file": "roli/proekt.md"}


def _core(created, listed=None):
    return Core(
        {
            ("GET", "/subs"): (200, {"items": [SUB], "channels": []}),
            ("POST", "/agent-requests"): (201, created),
            ("GET", "/agent-requests"): (200, {"requests": listed or []}),
        }
    )


async def test_request_sends_folder_and_role_file_and_reply_names_them(config_dir, make_server):
    created = {
        "id": 7, "status": "pending", "auto": False, "payload": dict(PLACE),
        "subdir_warning": "папки proekty/zakaz-17 нет в проекте",
        "role_file_warning": "файл роли roli/proekt.md: файла нет",
    }  # fmt: skip
    core = _core(created)
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(
        await _call(
            server,
            "hub_request_session",
            role="проект",
            task="веди заказ",
            subdir=" proekty/zakaz-17 ",
            role_file="roli/proekt.md",
        )
    )
    [body] = _bodies(core, "/agent-requests")
    assert body["subdir"] == "proekty/zakaz-17" and body["role_file"] == "roli/proekt.md"
    assert body["session"] == ME and body["session_key"] == KEY
    head, _, data = text.partition("----- ДАННЫЕ ")
    assert head.startswith("Заявка №7 на напарника · ждёт владельца.")
    # пути и замечания пишет сессия и диск — внутри блока данных
    assert "папка (от папки проекта)" not in head and "сейчас:" not in head
    assert "    папка (от папки проекта): proekty/zakaz-17" in data
    assert "    файл роли (от папки проекта): roli/proekt.md" in data
    assert "    сейчас: папки proekty/zakaz-17 нет в проекте" in data
    assert "    сейчас: файл роли roli/proekt.md: файла нет" in data


async def test_request_without_folder_is_the_old_body_and_reply(config_dir, make_server):
    core = _core({"id": 3, "status": "pending", "auto": False, "payload": {"role": "тестер", "task": "x"}})
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(await _call(server, "hub_request_session", role="тестер", task="x"))
    [body] = _bodies(core, "/agent-requests")
    assert "subdir" not in body and "role_file" not in body
    assert text.startswith("Заявка №3 на напарника · ждёт владельца.") and "----- ДАННЫЕ " not in text
    # пустая строка — «не задано», в ядро не уходит
    await _call(server, "hub_request_session", role="тестер", task="x", subdir="  ", role_file="")
    assert "subdir" not in _bodies(core, "/agent-requests")[1]
    assert "role_file" not in _bodies(core, "/agent-requests")[1]


async def test_own_requests_list_shows_folder_role_file_and_warnings(config_dir, make_server):
    listed = [
        {"id": 7, "kind": "new_session", "project_id": 3, "status": "pending", "auto": False, "reason": None,
         "note": None, "payload": dict(PLACE), "subdir_warning": "в папке свои настройки движка: a/.mcp.json"},  # noqa: E501
        {"id": 6, "kind": "new_session", "project_id": 3, "status": "approved", "auto": False, "reason": None,
         "note": None, "payload": {"role": "тестер", "task": "x", "subdir": None, "role_file": None}},
    ]  # fmt: skip
    core = _core({}, listed)
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    shown = text_of(await _call(server, "hub_agent_requests"))
    head, _, data = shown.partition("----- ДАННЫЕ ")
    assert "proekty/zakaz-17" not in head
    seventh, _, sixth = data.partition("№6 ")
    assert "    папка (от папки проекта): proekty/zakaz-17" in seventh
    assert "    файл роли (от папки проекта): roli/proekt.md" in seventh
    assert "    сейчас: в папке свои настройки движка: a/.mcp.json" in seventh
    assert "папка (от папки проекта)" not in sixth and "сейчас:" not in sixth


async def test_tool_description_says_paths_are_from_the_project_folder(config_dir, make_server):
    server, _hub = _server(make_server, config_dir, _core({}), environ=HELD)
    [tool] = [t for t in await server.list_tools() if t.name == "hub_request_session"]
    props = tool.input_schema["properties"]
    assert "subdir" in props and "role_file" in props
    assert "ОТНОСИТЕЛЬНО ПАПКИ ПРОЕКТА (не твоей рабочей папки)" in tool.description
