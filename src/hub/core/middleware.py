"""Host / Origin / CSRF / заголовки безопасности. Чистый ASGI — не `BaseHTTPMiddleware` (тот буферизует
тело ответа и ломает SSE, см. раздел «Middleware» docs/architecture.md).

Порядок проверок внутри одного запроса: Host -> OPTIONS(405) -> Origin/Sec-Fetch-Site/CSRF для не-GET/HEAD.
Заголовки безопасности навешиваются на КАЖДЫЙ исходящий ответ, включая те, что эта middleware обрывает сама.

Ф10-7 (docs/architecture.md–2): у ядра два слушателя одного приложения —
локальный (`port`, 127.0.0.1:8787) и удалённый (`remote_port`, 127.0.0.1:8790, только для туннеля с телефона).
Какой это слушатель, решает ПРИНЯВШИЙ сокет (`scope["server"][1]`), никогда не заголовки; прочий порт -> 421.
Пометка `scope["state"]["hub_remote"]` — для роутов (`security.request_is_remote`). Удалённый: доступ
выключен -> 421; Host ровно один и из `remote.hosts`, иначе 421; любой `Authorization` -> 403; OPTIONS -> 405;
не-GET/HEAD — Origin обязателен и равен `<схема>://` + Host этого запроса, Sec-Fetch-Site если есть —
same-origin, CSRF как у локального, из CSRF исключён только вход паролем. Ф14-9 решение 5: схема — ровно
`scope["scheme"]` принявшего сокета (uvicorn ставит `https` на TLS-сокете): на https-слушателе `http://…` —
403, и наоборот.
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .security import extract_cookie, verify_csrf

if TYPE_CHECKING:
    from .remote import RemoteState

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Вход: CSRF не требуется даже при наличии cookie — иначе с протухшей cookie в браузере повторный вход
# невозможен (HttpOnly-cookie страница не читает и CSRF вычислить не может). Секрет запроса — сам
# одноразовый код, Origin проверяется как обычно.
_CSRF_EXEMPT_PATHS = frozenset({"/api/v1/auth/session"})
# Ф10-7, решение 2: на удалённом слушателе без CSRF — только вход паролем (секрет запроса — сам пароль).
_REMOTE_CSRF_EXEMPT_PATHS = frozenset({"/api/v1/auth/password"})
# Ф10-7, находка ревью: FastAPI читает JSON-тело целиком ДО очереди входа, блока и проверки сессии —
# достучавшийся до loopback сервера VPN без пароля держал бы в памяти ядра сколько угодно мегабайт на запрос.
# На удалённом слушателе не-GET/HEAD — только с `Content-Length` (иначе 411) и не больше 1 МиБ (иначе 413):
# самое длинное честное тело — текст сообщения (Slack до 40 000 символов, кириллица — ~80 КБ) и черновик.
# Загрузка файлов — свой путь: `Content-Length` обязателен, потолок 50 МиБ, тело читается потоком ПОСЛЕ
# проверки сессии (`routes/uploads.py`).
_REMOTE_BODY_MAX = 1024 * 1024
_REMOTE_STREAMED_BODY_PATHS = frozenset({"/api/v1/uploads"})

# Раздел 5 плана, буквально. Ф8-5 A3: `media-src 'self'` — `<video>`/`<audio>` из `/api/v1/media/{id}`.
# Ф14-4 A3 (П12): `blob:` в `media-src` — прослушать свою запись голосового до отправки.
_CSP = (
    b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    b"connect-src 'self'; frame-src http://127.0.0.1:8788; object-src 'none'; base-uri 'none'; "
    b"form-action 'none'; frame-ancestors 'none'"
)
# Ф56-5б: xterm 6 вставляет свои `<style>` (цвета, размеры ячеек) — под `style-src 'self'` терминал не
# рисуется (проба `.runs/f565-ui/csp`). Только для страниц приложения (не `/api/`): `<style>`-элементы
# разрешены, атрибуты `style=` и остальное — прежние. Скрипты и подключение по-прежнему только свои.
_APP_CSP = _CSP + b"; style-src-elem 'self' 'unsafe-inline'"
_BASE_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"content-security-policy", _CSP),
)


def _get_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def _count_header(headers: list[tuple[bytes, bytes]], name: bytes) -> int:
    return sum(1 for key, _value in headers if key.lower() == name)


def _all_cookies(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Все заголовки Cookie, склеенные в один: HTTP/2 вправе дробить их, а middleware и security.py обязаны
    видеть одно и то же — иначе один решит «cookie нет», а другой найдёт сессию во втором заголовке."""
    values = [value.decode("latin-1") for key, value in headers if key.lower() == b"cookie"]
    return "; ".join(values) if values else None


def security_headers(path: str) -> list[tuple[bytes, bytes]]:
    headers = list(_BASE_SECURITY_HEADERS)
    if path.startswith("/api/"):
        headers.append((b"cache-control", b"no-store"))
    else:
        headers = [(k, _APP_CSP if k == b"content-security-policy" else v) for k, v in headers]
    return headers


async def _send_empty(send: Send, status: int, path: str) -> None:
    await send({"type": "http.response.start", "status": status, "headers": security_headers(path)})
    await send({"type": "http.response.body", "body": b""})


class SecurityMiddleware:
    """`app.add_middleware(SecurityMiddleware, port=8787, remote_port=8790, remote_state=state)` — чистый
    ASGI-класс, Starlette оборачивает его так же, как BaseHTTPMiddleware, но без буферизации: `__call__` сам
    решает, звать ли `receive`/`send`. `remote_port=0` — удалённого слушателя нет (любой другой порт -> 421).
    """

    def __init__(
        self, app: Callable, *, port: int, remote_port: int = 0, remote_state: RemoteState | None = None
    ) -> None:
        self.app = app
        self._port = port
        self._remote_port = remote_port
        self._remote_state = remote_state
        self._hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self._origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        server = scope.get("server")
        listener_port = server[1] if isinstance(server, (tuple, list)) and len(server) == 2 else None
        if listener_port == self._port:
            remote = False
        elif self._remote_port and listener_port == self._remote_port:
            remote = True
        else:
            remote = None

        if scope["type"] != "http":
            # Вебсокетов у хаба нет. Локальный слушатель — как было (пропускаем, роутер сам закроет);
            # удалённый и чужой порт — закрыть сразу.
            if remote is False:
                await self.app(scope, receive, send)
            elif scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        path = scope.get("path", "")
        if remote is None:
            await _send_empty(send, 421, path)
            return
        if remote:
            await self._remote(scope, receive, send, path)
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        method = scope["method"]

        host = _get_header(headers, b"host")
        # Дубликат Host/Origin — не браузер и не честный клиент: «первый верный, второй чужой» не пройдёт.
        if host not in self._hosts or _count_header(headers, b"host") != 1:
            await _send_empty(send, 421, path)
            return

        if method == "OPTIONS":
            await _send_empty(send, 405, path)
            return

        if method not in _SAFE_METHODS and not self._origin_and_csrf_ok(headers, path):
            await _send_empty(send, 403, path)
            return

        scope.setdefault("state", {})["hub_remote"] = False
        await self.app(scope, receive, _wrap_send(send, path))

    async def _remote(self, scope: Scope, receive: Receive, send: Send, path: str) -> None:
        """Удалённый слушатель (туннель с телефона), решение 2 спеки Ф10-7."""
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        method = scope["method"]
        state = self._remote_state

        if state is None or not state.enabled:
            await _send_empty(send, 421, path)
            return

        host = _get_header(headers, b"host")
        if host is None or host not in state.host_set or _count_header(headers, b"host") != 1:
            await _send_empty(send, 421, path)
            return

        # Bearer-токены (MCP, агенты, хост сессий) извне не ходят — любой вид заголовка, на любом методе.
        if _count_header(headers, b"authorization") > 0:
            await _send_empty(send, 403, path)
            return

        if method == "OPTIONS":
            await _send_empty(send, 405, path)
            return

        scheme = scope.get("scheme")
        if method not in _SAFE_METHODS and not self._remote_origin_and_csrf_ok(headers, path, host, scheme):
            await _send_empty(send, 403, path)
            return

        if method not in _SAFE_METHODS and path not in _REMOTE_STREAMED_BODY_PATHS:
            status = _remote_body_refusal(headers)
            if status is not None:
                await _send_empty(send, status, path)
                return

        scope.setdefault("state", {})["hub_remote"] = True
        await self.app(scope, receive, _wrap_send(send, path))

    @staticmethod
    def _remote_origin_and_csrf_ok(
        headers: list[tuple[bytes, bytes]], path: str, host: bytes, scheme: object
    ) -> bool:
        if scheme not in ("http", "https"):
            return False
        if _count_header(headers, b"origin") != 1:
            return False
        if _get_header(headers, b"origin") != scheme.encode("ascii") + b"://" + host:
            return False
        sec_fetch_site = _get_header(headers, b"sec-fetch-site")
        if sec_fetch_site is not None and sec_fetch_site != b"same-origin":
            return False
        cookie_value = extract_cookie(_all_cookies(headers))
        if cookie_value is not None and path not in _REMOTE_CSRF_EXEMPT_PATHS:
            csrf_header = _get_header(headers, b"x-hub-csrf")
            if csrf_header is None or not verify_csrf(cookie_value, csrf_header.decode("latin-1")):
                return False
        return True

    def _origin_and_csrf_ok(self, headers: list[tuple[bytes, bytes]], path: str) -> bool:
        cookie_value = extract_cookie(_all_cookies(headers))
        auth = _get_header(headers, b"authorization")
        is_bearer = auth is not None and auth.lower().startswith(b"bearer ")

        origin = _get_header(headers, b"origin")
        if _count_header(headers, b"origin") > 1:
            return False
        # Послабление «не браузер» выдаётся по одному лишь ВИДУ заголовка Authorization, поэтому на ручке
        # входа, где принципала никто не проверяет, оно не действует: Origin обязателен всегда (критик, F1).
        if is_bearer and cookie_value is None and path not in _CSRF_EXEMPT_PATHS:
            # Не браузер — Origin не обязателен, но если пришёл чужой, всё равно 403.
            if origin is not None and origin not in self._origins:
                return False
        else:
            if origin is None or origin not in self._origins:
                return False
            sec_fetch_site = _get_header(headers, b"sec-fetch-site")
            if sec_fetch_site is not None and sec_fetch_site != b"same-origin":
                return False

        if cookie_value is not None and path not in _CSRF_EXEMPT_PATHS:
            csrf_header = _get_header(headers, b"x-hub-csrf")
            if csrf_header is None:
                return False
            if not verify_csrf(cookie_value, csrf_header.decode("latin-1")):
                return False

        return True


def _remote_body_refusal(headers: list[tuple[bytes, bytes]]) -> int | None:
    """411 — нет `Content-Length` (или их несколько, или не число); 413 — больше `_REMOTE_BODY_MAX`."""
    chunked = _get_header(headers, b"transfer-encoding") is not None
    if chunked or _count_header(headers, b"content-length") != 1:
        return 411
    raw = _get_header(headers, b"content-length") or b""
    if not raw.isdigit():
        return 411
    if int(raw) > _REMOTE_BODY_MAX:
        return 413
    return None


def _wrap_send(send: Send, path: str) -> Send:
    """Добавить заголовки безопасности к ответу, который сформирует нижестоящее приложение.

    Ф8-5 П9: заголовок, который приложение уже поставило само (отдача медиа — свой `Content-Security-Policy`
    с `sandbox` и `Cache-Control: private`; SSE и статика — свой `Cache-Control`), не дописывается второй раз:
    каждый заголовок в ответе ровно один раз."""

    async def _send(message: dict) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            present = {key.lower() for key, _value in headers}
            extra = [(key, value) for key, value in security_headers(path) if key not in present]
            message = {**message, "headers": [*headers, *extra]}
        await send(message)

    return _send
