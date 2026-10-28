"""Ф38-3 B (docs/architecture.md): строка беседы в `GET /projects/{id}/channels` несёт
`account_id` — по нему hub-wake сверяет беседы сводки с фильтрами подписок по аккаунтам. Поле — то же, что у
`GET /channels/{id}`; беседы обоих файлов (main и work)."""

from .conftest import insert_account, insert_channel, insert_message, login_via_code


async def test_project_channels_rows_carry_account_id(client, db, clock):
    acc_main = insert_account(db)
    acc_work = insert_account(db, source="slack", db_name="work")
    ch_main = insert_channel(db, acc_main, ext_id="m1")
    ch_work = insert_channel(db, acc_work, ext_id="w1", db_name="work")
    insert_message(db, ch_main, ext_id="1", ts=1000, text="а")
    insert_message(db, ch_work, ext_id="1", ts=2000, text="б")

    await login_via_code(client, db, clock)

    r = await client.get("/api/v1/projects/-1/channels")
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.json()}
    assert by_id[ch_main]["account_id"] == acc_main
    assert by_id[ch_work]["account_id"] == acc_work
    # Соседние поля строки не сдвинулись (столбец добавлен в конец).
    assert by_id[ch_work]["source"] == "slack"
    assert by_id[ch_main]["stage"] is None
    for cid in (ch_main, ch_work):
        one = await client.get(f"/api/v1/channels/{cid}")
        assert one.status_code == 200
        assert one.json()["account_id"] == by_id[cid]["account_id"]
