"""Ф14-9 П1 (docs/architecture.md): hub-watch поднимает `remote_tls` — пара https удалённого
слушателя не грузится (вход с телефона не поднят) или до конца сертификата меньше 14 дней (продление не идёт).
`remote_tls: null` (http, пары нет) и далёкий срок — молчание."""

from hub import watch as watch_mod
from hub.watch import evaluate

NOW = 1_800_000_000_000
DAY = 24 * 60 * 60 * 1000


def _snapshot(remote_tls) -> dict:
    return {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0, "remote_tls": remote_tls}


def test_no_pair_and_far_expiry_are_silent():
    assert evaluate(_snapshot(None), now_ms=NOW) == []
    assert evaluate({"accounts": []}, now_ms=NOW) == []
    assert evaluate(_snapshot({"not_after_ms": NOW + 60 * DAY, "error": None}), now_ms=NOW) == []
    assert (
        evaluate(_snapshot({"not_after_ms": NOW + watch_mod.REMOTE_TLS_WARN_MS, "error": None}), now_ms=NOW)
        == []
    )


def test_less_than_14_days_is_a_problem():
    problems = evaluate(_snapshot({"not_after_ms": NOW + 13 * DAY + 1000, "error": None}), now_ms=NOW)
    assert [p.key for p in problems] == ["remote_tls"]
    assert problems[0].text == (
        "сертификат https для телефона: осталось 13 дней — продление не идёт? "
        "journalctl --user -u hub-cert-renew"
    )
    problems = evaluate(_snapshot({"not_after_ms": NOW + 2 * DAY + 1000, "error": None}), now_ms=NOW)
    assert "осталось 2 дня" in problems[0].text
    problems = evaluate(_snapshot({"not_after_ms": NOW + 1 * DAY + 1000, "error": None}), now_ms=NOW)
    assert "осталось 1 день" in problems[0].text


def test_expired_is_a_problem():
    problems = evaluate(_snapshot({"not_after_ms": NOW - DAY, "error": None}), now_ms=NOW)
    assert [p.key for p in problems] == ["remote_tls"]
    assert problems[0].text.startswith("сертификат https для телефона: истёк — ")


def test_error_is_a_problem():
    problems = evaluate(
        _snapshot({"not_after_ms": None, "error": "ключ не от этого сертификата"}), now_ms=NOW
    )
    assert [p.key for p in problems] == ["remote_tls"]
    assert problems[0].text == (
        "https для телефона: ОШИБКА — ключ не от этого сертификата, вход с телефона не поднят"
    )
