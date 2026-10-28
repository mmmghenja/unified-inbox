"""Ф54-2 П8 (docs/architecture.md): сторож тревожит по упавшему юниту бота владельца в Telegram."""

import subprocess

from hub import watch as watch_mod

UNIT = "hub-human-bot.service"


def _snapshot(services) -> dict:
    return {
        "accounts": [],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
        "failed_connector_units": [],
        "failed_service_units": services,
    }


def test_failed_bot_unit_is_a_problem() -> None:
    problems = watch_mod.evaluate(_snapshot([UNIT, "hub-core.service"]), 1_000_000)
    assert [(p.key, p.text) for p in problems] == [
        (f"service_unit:{UNIT}:failed", "бот владельца в Telegram (hub-human-bot) упал — см. журнал службы")
    ]
    assert watch_mod.evaluate(_snapshot([]), 1_000_000) == []
    assert watch_mod.evaluate(_snapshot(None), 1_000_000) == []


def test_failed_service_units_asks_systemctl_by_exact_names(monkeypatch) -> None:
    seen: dict = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        out = f"{UNIT} loaded failed failed hub human-bot\n  hub-core.service loaded failed failed x\n\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out)

    monkeypatch.setattr(watch_mod.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(watch_mod.subprocess, "run", _run)
    assert watch_mod.failed_service_units() == [UNIT]
    assert seen["argv"] == [
        "/usr/bin/systemctl",
        "--user",
        "list-units",
        UNIT,
        "--state=failed",
        "--no-legend",
        "--plain",
    ]
    monkeypatch.setattr(watch_mod.shutil, "which", lambda _name: None)
    assert watch_mod.failed_service_units() is None


def test_unknown_services_keep_prior_problem(monkeypatch, tmp_path) -> None:
    key = f"service_unit:{UNIT}:failed"
    snapshot = _snapshot(None)
    monkeypatch.setattr(watch_mod, "gather_snapshot", lambda **_kw: dict(snapshot))
    state_path = tmp_path / watch_mod.STATE_FILE_NAME
    state_path.write_text(
        '{"problems": {"%s": {"first_ts": 1, "last_notified_ts": 1}}}' % key,  # noqa: UP031
        encoding="utf-8",
    )
    notes: list = []
    kwargs = {"port": 1, "data_dir": tmp_path, "config_dir": tmp_path, "admin_secret": "x"}
    watch_mod.run_watch(**kwargs, now_ms=5, notifier=lambda *a: notes.append(a))
    assert notes == [] and key in state_path.read_text(encoding="utf-8")
    snapshot["failed_service_units"] = []
    watch_mod.run_watch(**kwargs, now_ms=6, notifier=lambda *a: notes.append(a))
    assert notes == [(f"hub: {key}", "восстановилось")]
