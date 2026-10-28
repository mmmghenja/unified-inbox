"""Ф8-3, ядро: режим беседы, внимание к сообщению, `unread_react`, `PATCH attention`, присутствие, миграция
0010, широковещательные упоминания и подписка на тред. Спецификация: docs/architecture.md, тесты
T1–T6 и поправки П1, П2, П5, П6, П8, П9, П11–П13, П15, П16.

Стиль — как test_agent_items.py: публичное приложение через ASGITransport (фикстуры `client`/`db`/`clock`),
события — через `ingest.apply_batch`, как у коннекторов.
"""

import json
import re

import pytest

from hub.core import admin, attention, ingest, queries, security
from hub.core import db as db_module
from hub.core.db import Db
from hub.core.presence import TTL_MS, is_visible

from .conftest import (
    csrf_headers,
    insert_account,
    insert_project,
    login_via_code,
    make_bearer_token,
)

UI = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)


def _ingest(db, account_id: int, events: list[dict], now_ms: int = 10_000) -> None:
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0, result


def _msg(
    chat: str,
    ext_id: str,
    ts: int,
    *,
    kind: str = "group",
    attrs: dict | None = None,
    mentions_me: bool = False,
    is_me: bool = False,
    thread: str | None = None,
    broadcast_only: bool = False,
    title: str | None = None,
) -> dict:
    channel = {"ext_id": chat, "kind": kind, "title": title or f"чат {chat}"}
    if attrs is not None:
        channel["attrs"] = attrs
    event = {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "sender": {"ext_id": "1", "is_me": True} if is_me else {"ext_id": "7", "name": "Пётр"},
        "is_me": is_me,
        "kind": "text",
        "text": f"сообщение {ext_id}",
        "mentions_me": mentions_me,
    }
    if thread is not None:
        event["thread_ext_id"] = thread
    if broadcast_only:
        event["mentions_broadcast_only"] = True
    return event


def _channel_id(db, chat: str, schema: str = "main") -> int:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (chat,),
        ).fetchone()[0]


def _set_project(db, channel_id: int, project_id: int | None) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {db_module.db_of(channel_id)}.channel SET project_id = ? WHERE id = ?",  # noqa: S608
            (project_id, channel_id),
        )
    )


def _policy(db, project_id: int, changes: dict, **kw) -> dict:
    return db.write_sync(lambda conn: admin.set_project_attention(conn, project_id, changes=changes, **kw))


def _channel(db, channel_id: int) -> dict:
    with db.reader() as conn:
        return queries.get_channel(conn, UI, channel_id)


def _messages(db, channel_id: int) -> dict[str, dict]:
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, channel_id, before=None, limit=200)
    return {m["ext_id"]: m for m in page.messages}


def _thread_messages(db, channel_id: int, thread: str) -> dict[str, dict]:
    with db.reader() as conn:
        page = queries.list_thread_messages(conn, UI, channel_id, thread, before=None, limit=200)
    return {m["ext_id"]: m for m in page.messages}


# --- T1: эффективный режим по всем веткам A3 в порядке П2 ---------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # muted канала / проекта — раньше всего, даже ручного режима
        ((1, 0, "react", 5, "dm", None, None), "muted"),
        ((0, 1, "react", 5, "dm", None, None), "muted"),
        # ручной — раньше Неразобранного и политики
        ((0, 0, "info", None, "dm", None, None), "info"),
        ((0, 0, "digest", 5, "dm", None, '{"attention": {"dm": "info"}}'), "info"),
        # Неразобранное (Ф57-1): лички (dm, mpim) и почта — react, остальное — info
        ((0, None, None, None, "dm", None, None), "react"),
        ((0, None, None, None, "group", "mpim", None), "react"),
        ((0, None, None, None, "group", "public", None), "info"),
        ((0, None, None, None, "channel", None, None), "info"),
        ((0, None, None, None, "mail_thread", None, None), "react"),
        # Ф57-1: `triage`, оставшийся в данных (ручной режим, политика), читается как отсутствующий —
        # действует следующий шаг цепочки
        ((0, 0, "triage", 5, "group", None, None), "info"),
        ((0, 0, "triage", 5, "dm", None, None), "react"),
        ((0, None, "triage", None, "mail_thread", None, None), "react"),
        ((0, 0, "triage", 5, "group", None, '{"attention": {"group": "digest"}}'), "info"),
        ((0, 0, None, 5, "group", None, '{"attention": {"group": "triage", "default": "react"}}'), "react"),
        ((0, 0, None, 5, "group", None, '{"attention": {"default": "triage"}}'), "info"),
        ((0, 0, None, 5, "mail_thread", None, '{"attention": {"account:7": "triage"}}', 7), "react"),
        # политика по виду
        ((0, 0, None, 5, "group", None, '{"attention": {"group": "react"}}'), "react"),
        ((0, 0, None, 5, "mail_thread", None, '{"attention": {"mail_thread": "digest"}}'), "react"),
        # mpim — сначала ключ mpim; ключ group групповую личку не трогает (см. отступление в attention.py)
        ((0, 0, None, 5, "group", "mpim", '{"attention": {"mpim": "info"}}'), "info"),
        ((0, 0, None, 5, "group", "mpim", '{"attention": {"group": "info"}}'), "react"),
        # П2: default проекта НЕ глушит лички; явный ключ dm — глушит
        ((0, 0, None, 5, "dm", None, '{"attention": {"default": "info"}}'), "react"),
        ((0, 0, None, 5, "dm", None, '{"attention": {"dm": "info"}}'), "info"),
        # default проекта
        ((0, 0, None, 5, "group", None, '{"attention": {"default": "react"}}'), "react"),
        ((0, 0, None, 5, "mail_thread", None, '{"attention": {"default": "info"}}'), "info"),
        # встроенные умолчания: личка/mpim — react, почта — react, остальное — info
        ((0, 0, None, 5, "dm", None, None), "react"),
        ((0, 0, None, 5, "group", "mpim", None), "react"),
        ((0, 0, None, 5, "mail_thread", None, None), "react"),
        ((0, 0, None, 5, "group", "public", None), "info"),
        ((0, 0, None, 5, "channel", None, None), "info"),
        ((0, 0, None, 5, "topic", None, "{битый"), "info"),
        # неизвестный режим в политике — как отсутствие ключа
        ((0, 0, None, 5, "group", None, '{"attention": {"group": "loud"}}'), "info"),
    ],
)
def test_t1_effective_mode_branches(args, expected):
    assert attention.effective_mode(*args) == expected


def test_t1_mode_through_sql_uses_slack_type_and_project_policy(db):
    """Та же функция — через SQLite (П5): `slack_type` берётся из `meta_json.attrs`, политика — из проекта."""
    acc = insert_account(db, source="slack", db_name="work")
    pid = insert_project(db, name="Globex")
    _ingest(
        db,
        acc,
        [
            _msg("G1", "1.0", 1000, attrs={"slack_type": "mpim"}),
            _msg("C1", "2.0", 2000, attrs={"slack_type": "public"}),
        ],
    )
    mpim, public = _channel_id(db, "G1", "work"), _channel_id(db, "C1", "work")
    assert _channel(db, mpim)["attention"] == "react"  # Неразобранное: групповая личка
    assert _channel(db, public)["attention"] == "info"  # Неразобранное: остальное
    _set_project(db, mpim, pid)
    _set_project(db, public, pid)
    _policy(db, pid, {"default": "info", "mpim": "info"})
    assert _channel(db, mpim)["attention"] == "info"
    assert _channel(db, public)["attention"] == "info"
    muted_pid = insert_project(db, name="Тихий", muted=1)
    _set_project(db, public, muted_pid)
    got = _channel(db, public)
    assert (got["attention"], got["attention_manual"], got["attention_auto"]) == ("muted", None, "muted")


# --- T2 + П1: for_me и attention сообщения ------------------------------------------------------------------


def test_t2_message_for_me_and_attention(db):
    acc = insert_account(db, source="slack", db_name="main")
    pid = insert_project(db, name="P")
    _ingest(
        db,
        acc,
        [
            _msg("C1", "1.0", 1000),
            _msg("C1", "2.0", 2000, mentions_me=True),
            # тред, где я писал (подписан), и тред, где нет
            _msg("C1", "3.0", 3000),
            _msg("C1", "3.1", 3100, thread="3.0", is_me=True),
            _msg("C1", "3.2", 3200, thread="3.0"),
            _msg("C1", "4.0", 4000),
            _msg("C1", "4.1", 4100, thread="4.0"),
        ],
    )
    cid = _channel_id(db, "C1")
    _set_project(db, cid, pid)
    top = _messages(db, cid)
    assert (top["1.0"]["for_me"], top["1.0"]["attention"]) == (False, "info")
    assert (top["2.0"]["for_me"], top["2.0"]["attention"]) == (True, "react")
    assert (
        _thread_messages(db, cid, "3.0")["3.2"]["for_me"],
        _thread_messages(db, cid, "3.0")["3.2"]["attention"],
    ) == (
        True,
        "react",
    )
    reply_unsub = _thread_messages(db, cid, "4.0")["4.1"]
    assert (reply_unsub["for_me"], reply_unsub["attention"]) == (False, "info")

    # Ф57-1: Неразобранное, не личка — «к сведению»; упоминание поднимает до react, как в проекте
    _set_project(db, cid, None)
    assert _messages(db, cid)["1.0"]["attention"] == "info"
    assert _messages(db, cid)["2.0"]["attention"] == "react"
    # muted + упоминание — muted
    _set_project(db, cid, pid)
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET muted = 1 WHERE id = ?", (cid,)))
    assert _messages(db, cid)["2.0"]["attention"] == "muted"


def test_t2_p1_mail_to_me_not_raised_in_info(db):
    """П1: у почты `mentions_me` = «мой адрес в Кому» — поднятия до react нет из info."""
    acc = insert_account(db, source="mail", db_name="work")
    pid = insert_project(db, name="Globex")
    _ingest(db, acc, [_msg("t1", "m1", 1000, kind="mail_thread", mentions_me=True)])
    cid = _channel_id(db, "t1", "work")
    _set_project(db, cid, pid)
    _policy(db, pid, {"mail_thread": "info"})
    assert _messages(db, cid)["m1"]["attention"] == "info"
    assert _channel(db, cid)["unread_react"] == 0


# --- T3 + П12: unread_react канала и проекта; поле сообщения и счётчик сходятся -----------------------------


def _react_unread_by_field(db, cid: int) -> int:
    top = [m for m in _messages(db, cid).values() if m["unread"] and m["attention"] == "react"]
    replies = []
    for parent in _messages(db, cid).values():
        if parent["thread"]:
            replies += [
                m
                for m in _thread_messages(db, cid, parent["ext_id"]).values()
                if m["unread"] and m["attention"] == "react"
            ]
    return len(top) + len(replies)


async def test_t3_unread_react_channel_and_project(client, db, clock):
    acc = insert_account(db, source="slack", db_name="main")
    pid = insert_project(db, name="P")
    _ingest(
        db,
        acc,
        [
            # react-беседа (личка в проекте): все непрочитанные
            _msg("D1", "1.0", 1000, kind="dm"),
            _msg("D1", "2.0", 2000, kind="dm"),
            # info-беседа: упоминание, подписанный тред (мой родитель) с ответом, неподписанный тред
            _msg("C1", "1.0", 1000),
            _msg("C1", "2.0", 2000, mentions_me=True),
            _msg("C1", "3.0", 3000, is_me=True),
            _msg("C1", "3.1", 3100, thread="3.0"),
            _msg("C1", "4.0", 4000),
            _msg("C1", "4.1", 4100, thread="4.0"),
            # Неразобранное, не личка, без упоминания — ноль (Ф57-1: «к сведению»)
            _msg("C2", "1.0", 1000),
        ],
    )
    dm, info, unsorted = _channel_id(db, "D1"), _channel_id(db, "C1"), _channel_id(db, "C2")
    _set_project(db, dm, pid)
    _set_project(db, info, pid)
    assert _channel(db, dm)["unread_react"] == 2
    assert _channel(db, info)["unread_react"] == 2  # упоминание + ответ подписанного треда
    assert _channel(db, unsorted)["unread_react"] == 0
    for cid in (dm, info, unsorted):
        assert _channel(db, cid)["unread_react"] == _react_unread_by_field(db, cid)

    cookie, _ = await login_via_code(client, db, clock)
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[pid]["unread_react"] == 4
    assert projects[queries.UNSORTED_PROJECT_ID]["unread_react"] == 0
    assert projects[queries.ALL_PROJECTS_ID]["unread_react"] == 4
    channels = {c["id"]: c for c in (await client.get(f"/api/v1/projects/{pid}/channels")).json()}
    assert (channels[dm]["unread_react"], channels[info]["unread_react"]) == (2, 2)
    assert channels[info]["attention"] == "info" and channels[info]["attention_manual"] is None

    # Прочтение до конца обнуляет (маркер канала и маркер треда).
    last_dm = _messages(db, dm)["2.0"]["id"]
    r = await client.post(
        f"/api/v1/channels/{dm}/read", json={"up_to_message_id": last_dm}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200
    assert _channel(db, dm)["unread_react"] == 0
    last_top = _messages(db, info)["4.0"]["id"]
    await client.post(
        f"/api/v1/channels/{info}/read", json={"up_to_message_id": last_top}, headers=csrf_headers(cookie)
    )
    reply = _thread_messages(db, info, "3.0")["3.1"]["id"]
    r = await client.post(
        f"/api/v1/channels/{info}/threads/3.0/read",
        json={"up_to_message_id": reply},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200
    assert _channel(db, info)["unread_react"] == 0
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[pid]["unread_react"] == 0


# --- T4 + П6 + П15: PATCH attention — только UI; seq и /sync; политика двигает seq каналов ------------------


async def test_t4_patch_attention_ui_only_seq_and_sync(client, db, clock):
    acc = insert_account(db, source="telegram")
    pid = insert_project(db, name="P")
    _ingest(db, acc, [_msg("C1", "1", 1000), _msg("C2", "1", 1000)])
    cid, other = _channel_id(db, "C1"), _channel_id(db, "C2")
    _set_project(db, cid, pid)
    _set_project(db, other, pid)

    # П6: bearer — 403 при любом скоупе, даже с `organize` и даже с `null`.
    for scopes in (("read", "organize"), ("read",)):
        token = make_bearer_token(db, scopes=scopes, name=f"t-{len(scopes)}")
        for value in ("react", None):
            r = await client.patch(
                f"/api/v1/channels/{cid}",
                json={"attention": value},
                headers={"Authorization": f"Bearer {token}", "Origin": "http://127.0.0.1:8787"},
            )
            assert r.status_code == 403, (scopes, value)
    assert _channel(db, cid)["attention_manual"] is None

    cookie, _ = await login_via_code(client, db, clock)
    since = (await client.get("/api/v1/sync")).json()["max_seq"]
    seq_before = _channel_seq(db, cid)
    r = await client.patch(
        f"/api/v1/channels/{cid}", json={"attention": "react"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200
    got = _channel(db, cid)
    assert (got["attention"], got["attention_manual"], got["attention_auto"]) == ("react", "react", "info")
    assert _channel_seq(db, cid) > seq_before
    page = (await client.get("/api/v1/sync", params={"since": since})).json()
    changed = {c["id"]: c for c in page["channels"]}
    assert changed[cid]["attention"] == "react" and changed[cid]["attention_manual"] == "react"
    assert "unread_react" in changed[cid]
    assert all("for_me" in m and "attention" in m for m in page["messages"])

    r = await client.patch(
        f"/api/v1/channels/{cid}", json={"attention": "loud"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422
    r = await client.patch(f"/api/v1/channels/{cid}", json={"attention": None}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    assert _channel(db, cid)["attention_manual"] is None and _channel(db, cid)["attention"] == "info"
    with db.reader() as conn:
        audit = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'channel_update' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert json.loads(audit[0]) == {"attention": None}

    # Смена политики проекта двигает seq ВСЕХ его каналов.
    since = (await client.get("/api/v1/sync")).json()["max_seq"]
    _policy(db, pid, {"group": "react"})
    page = (await client.get("/api/v1/sync", params={"since": since})).json()
    assert {c["id"] for c in page["channels"]} == {cid, other}
    assert all(c["attention"] == "react" for c in page["channels"])


def _channel_seq(db, cid: int) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT seq FROM main.channel WHERE id = ?", (cid,)).fetchone()[0]


def test_t4_policy_merge_validation(db):
    pid = insert_project(db, name="P")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?", ('{"other": 1}', pid)
        )
    )
    assert _policy(db, pid, {"dm": "react", "mail_thread": "info"}) == {"dm": "react", "mail_thread": "info"}
    assert _policy(db, pid, {"dm": None}) == {"mail_thread": "info"}
    with db.reader() as conn:
        stored = json.loads(
            conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (pid,)).fetchone()[0]
        )
    assert stored["other"] == 1  # прочие ключи политики не тронуты
    for bad in ({"chat": "react"}, {"dm": "loud"}, {"dm": "digest"}):
        with pytest.raises(ValueError):
            _policy(db, pid, bad)
    assert _policy(db, 999, {"dm": "react"}) is None


# --- T5 + П8: присутствие -----------------------------------------------------------------------------------


async def test_t5_presence_ui_only_expiry_tabs_and_scope(client, db, clock):
    acc = insert_account(db, source="telegram")
    p1, p2 = insert_project(db, name="A"), insert_project(db, name="B")
    _ingest(db, acc, [_msg("C1", "1", 1000), _msg("C2", "1", 1000)])
    c1, c2 = _channel_id(db, "C1"), _channel_id(db, "C2")
    _set_project(db, c1, p1)
    _set_project(db, c2, p2)
    scoped = make_bearer_token(db, scopes=("read",), project_ids=[p1], name="scoped")
    wide = make_bearer_token(db, scopes=("read",), name="wide")

    # bearer POST — 403 (раньше, чем появится cookie: bearer и cookie вместе — 400)
    r = await client.post(
        "/api/v1/presence",
        json={"client_id": "x", "channel_id": c1},
        headers={"Authorization": f"Bearer {wide}", "Origin": "http://127.0.0.1:8787"},
    )
    assert r.status_code == 403

    cookie, _ = await login_via_code(client, db, clock)
    h = csrf_headers(cookie)
    assert (
        await client.post("/api/v1/presence", json={"client_id": "tab1", "channel_id": c1}, headers=h)
    ).status_code == 200
    await client.post(
        "/api/v1/presence", json={"client_id": "tab2", "channel_id": c2, "thread_ext_id": "9.0"}, headers=h
    )
    got = (await client.get("/api/v1/presence")).json()["items"]
    assert got == [{"channel_id": c1, "thread_ext_id": None}, {"channel_id": c2, "thread_ext_id": "9.0"}]
    # П8: верхний уровень видно по любой записи канала; ответ в треде — только при открытом этом треде
    assert is_visible(got, c2, None) and is_visible(got, c2, "9.0") and not is_visible(got, c2, "8.0")
    assert is_visible(got, c1, None) and not is_visible(got, c1, "1.0")

    # null снимает только запись этой вкладки
    await client.post("/api/v1/presence", json={"client_id": "tab2", "channel_id": None}, headers=h)
    assert (await client.get("/api/v1/presence")).json()["items"] == [
        {"channel_id": c1, "thread_ext_id": None}
    ]

    # проектный токен видит только каналы своего проекта
    await client.post("/api/v1/presence", json={"client_id": "tab2", "channel_id": c2}, headers=h)
    cookies = dict(client.cookies)
    client.cookies.clear()
    r = await client.get("/api/v1/presence", headers={"Authorization": f"Bearer {scoped}"})
    assert r.json()["items"] == [{"channel_id": c1, "thread_ext_id": None}]
    r = await client.get("/api/v1/presence", headers={"Authorization": f"Bearer {wide}"})
    assert len(r.json()["items"]) == 2
    client.cookies.update(cookies)

    # истекает через 45 с без обновления
    clock.advance_ms(TTL_MS - 1000)
    assert len((await client.get("/api/v1/presence")).json()["items"]) == 2
    await client.post("/api/v1/presence", json={"client_id": "tab1", "channel_id": c1}, headers=h)
    clock.advance_ms(2000)
    assert (await client.get("/api/v1/presence")).json()["items"] == [
        {"channel_id": c1, "thread_ext_id": None}
    ]
    clock.advance_ms(TTL_MS)
    assert (await client.get("/api/v1/presence")).json()["items"] == []

    # несуществующий канал — 404, без client_id — 422
    assert (
        await client.post("/api/v1/presence", json={"client_id": "t", "channel_id": 10**6}, headers=h)
    ).status_code == 404
    assert (await client.post("/api/v1/presence", json={"channel_id": c1}, headers=h)).status_code == 422


# --- T6 + П16: миграция 0010 на копии БД предыдущей версии; номера без пропусков ----------------------------


def test_t6_migration_0010_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_9(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 9}

    monkeypatch.setattr(db_module, "_migrations", _up_to_9)
    old = Db(tmp_path)
    old.start()
    try:
        # Строки — прямым SQL, как их писало ядро версии 9 (нынешний ingest знает новую колонку сообщений).
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts, muted) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0, 1)",
                    (acc,),
                ).lastrowid
                conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "  # noqa: S608
                    "ts, is_me, kind, text, mentions_me) "
                    "VALUES (1, 1, ?, ?, '1', 1000, 0, 'text', 'привет', 1)",
                    (acc, cid),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 9
            cols = [r[1] for r in conn.execute("PRAGMA main.table_info(channel)")]
            assert "attention" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 10
                channel_cols = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(channel)")]
                message_cols = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(message)")]
                assert "attention" in channel_cols and "mentions_broadcast" in message_cols
                row = conn.execute(
                    f"SELECT title, muted, attention FROM {schema}.channel WHERE ext_id = 'c1'"  # noqa: S608
                ).fetchone()
                assert row == ("Вася", 1, None)
                msg = conn.execute(
                    f"SELECT text, mentions_me, mentions_broadcast FROM {schema}.message"  # noqa: S608
                ).fetchall()
                assert msg == [("привет", 1, 0)]
        cid = _channel_id(new, "c1", "work")
        assert _channel(new, cid)["attention"] == "muted"
    finally:
        new.close()


def test_p16_migration_numbers_are_contiguous():
    versions = sorted(db_module._migrations(main=True))
    assert versions == list(range(1, len(versions) + 1))
    names = sorted(p.name for p in db_module._MIGRATIONS_DIR.glob("*.sql"))
    assert all(re.fullmatch(r"\d{4}_(main|shared)\.sql", n) for n in names)


# --- П9: миграция группы Telegram в супергруппу переносит attention -----------------------------------------


def test_p9_channel_migrate_carries_attention(db):
    acc = insert_account(db, source="telegram")
    _ingest(db, acc, [_msg("-100", "1", 1000)])
    old_id = _channel_id(db, "-100")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET attention = 'react' WHERE id = ?", (old_id,))
    )
    _ingest(
        db,
        acc,
        [{"type": "channel_migrate", "old_ext_id": "-100", "channel": {"ext_id": "-1009", "kind": "group"}}],
    )
    new_id = _channel_id(db, "-1009")
    assert _channel(db, new_id)["attention_manual"] == "react"


# --- П11 + П13: широковещательное упоминание тред не подписывает; упоминание в родителе — подписывает -------


def _subscribed(db, cid: int, thread: str) -> bool | None:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT subscribed FROM main.thread WHERE channel_id = ? AND ext_id = ?", (cid, thread)
        ).fetchone()
    return None if row is None else bool(row[0])


def test_p11_broadcast_only_mention_does_not_subscribe_thread(db):
    acc = insert_account(db, source="slack")
    pid = insert_project(db, name="P")
    _ingest(
        db,
        acc,
        [
            _msg("C1", "1.0", 1000),
            _msg("C1", "1.1", 1100, thread="1.0", mentions_me=True, broadcast_only=True),
            _msg("C1", "2.0", 2000),
            _msg("C1", "2.1", 2100, thread="2.0", mentions_me=True),
        ],
    )
    cid = _channel_id(db, "C1")
    _set_project(db, cid, pid)
    assert _subscribed(db, cid, "1.0") is False
    assert _subscribed(db, cid, "2.0") is True
    # в БД mentions_me = 1 как обычно; for_me сообщения — нет (Ф20-1 B1: `@channel` не личное), и следующий
    # ответ треда — нет
    _ingest(db, acc, [_msg("C1", "1.2", 1200, thread="1.0")])
    replies = _thread_messages(db, cid, "1.0")
    assert replies["1.1"]["for_me"] is False and replies["1.2"]["for_me"] is False
    with db.reader() as conn:
        row = conn.execute(
            "SELECT mentions_me, mentions_broadcast FROM main.message WHERE ext_id = '1.1'"
        ).fetchone()
    assert row == (1, 1)


def test_p13_direct_mention_in_parent_subscribes_thread(db):
    acc = insert_account(db, source="slack")
    pid = insert_project(db, name="P")
    _ingest(
        db,
        acc,
        [
            _msg("C1", "1.0", 1000, mentions_me=True),
            _msg("C1", "1.1", 1100, thread="1.0"),
            _msg("C1", "2.0", 2000, mentions_me=True, broadcast_only=True),
            _msg("C1", "2.1", 2100, thread="2.0"),
        ],
    )
    cid = _channel_id(db, "C1")
    _set_project(db, cid, pid)
    assert _subscribed(db, cid, "1.0") is True
    assert _subscribed(db, cid, "2.0") is False
    reply = _thread_messages(db, cid, "1.0")["1.1"]
    assert (reply["for_me"], reply["attention"]) == (True, "react")
    assert _thread_messages(db, cid, "2.0")["2.1"]["attention"] == "info"


# --- Ф66-3 E: серый счётчик проекта без заглушённых бесед ---------------------------------------------------


async def test_f66_e_project_unread_skips_muted_channels(client, db, clock):
    acc = insert_account(db, source="slack", db_name="main")
    pid = insert_project(db, name="P")
    _ingest(
        db,
        acc,
        [
            _msg("D1", "1.0", 1000, kind="dm"),
            _msg("D1", "2.0", 2000, kind="dm"),
            _msg("C1", "1.0", 1000),
            _msg("C1", "2.0", 2000),
            _msg("C1", "3.0", 3000),
            _msg("C2", "1.0", 1000),  # Неразобранное: заглушённая беседа
            _msg("C3", "1.0", 1000),  # Неразобранное: обычная
        ],
    )
    normal, muted = _channel_id(db, "D1"), _channel_id(db, "C1")
    loose_muted = _channel_id(db, "C2")
    _set_project(db, normal, pid)
    _set_project(db, muted, pid)
    for cid in (muted, loose_muted):
        db.write_sync(
            lambda conn, cid=cid: conn.execute(
                f"UPDATE {db_module.db_of(cid)}.channel SET muted = 1 WHERE id = ?",  # noqa: S608
                (cid,),
            )
        )

    await login_via_code(client, db, clock)
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[pid]["unread"] == 2  # только обычная; заглушённая (3) не считается
    assert projects[queries.UNSORTED_PROJECT_ID]["unread"] == 1
    assert projects[queries.ALL_PROJECTS_ID]["unread"] == 3
    # Внутри заглушённой беседы свой счётчик прежний.
    assert _channel(db, muted)["unread"] == 3
    assert _channel(db, loose_muted)["unread"] == 1
