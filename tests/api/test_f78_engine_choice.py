"""Ф78-2 (docs/architecture.md, 3.1–3.3, П3, П5): переключатель «движок и модель».

- четыре движка; выбор модели и усилия помнится ПО КАЖДОМУ движку (`engine_choice`);
- недоступный движок (нет исполняемого файла) — 409 с причиной;
- проверка модели по списку движка (claude — профиль, cursor — семейства, codex — живой список хоста; нет
  списка — правило имени; opencode — «провайдер/модель», список агента — подсказка);
- старые ключи (`owner_agent_model`, `claude_model` в `facts.json`) читаются, пока нет записи;
- `/status` несёт модель и усилие; «Модель ▾» агента (`/owner_channel/agent-settings`) — та же запись;
- замена по проекту пишется с текущим движком, `projects` — список с `engine`/`effort` (П5).

Исполняемые файлы движков — подставные в домашнем каталоге теста (`engine._home`)."""

import json

import pytest

from hub.core import engine as engine_mod
from hub.core import engine_models as engine_models_mod

from . import test_hub_sessions as base
from .test_f56_queue import _agent
from .test_hub_sessions import _audits, _h

ui = base.ui
world = base.world
procs = base.procs
API = "/api/v1"


@pytest.fixture
def home(tmp_path, monkeypatch, app):
    """Домашний каталог с подставными `codex` и `cursor-agent`; каталог конфига — пустой временный."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("codex", "cursor-agent"):
        target = bin_dir / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
    monkeypatch.setattr(engine_mod, "_home", lambda: home)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / engine_mod.SETUP_FILE).write_text("{}", encoding="utf-8")  # OpenCode «настроен» (П13)
    app.state.config_dir = config_dir
    return home


async def _engine(ui) -> dict:
    r = await ui.get(f"{API}/engine")
    assert r.status_code == 200, r.text
    return r.json()


def _row(body: dict, name: str) -> dict:
    return next(e for e in body["engines"] if e["name"] == name)


async def test_choice_remembered_per_engine(ui, db, home):
    r = await ui.put(f"{API}/engine", {"engine": "codex", "model": "gpt-6.1-sol", "effort": "high"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["engine"], body["model"], body["effort"]) == ("codex", "gpt-6.1-sol", "high")
    assert body["old_engine"] == "claude"
    # Claude с моделью и усилием, потом обратно на Codex без модели — выбор Codex вернулся.
    r = await ui.put(f"{API}/engine", {"engine": "claude", "model": "opus", "effort": "low"})
    assert (r.json()["model"], r.json()["effort"]) == ("opus", "low")
    r = await ui.put(f"{API}/engine", {"engine": "codex"})
    assert (r.json()["model"], r.json()["effort"]) == ("gpt-6.1-sol", "high")
    status = await _engine(ui)
    assert _row(status, "claude")["model"] == "opus" and _row(status, "claude")["effort"] == "low"
    # `null` — умолчание движка (модель не бывает пустой в ответе).
    r = await ui.put(f"{API}/engine", {"engine": "codex", "model": None, "effort": None})
    assert (r.json()["model"], r.json()["effort"]) == (
        engine_mod.CODEX_DEFAULT_MODEL,
        engine_mod.CODEX_DEFAULT_EFFORT,
    )
    assert _row(r.json(), "codex")["model"] is None
    details = _audits(db, engine_mod.AUDIT_ACTION)
    assert details[0] == {"old": "claude", "new": "codex", "model": "gpt-6.1-sol", "effort": "high"}
    assert len(details) == 4  # повтор без изменений аудита не пишет
    r = await ui.put(f"{API}/engine", {"engine": "codex", "model": None})
    assert len(_audits(db, engine_mod.AUDIT_ACTION)) == 4


async def test_unavailable_engine_409(ui, db, home):
    (home / ".local" / "bin" / "cursor-agent").unlink()
    r = await ui.put(f"{API}/engine", {"engine": "cursor"})
    assert r.status_code == 409 and "cursor-agent не найден" in r.json()["detail"]
    row = _row(await _engine(ui), "cursor")
    assert row["available"] is False and "cursor-agent не найден" in row["reason"]
    assert _row(await _engine(ui), "claude")["available"] is True
    with db.reader() as conn:
        assert engine_mod.get_engine(conn) == "claude"


async def test_model_and_effort_checked_by_engine(ui, db, home):
    bad = [
        {"engine": "claude", "model": "gpt-6-luna"},  # не из профиля Claude
        {"engine": "claude", "effort": "ultra"},
        {"engine": "cursor", "model": "grok-4.7", "effort": "low"},  # П11: не ниже high
        {"engine": "cursor", "model": "grok-4.7-fast"},
        {"engine": "opencode", "model": "без-слеша"},
        {"engine": "codex", "model": "два слова"},
    ]
    for body in bad:
        r = await ui.put(f"{API}/engine", body)
        assert r.status_code == 422, (body, r.status_code, r.text)
    # Codex без живого списка — любая модель по правилу имени; со списком — только из него.
    r = await ui.put(f"{API}/engine", {"engine": "codex", "model": "gpt-9-x", "effort": "ultra"})
    assert r.status_code == 200, r.text
    models = [{"id": "gpt-6-luna", "efforts": ["low", "high"], "default_effort": "low"}]
    db.write_sync(lambda conn: engine_models_mod.store(conn, "codex", models, now_ms=1))
    assert (await ui.put(f"{API}/engine", {"engine": "codex", "model": "gpt-9-x"})).status_code == 422
    r = await ui.put(f"{API}/engine", {"engine": "codex", "model": "gpt-6-luna", "effort": "ultra"})
    assert r.status_code == 422  # усилие — из списка модели
    r = await ui.put(f"{API}/engine", {"engine": "codex", "model": "gpt-6-luna", "effort": "high"})
    assert r.status_code == 200
    codex = _row(r.json(), "codex")
    assert codex["models"] == [{"id": "gpt-6-luna", "name": None, "images": True, "efforts": ["low", "high"]}]
    cursor = _row(r.json(), "cursor")
    assert [m["id"] for m in cursor["models"]] == [
        "grok-4.7", "cursor-grok-4.6", "claude-sonnet-5-5", "claude-opus-5-5",
    ]
    assert cursor["default_model"] == "grok-4.7" and cursor["default_effort"] == "high"


async def test_status_carries_model_and_effort(client, ui, db, home):
    reader, _ = base._token(db, name="mcp:read", kind="background", scopes=("read",))
    r = await client.get(f"{API}/status", headers=_h(reader))
    assert r.status_code == 200, r.text
    assert (r.json()["engine"], r.json()["engine_model"], r.json()["engine_effort"]) == (
        "claude",
        "sonnet",
        None,
    )
    await ui.put(f"{API}/engine", {"engine": "codex", "effort": "high"})
    r = await client.get(f"{API}/status", headers=_h(reader))
    assert (r.json()["engine"], r.json()["engine_model"], r.json()["engine_effort"]) == (
        "codex",
        "gpt-6.1-sol",
        "high",
    )


async def test_agent_settings_is_choice_of_current_engine(client, ui, db, home, world):
    agent, _ = _agent(db)
    r = await ui.put(f"{API}/owner_channel/agent-settings", {"model": "opus", "effort": "low"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["engine"], body["model"], body["effort"], body["variant"]) == (
        "claude",
        "opus",
        "low",
        "low",
    )
    assert [m["id"] for m in body["models"]] == ["sonnet", "opus"]
    assert body["default"] == {"model": "sonnet", "effort": None, "variant": None}
    # Та же запись, что у переключателя.
    status = await _engine(ui)
    assert (status["model"], status["effort"]) == ("opus", "low")
    # Модель без слэша — норма для claude; чужая — 422.
    assert (await ui.put(f"{API}/owner_channel/agent-settings", {"model": "deepseek/x"})).status_code == 422
    # Замена по проекту — с текущим движком; `projects` — список (П5).
    url = f"{API}/owner_channel/agent-settings/projects/{world['p3']}"
    r = await ui.put(url, {"model": "opus", "effort": "high"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "project_id": world["p3"],
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "variant": "high",
        "set_ts": r.json()["set_ts"],
    }
    # Старая замена без движка читается как opencode.
    old = json.dumps({"agent_model": {"model": "alibaba/qwen-max", "variant": "max", "ts": 1}})
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?", (old, world["p4"])
        )
    )
    got = (await client.get(f"{API}/owner_channel/agent-settings", headers=_h(agent))).json()
    assert isinstance(got["projects"], list)
    by_id = {p["project_id"]: p for p in got["projects"]}
    assert (by_id[world["p3"]]["engine"], by_id[world["p3"]]["effort"]) == ("claude", "high")
    assert (by_id[world["p4"]]["engine"], by_id[world["p4"]]["effort"]) == ("opencode", "max")
    assert by_id[world["p4"]]["variant"] == "max"
    assert _audits(db, "owner_agent_model")[0] == {"engine": "claude", "model": "opus", "effort": "low"}


async def test_agent_models_per_engine(client, ui, db, home):
    agent, _ = _agent(db)
    items = [{"id": "gpt-6-luna", "name": "Luna", "images": True, "efforts": ["low", "high"]}]
    r = await client.put(
        f"{API}/owner_channel/agent-models", json={"engine": "codex", "models": items}, headers=_h(agent)
    )
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "codex" and r.json()["count"] == 1
    r = await client.put(
        f"{API}/owner_channel/agent-models", json={"engine": "gemini", "models": []}, headers=_h(agent)
    )
    assert r.status_code == 422
    await ui.put(f"{API}/engine", {"engine": "codex"})
    body = (await client.get(f"{API}/owner_channel/agent-settings", headers=_h(agent))).json()
    assert body["engine"] == "codex"
    assert body["models"] == [
        {
            "id": "gpt-6-luna",
            "name": "Luna",
            "images": True,
            "efforts": ["low", "high"],
            "variants": ["low", "high"],
        }
    ]
    # OpenCode-список не тронут (прежний ключ).
    with db.reader() as conn:
        assert engine_mod.models_of(conn, "opencode") == []


async def test_claude_below_sonnet_refused(ui, home, world):
    """Владелец 22.10: Claude у агента и фактов — минимум Sonnet; haiku переключатель не принимает."""
    r = await ui.put(f"{API}/engine", {"engine": "claude", "model": "haiku"})
    assert r.status_code == 422, r.text


def test_legacy_haiku_is_not_a_choice(tmp_path):
    """П3 + минимум Sonnet: старое `claude_model: haiku` фактов не переносится — остаётся умолчание."""
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "facts.json").write_text(json.dumps({"claude_model": "haiku"}), "utf-8")
    assert engine_mod._legacy_claude_model(tmp_path) is None  # noqa: SLF001


# --- Ф78-2, раздел 12: свой выбор службы фактов ----------------------------------------------------------

FACTS_AUDIT = engine_mod.FACTS_AUDIT_ACTION


async def test_facts_follow_agent_without_record(ui, db, home):
    body = await _engine(ui)
    assert body["facts"] == {
        "engine": "claude",
        "model": "sonnet",
        "effort": None,
        "follows_agent": True,
        "ts": None,
        # Ф83-1 (П10): запасной и действующий выбор фактов.
        "effective": {"engine": "claude", "model": "sonnet", "effort": None},
        "on_fallback": None,
        "fallback": None,
    }
    await ui.put(f"{API}/engine", {"engine": "codex", "model": "gpt-6-luna", "effort": "high"})
    facts = (await _engine(ui))["facts"]
    assert (facts["engine"], facts["model"], facts["effort"], facts["follows_agent"]) == (
        "codex",
        "gpt-6-luna",
        "high",
        True,
    )
    assert _audits(db, FACTS_AUDIT) == []


async def test_facts_own_choice_keep_and_reset(ui, db, home):
    r = await ui.put(f"{API}/engine/facts", {"engine": "claude", "model": "opus", "effort": "low"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["facts"]["engine"] == "claude" and body["facts"]["follows_agent"] is False
    assert (body["facts"]["model"], body["facts"]["effort"]) == ("opus", "low")
    assert isinstance(body["facts"]["ts"], int)
    # Агент свой выбор не потерял, и факты за ним больше не ходят.
    assert (body["engine"], body["model"]) == ("claude", "sonnet")
    r = await ui.put(f"{API}/engine", {"engine": "codex"})
    assert r.json()["engine"] == "codex" and r.json()["facts"]["engine"] == "claude"
    # Поле не передано — прежнее значение того же движка; повтор без изменений — без аудита.
    r = await ui.put(f"{API}/engine/facts", {"engine": "claude"})
    assert (r.json()["facts"]["model"], r.json()["facts"]["effort"]) == ("opus", "low")
    r = await ui.put(f"{API}/engine/facts", {"engine": "claude", "effort": None})
    assert (r.json()["facts"]["model"], r.json()["facts"]["effort"]) == ("opus", None)
    # Другой движок без модели — умолчание: у OpenCode — роль `facts`.
    r = await ui.put(f"{API}/engine/facts", {"engine": "opencode"})
    assert r.status_code == 200, r.text
    assert (r.json()["facts"]["model"], r.json()["facts"]["effort"]) == ("deepseek/deepseek-flash", "max")
    with db.reader() as conn:
        assert engine_mod.facts_stored(conn)["model"] is None
    # `null` — снять запись: факты снова за агентом (codex).
    r = await ui.put(f"{API}/engine/facts", {"engine": None})
    assert r.status_code == 200, r.text
    assert r.json()["facts"]["follows_agent"] is True and r.json()["facts"]["engine"] == "codex"
    assert (await ui.put(f"{API}/engine/facts", {"engine": None})).status_code == 200  # повтор — без аудита
    assert _audits(db, FACTS_AUDIT) == [
        {"old": None, "new": "claude", "model": "opus", "effort": "low"},
        {"old": "claude", "new": "claude", "model": "opus", "effort": None},
        {"old": "claude", "new": "opencode", "model": None, "effort": None},
        {"old": "opencode", "new": None, "model": None, "effort": None},
    ]
    with db.reader() as conn:
        assert engine_mod.get_engine(conn) == "codex"


async def test_facts_fastest_model_and_haiku_allowed(ui, db, home):
    """Владелец 22.10: фактам по умолчанию — самая быстрая модель, у Claude можно `haiku` (агенту — нет)."""
    body = await _engine(ui)
    by_name = {e["name"]: e for e in body["engines"]}
    assert by_name["claude"]["facts_default_model"] == "haiku"
    assert by_name["codex"]["facts_default_model"] == "gpt-6-luna"
    assert "haiku" in [m["id"] for m in by_name["claude"]["facts_models"]]
    assert "haiku" not in [m["id"] for m in by_name["claude"]["models"]]
    r = await ui.put(f"{API}/engine/facts", {"engine": "claude"})
    assert r.status_code == 200 and r.json()["facts"]["model"] == "haiku"
    r = await ui.put(f"{API}/engine/facts", {"engine": "claude", "model": "haiku"})
    assert r.status_code == 200 and r.json()["facts"]["model"] == "haiku"
    assert (await ui.put(f"{API}/engine", {"engine": "claude", "model": "haiku"})).status_code == 422


async def test_facts_choice_checked_422_and_409(ui, db, home):
    bad = [
        {"engine": "claude", "effort": "ultra"},
        {"engine": "cursor", "model": "grok-4.7", "effort": "low"},
        {"engine": "opencode", "model": "без-слеша"},
        {"engine": "gemini"},
        {"model": "opus"},  # движок обязателен (снять запись — явный null)
        {"engine": "claude", "x": 1},
    ]
    for body in bad:
        r = await ui.put(f"{API}/engine/facts", body)
        assert r.status_code == 422, (body, r.status_code, r.text)
    (home / ".local" / "bin" / "cursor-agent").unlink()
    r = await ui.put(f"{API}/engine/facts", {"engine": "cursor"})
    assert r.status_code == 409 and "cursor-agent не найден" in r.json()["detail"]
    assert (await _engine(ui))["facts"]["follows_agent"] is True
    assert _audits(db, FACTS_AUDIT) == []


async def test_facts_put_bearer_refused(client, ui, db, home, world):
    for token in (world["tok"], world["host"]):
        r = await client.put(f"{API}/engine/facts", json={"engine": "claude"}, headers=_h(token))
        assert r.status_code == 403
    r = await client.get(f"{API}/engine", headers=_h(world["tok"]))
    assert r.json()["facts"]["follows_agent"] is True
    assert _audits(db, FACTS_AUDIT) == []


def test_facts_garbage_record_is_follow_agent(db):
    db.write_sync(
        lambda conn: engine_mod._meta_set(  # noqa: SLF001
            conn, engine_mod.META_FACTS, json.dumps({"engine": "gemini", "model": "x"})
        )
    )
    with db.reader() as conn:
        assert engine_mod.facts_stored(conn) is None
        assert engine_mod.facts_choice(conn)["follows_agent"] is True
