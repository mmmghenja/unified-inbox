"""Общее для тестов терминала (Ф56-5): настоящий tmux на своём сокете в коротком временном каталоге, без
`systemd-run` (`use_systemd=False`); боевой сервер `hub-term` и его сокет не трогаются."""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from api.conftest import BASE_URL, FakeClock, login_via_code
from httpx import ASGITransport, AsyncClient

from hub.core import security
from hub.core.app_public import create_public_app
from hub.term.config import TermConfig
from hub.term.hub import TermHub

API = "/api/v1"

if shutil.which("tmux") is None:  # pragma: no cover
    pytest.skip("нет tmux", allow_module_level=True)


def make_cfg(root: Path, **kw) -> TermConfig:
    params = {
        "socket": root / "s",
        "conf": root / "c.conf",
        "use_systemd": False,
        "shell": ("bash", "--norc", "--noprofile", "-i"),
        "cwd": root,
        "extra_env": (("HISTFILE", "/dev/null"), ("PS1", "$ "), ("PROMPT_COMMAND", "")),
    }
    params.update(kw)
    return TermConfig(**params)


@pytest.fixture
def term_root():
    # Короткий путь: сокет unix — меньше 108 байт (tmp_path pytest бывает длиннее).
    root = Path(tempfile.mkdtemp(prefix="ht", dir="/tmp" if os.path.isdir("/tmp") else None))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def term_hub(term_root):
    hub = TermHub(make_cfg(term_root))
    yield hub
    await hub.aclose()
    await hub.server.kill_server()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(db, clock, term_hub):
    return create_public_app(db, clock=clock, port=8787, term=term_hub)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def ui(client, db, clock):
    """Клиент с кукой владельца и CSRF в заголовках."""
    _cookie, csrf = await login_via_code(client, db, clock)
    client.headers.update({"Origin": BASE_URL, "X-Hub-CSRF": csrf})
    return client


def bearer(db, scopes, *, kind="background", project_ids=None, name="t") -> tuple[str, int]:
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def wait_for(predicate, wait_s: float = 5.0, step: float = 0.05):
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(step)


def ring_text(bridge) -> str:
    return bridge.ring.read(bridge.ring.start, bridge.ring.size).decode("utf-8", errors="replace")


async def wait_out(bridge, needle: str, wait_s: float = 5.0) -> str:
    return await wait_for(lambda: needle in ring_text(bridge) and ring_text(bridge), wait_s)
