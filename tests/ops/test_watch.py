"""Тесты 6-7 (кусок 7): `evaluate()` — таблица случаев + границы (ровно 5 мин, ровно 36 ч), антиспам
(`apply_antispam`); `hub watch` при мёртвом core, `notify-send` отсутствует -> stderr, код 0. Спецификация:
docs/architecture.md
"""

import pytest

from hub import watch as watch_mod
from hub.watch import Problem, apply_antispam, evaluate

_HOUR_MS = 60 * 60 * 1000


def _account(**overrides) -> dict:
    base = {"id": 1, "source": "tg", "title": "Личный", "status": "ok", "last_ok_ts": 0, "flood_until": None}
    base.update(overrides)
    return base


# --- evaluate(): таблица случаев -----------------------------------------------------------------------


def test_core_down() -> None:
    assert evaluate({"core_down": True}, now_ms=1_000_000) == [Problem("core_down", "core недоступен")]


def test_account_auth_required() -> None:
    snapshot = {"accounts": [_account(status="auth_required")], "dead_letter_total": 0, "outbox_unknown": 0}
    problems = evaluate(snapshot, now_ms=1_000_000)
    assert [p.key for p in problems] == ["account:1:auth_required"]


def test_account_limited() -> None:
    snapshot = {"accounts": [_account(status="limited")], "dead_letter_total": 0, "outbox_unknown": 0}
    problems = evaluate(snapshot, now_ms=1_000_000)
    assert [p.key for p in problems] == ["account:1:limited"]


@pytest.mark.parametrize("status", ["no_network", "stale"])
def test_account_no_network_or_stale_under_5min_is_silent(status) -> None:
    snapshot = {
        "accounts": [_account(status=status, last_ok_ts=1_000_000 - watch_mod.STALE_ACCOUNT_MS)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    assert evaluate(snapshot, now_ms=1_000_000) == []  # ровно 5 мин -- ещё не проблема (граница)


@pytest.mark.parametrize("status", ["no_network", "stale"])
def test_account_no_network_or_stale_over_5min_is_a_problem(status) -> None:
    snapshot = {
        "accounts": [_account(status=status, last_ok_ts=1_000_000 - watch_mod.STALE_ACCOUNT_MS - 1)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    problems = evaluate(snapshot, now_ms=1_000_000)
    assert [p.key for p in problems] == [f"account:1:{status}"]


def test_account_ok_is_never_a_problem() -> None:
    snapshot = {
        "accounts": [_account(status="ok", last_ok_ts=0)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    assert evaluate(snapshot, now_ms=10**9) == []


# --- Тест 13 (кусок 1a фазы 1): account:<id>:silent --------------------------------------------------------


def test_1a_13_silent_appears_after_5min_without_heartbeat() -> None:
    last_ok_ts = 1_000_000 - watch_mod.SILENT_CONNECTOR_MS - 1
    snapshot = {
        "accounts": [_account(status="ok", last_ok_ts=last_ok_ts, has_connector_secret=True)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    problems = evaluate(snapshot, now_ms=1_000_000)
    assert [p.key for p in problems] == ["account:1:silent"]


def test_1a_13_silent_not_a_problem_under_5min() -> None:
    last_ok_ts = 1_000_000 - watch_mod.SILENT_CONNECTOR_MS
    snapshot = {
        "accounts": [_account(status="ok", last_ok_ts=last_ok_ts, has_connector_secret=True)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    assert evaluate(snapshot, now_ms=1_000_000) == []  # ровно 5 мин — граница, ещё не проблема


def test_1a_13_silent_ignored_without_connector_secret() -> None:
    """Аккаунт без `connector_secret` (голая запись, коннектора ещё нет) — не должен шуметь про молчание."""
    snapshot = {
        "accounts": [_account(status="ok", last_ok_ts=0, has_connector_secret=False)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    assert evaluate(snapshot, now_ms=10**9) == []


@pytest.mark.parametrize("status", ["limited", "auth_required"])
def test_1a_13_silent_not_reported_for_limited_or_auth_required(status) -> None:
    """У `limited`/`auth_required` уже своя проблема (тест выше) — `silent` не дублирует её."""
    snapshot = {
        "accounts": [_account(status=status, last_ok_ts=0, has_connector_secret=True)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    problems = evaluate(snapshot, now_ms=10**9)
    assert [p.key for p in problems] == [f"account:1:{status}"]
    assert not any(p.key.endswith(":silent") for p in problems)


def test_dead_letter_total() -> None:
    snapshot = {"accounts": [], "dead_letter_total": 3, "outbox_unknown": 0}
    problems = evaluate(snapshot, now_ms=0)
    assert [p.key for p in problems] == ["dead_letter"]


def test_outbox_unknown() -> None:
    snapshot = {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 2}
    problems = evaluate(snapshot, now_ms=0)
    assert [p.key for p in problems] == ["outbox_unknown"]


def test_backup_stale_ignored_when_recipient_not_configured() -> None:
    snapshot = {
        "accounts": [],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
        "backup_recipient_configured": False,
        "last_backup_ts": None,
    }
    assert evaluate(snapshot, now_ms=10**9) == []


def test_backup_never_ran_but_recipient_configured_is_a_problem() -> None:
    snapshot = {
        "accounts": [],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
        "backup_recipient_configured": True,
        "last_backup_ts": None,
    }
    problems = evaluate(snapshot, now_ms=10**9)
    assert [p.key for p in problems] == ["backup_stale"]


def test_backup_exactly_36h_is_not_a_problem() -> None:
    now_ms = 10 * _HOUR_MS + watch_mod.BACKUP_STALE_MS
    snapshot = {
        "accounts": [],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
        "backup_recipient_configured": True,
        "last_backup_ts": 10 * _HOUR_MS,
    }
    assert evaluate(snapshot, now_ms=now_ms) == []  # ровно 36 ч -- граница, ещё не проблема


def test_backup_over_36h_is_a_problem() -> None:
    now_ms = 10 * _HOUR_MS + watch_mod.BACKUP_STALE_MS + 1
    snapshot = {
        "accounts": [],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
        "backup_recipient_configured": True,
        "last_backup_ts": 10 * _HOUR_MS,
    }
    problems = evaluate(snapshot, now_ms=now_ms)
    assert [p.key for p in problems] == ["backup_stale"]


# --- антиспам -------------------------------------------------------------------------------------------


def test_antispam_new_problem_notifies_immediately() -> None:
    notified = []
    state = apply_antispam(
        [Problem("core_down", "core недоступен")], {}, now_ms=0, notifier=lambda t, x: notified.append((t, x))
    )
    assert len(notified) == 1
    assert "core_down" in state["problems"]


def test_antispam_lingering_problem_10min_is_silent() -> None:
    notified = []
    state = apply_antispam(
        [Problem("core_down", "x")], {}, now_ms=0, notifier=lambda t, x: notified.append(x)
    )
    assert len(notified) == 1
    state = apply_antispam(
        [Problem("core_down", "x")], state, now_ms=10 * 60 * 1000, notifier=lambda t, x: notified.append(x)
    )
    assert len(notified) == 1  # 10 минут < 30 -- молчим


def test_antispam_lingering_problem_31min_reminds() -> None:
    notified = []
    state = apply_antispam(
        [Problem("core_down", "x")], {}, now_ms=0, notifier=lambda t, x: notified.append(x)
    )
    state = apply_antispam(
        [Problem("core_down", "x")], state, now_ms=31 * 60 * 1000, notifier=lambda t, x: notified.append(x)
    )
    assert len(notified) == 2  # 31 минута >= 30 -- напоминаем


def test_antispam_resolved_problem_notifies_once() -> None:
    notified = []
    state = apply_antispam(
        [Problem("core_down", "x")], {}, now_ms=0, notifier=lambda t, x: notified.append(x)
    )
    state = apply_antispam([], state, now_ms=1000, notifier=lambda t, x: notified.append(x))
    assert notified[-1] == "восстановилось"
    assert state["problems"] == {}

    # Проблема пропала -- второй пустой прогон не должен повторно уведомлять.
    before = len(notified)
    apply_antispam([], state, now_ms=2000, notifier=lambda t, x: notified.append(x))
    assert len(notified) == before


def test_antispam_broken_state_file_is_empty_state(tmp_path) -> None:
    state_path = tmp_path / "watch-state.json"
    state_path.write_text("не json{{{", encoding="utf-8")
    assert watch_mod._load_state(state_path) == {}  # noqa: SLF001 -- прямая проверка приватного помощника


# --- hub watch: мёртвый core, notify-send отсутствует ---------------------------------------------------


def test_run_watch_core_down_returns_0_and_notifies_core_down(tmp_path) -> None:
    notified = []
    code = watch_mod.run_watch(
        port=1,  # ничего не слушает
        data_dir=tmp_path,
        config_dir=tmp_path,
        admin_secret="",
        now_ms=1000,
        notifier=lambda title, text: notified.append((title, text)),
    )
    assert code == 0
    assert any("core_down" in title for title, _text in notified)


def test_notify_send_missing_binary_goes_to_stderr_exit_0(monkeypatch, capsys) -> None:
    monkeypatch.setattr(watch_mod.shutil, "which", lambda _name: None)
    watch_mod.notify_send("title", "text")  # настоящий notify-send НЕ зовём -- бинарник «не найден»
    captured = capsys.readouterr()
    assert "title" in captured.err
    assert "text" in captured.err


# --- поправки после opus-критика куска 7 --------------------------------------------------------------


def test_core_down_does_not_announce_other_problems_as_recovered() -> None:
    """Находка 6: при `core_down` о прочих проблемах ничего не известно — «восстановилось» по ним не шлём,
    а по возвращении core не шлём их заново."""
    sent: list[tuple[str, str]] = []

    def _notifier(title: str, text: str) -> None:
        sent.append((title, text))

    problems = [watch_mod.Problem("dead_letter", "3 событий"), watch_mod.Problem("outbox_unknown", "1")]
    state = watch_mod.apply_antispam(problems, {}, 0, notifier=_notifier)
    assert len(sent) == 2

    sent.clear()
    down = watch_mod.evaluate({"core_down": True}, 120_000)
    state = watch_mod.apply_antispam(down, state, 120_000, notifier=_notifier, partial=True)
    assert [title for title, _ in sent] == ["hub: core_down"]
    assert set(state["problems"]) == {"core_down", "dead_letter", "outbox_unknown"}

    sent.clear()
    state = watch_mod.apply_antispam(problems, state, 240_000, notifier=_notifier)
    assert sent == [("hub: core_down", "восстановилось")]
    assert set(state["problems"]) == {"dead_letter", "outbox_unknown"}


def test_unauthorized_socket_is_its_own_problem_not_core_down() -> None:
    """Находка 7: 401 от сокета при живом healthz — не «core недоступен»."""
    problems = watch_mod.evaluate({"unauthorized": True}, 0)
    assert [p.key for p in problems] == ["watch_unauthorized"]


def test_1a_crit4_no_silent_before_first_heartbeat() -> None:
    """Аккаунт заведён `hub auth tg` до входа и включения юнита — пульса ещё не было. Раньше это сразу давало
    `silent` и повторялось всё время входа (находка 4а критика 1a)."""
    snapshot = {
        "accounts": [_account(status="ok", last_ok_ts=None, has_connector_secret=True)],
        "dead_letter_total": 0,
        "outbox_unknown": 0,
    }
    assert evaluate(snapshot, now_ms=10**9) == []
