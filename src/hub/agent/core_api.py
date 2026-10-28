"""Клиент публичного API ядра для hub-agent. Спека 7b, B (обычный клиент API, как hub-mcp), поправка П7.

- `httpx.AsyncClient(trust_env=False)` ОБЯЗАТЕЛЬНО (приём 7a, П3): токен ядра не уходит в прокси окружения.
- `Authorization: Bearer <токен агента>`, `X-Hub-Client: agent:triage` (идёт в `audit.client_name`).
- 401 -> один раз перечитать файл токена: значение сменилось (повторный `hub agent setup`) — повторить запрос
  новым токеном; не сменилось — `CoreUnauthorized` (процесс выходит с кодом 78, юнит не перезапускается).
- Ядро не отвечает / 5xx — `CoreUnavailable` (пауза и повтор, курсор не двигается).
- Ошибки несут только HTTP-код: тело ответа ядра (422 FastAPI кладёт туда входные данные) в текст
  исключений и в журнал не попадает (П8).
"""

from pathlib import Path

import httpx

from . import files

DEFAULT_URL = "http://127.0.0.1:8787/api/v1"
TIMEOUT_S = 15.0
CLIENT_NAME = "agent:triage"


class CoreUnavailable(Exception):
    """Ядро не ответило или ответило 5xx."""


class CoreUnauthorized(Exception):
    """401 и после перечитывания файла токена (или токена нет вовсе)."""


class CoreError(Exception):
    """4xx от ядра (кроме 401). Текст — только код."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class CoreApi:
    def __init__(
        self,
        *,
        token_file: Path,
        base_url: str = DEFAULT_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._token_file = token_file
        self._token = files.read_secret(token_file)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            headers={"X-Hub-Client": CLIENT_NAME},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, token: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.HTTPError as exc:
            # Текст исключения httpx может нести URL — наружу только класс.
            raise CoreUnavailable(exc.__class__.__name__) from None

    async def request(self, method: str, path: str, *, params: dict | None = None, json=None):
        token = self._token or files.read_secret(self._token_file)
        if token is None:
            raise CoreUnauthorized("нет токена агента: hub agent setup")
        self._token = token
        kwargs: dict = {}
        if params is not None:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if json is not None:
            kwargs["json"] = json

        resp = await self._send(method, path, token, **kwargs)
        if resp.status_code == 401:
            fresh = files.read_secret(self._token_file)
            if fresh is not None and fresh != token:
                self._token = fresh
                resp = await self._send(method, path, fresh, **kwargs)
        status = resp.status_code
        if status == 401:
            raise CoreUnauthorized("токен агента отозван: hub agent setup")
        if status >= 500:
            raise CoreUnavailable(f"HTTP {status}")
        if status >= 400:
            raise CoreError(status)
        try:
            return resp.json()
        except ValueError:
            raise CoreUnavailable("неразборчивый ответ ядра") from None

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """Ф41-3в: тело ответа байтами, потоком и не больше `max_bytes` (лишнее — `CoreError(413)`, чтение
        обрывается). Сеть — `CoreUnavailable`; любой отказ ядра со статусом, включая 5xx, — `CoreError`
        (вызывающий считает попытки); 401 — как у `request`."""
        token = self._token or files.read_secret(self._token_file)
        if token is None:
            raise CoreUnauthorized("нет токена агента: hub agent setup")
        self._token = token
        for attempt in (1, 2):
            try:
                async with self._http.stream(
                    "GET", path, headers={"Authorization": f"Bearer {token}"}
                ) as resp:
                    if resp.status_code == 401 and attempt == 1:
                        fresh = files.read_secret(self._token_file)
                        if fresh is not None and fresh != token:
                            self._token = token = fresh
                            continue
                    if resp.status_code == 401:
                        raise CoreUnauthorized("токен агента отозван: hub agent setup")
                    if resp.status_code >= 400:
                        raise CoreError(resp.status_code)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise CoreError(413)
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise CoreUnavailable(exc.__class__.__name__) from None
        raise CoreUnauthorized("токен агента отозван: hub agent setup")

    async def get(self, path: str, params: dict | None = None):
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json=None):
        return await self.request("POST", path, json=json)
