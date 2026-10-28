"""Ф10-5, часть B (ядро): беседы уведомлений трекеров с почты — внимание (B1), запрет ответа (B2), поля
`can_reply`/`tracker` беседы наружу (B3). Спецификация: docs/architecture.md,
тесты B-T1…B-T4.

Контракт с коннектором (часть A) — только `attrs` беседы (`tracker`, `key`, `url`, `board`?, `no_reply: true`)
и виды `issue`/`card`; события подаются через `ingest.apply_batch`, как от коннектора.
"""

import pytest

from hub.core import admin, attention, ingest, outbox, queries, security
from hub.core import db as db_module

from .conftest import (
    csrf_headers,
    insert_account,
    insert_project,
    login_via_code,
    make_bearer_token,
)

UI = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)

JIRA_ATTRS = {
    "tracker": "jira",
    "key": "WEB-39",
    "url": "https://acme.atlassian.net/browse/WEB-39",
    "no_reply": True,
}
TRELLO_ATTRS = {
    "tracker": "trello",
    "key": "Tr1CardA",
    "url": "https://trello.com/c/Tr1CardA",
    "board": "Тест — команда",
    "no_reply": True,
}


def _ingest(db, account_id: int, events: list[dict], now_ms: int = 10_000) -> None:
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0, result


def _msg(
    chat: str, ext_id: str, ts: int, *, kind: str, attrs: dict | None, mentions_me: bool = False
) -> dict:
    channel: dict = {"ext_id": chat, "kind": kind, "title": f"беседа {chat}", "own_id_space": True}
    if attrs is not None:
        channel["attrs"] = attrs
    return {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "sender": {"ext_id": "trello:анна тестова", "name": "Анна Тестова"},
        "is_me": False,
        "kind": "text",
        "text": f"уведомление {ext_id}",
        "mentions_me": mentions_me,
    }


def _channel_id(db, chat: str) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.channel WHERE ext_id = ?", (chat,)).fetchone()[0]


def _set_project(db, channel_id: int, project_id: int | None) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {db_module.db_of(channel_id)}.channel SET project_id = ? WHERE id = ?",  # noqa: S608
            (project_id, channel_id),
        )
    )


def _policy(db, project_id: int, changes: dict) -> None:
    db.write_sync(lambda conn: admin.set_project_attention(conn, project_id, changes=changes))


def _channel(db, channel_id: int) -> dict:
    with db.reader() as conn:
        return queries.get_channel(conn, UI, channel_id)


def _messages(db, channel_id: int) -> dict[str, dict]:
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, channel_id, before=None, limit=200)
    return {m["ext_id"]: m for m in page.messages}


# --- B-T1: effective_mode — `default` проекта не действует на задачи трекеров -----------------------------


@pytest.mark.parametrize("kind", ["card", "issue", "task"])
def test_b_t1_task_kinds_ignore_project_default(kind):
    react_default = '{"attention": {"default": "react"}}'
    assert attention.effective_mode(0, 0, None, 5, kind, None, react_default) == "info"
    assert attention.effective_mode(0, 0, None, 5, kind, None, None) == "info"
    # явный ключ вида — действует
    assert (
        attention.effective_mode(0, 0, None, 5, kind, None, f'{{"attention": {{"{kind}": "react"}}}}')
        == "react"
    )
    # Неразобранное — info (Ф57-1), заглушено — muted, ручной — ручной
    assert attention.effective_mode(0, None, None, None, kind, None, react_default) == "info"
    assert attention.effective_mode(1, 0, None, 5, kind, None, react_default) == "muted"
    assert attention.effective_mode(0, 1, "react", 5, kind, None, react_default) == "muted"
    assert attention.effective_mode(0, 0, "react", 5, kind, None, None) == "react"


def test_b_t1_non_task_kinds_still_follow_default():
    react_default = '{"attention": {"default": "react"}}'
    assert attention.effective_mode(0, 0, None, 5, "mail_thread", None, react_default) == "react"
    assert attention.effective_mode(0, 0, None, 5, "group", None, react_default) == "react"
    assert (
        attention.effective_mode(0, 0, None, 5, "mail_thread", None, '{"attention": {"default": "info"}}')
        == "info"
    )


def test_b_t1_through_sql_and_project_set_card_react(db):
    """Та же функция через SQLite: Umbrella (`default=react`) — карточка «к сведению», пока нет `card=react`;
    упоминание в карточке поднимает сообщение до «реагирую» (B-T2 через выдачу)."""
    acc = insert_account(db, source="mail")
    pid = insert_project(db, name="Umbrella")
    _ingest(
        db,
        acc,
        [
            _msg("trello:c:Tr1CardA", "<a@x>", 1000, kind="card", attrs=TRELLO_ATTRS),
            _msg("trello:c:Tr1CardA", "<b@x>", 2000, kind="card", attrs=TRELLO_ATTRS, mentions_me=True),
        ],
    )
    cid = _channel_id(db, "trello:c:Tr1CardA")
    _set_project(db, cid, pid)
    _policy(db, pid, {"default": "react"})
    assert _channel(db, cid)["attention"] == "info"
    got = _messages(db, cid)
    assert got["<a@x>"]["attention"] == "info"
    assert got["<b@x>"]["attention"] == "react"
    assert _channel(db, cid)["unread_react"] == 1
    _policy(db, pid, {"card": "react"})
    assert _channel(db, cid)["attention"] == "react"


# --- B-T2: message_attention — «для меня» поднимает карточку, почту — нет -----------------------------------


def test_b_t2_message_attention_for_me():
    assert attention.message_attention("info", 1, "card") == "react"
    assert attention.message_attention("info", 1, "issue") == "react"
    assert attention.message_attention("info", 1, "mail_thread") == "info"
    assert attention.message_attention("info", 0, "card") == "info"


# --- B-T3: отправка и черновик в беседу трекера — отказ; обычная почта — как раньше ------------------------


def _outbox_row(db, outbox_id: int) -> tuple:
    with db.reader() as conn:
        return conn.execute("SELECT status, text FROM main.outbox WHERE id = ?", (outbox_id,)).fetchone()


async def test_b_t3_send_and_draft_refused_in_tracker_channel(client, db, clock):
    acc = insert_account(db, source="mail")
    _ingest(
        db,
        acc,
        [
            _msg("jira:acme.atlassian.net:WEB-39", "<j@x>", 1000, kind="issue", attrs=JIRA_ATTRS),
            _msg("12345", "<m@x>", 1000, kind="mail_thread", attrs={"labels": ["INBOX"]}),
        ],
    )
    tracker, mail = _channel_id(db, "jira:acme.atlassian.net:WEB-39"), _channel_id(db, "12345")
    # bearer (ручка агента / MCP hub_draft) — до входа UI: у клиента ещё нет куки
    token = make_bearer_token(db, scopes=("read", "draft"))
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": tracker, "text": "черновик", "idempotency_key": "b-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    assert "трекер" in r.json()["detail"]

    cookie, _ = await login_via_code(client, db, clock)

    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": tracker, "text": "ответ", "idempotency_key": "ui-1"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 422
    assert "трекер" in r.json()["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0

    # обычная почтовая беседа — как раньше
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": mail, "text": "ответ", "idempotency_key": "ui-2"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


async def test_b_t3_approve_pending_draft_refused_after_channel_became_tracker(client, db, clock):
    """Черновик положен, пока беседа была обычной; затем коннектор пометил её `no_reply` — `approve`
    отказывает, строка остаётся `pending_approval` с прежним текстом."""
    acc = insert_account(db, source="mail")
    chat = "jira:acme.atlassian.net:WEB-39"
    _ingest(db, acc, [_msg(chat, "<j1@x>", 1000, kind="issue", attrs={"tracker": "jira"})])
    cid = _channel_id(db, chat)
    token = make_bearer_token(db, scopes=("read", "draft"))
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": cid, "text": "черновик", "idempotency_key": "b-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    outbox_id = r.json()["outbox_id"]
    assert _outbox_row(db, outbox_id) == ("pending_approval", "черновик")

    _ingest(db, acc, [_msg(chat, "<j2@x>", 2000, kind="issue", attrs=JIRA_ATTRS)], now_ms=20_000)
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/outbox/{outbox_id}/approve", json={"text": "новый"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422
    assert "трекер" in r.json()["detail"]
    assert _outbox_row(db, outbox_id) == ("pending_approval", "черновик")

    with pytest.raises(outbox.OutboxError):
        db.write_sync(lambda conn: outbox.approve(conn, outbox_id, now_ms=30_000))
    assert _outbox_row(db, outbox_id) == ("pending_approval", "черновик")


def test_b_t3_channel_no_reply_parsing():
    assert outbox.channel_no_reply('{"attrs": {"no_reply": true}}') is True
    assert outbox.channel_no_reply('{"attrs": {"no_reply": "true"}}') is False
    assert outbox.channel_no_reply('{"attrs": {"no_reply": 1}}') is False
    assert outbox.channel_no_reply('{"attrs": []}') is False
    assert outbox.channel_no_reply("{битый") is False
    assert outbox.channel_no_reply(None) is False


def test_f84_notify_channel_has_tracker_and_no_reply():
    """Ф84-1: беседа письма-уведомления по ключу (`attrs.tracker == notify`) — ссылка и `can_reply=false`."""
    import json

    url = "https://www.jobs.example/m?dialogId=2000002"
    attrs = {"tracker": "notify", "key": "2000002", "url": url, "no_reply": True}
    fields = queries._reply_fields(json.dumps({"attrs": attrs}))
    assert fields["can_reply"] is False
    assert fields["tracker"] == {"name": "notify", "key": "2000002", "url": attrs["url"]}


# --- B-T4: `/sync`, список бесед и одна беседа — `can_reply`, `tracker` ------------------------------------


async def test_b_t4_channel_fields_in_list_sync_and_one(client, db, clock):
    acc = insert_account(db, source="mail")
    pid = insert_project(db, name="Initech")
    bad_url = dict(JIRA_ATTRS, key="WEB-40", url="http://acme.atlassian.net/browse/WEB-40")
    long_key = dict(JIRA_ATTRS, key="K" * 65)
    unknown = dict(JIRA_ATTRS, tracker="asana")
    _ingest(
        db,
        acc,
        [
            _msg("jira:a:WEB-39", "<1@x>", 1000, kind="issue", attrs=JIRA_ATTRS),
            _msg("trello:c:Tr1CardA", "<2@x>", 1000, kind="card", attrs=TRELLO_ATTRS),
            _msg("jira:a:WEB-40", "<3@x>", 1000, kind="issue", attrs=bad_url),
            _msg("jira:a:LONG", "<4@x>", 1000, kind="issue", attrs=long_key),
            _msg("jira:a:ASANA", "<5@x>", 1000, kind="issue", attrs=unknown),
            _msg("777", "<6@x>", 1000, kind="mail_thread", attrs={"labels": ["INBOX"]}),
        ],
    )
    ids = {
        name: _channel_id(db, ext)
        for name, ext in [
            ("jira", "jira:a:WEB-39"),
            ("trello", "trello:c:Tr1CardA"),
            ("bad_url", "jira:a:WEB-40"),
            ("long_key", "jira:a:LONG"),
            ("unknown", "jira:a:ASANA"),
            ("mail", "777"),
        ]
    }
    for cid in ids.values():
        _set_project(db, cid, pid)
    expected = {
        "jira": (False, {"name": "jira", "key": "WEB-39", "url": "https://acme.atlassian.net/browse/WEB-39"}),
        "trello": (False, {"name": "trello", "key": "Tr1CardA", "url": "https://trello.com/c/Tr1CardA"}),
        "bad_url": (False, None),
        "long_key": (False, None),
        "unknown": (False, None),
        "mail": (True, None),
    }

    cookie, _ = await login_via_code(client, db, clock)
    listed = {c["id"]: c for c in (await client.get(f"/api/v1/projects/{pid}/channels")).json()}
    synced = {c["id"]: c for c in (await client.get("/api/v1/sync", params={"since": 0})).json()["channels"]}
    for name, cid in ids.items():
        want = expected[name]
        one = (await client.get(f"/api/v1/channels/{cid}")).json()
        for source, got in (("list", listed[cid]), ("sync", synced[cid]), ("one", one)):
            assert (got["can_reply"], got["tracker"]) == want, (name, source)
    assert cookie
