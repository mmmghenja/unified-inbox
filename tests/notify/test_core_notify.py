"""Ф33-2: кольцо уведомлений ядра, внутренняя ручка записи и публичный long-poll (скоуп `notify`)."""

import asyncio

import httpx
import pytest

from hub.core import security
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.notify_ring import NotifyRing

ADMIN = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
BASE = "http://127.0.0.1:8787"
API = "/api/v1"
NOW_S = 1_700_000_000.0


def _token(db, *, scopes, kind="background", project_ids=None) -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="t", kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    ).token


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def env(db):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    public = create_public_app(db, clock=lambda: NOW_S, port=8787, notifier=notifier)
    internal = create_internal_app(
        db, clock=lambda: NOW_S, notifier=notifier, admin_key_hash=hash_secret(ADMIN)
    )
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url=BASE) as pub,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal), base_url="http://hub-internal"
        ) as internal_client,
    ):
        yield pub, internal_client, notifier


async def _post(internal, title="t", text="x", urgency="critical", admin=ADMIN):
    return await internal.post(
        "/internal/v1/notify",
        json={"title": title, "text": text, "urgency": urgency},
        headers=_bearer(admin),
    )


# --- кольцо -------------------------------------------------------------------------------------------------


def test_ring_numbers_grow_and_size_is_capped() -> None:
    ring = NotifyRing(size=3, clock=lambda: 5.0)
    for i in range(5):
        ring.add(f"t{i}", "x", "critical")
    assert ring.last == 5
    assert [i["id"] for i in ring.after(0)] == [3, 4, 5]
    assert [i["id"] for i in ring.after(4)] == [5]
    assert ring.after(0)[0]["ts"] == 5000


async def test_ring_wait_times_out_and_wakes() -> None:
    ring = NotifyRing()
    assert await ring.wait(0, 0.05) == []
    task = asyncio.ensure_future(ring.wait(0, 5))
    await asyncio.sleep(0.01)
    ring.add("a", "b", "normal")
    assert [i["title"] for i in await asyncio.wait_for(task, 2)] == ["a"]


# --- ручки --------------------------------------------------------------------------------------------------


async def test_internal_post_needs_admin_secret_and_validates(env) -> None:
    _pub, internal, notifier = env
    assert (await _post(internal, admin="huba_wrong")).status_code in (401, 403)
    assert (await _post(internal, urgency="loud")).status_code == 422
    r = await _post(internal, title="x" * 500, text="y" * 5000)
    assert r.status_code == 200 and r.json()["id"] == 1
    item = notifier.ring.after(0)[0]
    assert len(item["title"]) == 200 and len(item["text"]) == 2000


async def test_wait_baseline_then_items_then_longpoll(env, db) -> None:
    pub, internal, _ = env
    tok = _token(db, scopes=("notify",))
    await _post(internal, title="old")
    base = (await pub.get(f"{API}/notify/wait", headers=_bearer(tok))).json()
    assert base["items"] == [] and base["last"] == 1 and base["reset"] is False
    # ожидание просыпается от записи
    waiter = asyncio.ensure_future(
        pub.get(
            f"{API}/notify/wait",
            params={"after": 1, "epoch": base["epoch"], "timeout_s": 5},
            headers=_bearer(tok),
        )
    )
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await _post(internal, title="new", urgency="normal")
    body = (await asyncio.wait_for(waiter, 3)).json()
    assert [(i["id"], i["title"], i["urgency"]) for i in body["items"]] == [(2, "new", "normal")]
    assert body["last"] == 2
    # пусто -> таймаут без записей
    r = await pub.get(f"{API}/notify/wait", params={"after": 2, "timeout_s": 0}, headers=_bearer(tok))
    assert r.json()["items"] == []


async def test_wait_reset_after_core_restart(env, db) -> None:
    pub, internal, _ = env
    tok = _token(db, scopes=("notify",))
    await _post(internal, title="a")
    # номер клиента больше текущего (ядро перезапустилось) -> всё кольцо с начала
    body = (await pub.get(f"{API}/notify/wait", params={"after": 40}, headers=_bearer(tok))).json()
    assert body["reset"] is True and [i["title"] for i in body["items"]] == ["a"] and body["last"] == 1
    # другая метка запуска -> то же, даже если номер «подходит»
    body = (
        await pub.get(f"{API}/notify/wait", params={"after": 1, "epoch": "deadbeef"}, headers=_bearer(tok))
    ).json()
    assert body["reset"] is True and len(body["items"]) == 1


# --- права --------------------------------------------------------------------------------------------------


async def test_notify_scope_only_for_wait_and_wait_only_for_notify(env, db) -> None:
    pub, _internal, _ = env
    tok = _token(db, scopes=("notify",))
    read = _token(db, scopes=("read",), kind="interactive")
    assert (await pub.get(f"{API}/notify/wait", headers=_bearer(read))).status_code == 403
    assert (await pub.get(f"{API}/notify/wait")).status_code == 401
    for path in ("projects", "channels/1", "sync", "load"):
        r = await pub.get(f"{API}/{path}", headers=_bearer(tok))
        assert r.status_code == 403, path


def test_notify_token_shape_is_enforced(db) -> None:
    for kwargs in (
        {"kind": "interactive", "scopes": ["notify"], "project_ids": None},
        {"kind": "background", "scopes": ["notify"], "project_ids": [1]},
        {"kind": "background", "scopes": ["notify", "read"], "project_ids": None},
    ):
        with pytest.raises(ValueError, match="notify"):
            db.write_sync(
                lambda conn, kw=kwargs: security.create_api_token(conn, name="t", expires_ts=None, **kw)
            )


async def test_post_reports_whether_anybody_listens(env, db) -> None:
    pub, internal, notifier = env
    now = {"t": 1000.0}
    notifier.ring._clock = lambda: now["t"]
    tok = _token(db, scopes=("notify",))
    assert (await _post(internal)).json()["listened"] is False  # спутник не включён
    await pub.get(f"{API}/notify/wait", headers=_bearer(tok))  # базовый запрос
    assert (await _post(internal)).json()["listened"] is True
    now["t"] += 179
    assert (await _post(internal)).json()["listened"] is True
    now["t"] += 2  # прошло больше 3 минут без запросов
    assert (await _post(internal)).json()["listened"] is False


async def test_open_longpoll_counts_as_listening(env, db) -> None:
    pub, internal, notifier = env
    notifier.ring._clock = lambda: 1000.0
    tok = _token(db, scopes=("notify",))
    waiter = asyncio.ensure_future(
        pub.get(f"{API}/notify/wait", params={"after": 0, "timeout_s": 5}, headers=_bearer(tok))
    )
    await asyncio.sleep(0.05)
    notifier.ring._clock = lambda: 1000.0 + 3600  # давно, но ожидание открыто
    assert notifier.ring.listened() is True
    await _post(internal)
    await asyncio.wait_for(waiter, 3)
