"""Ф75-1 (docs/architecture.md): модель агента владельца — выбор владельца в ядре.

- `GET /owner_channel/agent-settings` — кука и агент (`human`); бот и прочий bearer — 403;
- `PUT /owner_channel/agent-settings` — ТОЛЬКО кука; агент, бот, bearer сессии (`mcp:all`) — 403, ничего не пишется;
- `PUT /owner_channel/agent-models` — только агент; список моделей виден в GET; модель вне списка — не отказ;
- умолчание — роль `agent` с переопределением `engine.json`; 422 на негодную модель; аудит."""

import json

from hub.core import engine as engine_mod

from . import test_hub_sessions as base
from .test_f56_queue import _agent, _bot
from .test_hub_sessions import _audits, _h, _token

ui = base.ui
API = "/api/v1"
MODELS = [
    {"id": "deepseek/deepseek-flash", "name": "DeepSeek Flash", "images": True, "variants": ["low", "max"]},
    {"id": "alibaba/qwen-max", "name": "Qwen Max", "images": False, "variants": []},
]


def _opencode(db) -> None:
    """Ф78-2: «Модель ▾» агента — выбор текущего движка; эти тесты — про модели OpenCode."""
    db.write_sync(lambda conn: engine_mod._meta_set(conn, engine_mod.META_ENGINE, "opencode"))  # noqa: SLF001


async def _get(client, *, token=None, ui=None, expect=200):
    r = await (
        ui.get(f"{API}/owner_channel/agent-settings")
        if ui is not None
        else client.get(f"{API}/owner_channel/agent-settings", headers=_h(token))
    )
    assert r.status_code == expect, r.text
    return r.json()


async def test_get_by_cookie_and_agent_default(client, ui, db, app, tmp_path):
    agent, _ = _agent(db)
    _opencode(db)
    app.state.config_dir = tmp_path
    body = await _get(client, ui=ui)
    assert body == {
        "engine": "opencode",
        "model": None,
        "effort": None,
        "variant": None,
        "set_ts": None,
        "default": {"model": "deepseek/deepseek-flash", "effort": "max", "variant": "max"},
        "models": [],
        "models_ts": None,
        "projects": [],  # Ф76-3: замен модели по проектам нет
        # Ф83-1: действующий выбор агента и запасной.
        "effective": {"engine": "opencode", "model": "deepseek/deepseek-flash", "effort": "max"},
        "on_fallback": None,
        "fallback": None,
    }
    assert await _get(client, token=agent) == body
    (tmp_path / "engine.json").write_text(
        json.dumps({"opencode": {"roles": {"agent": {"model": "alibaba/qwen-max", "variant": "high"}}}}),
        encoding="utf-8",
    )
    default = (await _get(client, ui=ui))["default"]
    assert default == {"model": "alibaba/qwen-max", "effort": "high", "variant": "high"}


async def test_put_only_cookie(client, ui, db, clock):
    agent, _ = _agent(db)
    _opencode(db)
    bot, _ = _bot(db)
    other, _ = _token(db)  # bearer сессии (mcp:all)
    body = {"model": "alibaba/qwen-max", "variant": "high"}
    for token in (agent, bot, other):
        r = await client.put(f"{API}/owner_channel/agent-settings", json=body, headers=_h(token))
        assert r.status_code == 403, r.status_code
    assert (await _get(client, ui=ui))["model"] is None  # ничего не записалось
    for token in (bot, other):
        await _get(client, token=token, expect=403)

    r = await ui.put(f"{API}/owner_channel/agent-settings", body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["model"] == "alibaba/qwen-max" and saved["variant"] == "high"
    assert saved["set_ts"] == int(clock() * 1000)
    assert (await _get(client, token=agent))["model"] == "alibaba/qwen-max"  # агент видит выбор владельца
    assert _audits(db, "owner_agent_model") == [
        {"engine": "opencode", "model": "alibaba/qwen-max", "effort": "high"}
    ]

    # Ф78-2: модель и усилие независимы — `null` у модели снимает только модель.
    r = await ui.put(f"{API}/owner_channel/agent-settings", {"model": None, "effort": "max"})
    assert r.status_code == 200 and r.json()["model"] is None and r.json()["effort"] == "max"


async def test_put_validation(ui, db):
    _opencode(db)
    for bad in (
        {"model": "без-слеша"},
        {"model": "a/b c"},
        {"model": "x/y", "variant": "max max"},
        {"extra": 1},
    ):
        r = await ui.put(f"{API}/owner_channel/agent-settings", bad)
        assert r.status_code == 422, (bad, r.status_code)
    # Модель вне списка OpenCode — не отказ ядра (свободный ввод; служба сама уйдёт на запасную).
    r = await ui.put(f"{API}/owner_channel/agent-settings", {"model": "openrouter/some:model", "variant": None})
    assert r.status_code == 200 and r.json()["model"] == "openrouter/some:model"


async def test_models_list_only_agent(client, ui, db, clock):
    agent, _ = _agent(db)
    _opencode(db)
    bot, _ = _bot(db)
    other, _ = _token(db)
    r = await ui.put(f"{API}/owner_channel/agent-models", {"models": MODELS})
    assert r.status_code == 403
    for token in (bot, other):
        r = await client.put(f"{API}/owner_channel/agent-models", json={"models": MODELS}, headers=_h(token))
        assert r.status_code == 403
    junk = [
        *MODELS,
        {"id": "негодный"},
        {"id": "deepseek/deepseek-flash"},
        {"id": "a/b", "variants": ["ok", "x y"]},
    ]
    r = await client.put(f"{API}/owner_channel/agent-models", json={"models": junk}, headers=_h(agent))
    assert r.status_code == 200, r.text
    assert r.json() == {"engine": "opencode", "count": 3, "models_ts": int(clock() * 1000)}
    body = await _get(client, ui=ui)
    expected = [*MODELS, {"id": "a/b", "name": None, "images": None, "variants": ["ok"]}]
    assert body["models"] == [{**m, "efforts": m["variants"]} for m in expected]
    assert body["models_ts"] == int(clock() * 1000)
