"""Внутренний сокет для тредов Slack (спека `docs/architecture.md`/H):
`GET /channels/heads` (`max_ext_id`), `GET /channels/window?thread_ext_id=`, `GET /threads/heads`."""

from .conftest import connector_headers, insert_channel, insert_message, insert_thread, make_connector_account

# --- Тест 1 (частично): heads.max_ext_id — последний top_level, ответ его не двигает ------------------------


async def test_channels_heads_max_ext_id_ignores_replies(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="slack", db_name="work")
    ch = insert_channel(db, account_id, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="100.000000", ts=1000)
    insert_message(db, ch, ext_id="200.000000", ts=2000, thread_ext_id="100.000000")  # ответ, позже родителя

    r = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    assert r.status_code == 200, r.text
    head = next(h for h in r.json() if h["ext_id"] == "c1")
    assert head["max_ext_id"] == "100.000000"  # ответ не двигает курсор top_level


async def test_channels_heads_max_ext_id_broadcast_counts_as_top_level(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="slack", db_name="work")
    ch = insert_channel(db, account_id, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="100.000000", ts=1000)
    insert_message(
        db, ch, ext_id="200.000000", ts=2000, thread_ext_id="100.000000", thread_broadcast=1
    )

    r = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    head = next(h for h in r.json() if h["ext_id"] == "c1")
    assert head["max_ext_id"] == "200.000000"  # broadcast — top_level


# --- Тест 7: окно по умолчанию без ответов; ?thread_ext_id= — окно треда; Telegram-окно не изменилось -------


async def test_channels_window_default_excludes_replies(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="slack", db_name="work")
    ch = insert_channel(db, account_id, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")

    r = await internal_client.get(
        "/internal/v1/channels/window", params={"ext_id": "c1"}, headers=connector_headers(secret)
    )
    assert r.status_code == 200, r.text
    assert r.json() == ["P1"]


async def test_channels_window_thread_ext_id_returns_only_replies(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="slack", db_name="work")
    ch = insert_channel(db, account_id, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    insert_message(db, ch, ext_id="R2", ts=1002, thread_ext_id="P1")

    r = await internal_client.get(
        "/internal/v1/channels/window",
        params={"ext_id": "c1", "thread_ext_id": "P1"},
        headers=connector_headers(secret),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"R1", "R2"}


async def test_telegram_window_unchanged(internal_client, db) -> None:
    """Регресс: Telegram (без thread_ext_id вообще) видит то же самое окно, что раньше."""
    account_id, secret = make_connector_account(db, source="tg", db_name="main")
    ch = insert_channel(db, account_id, ext_id="c1", db_name="main")
    insert_message(db, ch, ext_id="98", ts=1000)
    insert_message(db, ch, ext_id="99", ts=1001)

    r = await internal_client.get(
        "/internal/v1/channels/window", params={"ext_id": "c1"}, headers=connector_headers(secret)
    )
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"98", "99"}


# --- Тест 11: threads/heads — только свой аккаунт, фильтр по времени, parent_known -------------------------


async def test_threads_heads_own_account_only(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="slack", db_name="work")
    other_id, _other_secret = make_connector_account(db, source="slack", db_name="work")

    ch = insert_channel(db, account_id, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    insert_message(db, ch, ext_id="R1", ts=2000, thread_ext_id="P1")
    insert_thread(db, ch, ext_id="P1", last_reply_ts=2000, subscribed=1, first_seen_ts=500)

    other_ch = insert_channel(db, other_id, ext_id="oc1", db_name="work")
    insert_message(db, other_ch, ext_id="OP1", ts=1000)
    insert_thread(db, other_ch, ext_id="OP1", last_reply_ts=2000, subscribed=1, first_seen_ts=500)

    r = await internal_client.get(
        "/internal/v1/threads/heads", params={"since_ms": 0}, headers=connector_headers(secret)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["channel_ext_id"] == "c1"
    assert body[0]["thread_ext_id"] == "P1"
    assert body[0]["max_reply_ext_id"] == "R1"
    assert body[0]["parent_known"] is True
    assert body[0]["subscribed"] is True


async def test_threads_heads_time_filter_and_parent_known_false(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="slack", db_name="work")
    ch = insert_channel(db, account_id, ext_id="c1", db_name="work")
    # тред без родителя в ядре (parent_known=false — коннектор обязан догрузить родителя)
    insert_message(db, ch, ext_id="R1", ts=5000, thread_ext_id="ORPHAN")
    insert_thread(db, ch, ext_id="ORPHAN", last_reply_ts=5000, subscribed=0, first_seen_ts=4000)

    # старый тред, ничего свежего — не должен попасть при высоком since_ms
    old_ch = insert_channel(db, account_id, ext_id="c2", db_name="work")
    insert_message(db, old_ch, ext_id="OLD_P", ts=100)
    insert_thread(db, old_ch, ext_id="OLD_P", last_reply_ts=200, subscribed=0, first_seen_ts=50)

    r = await internal_client.get(
        "/internal/v1/threads/heads", params={"since_ms": 1000}, headers=connector_headers(secret)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ext_ids = {t["thread_ext_id"] for t in body}
    assert ext_ids == {"ORPHAN"}
    orphan = next(t for t in body if t["thread_ext_id"] == "ORPHAN")
    assert orphan["parent_known"] is False
