"""Фейковое ядро для тестов отправки с файлами (Ф10-4 и Ф11-3b, часть B): ручка байтов
`GET /internal/v1/outbox/{id}/file?lease_gen=N&idx=K` по контракту A↔B спек
`docs/architecture.md` и `docs/architecture.md` (ядро делает параллельный
кусок A — здесь только двойник с фиксированными ответами) плюс `report`/`lease`.
"""

from __future__ import annotations

import hashlib
import json

import httpx

from hub.connectors.base import CoreClient

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"picture-bytes"
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 300


def file_meta(data: bytes, *, kind: str = "photo", mime: str = "image/png", name: str = "shot.png") -> dict:
    return {
        "size": len(data),
        "mime": mime,
        "name": name,
        "kind": kind,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class FakeFileCore:
    """`files[outbox_id] = bytes` (один файл, `idx=0`) или список байтов (файл `idx` — элемент списка) — что
    отдаёт ручка байтов; `idx` вне списка — 404. `status` — код ответа вместо 200; `idx_status[idx]` — код
    ответа только для этого `idx`; `content_length` — подменить заголовок (строка) или убрать (`"omit"`);
    `transport_error` — исключение транспорта вместо ответа."""

    def __init__(self) -> None:
        self.files: dict[int, bytes | list[bytes]] = {}
        self.status = 200
        self.idx_status: dict[int, int] = {}
        self.content_length: str | None = None
        self.transport_error: Exception | None = None
        self.fetches: list[dict] = []
        self.reports: list[dict] = []
        self.leases: list[dict] = []

    def client(self) -> CoreClient:
        return CoreClient(secret="s", transport=httpx.MockTransport(self.handler))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/internal/v1")
        if path.startswith("/outbox/") and path.endswith("/file") and request.method == "GET":
            outbox_id = int(path.split("/")[2])
            self.fetches.append(
                {
                    "outbox_id": outbox_id,
                    "lease_gen": request.url.params.get("lease_gen"),
                    "idx": request.url.params.get("idx"),
                    "authorization": request.headers.get("Authorization"),
                }
            )
            idx = int(request.url.params.get("idx", "0"))
            if self.transport_error is not None:
                raise self.transport_error
            status = self.idx_status.get(idx, self.status)
            if status != 200:  # noqa: PLR2004
                return httpx.Response(status, json={"detail": "x"})
            stored = self.files.get(outbox_id)
            items = stored if isinstance(stored, list) else ([stored] if stored is not None else [])
            if not 0 <= idx < len(items):
                return httpx.Response(404, json={"detail": "no file"})
            data = items[idx]

            async def body():
                for i in range(0, len(data), 7):
                    yield data[i : i + 7]

            headers = {"Content-Type": "application/octet-stream"}
            if self.content_length != "omit":
                headers["Content-Length"] = self.content_length or str(len(data))
            return httpx.Response(200, headers=headers, content=body())
        if path == "/outbox/report":
            self.reports.append(json.loads(await request.aread()))
            return httpx.Response(200, json={"ok": True})
        if path == "/outbox/lease":
            out, self.leases = self.leases, []
            return httpx.Response(200, json=out)
        if path in ("/account/status", "/events"):
            return httpx.Response(200, json={"ok": True})
        if path == "/cursor":  # Ф42-1: курсор аккаунта (дыры догона Telegram) — пусто
            return httpx.Response(200, content=b"null", headers={"Content-Type": "application/json"})
        return httpx.Response(404, json={"detail": path})
