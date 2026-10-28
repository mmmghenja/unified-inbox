"""Клиент публичного API ядра для службы записи `hub-listen` (Ф60-1, «Контракт ручек A (факт)»).

- `httpx.AsyncClient(trust_env=False)`: токен не уходит в прокси окружения.
- `Authorization: Bearer <токен службы>` (ровно скоуп `listen`), `X-Hub-Client: listen:recorder`.
- 401 -> один раз перечитать файл токена (повторный `hub listen setup`); не помогло — `CoreUnauthorized`.
- Ядро не отвечает — `CoreUnavailable`. Коды ответа `chunk`/`warn` разбирает служба (409 на кусок в полёте
  глотается молча). Тела ответов в текст ошибок не идут."""

from dataclasses import dataclass
from pathlib import Path

import httpx

from . import files

TIMEOUT_S = 10.0
CHUNK_TIMEOUT_S = 40.0  # распознавание у ядра: таймаут SpeechKit плюс запас
CLIENT_NAME = "listen:recorder"


class CoreUnavailable(Exception):
    pass


class CoreUnauthorized(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    status: int
    detail: str | None = None


class ListenCore:
    def __init__(
        self,
        *,
        token_file: Path,
        base_url: str = files.DEFAULT_URL,
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

    def _current_token(self) -> str:
        token = self._token or files.read_secret(self._token_file)
        if token is None:
            raise CoreUnauthorized("нет токена службы: hub listen setup")
        self._token = token
        return token

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = self._current_token()
        try:
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
            if resp.status_code == 401:
                fresh = files.read_secret(self._token_file)
                if fresh is not None and fresh != token:
                    self._token = fresh
                    resp = await self._http.request(
                        method, path, headers={"Authorization": f"Bearer {fresh}"}, **kwargs
                    )
        except httpx.HTTPError as exc:
            raise CoreUnavailable(exc.__class__.__name__) from None
        if resp.status_code in (401, 403):
            raise CoreUnauthorized("токен службы отозван или без скоупа listen: hub listen setup")
        return resp

    @staticmethod
    def _detail(resp: httpx.Response) -> str | None:
        try:
            body = resp.json()
        except ValueError:
            return None
        detail = body.get("detail") if isinstance(body, dict) else None
        return detail if isinstance(detail, str) else None

    async def current(self) -> dict | None:
        """`GET /listen/current` -> `{id, status, them_lang}` или `None` (интервью нет)."""
        resp = await self._send("GET", "/listen/current")
        if resp.status_code != 200:
            raise CoreUnavailable(f"HTTP {resp.status_code}")
        try:
            item = resp.json().get("interview")
        except ValueError, AttributeError:
            raise CoreUnavailable("неразборчивый ответ ядра") from None
        if item is None:
            return None
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise CoreUnavailable("неразборчивый ответ ядра")
        return item

    async def chunk(self, interview_id: int, *, who: str, start_ms: int, end_ms: int, pcm: bytes) -> Reply:
        resp = await self._send(
            "POST",
            f"/listen/{int(interview_id)}/chunk",
            params={"who": who, "start_ms": int(start_ms), "end_ms": int(end_ms)},
            content=pcm,
            timeout=CHUNK_TIMEOUT_S,
        )
        return Reply(resp.status_code, None if resp.status_code == 200 else self._detail(resp))

    async def warn(self, interview_id: int, text: str) -> Reply:
        resp = await self._send("POST", f"/listen/{int(interview_id)}/warn", json={"text": text})
        return Reply(resp.status_code, None if resp.status_code == 200 else self._detail(resp))
