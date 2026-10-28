"""Клиент публичного API ядра для службы расшифровки (Ф14-6, раздел C) — обычный клиент, как hub-agent.

- `httpx.AsyncClient(trust_env=False)`: токен не уходит в прокси окружения.
- `Authorization: Bearer <токен службы>`, `X-Hub-Client: transcribe:worker`.
- 401 -> один раз перечитать файл токена (повторный `hub transcribe setup`); не помогло — `CoreUnauthorized`.
- Ядро не отвечает / 5xx — `CoreUnavailable`; 4xx — `CoreError` с кодом. Тела ответов в текст ошибок не идут.
"""

import os
from pathlib import Path

import httpx

from . import files

DEFAULT_URL = "http://127.0.0.1:8787/api/v1"
TIMEOUT_S = 30.0
CLIENT_NAME = "transcribe:worker"
DOWNLOAD_MAX_BYTES = 400 * 1024 * 1024  # потолок «по клику» у медиа — 300 МиБ; с запасом


class CoreUnavailable(Exception):
    pass


class CoreUnauthorized(Exception):
    pass


class CoreError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class TranscribeCore:
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

    def _current_token(self) -> str:
        token = self._token or files.read_secret(self._token_file)
        if token is None:
            raise CoreUnauthorized("нет токена службы: hub transcribe setup")
        self._token = token
        return token

    async def _send(self, method: str, path: str, *, http_timeout: float | None = None, **kwargs):
        token = self._current_token()
        extra = {} if http_timeout is None else {"timeout": http_timeout}
        try:
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **extra, **kwargs
            )
            if resp.status_code == 401:
                fresh = files.read_secret(self._token_file)
                if fresh is not None and fresh != token:
                    self._token = fresh
                    resp = await self._http.request(
                        method, path, headers={"Authorization": f"Bearer {fresh}"}, **extra, **kwargs
                    )
        except httpx.HTTPError as exc:
            raise CoreUnavailable(exc.__class__.__name__) from None
        if resp.status_code == 401:
            raise CoreUnauthorized("токен службы отозван: hub transcribe setup")
        if resp.status_code >= 500:
            raise CoreUnavailable(f"HTTP {resp.status_code}")
        return resp

    async def _json(self, method: str, path: str, *, http_timeout: float | None = None, json=None):
        resp = await self._send(method, path, http_timeout=http_timeout, json=json)
        if resp.status_code == 204:
            return None
        if resp.status_code >= 400:
            raise CoreError(resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise CoreUnavailable("неразборчивый ответ ядра") from None

    async def lease(self, wait_s: float, *, fast: bool = False) -> dict | None:
        """Задание или `None` (204 — за `wait_s` ничего не пришло). Ф79-1: `fast` — быстрый работник (поле
        шлётся только им: медленный работает и со старым ядром)."""
        body: dict = {"wait_s": wait_s}
        if fast:
            body["fast"] = True
        return await self._json("POST", "/transcripts/lease", json=body, http_timeout=wait_s + TIMEOUT_S)

    async def speech_lease(self, wait_s: float) -> dict | None:
        """Ф79-1: кусок диктовки `{job_id, lang, audio_b64}` или `None` (204)."""
        return await self._json(
            "POST", "/speech/lease", json={"wait_s": wait_s}, http_timeout=wait_s + TIMEOUT_S
        )

    async def speech_result(self, job_id: str, body: dict) -> None:
        """Ф79-1: `{text}` или `{error}`; 409 — задание снято (ядро ответило без нас) — `CoreError`."""
        await self._json("POST", f"/speech/{job_id}/result", json=body)

    async def extend(self, media_id: int, lease_gen: int, duration_s: float) -> dict:
        return await self._json(
            "POST",
            f"/transcripts/{int(media_id)}/extend",
            json={"lease_gen": lease_gen, "duration_s": duration_s},
        )

    async def report(self, media_id: int, body: dict) -> dict:
        return await self._json("POST", f"/transcripts/{int(media_id)}/report", json=body)

    async def download(self, media_id: int, dest: Path) -> int:
        """`GET /media/{id}` потоком в `dest` (0600, O_EXCL). 404 -> `CoreError(404)` (файла нет, П10)."""
        token = self._current_token()
        try:
            async with self._http.stream(
                "GET", f"/media/{int(media_id)}", headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status_code == 401:
                    raise CoreUnauthorized("токен службы отозван: hub transcribe setup")
                if resp.status_code >= 500:
                    raise CoreUnavailable(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise CoreError(resp.status_code)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                size = 0
                with os.fdopen(fd, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > DOWNLOAD_MAX_BYTES:
                            raise CoreError(413)
                        f.write(chunk)
                return size
        except httpx.HTTPError as exc:
            raise CoreUnavailable(exc.__class__.__name__) from None
