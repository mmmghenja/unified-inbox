"""Общее для тестов картинок (Ф91): каталог конфига во временном каталоге, подделка DashScope на
`httpx.MockTransport` (настоящей сети нет; живой вызов Alibaba в тестах не делается)."""

import json
import struct
import zlib

import httpx
import pytest

KEY = "sk-fake-dashscope-0123456789"
API_HOST = "dashscope-intl.aliyuncs.com"
IMG_HOST = "dashscope-result-sgp.oss-ap-southeast-1.aliyuncs.com"


def png_bytes(width: int = 64, height: int = 36) -> bytes:
    """Настоящий PNG (IHDR с размерами), маленький."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(b"\x00" + b"\x00" * width * 3 for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeDashScope:
    """Подделка DashScope: генерация отвечает ссылками, ссылки отдают PNG. Помнит запросы."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.gen_bodies: list[dict] = []
        self.gen_status = 200
        self.gen_body: object | None = None  # None — обычный ответ со ссылками по `n`
        self.image = png_bytes()
        self.image_status = 200
        self.image_host = IMG_HOST
        self.raise_on_gen: Exception | None = None
        self.image_fail: set[int] = set()  # номера ссылок (`/out/<i>.png`), что отвечают 500
        self.gen_advance_s = 0.0  # сколько «идёт» генерация по часам `clock` (тест окна)
        self.clock = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            body = json.loads(request.read())
            self.gen_bodies.append(body)
            if self.clock is not None:
                self.clock.value += self.gen_advance_s
            if self.raise_on_gen is not None:
                raise self.raise_on_gen
            if self.gen_body is not None:
                if isinstance(self.gen_body, bytes):
                    return httpx.Response(self.gen_status, content=self.gen_body)
                return httpx.Response(self.gen_status, json=self.gen_body)
            n = int(body.get("parameters", {}).get("n") or 1)
            content = [
                {"image": f"https://{self.image_host}/out/{i}.png?Expires=1&Signature=x"} for i in range(n)
            ]
            return httpx.Response(
                self.gen_status,
                json={
                    "output": {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                                                                                  "content": content}}]},
                    "usage": {"image_count": n},
                    "request_id": "req-1",
                },
            )  # fmt: skip
        if any(request.url.path == f"/out/{i}.png" for i in self.image_fail):
            return httpx.Response(500, content=b"oops")
        return httpx.Response(self.image_status, content=self.image)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _private_config_dir(tmp_path, monkeypatch):
    """Ни один тест не трогает боевой каталог конфига; адрес API — настоящий по умолчанию (транспорт
    поддельный)."""
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("HUB_DASHSCOPE_URL", raising=False)


@pytest.fixture
def config_dir(tmp_path):
    path = tmp_path / "config"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


@pytest.fixture
def fake():
    return FakeDashScope()
