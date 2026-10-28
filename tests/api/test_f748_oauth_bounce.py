"""Ф74-8 (план фазы 74, раздел 7, п. 7): ответ браузера на вход MCP по OAuth у Cursor
(`http://localhost:8787/callback?code=…&state=…`) пришёл в ядро хаба (127.0.0.1:8787 занят им, Cursor ждёт на
`[::1]:8787`) — ядро отсылает его туда, а не в интерфейс хаба."""

from hub.core import static_ui

from .test_static_ui import _client_for, _make_app, _write_ui_fixture


async def test_callback_with_state_and_code_goes_to_ipv6_loopback(db, clock, tmp_path):
    ui_dir, _outside = _write_ui_fixture(tmp_path)
    async with await _client_for(_make_app(db, clock, ui_dir)) as client:
        r = await client.get("/callback?code=abc&state=s1%2Bx")
        assert r.status_code == 307
        assert r.headers["location"] == "http://[::1]:8787/callback?code=abc&state=s1%2Bx"
        assert r.headers["cache-control"] == "no-store"
        err = await client.get("/callback?error=access_denied&state=s2")
        assert err.headers["location"] == "http://[::1]:8787/callback?error=access_denied&state=s2"
        # Не ответ входа — обычная страница интерфейса.
        for url in ("/callback", "/callback?code=abc", "/callback?state=s1", "/callbacks?code=a&state=b"):
            page = await client.get(url)
            assert page.status_code == 200 and b"demo" in page.content, url


def _scope(*, remote: bool | None, query: bytes, port: object = 8787) -> dict:
    scope = {"query_string": query, "server": ("127.0.0.1", port)}
    if remote is not None:
        scope["state"] = {"hub_remote": remote}
    return scope


def test_bounce_only_on_local_listener():
    query = b"code=a&state=b"
    assert static_ui._oauth_bounce(_scope(remote=False, query=query), "/callback")  # noqa: SLF001
    assert static_ui._oauth_bounce(_scope(remote=True, query=query), "/callback") is None  # noqa: SLF001
    # Пометки слушателя нет — не отсылать (не знаем, чей слушатель).
    assert static_ui._oauth_bounce(_scope(remote=None, query=query), "/callback") is None  # noqa: SLF001
    for port in (None, 0, True, "8787", 70000):
        bounce = static_ui._oauth_bounce(_scope(remote=False, query=query, port=port), "/callback")  # noqa: SLF001
        assert bounce is None, port
    long = b"state=b&code=" + b"x" * 17000
    assert static_ui._oauth_bounce(_scope(remote=False, query=long), "/callback") is None  # noqa: SLF001
