"""Ф51-1 A4 (docs/architecture.md): сторож видит упавший юнит коннектора; причина отказа входа —
в тексте проблемы `auth_required`."""

import subprocess

from hub import watch as watch_mod


def _account(account_id=6, *, status="ok", last_error=None) -> dict:
    return {
        "id": account_id,
        "source": "mail",
        "title": "ящик",
        "status": status,
        "last_ok_ts": 1_000_000,
        "flood_until": None,
        "has_connector_secret": True,
        "last_alert_ts": None,
        "last_error": last_error,
    }


def _snapshot(accounts, failed) -> dict:
    return {
        "accounts": accounts,
        "dead_letter_total": 0,
        "outbox_unknown": 0,
        "failed_connector_units": failed,
    }


def test_failed_unit_without_auth_required_is_a_problem() -> None:
    problems = watch_mod.evaluate(_snapshot([_account(6)], [6]), 1_000_000)
    assert [(p.key, p.text) for p in problems] == [
        ("connector_unit:6:failed", "коннектор аккаунта 6 упал — см. журнал hub-connector@6")
    ]


def test_failed_unit_with_auth_required_is_not_doubled() -> None:
    snapshot = _snapshot([_account(6, status="auth_required"), _account(7)], [6, 7])
    keys = [p.key for p in watch_mod.evaluate(snapshot, 1_000_000)]
    assert keys == ["account:6:auth_required", "connector_unit:7:failed"]


def test_auth_required_text_carries_last_error() -> None:
    snapshot = _snapshot([_account(6, status="auth_required", last_error="imap: вход не принят (other)")], [])
    (problem,) = watch_mod.evaluate(snapshot, 1_000_000)
    assert problem.text == "mail ящик: требуется вход — imap: вход не принят (other)"


def test_auth_required_text_without_last_error_is_as_before() -> None:
    snapshot = _snapshot([_account(6, status="auth_required")], [])
    (problem,) = watch_mod.evaluate(snapshot, 1_000_000)
    assert problem.text == "mail ящик: требуется вход"


def test_no_failed_units_key_is_fine() -> None:
    snapshot = {"accounts": [_account(6)], "dead_letter_total": 0, "outbox_unknown": 0}
    assert watch_mod.evaluate(snapshot, 1_000_000) == []


def test_parse_failed_units() -> None:
    output = (
        "hub-connector@6.service loaded failed failed Hub connector 6\n"
        "  hub-connector@12.service loaded failed failed Hub connector 12\n"
        "hub-connector@6.service loaded failed failed Hub connector 6\n"
        "hub-core.service loaded failed failed Hub core\n"
        "hub-connector@x.service loaded failed failed мусор\n"
        "\n"
    )
    assert watch_mod.parse_failed_units(output) == [6, 12]


def test_failed_connector_units_runs_systemctl(monkeypatch) -> None:
    seen: dict = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="hub-connector@6.service loaded failed failed x\n")

    monkeypatch.setattr(watch_mod.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(watch_mod.subprocess, "run", _run)
    assert watch_mod.failed_connector_units() == [6]
    assert seen["argv"] == [
        "/usr/bin/systemctl",
        "--user",
        "list-units",
        "hub-connector@*",
        "--state=failed",
        "--no-legend",
        "--plain",
    ]
    assert seen["kwargs"]["timeout"] == 5.0


def test_failed_connector_units_command_failure_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(watch_mod.shutil, "which", lambda _name: "/usr/bin/systemctl")

    def _timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(watch_mod.subprocess, "run", _timeout)
    assert watch_mod.failed_connector_units() is None

    def _fail(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(watch_mod.subprocess, "run", _fail)
    assert watch_mod.failed_connector_units() is None

    monkeypatch.setattr(watch_mod.shutil, "which", lambda _name: None)
    assert watch_mod.failed_connector_units() is None


def test_failed_unit_is_not_doubled_with_any_account_problem() -> None:
    """Ревью Ф51-1 A4: упавший юнит у аккаунта, который уже назван («молчит») — одна тревога, не две."""
    now = 10_000_000_000
    silent = {**_account(6), "last_ok_ts": now - 3 * 3600 * 1000}
    fresh = {**_account(7), "last_ok_ts": now}
    keys = [p.key for p in watch_mod.evaluate(_snapshot([silent, fresh], [6, 7]), now)]
    assert keys == ["account:6:silent", "connector_unit:7:failed"]


def test_unknown_units_keep_prior_unit_problems_untouched() -> None:
    """`systemctl` не ответил — о юнитах неизвестно: прежняя проблема не «восстановилась» и не напомнена."""
    state = {"problems": {"connector_unit:6:failed": {"first_ts": 1, "last_notified_ts": 1}}}
    notes: list[tuple[str, str]] = []

    def notifier(title: str, text: str) -> None:
        notes.append((title, text))

    late = watch_mod.REMIND_MS * 3
    kept = watch_mod.apply_antispam([], state, late, notifier=notifier, unknown_prefixes=("connector_unit:",))
    assert kept == state and notes == []
    # список получен и пуст — «восстановилось»
    gone = watch_mod.apply_antispam([], state, late, notifier=notifier)
    assert gone == {"problems": {}} and notes == [("hub: connector_unit:6:failed", "восстановилось")]


def test_run_watch_passes_unknown_units(monkeypatch, tmp_path) -> None:
    snapshot = {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0, "failed_connector_units": None}
    monkeypatch.setattr(watch_mod, "gather_snapshot", lambda **_kw: dict(snapshot))
    state_path = tmp_path / watch_mod.STATE_FILE_NAME
    state_path.write_text(
        '{"problems": {"connector_unit:6:failed": {"first_ts": 1, "last_notified_ts": 1}}}', encoding="utf-8"
    )
    notes: list = []
    kwargs = {"port": 1, "data_dir": tmp_path, "config_dir": tmp_path, "admin_secret": "x"}
    watch_mod.run_watch(**kwargs, now_ms=5, notifier=lambda *a: notes.append(a))
    assert notes == [] and "connector_unit:6:failed" in state_path.read_text(encoding="utf-8")
    snapshot["failed_connector_units"] = []
    watch_mod.run_watch(**kwargs, now_ms=6, notifier=lambda *a: notes.append(a))
    assert notes == [("hub: connector_unit:6:failed", "восстановилось")]
