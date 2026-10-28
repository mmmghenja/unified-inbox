"""Ф85-1 (C.1): виды отказа `refusal` и `transient` — в `KINDS` с подписями; повод — только у агента владельца."""

from hub.engine import failure


def test_new_kinds_and_reasons():
    assert {"refusal", "transient"} <= set(failure.KINDS)
    assert failure.label("refusal") == "модель отказалась"
    assert failure.label("transient") == "временный сбой"
    assert failure.AGENT_REASONS == failure.BACKGROUND_REASONS | {"refusal", "transient"}
    assert not {"refusal", "transient"} & failure.BACKGROUND_REASONS
    assert failure.SESSION_REASONS == frozenset({"limit", "balance"})
    # Явный вид драйвера проходит как есть; классификатор текста их сам не ставит.
    assert failure.classify("opencode", {"status": "rejected", "kind": "refusal"}) == "refusal"
    assert failure.classify("claude", "I can't help with that") == "other"
