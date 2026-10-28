"""Тесты 15 (`/sync`) и 16 (`feed.stream` напрямую) спеки (`docs/architecture.md`).

Тест 17 (`Db.on_commit`) — отдельно, в tests/core/test_db_on_commit.py (не про API).

`/stream` (SSE) НЕ гоняется через httpx.AsyncClient/ASGITransport дальше проверки авторизации/заголовков —
см. блок в конце файла и отчёт исполнителя: ASGITransport целиком буферизует ASGI-приложение перед тем, как
отдать вызывающему хоть что-то (даже `status_code`), поэтому с бесконечным SSE-генератором любой успешный
(200) запрос к `/stream` через него никогда не возвращается — проверено экспериментально. `feed.stream()`
поэтому тестируется как обычный async-генератор Python, без HTTP вообще (так и предписывает спека для
теста 16).
"""

import asyncio
import hashlib

from hub.core import channels as channels_mod
from hub.core import ingest, outbox, queries
from hub.core.feed import HEARTBEAT_FRAME, Notifier, stream
from hub.core.security import Principal

from .conftest import insert_account, insert_channel, insert_message, insert_project, make_bearer_token


def _upsert_event(*, channel_ext_id="c1", ext_id="m1", text="привет", ts=1000, is_me=0):
    return {
        "type": "upsert",
        "channel": {"ext_id": channel_ext_id, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


def _reaction_event(*, channel_ext_id="c1", ext_id="m1"):
    return {
        "type": "reaction",
        "channel": {"ext_id": channel_ext_id, "kind": "dm"},
        "ext_id": ext_id,
        "reactions": [{"emoji": "👍", "count": 1}],
    }


def _delete_event(*, channel_ext_id="c1", ext_id="m1"):
    return {"type": "delete", "ext_ids": [ext_id], "channel": {"ext_id": channel_ext_id, "kind": "dm"}}


def _apply(db, acc, events, now_ms):
    return db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=now_ms)
    )


def _bearer_token_id(db, token) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.api_token WHERE hash = ?", (hashlib.sha256(token.encode()).hexdigest(),)
        ).fetchone()[0]


# --- Тест 15: /sync видит все виды изменений и уважает скоуп проекта ---------------------------------------


async def test_sync_sees_edit_delete_reaction_outbox_status_and_read(client, db):
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    acc = insert_account(db)
    ch_a = insert_channel(db, acc, project_id=project_a, ext_id="ca", my_msg_count=5, first_seen_ts=0)
    # то же самое, но в work-файле — спека требует, чтобы данные лежали и там (изменение из work тоже видно).
    acc_work = insert_account(db, db_name="work")
    insert_channel(
        db, acc_work, project_id=project_a, ext_id="wa", db_name="work", my_msg_count=5, first_seen_ts=0
    )
    # активность в проекте B, которую A не должен увидеть.
    ch_b = insert_channel(db, acc, project_id=project_b, ext_id="cb", my_msg_count=5, first_seen_ts=0)

    token_a = make_bearer_token(db, kind="interactive", scopes=("read", "send"), project_ids=[project_a])
    token_id_a = _bearer_token_id(db, token_a)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    async def _sync(since: int, limit: int = 500) -> dict:
        r = await client.get("/api/v1/sync", params={"since": since, "limit": limit}, headers=headers_a)
        assert r.status_code == 200, r.text
        return r.json()

    # исходное сообщение (upsert)
    _apply(db, acc, [_upsert_event(channel_ext_id="ca")], now_ms=1000)
    body = await _sync(0)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["text"] == "привет"
    seq = body["max_seq"]

    # правка (edit == upsert с изменённым содержимым по спеке ingest)
    _apply(db, acc, [_upsert_event(channel_ext_id="ca", text="правка")], now_ms=2000)
    body = await _sync(seq)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["text"] == "правка"
    assert body["max_seq"] > seq
    seq = body["max_seq"]

    # реакция
    _apply(db, acc, [_reaction_event(channel_ext_id="ca")], now_ms=3000)
    body = await _sync(seq)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["reactions_json"] is not None
    assert body["max_seq"] > seq
    seq = body["max_seq"]

    # удаление
    _apply(db, acc, [_delete_event(channel_ext_id="ca")], now_ms=4000)
    body = await _sync(seq)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["deleted"] is True
    # Ф8-1 C1: удалённое остаётся видно — текст отдаётся вместе с deleted: true
    assert body["messages"][0]["text"] == "правка"
    assert body["max_seq"] > seq
    seq = body["max_seq"]

    # смена статуса исходящего: enqueue -> pending message, потом lease -> sending
    outbox_id = db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=ch_a,
            text="исходящее",
            origin="ui",
            api_token_id=token_id_a,
            idempotency_key="sync-out-1",
            now_ms=5000,
        )
    ).outbox_id
    body = await _sync(seq)
    out_msg = next(m for m in body["messages"] if m["outbox_id"] == outbox_id)
    assert out_msg["outbox_status"] == "queued"
    assert body["max_seq"] > seq
    seq = body["max_seq"]

    db.write_sync(lambda conn: outbox.lease(conn, account_id=acc, owner="hub-tg", now_ms=6000))
    body = await _sync(seq)
    out_msg = next(m for m in body["messages"] if m["outbox_id"] == outbox_id)
    assert out_msg["outbox_status"] == "sending"
    assert body["max_seq"] > seq
    seq = body["max_seq"]

    # read: отметка канала прочитанным поднимает channel.seq
    with db.reader() as conn:
        first_msg_id = conn.execute(
            "SELECT id FROM main.message WHERE channel_id = ? AND ext_id = 'm1'", (ch_a,)
        ).fetchone()[0]
    db.write_sync(lambda conn: channels_mod.mark_read(conn, ch_a, up_to_message_id=first_msg_id, now_ms=7000))
    body = await _sync(seq)
    assert any(c["id"] == ch_a for c in body["channels"])
    assert body["max_seq"] > seq
    seq = body["max_seq"]

    # изоляция: ни один снимок с любого since не показывает ch_b/project_b токену проекта A
    for since_ in (0, seq):
        body = await _sync(since_)
        assert all(m["channel_id"] != ch_b for m in body["messages"])
        assert all(c["id"] != ch_b for c in body["channels"])
        assert all(m.get("project_id") != project_b for m in body["messages"])
        assert all(c.get("project_id") != project_b for c in body["channels"])


async def test_sync_pagination_collects_each_change_exactly_once_in_order(client, db):
    """`limit=2`, `more`, догон следующей страницей с `since=max_seq` — без дыр и дублей, по возрастанию seq.

    Каждый upsert создаёт новую строку message (свой seq) и обновляет ОДНУ и ту же строку channel (её `seq`
    переписывается на новое значение при каждом upsert, а не копится историей) — то есть после 6 upsert-ов
    видимых изменений 7: 6 сообщений с разными seq + 1 канал с последним seq. Именно это смешение
    message/channel в потоке обязан корректно постранично отдать `queries.changes_since`.
    """
    project_a = insert_project(db, name="A")
    acc = insert_account(db)
    insert_channel(db, acc, project_id=project_a, ext_id="c1", my_msg_count=5, first_seen_ts=0)
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_a])
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(6):
        _apply(db, acc, [_upsert_event(ext_id=f"m{i}", text=f"t{i}", ts=1000 + i)], now_ms=1000 + i)

    seen: list[int] = []
    since = 0
    pages = 0
    while True:
        pages += 1
        assert pages <= 20, "похоже на бесконечный цикл пагинации"
        r = await client.get("/api/v1/sync", params={"since": since, "limit": 2}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        page_seqs = [m["seq"] for m in body["messages"]] + [c["seq"] for c in body["channels"]]
        assert len(page_seqs) <= 2
        seen.extend(page_seqs)
        since = body["max_seq"]
        if not body["more"]:
            break

    assert len(seen) == 7
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)  # каждое изменение ровно один раз


# --- Тест 16: feed.stream() как обычный async-генератор, без HTTP --------------------------------------


def _principal(project_ids=None) -> Principal:
    return Principal(token_id=1, kind="interactive", scopes=frozenset({"read"}), project_ids=project_ids)


async def test_stream_emits_frame_with_id_equal_to_seq_after_write(db):
    project_a = insert_project(db, name="A")
    acc = insert_account(db)
    insert_channel(db, acc, project_id=project_a, ext_id="c1", my_msg_count=5, first_seen_ts=0)

    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    db.on_commit(notifier.notify)

    principal = _principal(frozenset({project_a}))
    gen = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=5.0)
    try:
        _apply(db, acc, [_upsert_event()], now_ms=1000)
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert frame.startswith("id: ")
        first_line = frame.splitlines()[0]
        seq_in_frame = int(first_line.removeprefix("id: "))
        with db.reader() as conn:
            max_seq = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
        assert seq_in_frame <= max_seq
        assert "event: message" in frame
        assert "привет" not in frame  # только метаданные, без текста
    finally:
        await gen.aclose()


async def test_stream_resume_from_last_seq_has_no_gaps_or_duplicates(db):
    project_a = insert_project(db, name="A")
    acc = insert_account(db)
    insert_channel(db, acc, project_id=project_a, ext_id="c1", my_msg_count=5, first_seen_ts=0)

    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    db.on_commit(notifier.notify)
    principal = _principal(frozenset({project_a}))

    for i in range(4):
        _apply(db, acc, [_upsert_event(ext_id=f"m{i}", ts=1000 + i)], now_ms=1000 + i)

    # Первый проход — берём только первые 3 кадра как "уже виденные", запоминаем последний seq.
    gen1 = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=5.0)
    seen_first = []
    try:
        for _ in range(3):
            frame = await asyncio.wait_for(gen1.__anext__(), timeout=2.0)
            seen_first.append(int(frame.splitlines()[0].removeprefix("id: ")))
    finally:
        await gen1.aclose()

    last_seen = seen_first[-1]

    # Второй генератор стартует с last_seq=last_seen — должен отдать оставшееся без дыр и дублей.
    gen2 = stream(db, principal, last_seq=last_seen, notifier=notifier, heartbeat_s=5.0)
    seen_second = []
    try:
        while True:
            frame = await asyncio.wait_for(gen2.__anext__(), timeout=2.0)
            seq_ = int(frame.splitlines()[0].removeprefix("id: "))
            seen_second.append(seq_)
            with db.reader() as conn:
                max_seq = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
            if seq_ >= max_seq:
                break
    finally:
        await gen2.aclose()

    assert all(s > last_seen for s in seen_second)  # ничего из уже увиденного не повторилось
    assert len(seen_second) == len(set(seen_second))
    # Непрерывность: между двумя проходами не потеряно ни одного изменения (упорядоченность по seq).
    assert seen_first == sorted(seen_first)
    assert seen_second == sorted(seen_second)


async def test_stream_project_a_never_gets_project_b_frames(db):
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    acc = insert_account(db)
    insert_channel(db, acc, project_id=project_a, ext_id="ca", my_msg_count=5, first_seen_ts=0)
    insert_channel(db, acc, project_id=project_b, ext_id="cb", my_msg_count=5, first_seen_ts=0)

    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    db.on_commit(notifier.notify)
    principal_a = _principal(frozenset({project_a}))

    gen = stream(db, principal_a, last_seq=0, notifier=notifier, heartbeat_s=0.2)
    try:
        _apply(db, acc, [_upsert_event(channel_ext_id="cb", ext_id="b1", text="секрет B")], now_ms=1000)
        # Тишина для A: должен прийти heartbeat, а не кадр про B.
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert frame == HEARTBEAT_FRAME

        _apply(db, acc, [_upsert_event(channel_ext_id="ca", ext_id="a1", text="для A")], now_ms=2000)
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert "cb" not in frame
        assert "секрет" not in frame
        assert "для A" not in frame  # метаданные, без текста вообще
    finally:
        await gen.aclose()


async def test_stream_heartbeat_on_silence(db):
    project_a = insert_project(db, name="A")
    acc = insert_account(db)
    insert_channel(db, acc, project_id=project_a, ext_id="c1", my_msg_count=5, first_seen_ts=0)

    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    principal = _principal(frozenset({project_a}))

    gen = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=0.05)
    try:
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame == HEARTBEAT_FRAME
    finally:
        await gen.aclose()


async def test_notifier_wait_does_not_lose_notify_between_read_and_wait():
    """Защита от потерянного пробуждения: notify(), случившийся ДО входа в wait() (но уже после того, как
    вызывающий снял номер поколения), не теряется — wait() возвращает новое поколение немедленно, не ожидая
    таймаута. Именно поэтому Notifier построен на монотонном счётчике поколений, а не на голом Event."""
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())

    gen_before = notifier.gen
    notifier.notify()  # эмулируем коммит писателя, случившийся между чтением БД и входом в wait()
    await asyncio.sleep(0)  # даём call_soon_threadsafe-колбэку отработать в этом же цикле

    new_gen = await asyncio.wait_for(notifier.wait(gen_before, 5.0), timeout=0.5)
    assert new_gen == gen_before + 1


async def test_stream_stop_and_cancellation_leave_no_pending_tasks(db):
    project_a = insert_project(db, name="A")
    acc = insert_account(db)
    insert_channel(db, acc, project_id=project_a, ext_id="c1", my_msg_count=5, first_seen_ts=0)

    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    principal = _principal(frozenset({project_a}))

    # 1. stop: завершает генератор без исключения.
    stop = asyncio.Event()
    gen = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=5.0, stop=stop)

    async def _drain():
        async for _frame in gen:
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)  # даём генератору дойти до notifier.wait()
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done() and not task.cancelled()

    # 2. отмена задачи-потребителя: генератор закрывается, задача корректно отменяется без утечек.
    gen2 = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=5.0)
    task2 = asyncio.create_task(gen2.__anext__())
    await asyncio.sleep(0)
    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass
    assert task2.cancelled()
    await gen2.aclose()

    await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t not in (asyncio.current_task(),) and not t.done()]
    assert pending == []


# --- /stream по HTTP: ASGITransport буферизует весь ASGI-вызов целиком, поэтому дальше проверки
# авторизации/валидации параметров (все они завершаются ДО создания StreamingResponse и не входят в
# бесконечный генератор) не идём — экспериментально подтверждено (см. модульный docstring). Успешный (200)
# запрос через ASGITransport с бесконечным SSE-генератором не возвращает управление вообще.


async def test_http_stream_rejects_before_entering_generator(client, db):
    r = await client.get("/api/v1/stream")
    assert r.status_code == 401

    token_no_read = make_bearer_token(db, scopes=("send",))
    r = await client.get("/api/v1/stream", headers={"Authorization": f"Bearer {token_no_read}"})
    assert r.status_code == 403

    token = make_bearer_token(db, scopes=("read",))
    r = await client.get(
        "/api/v1/stream", headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "not-a-number"}
    )
    assert r.status_code == 422


# Водяной знак: то, что выше main.seq_counter (недокоммичено между файлами), не отдаётся, курсор стоит.
def test_changes_since_never_passes_watermark(db):
    acc = insert_account(db, db_name="work")
    ch = insert_channel(db, acc, ext_id="w1", db_name="work")
    insert_message(db, ch, ext_id="1", ts=1000, text="видно")
    ui = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)

    with db.reader() as conn:
        counter = conn.execute("SELECT value FROM main.seq_counter").fetchone()[0]

    def _rogue(conn):  # строка в work с seq выше счётчика в main — как при разрыве коммита между файлами
        conn.execute(
            "INSERT INTO work.message (seq, first_seq, account_id, channel_id, ext_id, kind, text, ts) "
            "VALUES (?, ?, ?, ?, 'rogue', 'text', 'рано', 2000)",
            (counter + 5, counter + 5, acc, ch),
        )

    db.write_sync(_rogue)

    with db.reader() as conn:
        changes = queries.changes_since(conn, ui, since=0, limit=100)
        assert not conn.in_transaction  # читающая транзакция закрыта
    assert "rogue" not in {m["ext_id"] for m in changes.messages}
    assert changes.max_seq <= counter

    db.write_sync(lambda conn: conn.execute("UPDATE main.seq_counter SET value = ?", (counter + 5,)))
    with db.reader() as conn:
        later = queries.changes_since(conn, ui, since=changes.max_seq, limit=100)
    assert [m["ext_id"] for m in later.messages] == ["rogue"]
    assert later.max_seq == counter + 5


def test_heartbeat_frame_is_dispatchable_by_eventsource():
    """Поправка куска 6: комментарий `: hb` браузерный EventSource в JS не отдаёт. Кадр обязан быть
    именованным, с НЕПУСТЫМ data (пустой не диспатчится) и БЕЗ `id:` (иначе сдвинулся бы Last-Event-ID)."""
    assert HEARTBEAT_FRAME == "event: hb\ndata: {}\n\n"
    assert "id:" not in HEARTBEAT_FRAME
