"""Общее для тестов диктовки (Ф59-1): каталог конфига во временном каталоге, подделка SpeechKit на
`httpx.MockTransport` (настоящей сети нет), приложение ядра с поставленными `config_dir` и транспортом."""

import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import security
from hub.core.app_public import create_public_app
from hub.speech import config as speech_config

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE_URL
KEY = "AQVN-fake-key-0123456789"
FOLDER = "b1g-fake-folder"


class FakeClock:
    def __init__(self, start_s: float = 1_700_000_000.0) -> None:
        self.value = start_s

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, ms: int) -> None:
        self.value += ms / 1000


class FakeSpeechKit:
    """Подделка SpeechKit: помнит запросы, отвечает по `reply` (функция или кортеж `(код, тело)`)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.status = 200
        self.body: object = {"result": "привет мир"}
        self.raise_exc: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        if self.raise_exc is not None:
            raise self.raise_exc
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _private_config_dir(tmp_path, monkeypatch):
    """Ни один тест не трогает боевой каталог конфига: `paths.config_dir()` смотрит во временный."""
    cfg = tmp_path / "config"
    monkeypatch.setenv("HUB_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("HUB_SPEECHKIT_URL", raising=False)
    return cfg


@pytest.fixture
def config_dir(_private_config_dir):
    return _private_config_dir


def configure(config_dir, *, key=KEY, folder=FOLDER, **extra) -> None:
    speech_config.save(config_dir, api_key=key, folder_id=folder)
    if extra:
        path = speech_config.config_path(config_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(extra)
        path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def fake_sk() -> FakeSpeechKit:
    return FakeSpeechKit()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(db, clock, config_dir, fake_sk):
    application = create_public_app(db, clock=clock, port=PORT)
    application.state.config_dir = config_dir
    application.state.speech_transport = fake_sk.transport
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


async def login(client, db, clock) -> str:
    """Вход кодом; возвращает csrf для не-GET запросов."""
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def make_bearer(db, scopes=("read",), kind="interactive") -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="b", kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token
