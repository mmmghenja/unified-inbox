"""Ф14-9, тест T6 и П6 (docs/architecture.md): хук certbot `scripts/hub-tls-deploy-hook.sh`
копирует пару из `$RENEWED_LINEAGE` в `$HUB_CONFIG_DIR/tls/` (0600) и один раз зовёт `systemctl --user
try-restart hub-core.service`; без `RENEWED_LINEAGE` — ошибка без копирования и без рестарта. Оба скрипта
исполняемые (certbot отказывает неисполняемому хуку), `hub-tls-setup.sh` — синтаксис и shellcheck.

Настоящий certbot и настоящий `systemctl` здесь не зовутся: поддельный `systemctl` первым в `PATH` пишет свои
аргументы в файл.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
HOOK = _SCRIPTS / "hub-tls-deploy-hook.sh"
SETUP = _SCRIPTS / "hub-tls-setup.sh"


@pytest.fixture
def env(tmp_path) -> dict:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(f'#!/usr/bin/env bash\necho "$*" >> {log}\n', encoding="utf-8")
    systemctl.chmod(0o755)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HUB_CONFIG_DIR": str(config_dir),
        "HOME": str(tmp_path / "home"),
    }


def _lineage(tmp_path: Path) -> Path:
    """Как у certbot: live/<домен>/*.pem — символические ссылки в archive/."""
    archive = tmp_path / "certbot" / "archive" / "hub.test"
    live = tmp_path / "certbot" / "live" / "hub.test"
    archive.mkdir(parents=True)
    live.mkdir(parents=True)
    (archive / "fullchain1.pem").write_text("CERT-CHAIN\n", encoding="utf-8")
    (archive / "privkey1.pem").write_text("PRIVATE-KEY\n", encoding="utf-8")
    (live / "fullchain.pem").symlink_to(archive / "fullchain1.pem")
    (live / "privkey.pem").symlink_to(archive / "privkey1.pem")
    return live


def _systemctl_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "systemctl.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_scripts_are_executable() -> None:
    assert os.access(HOOK, os.X_OK)
    assert os.access(SETUP, os.X_OK)


def test_hook_copies_pair_0600_and_restarts_core_once(tmp_path, env) -> None:
    lineage = _lineage(tmp_path)
    r = subprocess.run(  # noqa: S603 — скрипт репозитория
        [str(HOOK)], env={**env, "RENEWED_LINEAGE": str(lineage)}, capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    tls_dir = Path(env["HUB_CONFIG_DIR"]) / "tls"
    assert (tls_dir / "fullchain.pem").read_text(encoding="utf-8") == "CERT-CHAIN\n"
    assert (tls_dir / "privkey.pem").read_text(encoding="utf-8") == "PRIVATE-KEY\n"
    for name in ("fullchain.pem", "privkey.pem"):
        path = tls_dir / name
        assert not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
    assert stat.S_IMODE(tls_dir.stat().st_mode) == 0o700
    assert sorted(p.name for p in tls_dir.iterdir()) == ["fullchain.pem", "privkey.pem"]  # временных нет
    assert _systemctl_calls(tmp_path) == ["--user try-restart hub-core.service"]


def test_hook_replaces_existing_pair(tmp_path, env) -> None:
    tls_dir = Path(env["HUB_CONFIG_DIR"]) / "tls"
    tls_dir.mkdir(mode=0o700)
    (tls_dir / "fullchain.pem").write_text("OLD\n", encoding="utf-8")
    (tls_dir / "privkey.pem").write_text("OLD\n", encoding="utf-8")
    lineage = _lineage(tmp_path)
    r = subprocess.run(  # noqa: S603
        [str(HOOK)], env={**env, "RENEWED_LINEAGE": str(lineage)}, capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    assert (tls_dir / "privkey.pem").read_text(encoding="utf-8") == "PRIVATE-KEY\n"
    assert stat.S_IMODE((tls_dir / "privkey.pem").stat().st_mode) == 0o600


def test_hook_without_lineage_fails_without_copy_or_restart(tmp_path, env) -> None:
    env.pop("RENEWED_LINEAGE", None)
    r = subprocess.run([str(HOOK)], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert r.returncode != 0
    assert "RENEWED_LINEAGE" in r.stderr
    assert not (Path(env["HUB_CONFIG_DIR"]) / "tls").exists()
    assert _systemctl_calls(tmp_path) == []


def test_hook_with_missing_key_fails_without_restart(tmp_path, env) -> None:
    lineage = _lineage(tmp_path)
    (lineage / "privkey.pem").unlink()
    r = subprocess.run(  # noqa: S603
        [str(HOOK)], env={**env, "RENEWED_LINEAGE": str(lineage)}, capture_output=True, text=True, timeout=30
    )
    assert r.returncode != 0
    assert not (Path(env["HUB_CONFIG_DIR"]) / "tls").exists()
    assert _systemctl_calls(tmp_path) == []


def test_setup_script_syntax() -> None:
    r = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True, timeout=30)  # noqa: S603, S607
    assert r.returncode == 0, r.stderr


def test_setup_script_rejects_bad_domain(tmp_path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    for argv in ([], ["hub.example.com; rm -rf /"], ["-d"]):
        r = subprocess.run([str(SETUP), *argv], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
        assert r.returncode == 2, argv
        assert "использование" in r.stderr
    assert not (tmp_path / ".config").exists()


def _fake_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def setup_home(tmp_path) -> tuple[Path, dict]:
    """Поддельный HOME для `hub-tls-setup.sh`: certbot, uv, systemctl, боевой hub и хук пишут аргументы в
    `calls.log`; настоящих certbot/Cloudflare/systemd здесь нет (П7: токен — из поддельного cf-token)."""
    home = tmp_path / "home"
    calls = tmp_path / "calls.log"
    log = f'echo "$(basename "$0") $*" >> {calls}'
    _fake_exe(
        home / ".local" / "bin" / "certbot",
        f'{log}\nif [ "$1" = plugins ] && [ -z "${{NO_PLUGIN:-}}" ]; then '
        'printf "%s\\n" "----" "* dns-cloudflare" "Description: x" "* standalone"; fi',
    )
    _fake_exe(tmp_path / "bin" / "uv", log)
    _fake_exe(tmp_path / "bin" / "systemctl", log)
    _fake_exe(home / "projects" / "hub-prod" / ".venv" / "bin" / "hub", log)
    _fake_exe(
        home / "projects" / "hub-prod" / "scripts" / "hub-tls-deploy-hook.sh",
        f'{log}\necho "$RENEWED_LINEAGE" >> {calls}',
    )
    env = {**os.environ, "HOME": str(home), "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    env.pop("HUB_CONFIG_DIR", None)
    return home, env


def _run_setup(env: dict, stdin: str = "Y\n") -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [str(SETUP), "hub.test.example"], env=env, input=stdin, capture_output=True, text=True, timeout=60
    )


def test_setup_token_from_family_vpn_trimmed_into_ini_only(tmp_path, setup_home) -> None:
    home, env = setup_home
    token_file = home / "cf-secrets" / ".secrets" / "cf-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(" tok-SECRET-123 \r\n", encoding="utf-8")
    r = _run_setup(env)
    assert r.returncode == 0, r.stdout + r.stderr
    ini = home / ".config" / "hub" / "tls" / "cloudflare.ini"
    assert ini.read_bytes() == b"dns_cloudflare_api_token = tok-SECRET-123\n"  # байтами: \r не прячется
    assert stat.S_IMODE(ini.stat().st_mode) == 0o600
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "tok-SECRET-123" not in calls  # ни в одном аргументе
    assert "tok-SECRET-123" not in r.stdout + r.stderr
    assert "uv " not in calls  # certbot с плагином уже стоит — не переустанавливать
    certonly = next(ln for ln in calls.splitlines() if ln.startswith("certbot certonly"))
    assert f"--dns-cloudflare-credentials {ini}" in certonly
    assert "-d hub.test.example" in certonly
    assert f"--deploy-hook {home}/projects/hub-prod/scripts/hub-tls-deploy-hook.sh" in certonly
    assert f"{home}/.config/hub/tls/certbot/live/hub.test.example" in calls  # хук позван и без certbot
    assert "systemctl --user enable --now hub-cert-renew.timer" in calls
    assert "hub remote status" in calls
    assert "https://hub.test.example:8787" in r.stdout


def test_setup_empty_family_vpn_token_refuses(tmp_path, setup_home) -> None:
    home, env = setup_home
    token_file = home / "cf-secrets" / ".secrets" / "cf-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(" \r\n", encoding="utf-8")
    r = _run_setup(env)
    assert r.returncode != 0
    assert "cf-token пуст" in r.stderr
    assert not (home / ".config" / "hub" / "tls" / "cloudflare.ini").exists()
    assert "certbot certonly" not in (tmp_path / "calls.log").read_text(encoding="utf-8")


def test_setup_installs_plugin_when_missing(tmp_path, setup_home) -> None:
    home, env = setup_home
    ini = home / ".config" / "hub" / "tls" / "cloudflare.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("dns_cloudflare_api_token = x\n", encoding="utf-8")
    r = _run_setup({**env, "NO_PLUGIN": "1"}, stdin="")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "uv tool install --force certbot --with certbot-dns-cloudflare" in calls
    plugins = next(ln for ln in calls if ln.startswith("certbot plugins"))
    assert f"--config-dir {home}/.config/hub/tls/certbot" in plugins


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck не установлен")
def test_scripts_pass_shellcheck() -> None:
    r = subprocess.run(  # noqa: S603, S607
        ["shellcheck", str(SETUP), str(HOOK)], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
