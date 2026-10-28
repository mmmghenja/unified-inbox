"""Ф14-6 E (docs/architecture.md): hub-watch поднимает `transcribe_stalled`, если старейшая
выдаваемая строка очереди расшифровки старше 30 минут, — только когда служба заведена (токен
`transcribe:worker` жив)."""

from hub import watch as watch_mod
from hub.watch import evaluate

NOW = 10_000_000_000


def _snapshot(**transcribe) -> dict:
    base = {"configured": True, "queued": 1, "working": 0, "failed_24h": 0, "oldest_queued_ts": None}
    base.update(transcribe)
    return {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0, "transcribe": base}


def test_stalled_queue_is_a_problem():
    oldest = NOW - watch_mod.TRANSCRIBE_STALLED_MS - 60_000
    problems = evaluate(_snapshot(oldest_queued_ts=oldest), now_ms=NOW)
    assert [p.key for p in problems] == ["transcribe_stalled"]
    assert "31 мин" in problems[0].text


def test_boundary_and_fresh_queue_silent():
    assert evaluate(_snapshot(oldest_queued_ts=NOW - watch_mod.TRANSCRIBE_STALLED_MS), now_ms=NOW) == []
    assert evaluate(_snapshot(oldest_queued_ts=None), now_ms=NOW) == []


def test_not_configured_is_silent():
    old = NOW - 10 * watch_mod.TRANSCRIBE_STALLED_MS
    assert evaluate(_snapshot(configured=False, oldest_queued_ts=old), now_ms=NOW) == []
