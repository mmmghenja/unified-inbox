"""Тест 9 (кусок 7): `systemd-analyze --user verify` на реальных юнитах (skip, если нет `systemd-analyze`);
грепом — в `deploy/` нет абсолютных `/home/...`, только `%h`.
Тест 16 (кусок 1a фазы 1): `hub-connector@.service`
получил боевой `ExecStart` и `RestartPreventExitStatus=78` — юниты установки его копируют, но НЕ включают
(инстанс нужен — `hub-connector@<account_id>.service`, включает владелец сам после `hub auth tg`).

`hub-connector@.service` — юнит-шаблон (`%i`): `systemd-analyze verify` без указания инстанса на шаблонах не
работает так же, как на обычных юнитах, поэтому он в список `_REAL_UNITS` verify не входит; сам факт «в
deploy/ нет абсолютных путей» проверяется по ВСЕМ файлам, включая шаблон.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
_REAL_UNITS = [
    "hub-core.service",
    "hub-watch.service",
    "hub-watch.timer",
    "hub-backup.service",
    "hub-backup.timer",
    "hub-agent.service",  # кусок 7b
    "hub-transcribe.service",  # Ф14-6
]

_HAS_SYSTEMD_ANALYZE = shutil.which("systemd-analyze") is not None
# verify проверяет, что ExecStart исполняемый: без боевого дерева (CI, чужая машина) — пропуск.
_HAS_PROD = (Path.home() / "projects/hub-prod/.venv/bin/hub").exists()


@pytest.mark.skipif(not _HAS_SYSTEMD_ANALYZE, reason="systemd-analyze не установлен")
@pytest.mark.skipif(not _HAS_PROD, reason="нет ~/projects/hub-prod/.venv/bin/hub")
def test_units_pass_systemd_analyze_verify() -> None:
    paths = [str(_DEPLOY_DIR / name) for name in _REAL_UNITS]
    r = subprocess.run(  # noqa: S603, S607
        ["systemd-analyze", "--user", "verify", *paths], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_absolute_home_paths_in_deploy_dir() -> None:
    offenders = []
    for path in _DEPLOY_DIR.iterdir():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "/home/" in text:
            offenders.append(path.name)
    assert offenders == []


def test_deploy_dir_has_expected_units() -> None:
    names = {p.name for p in _DEPLOY_DIR.iterdir()}
    assert set(_REAL_UNITS) <= names
    assert "hub-connector@.service" in names


def test_1a_16_connector_template_has_execstart_and_restart_prevent_exit_status() -> None:
    """Тест 16 куска 1a: `%i` = account_id, боевой `ExecStart`, `RestartPreventExitStatus=78` (78 —
    ConnectorBase.run() решил не перезапускаться: секрет отозван / источник не авторизован), та же
    песочница, что у hub-core.service (сверка построчно — набор ключей sandbox должен совпадать)."""
    text = (_DEPLOY_DIR / "hub-connector@.service").read_text(encoding="utf-8")
    assert "\nExecStart=%h/projects/hub-prod/.venv/bin/hub connector %i\n" in text
    assert "\nRestartPreventExitStatus=78\n" in text

    def _sandbox_keys(unit_text: str) -> set[str]:
        keys = {
            "NoNewPrivileges",
            "ProtectSystem",
            "ProtectHome",
            "PrivateTmp",
            "RestrictAddressFamilies",
            "LockPersonality",
            "SystemCallArchitectures",
            "UMask",
        }
        present = set()
        for line in unit_text.splitlines():
            key = line.split("=", 1)[0]
            if key in keys:
                present.add(key)
        return present

    core_text = (_DEPLOY_DIR / "hub-core.service").read_text(encoding="utf-8")
    assert _sandbox_keys(text) == _sandbox_keys(core_text)


def test_1a_16_units_install_copies_connector_template_without_enabling() -> None:
    """Тест 16: `hub-connector@.service` копируется скриптом установки, но `enable --now` для него не
    вызывается (юнит-шаблон — нужен конкретный account_id, решает владелец после `hub auth tg`)."""
    script = (Path(__file__).resolve().parents[2] / "scripts" / "hub-units-install.sh").read_text(
        encoding="utf-8"
    )
    assert "hub-connector@.service" in script
    for line in script.splitlines():
        assert "enable --now hub-connector@.service" not in line


def test_install_script_creates_rw_dirs_before_enabling_units() -> None:
    """Приёмка у владельца: `ReadWritePaths=` требует существующих каталогов — на чистой машине core падал с
    226/NAMESPACE по кругу. Каждый путь `ReadWritePaths` из юнитов скрипт обязан создать ДО `enable --now`."""
    repo = Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "hub-units-install.sh").read_text(encoding="utf-8")
    mkdir_line = next(ln for ln in script.splitlines() if ln.startswith("mkdir -p -m 700"))
    rw_paths: set[str] = set()
    for unit in (repo / "deploy" / "systemd").glob("hub-*.service"):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("ReadWritePaths="):
                rw_paths.update(line.split("=", 1)[1].split())
    assert rw_paths
    for path in rw_paths:
        assert path.replace("%h", "$HOME") in mkdir_line, path
    assert script.index(mkdir_line) < script.index("systemctl --user enable --now")


def test_units_run_from_prod_worktree_not_dev_tree() -> None:
    """Боевой запуск отделён от разработки (22.09.2026): юниты берут код из `~/projects/hub-prod` (ветка
    `prod`, обновляется только `scripts/hub-deploy.sh` после приёмки). Editable-установка в dev-дереве
    означала бы, что перезагрузка ноутбука поднимает недописанный код и мигрирует им боевую базу."""
    units = sorted((Path(__file__).resolve().parents[2] / "deploy" / "systemd").glob("*.service"))
    assert units
    for unit in units:
        if unit.name == "hub-tunnel@.service":
            continue  # Ф10-7: туннель — `ssh`, не код хаба (его проверяет тест ниже)
        if unit.name == "hub-cert-renew.service":
            continue  # Ф14-9 П2: продление — certbot, не код хаба (его проверяет тест ниже)
        text = unit.read_text(encoding="utf-8")
        exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]
        assert exec_lines, unit.name
        for line in exec_lines:
            assert line.startswith("ExecStart=%h/projects/hub-prod/.venv/bin/hub "), (unit.name, line)


def test_f10_7_core_listens_remote_port_and_tunnel_template_is_manual() -> None:
    """Ф10-7, решение 10 и П2: боевой `hub core` явно поднимает удалённый слушатель 8790 (умолчание CLI — 0);
    туннель `hub-tunnel@.service` ведёт 127.0.0.1:8787 сервера на 127.0.0.1:8790 ноутбука, копируется
    скриптом установки, но не включается."""
    core = (_DEPLOY_DIR / "hub-core.service").read_text(encoding="utf-8")
    assert "\nExecStart=%h/projects/hub-prod/.venv/bin/hub core --remote-port 8790\n" in core
    tunnel = (_DEPLOY_DIR / "hub-tunnel@.service").read_text(encoding="utf-8")
    assert "-R 127.0.0.1:8787:127.0.0.1:8790 %i" in tunnel
    for option in ("BatchMode=yes", "ExitOnForwardFailure=yes", "ServerAliveInterval=20"):
        assert option in tunnel
    assert "\nRestart=always\n" in tunnel
    script = (Path(__file__).resolve().parents[2] / "scripts" / "hub-units-install.sh").read_text(
        encoding="utf-8"
    )
    manual = script[script.index("MANUAL_UNITS=(") : script.index(")", script.index("MANUAL_UNITS=("))]
    assert "hub-tunnel@.service" in manual
    for line in script.splitlines():
        if line.startswith("systemctl"):
            assert "hub-tunnel" not in line


def test_f14_6_transcribe_unit_is_manual_with_gpu_and_own_rw_dir() -> None:
    """Ф14-6, раздел E и П11: служба расшифровки — песочница как у hub-agent, но без `PrivateDevices`
    (движку нужны /dev/nvidia*); пишет только в свой каталог данных, кэш CUDA — там же; 78 не перезапускается;
    скрипт установки копирует юнит, но не включает, деплой перезапускает, только если он уже работает."""
    unit = (_DEPLOY_DIR / "hub-transcribe.service").read_text(encoding="utf-8")
    assert "\nExecStart=%h/projects/hub-prod/.venv/bin/hub transcribe run\n" in unit
    assert "\nRestartPreventExitStatus=78\n" in unit
    assert "\nReadWritePaths=%h/.local/share/hub/transcribe\n" in unit
    assert "\nProtectHome=read-only\n" in unit
    assert "\nEnvironment=CUDA_CACHE_PATH=%h/.local/share/hub/transcribe/cuda-cache\n" in unit
    assert not [ln for ln in unit.splitlines() if ln.startswith(("PrivateDevices", "DevicePolicy"))]
    repo = Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "hub-units-install.sh").read_text(encoding="utf-8")
    manual = script[script.index("MANUAL_UNITS=(") : script.index(")", script.index("MANUAL_UNITS=("))]
    assert "hub-transcribe.service" in manual
    for line in script.splitlines():
        if line.startswith("systemctl"):
            assert "hub-transcribe" not in line
    deploy = (repo / "scripts" / "hub-deploy.sh").read_text(encoding="utf-8")
    assert "is-active --quiet hub-transcribe.service" in deploy


def test_f14_9_cert_renew_units_sandbox_and_manual() -> None:
    """Ф14-9, решение 8 и П2–П3: `certbot renew` oneshot с явной песочницей (пишет только в свои каталоги и в
    ~/.config/hub/tls, сеть — ACME и Cloudflare, AF_UNIX — `systemctl --user` из хука); таймер 04:30 ±30 мин,
    догоняет пропущенное; скрипт установки копирует оба, но не включает — таймер включает hub-tls-setup.sh."""
    unit = (_DEPLOY_DIR / "hub-cert-renew.service").read_text(encoding="utf-8")
    exec_lines = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert exec_lines == [
        "ExecStart=%h/.local/bin/certbot renew --quiet --config-dir %h/.config/hub/tls/certbot "
        "--work-dir %h/.local/state/hub/certbot --logs-dir %h/.local/state/hub/certbot/logs"
    ]
    for line in (
        "Type=oneshot",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ReadWritePaths=%h/.config/hub/tls %h/.local/state/hub/certbot",
        "PrivateTmp=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "UMask=0077",
    ):
        assert f"\n{line}\n" in unit, line
    timer = (_DEPLOY_DIR / "hub-cert-renew.timer").read_text(encoding="utf-8")
    for line in ("OnCalendar=*-*-* 04:30", "RandomizedDelaySec=30min", "Persistent=true"):
        assert f"\n{line}\n" in timer, line

    repo = Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "hub-units-install.sh").read_text(encoding="utf-8")
    manual = script[script.index("MANUAL_UNITS=(") : script.index(")", script.index("MANUAL_UNITS=("))]
    assert "hub-cert-renew.service" in manual
    assert "hub-cert-renew.timer" in manual
    for line in script.splitlines():
        if line.startswith("systemctl"):
            assert "hub-cert-renew" not in line
    setup = (repo / "scripts" / "hub-tls-setup.sh").read_text(encoding="utf-8")
    assert "\nsystemctl --user enable --now hub-cert-renew.timer\n" in setup


@pytest.mark.skipif(
    not _HAS_SYSTEMD_ANALYZE or not (Path.home() / ".local" / "bin" / "certbot").exists(),
    reason="нет systemd-analyze или certbot (~/.local/bin/certbot)",
)
def test_f14_9_cert_renew_units_pass_systemd_analyze_verify() -> None:
    paths = [str(_DEPLOY_DIR / name) for name in ("hub-cert-renew.service", "hub-cert-renew.timer")]
    r = subprocess.run(  # noqa: S603, S607
        ["systemd-analyze", "--user", "verify", *paths], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stdout + r.stderr
