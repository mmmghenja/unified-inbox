"""Ф91 Р3 и Р5: MCP `hub_image` — только профилю с `attach`; картинки из ответа ядра ложатся в
`<рабочая папка>/hub-files/` (без папки — `~/hub-files/project-N/`), ответ — пути и цена; готовый путь
принимает `hub_draft(files=[путь])`. Ядро — настоящее в процессе, DashScope — подделка (`MockTransport`)."""

import json
import os
import stat
import struct
import zlib

import httpx
import pytest
from mcp import Client

from hub.images import config as image_config
from hub.mcp_server import profile as profile_mod

from .conftest import insert_account, insert_channel, insert_project, make_token, text_of

KEY = "sk-fake-dashscope-mcp"
IMG_HOST = "dashscope-result-sgp.oss-ap-southeast-1.aliyuncs.com"


def _png(width: int = 40, height: int = 30) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(b"\x00" + b"\x10" * width * 3 for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _Fake:
    def __init__(self) -> None:
        self.status = 200
        self.body: dict | None = None
        self.gen: list[dict] = []
        self.image = _png()
        self.fail: set[str] = set()  # пути ссылок, что отвечают 500

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.read())
            self.gen.append(body)
            if self.body is not None:
                return httpx.Response(self.status, json=self.body)
            n = body["parameters"]["n"]
            content = [{"image": f"https://{IMG_HOST}/o/{i}.png"} for i in range(n)]
            return httpx.Response(200, json={"output": {"choices": [{"message": {"content": content}}]}})
        if request.url.path in self.fail:
            return httpx.Response(500, content=b"oops")
        return httpx.Response(200, content=self.image)


@pytest.fixture
def fake(public_app, config_dir):
    fake = _Fake()
    public_app.state.config_dir = config_dir
    public_app.state.image_transport = httpx.MockTransport(fake.handler)
    return fake


@pytest.fixture
def keyed(config_dir):
    image_config.save(config_dir, api_key=KEY)


def _profile(db, config_dir, *, scopes=("read", "draft", "attach")) -> None:
    token, token_id = make_token(db, scopes=scopes, name="mcp:all")
    profile_mod.write_profile(
        config_dir, "all", token=token, meta={"token_id": token_id, "projects": None, "scopes": list(scopes)}
    )


def _server(make_server, tmp_path):
    server, hub = make_server("all")
    hub.home = str(tmp_path / "home")
    os.makedirs(hub.home, exist_ok=True)
    return server


async def _call(server, **args):
    async with Client(server) as client:
        return await client.call_tool("hub_image", args)


async def test_tool_only_with_attach(db, config_dir, make_server):
    _profile(db, config_dir, scopes=("read", "draft"))
    server, _hub = make_server("all")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "hub_image" not in names


async def test_description_teaches_prompt(db, config_dir, make_server):
    _profile(db, config_dir)
    server, _hub = make_server("all")
    async with Client(server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "hub_image")
    assert "главный предмет" in tool.description and "БЕЗ текста в кадре" in tool.description
    assert "hub_draft(channel_id=…, files=[путь])" in tool.description


async def test_writes_into_workdir_and_draft_accepts_path(db, config_dir, make_server, tmp_path, fake, keyed):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    _profile(db, config_dir)
    server = _server(make_server, tmp_path)
    async with Client(server) as client:
        out = await client.call_tool(
            "hub_image", {"prompt": "кот", "n": 2, "project_id": project, "size": "1:1"}
        )
        text = text_of(out)
        assert not out.is_error, text
        assert "Готово: 2 шт., модель qwen-image-2.0-pro, цена $0.15" in text
        assert "проекту 58, всего 198" in text
        files = sorted((workdir / "hub-files").glob("image-*.png"))
        assert len(files) == 2
        for path in files:
            assert path.read_bytes() == fake.image
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert str(path) in text
        assert "40×30" in text
        assert (workdir / "hub-files" / ".gitignore").exists()
        assert fake.gen[0]["parameters"]["size"] == "2048*2048"

        # Р5: путь из hub_image уходит вложением в черновик тем же путём, что файлы сессий (Ф14-3).
        account = insert_account(db, source="telegram")
        channel = insert_channel(db, account, project_id=project, ext_id="c1", title="Канал")
        draft = await client.call_tool("hub_draft", {"channel_id": channel, "files": [str(files[0])]})
    assert not draft.is_error, text_of(draft)
    assert "с файлами (1)" in text_of(draft)


async def test_no_workdir_goes_to_home(db, config_dir, make_server, tmp_path, fake, keyed):
    project = insert_project(db, name="Личное")
    _profile(db, config_dir)
    out = await _call(_server(make_server, tmp_path), prompt="кот", project_id=project, fast=True)
    text = text_of(out)
    assert not out.is_error, text
    files = list((tmp_path / "home" / "hub-files" / f"project-{project}").glob("image-*.png"))
    assert len(files) == 1 and "в черновик его приложить нельзя" in text
    assert "qwen-image-2.0," in text and "$0.035" in text


async def test_no_key_text(db, config_dir, make_server, tmp_path, fake):
    _profile(db, config_dir)
    out = await _call(_server(make_server, tmp_path), prompt="кот")
    assert out.is_error
    assert "картинки не настроены" in text_of(out) and "hub auth alibaba" in text_of(out)


async def test_limit_text(db, config_dir, make_server, tmp_path, fake, keyed):
    image_config.config_path(config_dir).write_text(
        json.dumps({"image_daily": {"project": 1, "total": 9}}), encoding="utf-8"
    )
    project = insert_project(db, name="P")
    _profile(db, config_dir)
    server = _server(make_server, tmp_path)
    assert not (await _call(server, prompt="a", project_id=project)).is_error
    out = await _call(server, prompt="a", project_id=project)
    assert out.is_error and "предел картинок проекта: 1 в сутки, осталось 0" in text_of(out)


async def test_provider_refusal_text_without_key(db, config_dir, make_server, tmp_path, fake, keyed):
    fake.status, fake.body = 401, {"code": "InvalidApiKey", "message": f"bad {KEY}"}
    _profile(db, config_dir)
    out = await _call(_server(make_server, tmp_path), prompt="a")
    assert out.is_error and "ключ Alibaba не принят" in text_of(out) and KEY not in text_of(out)


async def test_bad_size_text(db, config_dir, make_server, tmp_path, fake, keyed):
    _profile(db, config_dir)
    out = await _call(_server(make_server, tmp_path), prompt="a", size="2:1")
    assert out.is_error and "size:" in text_of(out)
    assert not fake.gen


async def test_title_passed_and_drawn(db, config_dir, make_server, tmp_path, fake, keyed):
    project = insert_project(db, name="Личное")
    _profile(db, config_dir)
    server = _server(make_server, tmp_path)
    out = await _call(
        server, prompt="офис", project_id=project, title="Заголовок статьи", watermark="umbrella.example.com"
    )
    assert not out.is_error, text_of(out)
    (path,) = (tmp_path / "home" / "hub-files" / f"project-{project}").glob("image-*.png")
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG") and data != fake.image


async def test_watermark_passed(db, config_dir, make_server, tmp_path, fake, keyed):
    project = insert_project(db, name="Личное")
    _profile(db, config_dir)
    server = _server(make_server, tmp_path)
    out = await _call(
        server, prompt="офис", project_id=project, watermark="umbrella.example.com", watermark_style="diagonal",
        watermark_opacity=0.3,
    )  # fmt: skip
    assert not out.is_error, text_of(out)
    (path,) = (tmp_path / "home" / "hub-files" / f"project-{project}").glob("image-*.png")
    assert path.read_bytes() != fake.image
    bad = await _call(server, prompt="офис", watermark="x", watermark_style="spiral")
    assert bad.is_error and "watermark_style" in text_of(bad)


async def test_second_file_write_fails_first_kept(
    db, config_dir, make_server, tmp_path, fake, keyed, monkeypatch
):
    """Ревью 24.10: вторая картинка не записалась — первая остаётся, её путь в ответе (картинки оплачены)."""
    from hub import workdir_files

    real = workdir_files.Target.write_new
    calls = []

    def flaky(self, name, data):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("диск полон")
        return real(self, name, data)

    monkeypatch.setattr(workdir_files.Target, "write_new", flaky)
    project = insert_project(db, name="Личное")
    _profile(db, config_dir)
    out = await _call(_server(make_server, tmp_path), prompt="кот", n=2, project_id=project)
    text = text_of(out)
    assert not out.is_error, text
    (path,) = (tmp_path / "home" / "hub-files" / f"project-{project}").glob("image-*.png")
    assert str(path) in text and "Готово: 1 шт." in text
    assert "картинка 2 не записалась" in text and "диск полон" in text


async def test_missed_download_noted(db, config_dir, make_server, tmp_path, fake, keyed):
    """Одна из двух оплаченных не скачалась: сессия получает скачанную и строку «не скачались (оплачены)»."""
    project = insert_project(db, name="Личное")
    _profile(db, config_dir)
    fake.fail = {"/o/1.png"}
    out = await _call(_server(make_server, tmp_path), prompt="кот", n=2, project_id=project)
    text = text_of(out)
    assert not out.is_error, text
    assert "Готово: 1 шт." in text and "цена $0.15" in text
    assert "Не скачались (оплачены): не скачались 1 из 2" in text
