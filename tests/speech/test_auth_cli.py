"""Ф59-1: `hub auth speechkit [--from-env-file PATH] [--force] [--check]` — значения не печатаются, права
файлов, вопрос «заменить?», пробный запрос через подделку SpeechKit."""

import stat
import sys

import pytest

from hub import cli as hub_cli
from hub.speech import auth as speech_auth
from hub.speech import config as speech_config

from .conftest import FOLDER, KEY, configure


def _env_file(tmp_path, text: str):
    path = tmp_path / "dotenv"
    path.write_text(text, encoding="utf-8")
    return path


def _no_ask(_prompt):
    raise AssertionError("вопроса быть не должно")


def _collect():
    lines: list[str] = []
    return lines, lines.append


def _run_cli(argv):
    old = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old


def test_from_env_file_saves_without_printing_values(tmp_path, config_dir):
    env = _env_file(tmp_path, f"OTHER=1\nSPEECHKIT_API_KEY={KEY}\nSPEECHKIT_FOLDER_ID={FOLDER}\n")
    lines, out = _collect()
    code = speech_auth.auth(config_dir=config_dir, env_file=env, ask=_no_ask, ask_secret=_no_ask, out=out)
    assert code == 0
    printed = "\n".join(lines)
    assert "ключ SpeechKit сохранён, каталог: задан" in printed
    assert KEY not in printed and FOLDER not in printed
    cfg = speech_config.load(config_dir)
    assert (cfg.api_key, cfg.folder_id) == (KEY, FOLDER)
    assert stat.S_IMODE(speech_config.key_path(config_dir).stat().st_mode) == 0o600
    assert stat.S_IMODE(speech_config.service_dir(config_dir).stat().st_mode) == 0o700


def test_from_env_file_folder_falls_back_to_ocr(tmp_path, config_dir):
    env = _env_file(
        tmp_path, f'SPEECHKIT_API_KEY="{KEY}"\nSPEECHKIT_FOLDER_ID=\nOCR_YANDEX_FOLDER_ID=b1g-ocr\n'
    )
    assert speech_auth.auth(config_dir=config_dir, env_file=env, out=lambda _l: None) == 0
    assert speech_config.load(config_dir).folder_id == "b1g-ocr"


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        (f"SPEECHKIT_FOLDER_ID={FOLDER}\n", "SPEECHKIT_API_KEY"),
        (f"SPEECHKIT_API_KEY={KEY}\n", "SPEECHKIT_FOLDER_ID (или OCR_YANDEX_FOLDER_ID)"),
    ],
)
def test_from_env_file_missing_writes_nothing(tmp_path, config_dir, text, missing):
    lines, out = _collect()
    code = speech_auth.auth(config_dir=config_dir, env_file=_env_file(tmp_path, text), out=out)
    assert code == 1
    assert any(missing in line and "ничего не записано" in line for line in lines)
    assert not speech_config.key_path(config_dir).exists()
    printed = "\n".join(lines)
    assert KEY not in printed and FOLDER not in printed


def test_from_env_file_unreadable(tmp_path, config_dir):
    lines, out = _collect()
    assert speech_auth.auth(config_dir=config_dir, env_file=tmp_path / "нет-файла", out=out) == 1
    assert "файл не читается" in lines[0]


def test_existing_key_asks_and_keeps_on_no(tmp_path, config_dir):
    configure(config_dir)
    env = _env_file(tmp_path, "SPEECHKIT_API_KEY=NEW\nSPEECHKIT_FOLDER_ID=b1g-new\n")
    prompts: list[str] = []
    lines, out = _collect()

    def ask(prompt):
        prompts.append(prompt)
        return "n"

    assert speech_auth.auth(config_dir=config_dir, env_file=env, ask=ask, out=out) == 0
    assert len(prompts) == 1 and "Заменить?" in prompts[0]
    assert speech_config.load(config_dir).api_key == KEY
    assert lines == ["оставлен прежний ключ"]


def test_existing_key_replaced_on_yes(tmp_path, config_dir):
    configure(config_dir)
    env = _env_file(tmp_path, "SPEECHKIT_API_KEY=NEW\nSPEECHKIT_FOLDER_ID=b1g-new\n")
    assert (
        speech_auth.auth(config_dir=config_dir, env_file=env, ask=lambda _p: "да", out=lambda _l: None) == 0
    )
    assert speech_config.load(config_dir).api_key == "NEW"


def test_force_replaces_without_question(tmp_path, config_dir):
    configure(config_dir)
    env = _env_file(tmp_path, "SPEECHKIT_API_KEY=NEW\nSPEECHKIT_FOLDER_ID=b1g-new\n")
    assert (
        speech_auth.auth(config_dir=config_dir, env_file=env, force=True, ask=_no_ask, out=lambda _l: None)
        == 0
    )
    assert speech_config.load(config_dir).api_key == "NEW"
    assert stat.S_IMODE(speech_config.key_path(config_dir).stat().st_mode) == 0o600


def test_interactive_getpass_and_folder(config_dir):
    secrets_asked: list[str] = []

    def ask_secret(prompt):
        secrets_asked.append(prompt)
        return KEY

    lines, out = _collect()
    assert speech_auth.auth(config_dir=config_dir, ask_secret=ask_secret, ask=lambda _p: FOLDER, out=out) == 0
    assert len(secrets_asked) == 1
    assert speech_config.load(config_dir).folder_id == FOLDER
    assert KEY not in "\n".join(lines)


def test_interactive_empty_folder_keeps_previous(config_dir):
    configure(config_dir)
    code = speech_auth.auth(
        config_dir=config_dir, force=True, ask_secret=lambda _p: "NEW", ask=lambda _p: "", out=lambda _l: None
    )
    assert code == 0
    assert (speech_config.load(config_dir).api_key, speech_config.load(config_dir).folder_id) == (
        "NEW",
        FOLDER,
    )


def test_interactive_empty_key_writes_nothing(config_dir):
    assert (
        speech_auth.auth(config_dir=config_dir, ask_secret=lambda _p: "", ask=_no_ask, out=lambda _l: None)
        == 1
    )
    assert not speech_config.key_path(config_dir).exists()


# --- --check --------------------------------------------------------------------------------------


def test_check_ok_sends_half_second_of_silence(config_dir, fake_sk):
    configure(config_dir)
    fake_sk.body = {"result": ""}
    lines, out = _collect()
    assert (
        speech_auth.auth(
            config_dir=config_dir, run_check=True, ask=_no_ask, out=out, transport=fake_sk.transport
        )
        == 0
    )
    assert lines == ["ключ работает"]
    assert fake_sk.bodies == [bytes(16_000)]


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (401, "ключ не принят (HTTP 401): у ключа нет роли ai.speechkit-stt.user"),
        (403, "ключ не принят (HTTP 403)"),
        (500, "сбой SpeechKit (HTTP 500)"),
    ],
)
def test_check_reports_reason(config_dir, fake_sk, status, text):
    configure(config_dir)
    fake_sk.status = status
    lines, out = _collect()
    assert speech_auth.check(config_dir, out=out, transport=fake_sk.transport) == 1
    assert text in lines[0] and KEY not in lines[0]


def test_check_not_configured(config_dir, fake_sk):
    lines, out = _collect()
    assert speech_auth.check(config_dir, out=out, transport=fake_sk.transport) == 1
    assert lines == ["диктовка не настроена — hub auth speechkit"]
    assert fake_sk.requests == []


def test_from_env_file_then_check(tmp_path, config_dir, fake_sk):
    env = _env_file(tmp_path, f"SPEECHKIT_API_KEY={KEY}\nSPEECHKIT_FOLDER_ID={FOLDER}\n")
    lines, out = _collect()
    code = speech_auth.auth(
        config_dir=config_dir, env_file=env, run_check=True, out=out, transport=fake_sk.transport
    )
    assert code == 0 and lines[-1] == "ключ работает"


# --- разбор аргументов `hub auth` -----------------------------------------------------------------


def test_cli_wiring_from_env_file_force(tmp_path, config_dir, capsys):
    configure(config_dir)
    env = _env_file(tmp_path, "SPEECHKIT_API_KEY=CLI-KEY\nSPEECHKIT_FOLDER_ID=b1g-cli\n")
    _run_cli(["auth", "speechkit", "--from-env-file", str(env), "--force"])
    printed = capsys.readouterr().out
    assert "ключ SpeechKit сохранён, каталог: задан" in printed
    assert "CLI-KEY" not in printed and "b1g-cli" not in printed
    assert speech_config.load(config_dir).api_key == "CLI-KEY"


def test_cli_missing_key_exits_nonzero(tmp_path, config_dir, capsys):
    env = _env_file(tmp_path, "NOTHING=1\n")
    with pytest.raises(SystemExit) as exc:
        _run_cli(["auth", "speechkit", "--from-env-file", str(env)])
    assert exc.value.code == 1
    assert not speech_config.key_path(config_dir).exists()
