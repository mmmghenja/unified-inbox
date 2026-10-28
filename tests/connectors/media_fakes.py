"""Фейковое ядро для тестов загрузчика медиа (Ф8-5, часть 5b): ручки `/internal/v1/media/*` по контракту
A2.4 + П3/П4 спеки `docs/architecture.md` (ядро делает параллельный кусок 5a — здесь только
двойник), плюс минимум outbox/heads для теста «загрузчик не мешает отправке и сверке» (T13).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from hub.connectors.base import CoreClient

MIB = 1024 * 1024


def media_job(
    media_id: int = 1,
    *,
    lease_gen: int = 1,
    channel_ext_id: str = "100",
    ext_id: str = "5",
    thread_ext_id: str | None = None,
    idx: int = 0,
    kind: str = "photo",
    size: int | None = None,
    max_bytes: int = 20 * MIB,
    src_id: str | None = None,
    ref: dict | None = None,
) -> dict:
    return {
        "media_id": media_id,
        "lease_gen": lease_gen,
        "max_bytes": max_bytes,
        "message": {"channel_ext_id": channel_ext_id, "ext_id": ext_id, "thread_ext_id": thread_ext_id},
        "idx": idx,
        "kind": kind,
        "mime": None,
        "size": size,
        "name": None,
        "src_id": src_id,
        "ref": ref,
    }


class FakeMediaCore:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.puts: list[dict] = []
        self.reports: list[dict] = []
        self.put_status = 200
        self.lease_calls: list[dict] = []
        self.outbox_leases: list[dict] = []
        self.outbox_reports: list[dict] = []
        self.heads: list[dict] = []
        self.paths: list[str] = []

    def client(self) -> CoreClient:
        return CoreClient(secret="s", transport=httpx.MockTransport(self.handler))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/internal/v1")
        self.paths.append(f"{request.method} {path}")
        if path == "/media/wait":
            if not self.jobs:
                await asyncio.sleep(0.01)
            return httpx.Response(200, json={"ready": bool(self.jobs)})
        if path == "/media/lease":
            body = json.loads(await request.aread())
            self.lease_calls.append(body)
            out, self.jobs = self.jobs[: body["limit"]], self.jobs[body["limit"] :]
            return httpx.Response(200, json=out)
        if path.startswith("/media/") and path.endswith("/content") and request.method == "PUT":
            media_id = int(path.split("/")[2])
            data = await request.aread()
            self.puts.append(
                {
                    "media_id": media_id,
                    "lease_gen": int(request.url.params["lease_gen"]),
                    "content_length": request.headers.get("Content-Length"),
                    "transfer_encoding": request.headers.get("Transfer-Encoding"),
                    "body": data,
                }
            )
            if self.put_status != 200:  # noqa: PLR2004
                return httpx.Response(self.put_status, json={"detail": "x"})
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/media/") and path.endswith("/report"):
            media_id = int(path.split("/")[2])
            body = json.loads(await request.aread())
            self.reports.append({"media_id": media_id, **body})
            return httpx.Response(200, json={"ok": True})
        if path == "/outbox/wait":
            return httpx.Response(200, json={"ready": bool(self.outbox_leases)})
        if path == "/outbox/lease":
            out, self.outbox_leases = self.outbox_leases, []
            return httpx.Response(200, json=out)
        if path == "/outbox/report":
            self.outbox_reports.append(json.loads(await request.aread()))
            return httpx.Response(200, json={"ok": True})
        if path == "/channels/heads":
            return httpx.Response(200, json=self.heads)
        if path in ("/account/status", "/account/activity", "/events"):
            return httpx.Response(200, json={"ok": True})
        if path == "/cursor":  # Ф42-1: курсор аккаунта (дыры догона Telegram) — пусто
            return httpx.Response(200, content=b"null", headers={"Content-Type": "application/json"})
        return httpx.Response(404, json={"detail": path})
