"""`hub transcribe setup` на двух машинах (Ф79-1, docs/architecture.md):
у каждой машины свой токен службы — отзывается только выпущенный здесь (`host` в token.json); token.json
без `host` (до Ф79-1, мог приехать копией конфига) на спутнике не отзывается; `--device` пишет устройство
в transcribe.json. Против настоящего ядра дочерним процессом, как test_setup_cli.py."""

import json
import socket

from hub import cli as hub_cli
from hub.transcribe import files
from hub.transcribe import main as tmain

from .conftest import api_get as _api
from .test_setup_cli import _runner


def _setup(config_dir, data_dir, **kw):
    with hub_cli._admin_client() as admin:
        return tmain.run_setup(admin, config_dir=config_dir, data_dir=data_dir, runner=_runner(), **kw)


def _meta(config_dir) -> dict:
    return json.loads(files.token_meta_path(config_dir).read_text())


def test_device_flag_and_host_in_meta(real_core) -> None:
    data_dir, config_dir, _port = real_core
    ok, text = _setup(config_dir, data_dir, device="cpu")
    assert ok, text
    assert json.loads(files.config_path(config_dir).read_text())["device"] == "cpu"
    assert "медленный работник" in text
    assert _meta(config_dir)["host"] == socket.gethostname()
    ok, text = _setup(config_dir, data_dir, device="cuda")
    assert json.loads(files.config_path(config_dir).read_text())["device"] == "cuda"
    assert "быстрый работник" in text and "отозван" in text  # свой прежний — отозван, как раньше


def test_token_of_other_machine_is_not_revoked(real_core) -> None:
    data_dir, config_dir, port = real_core
    _setup(config_dir, data_dir)
    first, first_meta = files.read_secret(files.token_path(config_dir)), _meta(config_dir)
    # Конфиг приехал с другой машины: её токен и её token.json.
    files.atomic_write(files.token_meta_path(config_dir), json.dumps({**first_meta, "host": "desk-other"}))
    ok, text = _setup(config_dir, data_dir)
    assert ok
    assert _api(port, first, "/projects").status_code == 200  # служба другой машины работает дальше
    assert "не отзывался" in text and "отозван." not in text


def test_legacy_meta_without_host_kept_on_satellite_revoked_elsewhere(real_core) -> None:
    data_dir, config_dir, port = real_core
    _setup(config_dir, data_dir)
    second = files.read_secret(files.token_path(config_dir))
    files.atomic_write(
        files.token_meta_path(config_dir), json.dumps({"token_id": _meta(config_dir)["token_id"]})
    )
    ok, text = _setup(config_dir, data_dir)  # одна машина / главная: без host — отзывается, как до Ф79-1
    assert ok and "отозван" in text
    assert _api(port, second, "/projects").status_code == 401
