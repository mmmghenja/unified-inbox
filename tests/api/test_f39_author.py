"""Ф39 B (docs/architecture.md): автор черновика сессии.

- B2: автор пишется строке, созданной запросом с `session` (роль проекта беседы, вторая линия роли, агент вне
  проектов, без роли, без подписки, мёртвый процесс, повтор по ключу); `POST /drafts/new` с `session`.
- B3: `outbox_author_role` в ленте, `/sync`, поиске, `channels/resolve`; `author_role` у строк outbox.
- B4 + П7: `heard` — чем сессия слышит беседу, все пять значений и отказы.
- B5 + П3–П6: исходы — по ветке на каждый; сигнал — линии роли после перезапуска сессии, один раз, не чужой
  линии, безрольному — только той же подписке, вне проектов — только из той же папки, старше суток — нет,
  клиенту протокола 4 не считается и не выдаётся.
- B1: миграция 0055 (оба файла).

Обвязка — `test_f37_autosend.py`: публичное приложение через ASGITransport, `/proc` подменён словарём, часы —
`FakeClock`, владелец — своим клиентом с cookie."""

import json

import pytest

from hub.core import db as db_module
from hub.core import draft_outcomes, outbox, subs
from hub.core.db import Db, db_of

from . import test_f37_autosend as base
from .conftest import insert_channel, insert_message
from .test_f37_autosend import (
    MIN,
    ROLE,
    A,
    B,
    _allow,
    _audits,
    _h,
    _now,
    _row,
    _sess,
)

procs = base.procs  # фикстуры
ui = base.ui

C = (6161, 999_000, "sess-cccccccc-3333")  # pid из таблицы `procs` обвязки (там он у сессии OpenCode)
D = (7171, 111_000, "sess-dddddddd-4444")
A2 = (8181, 222_000, "sess-aaaaaaaa-9999")  # новая сессия той же роли (перезапуск)
ADMIN = "сисадмин"
HOUR = 60 * MIN


@pytest.fixture
def world(db, tmp_path, clock, procs):
    procs[D[0]] = D[1]
    procs[A2[0]] = A2[1]
    w = base.world.__wrapped__(db, tmp_path, clock, procs)
    for name in ("free", "free2"):
        (tmp_path / name).mkdir()
    return {**w, "free": str(tmp_path / "free"), "free2": str(tmp_path / "free2")}


async def _sub(client, w, who, *, role=ROLE, cwd=None, expect=200, **extra) -> dict:
    return await base._sub(client, w, who, role=role, cwd=cwd, expect=expect, **extra)


async def _sub_id(client, w, who, **kw) -> int:
    return (await _sub(client, w, who, **kw))["sub"]["id"]


async def _send(client, w, who=A, *, key="k1", **kw):
    return await base._send(client, w, who, key=key, **kw)


async def _draft(client, w, who=A, *, key="k1", **kw) -> int:
    r = await _send(client, w, who, key=key, **kw)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    return r.json()["outbox_id"]


async def _end(client, w, who, sub_id):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/end",
        json={"session": _sess(who), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text


async def _approve(ui, outbox_id, **body):
    r = await ui.post(f"/api/v1/outbox/{outbox_id}/approve", body)
    assert r.status_code == 200, r.text
    return r.json()


async def _cancel(ui, outbox_id) -> str:
    r = await ui.post(f"/api/v1/outbox/{outbox_id}/cancel", {})
    assert r.status_code == 200, r.text
    return r.json()["status"]


def _author(db, outbox_id) -> tuple:
    row = _row(db, outbox_id)
    return row["author_role"], row["author_sub_id"]


def _outcomes(db) -> list[tuple]:
    """`(outbox_id, author_sub_id, project_id, role_key, role_no, outcome, signaled?)` всех исходов."""
    with db.reader() as conn:
        return conn.execute(
            "SELECT outbox_id, author_sub_id, project_id, role_key, role_no, outcome, "
            "signaled_ts IS NOT NULL FROM main.draft_outcome ORDER BY id"
        ).fetchall()


def _count(db, sub_id, clock) -> int:
    with db.reader() as conn:
        return draft_outcomes.signal_count(conn, subs.get_sub(conn, sub_id), now_ms=_now(clock))


def _take(db, sub_id, clock, *, limit=20) -> list[dict]:
    def _fn(conn):
        return draft_outcomes.take_signals(
            conn,
            subs.get_sub(conn, sub_id),
            now_ms=_now(clock),
            limit=limit,
            api_token_id=1,
            client_name=None,
            request_id="r",
        )

    return db.write_sync(_fn)


async def _wait(client, w, sub_id, who, *, proto=5) -> list[dict]:
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _sess(who), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers={**_h(w["tok"]), "X-Hub-Sub-Wait": str(proto)},
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


# --- B2: кто автор ------------------------------------------------------------------------------------------


async def test_author_is_role_of_channel_project(client, db, world):
    sub_id = await _sub_id(client, world, A)
    outbox_id = await _draft(client, world)
    assert _author(db, outbox_id) == (ROLE, sub_id)
    assert _row(db, outbox_id)["auto_role"] is None


async def test_author_second_line_has_number(client, db, world):
    await _sub(client, world, A)
    second = await _sub(client, world, B)
    assert second["sub"]["role_no"] == 2
    outbox_id = await _draft(client, world, B)
    assert _author(db, outbox_id) == (f"{ROLE} 2", second["sub"]["id"])


async def test_author_free_agent_outside_projects(client, db, world):
    sub = await _sub(client, world, C, role=ADMIN, cwd=world["free"], mode="direct")
    assert sub["sub"]["project_id"] == subs.NO_PROJECT_ID
    outbox_id = await _draft(client, world, C)
    assert _author(db, outbox_id) == (ADMIN, sub["sub"]["id"])


async def test_author_sub_of_other_project_when_no_better(client, db, world):
    """Подписка только на другой проект — автор всё равно она (подпись — сведение, прав не даёт)."""
    sub_id = await _sub_id(client, world, A, role="аналитик", cwd=world["dir4"])
    outbox_id = await _draft(client, world)
    assert _author(db, outbox_id) == ("аналитик", sub_id)


async def test_author_without_role_keeps_sub_id(client, db, world):
    sub_id = await _sub_id(client, world, A, role=None)
    outbox_id = await _draft(client, world)
    assert _author(db, outbox_id) == (None, sub_id)


async def test_author_empty_without_session_sub_or_process(client, db, world, procs):
    await _sub(client, world, A)
    no_session = await _draft(client, world, None, key="k-none")
    no_sub = await _draft(client, world, B, key="k-b")
    del procs[A[0]]  # процесса автора уже нет — подписку ещё не сняли
    dead = await _draft(client, world, A, key="k-dead")
    assert [_author(db, i) for i in (no_session, no_sub, dead)] == [(None, None)] * 3


async def test_author_not_rewritten_on_idempotent_repeat(client, db, world):
    first = await _send(client, world, key="same")
    assert first.json()["created"] is True
    await _sub(client, world, A)
    again = await _send(client, world, key="same")
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["outbox_id"] == first.json()["outbox_id"]
    assert _author(db, first.json()["outbox_id"]) == (None, None)


async def test_author_and_auto_role_together_when_sent_at_once(client, ui, db, world):
    sub_id = await _sub_id(client, world, A)
    await _allow(ui, world["ch"])
    r = await _send(client, world)
    assert r.json()["status"] == "queued" and r.json()["auto"] == {"role": ROLE}
    row = _row(db, r.json()["outbox_id"])
    assert (row["auto_role"], row["author_role"], row["author_sub_id"]) == (ROLE, ROLE, sub_id)


async def test_author_session_writes_author_and_never_sends_at_once(client, ui, db, world):
    """Приёмка B2: `author_session` — сессия черновика, который она сама просит подтвердить (`confirm=True`
    в MCP). Автор и `heard` пишутся, исход автору приходит, но авто-отправка эту сессию не видит — и в беседе
    «без подтверждения» строка остаётся черновиком. UI и пара с `session` — 422."""
    sub_id = await _sub_id(client, world, A)
    await _allow(ui, world["ch"])
    r = await _send(client, world, None, author_session=_sess(A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval" and "auto" not in body and "auto_denied" not in body
    assert body["heard"] in draft_outcomes.HEARD
    row = _row(db, body["outbox_id"])
    assert (row["auto_role"], row["author_role"], row["author_sub_id"]) == (None, ROLE, sub_id)
    assert _audits(db, "outbox_autosend") == []
    await _approve(ui, body["outbox_id"])
    assert [(o[0], o[5]) for o in _outcomes(db)] == [(body["outbox_id"], "approved")]

    r = await _send(client, world, A, key="k2", author_session=_sess(A))
    assert r.status_code == 422 and "author_session" in r.text
    r = await ui.post(
        "/api/v1/messages/send",
        {"channel_id": world["ch"], "text": "от владельца", "idempotency_key": "k3", "author_session": _sess(A)},
    )
    assert r.status_code == 422 and "author_session" in r.text


async def test_drafts_new_with_session_writes_author(client, db, world):
    sub_id = await _sub_id(client, world, A)
    body = {
        "account_id": world["acc"],
        "project_id": world["p3"],
        "to": "@petr_ivanov",
        "text": "Здравствуйте!",
        "idempotency_key": "new-1",
        "session": _sess(A),
    }
    r = await client.post("/api/v1/drafts/new", json=body, headers=_h(world["tok"]))
    assert r.status_code == 200, r.text
    assert _author(db, r.json()["outbox_id"]) == (ROLE, sub_id)
    # без `session` — как раньше; повтор по ключу автора не дописывает
    plain = {**body, "to": "@other_user", "idempotency_key": "new-2"}
    del plain["session"]
    r2 = await client.post("/api/v1/drafts/new", json=plain, headers=_h(world["tok"]))
    assert r2.status_code == 200, r2.text
    again = await client.post(
        "/api/v1/drafts/new", json={**plain, "session": _sess(A)}, headers=_h(world["tok"])
    )
    assert again.status_code == 200 and again.json()["outbox_id"] == r2.json()["outbox_id"]
    assert _author(db, r2.json()["outbox_id"]) == (None, None)
    bad = await client.post(
        "/api/v1/drafts/new", json={**body, "session": {"pid": 0}}, headers=_h(world["tok"])
    )
    assert bad.status_code == 422


# --- B3: роль автора в чтении -------------------------------------------------------------------------------


async def test_author_role_in_reads(client, ui, db, world):
    w = world
    await _sub(client, w, A)
    mine = await _draft(client, w, text="ответ маркетолога уникум")
    plain = await _draft(client, w, None, key="k2", text="черновик без сессии")
    headers = _h(w["tok"])

    messages = (await client.get(f"/api/v1/channels/{w['ch']}/messages", headers=headers)).json()["messages"]
    roles = {m["outbox_id"]: m["outbox_author_role"] for m in messages}
    assert roles[mine] == ROLE and roles[plain] is None and roles[None] is None
    assert all(m["outbox_auto_role"] is None for m in messages)

    one = (await client.get(f"/api/v1/outbox/{mine}", headers=headers)).json()
    assert (one["author_role"], one["auto_role"]) == (ROLE, None)
    pending = {"status": "pending_approval"}
    listed = (await client.get("/api/v1/outbox", params=pending, headers=headers)).json()
    assert {row["id"]: row["author_role"] for row in listed} == {mine: ROLE, plain: None}
    listed_ui = (await ui.get("/api/v1/outbox", params=pending)).json()
    assert {row["id"]: row["author_role"] for row in listed_ui} == {mine: ROLE, plain: None}

    sync = (await ui.get("/api/v1/sync", params={"since": 0})).json()
    by_outbox = {m["outbox_id"]: m["outbox_author_role"] for m in sync["messages"]}
    assert by_outbox[mine] == ROLE and by_outbox[plain] is None

    found = (await client.get("/api/v1/search", params={"q": "уникум"}, headers=headers)).json()
    assert [(row["is_me"], row["outbox_author_role"], row["outbox_auto_role"]) for row in found] == [
        (True, ROLE, None)
    ]
    resolved = (
        await client.post("/api/v1/channels/resolve", json={"query": "Команда"}, headers=headers)
    ).json()
    [chan] = [c for c in resolved if c["id"] == w["ch"]]
    assert {m["outbox_author_role"] for m in chan["recent_messages"]} == {ROLE, None}
    assert all("outbox_auto_role" in m for m in chan["recent_messages"])


# --- B4 + П7: чем сессия слышит беседу ----------------------------------------------------------------------


async def _heard(client, w, who=A, *, key, **kw):
    r = await _send(client, w, who, key=key, **kw)
    assert r.status_code == 200, r.text
    return r.json().get("heard", "нет поля")


async def test_heard_no_field_without_session_and_no_without_sub(client, world):
    assert await _heard(client, world, None, key="h1") == "нет поля"
    assert await _heard(client, world, key="h2") == "no"


async def test_heard_by_project_sub_modes(client, world):
    w = world
    await _sub(client, w, A)  # режим карточек
    assert await _heard(client, w, key="h1") == "cards"
    await _sub(client, w, A, mode="messages")
    assert await _heard(client, w, key="h2") == "project"
    await _sub(client, w, A, mode="messages", msg_filter="personal")
    assert await _heard(client, w, key="h3") == "personal"
    await _sub(client, w, A, mode="direct")
    assert await _heard(client, w, key="h4") == "no"


async def test_heard_channel_sub_wins(client, world):
    w = world
    await _sub(client, w, A)
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch"], "session": {**_sess(A), "cwd": w["dir3"], "kind": "interactive"}},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    assert await _heard(client, w, key="h1") == "channel"
    assert await _heard(client, w, key="h2", channel=w["ch2"]) == "cards"  # другую беседу — по проекту
    assert await _heard(client, w, B, key="h3") == "no"  # подписка на беседу — чужой сессии


async def test_heard_no_for_other_project_filter_and_muted(client, db, world):
    w = world
    await _sub(client, w, A, role="аналитик", cwd=w["dir4"], mode="messages")
    assert await _heard(client, w, key="h1") == "no"  # подписка на другой проект
    other_acc = base._account(db)
    await _sub(client, w, B, mode="messages", accounts=[other_acc])
    assert await _heard(client, w, B, key="h2") == "no"  # фильтр аккаунтов беседу не слышит
    await _sub(client, w, B, mode="messages", accounts=[])
    assert await _heard(client, w, B, key="h3") == "project"
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET muted = 1 WHERE id = ?", (w["ch"],)))
    assert await _heard(client, w, B, key="h4") == "no"  # беседа заглушена
    assert await _heard(client, w, B, key="h5", channel=w["ch2"]) == "project"
    db.write_sync(lambda conn: conn.execute("UPDATE main.project SET muted = 1 WHERE id = ?", (w["p3"],)))
    assert await _heard(client, w, B, key="h6", channel=w["ch2"]) == "no"  # проект заглушён


async def test_heard_no_field_for_new_recipient_channel(client, db, world):
    """Беседа-заготовка `new:…`: подписаться на неё нельзя — поля нет, подсказки не будет."""
    await _sub(client, world, A, mode="messages")
    temp = insert_channel(db, world["acc"], project_id=world["p3"], ext_id="new:abc", title="@petr")
    assert await _heard(client, world, key="h1", channel=temp) == "нет поля"


# --- B5: исходы ---------------------------------------------------------------------------------------------


async def test_outcome_approved_and_edited(client, ui, db, world):
    w = world
    sub_id = await _sub_id(client, w, A)
    plain = await _draft(client, w, key="k1", text="как есть")
    same = await _draft(client, w, key="k2", text="тот же текст")
    edited = await _draft(client, w, key="k3", text="было")
    await _approve(ui, plain)
    await _approve(ui, same, text="  тот же текст\r\n")  # правка только пробелов — не правка
    await _approve(ui, edited, text="стало")
    assert _outcomes(db) == [
        (plain, sub_id, w["p3"], ROLE, 1, "approved", 0),
        (same, sub_id, w["p3"], ROLE, 1, "approved", 0),
        (edited, sub_id, w["p3"], ROLE, 1, "edited", 0),
    ]


async def test_outcome_batch_per_row_under_savepoint(client, ui, db, world):
    w = world
    sub_id = await _sub_id(client, w, A)
    first = await _draft(client, w, key="k1")
    second = await _draft(client, w, key="k2", channel=w["ch2"])
    gone = await _draft(client, w, key="k3")
    assert await _cancel(ui, gone) == "cancelled"
    r = await ui.post("/api/v1/outbox/approve-batch", {"ids": [first, gone, 999_999, second]})
    assert [item["status"] for item in r.json()["results"]] == ["queued", "error", "error", "queued"]
    assert [(o[0], o[1], o[5]) for o in _outcomes(db)] == [
        (gone, sub_id, "declined"),
        (first, sub_id, "approved"),
        (second, sub_id, "approved"),
    ]


async def test_outcome_declined_withdrawn_and_unknown(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A)
    declined = await _draft(client, w, key="k1")
    withdrawn = await _draft(client, w, key="k2", channel=w["ch2"])
    assert await _cancel(ui, declined) == "cancelled"
    await _approve(ui, withdrawn)
    assert await _cancel(ui, withdrawn) == "cancelled"  # подтвердил и снял до отправки
    assert [(o[0], o[5]) for o in _outcomes(db)] == [
        (declined, "declined"),
        (withdrawn, "approved"),
        (withdrawn, "withdrawn"),
    ]
    # Строку уже выдавали коннектору — отмена даёт `unknown` (могло уйти): без исхода.
    maybe = await _draft(client, w, key="k3")
    await _approve(ui, maybe)
    now = _now(clock)
    leases = db.write_sync(lambda conn: outbox.lease(conn, account_id=w["acc"], owner="c1", now_ms=now))
    assert [lease.outbox_id for lease in leases] == [maybe]
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {db_of(maybe)}.outbox SET status = 'queued', lease_owner = NULL WHERE id = ?",  # noqa: S608
            (maybe,),
        )
    )
    assert await _cancel(ui, maybe) == "unknown"
    assert [(o[0], o[5]) for o in _outcomes(db)][3:] == [(maybe, "approved")]


async def test_outcome_cancel_by_session_gives_no_signal(client, db, clock, world):
    """Отмена сессией — без сигнала: исход пишут только ручки владельца."""
    sub_id = await _sub_id(client, world, A)
    outbox_id = await _draft(client, world)
    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=_h(world["tok"]))
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    assert _outcomes(db) == []
    assert _count(db, sub_id, clock) == 0


async def test_outcome_nothing_for_authorless_and_autosent(client, ui, db, clock, world):
    w = world
    plain = await _draft(client, w, None, key="k1")  # без сессии — автора нет
    await _approve(ui, plain)
    no_sub = await _draft(client, w, B, key="k2")  # сессия без подписки — автора нет
    assert await _cancel(ui, no_sub) == "cancelled"
    assert _outcomes(db) == []
    # Ушедшее без подтверждения владельца исхода не имеет (и снять его владелец может — тоже без сигнала).
    sub_id = await _sub_id(client, w, A)
    await _allow(ui, w["ch2"])
    auto = (await _send(client, w, key="k3", channel=w["ch2"])).json()
    assert auto["status"] == "queued" and _author(db, auto["outbox_id"]) == (ROLE, sub_id)
    assert await _cancel(ui, auto["outbox_id"]) == "cancelled"
    assert _outcomes(db) == []
    with pytest.raises(ValueError):
        db.write_sync(lambda conn: draft_outcomes.record(conn, plain, "sent", now_ms=_now(clock)))


# --- B5: сигнал ---------------------------------------------------------------------------------------------


async def test_signal_delivered_once_with_numbers_only(client, ui, db, clock, world):
    w = world
    sub_id = await _sub_id(client, w, A, mode="messages")
    edited = await _draft(client, w, key="k1", text="было")
    declined = await _draft(client, w, key="k2", channel=w["ch2"])
    await _approve(ui, edited, text="стало")
    await _cancel(ui, declined)
    assert _count(db, sub_id, clock) == 2
    signals = await _wait(client, w, sub_id, A)
    assert signals == [
        {"kind": "draft", "outbox_id": edited, "channel_id": w["ch"], "outcome": "edited"},
        {"kind": "draft", "outbox_id": declined, "channel_id": w["ch2"], "outcome": "declined"},
    ]
    assert [o[6] for o in _outcomes(db)] == [1, 1] and _count(db, sub_id, clock) == 0
    assert _take(db, sub_id, clock) == []  # второй раз не отдаётся
    audits = [a for a in _audits(db, "sub_signal") if json.loads(a[4]).get("kind") == "draft"]
    assert [(a[1], a[2], a[3], json.loads(a[4])) for a in audits] == [
        ("delivered", w["ch"], edited, {"sub_id": sub_id, "kind": "draft", "outcome": "edited"}),
        ("delivered", w["ch2"], declined, {"sub_id": sub_id, "kind": "draft", "outcome": "declined"}),
    ]


async def test_signal_protocol_4_neither_counts_nor_delivers(client, ui, db, clock, world):
    w = world
    sub_id = await _sub_id(client, w, A, mode="messages")
    outbox_id = await _draft(client, w)
    await _approve(ui, outbox_id)
    assert await _wait(client, w, sub_id, A, proto=4) == []
    assert [o[6] for o in _outcomes(db)] == [0]  # не помечен выданным — дождётся нового клиента
    with db.reader() as conn:
        assert subs.wait_state(conn, sub_id, now_ms=_now(clock), peers=True) == (0, 0)
        assert subs.wait_state(conn, sub_id, now_ms=_now(clock), peers=True, drafts=True) == (1, 0)
    [signal] = await _wait(client, w, sub_id, A)
    assert (signal["kind"], signal["outbox_id"], signal["outcome"]) == ("draft", outbox_id, "approved")


async def test_signal_goes_to_line_after_session_restart(client, ui, db, clock, world):
    """Сессия роли перезапущена: исход получает новая подписка той же линии; чужая линия и роль — нет."""
    w = world
    old = await _sub_id(client, w, A)
    outbox_id = await _draft(client, w)
    await _end(client, w, A, old)
    new = await _sub_id(client, w, A2)  # та же роль, линия 1
    second_line = await _sub_id(client, w, B)  # та же роль, линия 2
    other_role = await _sub_id(client, w, C, role="аналитик")
    with db.reader() as conn:
        assert (subs.get_sub(conn, new).role_no, subs.get_sub(conn, second_line).role_no) == (1, 2)
    await _approve(ui, outbox_id)
    assert [_count(db, s, clock) for s in (new, second_line, other_role)] == [1, 0, 0]
    assert _take(db, second_line, clock) == [] and _take(db, other_role, clock) == []
    assert _take(db, new, clock) == [
        {"kind": "draft", "outbox_id": outbox_id, "channel_id": w["ch"], "outcome": "approved"}
    ]
    assert _count(db, new, clock) == 0


async def test_signal_stays_with_live_author_after_role_change_in_place(client, ui, db, clock, world):
    """Ревью (находка 1): автор A (роль №1) положил черновик, владелец подтвердил; A исход ещё не забрала и
    сменила роль на месте (та же строка подписки), линию «роль №1» взяла живая сессия B. Исход ждёт A — пока
    её подписка жива, по линии он не уходит."""
    a = await _sub_id(client, world, A)
    d = await _draft(client, world, A)
    await _approve(ui, d)
    body = await _sub(client, world, A, role=ADMIN)
    assert body["sub"]["id"] == a
    b = await _sub_id(client, world, B)
    assert _count(db, b, clock) == 0 and await _wait(client, world, b, B) == []
    got = await _wait(client, world, a, A)
    assert [(s["kind"], s["outbox_id"], s["outcome"]) for s in got] == [("draft", d, "approved")]


async def test_signal_roleless_only_same_subscription(client, ui, db, clock, world):
    w = world
    author = await _sub_id(client, w, A, role=None)
    other = await _sub_id(client, w, B, role=None)  # тоже без роли, другая сессия
    outbox_id = await _draft(client, w)
    await _approve(ui, outbox_id)
    assert _outcomes(db) == [(outbox_id, author, w["p3"], None, 1, "approved", 0)]
    assert (_count(db, author, clock), _count(db, other, clock)) == (1, 0)
    assert _take(db, other, clock) == []
    # П3: роль поставлена вторым вызовом в ту же строку подписки — исход всё равно её
    assert await _sub_id(client, w, A, role="аналитик") == author
    assert _count(db, author, clock) == 1
    assert [s["outbox_id"] for s in _take(db, author, clock)] == [outbox_id]


async def test_signal_roleless_restart_does_not_inherit(client, ui, db, clock, world):
    w = world
    author = await _sub_id(client, w, A, role=None)
    outbox_id = await _draft(client, w)
    await _end(client, w, A, author)
    again = await _sub_id(client, w, A2, role=None)
    await _approve(ui, outbox_id)
    assert again != author and _count(db, again, clock) == 0


async def test_free_agent_outcome_stays_in_its_folder(client, ui, db, clock, world):
    """П3: исход агента вне проектов не уходит сессии из другой папки (вне проектов линии нет) — только
    подписке автора и той же роли из той же папки."""
    w = world
    author = await _sub_id(client, w, C, role=ADMIN, cwd=w["free"], mode="direct")
    outbox_id = await _draft(client, w, C)
    await _end(client, w, C, author)
    elsewhere = await _sub(client, w, D, role=ADMIN, cwd=w["free2"], mode="direct")
    assert (elsewhere["sub"]["project_id"], elsewhere["sub"]["role_no"]) == (subs.NO_PROJECT_ID, 1)
    await _approve(ui, outbox_id)
    assert _outcomes(db) == [(outbox_id, author, subs.NO_PROJECT_ID, ADMIN, 1, "approved", 0)]
    assert _count(db, elsewhere["sub"]["id"], clock) == 0
    assert _take(db, elsewhere["sub"]["id"], clock) == []
    await _end(client, w, D, elsewhere["sub"]["id"])
    same_folder = await _sub_id(client, w, A2, role=ADMIN, cwd=w["free"], mode="direct")
    assert _count(db, same_folder, clock) == 1
    assert [s["outbox_id"] for s in await _wait(client, w, same_folder, A2)] == [outbox_id]


async def test_signal_expires_after_a_day(client, ui, db, clock, world):
    w = world
    sub_id = await _sub_id(client, w, A)
    outbox_id = await _draft(client, w)
    await _approve(ui, outbox_id)
    clock.advance_ms(draft_outcomes.OUTCOME_TTL_MS - MIN)
    assert _count(db, sub_id, clock) == 1
    clock.advance_ms(2 * MIN)
    assert _count(db, sub_id, clock) == 0 and _take(db, sub_id, clock) == []
    assert [o[6] for o in _outcomes(db)] == [0]  # не выдан никогда


async def test_signal_limit_and_missing_outbox_row(client, ui, db, clock, world):
    """Не влезшие в предел остаются до следующей выдачи; П5: строки outbox нет — исход помечается отданным и
    пропускается (счёт и выдача — один предикат, ожидание не крутится)."""
    w = world
    sub_id = await _sub_id(client, w, A)
    first = await _draft(client, w, key="k1")
    second = await _draft(client, w, key="k2", channel=w["ch2"])
    await _approve(ui, first)
    await _approve(ui, second)
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.draft_outcome (outbox_id, author_sub_id, project_id, role_key, role_no, "
            "outcome, created_ts) VALUES (999999, ?, ?, ?, 1, 'approved', ?)",
            (sub_id, w["p3"], ROLE, _now(clock)),
        )
    )
    assert _count(db, sub_id, clock) == 3
    assert [s["outbox_id"] for s in _take(db, sub_id, clock, limit=1)] == [first]
    assert _count(db, sub_id, clock) == 1  # пропавшая строка помечена, вторая ждёт
    assert _take(db, sub_id, clock, limit=0) == [] and _count(db, sub_id, clock) == 1
    assert [s["outbox_id"] for s in _take(db, sub_id, clock)] == [second]
    assert _count(db, sub_id, clock) == 0


async def test_signal_reads_channel_from_outbox_after_migrate(client, ui, db, clock, world):
    """П5: беседы в строке исхода нет — берётся из строки outbox на момент выдачи."""
    w = world
    sub_id = await _sub_id(client, w, A)
    outbox_id = await _draft(client, w)
    await _approve(ui, outbox_id)
    moved = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="real", title="Настоящая")
    insert_message(db, moved, ext_id="1", ts=_now(clock))
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.outbox SET channel_id = ? WHERE id = ?", (moved, outbox_id))
    )
    assert [s["channel_id"] for s in _take(db, sub_id, clock)] == [moved]


# --- B1: миграция -------------------------------------------------------------------------------------------


def test_migration_0055_author_columns_and_outcomes(tmp_path, monkeypatch):
    original = db_module._migrations

    def _upto(version):
        return lambda *, main: {v: f for v, f in original(main=main).items() if v <= version}

    monkeypatch.setattr(db_module, "_migrations", _upto(54))
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            for schema in ("main", "work"):
                columns = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert "author_role" not in columns and "auto_role" in columns
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", _upto(55))
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                for schema in ("main", "work"):
                    assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 55
                    columns = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                    assert {"author_role", "author_sub_id"} <= columns
                tables = {
                    r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")
                }
                assert "draft_outcome" in tables
                columns = {r[1] for r in conn.execute("PRAGMA main.table_info(draft_outcome)")}
                assert "channel_id" not in columns  # П5
            with pytest.raises(Exception, match="CHECK"):
                new.write_sync(
                    lambda conn: conn.execute(
                        "INSERT INTO main.draft_outcome (outbox_id, author_sub_id, project_id, outcome, "
                        "created_ts) VALUES (1, 1, 1, 'sent', 0)"
                    )
                )
        finally:
            new.close()
