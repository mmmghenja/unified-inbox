"""Клиент публичного API ядра для hub-mcp. Спека 7a, B2/B3 и П3/П7/П9.

- `httpx.AsyncClient(trust_env=False)` ОБЯЗАТЕЛЬНО (П3): процесс запускают Claude Code/OpenCode со своим
  окружением, где может стоять `HTTP(S)_PROXY`/`ALL_PROXY`, — bearer-токен не должен уйти в прокси.
- `Authorization: Bearer <токен профиля>`, `X-Hub-Client: mcp:<профиль>` (идёт в `audit.client_name`),
  таймаут 10 с.
- Токен читается из файла профиля; на 401 файл перечитывается один раз и запрос повторяется новым токеном
  (П7: открытые сессии Claude Code переживают повторный `hub mcp setup`).
- Любой отказ — `HubError` с понятным текстом; инструмент превращает его в результат-ошибку, процесс жив.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, urlsplit

import httpx

from . import profile as profile_mod

DEFAULT_URL = "http://127.0.0.1:8787/api/v1"
TIMEOUT_S = 10.0
UPLOAD_TIMEOUT_S = 300.0  # Ф14-3 B3: загрузка файла до 50 МиБ


class HubError(Exception):
    """Понятная агенту ошибка обращения к ядру. `status` — HTTP-код ядра (None — ядро не ответило
    или профиль не настроен)."""

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        # Ф41-5б (5.2): машинное поле отказа ядра `request_hint` — можно подать заявку владельцу.
        self.request_hint = False


class BodyTooLarge(HubError):
    """Ф22-1 C3: тело `GET /media/{id}` длиннее ожидаемого — загрузка оборвана."""


class FileChunk(NamedTuple):
    """Ф10-2 B2: начало файла из `GET /media/{id}`. `size` — полный размер по `Content-Length` (или длина
    прочитанного, если заголовка нет и файл дочитан); `complete` — прочитан ли файл целиком."""

    content_type: str
    headers: httpx.Headers
    data: bytes
    size: int | None
    complete: bool


class HubClient:
    def __init__(
        self,
        *,
        profile: str,
        config_dir: Path,
        base_url: str = DEFAULT_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.profile = profile
        self._config_dir = config_dir
        self._base_url = base_url
        self._token = profile_mod.read_token(config_dir, profile)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            headers={"X-Hub-Client": f"mcp:{profile}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _core_address(self) -> str:
        parts = urlsplit(self._base_url)
        return parts.netloc or self._base_url

    def _setup_hint(self) -> str:
        return f"hub mcp setup --profile {self.profile}"

    async def _send(self, method: str, path: str, token: str, *, headers: dict | None = None, **kwargs):
        try:
            return await self._http.request(
                method, path, headers={**(headers or {}), "Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.TransportError as exc:
            raise HubError(f"хаб недоступен (core на {self._core_address()} не отвечает)") from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json=None,
        http_timeout: float | None = None,
    ):
        """`http_timeout` — свой таймаут этого запроса (Ф11-1 B1: ожидание беседы держит запрос до 135 с)."""
        token = self._token or profile_mod.read_token(self._config_dir, self.profile)
        if token is None:
            raise HubError(f"профиль {self.profile} не настроен: {self._setup_hint()}")
        self._token = token

        kwargs: dict = {}
        if params is not None:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if json is not None:
            kwargs["json"] = json
        if http_timeout is not None:
            kwargs["timeout"] = http_timeout

        resp = await self._send(method, path, token, **kwargs)
        if resp.status_code == 401:
            fresh = profile_mod.read_token(self._config_dir, self.profile)
            if fresh is not None and fresh != token:
                self._token = fresh
                resp = await self._send(method, path, fresh, **kwargs)
        return self._result(resp)

    def _result(self, resp: httpx.Response):
        status = resp.status_code
        if 200 <= status < 300:
            return resp.json()
        detail = _detail(resp)
        if status == 401:
            raise HubError(f"токен профиля отозван или неверный: {self._setup_hint()}", status=status)
        if status == 403:
            raise HubError("нет права (скоуп профиля этого не позволяет)", status=status, detail=detail)
        if status == 404:
            raise HubError("не найдено или вне проектов профиля", status=status, detail=detail)
        if status in (409, 422, 429):
            error = HubError(f"ядро отказало (HTTP {status}): {detail}", status=status, detail=detail)
            error.request_hint = _request_hint(resp)
            raise error
        raise HubError(f"ошибка ядра (HTTP {status})", status=status, detail=detail)

    @asynccontextmanager
    async def _stream(self, path: str) -> AsyncIterator[httpx.Response]:
        """GET потоком с тем же токеном и повтором на 401, что `request`; не-2xx — `HubError`."""
        token = self._token or profile_mod.read_token(self._config_dir, self.profile)
        if token is None:
            raise HubError(f"профиль {self.profile} не настроен: {self._setup_hint()}")
        self._token = token
        resp = await self._send_stream(path, token)
        if resp.status_code == 401:
            fresh = profile_mod.read_token(self._config_dir, self.profile)
            if fresh is not None and fresh != token:
                await resp.aclose()
                self._token = fresh
                resp = await self._send_stream(path, fresh)
        try:
            if not 200 <= resp.status_code < 300:
                await resp.aread()
                self._result(resp)
            yield resp
        finally:
            await resp.aclose()

    async def _send_stream(self, path: str, token: str) -> httpx.Response:
        request = self._http.build_request("GET", path, headers={"Authorization": f"Bearer {token}"})
        try:
            return await self._http.send(request, stream=True)
        except httpx.TransportError as exc:
            raise HubError(f"хаб недоступен (core на {self._core_address()} не отвечает)") from exc

    async def get_file(self, path: str, limit_for) -> FileChunk:
        """Ф10-2 B2: файл ядра потоком — не больше `limit_for(content_type, content_length, headers)` байт
        (0 — тело не читается вовсе: видео, большие картинки). Файловую систему не трогает."""
        async with self._stream(path) as resp:
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            raw_length = resp.headers.get("content-length")
            length = int(raw_length) if raw_length and raw_length.isdigit() else None
            limit = max(0, int(limit_for(content_type, length, resp.headers)))
            chunks: list[bytes] = []
            got = 0
            complete = True
            if limit > 0:
                try:
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        got += len(chunk)
                        if got > limit:
                            complete = False
                            break
                except httpx.TransportError as exc:
                    raise HubError(f"хаб недоступен (core на {self._core_address()} не отвечает)") from exc
            else:
                complete = length == 0
            data = b"".join(chunks)[:limit]
            if length is None and complete:
                length = got
            return FileChunk(content_type, resp.headers, data, length, complete and got <= limit)

    async def stream_file(self, path: str, sink, *, max_bytes: int) -> int:
        """Ф22-1 C3: файл ядра потоком в `sink(chunk)` порциями, НЕ в память. `Content-Length` больше
        `max_bytes` — `BodyTooLarge` до чтения тела; тело длиннее — `BodyTooLarge` с обрывом. Возвращает
        число полученных байт."""
        async with self._stream(path) as resp:
            raw_length = resp.headers.get("content-length")
            if raw_length and raw_length.isdigit() and int(raw_length) > max_bytes:
                raise BodyTooLarge("файл в хабе больше ожидаемого — повтори hub_download")
            got = 0
            try:
                async for chunk in resp.aiter_bytes():
                    got += len(chunk)
                    if got > max_bytes:
                        raise BodyTooLarge("файл в хабе больше ожидаемого — повтори hub_download")
                    sink(chunk)
            except httpx.TransportError as exc:
                raise HubError(f"хаб недоступен (core на {self._core_address()} не отвечает)") from exc
            return got

    async def upload(self, path: str, *, params: dict, content_factory, size: int, filename: str):
        """Ф14-3 B3: сырые байты в `POST /uploads` — `Content-Length`, `X-Filename` (urlencoded UTF-8),
        таймаут `UPLOAD_TIMEOUT_S`. `content_factory()` — новый асинхронный поток байт на каждую попытку
        (повтор на 401 с перечитанным токеном — как у `request`)."""
        token = self._token or profile_mod.read_token(self._config_dir, self.profile)
        if token is None:
            raise HubError(f"профиль {self.profile} не настроен: {self._setup_hint()}")
        self._token = token
        headers = {
            "Content-Length": str(size),
            "Content-Type": "application/octet-stream",
            "X-Filename": quote(filename, safe=""),
        }

        async def _once(value: str) -> httpx.Response:
            return await self._send(
                "POST",
                path,
                value,
                params=params,
                content=content_factory(),
                headers=headers,
                timeout=UPLOAD_TIMEOUT_S,
            )

        resp = await _once(token)
        if resp.status_code == 401:
            fresh = profile_mod.read_token(self._config_dir, self.profile)
            if fresh is not None and fresh != token:
                self._token = fresh
                resp = await _once(fresh)
        return self._result(resp)

    async def post_form(self, path: str, *, data: str, file: bytes | None = None, filename: str = "shot"):
        """Ф54-1 П10: `multipart/form-data` одним запросом — часть `data` (JSON строкой) и необязательная
        часть `shot` (байты файла). Повтор на 401 с перечитанным токеном — как у `request`."""
        token = self._token or profile_mod.read_token(self._config_dir, self.profile)
        if token is None:
            raise HubError(f"профиль {self.profile} не настроен: {self._setup_hint()}")
        self._token = token

        async def _once(value: str) -> httpx.Response:
            parts: dict = {"data": (None, data)}
            if file is not None:
                parts["shot"] = (filename, file, "application/octet-stream")
            return await self._send("POST", path, value, files=parts, timeout=UPLOAD_TIMEOUT_S)

        resp = await _once(token)
        if resp.status_code == 401:
            fresh = profile_mod.read_token(self._config_dir, self.profile)
            if fresh is not None and fresh != token:
                self._token = fresh
                resp = await _once(fresh)
        return self._result(resp)

    async def get(self, path: str, params: dict | None = None):
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json=None):
        return await self.request("POST", path, json=json)

    async def patch(self, path: str, json=None):
        return await self.request("PATCH", path, json=json)


def _request_hint(resp: httpx.Response) -> bool:
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("request_hint") is True


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:500]
    return str(body)[:300]
