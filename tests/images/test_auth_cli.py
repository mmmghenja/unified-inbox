"""Ф91 Р1: `hub auth alibaba [--from-env-file PATH] [--var NAME] [--force] [--check]` — значение не
печатается, права файлов, вопрос «заменить?», `--check` бьёт в подделку DashScope (картинка не
заказывается)."""

import json
import stat
import sys

import httpx
import pytest

from hub import cli as hub_cli
from hub.images import auth as image_auth
from hub.images import config as image_config

from .conftest import KEY


def _env(tmp_path, text: str):
    path = tmp_path / "dotenv"
    path.write_text(text, encoding="utf-8")
    return path


def _no_ask(_prompt):
    raise AssertionError("вопроса быть не должно")


def _collect():
    lines: list[str] = []
    return lines, lines.append


def test_from_env_file_default_var_saves_0600_without_printing(tmp_path, config_dir):
    env = _env(tmp_path, f"OTHER=1\nDASHSCOPE_API_KEY={KEY}\n")
    lines, out = _collect()
    assert image_auth.auth(config_dir=config_dir, env_file=env, ask=_no_ask, ask_secret=_no_ask, out=out) == 0
    assert KEY not in "\n".join(lines)
    assert "ключ Alibaba сохранён" in lines
    assert image_config.load(config_dir).api_key == KEY
    assert stat.S_IMODE(image_config.key_path(config_dir).stat().st_mode) == 0o600
    assert stat.S_IMODE(image_config.service_dir(config_dir).stat().st_mode) == 0o700


def test_from_env_file_custom_var(tmp_path, config_dir):
    env = _env(tmp_path, f'export ALIBABA_MODEL_STUDIO_KEY="{KEY}"\nDASHSCOPE_API_KEY=other\n')
    code = image_auth.auth(
        config_dir=config_dir, env_file=env, var="ALIBABA_MODEL_STUDIO_KEY", out=lambda _l: None
    )
    assert code == 0
    assert image_config.read_key(config_dir) == KEY


def test_from_env_file_missing_var_writes_nothing(tmp_path, config_dir):
    lines, out = _collect()
    env = _env(tmp_path, f"SOMETHING={KEY}\n")
    assert image_auth.auth(config_dir=config_dir, env_file=env, out=out) == 1
    assert lines == ["в файле нет: DASHSCOPE_API_KEY — ничего не записано"]
    assert not image_config.key_path(config_dir).exists()


def test_unreadable_env_file(tmp_path, config_dir):
    lines, out = _collect()
    assert image_auth.auth(config_dir=config_dir, env_file=tmp_path / "нет", out=out) == 1
    assert "файл не читается" in lines[0]


def test_interactive_key(config_dir):
    assert image_auth.auth(config_dir=config_dir, ask_secret=lambda _p: f" {KEY} ", out=lambda _l: None) == 0
    assert image_config.read_key(config_dir) == KEY


def test_interactive_empty_writes_nothing(config_dir):
    lines, out = _collect()
    assert image_auth.auth(config_dir=config_dir, ask_secret=lambda _p: "", out=out) == 1
    assert lines == ["ключ не введён — ничего не записано"]
    assert not image_config.key_path(config_dir).exists()


def test_existing_key_asks_and_keeps(tmp_path, config_dir):
    image_config.save(config_dir, api_key=KEY)
    env = _env(tmp_path, "DASHSCOPE_API_KEY=NEW\n")
    lines, out = _collect()
    assert image_auth.auth(config_dir=config_dir, env_file=env, ask=lambda _p: "n", out=out) == 0
    assert lines == ["оставлен прежний ключ"]
    assert image_config.read_key(config_dir) == KEY


def test_force_replaces_without_question(tmp_path, config_dir):
    image_config.save(config_dir, api_key=KEY)
    env = _env(tmp_path, "DASHSCOPE_API_KEY=NEW\n")
    assert (
        image_auth.auth(config_dir=config_dir, env_file=env, force=True, ask=_no_ask, out=lambda _l: None)
        == 0
    )
    assert image_config.read_key(config_dir) == "NEW"
    assert stat.S_IMODE(image_config.key_path(config_dir).stat().st_mode) == 0o600


def test_check_without_key(config_dir):
    lines, out = _collect()
    assert image_auth.auth(config_dir=config_dir, run_check=True, out=out) == 1
    assert lines == ["ключ Alibaba не задан — hub auth alibaba"]


def _probe_transport(status: int, body: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_check_400_invalid_parameter_means_key_works(config_dir):
    image_config.save(config_dir, api_key=KEY)
    seen: list[httpx.Request] = []
    lines, out = _collect()
    transport = _probe_transport(400, {"code": "InvalidParameter", "message": "size invalid"}, seen)
    assert image_auth.auth(config_dir=config_dir, run_check=True, out=out, transport=transport) == 0
    assert lines == ["ключ работает"]
    request = seen[0]
    assert request.url.host == "dashscope-intl.aliyuncs.com"
    assert request.url.path == "/api/v1/services/aigc/multimodal-generation/generation"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.read())
    assert body["parameters"]["size"] == "1*1"  # заведомо неверный размер: картинка не заказывается
    assert body["model"] == "qwen-image-2.0"


def test_check_401_key_rejected_no_key_in_output(config_dir):
    image_config.save(config_dir, api_key=KEY)
    lines, out = _collect()
    transport = _probe_transport(401, {"code": "InvalidApiKey", "message": f"bad {KEY}"}, [])
    assert image_auth.auth(config_dir=config_dir, run_check=True, out=out, transport=transport) == 1
    assert "ключ не принят (HTTP 401)" in lines[0]
    assert KEY not in "\n".join(lines)


def test_check_after_save_from_env_file(tmp_path, config_dir):
    seen: list[httpx.Request] = []
    env = _env(tmp_path, f"DASHSCOPE_API_KEY={KEY}\n")
    transport = _probe_transport(400, {"code": "InvalidParameter"}, seen)
    lines, out = _collect()
    code = image_auth.auth(config_dir=config_dir, env_file=env, run_check=True, out=out, transport=transport)
    assert code == 0 and lines[-1] == "ключ работает" and len(seen) == 1


def test_check_network_error(config_dir):
    image_config.save(config_dir, api_key=KEY)

    def handler(request):
        raise httpx.ConnectError("нет", request=request)

    lines, out = _collect()
    assert image_auth.check(config_dir, out=out, transport=httpx.MockTransport(handler)) == 1
    assert lines == ["нет связи с Alibaba"]


def _run_cli(argv):
    old = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old


def test_cli_from_env_file_with_var(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config"
    monkeypatch.setenv("HUB_CONFIG_DIR", str(cfg))
    env = _env(tmp_path, f"MY_KEY={KEY}\n")
    _run_cli(["auth", "alibaba", "--from-env-file", str(env), "--var", "MY_KEY", "--force"])
    assert image_config.read_key(cfg) == KEY
    assert KEY not in capsys.readouterr().out


def test_cli_missing_var_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path / "config"))
    env = _env(tmp_path, "OTHER=1\n")
    with pytest.raises(SystemExit) as exc:
        _run_cli(["auth", "alibaba", "--from-env-file", str(env)])
    assert exc.value.code == 1
