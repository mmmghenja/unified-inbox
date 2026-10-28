"""Тесты куска 6a: раздача статики UI. Спецификация: docs/architecture.md, блок «6a (pytest)»,
пункты 1-6.

Своя фабрика приложения (`_make_app`) вместо фикстуры `app` из `tests/api/conftest.py` — той нужен свой
`ui_dir` на каждый тест (`tmp_path`), а фикстуры оттуда (`db`, `clock`) переиспользуются как есть (conftest
не трогаем, только читаем его константы копированием — тот же приём, что и во всём проекте: PORT=8787
совпадает с портом, на который рассчитан `SecurityMiddleware` по умолчанию в `create_public_app`).

Сырые пути с `..`/`%2e%2e`/двойным слэшем httpx нормализует ДО отправки запроса (это поведение самого httpx,
не сервера) — тест 4 поэтому идёт в обход `AsyncClient` и зовёт ASGI-приложение напрямую своим `scope`
(`_raw_request`), как и требует спека.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core.app_public import create_public_app

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
HOST_HEADER = f"127.0.0.1:{PORT}"


def _make_app(db, clock, ui_dir):
    return create_public_app(db, clock=clock, port=PORT, ui_dir=ui_dir)


async def _client_for(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url=BASE_URL)


def _write_ui_fixture(tmp_path):
    """Минимальный `ui_dir`: index.html, ассет с хешем, файл без известного расширения, символическая
    ссылка наружу. Возвращает `ui_dir` и путь к файлу ВНЕ него (цель обхода каталога/симлинка)."""
    ui_dir = tmp_path / "ui_dir"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<!doctype html><html><body>demo</body></html>", encoding="utf-8")
    assets = ui_dir / "assets"
    assets.mkdir()
    (assets / "app.abc123.js").write_text("console.log('hi')", encoding="utf-8")
    (ui_dir / "favicon.ico").write_bytes(b"\x00\x01ICO")
    (ui_dir / "readme.xyz").write_text("расширение вне таблицы", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pyproject.toml").write_text("СЕКРЕТ_СНАРУЖИ", encoding="utf-8")
    secret_js = outside / "secret.js"
    secret_js.write_text("СЕКРЕТ_JS_СНАРУЖИ", encoding="utf-8")
    (ui_dir / "evil.js").symlink_to(secret_js)

    return ui_dir, outside


async def _raw_request(
    app,
    *,
    path: str,
    method: str = "GET",
    raw_path: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Позвать ASGI-приложение напрямую со своим scope — так, как реальный сервер отдал бы уже
    процентно-декодированный `path` (никакой httpx-нормализации `..`/двойного слэша по дороге)."""
    header_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if not any(k == b"host" for k, _ in header_list):
        header_list.append((b"host", HOST_HEADER.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode(),
        "query_string": b"",
        "headers": header_list,
        "server": ("127.0.0.1", PORT),
        "client": ("127.0.0.1", 54321),
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], start["headers"], body


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


# --- 1. GET /, /login, /any/spa/path -> index.html 200, no-store, CSP и nosniff --------------------------


async def test_spa_paths_serve_index_html(db, clock, tmp_path):
    ui_dir, _outside = _write_ui_fixture(tmp_path)
    app = _make_app(db, clock, ui_dir)
    async with await _client_for(app) as client:
        for path in ("/", "/login", "/any/spa/path"):
            r = await client.get(path)
            assert r.status_code == 200, (path, r.text)
            assert r.headers["content-type"].startswith("text/html")
            assert r.headers["cache-control"] == "no-store"
            assert "content-security-policy" in r.headers
            assert r.headers["x-content-type-options"] == "nosniff"
            assert "demo" in r.text

        # HEAD — те же заголовки, без тела.
        r = await client.head("/login")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert r.content == b""


# --- 2. /assets/app.abc123.js -> верный Content-Type, immutable; несуществующий -> 404 ---------------------


async def test_assets_content_type_and_immutable_cache(db, clock, tmp_path):
    ui_dir, _outside = _write_ui_fixture(tmp_path)
    app = _make_app(db, clock, ui_dir)
    async with await _client_for(app) as client:
        r = await client.get("/assets/app.abc123.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/javascript")
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert r.text == "console.log('hi')"

        r = await client.get("/assets/x.js")
        assert r.status_code == 404
        assert r.json() == {"detail": "not found"}

        # Прочий файл (не /assets/*) — no-cache, не immutable.
        r = await client.get("/favicon.ico")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"


# --- 3. /api/v1/nope -> JSON 404, не HTML; POST / -> 403/405, не раздача ----------------------------------


async def test_api_prefix_never_served_as_static_and_post_root_rejected(db, clock, tmp_path):
    ui_dir, _outside = _write_ui_fixture(tmp_path)
    app = _make_app(db, clock, ui_dir)
    async with await _client_for(app) as client:
        r = await client.get("/api/v1/nope")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"detail": "not found"}
        assert "demo" not in r.text

        r = await client.get("/api")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")

        # Без Origin — middleware отбивает раньше статики: 403.
        r = await client.post("/", json={})
        assert r.status_code == 403

        # С верным Origin, без cookie — middleware не требует Origin/CSRF (не браузерная cookie-сессия),
        # запрос доходит до статики, а там метод не поддержан: 405 (спека допускает оба исхода).
        r = await client.post("/", json={}, headers={"Origin": BASE_URL})
        assert r.status_code == 405
        assert r.json() == {"detail": "method not allowed"}


# --- 4. Обход каталога -------------------------------------------------------------------------------------


async def test_path_traversal_is_rejected(db, clock, tmp_path):
    ui_dir, outside = _write_ui_fixture(tmp_path)
    app = _make_app(db, clock, ui_dir)

    secret_toml = (outside / "pyproject.toml").read_text(encoding="utf-8")
    secret_js = (outside / "secret.js").read_text(encoding="utf-8")

    # `/../pyproject.toml` — decoded-путь, как его дал бы реальный ASGI-сервер.
    status, _headers, body = await _raw_request(app, path="/../outside/pyproject.toml")
    assert status == 404, body
    assert json.loads(body) == {"detail": "not found"}
    assert secret_toml not in body.decode(errors="replace")

    # `%2e%2e` — ASGI-сервер отдаёт path УЖЕ раскодированным; raw_path хранит исходные байты для полноты.
    status, _headers, body = await _raw_request(
        app, path="/../outside/pyproject.toml", raw_path=b"/%2e%2e/outside/pyproject.toml"
    )
    assert status == 404
    assert json.loads(body) == {"detail": "not found"}
    assert secret_toml not in body.decode(errors="replace")

    # Двойной слэш в начале: наивный `ui_dir / path` отдал бы pathlib абсолютный путь и вышел из root.
    status, _headers, body = await _raw_request(app, path="//outside/pyproject.toml")
    assert status == 404, body
    assert json.loads(body) == {"detail": "not found"}
    assert secret_toml not in body.decode(errors="replace")

    # Файл с расширением вне таблицы — 404, содержимого нет.
    status, _headers, body = await _raw_request(app, path="/readme.xyz")
    assert status == 404
    assert b"detail" in body

    # Обратный слэш — не разделитель на POSIX: весь "..\\..\\etc\\passwd" остаётся ОДНИМ именем файла
    # внутри ui_dir (не выходит наружу) и просто не существует -> 404, никакого 500 и никакой утечки.
    status, _headers, body = await _raw_request(app, path="/..\\..\\etc\\passwd")
    assert status == 404
    assert b"passwd" not in body or json.loads(body) == {"detail": "not found"}

    # Симлинк из ui_dir, ведущий наружу: не отдаётся, несмотря на "правильное" (.js) расширение.
    status, _headers, body = await _raw_request(app, path="/evil.js")
    assert status == 404, body
    assert secret_js not in body.decode(errors="replace")


# --- 5. Чужой Host на статике -> 421; ui_dir=None -> GET / JSON 404, API работает ---------------------------


async def test_wrong_host_on_static_and_missing_ui_dir(db, clock, tmp_path):
    ui_dir, _outside = _write_ui_fixture(tmp_path)
    app = _make_app(db, clock, ui_dir)

    status, headers, body = await _raw_request(app, path="/login", headers={"Host": "evil.com"})
    assert status == 421
    assert body == b""

    status, headers, body = await _raw_request(app, path="/login", headers={"Host": "127.0.0.1:9999"})
    assert status == 421

    # ui_dir=None: GET / -> JSON 404 "ui not built"; API продолжает работать как обычно.
    app_no_ui = _make_app(db, clock, None)
    async with await _client_for(app_no_ui) as client:
        r = await client.get("/")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"detail": "ui not built"}

        r = await client.get("/api/v1/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # /assets/... тоже 404 "ui not built", не "not found" — весь UI одинаково недоступен.
        r = await client.get("/assets/app.js")
        assert r.status_code == 404
        assert r.json() == {"detail": "ui not built"}


# --- 6. Настоящий index.html из ui/dist (если собран) — без инлайн-script/style/on*=/style= -----------------


def test_real_built_index_html_has_no_inline_script_or_style():
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    index_path = repo_root / "ui" / "dist" / "index.html"
    if not index_path.is_file():
        pytest.skip("ui/dist ещё не собран (кусок 6b)")

    html = index_path.read_text(encoding="utf-8")
    lowered = html.lower()
    assert "<script>" not in lowered.replace(" ", "")
    assert "<style" not in lowered
    assert " style=" not in lowered
    assert " on" not in lowered or not any(
        f" on{event}=" in lowered
        for event in ("click", "error", "load", "mouseover", "focus", "input", "change")
    )


# --- Ф56-5б: CSP страницы приложения разрешает `<style>`-элементы (xterm), API — нет ----------------------


async def test_app_page_csp_allows_style_elements_api_does_not(db, clock, tmp_path):
    ui_dir, _outside = _write_ui_fixture(tmp_path)
    app = _make_app(db, clock, ui_dir)
    async with await _client_for(app) as client:
        page = (await client.get("/")).headers["content-security-policy"]
        api = (await client.get("/api/v1/healthz")).headers["content-security-policy"]
    assert "style-src-elem 'self' 'unsafe-inline'" in page
    assert "style-src 'self';" in page  # атрибуты `style=` по-прежнему закрыты
    assert "script-src 'self';" in page and "'unsafe-inline'" not in page.split("style-src-elem")[0]
    assert "style-src-elem" not in api and "unsafe-inline" not in api
