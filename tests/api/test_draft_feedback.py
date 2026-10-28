"""Ф19-1 (docs/architecture.md): правки и отказы черновиков — раздел H и поправки П1–П3,
П6–П9, П11. Миграция 0032 (`outbox.draft_text`/`draft_ts`), запись правки в `outbox.approve` и аудит `edited`,
стирание вместе с текстом (C), эмодзи в аудите реакций (D), ручка `GET /outbox/feedback` (E).

Черновики сессий кладутся `outbox.enqueue(origin='api')` с токеном сессии; решения владельца — через публичное
приложение под cookie-сессией (аудит `actor = 'ui'`), отмена сессией — bearer-токеном."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import db as db_module
from hub.core import outbox, retention, security
from hub.core.db import Db, db_of

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

API = "/api/v1"
MIN_MS = 60 * 1000


# --- обвязка ------------------------------------------------------------------------------------------------


def _now(clock) -> int:
    return int(clock() * 1000)


def _token(db, *, project_ids=None, scopes=("read", "draft"), name="mcp:sess"):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind="interactive", scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _draft(db, clock, channel_id, token_id, text="черновик сессии", *, thread=None):
    """Черновик сессии (`pending_approval`) -> (outbox_id, id заготовки)."""
    result = db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            thread_ext_id=thread,
            origin="api",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=_now(clock),
        )
    )
    assert result.status == "pending_approval"
    return result.outbox_id, result.message_id


def _row(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT text, draft_text, draft_ts, status FROM {db_of(outbox_id)}.outbox WHERE id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()
    return {"text": row[0], "draft_text": row[1], "draft_ts": row[2], "status": row[3]}


def _audit_detail(db, action, outbox_id) -> list:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = ? AND outbox_id = ? AND actor = 'ui' "
            "ORDER BY id",
            (action, outbox_id),
        ).fetchall()
    return [json.loads(r[0]) if r[0] else None for r in rows]


@pytest.fixture
async def bearer_client(app):
    """Отдельный клиент для bearer-запросов: у `client` после входа кука UI-сессии (bearer вместе с кукой —
    400)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


async def _approve(client, ui, outbox_id, text=None):
    body = {} if text is None else {"text": text}
    r = await client.post(f"{API}/outbox/{outbox_id}/approve", json=body, headers=ui)
    assert r.status_code == 200, r.text


async def _cancel(client, headers, outbox_id, status=200):
    r = await client.post(f"{API}/outbox/{outbox_id}/cancel", headers=headers)
    assert r.status_code == status, r.text


async def _feedback(client, headers, **params) -> list[dict]:
    r = await client.get(f"{API}/outbox/feedback", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def world(db):
    """Проект 1 с беседами в main (Telegram) и work (Slack), проект 2 — со своей беседой; токен сессии
    проекта 1."""
    p1 = insert_project(db, name="P1")
    p2 = insert_project(db, name="P2")
    tg = insert_account(db, source="telegram")
    sl = insert_account(db, source="slack", db_name="work")
    _token_value, token_id = _token(db, project_ids=[p1])
    return {
        "p1": p1,
        "p2": p2,
        "main": insert_channel(db, tg, project_id=p1, ext_id="u1", title="Пётр"),
        "main2": insert_channel(db, tg, project_id=p1, ext_id="u2", title="Анна"),
        "work": insert_channel(db, sl, project_id=p1, ext_id="C1", kind="group", db_name="work", title="dev"),
        "other": insert_channel(db, tg, project_id=p2, ext_id="u3", title="Чужой"),
        "token": _token_value,
        "token_id": token_id,
    }


# --- H1: миграция 0032 на прошлой версии обоих файлов ----------------------------------------------------


def test_h1_migration_0032_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_31(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 31}

    monkeypatch.setattr(db_module, "_migrations", _up_to_31)
    old = Db(tmp_path)
    old.start()
    old_ids = {}
    try:
        _value, old_token_id = _token(old)
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)
            ch = insert_channel(old, acc, ext_id="c1", db_name=schema)
            result = old.write_sync(
                lambda conn, ch=ch: outbox.enqueue(
                    conn,
                    channel_id=ch,
                    text="старый",
                    origin="api",
                    api_token_id=old_token_id,
                    idempotency_key=None,
                    now_ms=1000,
                )
            )
            old_ids[schema] = result.outbox_id
        with old.reader() as conn:
            for schema in ("main", "work"):
                # work — по последней общей (0029): 0030/0031 — только main.
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == (
                    31 if schema == "main" else 29
                )
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert "draft_text" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 32
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert {"draft_text", "draft_ts"} <= cols
                index = conn.execute(
                    f"SELECT sql FROM {schema}.sqlite_master WHERE type = 'index' AND name = ?",  # noqa: S608
                    ("outbox_draft_edited",),
                ).fetchone()
                assert index is not None and "draft_text IS NOT NULL" in index[0]
                row = conn.execute(
                    f"SELECT text, draft_text, draft_ts FROM {schema}.outbox WHERE id = ?",  # noqa: S608
                    (old_ids[schema],),
                ).fetchone()
                assert row == ("старый", None, None)
    finally:
        new.close()


# --- H2: запись правки при подтверждении -------------------------------------------------------------------


async def test_h2_approve_with_edit_keeps_draft_and_audits_edited(client, db, clock, ui, world):
    oid, _ = _draft(db, clock, world["main"], world["token_id"], "Привет!\nПосмотрю завтра.")
    clock.advance_ms(5000)
    await _approve(client, ui, oid, "Привет! Посмотрю сегодня.")
    row = _row(db, oid)
    assert row["draft_text"] == "Привет!\nПосмотрю завтра." and row["text"] == "Привет! Посмотрю сегодня."
    assert row["draft_ts"] == _now(clock)
    assert _audit_detail(db, "outbox_approve", oid) == [{"text_len": 25, "edited": True}]


@pytest.mark.parametrize(
    "new_text",
    [
        "Как было",  # тем же текстом
        "  Как было\n",  # только пробелы и переводы строк по краям
        "Как было\r\n",  # \r\n у края
    ],
)
async def test_h2_approve_same_text_is_not_edit(client, db, clock, ui, world, new_text):
    oid, _ = _draft(db, clock, world["main"], world["token_id"], "Как было")
    await _approve(client, ui, oid, new_text)
    row = _row(db, oid)
    assert row["draft_text"] is None and row["draft_ts"] is None
    assert _audit_detail(db, "outbox_approve", oid) == [{"text_len": len(new_text), "edited": False}]


async def test_h2_inner_newlines_are_an_edit(client, db, clock, ui, world):
    """П5: `_norm` — только `\\r\\n` и края; перенос абзаца внутри — правка."""
    oid, _ = _draft(db, clock, world["main"], world["token_id"], "Первый абзац. Второй абзац.")
    await _approve(client, ui, oid, "Первый абзац.\n\nВторой абзац.")
    assert _row(db, oid)["draft_text"] == "Первый абзац. Второй абзац."


async def test_h2_approve_without_text_no_edited_key(client, db, clock, ui, world):
    oid, _ = _draft(db, clock, world["main"], world["token_id"])
    await _approve(client, ui, oid)
    assert _row(db, oid)["draft_text"] is None
    assert _audit_detail(db, "outbox_approve", oid) == [None]


# --- H3: стирание вместе с текстом -------------------------------------------------------------------------


async def test_h3_hide_message_clears_draft_text(client, db, clock, ui, world):
    oid, placeholder = _draft(db, clock, world["main"], world["token_id"], "исходный")
    await _approve(client, ui, oid, "правленый")
    assert _row(db, oid)["draft_text"] == "исходный"
    r = await client.delete(f"{API}/messages/{placeholder}", headers=ui)
    assert r.status_code == 204, r.text
    row = _row(db, oid)
    assert row["text"] == "" and row["draft_text"] is None and row["status"] == "cancelled"


async def test_h3_hide_channel_clears_draft_text(client, db, clock, ui, world):
    oid, _ = _draft(db, clock, world["work"], world["token_id"], "исходный")
    await _approve(client, ui, oid, "правленый")
    r = await client.delete(f"{API}/channels/{world['work']}", headers=ui)
    assert r.status_code == 204, r.text
    row = _row(db, oid)
    assert row["text"] == "" and row["draft_text"] is None


async def test_h3_clear_text_if_hidden_clears_draft_text(client, db, clock, ui, world):
    """Эхо/отчёт после скрытия заготовки (`_clear_text_if_hidden`): строка в конечном статусе."""
    oid, placeholder = _draft(db, clock, world["main"], world["token_id"], "исходный")
    await _approve(client, ui, oid, "правленый")

    def _fn(conn):
        conn.execute("UPDATE main.outbox SET status = 'sent' WHERE id = ?", (oid,))
        conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (placeholder,))
        outbox._clear_text_if_hidden(conn, "main", oid, placeholder)

    db.write_sync(_fn)
    row = _row(db, oid)
    assert row["text"] == "" and row["draft_text"] is None


@pytest.mark.parametrize("final_text", ["правленый", ""])
async def test_h3_retention_clears_draft_text(client, db, clock, ui, world, final_text):
    """Ретенция (функция напрямую). П8: правленый с пустым текстом (как файл с пустой подписью) — не
    пропускается, пока у строки есть `draft_text`."""
    oid, _ = _draft(db, clock, world["work"], world["token_id"], "исходный")
    await _approve(client, ui, oid, "правленый")

    def _fn(conn):
        conn.execute("UPDATE work.outbox SET status = 'sent', text = ? WHERE id = ?", (final_text, oid))
        return retention._purge_outbox_portion(conn, after_id=0, cutoff=_now(clock) + 1)

    cleaned, _last, _more = db.write_sync(_fn)
    assert cleaned == 1
    row = _row(db, oid)
    assert row["text"] == "" and row["draft_text"] is None


# --- H4: эмодзи в аудите реакций ---------------------------------------------------------------------------


async def test_h4_reaction_audit_has_normalized_emoji(client, db, clock, ui, world):
    """D + П11: `emoji` в `detail` у `reaction_set` и `reaction_off` — после `normalize_emoji` (Telegram без
    U+FE0F). Сессия — `tests/api/test_session_reactions.py::test_f1_project_off_403_on_queued_with_pause`."""
    m = insert_message(db, world["main"], ext_id="10", ts=1000, text="входящее")
    r = await client.post(f"{API}/messages/{m}/reactions", json={"emoji": "❤️", "on": True}, headers=ui)
    assert r.status_code == 200, r.text
    r = await client.post(f"{API}/messages/{m}/reactions", json={"emoji": "❤️", "on": False}, headers=ui)
    assert r.status_code == 200, r.text
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = ? WHERE id = (SELECT account_id FROM main.channel "
            "WHERE id = ?)",
            (json.dumps({"reactions": False}), world["main"]),
        )
    )
    r = await client.post(f"{API}/messages/{m}/reactions", json={"emoji": "👍", "on": True}, headers=ui)
    assert r.json() == {"status": "off"}
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT action, detail_json FROM main.audit WHERE action IN ('reaction_set', 'reaction_off') "
            "ORDER BY id"
        ).fetchall()
    assert [(a, json.loads(d)["emoji"], json.loads(d)["on"]) for a, d in rows] == [
        ("reaction_set", "❤", True),
        ("reaction_set", "❤", False),
        ("reaction_off", "👍", True),
    ]


# --- H5: GET /outbox/feedback -------------------------------------------------------------------------------


async def test_h5_p6_route_is_not_outbox_id(client, ui):
    """П6: `/outbox/feedback` объявлена выше `/outbox/{outbox_id}` — 200, а не 422."""
    r = await client.get(f"{API}/outbox/feedback", headers=ui)
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_h5_edited_and_rejected_in_both_files_ordered(client, db, clock, ui, world):
    edited_main, _ = _draft(db, clock, world["main"], world["token_id"], "было main")
    rejected_work, _ = _draft(db, clock, world["work"], world["token_id"], "было work")
    rejected_main, _ = _draft(db, clock, world["main2"], world["token_id"], "было main2")
    edited_work, _ = _draft(db, clock, world["work"], world["token_id"], "было work 2", thread="1.000")

    clock.advance_ms(MIN_MS)
    await _approve(client, ui, edited_main, "стало main")
    t_edited_main = _now(clock)
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, rejected_work)
    t_rejected_work = _now(clock)
    clock.advance_ms(MIN_MS)
    await _approve(client, ui, edited_work, "стало work 2")
    t_edited_work = _now(clock)
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, rejected_main)
    t_rejected_main = _now(clock)

    items = await _feedback(client, ui)
    assert [(i["id"], i["kind"], i["decided_ts"]) for i in items] == [
        (rejected_main, "rejected", t_rejected_main),
        (edited_work, "edited", t_edited_work),
        (rejected_work, "rejected", t_rejected_work),
        (edited_main, "edited", t_edited_main),
    ]
    by_id = {i["id"]: i for i in items}
    assert by_id[edited_main]["draft_text"] == "было main" and by_id[edited_main]["text"] == "стало main"
    assert by_id[edited_main]["status"] == "queued" and "instead" not in by_id[edited_main]
    assert by_id[edited_work]["thread_ext_id"] == "1.000" and by_id[edited_work]["source"] == "slack"
    rejected = by_id[rejected_work]
    assert rejected["draft_text"] == "было work" and rejected["status"] == "cancelled"
    assert rejected["instead"] is None
    assert rejected["channel_title"] == "dev" and rejected["project_id"] == world["p1"]
    assert rejected["files"] == 0

    # limit
    assert [i["id"] for i in await _feedback(client, ui, limit=2)] == [rejected_main, edited_work]
    # since — по времени решения
    assert [i["id"] for i in await _feedback(client, ui, since=t_rejected_work)] == [
        rejected_main,
        edited_work,
        rejected_work,
    ]
    # channel_id
    assert [i["id"] for i in await _feedback(client, ui, channel_id=world["work"])] == [
        edited_work,
        rejected_work,
    ]


async def test_h5_session_cancel_and_hiding_are_not_rejections(client, bearer_client, db, clock, ui, world):
    by_session, _ = _draft(db, clock, world["main"], world["token_id"], "сама отменила")
    await _cancel(bearer_client, _bearer(world["token"]), by_session)  # hub_cancel_draft
    by_hiding, placeholder = _draft(db, clock, world["main"], world["token_id"], "скрыл")
    r = await client.delete(f"{API}/messages/{placeholder}", headers=ui)
    assert r.status_code == 204, r.text
    assert _row(db, by_session)["status"] == "cancelled" and _row(db, by_hiding)["status"] == "cancelled"
    assert await _feedback(client, ui) == []


async def test_h5_p2_approved_then_cancelled_is_not_rejected(client, db, clock, ui, world):
    """П2: подтвердил, потом снял из `queued` — не отказ; правленая такая строка — только `edited`."""
    plain, _ = _draft(db, clock, world["main"], world["token_id"], "без правки")
    edited, _ = _draft(db, clock, world["main2"], world["token_id"], "с правкой")
    await _approve(client, ui, plain)
    await _approve(client, ui, edited, "правка владельца")
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, plain)
    await _cancel(client, ui, edited)
    assert _row(db, plain)["status"] == "cancelled" and _row(db, edited)["status"] == "cancelled"
    items = await _feedback(client, ui)
    assert [(i["id"], i["kind"], i["status"]) for i in items] == [(edited, "edited", "cancelled")]


async def test_h5_p3_hidden_edited_not_returned(client, db, clock, ui, world):
    """П3: правленый, чья строка ленты скрыта у себя, не отдаётся (даже если текст ещё не стёрт)."""
    oid, placeholder = _draft(db, clock, world["main"], world["token_id"], "исходный")
    await _approve(client, ui, oid, "правленый")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (placeholder,))
    )
    assert _row(db, oid)["draft_text"] == "исходный"
    assert await _feedback(client, ui) == []


async def test_h5_instead_in_window_and_out_of_window(client, db, clock, ui, world):
    """`instead` — первое своё сообщение владельца в той же беседе в окне (−10 мин, +30 мин] вокруг отказа."""
    early, _ = _draft(db, clock, world["main"], world["token_id"], "ответила бы сессия")
    late, _ = _draft(db, clock, world["main2"], world["token_id"], "и тут")
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, early)
    t_early = _now(clock)
    await _cancel(client, ui, late)
    t_late = _now(clock)
    # Владелец ответил сам с телефона ДО отказа (окно назад) — в беседе `main`.
    insert_message(db, world["main"], ext_id="100", ts=t_early - 5 * MIN_MS, text="сам с телефона", is_me=1)
    insert_message(db, world["main"], ext_id="101", ts=t_early + 2 * MIN_MS, text="второе своё", is_me=1)
    # Чужое (входящее) в окне — не «сам».
    insert_message(db, world["main2"], ext_id="200", ts=t_late + MIN_MS, text="входящее", is_me=0)
    # Своё в другой беседе в окне — не попадает.
    insert_message(db, world["other"], ext_id="300", ts=t_late + MIN_MS, text="в чужой беседе", is_me=1)
    # Своё в беседе `main2`, но вне окна (+40 мин и −20 мин).
    insert_message(db, world["main2"], ext_id="201", ts=t_late + 40 * MIN_MS, text="поздно", is_me=1)
    insert_message(db, world["main2"], ext_id="199", ts=t_late - 20 * MIN_MS, text="рано", is_me=1)

    by_id = {i["id"]: i for i in await _feedback(client, ui)}
    instead = by_id[early]["instead"]
    assert instead["text"] == "сам с телефона" and instead["ts"] == t_early - 5 * MIN_MS
    assert by_id[late]["instead"] is None


async def test_h5_p1_session_drafts_are_not_instead(client, db, clock, ui, world):
    """П1: отклонил -> сессия положила новый черновик в той же беседе -> `instead = null`; подтверждённый
    владельцем черновик сессии (ушёл) — тоже не «написал сам». Своё из хаба (`origin = 'ui'`) — «сам»."""
    rejected, _ = _draft(db, clock, world["main"], world["token_id"], "первый")
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, rejected)
    clock.advance_ms(MIN_MS)
    second, _ = _draft(db, clock, world["main"], world["token_id"], "второй черновик")
    items = await _feedback(client, ui)
    assert [i["id"] for i in items] == [rejected] and items[0]["instead"] is None

    await _approve(client, ui, second)
    db.write_sync(lambda conn: conn.execute("UPDATE main.outbox SET status = 'sent' WHERE id = ?", (second,)))
    assert (await _feedback(client, ui))[0]["instead"] is None

    clock.advance_ms(MIN_MS)
    r = await client.post(
        f"{API}/messages/send",
        json={"channel_id": world["main"], "text": "написал сам из хаба", "idempotency_key": "k1"},
        headers=ui,
    )
    assert r.status_code == 200, r.text
    instead = (await _feedback(client, ui))[0]["instead"]
    assert instead["text"] == "написал сам из хаба" and instead["message_id"] == r.json()["message_id"]


async def test_h5_p1_session_album_elements_are_not_instead(client, db, clock, ui, world):
    """Ревью Ф19-1: элементы 1..N−1 альбома черновика сессии (`_apply_album`: `is_me=1`, `outbox_id` NULL,
    пустой текст) — не «написал сам» у соседнего отказа."""
    rejected, _ = _draft(db, clock, world["main"], world["token_id"], "первый")
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, rejected)
    clock.advance_ms(MIN_MS)
    album_oid, placeholder = _draft(db, clock, world["main"], world["token_id"], "подпись альбома")
    await _approve(client, ui, album_oid)

    def _fn(conn):
        conn.execute("UPDATE main.outbox SET status = 'sent' WHERE id = ?", (album_oid,))
        conn.execute("UPDATE main.message SET ext_id = '500' WHERE id = ?", (placeholder,))
        outbox._apply_album(
            conn,
            "main",
            outbox_id=album_oid,
            placeholder_id=placeholder,
            element0_id=placeholder,
            channel_id=world["main"],
            album=outbox.Album(["500", "501", "502"], "g1"),
            now_ms=_now(clock),
        )

    db.write_sync(_fn)
    item = (await _feedback(client, ui))[0]
    assert item["id"] == rejected
    assert item["instead"] is None, item["instead"]


async def test_h5_p9_instead_same_thread(client, db, clock, ui, world):
    """П9: черновик в треде — «сам» только из того же треда; черновик в ленте — только из ленты."""
    in_thread, _ = _draft(db, clock, world["work"], world["token_id"], "в тред", thread="1.000")
    in_feed, _ = _draft(db, clock, world["main"], world["token_id"], "в ленту")
    clock.advance_ms(MIN_MS)
    await _cancel(client, ui, in_thread)
    await _cancel(client, ui, in_feed)
    t = _now(clock)
    insert_message(db, world["work"], ext_id="2.000", ts=t + MIN_MS, text="в ленте канала", is_me=1)
    insert_message(
        db,
        world["work"],
        ext_id="3.000",
        ts=t + MIN_MS,
        text="в другом треде",
        is_me=1,
        thread_ext_id="9.000",
    )
    insert_message(
        db, world["main"], ext_id="50", ts=t + MIN_MS, text="в треде лички", is_me=1, thread_ext_id="49"
    )
    by_id = {i["id"]: i for i in await _feedback(client, ui)}
    assert by_id[in_thread]["instead"] is None and by_id[in_feed]["instead"] is None

    insert_message(
        db,
        world["work"],
        ext_id="4.000",
        ts=t + 2 * MIN_MS,
        text="в том же треде",
        is_me=1,
        thread_ext_id="1.000",
    )
    insert_message(db, world["main"], ext_id="51", ts=t + 2 * MIN_MS, text="в ленте лички", is_me=1)
    by_id = {i["id"]: i for i in await _feedback(client, ui)}
    assert by_id[in_thread]["instead"]["text"] == "в том же треде"
    assert by_id[in_feed]["instead"]["text"] == "в ленте лички"


async def test_h5_scope_project_token_does_not_see_other_project(client, bearer_client, db, clock, ui, world):
    mine, _ = _draft(db, clock, world["main"], world["token_id"], "мой")
    theirs, _ = _draft(db, clock, world["other"], world["token_id"], "чужой проект")
    await _approve(client, ui, mine, "мой правленый")
    await _cancel(client, ui, theirs)
    assert {i["id"] for i in await _feedback(client, ui)} == {mine, theirs}
    assert [i["id"] for i in await _feedback(bearer_client, _bearer(world["token"]))] == [mine]
    other_token, _ = _token(db, project_ids=[world["p2"]], name="mcp:other")
    assert [i["id"] for i in await _feedback(bearer_client, _bearer(other_token))] == [theirs]
    assert await _feedback(bearer_client, _bearer(world["token"]), channel_id=world["other"]) == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"since": -1}])
async def test_h5_bad_params_422(client, ui, params):
    r = await client.get(f"{API}/outbox/feedback", params=params, headers=ui)
    assert r.status_code == 422
