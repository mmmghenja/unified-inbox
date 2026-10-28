"""Ф56-1в (2.4): снимок фоновых задач сессии хаба несёт `wake_at_ts` — объявленное пробуждение движка.

`bg_sets` пишет ключ, только когда пробуждение есть (снимок без него — прежнего вида); `bg_of` отдаёт его;
снимок с пробуждением и без задач — не «задач нет»; у `asleep` снимок не пишется."""

import json
from types import SimpleNamespace

from hub.core import hub_sessions


def _rec(bg_json=None):
    return SimpleNamespace(bg_json=bg_json)


def test_bg_sets_keeps_wake_at_without_items():
    sets = hub_sessions.bg_sets(_rec(), "idle", {"items": [], "wake_at_ts": 1_700_000_000_000})
    assert json.loads(sets["bg_json"]) == {"items": [], "wake_at_ts": 1_700_000_000_000}
    rec = _rec(sets["bg_json"])
    assert hub_sessions.bg_of(rec) == {"items": [], "wake_at_ts": 1_700_000_000_000}
    # Тот же снимок — без изменения; пробуждение снято — ключа нет, столбец меняется.
    assert hub_sessions.bg_sets(rec, "idle", {"items": [], "wake_at_ts": 1_700_000_000_000}) == {}
    cleared = hub_sessions.bg_sets(rec, "idle", {"items": [], "wake_at_ts": None})
    assert json.loads(cleared["bg_json"]) == {"items": []}
    assert hub_sessions.bg_of(_rec(cleared["bg_json"])) == {"items": []}


def test_bg_sets_without_wake_at_as_before():
    assert hub_sessions.bg_sets(_rec(), "idle", {"items": []}) == {}
    assert hub_sessions.bg_sets(_rec(), "idle", {"items": [], "wake_at_ts": None}) == {}
    assert hub_sessions.bg_sets(_rec(), "asleep", {"items": [], "wake_at_ts": 5}) == {}
    # Кривое значение (не целое) — как нет пробуждения.
    assert hub_sessions.bg_sets(_rec(), "idle", {"items": [], "wake_at_ts": True}) == {}
    assert hub_sessions.bg_of(_rec(json.dumps({"items": [], "wake_at_ts": "скоро"}))) == {"items": []}
