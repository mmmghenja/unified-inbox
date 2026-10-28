import pytest

from .conftest import insert_account, insert_channel, insert_message, login_via_code

XSS_PAYLOADS = [
    "<img src=x onerror=alert(1)>",
    "<svg><script>alert(1)</script></svg>",
    "javascript:alert(1)",
    '"><script>alert(1)</script>',
]


# 8. Пагинация: 5 сообщений с общим ts и числовыми ext_id 98-102 плюс заготовка (ext_id NULL, ts позже) —
#    три страницы по 2 дают все 6 строк ровно по разу в порядке 98,99,100,101,102,заготовка.
async def test_pagination_covers_all_rows_in_order(client, db, clock):
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id="c1")
    for ext_id in ("98", "99", "100", "101", "102"):
        insert_message(db, ch, ext_id=ext_id, ts=1000, text=f"m{ext_id}")
    insert_message(db, ch, ext_id=None, ts=2000, text="черновик")  # заготовка: ext_id NULL

    await login_via_code(client, db, clock)

    pages: list[list[str | None]] = []
    before = None
    for _page in range(3):
        params: dict = {"limit": 2}
        if before is not None:
            params["before"] = before
        r = await client.get(f"/api/v1/channels/{ch}/messages", params=params)
        assert r.status_code == 200
        body = r.json()
        assert len(body["messages"]) == 2
        pages.append([m["ext_id"] for m in body["messages"]])
        before = body["next_before"]

    # страницы идут от новых к старым, внутри страницы — порядок показа (старые сверху)
    assert pages == [["102", None], ["100", "101"], ["98", "99"]]
    assert before is None  # после третьей страницы дальше ничего нет


# 9. XSS-набор в тексте, названии канала и имени отправителя: application/json, nosniff, строки как были.
@pytest.mark.parametrize(("idx", "payload"), list(enumerate(XSS_PAYLOADS)))
async def test_xss_payloads_returned_byte_for_byte(client, db, clock, idx, payload):
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id=f"xss-{idx}", title=payload)
    contact_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.contact (account_id, ext_id, name) VALUES (?, 'sender', ?)", (acc, payload)
            ).lastrowid
        )
    )
    insert_message(db, ch, ext_id="m1", ts=1000, text=payload, sender_contact_id=contact_id)

    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/channels/{ch}/messages")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    msg = r.json()["messages"][0]
    assert msg["text"] == payload
    assert msg["sender"]["name"] == payload

    r2 = await client.get("/api/v1/projects/-1/channels")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("application/json")
    titles = [c["title"] for c in r2.json()]
    assert payload in titles

    r3 = await client.get(f"/api/v1/search?q={payload}")
    assert r3.status_code == 200
    assert r3.headers["content-type"].startswith("application/json")


# 10. Удалённое сообщение (Ф8-1 C1, правило владельца: удалённое отправителем остаётся видно): text
#     отдаётся вместе с deleted: true в ленте канала, /sync и странице треда (и у родителя, и у ответа);
#     поиск удалённое по-прежнему не находит. /healthz без авторизации не содержит ни одного текста/названия.
async def test_deleted_message_keeps_text_and_healthz_is_bare(client, db, clock):
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id="del-ch", title="совершенно секретное название канала")
    insert_message(db, ch, ext_id="d1", ts=1000, text="совершенно секретный текст сообщения", deleted=1)
    insert_message(db, ch, ext_id="d2", ts=1001, text="удалённыйответ", thread_ext_id="d1", deleted=1)

    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/channels/{ch}/messages")
    assert r.status_code == 200
    msg = next(m for m in r.json()["messages"] if m["ext_id"] == "d1")
    assert msg["deleted"] is True
    assert msg["text"] == "совершенно секретный текст сообщения"

    rs = await client.get("/api/v1/sync", params={"since": 0})
    assert rs.status_code == 200
    synced = {m["ext_id"]: m for m in rs.json()["messages"]}
    assert synced["d1"]["deleted"] is True
    assert synced["d1"]["text"] == "совершенно секретный текст сообщения"
    assert synced["d2"]["deleted"] is True
    assert synced["d2"]["text"] == "удалённыйответ"

    rt = await client.get(f"/api/v1/channels/{ch}/threads/d1/messages")
    assert rt.status_code == 200
    page = rt.json()
    assert page["parent"]["deleted"] is True
    assert page["parent"]["text"] == "совершенно секретный текст сообщения"
    reply = next(m for m in page["messages"] if m["ext_id"] == "d2")
    assert reply["deleted"] is True
    assert reply["text"] == "удалённыйответ"

    rq = await client.get("/api/v1/search", params={"q": "удалённыйответ"})
    assert rq.status_code == 200
    assert rq.json() == []

    r2 = await client.get("/api/v1/healthz")
    assert r2.status_code == 200
    assert "секретное" not in r2.text
    assert "секретный" not in r2.text
    assert set(r2.json().keys()) == {"ok", "max_seq", "now"}
