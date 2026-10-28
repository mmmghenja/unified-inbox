"""Ф52-1 (docs/architecture.md): ручки учёта нагрузки — `GET /load`,
`POST /load/claims`, `DELETE /load/claims/{id}` и их двойники админ-сокета. Доступ: кука интерфейса и bearer
любого профиля без скоупа; с удалённого слушателя (телефон) — только смотреть; без входа — 401.
`/proc` процессов подменяется (`load.proc_start`)."""

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import load
from hub.core import remote as remote_mod
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.remote import RemoteState

from .conftest import PORT, csrf_headers, insert_project, login_via_code, make_bearer_token

API = "/api/v1"
ADMIN = "/internal/v1/admin"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
ORIGIN = f"http://127.0.0.1:{PORT}"
REMOTE_PORT = 8790
REMOTE_HOST = "hub.test.example:8787"
REMOTE_ORIGIN = f"http://{REMOTE_HOST}"
PASSWORD = "correct horse battery"
CLAIM = {"who": "hub", "what": "полный прогон", "cores": 8, "mem_mb": 4096, "pid": 100, "minutes": 25}


@pytest.fixture
def procs(monkeypatch):
    table = {100: 5000, 200: 6000}
    monkeypatch.setattr(load, "proc_start", lambda pid: table.get(pid))
    monkeypatch.setattr(load, "machine_limits", lambda: (16, 32 * 1024))  # «машина» теста
    return table


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


def _count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.load_claim").fetchone()[0]


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        remote=app.state.remote,
        sub_waiters=app.state.sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


async def test_anonymous_refused(client, db, procs):
    assert (await client.get(f"{API}/load")).status_code == 401
    assert (await client.post(f"{API}/load/claims", json=CLAIM)).status_code in (401, 403)
    assert (await client.delete(f"{API}/load/claims/1")).status_code in (401, 403)
    assert _count(db) == 0


async def test_bearer_of_any_profile_three_routes(client, db, clock, procs):
    """Токен одного проекта со скоупом только `read` — записать, увидеть, снять."""
    project = insert_project(db, name="P")
    token = make_bearer_token(db, scopes=("read",), project_ids=[project])
    r = await client.post(f"{API}/load/claims", json=CLAIM, headers=_bearer(token))
    assert r.status_code == 200, r.text
    claim = r.json()
    assert claim["who"] == "hub" and claim["minutes"] == 25

    clock.advance_ms(3 * 60_000)
    body = (await client.get(f"{API}/load", headers=_bearer(token))).json()
    assert [c["id"] for c in body["claims"]] == [claim["id"]]
    assert body["claimed"] == {"cores": 8, "mem_mb": 4096}
    assert body["now_ts"] - body["claims"][0]["started_ts"] == 3 * 60_000
    assert set(body["system"]) == {"cores", "load1", "load5", "mem_total_mb", "mem_available_mb"}

    r = await client.delete(f"{API}/load/claims/{claim['id']}", headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": True}
    # нет такой — тоже успех
    r = await client.delete(f"{API}/load/claims/{claim['id']}", headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": False}
    assert (await client.get(f"{API}/load", headers=_bearer(token))).json()["claims"] == []


async def test_ui_session_three_routes(client, db, clock, procs):
    cookie, _csrf = await login_via_code(client, db, clock)
    client.cookies.clear()
    headers = {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}
    r = await client.post(f"{API}/load/claims", json=CLAIM, headers=headers)
    assert r.status_code == 200, r.text
    assert len((await client.get(f"{API}/load", headers=headers)).json()["claims"]) == 1
    r = await client.delete(f"{API}/load/claims/{r.json()['id']}", headers=headers)
    assert r.status_code == 200
    assert _count(db) == 0


async def test_get_purges_dead_and_expired(client, db, clock, procs):
    token = make_bearer_token(db)
    for pid in (100, 200):
        r = await client.post(f"{API}/load/claims", json={**CLAIM, "pid": pid}, headers=_bearer(token))
        assert r.status_code == 200, r.text
    del procs[100]
    body = (await client.get(f"{API}/load", headers=_bearer(token))).json()
    assert [c["pid"] for c in body["claims"]] == [200]
    assert _count(db) == 1  # мёртвая запись удалена и из базы
    # срок: 25 × 1,5 + 5 минут
    clock.advance_ms(int(42.5 * 60_000) + 1)
    assert (await client.get(f"{API}/load", headers=_bearer(token))).json()["claims"] == []
    assert _count(db) == 0


async def test_get_without_stale_does_not_write(client, db, procs):
    """Чтение без отживших записей не идёт в поток-писатель: пустая запись будила бы длинные ожидания."""
    token = make_bearer_token(db)
    await client.post(f"{API}/load/claims", json=CLAIM, headers=_bearer(token))
    commits = []
    db.on_commit(lambda: commits.append(1))
    assert (await client.get(f"{API}/load", headers=_bearer(token))).status_code == 200
    assert commits == []


@pytest.mark.parametrize(
    ("patch", "status"),
    [
        ({"minutes": 0}, 422),
        ({"minutes": 241}, 422),
        ({"cores": 0}, 422),
        ({"cores": 17}, 422),  # ревью: больше, чем ядер у машины
        ({"cores": 4096}, 422),
        ({"mem_mb": 16 * 1024 * 1024}, 422),  # ревью: больше всей памяти машины
        ({"pid": 777}, 422),  # процесса нет
        ({"what": ""}, 422),
        ({"minutes": "25"}, 422),
        ({"extra": 1}, 422),
    ],
)
async def test_post_refusals(client, db, procs, patch, status):
    token = make_bearer_token(db)
    r = await client.post(f"{API}/load/claims", json={**CLAIM, **patch}, headers=_bearer(token))
    assert r.status_code == status, r.text
    assert _count(db) == 0


async def test_post_limit_409(client, db, procs, monkeypatch):
    monkeypatch.setattr(load, "MAX_CLAIMS", 2)
    token = make_bearer_token(db)
    for _ in range(2):
        r = await client.post(f"{API}/load/claims", json=CLAIM, headers=_bearer(token))
        assert r.status_code == 200
    r = await client.post(f"{API}/load/claims", json=CLAIM, headers=_bearer(token))
    assert r.status_code == 409
    assert _count(db) == 2


async def test_bearer_token_holds_at_most_10_claims(client, db, procs, admin_client):
    """Ревью Ф52-1 (`test_bearer_claims_with_foreign_pid_fill_the_table`): токен одного проекта (`read`)
    записывает чужой долгоживущий pid — занять таблицу он не может: 10 живых записей на токен, дальше 409.
    Другой токен и CLI (админ-сокет) от этого не страдают; снял запись — место вернулось."""
    token = make_bearer_token(db, scopes=("read",), project_ids=[insert_project(db, name="P")])
    body = {**CLAIM, "cores": 16, "mem_mb": 32 * 1024, "minutes": 240}
    replies = [await client.post(f"{API}/load/claims", json=body, headers=_bearer(token)) for _ in range(51)]
    codes = [r.status_code for r in replies]
    assert codes == [200] * load.TOKEN_MAX_CLAIMS + [409] * (51 - load.TOKEN_MAX_CLAIMS)
    assert "токена" in replies[-1].json()["detail"]
    assert _count(db) == load.TOKEN_MAX_CLAIMS
    other = make_bearer_token(db, scopes=("read",), project_ids=[insert_project(db, name="Q")])
    assert (await client.post(f"{API}/load/claims", json=CLAIM, headers=_bearer(other))).status_code == 200
    for _ in range(load.TOKEN_MAX_CLAIMS + 1):  # CLI — без предела на токен
        r = await admin_client.post("/internal/v1/admin/load/claims", json=CLAIM, headers=_admin())
        assert r.status_code == 200, r.text
    first = replies[0].json()["id"]
    assert (await client.delete(f"{API}/load/claims/{first}", headers=_bearer(token))).status_code == 200
    assert (await client.post(f"{API}/load/claims", json=body, headers=_bearer(token))).status_code == 200
    assert (await client.post(f"{API}/load/claims", json=body, headers=_bearer(token))).status_code == 409


async def test_bearer_limit_frees_with_dead_process(client, db, procs):
    """Отжившие записи токена в его счёт не идут."""
    token = make_bearer_token(db)
    for _ in range(load.TOKEN_MAX_CLAIMS):
        r = await client.post(f"{API}/load/claims", json=CLAIM, headers=_bearer(token))
        assert r.status_code == 200
    del procs[100]
    r = await client.post(f"{API}/load/claims", json={**CLAIM, "pid": 200}, headers=_bearer(token))
    assert r.status_code == 200 and _count(db) == 1


async def test_ui_session_has_no_token_limit(client, db, clock, procs):
    cookie, _csrf = await login_via_code(client, db, clock)
    client.cookies.clear()
    headers = {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}
    for _ in range(load.TOKEN_MAX_CLAIMS + 1):
        r = await client.post(f"{API}/load/claims", json=CLAIM, headers=headers)
        assert r.status_code == 200, r.text


async def test_delete_huge_claim_id(client, db, procs):
    """Ревью Ф52-1: номер записи больше 64 бит — не 500."""
    r = await client.delete(f"{API}/load/claims/{2**70}", headers=_bearer(make_bearer_token(db)))
    assert r.status_code in (200, 404, 422), r.text


async def test_admin_socket_three_routes(admin_client, db, procs):
    assert (await admin_client.get(f"{ADMIN}/load")).status_code in (401, 403)
    assert (await admin_client.post(f"{ADMIN}/load/claims", json=CLAIM)).status_code in (401, 403)
    assert (await admin_client.delete(f"{ADMIN}/load/claims/1")).status_code in (401, 403)

    r = await admin_client.post(f"{ADMIN}/load/claims", json=CLAIM, headers=_admin())
    assert r.status_code == 200, r.text
    claim_id = r.json()["id"]
    body = (await admin_client.get(f"{ADMIN}/load", headers=_admin())).json()
    assert [c["id"] for c in body["claims"]] == [claim_id]
    r = await admin_client.post(f"{ADMIN}/load/claims", json={**CLAIM, "pid": 777}, headers=_admin())
    assert r.status_code == 422 and "777" in r.json()["detail"]
    r = await admin_client.delete(f"{ADMIN}/load/claims/{claim_id}", headers=_admin())
    assert r.status_code == 200 and r.json()["removed"] is True
    assert _count(db) == 0


async def test_remote_listener_get_only(db, clock, procs, monkeypatch):
    """С телефона — только смотреть: запись и снятие отвечают только локальному слушателю."""
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)
    notifier = Notifier()
    db.on_commit(notifier.notify)
    state = RemoteState(port=REMOTE_PORT)
    with db.reader() as conn:
        state.load(conn)
    rapp = create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=state, notifier=notifier
    )
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=ORIGIN) as local:
        cookie, _csrf = await login_via_code(local, db, clock)
        local_headers = {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}
        local.cookies.clear()
        r = await local.put(f"{API}/remote/password", json={"password": PASSWORD}, headers=local_headers)
        assert r.status_code == 200, r.text
        r = await local.put(
            f"{API}/remote", json={"enabled": True, "hosts": [REMOTE_HOST]}, headers=local_headers
        )
        assert r.status_code == 200, r.text
        r = await local.post(f"{API}/load/claims", json=CLAIM, headers=local_headers)
        assert r.status_code == 200, r.text
        claim_id = r.json()["id"]

        async with AsyncClient(
            transport=ASGITransport(app=rapp),
            base_url=f"http://127.0.0.1:{REMOTE_PORT}",
            headers={"Host": REMOTE_HOST},
        ) as phone:
            r = await phone.post(
                f"{API}/auth/password", json={"password": PASSWORD}, headers={"Origin": REMOTE_ORIGIN}
            )
            assert r.status_code == 200, r.text
            csrf_phone = r.json()["csrf"]
            cookie_phone = phone.cookies.get("hub_session")
            phone.cookies.clear()
            phone_headers = {
                "Cookie": f"hub_session={cookie_phone}",
                "Origin": REMOTE_ORIGIN,
                "X-Hub-CSRF": csrf_phone,
            }
            r = await phone.get(f"{API}/load", headers=phone_headers)
            assert r.status_code == 200 and [c["id"] for c in r.json()["claims"]] == [claim_id]
            r = await phone.post(f"{API}/load/claims", json=CLAIM, headers=phone_headers)
            assert r.status_code == 403 and r.json()["detail"] == "forbidden: local only"
            # (удалённый слушатель требует Content-Length у пишущих запросов — пустое тело)
            r = await phone.delete(
                f"{API}/load/claims/{claim_id}", headers={**phone_headers, "Content-Length": "0"}
            )
            assert r.status_code == 403 and r.json()["detail"] == "forbidden: local only"
    assert _count(db) == 1
