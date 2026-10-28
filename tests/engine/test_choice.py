"""`hub.engine.choice`: роли, переопределения `engine.json`, путь к `opencode` (Ф16, 3.3)."""

import json

import pytest

from hub.engine import choice


def test_roles_all_deepseek_flash_max():
    # Владелец 29.09 вечером: резерв целиком на DeepSeek Flash (Qwen/Grok — дорого).
    assert set(choice.ROLES) == {"work", "project", "explore", "facts", "agent"}
    for role in choice.ROLES:
        assert choice.role_model(role) == ("deepseek", "deepseek-flash", "max")


def test_load_overrides_missing_file_is_defaults(tmp_path):
    assert choice.load_overrides(tmp_path) == {}


def test_load_overrides_reads_opencode_section(tmp_path):
    (tmp_path / "engine.json").write_text(
        json.dumps({"opencode": {"path": "/x/opencode", "disable_mcp": ["a"]}})
    )
    assert choice.load_overrides(tmp_path) == {"path": "/x/opencode", "disable_mcp": ["a"]}


@pytest.mark.parametrize("raw", ["не json", "[1, 2]", '{"opencode": "строка"}'])
def test_load_overrides_broken_file_is_unavailable(tmp_path, raw):
    (tmp_path / "engine.json").write_text(raw)
    with pytest.raises(choice.EngineUnavailable):
        choice.load_overrides(tmp_path)


def test_role_model_override_partial_and_full():
    over = {"roles": {"work": {"variant": "high"}, "project": {"model": "xai/grok-4.7", "variant": "low"}}}
    assert choice.role_model("work", over) == ("deepseek", "deepseek-flash", "high")
    assert choice.role_model("project", over) == ("xai", "grok-4.7", "low")
    # Ф57-2: ключ снятой роли `orch` в старом `engine.json` читается без падения и ни на что не влияет.
    old = {"roles": {"orch": {"variant": "low"}}}
    assert choice.role_model("work", old) == ("deepseek", "deepseek-flash", "max")


def test_role_model_rejects_unknown_role_and_bad_model():
    with pytest.raises(choice.EngineUnavailable):
        choice.role_model("nope")
    with pytest.raises(choice.EngineUnavailable):
        choice.role_model("work", {"roles": {"work": {"model": "без-провайдера"}}})


def test_opencode_path_from_overrides(tmp_path):
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert choice.opencode_path({"path": str(binary)}) == str(binary)
    # Путь из файла есть, но бинаря нет — ошибка, а не молчаливый поиск в PATH.
    with pytest.raises(choice.EngineUnavailable, match="opencode не найден"):
        choice.opencode_path({"path": str(tmp_path / "нет")})


def test_opencode_path_which_and_missing(tmp_path, monkeypatch):
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert choice.opencode_path({}) == str(binary)
    monkeypatch.setenv("PATH", str(tmp_path / "пусто"))
    with pytest.raises(choice.EngineUnavailable, match="^opencode не найден$"):
        choice.opencode_path(None)


def test_saved_overrides_with_removed_wake_role_are_read():
    """Ф57-2: в сохранённом `engine.json` может остаться роль `wake` убранной дежурной — читается, остальные
    роли не меняются."""
    over = {"roles": {"wake": {"model": "xai/grok-4.7", "variant": "low"}, "work": {"variant": "high"}}}
    assert choice.role_model("work", over) == ("deepseek", "deepseek-flash", "high")
    assert "wake" not in choice.ROLES
