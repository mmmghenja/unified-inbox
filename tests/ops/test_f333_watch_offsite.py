"""Ф33-3: `hub watch` тревожит «копия вне дома старше 6 часов» по меткам `scripts/hub-offsite-backup.sh`
(`<data_dir>/offsite-backup.json`); без файла меток (копия вне дома не заведена) — молчит."""

import json

from hub.watch import OFFSITE_STALE_MS, OFFSITE_STATE_NAME, Problem, _offsite_snapshot, evaluate

NOW = 10**12
STALE = Problem("offsite_backup_stale", "копия вне дома старше 6 часов")


def test_no_state_file_is_silent(tmp_path) -> None:
    snap = _offsite_snapshot(tmp_path)
    assert snap == {"offsite_state_present": False}
    assert evaluate(snap, NOW) == []


def test_fresh_offsite_copy_is_silent() -> None:
    snap = {"offsite_state_present": True, "offsite_ok_ts": NOW - OFFSITE_STALE_MS, "offsite_since_ts": 0}
    assert evaluate(snap, NOW) == []  # ровно 6 ч — ещё не проблема


def test_old_offsite_copy_is_a_problem() -> None:
    snap = {"offsite_state_present": True, "offsite_ok_ts": NOW - OFFSITE_STALE_MS - 1, "offsite_since_ts": 0}
    assert evaluate(snap, NOW) == [STALE]


def test_never_copied_counts_from_first_run() -> None:
    young = {"offsite_state_present": True, "offsite_ok_ts": None, "offsite_since_ts": NOW - 1000}
    assert evaluate(young, NOW) == []
    old = {
        "offsite_state_present": True,
        "offsite_ok_ts": None,
        "offsite_since_ts": NOW - OFFSITE_STALE_MS - 1,
    }
    assert evaluate(old, NOW) == [STALE]


def test_state_file_is_read(tmp_path) -> None:
    (tmp_path / OFFSITE_STATE_NAME).write_text(
        json.dumps({"since_ts": 5, "local_ok_ts": 7, "offsite_ok_ts": 6}), encoding="utf-8"
    )
    assert _offsite_snapshot(tmp_path) == {
        "offsite_state_present": True,
        "offsite_ok_ts": 6,
        "offsite_since_ts": 5,
    }


def test_broken_state_file_alarms(tmp_path) -> None:
    (tmp_path / OFFSITE_STATE_NAME).write_text("{не json", encoding="utf-8")
    snap = _offsite_snapshot(tmp_path)
    assert snap["offsite_state_present"] is True
    assert evaluate(snap, NOW) == [STALE]


def test_offsite_disabled_by_setting_is_silent(tmp_path) -> None:
    (tmp_path / OFFSITE_STATE_NAME).write_text(
        json.dumps({"since_ts": 5, "local_ok_ts": 7, "offsite_repo": None}), encoding="utf-8"
    )
    snap = _offsite_snapshot(tmp_path)
    assert snap == {"offsite_state_present": False}
    assert evaluate(snap, NOW) == []
