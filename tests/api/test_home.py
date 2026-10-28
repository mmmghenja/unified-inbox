"""Ф8-6, ядро: журнал запусков Claude (`agent_run`), черновики и отправленное с адресом, «Нужна реакция».
Спецификация: docs/architecture.md, тесты T1–T3 и поправка П1.

Стиль — как test_attention.py: публичное приложение через ASGITransport (фикстуры `client`/`db`/`clock`),
сообщения — через `ingest.apply_batch`, как у коннекторов.
"""

import json
import sqlite3

import pytest

from hub.core import agent_items, agent_runs, channels, ingest, outbox
from hub.core.db import WORK_ID_BASE, db_of

from .conftest import csrf_headers, insert_account, insert_project, login_via_code, make_bearer_token

NOW_MS = 1_700_000_000_000  # FakeClock по умолчанию


def _ingest(db, account_id: int, events: list[dict], now_ms: int = NOW_MS - 60_000) -> None:
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
    kind: str = "dm",
    text: str | None = None,
    mentions_me: bool = False,
    is_me: bool = False,
    thread: str | None = None,
    sender: str = "Пётр",
    title: str | None = None,
) -> dict:
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind, "title": title or f"чат {chat}"},
        "ext_id": ext_id,
        "ts": ts,
        "sender": {"ext_id": "1", "is_me": True} if is_me else {"ext_id": f"s-{sender}", "name": sender},
        "is_me": is_me,
        "kind": "text",
        "text": text if text is not None else f"сообщение {ext_id}",
        "mentions_me": mentions_me,
    }
    if thread is not None:
        event["thread_ext_id"] = thread
    return event


def _channel_id(db, chat: str, schema: str = "main") -> int:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (chat,),
        ).fetchone()[0]


def _message_id(db, channel_id: int, ext_id: str) -> int:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {db_of(channel_id)}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext_id),
        ).fetchone()[0]


def _set_project(db, channel_id: int, project_id: int | None) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {db_of(channel_id)}.channel SET project_id = ? WHERE id = ?",  # noqa: S608
            (project_id, channel_id),
        )
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _draft(db, channel_id: int, token_id_name: str, text: str, *, reply_to=None, thread=None, key="k") -> int:
    with db.reader() as conn:
        token_id = conn.execute("SELECT id FROM main.api_token WHERE name = ?", (token_id_name,)).fetchone()[
            0
        ]
    result = db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            reply_to_ext_id=reply_to,
            thread_ext_id=thread,
            origin="api",
            api_token_id=token_id,
            idempotency_key=key,
            now_ms=NOW_MS,
        )
    )
    assert result.status == "pending_approval"
    return result.outbox_id


@pytest.fixture
def world(db):
    """Проект A (только main) с беседой «a», проект W с work-беседой «w», токены профилей hub-wake."""
    acc = insert_account(db, source="telegram")
    acc_work = insert_account(db, source="slack", db_name="work")
    project_a = insert_project(db, name="Umbrella", color="#ff0000")
    project_w = insert_project(db, name="Globex")
    _ingest(db, acc, [_msg("a", "5", NOW_MS - 50_000, title="Покупатель")])
    _ingest(db, acc_work, [_msg("w", "1.0", NOW_MS - 50_000, kind="channel", title="dev")])
    ch_a = _channel_id(db, "a")
    ch_w = _channel_id(db, "w", "work")
    _set_project(db, ch_a, project_a)
    _set_project(db, ch_w, project_w)
    token_a = make_bearer_token(
        db, scopes=("read", "draft", "runs"), project_ids=[project_a], name=f"mcp:wake-{project_a}"
    )
    token_w = make_bearer_token(
        db, scopes=("read", "draft", "runs"), project_ids=[project_w], name=f"mcp:wake-{project_w}"
    )
    return {
        "acc": acc,
        "acc_work": acc_work,
        "a": project_a,
        "w": project_w,
        "ch_a": ch_a,
        "ch_w": ch_w,
        "token_a": token_a,
        "token_w": token_w,
    }


# --- T1: agent_run ---


def _run(
    db,
    project_id: int,
    *,
    kind="urgent",
    status="running",
    started_ts=NOW_MS,
    finished_ts=None,
    card_ids=(),
    channel_ids=(),
    draft_ids=None,
    result=None,
    failure=None,
    session_id=None,
    num_turns=None,
    duration_ms=None,
) -> int:
    """Строка `agent_run` напрямую: с Ф57-2 запуски дежурной не пишутся через API, строки заводят сессии хаба
    (`hub_sessions.py`, `held_work.py`) — тест читает то, что лежит в базе."""

    def _fn(conn):
        schema = agent_runs.schema_for_project(conn, project_id)
        cur = conn.execute(
            f"INSERT INTO {schema}.agent_run (project_id, kind, status, started_ts, finished_ts, "  # noqa: S608
            "card_ids_json, channel_ids_json, draft_ids_json, result, failure, session_id, num_turns, "
            "duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                kind,
                status,
                started_ts,
                finished_ts,
                json.dumps(list(card_ids)),
                json.dumps(list(channel_ids)),
                None if draft_ids is None else json.dumps(list(draft_ids)),
                result,
                failure,
                session_id,
                num_turns,
                duration_ms,
            ),
        )
        return cur.lastrowid

    return db.write_sync(_fn)


async def test_t1_get_by_project_token(client, db, world):
    draft_id = _draft(db, world["ch_a"], f"mcp:wake-{world['a']}", "Черновик")
    secret = "Итог: положил черновик.\nВторая строка итога."
    run_id = _run(
        db,
        world["a"],
        kind="urgent",
        status="ok",
        finished_ts=NOW_MS + 5000,
        card_ids=[7],
        channel_ids=[world["ch_a"]],
        draft_ids=[draft_id],
        result=secret,
        session_id="sid-1",
        num_turns=3,
        duration_ms=4000,
    )
    assert run_id < WORK_ID_BASE  # в проекте нет work-бесед — main
    r = await client.get(
        "/api/v1/agent/runs", params={"since": NOW_MS - 1}, headers=_bearer(world["token_a"])
    )
    assert r.status_code == 200, r.text
    [run] = r.json()
    assert run["id"] == run_id and run["status"] == "ok" and run["kind"] == "urgent"
    assert run["result"] == secret and run["draft_ids"] == [draft_id] and run["card_ids"] == [7]
    assert run["project_name"] == "Umbrella" and run["channel_titles"] == ["Покупатель"]
    assert run["channels"] == [{"id": world["ch_a"], "title": "Покупатель"}]
    assert (run["session_id"], run["num_turns"], run["duration_ms"]) == ("sid-1", 3, 4000)


async def test_t1_old_digest_kind_is_still_shown(client, db, world):
    """Ф57-2: режима «сводка» нет, но старая строка запуска с видом `digest` читается и показывается."""
    run_id = _run(db, world["a"], kind="digest", status="ok")
    r = await client.get("/api/v1/agent/runs", headers=_bearer(world["token_a"]))
    assert [(run["id"], run["kind"]) for run in r.json()] == [(run_id, "digest")]


async def test_t1_work_file_for_project_with_work_channel(client, db, world):
    run_id = _run(db, world["w"], kind="reply", channel_ids=[world["ch_w"]])
    assert run_id >= WORK_ID_BASE and db_of(run_id) == "work"
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.agent_run").fetchone()[0] == 0
    r = await client.get("/api/v1/agent/runs", headers=_bearer(world["token_w"]))
    assert [run["id"] for run in r.json()] == [run_id]


async def test_t1_write_routes_are_gone(client, db, clock, world):
    """Ф57-2: `POST`/`PATCH /agent/runs` убраны вместе с дежурной — ни токен с `runs`, ни UI их не пишут."""
    run_id = _run(db, world["a"])
    body = {"project_id": world["a"], "kind": "urgent"}
    r = await client.post("/api/v1/agent/runs", json=body, headers=_bearer(world["token_a"]))
    assert r.status_code in (404, 405)
    r = await client.patch(
        f"/api/v1/agent/runs/{run_id}", json={"status": "ok"}, headers=_bearer(world["token_a"])
    )
    assert r.status_code in (404, 405)
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post("/api/v1/agent/runs", json=body, headers=csrf_headers(cookie))
    assert r.status_code in (404, 405)
    r = await client.get("/api/v1/agent/runs")  # чтение UI — все проекты
    assert [run["id"] for run in r.json()] == [run_id]


async def test_t1_get_filters_by_project_and_since(client, db, clock, world):
    ids = []
    for pid in (world["a"], world["w"], world["a"]):
        ids.append(_run(db, pid, kind="reply", started_ts=NOW_MS + 1000 * len(ids)))
    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/agent/runs")
    assert [run["id"] for run in r.json()] == [ids[2], ids[1], ids[0]]  # новые сверху, оба файла
    r = await client.get("/api/v1/agent/runs", params={"project": world["a"]})
    assert [run["id"] for run in r.json()] == [ids[2], ids[0]]
    r = await client.get("/api/v1/agent/runs", params={"since": NOW_MS + 1000})
    assert [run["id"] for run in r.json()] == [ids[2], ids[1]]
    # проектный токен видит только свой проект; чужой проект фильтром — 404
    client.cookies.clear()
    r = await client.get("/api/v1/agent/runs", headers=_bearer(world["token_a"]))
    assert [run["id"] for run in r.json()] == [ids[2], ids[0]]
    r = await client.get(
        "/api/v1/agent/runs", params={"project": world["w"]}, headers=_bearer(world["token_a"])
    )
    assert r.status_code == 404


@pytest.mark.parametrize("schema", ["main", "work"])
def test_p1_check_allows_reserved_values_and_rejects_unknown(db, schema):
    def _insert(kind: str, status: str):
        return db.write_sync(
            lambda conn: (
                conn.execute(
                    f"INSERT INTO {schema}.agent_run (project_id, kind, status, started_ts, card_ids_json, "  # noqa: S608
                    "channel_ids_json) VALUES (1, ?, ?, 0, '[]', '[]')",
                    (kind, status),
                ).lastrowid
            )
        )

    for kind in ("urgent", "reply", "digest", "work"):
        assert _insert(kind, "running")
    for status in ("running", "waiting", "ok", "failed", "paused", "retry", "stopped"):
        assert _insert("work", status)
    with pytest.raises(sqlite3.IntegrityError):
        _insert("chat", "running")
    with pytest.raises(sqlite3.IntegrityError):
        _insert("urgent", "done")


# --- T2: /outbox с адресом ---


async def test_t2_outbox_address_reply_thread_run_and_sent_since(client, db, clock, world):
    # Slack-подобная беседа проекта A: родитель треда и ответ в тред
    _ingest(
        db,
        world["acc"],
        [
            _msg(
                "g", "100", NOW_MS - 40_000, kind="group", text="Родитель треда " + "х" * 300, title="общий"
            ),
            _msg("g", "101", NOW_MS - 30_000, kind="group", thread="100", sender="Анна"),
        ],
    )
    ch_g = _channel_id(db, "g")
    _set_project(db, ch_g, world["a"])
    wake_name = f"mcp:wake-{world['a']}"
    d_reply = _draft(db, world["ch_a"], wake_name, "Ответ Петру", reply_to="5", key="k1")
    d_thread = _draft(db, ch_g, wake_name, "В тред", thread="100", key="k2")
    other = make_bearer_token(db, scopes=("read", "draft"), name="hub-all")
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": world["ch_a"], "text": "от другого клиента", "idempotency_key": "k3"},
        headers=_bearer(other),
    )
    d_other = r.json()["outbox_id"]
    # запуск Claude, положивший первые два черновика
    run_id = _run(db, world["a"], status="ok", draft_ids=[d_reply, d_thread])

    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"})
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()}
    reply = rows[d_reply]
    assert reply["project_id"] == world["a"] and reply["project_name"] == "Umbrella"
    assert reply["channel_title"] == "Покупатель" and reply["source"] == "telegram" and reply["kind"] == "dm"
    assert reply["origin"] == "api" and reply["author"] == "Claude · Umbrella"
    assert reply["reply_to"] == {"ext_id": "5", "sender": "Пётр", "excerpt": "сообщение 5"}
    assert reply["thread"] is None and reply["run_id"] == run_id
    thread = rows[d_thread]
    assert thread["reply_to"] is None and thread["run_id"] == run_id
    assert thread["thread"]["ext_id"] == "100"
    assert thread["thread"]["excerpt"] == ("Родитель треда " + "х" * 300)[:200]
    assert rows[d_other]["author"] == "hub-all" and rows[d_other]["run_id"] is None

    # отправленное: фильтр по sent_ts >= since
    def _sent(oid: int, sent_ts: int) -> None:
        db.write_sync(
            lambda conn: conn.execute(
                f"UPDATE {db_of(oid)}.outbox SET status = 'sent', sent_ts = ? WHERE id = ?",  # noqa: S608
                (sent_ts, oid),
            )
        )

    _sent(d_reply, NOW_MS - 86_400_000)
    _sent(d_thread, NOW_MS + 10)
    r = await client.get("/api/v1/outbox", params={"status": "sent", "since": NOW_MS})
    assert [row["id"] for row in r.json()] == [d_thread]
    r = await client.get("/api/v1/outbox", params={"status": "sent"})
    assert [row["id"] for row in r.json()] == [d_thread, d_reply]
    # UI-отправка — автор «ты»
    ui_row = db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=world["ch_a"],
            text="сам",
            reply_to_ext_id=None,
            thread_ext_id=None,
            origin="ui",
            api_token_id=1,
            idempotency_key="ui",
            now_ms=NOW_MS,
        )
    )
    _sent(ui_row.outbox_id, NOW_MS + 20)
    r = await client.get("/api/v1/outbox", params={"status": "sent", "since": NOW_MS})
    assert [(row["id"], row["author"]) for row in r.json()] == [
        (ui_row.outbox_id, "ты"),
        (d_thread, "Claude · Umbrella"),
    ]


async def test_t2_outbox_address_respects_scope(client, db, world):
    _draft(db, world["ch_w"], f"mcp:wake-{world['w']}", "в work", key="w1")
    r = await client.get(
        "/api/v1/outbox", params={"status": "pending_approval"}, headers=_bearer(world["token_a"])
    )
    assert r.json() == []


# --- T3: «Нужна реакция» ---


async def test_t3_home_react(client, db, clock, world):
    acc = world["acc"]
    # react: личка проекта A с непрочитанным (беседа «a» уже есть, сообщение 5 — NOW-50 с)
    # info с упоминанием: группа проекта A (группа по умолчанию — «к сведению»), упоминание поднимает до react
    _ingest(
        db,
        acc,
        [
            _msg("info", "1", NOW_MS - 45_000, kind="group", title="команда"),
            _msg(
                "info", "2", NOW_MS - 20_000, kind="group", mentions_me=True, sender="Анна", title="команда"
            ),
            _msg("info", "3", NOW_MS - 10_000, kind="group", title="команда"),
        ],
    )
    ch_info = _channel_id(db, "info")
    _set_project(db, ch_info, world["a"])
    # Неразобранное: группа (режим «разбор моделью») с открытой карточкой urgent
    _ingest(db, acc, [_msg("uns", "1", NOW_MS - 30_000, kind="group", sender="Банк", title="банк")])
    ch_uns = _channel_id(db, "uns")
    mid_uns = _message_id(db, ch_uns, "1")
    db.write_sync(
        lambda conn: agent_items.upsert(
            conn,
            channel_id=ch_uns,
            message_ids=[mid_uns],
            label="urgent",
            summary="Банк просит код",
            action=None,
            provider="ollama",
            model="m",
            api_token_id=None,
            now_ms=NOW_MS,
        )
    )
    # прочитанная личка — нет
    _ingest(db, acc, [_msg("read", "1", NOW_MS - 5_000, title="прочитано")])
    ch_read = _channel_id(db, "read")
    _set_project(db, ch_read, world["a"])
    mid_read = _message_id(db, ch_read, "1")
    db.write_sync(lambda conn: channels.mark_read(conn, ch_read, up_to_message_id=mid_read, now_ms=NOW_MS))
    # группа «к сведению» без упоминания — нет
    _ingest(db, acc, [_msg("quiet", "1", NOW_MS - 1_000, kind="group", title="тихо")])
    _set_project(db, _channel_id(db, "quiet"), world["a"])
    # черновик в личке
    _draft(db, world["ch_a"], f"mcp:wake-{world['a']}", "черновик", key="d")

    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/home/react")
    assert r.status_code == 200, r.text
    items = r.json()
    assert [item["channel"]["id"] for item in items] == [ch_info, ch_uns, world["ch_a"]]
    info, uns, dm = items
    assert info["last"]["sender"] == "Анна" and info["last"]["excerpt"] == "сообщение 2"
    assert (
        info["last"]["message_id"] == _message_id(db, ch_info, "2") and info["channel"]["unread_react"] == 1
    )
    assert info["project"] == {"id": world["a"], "name": "Umbrella", "color": "#ff0000"}
    assert info["card"] is None and info["draft_count"] == 0
    assert uns["project"]["id"] == -1 and uns["card"]["label"] == "urgent"
    assert uns["card"]["summary"] == "Банк просит код" and uns["last"]["sender"] == "Банк"
    assert dm["draft_count"] == 1 and dm["last"]["excerpt"] == "сообщение 5"
    assert dm["channel"]["title"] == "Покупатель" and dm["channel"]["source"] == "telegram"

    # прочитали карточку Неразобранного — ушла
    db.write_sync(lambda conn: channels.mark_read(conn, ch_uns, up_to_message_id=mid_uns, now_ms=NOW_MS))
    r = await client.get("/api/v1/home/react")
    assert [item["channel"]["id"] for item in r.json()] == [ch_info, world["ch_a"]]

    # проектный токен: только свой проект, без Неразобранного
    client.cookies.clear()
    r = await client.get("/api/v1/home/react", headers=_bearer(world["token_w"]))
    assert r.json() == []


# --- Слияние Ф8-4 + Ф8-6: удалённое владельцем у себя в хабе на главную не попадает ---


async def test_merge_hidden_message_not_in_home_briefs(client, db, world):
    from hub.core import hiding

    wake_name = f"mcp:wake-{world['a']}"
    _draft(db, world["ch_a"], wake_name, "Ответ Петру", reply_to="5", key="h1")
    hidden_id = _message_id(db, world["ch_a"], "5")
    db.write_sync(lambda conn: hiding.hide_message(conn, hidden_id, now_ms=NOW_MS))
    r = await client.get(
        "/api/v1/outbox", params={"status": "pending_approval"}, headers=_bearer(world["token_a"])
    )
    (draft,) = r.json()
    # адрес черновика есть, а превью скрытого сообщения, на которое ответ, — нет
    assert draft["channel_id"] == world["ch_a"]
    assert draft["reply_to"] == {"ext_id": "5", "sender": None, "excerpt": None}


async def test_merge_hidden_own_message_not_in_outbox_list(client, db, world):
    from hub.core import hiding

    outbox_id = _draft(db, world["ch_a"], f"mcp:wake-{world['a']}", "мой текст", key="h2")
    with db.reader() as conn:
        placeholder = conn.execute(
            f"SELECT id FROM {db_of(outbox_id)}.message WHERE outbox_id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()[0]
    db.write_sync(lambda conn: hiding.hide_message(conn, placeholder, now_ms=NOW_MS))
    for status in ("pending_approval", "cancelled"):
        r = await client.get("/api/v1/outbox", params={"status": status}, headers=_bearer(world["token_a"]))
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            assert all(item["id"] != outbox_id for item in r.json())


# --- Ф57-2: старый скоуп `runs` ---


async def test_s5_legacy_runs_scope_token_still_reads(client, db, world):
    """Токен со старым скоупом `runs` (профиль убранной дежурной) читается без падения: чтение запусков
    работает, как у любого токена со скоупом `read`; записи запусков больше нет."""
    run_id = _run(db, world["a"])
    r = await client.get("/api/v1/agent/runs", headers=_bearer(world["token_a"]))
    assert r.status_code == 200 and [run["id"] for run in r.json()] == [run_id]


async def test_archived_channel_not_in_home_react_nor_project_counters(client, db, clock, world):
    """23.10: беседа в архиве с непрочитанным «реагирую» не держит «Нужна реакция» и счётчики проекта
    (дежурная Initech не могла убрать архивные письма из «Ждёт»: hub_handled на архив — 422)."""
    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/home/react")
    assert world["ch_a"] in [item["channel"]["id"] for item in r.json()]
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[world["a"]]["unread_react"] > 0 and projects[world["a"]]["unread"] > 0

    db.write_sync(lambda conn: channels.update_channel(conn, world["ch_a"], archived=True, now_ms=NOW_MS))
    r = await client.get("/api/v1/home/react")
    assert world["ch_a"] not in [item["channel"]["id"] for item in r.json()]
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[world["a"]]["unread_react"] == 0 and projects[world["a"]]["unread"] == 0


async def test_archived_channel_not_in_inbox(client, db, clock, world):
    """23.10: hub_inbox (`GET /inbox`) не показывает беседы в архиве, даже с непрочитанным."""
    await login_via_code(client, db, clock)
    ids = [row["id"] for row in (await client.get("/api/v1/inbox")).json()]
    assert world["ch_a"] in ids
    db.write_sync(lambda conn: channels.update_channel(conn, world["ch_a"], archived=True, now_ms=NOW_MS))
    ids = [row["id"] for row in (await client.get("/api/v1/inbox")).json()]
    assert world["ch_a"] not in ids
