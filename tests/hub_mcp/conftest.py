"""Обвязка тестов hub-mcp (спека 7a, тесты 8–15). Ядро — в процессе на временных каталогах (как в
`tests/api/`), HTTP от MCP-сервера к ядру — через подменяемый транспорт (`httpx.ASGITransport`);
клиент — сессия MCP SDK в памяти против `MCPServer`. Помощники наполнения БД — из `tests/api/conftest.py`
по файловому пути (тот же приём, что в `tests/e2e/conftest.py`). Каталог называется `hub_mcp`, а не `mcp`:
пакет `tests/mcp` заслонил бы SDK `mcp` в `sys.path`.
"""

import importlib.util
from pathlib import Path

import pytest
from httpx import ASGITransport

from hub.core import security
from hub.core.app_public import create_public_app
from hub.mcp_server import profile as profile_mod
from hub.mcp_server import server as mcp_server

_API_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "api" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_hub_test_api_conftest_hub_mcp", _API_CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_api_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_conftest)

FakeClock = _api_conftest.FakeClock
insert_account = _api_conftest.insert_account
insert_channel = _api_conftest.insert_channel
insert_message = _api_conftest.insert_message
insert_project = _api_conftest.insert_project
insert_thread = _api_conftest.insert_thread
login_via_code = _api_conftest.login_via_code
csrf_headers = _api_conftest.csrf_headers

PORT = 8787
CORE_URL = f"http://127.0.0.1:{PORT}/api/v1"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def public_app(db, clock):
    return create_public_app(db, clock=clock, port=PORT)


@pytest.fixture
def config_dir(tmp_path) -> Path:
    path = tmp_path / "config"
    path.mkdir(mode=0o700)
    return path


def make_token(db, *, scopes, project_ids=None, name="mcp:test", kind="interactive"):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def write_profile(config_dir: Path, profile: str, token: str, token_id: int = 0) -> None:
    profile_mod.write_profile(
        config_dir, profile, token=token, meta={"token_id": token_id, "projects": None, "scopes": []}
    )


@pytest.fixture
async def make_server(public_app, config_dir):
    """`make_server(profile, transport=None)` -> (MCPServer, Hub); клиенты ядра закрываются в конце теста."""
    hubs = []

    def _make(profile: str = "test", transport=None):
        server, hub = mcp_server.create(
            profile=profile,
            config_dir=config_dir,
            base_url=CORE_URL,
            transport=transport if transport is not None else ASGITransport(app=public_app),
        )
        hubs.append(hub)
        return server, hub

    yield _make
    for hub in hubs:
        await hub.client.aclose()


def text_of(result) -> str:
    return "\n".join(part.text for part in result.content if getattr(part, "type", None) == "text")
