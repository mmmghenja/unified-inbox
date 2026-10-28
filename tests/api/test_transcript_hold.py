"""Ф14-6b (docs/architecture.md, 14-6b, П1, «Мелкое», «Итоги приёмки 14-6a» п.1): потребители
ждут расшифровку голосового.

- `transcript_wait_ms` = 0, если служба не опрашивала `/transcripts/lease` последние 60 с (метка в памяти
  ядра).
- Сигналы подписок в режиме «каждое сообщение» (Ф13-1 `messages`, Ф14-5 беседы) держатся в читателе, без
  записей в БД, пока хоть одно сообщение сигнала ждёт расшифровку; готовность расшифровки будит ожидание.
- `hub_wait` (`/channels/{id}/wait`) отдаёт новое голосовое, когда расшифровка готова, или как есть по
  таймауту.
"""

import asyncio
import hashlib

import pytest

from hub.core import media, security, subs, transcripts
from hub.core.ingest import apply_batch

from .conftest import insert_account, insert_channel, insert_project

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
OGG = b"OggS" + b"\x00" * 60
VOICE = {"type": "voice", "mime": "audio/ogg", "size": 64, "duration_s": 12, "src_id": "v1"}
NEW_CLIENT = {"X-Hub-Sub-Wait": "2"}


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def hub_now(monkeypatch, clock):
    """Часы выдачи (`transcript_wait_ms` в `attach_media`) — те же, что часы приложения теста."""
    monkeypatch.setattr(transcripts, "now_ms", lambda: int(clock() * 1000))


def _h(token: str, **extra) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, **extra}


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="telegram")
    cid = insert_channel(db, acc, project_id=pid, ext_id="c1")
    tok = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    return {"pid": pid, "workdir": str(workdir), "acc": acc, "cid": cid, "tok": tok}


def _session(w, *, full=True):
    body = {"pid": PID, "proc_start": START, "session_id": SID}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


def _ingest(db, w, clock, ext_id, *, voice=True, text=None):
    now = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, now - 60_000))
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": ext_id,
        "ts": now - 1000,
        "is_me": False,
        "kind": "file" if voice else "text",
        "text": text,
        "attachments": [{**VOICE, "src_id": f"v{ext_id}"}] if voice else [],
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=now)
    )
    with db.reader() as conn:
        mid = conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]
        row = conn.execute("SELECT id FROM main.media WHERE message_id = ?", (mid,)).fetchone()
    return mid, (row[0] if row else None)


def _ready(db, w, media_id, fetched_ms):
    """Файл скачан (как `commit_upload`) и поставлен в очередь расшифровки."""
    sha = hashlib.sha256(OGG).hexdigest()
    path = media.file_path(db.data_dir, w["acc"], sha)
    media.ensure_dir(path.parent)
    path.write_bytes(OGG)

    def _fn(conn):
        conn.execute(
            "UPDATE main.media SET status = 'ready', sha256 = ?, stored_size = ?, detected = 'ogg', "
            "fetched_ts = ? WHERE id = ?",
            (sha, len(OGG), fetched_ms, media_id),
        )
        assert transcripts.on_media_ready(conn, media_id, now_ms=fetched_ms)

    db.write_sync(_fn)


async def _done(db, media_id, now_ms, text="договор подписан"):
    def _fn(conn):
        job = transcripts.lease(conn, now_ms=now_ms)
        assert job is not None and job["media_id"] == media_id
        transcripts.report(
            conn,
            media_id=media_id,
            lease_gen=job["lease_gen"],
            outcome="done",
            text=text,
            lang="ru",
            now_ms=now_ms,
        )

    await db.write(_fn)


def _commits(db) -> list[int]:
    counter: list[int] = []
    db.on_commit(lambda: counter.append(1))
    return counter


def _chan_signals(db):
    with db.reader() as conn:
        return conn.execute("SELECT status FROM main.session_chan_signal ORDER BY id").fetchall()


# --- служба жива (пункт архитектора 1) -------------------------------------------------------------------


async def test_wait_ms_zero_until_worker_polls_and_after_60s(client, db, world, clock, hub_now, monkeypatch):
    mid, media_id = _ingest(db, world, clock, "1")
    _ready(db, world, media_id, int(clock() * 1000))

    async def wait_ms():
        r = await client.get(f"/api/v1/messages/{mid}", headers=_h(world["tok"]))
        assert r.status_code == 200, r.text
        return r.json()["transcript_wait_ms"]

    assert await wait_ms() == 0  # после рестарта ядра служба «не брала» — никто не ждёт
    worker = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name=transcripts.TOKEN_NAME,
            kind="background",
            scopes=["read", "transcribe"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    monkeypatch.setattr("hub.core.routes.transcripts._POLL_S", 0.05)
    r = await client.post("/api/v1/transcripts/lease", json={"wait_s": 0}, headers=_h(worker))
    assert r.status_code == 200 and r.json()["media_id"] == media_id
    # ожидание не дольше, чем служба ещё считается живой: взятое задание — до срока лиза (120 с; окно — 180 с)
    assert await wait_ms() == 120_000
    base = transcripts._mono()
    monkeypatch.setattr(transcripts, "_mono", lambda: base + 61)
    assert 0 < await wait_ms() <= 120_000  # ревью Ф14-6: считает долго — жива, пока лиз не истёк
    clock.advance_ms(121_000)  # лиз истёк, опросов нет — служба мертва
    assert await wait_ms() == 0
    with db.reader() as conn:
        assert transcripts.pending_for_messages(conn, "main", [mid], now_ms=int(clock() * 1000)) == set()


async def test_wait_capped_by_worker_liveness(db, world, clock, monkeypatch):
    """Служба замолчала посреди ожидания: ждущие пересматривают через остаток её живости, а не через окно."""
    mid, media_id = _ingest(db, world, clock, "1")
    now = int(clock() * 1000)
    _ready(db, world, media_id, now)
    transcripts.mark_worker_seen()
    base = transcripts._mono()
    monkeypatch.setattr(transcripts, "_mono", lambda: base + 50)
    with db.reader() as conn:
        waits = transcripts.wait_ms_for_messages(conn, "main", [mid], now_ms=now)
    assert 0 < waits[mid] <= 10_000


async def test_working_lease_keeps_worker_alive(db, world, clock, monkeypatch):
    """Ревью Ф14-6 (`.runs/critic-f146/`): задание считается дольше 60 с, очередь служба не опрашивает — пока
    лиз `working` не истёк, служба жива, удержание не снимается."""
    mid, media_id = _ingest(db, world, clock, "1")
    now = int(clock() * 1000)
    _ready(db, world, media_id, now)
    transcripts.mark_worker_seen()
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=now))
    assert job["media_id"] == media_id
    base = transcripts._mono()
    monkeypatch.setattr(transcripts, "_mono", lambda: base + 61)
    with db.reader() as conn:
        waits = transcripts.wait_ms_for_messages(conn, "main", [mid], now_ms=now + 61_000)
    assert 0 < waits[mid] <= 59_000  # до срока лиза (120 с) и окна 180 с


async def test_lease_poll_end_does_not_extend_liveness(client, db, monkeypatch):
    """Метка — при взятии опроса и выдаче задания; конец долгого опроса (ушедшей службы) — нет."""
    worker = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name=transcripts.TOKEN_NAME,
            kind="background",
            scopes=["read", "transcribe"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    calls: list[int] = []

    def fake_mono() -> float:
        calls.append(1)
        return 0.0 if len(calls) == 1 else 100.0  # первый вызов — начало опроса, дальше «прошло 100 с»

    monkeypatch.setattr(transcripts, "_mono", fake_mono)
    r = await client.post("/api/v1/transcripts/lease", json={"wait_s": 0}, headers=_h(worker))
    assert r.status_code == 204
    assert not transcripts.worker_alive()


# --- подписка на проект, режим `messages` (П1) -------------------------------------------------------------


async def _project_sub(client, w):
    r = await client.post(
        "/api/v1/subs", json={"session": _session(w), "mode": "messages"}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


def _project_wait(client, w, sub_id, *, timeout_s=3, headers=NEW_CLIENT):
    return client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, full=False), "waiter": "0123456789abcdef", "timeout_s": timeout_s},
        headers=_h(w["tok"], **headers),
    )


async def test_project_signal_held_without_writes_then_woken_by_transcript(client, db, world, procs, clock):
    sub_id = await _project_sub(client, world)
    transcripts.mark_worker_seen()
    _mid, media_id = _ingest(db, world, clock, "1")
    _ingest(db, world, clock, "2", voice=False, text="и ещё текстом")  # следом текст: держит всё равно
    now = int(clock() * 1000)
    _ready(db, world, media_id, now)
    commits = _commits(db)

    task = asyncio.create_task(_project_wait(client, world, sub_id, timeout_s=5))
    await asyncio.sleep(0.8)
    assert not task.done()
    assert commits == []  # удержание — только чтение: ни одной записи в БД
    assert _chan_signals(db) == [("pending",)]
    await _done(db, media_id, now)
    r = await asyncio.wait_for(task, 3)
    assert r.status_code == 200, r.text
    (signal,) = r.json()["signals"]
    assert signal["kind"] == "messages" and signal["msg_count"] == 2
    assert _chan_signals(db) == [("delivered",)]


async def test_project_signal_released_by_term(client, db, world, procs, clock):
    sub_id = await _project_sub(client, world)
    transcripts.mark_worker_seen()
    _mid, media_id = _ingest(db, world, clock, "1")
    now = int(clock() * 1000)
    _ready(db, world, media_id, now - transcripts.TRANSCRIBE_WAIT_S * 1000 + 400)  # удержанию осталось 0,4 с
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(_project_wait(client, world, sub_id, timeout_s=5))
    await asyncio.sleep(0.1)
    assert not task.done()
    clock.advance_ms(500)  # срок ожидания расшифровки истёк — расшифровки нет, сигнал всё равно выдан
    r = await asyncio.wait_for(task, 4)
    assert r.status_code == 200 and len(r.json()["signals"]) == 1
    assert asyncio.get_running_loop().time() - started < 3


async def test_project_signal_not_held_when_worker_absent(client, db, world, procs, clock):
    sub_id = await _project_sub(client, world)
    _mid, media_id = _ingest(db, world, clock, "1")
    _ready(db, world, media_id, int(clock() * 1000))
    r = await _project_wait(client, world, sub_id, timeout_s=2)
    assert r.status_code == 200 and len(r.json()["signals"]) == 1


async def test_cards_mode_not_held(client, db, world, procs, clock):
    """Сигнал карточки (режим `cards`) не держится (карточку уже задержал разборщик) — `wait_state` считает
    его сразу, даже когда в беседе ждёт голосовое."""
    r = await client.post("/api/v1/subs", json={"session": _session(world)}, headers=_h(world["tok"]))
    sub_id = r.json()["sub"]["id"]
    transcripts.mark_worker_seen()
    _mid, media_id = _ingest(db, world, clock, "1")
    _ready(db, world, media_id, int(clock() * 1000))

    def _signal(conn):
        conn.execute(
            "INSERT INTO main.session_signal (sub_id, item_id, channel_id, project_id, account_id, label, "
            "covered_seq, msg_count, status, created_ts, updated_ts) "
            "VALUES (?, 1, ?, ?, ?, 'reply', 1, 1, 'pending', 0, 0)",
            (sub_id, world["cid"], world["pid"], world["acc"]),
        )

    db.write_sync(_signal)
    with db.reader() as conn:
        assert subs.wait_state(conn, sub_id, now_ms=int(clock() * 1000)) == (1, 0)


# --- подписка на беседу (Ф14-5, П1) ----------------------------------------------------------------------


async def test_chan_signal_held_then_woken(client, db, world, procs, clock):
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": world["cid"], "session": _session(world)},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    transcripts.mark_worker_seen()
    _mid, media_id = _ingest(db, world, clock, "1")
    now = int(clock() * 1000)
    _ready(db, world, media_id, now)
    commits = _commits(db)
    body = {"session": _session(world, full=False), "waiter": "0123456789abcdef", "timeout_s": 5}
    task = asyncio.create_task(
        client.post("/api/v1/chan-subs/wait", json=body, headers=_h(world["tok"], **NEW_CLIENT))
    )
    await asyncio.sleep(0.8)
    assert not task.done() and commits == []
    await _done(db, media_id, now)
    r = await asyncio.wait_for(task, 3)
    assert r.status_code == 200, r.text
    assert [s["channel_id"] for s in r.json()["signals"]] == [world["cid"]]


# --- hub_wait (`/channels/{id}/wait`) --------------------------------------------------------------------


async def test_channel_wait_holds_for_transcript(client, db, world, clock, hub_now):
    transcripts.mark_worker_seen()
    with db.reader() as conn:
        after = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    mid, media_id = _ingest(db, world, clock, "1")
    now = int(clock() * 1000)
    _ready(db, world, media_id, now)
    task = asyncio.create_task(
        client.get(
            f"/api/v1/channels/{world['cid']}/wait",
            params={"after": after, "timeout_s": 5},
            headers=_h(world["tok"]),
        )
    )
    await asyncio.sleep(0.5)
    assert not task.done()
    await _done(db, media_id, now)
    r = await asyncio.wait_for(task, 3)
    assert r.status_code == 200, r.text
    (msg,) = [m for m in r.json()["messages"] if m["id"] == mid]
    assert msg["media"][0]["transcript"]["status"] == "done" and msg["transcript_wait_ms"] == 0


async def test_channel_wait_timeout_returns_as_is(client, db, world, clock, hub_now):
    transcripts.mark_worker_seen()
    with db.reader() as conn:
        after = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    mid, media_id = _ingest(db, world, clock, "1")
    _ready(db, world, media_id, int(clock() * 1000))
    r = await client.get(
        f"/api/v1/channels/{world['cid']}/wait",
        params={"after": after, "timeout_s": 1},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200 and r.json()["woke"] is True
    (msg,) = [m for m in r.json()["messages"] if m["id"] == mid]
    assert msg["media"][0]["transcript"]["status"] == "queued"
