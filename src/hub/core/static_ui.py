"""Раздача статики UI (кусок 6a). Спецификация: docs/architecture.md,
«6a. Python-часть» и уточнения архитектора.

Свой маленький ASGI-обработчик, а не `StaticFiles(html=True)` — нужны: SPA-фолбэк на `index.html` для любого
пути без расширения, явный отказ обслуживать `/api/*`, и ручной контроль над Cache-Control/Content-Type.
Монтируется В КОНЦЕ, после роутеров `/api/v1` (`app_public.create_public_app`), поэтому `/api/v1/...` до
этого обработчика в принципе не доходит НИ РАЗУ, кроме путей, для которых нет ни одного маршрута — вот почему
здесь всё равно есть явная защита «/api/ никогда не отдаём статикой».

Заголовки безопасности (CSP, nosniff, ...) сюда не добавляются — их на КАЖДЫЙ ответ (в т.ч. с этого
обработчика) навешивает `SecurityMiddleware._wrap_send`, которая оборачивает всё приложение снаружи, включая
`app.mount(...)`. Здесь добавляются только `Content-Type` и `Cache-Control`.

Устойчивость к обходу каталога: `ui_dir.resolve()` берётся ОДИН раз при создании; кандидат на каждый запрос —
`(root / rel).resolve()` (без принудительного `lstrip` всех слэшей: если `rel` сам оказался абсолютным путём —
абсолютные пути и двойной слэш в начале — pathlib поглотит `root` целиком, и `.resolve()` даст путь ВНЕ
`root`, что `is_relative_to` поймает; см. тесты обхода каталога). Любая ошибка ОС/значения при разборе пути
(`OSError`, `ValueError` — в т.ч. нулевой байт) -> 404, никогда не 500.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]

# Раздел «6a. Python-часть» спеки, дословно: только эти расширения. Остальное -> 404, даже если файл есть.
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/vnd.microsoft.icon",
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".map": "application/json",
}

_INDEX_NAME = "index.html"
_ASSETS_PREFIX = "/assets/"

_METHOD_NOT_ALLOWED = {"detail": "method not allowed"}
_NOT_FOUND = {"detail": "not found"}
_UI_NOT_BUILT = {"detail": "ui not built"}


class StaticUIApp:
    """ASGI-приложение, монтируемое в `create_public_app` как `app.mount("/", StaticUIApp(ui_dir))`.

    `ui_dir=None` (или каталога не существует на момент создания) -> `self._root is None` -> любой путь,
    кроме `/api/*`, отвечает `{"detail": "ui not built"}` 404 (ядро работает без UI, раздел «Решения
    архитектора» спеки).
    """

    def __init__(self, ui_dir: Path | None) -> None:
        self._root: Path | None = ui_dir.resolve() if ui_dir is not None and ui_dir.is_dir() else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"  # noqa: S101 — сюда попадает только http (lifespan/websocket не мимо Mount)
        del receive

        path: str = scope.get("path", "/")
        method: str = scope.get("method", "GET")

        # `/api/` статикой не обслуживается НИКОГДА, независимо от метода и наличия UI — маршруты `/api/v1`
        # регистрируются раньше этого mount'а и перехватывают всё, что у них есть; сюда долетают только
        # НЕсуществующие /api/... пути, и они обязаны остаться JSON 404, а не стать index.html.
        if path == "/api" or path.startswith("/api/"):
            await _respond_json(send, 404, _NOT_FOUND, method)
            return

        if method not in ("GET", "HEAD"):
            await _respond_json(send, 405, _METHOD_NOT_ALLOWED, method)
            return

        bounce = _oauth_bounce(scope, path)
        if bounce is not None:
            await _respond(send, 307, [(b"location", bounce), (b"cache-control", b"no-store")], b"", method)
            return

        if self._root is None:
            await _respond_json(send, 404, _UI_NOT_BUILT, method)
            return

        await self._serve(send, path=path, method=method)

    async def _serve(self, send: Send, *, path: str, method: str) -> None:
        root = self._root
        assert root is not None  # noqa: S101 — проверено вызывающим (__call__)

        rel = path[1:]  # ASGI гарантирует path.startswith("/"); дальше — как есть, без нормализации.
        try:
            candidate = (root / rel).resolve() if rel else root
        except OSError, ValueError:
            # Нулевой байт и подобные некорректные для ОС значения -> 404, не 500 (уточнение архитектора).
            await _respond_json(send, 404, _NOT_FOUND, method)
            return

        try:
            inside = candidate.is_relative_to(root)
        except ValueError:
            inside = False
        if not inside:
            # Сюда попадают: `..`, `%2e%2e` (уже раскодирован ASGI-сервером в scope["path"]), абсолютные
            # пути и двойной слэш в начале (pathlib поглощает root при join с абсолютным операндом — тогда
            # .resolve() даёт путь вне root), и симлинк из ui_dir, ведущий наружу (resolve() его раскрывает).
            await _respond_json(send, 404, _NOT_FOUND, method)
            return

        last_segment = rel.rsplit("/", 1)[-1] if rel else ""
        suffix = PurePosixPath(last_segment).suffix.lower()

        if suffix == "":
            # SPA-фолбэк: путь без расширения внутри ui_dir -> index.html, каким бы он ни был (спека).
            await self._serve_index(send, method)
            return

        if suffix not in _CONTENT_TYPES:
            await _respond_json(send, 404, _NOT_FOUND, method)
            return

        try:
            is_file = candidate.is_file()
        except OSError, ValueError:
            is_file = False
        if not is_file:
            await _respond_json(send, 404, _NOT_FOUND, method)
            return

        if candidate == root / _INDEX_NAME:
            # Прямой запрос "/index.html" — та же семантика, что SPA-фолбэк (читать с диска каждый раз,
            # no-store): `vite build --watch` может переписать файл в любой момент.
            await self._serve_index(send, method)
            return

        try:
            data = candidate.read_bytes()
        except OSError, ValueError:
            await _respond_json(send, 404, _NOT_FOUND, method)
            return

        cache = b"public, max-age=31536000, immutable" if path.startswith(_ASSETS_PREFIX) else b"no-cache"
        headers = [
            (b"content-type", _CONTENT_TYPES[suffix].encode()),
            (b"cache-control", cache),
        ]
        await _respond(send, 200, headers, data, method)

    async def _serve_index(self, send: Send, method: str) -> None:
        assert self._root is not None  # noqa: S101
        try:
            data = (self._root / _INDEX_NAME).read_bytes()
        except OSError, ValueError:
            # Каталог ui_dir есть, но index.html в нём нет (сборка не завершена) — тот же ответ, что
            # "UI вообще не собран".
            await _respond_json(send, 404, _UI_NOT_BUILT, method)
            return
        headers = [
            (b"content-type", _CONTENT_TYPES[".html"].encode()),
            (b"cache-control", b"no-store"),
        ]
        await _respond(send, 200, headers, data, method)


_OAUTH_CALLBACK = "/callback"


def _oauth_bounce(scope: Scope, path: str) -> bytes | None:
    """Ф74-8 (план фазы 74, раздел 7, п. 7): вход MCP по OAuth у Cursor ждёт ответ браузера на
    `http://localhost:8787/callback` (`cursor-agent` 2026.10.19: слушает 127.0.0.1 и ::1 этого порта;
    127.0.0.1 занят ядром хаба — остаётся ::1). Браузер, ушедший на 127.0.0.1, попадал в интерфейс хаба, и
    вход терялся. Ядро отсылает такой ответ (`state` и `code` или `error` в запросе) на `[::1]` того же
    порта — Cursor его там ждёт. Только локальный слушатель (с телефона — нет); ничего не хранится и не
    читается."""
    if path != _OAUTH_CALLBACK or scope.get("state", {}).get("hub_remote", True):
        return None
    raw: bytes = scope.get("query_string") or b""
    keys = {part.split(b"=", 1)[0] for part in raw.split(b"&") if part}
    if b"state" not in keys or not keys & {b"code", b"error"} or len(raw) > 16384:
        return None
    server = scope.get("server") or (None, None)
    port = server[1] if len(server) > 1 else None
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    return f"http://[::1]:{port}{_OAUTH_CALLBACK}?".encode() + raw


async def _respond(
    send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes, method: str
) -> None:
    full_headers = [*headers, (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": full_headers})
    await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})


async def _respond_json(send: Send, status: int, payload: dict, method: str) -> None:
    body = json.dumps(payload).encode()
    await _respond(send, status, [(b"content-type", b"application/json")], body, method)
