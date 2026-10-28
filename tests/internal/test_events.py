"""Тест 4: `/events` — ядовитое событие в пачке (соседи применены, dead=1, курсор сдвинут); пачка > 500 ->
413. Спецификация: docs/architecture.md.
"""

from .conftest import connector_headers, make_connector_account


def _upsert(ext_id: str, text: str) -> dict:
    return {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": ext_id,
        "ts": 1_000,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


async def test_poisoned_event_isolated_neighbours_applied_cursor_moves(internal_client, db) -> None:
    _account_id, secret = make_connector_account(db)
    events = [
        _upsert("1", "hello"),
        {"type": "upsert", "channel": {"ext_id": "c1", "kind": "dm"}},  # ядовитое: нет обязательных полей
        _upsert("2", "world"),
    ]
    r = await internal_client.post(
        "/internal/v1/events",
        headers=connector_headers(secret),
        json={"events": events, "cursor": {"value": "cur-1", "ts": 900}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 2
    assert body["dead"] == 1

    cur = await internal_client.get("/internal/v1/cursor", headers=connector_headers(secret))
    assert cur.status_code == 200
    assert cur.json() == {"value": "cur-1", "ts": 900}


async def test_batch_over_500_events_rejected_with_413(internal_client, db) -> None:
    _account_id, secret = make_connector_account(db)
    events = [_upsert(str(i), "x") for i in range(501)]
    r = await internal_client.post(
        "/internal/v1/events", headers=connector_headers(secret), json={"events": events}
    )
    assert r.status_code == 413
