"""Ф48-3 (docs/architecture.md): MCP `hub_role` у сессии хаба с файлом роли — общий текст
хаба и ПУТЬ файла роли (содержимое сессия получила системным текстом); без файла — только общий текст."""

import pytest

from hub import roles
from hub.core import hub_sessions, subs

from .conftest import insert_project, make_token
from .test_f41_mcp_auto_sub import _hub, _session

PATH = "docs/roles/ROLE.md"
AUDIT = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r"}
LINE = (
    f"Файл роли (в папке проекта): {PATH} — его содержимое хаб подставил в твой системный текст при запуске."
)


@pytest.fixture
def stand(db, tmp_path, clock):
    workdir = tmp_path / "proj"
    (workdir / "docs" / "roles").mkdir(parents=True)
    (workdir / PATH).write_text("СОДЕРЖИМОЕ-ФАЙЛА-РОЛИ", encoding="utf-8")
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, project, True, now_ms=0))
    token, token_id = make_token(db, scopes=["read", "draft"], name="mcp:all")
    return {"project": project, "workdir": str(workdir), "token": token, "token_id": token_id}


async def test_hub_role_names_role_file_path_only(
    db, stand, make_server, config_dir, public_app, monkeypatch
):
    hub_id = _session(db, stand, role="программист")
    hub = _hub(make_server, config_dir, stand, hub_id, monkeypatch)
    assert await hub.auto_subscribe() == "subscribed"
    # Файла роли нет — только общий текст (встроенных текстов ролей нет).
    assert await hub.role_rules() == roles.common_text()

    db.write_sync(lambda conn: hub_sessions.set_role_file(conn, hub_id, PATH, now_ms=2, audit=AUDIT))
    text = await hub.role_rules()
    assert text == f"{roles.common_text()}\n\n{LINE}"
    assert "СОДЕРЖИМОЕ-ФАЙЛА-РОЛИ" not in text


async def test_file_line_only_for_hub_session(db, stand, make_server, config_dir, public_app, monkeypatch):
    """Строка о файле — только сессии хаба (`HUB_SESSION`): без неё сервер запись сессии не читает."""
    hub_id = _session(db, stand, role="программист")
    db.write_sync(lambda conn: hub_sessions.set_role_file(conn, hub_id, PATH, now_ms=2, audit=AUDIT))
    hub = _hub(make_server, config_dir, stand, hub_id, monkeypatch)
    assert await hub._role_file_line() == LINE
    hub.environ = {}
    assert await hub._role_file_line() is None
