"""Стенд сквозных тестов приёмки фазы 0 (кусок 5b) — сценарии 10-22, `docs/architecture.md`.

Оба ASGI-приложения (публичное и внутреннее) — в ОДНОМ процессе на общих `Db`/`FakeClock`/`Notifier`, как их
поднимает `hub core`. Помощники наполнения БД и входа (`FakeClock`, `login_via_code`, `csrf_headers`,
`insert_channel`) — те же, что в `tests/api/conftest.py`: импортируем их оттуда по файловому пути (тот же
приём, что в `tests/internal/conftest.py` — `tests/` не пакет, обычный импорт недоступен, а трогать
`tests/api/conftest.py` запрещено условиями задачи).

Аккаунты/проекты/токены/секреты коннекторов заводятся ЧЕРЕЗ admin-ручки внутреннего приложения
(`/internal/v1/admin/*`) — как это делал бы `hub` CLI, а не прямым SQL. Каналы — через `insert_channel`
напрямую: этой сущности нет в списке «через admin-ручки» условий задачи, а заводить их настоящими
ingest-событиями ради одной строки было бы лишним там, где сценарий не про приём.

`db` — фикстура из корневого `tests/conftest.py` (`Db(tmp_path)`, без подмены часов у самой `Db` — часы,
которые видят маршруты, приходят отдельным параметром `clock=` в `create_public_app`/`create_internal_app`,
`Db.clock` в самом слое БД не используется).
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from hub.connectors.base import CoreClient
from hub.connectors.fake import FakeConnector, FakeSource
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db
from hub.core.feed import Notifier

_API_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "api" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_hub_test_api_conftest_e2e", _API_CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_api_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_conftest)

FakeClock = _api_conftest.FakeClock
insert_channel = _api_conftest.insert_channel
login_via_code = _api_conftest.login_via_code
csrf_headers = _api_conftest.csrf_headers

PUBLIC_PORT = 8787
PUBLIC_BASE_URL = f"http://127.0.0.1:{PUBLIC_PORT}"
INTERNAL_BASE_URL = "http://hub-internal"
ADMIN_SECRET = ADMIN_PREFIX + "e2e-admin-secret-0123456789abcdefghijklmno"
DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def public_app(db, clock, notifier):
    return create_public_app(db, clock=clock, port=PUBLIC_PORT, notifier=notifier)


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
def internal_transport(internal_app):
    return ASGITransport(app=internal_app)


@pytest.fixture
async def public_client(public_app):
    transport = ASGITransport(app=public_app)
    async with AsyncClient(transport=transport, base_url=PUBLIC_BASE_URL) as c:
        yield c


@pytest.fixture
async def admin_client(internal_transport):
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=internal_transport, base_url=INTERNAL_BASE_URL, headers=headers) as c:
        yield c


# --- наполнение через admin-ручки внутреннего приложения (как это делал бы CLI) --------------------------


async def create_account(admin_client, *, source="fake", title=None, db_name="main", config=None):
    r = await admin_client.post(
        "/internal/v1/admin/accounts",
        json={"source": source, "title": title, "db": db_name, "config": config},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["account_id"], body["connector_secret"]


async def create_project(admin_client, *, name="P"):
    r = await admin_client.post("/internal/v1/admin/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


async def create_token(
    admin_client, *, name="tok", kind="interactive", scopes=("read", "send"), project_ids=None
):
    r = await admin_client.post(
        "/internal/v1/admin/tokens",
        json={"name": name, "kind": kind, "scopes": list(scopes), "project_ids": project_ids},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def setup_channel(db, clock, account_id, *, project_id=None, ext_id="c1", trusted=True):
    """Канал, готовый принимать `enqueue()`. `trusted=True` — доверенный (`my_msg_count>0`, старше 14 суток;
    для origin='ui' доверие роли не играет, большинство сценариев шлют под UI); `trusted=False` —
    недоверенный (`my_msg_count=0`), нужен для сценария 21 (агентский путь через bearer)."""
    now_ms = int(clock() * 1000)
    if trusted:
        my_msg_count, first_seen_ts = 5, now_ms - 20 * DAY_MS
    else:
        my_msg_count, first_seen_ts = 0, now_ms
    return insert_channel(
        db,
        account_id,
        project_id=project_id,
        ext_id=ext_id,
        my_msg_count=my_msg_count,
        first_seen_ts=first_seen_ts,
    )


def make_connector(internal_transport, *, secret: str, source: FakeSource, plan=None) -> FakeConnector:
    """`FakeConnector` поверх `CoreClient(transport=internal_transport)` — единственная разрешённая правка
    `base.py` (необязательный `transport=` в конструкторе `CoreClient`, см. отчёт исполнителя)."""
    client = CoreClient(secret=secret, transport=internal_transport)
    return FakeConnector(client, source, plan=plan)


async def restart_core(db, clock):
    """«Рестарт core» — `Db.close()` + новый `Db` на тех же файлах + новые приложения. `Notifier`
    пересоздаётся: чисто in-memory pub/sub, ничего не персистит и не обязан переживать рестарт процесса.
    Вызывающий отвечает за `new_db.close()` в конце теста (root-фикстура `db` знает только про старый)."""
    data_dir = db.data_dir
    db.close()
    new_db = Db(data_dir)
    new_db.start()
    new_notifier = Notifier()
    new_db.on_commit(new_notifier.notify)
    new_internal_app = create_internal_app(
        new_db, clock=clock, notifier=new_notifier, admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    new_public_app = create_public_app(new_db, clock=clock, port=PUBLIC_PORT, notifier=new_notifier)
    return new_db, new_internal_app, new_public_app


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


async def send_ui(
    public_client, cookie, *, channel_id, text, idempotency_key=None, thread=None, reply_to=None
) -> dict:
    body = {
        "channel_id": channel_id,
        "text": text,
        "idempotency_key": idempotency_key or new_idempotency_key(),
    }
    if thread is not None:
        body["thread"] = thread
    if reply_to is not None:
        body["reply_to"] = reply_to
    r = await public_client.post("/api/v1/messages/send", json=body, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    return r.json()


async def send_bearer(
    public_client, token, *, channel_id, text, idempotency_key=None, thread=None, reply_to=None
) -> dict:
    body = {
        "channel_id": channel_id,
        "text": text,
        "idempotency_key": idempotency_key or new_idempotency_key(),
    }
    if thread is not None:
        body["thread"] = thread
    if reply_to is not None:
        body["reply_to"] = reply_to
    r = await public_client.post(
        "/api/v1/messages/send", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def get_outbox(public_client, outbox_id, *, headers=None) -> dict:
    r = await public_client.get(f"/api/v1/outbox/{outbox_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def visible_rows(public_client, channel_id, *, headers=None) -> list[dict]:
    r = await public_client.get(f"/api/v1/channels/{channel_id}/messages", headers=headers)
    assert r.status_code == 200, r.text
    return [m for m in r.json()["messages"] if not m["deleted"]]


async def assert_outcome(
    public_client,
    source: FakeSource,
    *,
    outbox_id: int,
    channel_id: int,
    text: str,
    deliveries: int,
    visible: int,
    status: str,
    headers: dict | None = None,
) -> None:
    """Три вещи, которые проверяет КАЖДЫЙ сценарий 10-22 (спека, раздел «5b»): число доставок в
    `FakeSource` по этому тексту, число видимых строк в ленте с этим текстом через публичный API,
    итоговый статус outbox."""
    assert source.delivered_count(text) == deliveries, source.deliveries

    rows = [m for m in await visible_rows(public_client, channel_id, headers=headers) if m["text"] == text]
    assert len(rows) == visible, rows

    body = await get_outbox(public_client, outbox_id, headers=headers)
    assert body["status"] == status, body
