"""Обвязка тестов куска 7 (эксплуатация): дамп задач по SIGUSR1, бэкап, `hub watch`. Спецификация:
docs/architecture.md. Помощники наполнения БД (`FakeClock`, `insert_account`) — те же, что в
`tests/api/conftest.py`; импортируем их по файловому пути (тот же приём, что в `tests/internal/conftest.py`
и `tests/e2e/conftest.py` — `tests/` не пакет, трогать `tests/api/conftest.py` запрещено условиями задачи).

`internal_app` здесь — НЕ фикстура из `tests/internal/conftest.py` (та не выставляет `state.config_dir`,
которого требует `/admin/backup`, и трогать тот файл нам тоже запрещено условиями задачи): своя копия,
повторяющая то, что реально делает `runner.py` (`create_internal_app()` + `internal_app.state.config_dir =
config_dir` СНАРУЖИ, без правки `app_internal.py` — см. отчёт исполнителя).
"""

import importlib.util
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

_API_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "api" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_hub_test_api_conftest_ops", _API_CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_api_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_conftest)

FakeClock = _api_conftest.FakeClock
insert_account = _api_conftest.insert_account
insert_channel = _api_conftest.insert_channel
insert_message = _api_conftest.insert_message

INTERNAL_BASE_URL = "http://hub-internal"
ADMIN_SECRET = ADMIN_PREFIX + "ops-test-secret-0123456789abcdefghijklmno"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def config_dir(tmp_path) -> Path:
    d = tmp_path / "config"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def internal_app(db, clock, notifier, config_dir):
    db.on_commit(notifier.notify)
    app = create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))
    app.state.config_dir = config_dir
    return app


@pytest.fixture
def internal_transport(internal_app):
    return ASGITransport(app=internal_app)


@pytest.fixture
async def internal_client(internal_transport):
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=internal_transport, base_url=INTERNAL_BASE_URL, headers=headers) as c:
        yield c


@pytest.fixture
async def raw_client(internal_transport):
    """Без заголовков по умолчанию — для тестов аутентификации (тест 8: без ключа / чужой класс ключа)."""
    async with AsyncClient(transport=internal_transport, base_url=INTERNAL_BASE_URL) as c:
        yield c


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


def connector_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def set_account_status(db, account_id: int, *, status: str, last_ok_ts: int | None = None) -> None:
    """Помощник только для тестов ops — `insert_account` (`tests/api/conftest.py`) не принимает `status`."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET status = ?, last_ok_ts = ? WHERE id = ?",
            (status, last_ok_ts, account_id),
        )
    )
