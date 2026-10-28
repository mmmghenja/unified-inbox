"""Ф75-1 (A.2): `OcClient.model_list` — модели сервера OpenCode из `GET /config/providers`, без ключей
провайдеров: только id «провайдер/модель», имя, приём картинок и имена уровней."""

import httpx

from hub.engine.oc_server import OcClient

PROVIDERS = {
    "providers": [
        {
            "id": "deepseek",
            "key": "sk-СЕКРЕТ",
            "models": {
                "deepseek-flash": {
                    "name": "DeepSeek V4.1 Flash",
                    "capabilities": {"input": {"image": True}},
                    "variants": {"low": {}, "high": {}, "max": {}},
                    "options": {"apiKey": "sk-СЕКРЕТ"},
                },
                "deepseek-v4-pro": {"name": "DeepSeek V4 Pro", "capabilities": {"input": {"image": False}}},
            },
        },
        {"id": "broken", "models": "не словарь"},
    ],
    "default": {"deepseek": "deepseek-flash"},
}


async def test_model_list_shape_without_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/config/providers"
        return httpx.Response(200, json=PROVIDERS)

    client = OcClient("http://oc", ("u", "p"), "/w", transport=httpx.MockTransport(handler))
    items = await client.model_list()
    await client.aclose()
    flash = {
        "id": "deepseek/deepseek-flash",
        "name": "DeepSeek V4.1 Flash",
        "images": True,
        "variants": ["low", "high", "max"],
    }
    pro = {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek V4 Pro", "images": False, "variants": []}
    assert items == [flash, pro]
    assert "СЕКРЕТ" not in repr(items)
