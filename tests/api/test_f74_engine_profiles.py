"""Ф74-1 (docs/architecture.md): `/status` отдаёт профили движков сессий хаба; поле `engine`
ручек сессий проверяется по профилю — четыре движка (Ф74-5: `codex` больше не заготовка), неизвестное —
422. Помощники и
«мир» — из test_hub_sessions.py."""

from hub.core import agent_requests as ar_mod

from . import test_hub_sessions as base
from .test_hub_sessions import _mk

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def test_status_has_session_engine_profiles(ui):
    r = await ui.get(f"{API}/status")
    assert r.status_code == 200, r.text
    rows = r.json()["session_engines"]
    assert [(p["name"], p["label"], p["available"]) for p in rows] == [
        ("claude", "Claude Code", True),
        ("opencode", "OpenCode", True),
        ("cursor", "Cursor", True),
        ("codex", "Codex", True),
    ]
    claude = rows[0]
    assert claude["short"] == "Claude"
    assert claude["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert claude["model_hints"] == ["sonnet", "opus", "haiku"]
    assert (claude["mid_turn"], claude["compacts_self"], claude["context_known"]) == (True, False, True)


async def test_engine_field_checked_by_profile(ui, world):
    w = world
    for engine in ("claude", "opencode", "cursor", "codex"):
        row = await _mk(ui, w, engine=engine, role=f"тестер-{engine}")
        assert row["engine"] == engine
    await _mk(ui, w, engine="nope", expect=422)
    await _mk(ui, w, engine=1, expect=422)
    old = (await _mk(ui, w, role="передача"))["id"]
    r = await ui.post(f"{API}/sessions/{old}/handoff", {"engine": "nope"})
    assert r.status_code == 422, r.text
    r = await ui.post(f"{API}/sessions/{old}/handoff", {"engine": "codex"})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["engine"] == "codex"


async def test_agent_request_approve_engine_checked(ui):
    r = await ui.post(f"{API}/agent-requests/1/approve", {"engine": "nope"})
    assert r.status_code == 422, r.text
    assert "codex" in ar_mod.held_mod.ENGINES


async def test_host_reports_codex_models_into_status_profile(client, ui, world):
    """Ф74-5б: хост сообщает `model/list` Codex — профиль `codex` в `/status` получает поле `models`
    (контракт `docs/architecture.md`); у других движков поля нет; мусор отбрасывается."""
    w = world
    r = await ui.get(f"{API}/status")
    # до сообщения хоста — поля нет (у Cursor — семейства с усилиями по семейству, слово владельца)
    assert all("models" not in p for p in r.json()["session_engines"] if p["name"] != "cursor")
    cursor = next(p for p in r.json()["session_engines"] if p["name"] == "cursor")
    efforts = {m["id"]: m["efforts"] for m in cursor["models"]}
    assert efforts["claude-opus-5-5"] == ["low", "medium", "high", "xhigh", "max"]
    assert efforts["grok-4.7"] == ["high", "xhigh"]
    models = [
        {"id": "gpt-6.1-sol", "efforts": ["low", "medium", "ultra"], "default_effort": "low"},
        {"id": "gpt-6-luna", "efforts": ["low", "max", "low"], "default_effort": "ultra"},
        {"id": "плохое имя", "efforts": ["low"], "default_effort": "low"},
        {"id": "gpt-6.1-sol", "efforts": ["high"], "default_effort": "high"},
        {"id": None},
    ]
    got = await base._host(client, w, "engine-models", {"engine": "codex", "models": models})
    assert got == {"engine": "codex", "models": 2}
    rows = {p["name"]: p for p in (await ui.get(f"{API}/status")).json()["session_engines"]}
    assert rows["codex"]["models"] == [
        {"id": "gpt-6.1-sol", "efforts": ["low", "medium", "ultra"], "default_effort": "low"},
        {"id": "gpt-6-luna", "efforts": ["low", "max"], "default_effort": None},
    ]
    assert "models" not in rows["claude"] and "ultra" in rows["codex"]["efforts"]
    await base._host(client, w, "engine-models", {"engine": "nope", "models": []}, expect=422)
    r = await ui.post(f"{API}/work/host/engine-models", {"engine": "codex", "models": []})
    assert r.status_code in (401, 403), r.text  # только хост
    # Пустой список убирает поле — интерфейс берёт подсказки профиля.
    await base._host(client, w, "engine-models", {"engine": "codex", "models": []})
    rows = {p["name"]: p for p in (await ui.get(f"{API}/status")).json()["session_engines"]}
    assert "models" not in rows["codex"]



def test_opencode_profile_takes_agent_model_list():
    """24.10: у OpenCode хост своего списка не шлёт — профиль `opencode` в `/status` берёт список, который
    служба агента прислала со своего OpenCode (варианты модели — «Усилие» сессии); список хоста главнее."""
    import sqlite3

    from hub.core import engine_models as em
    from hub.core import owner_agent_model as am

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    assert "opencode" not in em.load_for_ui(conn)
    items = [
        {"id": "deepseek/deepseek-flash", "variants": ["high", "max"]},
        {"id": "anthropic/claude-sonnet-5-5", "variants": []},
        {"id": "плохое имя"},
    ]
    am.set_models(conn, items, now_ms=1)
    assert em.load_for_ui(conn)["opencode"] == [
        {"id": "deepseek/deepseek-flash", "efforts": ["high", "max"], "default_effort": None},
        {"id": "anthropic/claude-sonnet-5-5", "efforts": [], "default_effort": None},
    ]
    em.store(conn, "opencode", [{"id": "x/y", "efforts": ["low"]}], now_ms=2)
    assert [m["id"] for m in em.load_for_ui(conn)["opencode"]] == ["x/y"]  # список хоста главнее
