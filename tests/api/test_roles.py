"""Ф41-8: `GET /roles` — каталог ролей для интерфейса, без текстов; `GET /roles/{key}` — одна роль с текстом
(только чтение); только кука."""

import pytest
from httpx import ASGITransport, AsyncClient

from hub import roles

from .conftest import BASE_URL, login_via_code, make_bearer_token

URL = "/api/v1/roles"


async def test_cookie_gets_roles_without_text(client, db, clock):
    await login_via_code(client, db, clock)
    r = await client.get(URL)
    assert r.status_code == 200
    items = r.json()
    # порядок — как в форме «Новая сессия»: по имени файла роли
    want = ["duty", "programmer", "marketer", "researcher", "strategist", "product", "network"]
    assert [i["key"] for i in items] == want
    for item in items:
        # Ф48-2 (D): умолчания сжатия роли — необязательные поля шапки, у ролей хаба не заданы.
        assert set(item) == {"key", "title", "names", "sub", "compact_tokens", "compact_auto"}
        assert (item["compact_tokens"], item["compact_auto"]) == (None, None)
        assert item["names"] and item["title"]
    prog = next(i for i in items if i["key"] == "programmer")
    assert "программист" in prog["names"] and prog["sub"] == "direct"


async def test_no_login_refused(client):
    assert (await client.get(URL)).status_code in (401, 403)


@pytest.mark.parametrize("scope", ["read", "organize", "draft"])
async def test_bearer_forbidden(app, db, scope):
    token = make_bearer_token(db, scopes=(scope,), name=f"t-{scope}")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        r = await c.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_role_text_by_key(client, db, clock):
    await login_via_code(client, db, clock)
    r = await client.get(f"{URL}/programmer")
    assert r.status_code == 200, r.text
    item = r.json()
    assert set(item) == {"key", "title", "names", "sub", "text", "compact_tokens", "compact_auto"}
    assert item["key"] == "programmer" and "программист" in item["names"] and item["sub"] == "direct"
    # Ф48-3: встроенного текста роли нет — вставка хаба для роли — только общие правила.
    assert item["text"] == roles.prompt("программист") == roles.common_text()


async def test_role_text_unknown_key_404(client, db, clock):
    await login_via_code(client, db, clock)
    for key in ("tester", "_common", "программист"):
        assert (await client.get(f"{URL}/{key}")).status_code == 404


async def test_role_text_only_cookie(app, db, client):
    assert (await client.get(f"{URL}/programmer")).status_code in (401, 403)
    token = make_bearer_token(db, scopes=("read",), name="t-read-text")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        r = await c.get(f"{URL}/programmer", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_roles_have_no_write_handlers(client, db, clock):
    cookie, csrf = await login_via_code(client, db, clock)
    for method in ("put", "patch", "post", "delete"):
        r = await getattr(client, method)(f"{URL}/programmer", headers={"X-CSRF-Token": csrf or ""})
        assert r.status_code in (403, 404, 405), (method, r.status_code)
