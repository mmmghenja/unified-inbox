"""Ф91 Р2: вызов DashScope (подделка) — тело запроса, скачивание PNG сразу, потолки, ошибки провайдера
понятным текстом без ключа, отказ скачивать с чужого хоста."""

import asyncio

import httpx
import pytest

from hub.images import config as image_config
from hub.images import dashscope

from .conftest import KEY, png_bytes


def _cfg(**kw) -> image_config.ImageConfig:
    return image_config.ImageConfig(api_key=KEY, **kw)


def _gen(fake, **kw):
    params = {"prompt": "кот на подоконнике", "width": 2688, "height": 1536, "n": 1}
    params.update(kw)
    return asyncio.run(dashscope.generate(_cfg(), transport=fake.transport, **params))


def test_generate_body_and_download(fake):
    fake.image = png_bytes(80, 45)
    result = _gen(fake, n=2, negative="текст, надписи", extend=True)
    assert result.ok and len(result.images) == 2
    assert {(i.width, i.height, i.detected) for i in result.images} == {(80, 45, "png")}
    post = fake.requests[0]
    assert post.url == httpx.URL(
        "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert post.headers["authorization"] == f"Bearer {KEY}"
    body = fake.gen_bodies[0]
    assert body["model"] == "qwen-image-2.0-pro"
    assert body["input"]["messages"][0]["content"] == [{"text": "кот на подоконнике"}]
    assert body["parameters"] == {
        "size": "2688*1536",
        "n": 2,
        "prompt_extend": True,
        "watermark": False,
        "negative_prompt": "текст, надписи",
    }
    gets = [r for r in fake.requests if r.method == "GET"]
    assert len(gets) == 2
    assert all("authorization" not in r.headers for r in gets)  # ключ не уходит на ссылку картинки


def test_base_url_from_config(fake):
    cfg = _cfg(base_url="https://ws-1.ap-southeast-1.maas.aliyuncs.com")
    result = asyncio.run(
        dashscope.generate(cfg, prompt="x", width=1024, height=1024, transport=fake.transport)
    )
    assert result.ok
    assert fake.requests[0].url.host == "ws-1.ap-southeast-1.maas.aliyuncs.com"


@pytest.mark.parametrize(
    ("status", "body", "code", "needle"),
    [
        (401, {"code": "InvalidApiKey", "message": "Invalid API-key provided."}, "key", "не принят"),
        (429, {"code": "Throttling.RateQuota", "message": "Requests rate limit exceeded"}, "rate", "предел"),
        (400, {"code": "Arrearage", "message": "Access denied, account in arrears"}, "balance", "нет денег"),
        (400, {"code": "DataInspectionFailed", "message": "Input data may contain inappropriate content."},
         "rejected", "DataInspectionFailed"),
        (500, {"code": "InternalError", "message": "oops"}, "upstream", "HTTP 500"),
    ],
)  # fmt: skip
def test_provider_errors_readable_without_key(fake, status, body, code, needle):
    fake.gen_status = status
    fake.gen_body = {**body, "message": body["message"] + f" {KEY}"} if code == "key" else body
    result = _gen(fake)
    assert not result.ok and result.code == code
    assert needle in result.reason
    assert KEY not in result.reason
    assert not [r for r in fake.requests if r.method == "GET"]


def test_timeout(fake):
    fake.raise_on_gen = httpx.ReadTimeout("долго")
    result = _gen(fake)
    assert result.code == "timeout" and "не ответил" in result.reason


def test_connect_error(fake):
    fake.raise_on_gen = httpx.ConnectError("нет")
    result = _gen(fake)
    assert result.code == "upstream" and "нет связи" in result.reason


def test_slow_provider_hits_overall_timeout():
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    result = asyncio.run(
        dashscope.generate(
            _cfg(), prompt="x", width=1024, height=1024, transport=httpx.MockTransport(handler), timeout_s=0.2
        )
    )
    assert result.code == "timeout"


def test_foreign_image_host_refused(fake):
    fake.image_host = "169.254.169.254"
    result = _gen(fake)
    assert not result.ok and "чужого адреса" in result.reason
    assert not [r for r in fake.requests if r.method == "GET"]


def test_http_image_link_refused(fake):
    fake.gen_body = {
        "output": {"choices": [{"message": {"content": [{"image": "http://x.aliyuncs.com/a.png"}]}}]}
    }
    result = _gen(fake)
    assert not result.ok and "чужого адреса" in result.reason


def test_image_too_big(fake, monkeypatch):
    monkeypatch.setattr(dashscope, "MAX_IMAGE_BYTES", 20)
    result = _gen(fake)
    assert result.code == "too_big"


def test_not_an_image(fake):
    fake.image = b"<html>expired</html>"
    result = _gen(fake)
    assert not result.ok and "не картинка" in result.reason


def test_image_link_expired(fake):
    fake.image_status = 403
    result = _gen(fake)
    assert not result.ok and "HTTP 403" in result.reason


def test_no_images_in_answer(fake):
    fake.gen_body = {"output": {"choices": []}}
    assert "без картинок" in _gen(fake).reason


def test_response_too_big(fake, monkeypatch):
    monkeypatch.setattr(dashscope, "MAX_RESPONSE_BYTES", 10)
    assert "слишком большой" in _gen(fake).reason


def test_url_env_override_allows_its_host(fake, monkeypatch):
    monkeypatch.setenv("HUB_DASHSCOPE_URL", "http://127.0.0.1:18099")
    fake.image_host = "unused"
    fake.gen_body = {
        "output": {"choices": [{"message": {"content": [{"image": "http://127.0.0.1:18099/i/0.png"}]}}]}
    }
    result = _gen(fake)
    assert result.ok
    assert fake.requests[0].url.host == "127.0.0.1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, (2688, 1536)), ("16:9", (2688, 1536)), ("1:1", (2048, 2048)), ("9:16", (1536, 2688)),
     ("4:3", (2304, 1728)), ("1024*768", (1024, 768)), ("1280x720", (1280, 720))],
)  # fmt: skip
def test_parse_size_ok(value, expected):
    assert dashscope.parse_size(value) == expected


@pytest.mark.parametrize("value", ["2:1", "100*100", "4096*4096", "abc", "300*5000"])
def test_parse_size_bad(value):
    with pytest.raises(dashscope.BadParams):
        dashscope.parse_size(value)


def test_model_and_cost():
    assert dashscope.model_of(None) == "qwen-image-2.0-pro"
    assert dashscope.model_of(None, fast=True) == "qwen-image-2.0"
    with pytest.raises(dashscope.BadParams):
        dashscope.model_of("wanx")
    assert dashscope.cost_usd("qwen-image-2.0-pro", 2) == 0.15


def test_config_repr_hides_key():
    assert KEY not in repr(_cfg())


def test_config_load_settings(config_dir):
    assert image_config.load(config_dir) is None
    image_config.save(config_dir, api_key=KEY)
    image_config.config_path(config_dir).write_text(
        '{"base_url": "http://evil", "image_daily": {"project": 5, "total": true}, "proxy": "none"}',
        encoding="utf-8",
    )
    cfg = image_config.load(config_dir)
    assert cfg.base_url == image_config.DEFAULT_BASE_URL  # не https — адрес по умолчанию
    assert (cfg.daily_project, cfg.daily_total, cfg.proxy) == (5, 200, None)
