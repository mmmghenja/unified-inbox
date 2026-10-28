"""Ф31-1 (docs/architecture.md), ядро.

- B1 + П10: голосовое без текста — ключ по готовой расшифровке, повторная расшифровка второго сигнала не даёт.
- B2 + П4: правка — перепроверка ключей только у настоящей правки чужого видимого сообщения.
- B3 + П1, П2, П3, П6: ключи у подписки на беседу.
- B4 + П7: пометка `keys` в чтении — по тексту расшифровки и по ключам подписки на беседу.
- B5 + П12: пауза `hub_download` (`POST /media/{id}/fetch`) по `failed`.
- B7: миграция 0044 на копии БД с данными.
- П8: пометки «ключ сработал» старше 7 дней удаляются при вставке.

Стиль — как test_f25_sub_keywords.py: публичное приложение через ASGITransport, события через
`ingest.apply_batch`, `/proc` подменён словарём, часы — `FakeClock`."""

import hashlib
import json

import pytest

from hub import sub_wait
from hub.core import chan_subs, ingest, media, security, subs, transcripts
from hub.core import db as db_module
from hub.core.db import Db

from .conftest import csrf_headers, insert_account, insert_channel, insert_project, login_via_code

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
KEYS = {"keywords": ["штраф"], "exclude": ["вакансия"]}
OGG = b"OggS" + b"\x00" * 60
MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def hub_now(monkeypatch, clock):
    """Часы выдачи (`transcript_wait_ms` в `attach_media`) — те же, что часы приложения теста."""
    monkeypatch.setattr(transcripts, "now_ms", lambda: int(clock() * 1000))


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="telegram")
    ch = {ext: insert_channel(db, acc, project_id=pid, ext_id=ext, kind="group") for ext in ("G1", "G2")}
    ch["D1"] = insert_channel(db, acc, project_id=pid, ext_id="D1", kind="dm")
    return {
        "pid": pid,
        "workdir": str(workdir),
        "acc": acc,
        "ch": ch,
        "tok": _token(db),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


def _session(w, who, *, full=True):
    pid, start, sid = who
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


async def _sub(client, w, who, **extra):
    body: dict = {"session": _session(w, who), **extra}
    if extra.get("mode") == "personal":
        body["mode"], body["msg_filter"] = "messages", "personal"
    r = await client.post("/api/v1/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _chan_sub(client, w, who, channel_id, *, expect=200, **extra):
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": channel_id, "session": _session(w, who), **extra},
        headers=_h(w["tok"]),
    )
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, who):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


async def _chan_wait(client, w, who):
    r = await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


def _ev(
    chat,
    ext,
    ts,
    *,
    kind="group",
    text="x",
    is_me=False,
    mention=False,
    voice=False,
    edited_ts=None,
    type_="upsert",
):
    snd: dict = {"ext_id": "U1", "is_me": True} if is_me else {"ext_id": "U2", "name": "Пётр"}
    event = {
        "type": type_,
        "channel": {"ext_id": chat, "kind": kind},
        "ext_id": ext,
        "ts": ts,
        "sender": snd,
        "is_me": is_me,
        "kind": "file" if voice else "text",
        "text": None if voice else text,
        "mentions_me": mention,
    }
    if voice:
        event["attachments"] = [
            {"type": "voice", "mime": "audio/ogg", "size": 64, "duration_s": 12, "src_id": f"v{chat}{ext}"}
        ]
    if edited_ts is not None:
        event["edited_ts"] = edited_ts
    return event


def _ingest(db, w, events, now_ms):
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _now(clock) -> int:
    return int(clock() * 1000)


def _msg_id(db, channel_id, ext):
    schema = db_module.db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext),
        ).fetchone()[0]


def _media_of(db, message_id):
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.media WHERE message_id = ?", (message_id,)).fetchone()[0]


def _signals(db, channel_id=None) -> list[tuple]:
    """`(chan_sub_id, project_sub_id, channel_id, status, reason, msg_count, cat, keys)`."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT chan_sub_id, project_sub_id, channel_id, status, reason, msg_count, cat, keys_json "
            "FROM main.session_chan_signal ORDER BY id"
        ).fetchall()
    out = [(*r[:7], json.loads(r[7]) if r[7] else None) for r in rows]
    return [r for r in out if channel_id is None or r[2] == channel_id]


def _hits(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT sub_kind, sub_id, message_id FROM main.sub_keyword_hit "
            "ORDER BY sub_kind, sub_id, message_id"
        ).fetchall()


def _ready(db, w, media_id, fetched_ms):
    """Файл голосового скачан (как `commit_upload`) и поставлен в очередь расшифровки."""
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


def _done(db, media_id, now_ms, text):
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

    db.write_sync(_fn)


def _voice(db, w, chat, ext, now):
    """Голосовое пришло (без текста), файл скачан, расшифровка в очереди. -> (message_id, media_id)."""
    _ingest(db, w, [_ev(chat, ext, now, voice=True)], now)
    mid = _msg_id(db, w["ch"][chat], ext)
    media_id = _media_of(db, mid)
    _ready(db, w, media_id, now)
    return mid, media_id


def _audit(db, action):
    with db.reader() as conn:
        return [
            (result, json.loads(d))
            for result, d in conn.execute(
                "SELECT result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
            )
        ]


# --- B1 + П10: голосовое и расшифровка -------------------------------------------------------------------


async def test_b1_voice_keyword_after_transcript(client, db, world, procs, clock):
    sub = await _sub(client, world, A, mode="personal", batch_min=0, **KEYS)  # Ф36-1: без пачки
    now = _now(clock)
    g1 = world["ch"]["G1"]
    mid, media_id = _voice(db, world, "G1", "1", now)
    # при приёме текста нет — ключ не найден
    assert _signals(db, g1) == [(None, sub, g1, "skipped", "no_keyword", 1, None, None)]

    _done(db, media_id, now + 1000, text="Нам выписали ШТРАФ за склад")
    assert _signals(db, g1) == [
        (None, sub, g1, "skipped", "no_keyword", 1, None, None),
        (None, sub, g1, "pending", None, 1, "keyword", ["штраф"]),
    ]
    assert _hits(db) == [("project", sub, mid)]
    got = await _wait(client, world, sub, A)
    assert [(s["channel_id"], s["cat"], s["keys"]) for s in got] == [(g1, "keyword", ["штраф"])]

    # П10: «Расшифровать заново» — второй `report done` тем же текстом второго сигнала не даёт
    db.write_sync(lambda conn: transcripts.request(conn, media_id, now_ms=now + 2000))
    _done(db, media_id, now + 3000, text="Нам выписали ШТРАФ за склад")
    rows = _signals(db, g1)
    assert [r[3] for r in rows] == ["skipped", "delivered"]


async def test_b1_voice_without_keyword_and_old_voice_silent(client, db, world, procs, clock):
    sub = await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    _mid, media_id = _voice(db, world, "G2", "1", now)
    _done(db, media_id, now + 1000, text="просто привет")
    assert _signals(db, g2) == [(None, sub, g2, "skipped", "no_keyword", 1, None, None)]
    # минус-слово в расшифровке — тоже тишина
    _mid, media_id = _voice(db, world, "G2", "2", now + 1)
    _done(db, media_id, now + 2000, text="вакансия: штраф не платим")
    assert [r[3] for r in _signals(db, g2)] == ["skipped"]
    # голосовое старше суток к моменту готовой расшифровки — тишина
    _mid, media_id = _voice(db, world, "G1", "1", now)
    _done(db, media_id, now + 25 * HOUR_MS, text="штраф")
    assert [r[3] for r in _signals(db, g1)] == ["skipped"]
    assert _hits(db) == []


async def test_b1_chan_sub_with_keys_and_project_all_both_by_own_rules(client, db, world, procs, clock):
    """Расшифровка с ключом: подписка на беседу с ключами — сигнал `keyword`; подписка на проект «каждое
    сообщение» другой сессии — свой сигнал уже был при приёме, второго нет."""
    g1 = world["ch"]["G1"]
    chan = (await _chan_sub(client, world, A, g1, **KEYS))["sub"]["id"]
    other = await _sub(client, world, B, mode="messages", msg_filter="all")
    now = _now(clock)
    mid, media_id = _voice(db, world, "G1", "1", now)
    assert _signals(db, g1) == [
        (chan, None, g1, "skipped", "no_keyword", 1, None, None),
        (None, other, g1, "pending", None, 1, "other", None),
    ]
    _done(db, media_id, now + 1000, text="штраф пришёл")
    assert _signals(db, g1) == [
        (chan, None, g1, "skipped", "no_keyword", 1, None, None),
        (None, other, g1, "pending", None, 1, "other", None),
        (chan, None, g1, "pending", None, 1, "keyword", ["штраф"]),
    ]
    assert _hits(db) == [("channel", chan, mid)]


# --- B2 + П4: правка ---------------------------------------------------------------------------------


async def test_b2_edit_adds_keyword_once(client, db, world, procs, clock):
    sub = await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    g1 = world["ch"]["G1"]
    _ingest(db, world, [_ev("G1", "1", now, text="ни о чём")], now)
    assert [r[3] for r in _signals(db, g1)] == ["skipped"]
    _ingest(db, world, [_ev("G1", "1", now, text="у нас штраф", edited_ts=now + 10, type_="edit")], now + 10)
    assert _signals(db, g1)[-1] == (None, sub, g1, "pending", None, 1, "keyword", ["штраф"])
    snapshot = _signals(db)
    # вторая правка с тем же ключом — тишина (пометка «ключ сработал»)
    _ingest(db, world, [_ev("G1", "1", now, text="у нас штраф и бан", edited_ts=now + 20, type_="edit")], now)
    assert _signals(db) == snapshot
    # правка, убравшая ключ, сигнал не отзывает
    _ingest(db, world, [_ev("G1", "1", now, text="всё хорошо", edited_ts=now + 30, type_="edit")], now)
    assert _signals(db) == snapshot


async def test_b2_edit_silent_cases(client, db, world, procs, clock):
    await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    g1, g2, d1 = world["ch"]["G1"], world["ch"]["G2"], world["ch"]["D1"]
    _ingest(
        db,
        world,
        [
            _ev("G1", "1", now, is_me=True, text="мой пост"),
            _ev("G1", "2", now + 1, text="скрою"),
            _ev("G1", "3", now + 2, text="штраф сразу"),  # сигналило по ключу при приёме
            _ev("D1", "1", now + 3, kind="dm", text="привет"),  # личное
            _ev("G2", "1", now + 4, text="привет", edited_ts=now + 5),  # уже правленое при приёме
        ],
        now + 10,
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (_msg_id(db, g1, "2"),))
    )
    snapshot = _signals(db)
    assert [(r[2], r[3], r[6]) for r in snapshot] == [
        (g1, "skipped", None),  # «скрою» — без ключа
        (g1, "pending", "keyword"),
        (d1, "pending", "dm"),
        (g2, "skipped", None),
    ]
    later = now + 100
    _ingest(
        db,
        world,
        [
            _ev("G1", "1", now, is_me=True, text="мой штраф", edited_ts=later, type_="edit"),
            _ev("G1", "2", now + 1, text="скрою штраф", edited_ts=later, type_="edit"),
            _ev("G1", "3", now + 2, text="штраф и ещё штраф", edited_ts=later, type_="edit"),
            _ev("D1", "1", now + 3, kind="dm", text="привет, штраф", edited_ts=later, type_="edit"),
            # П4: тот же `edited_ts`, другой текст (перерасчёт текста сверкой/обработкой почты) — не правка
            _ev("G2", "1", now + 4, text="штраф", edited_ts=now + 5),
        ],
        later,
    )
    assert _signals(db) == snapshot


async def test_b2_edit_older_than_day_silent(client, db, world, procs, clock):
    await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="ни о чём")], now)
    snapshot = _signals(db)
    late = now + 25 * HOUR_MS
    _ingest(db, world, [_ev("G1", "1", now, text="штраф", edited_ts=late, type_="edit")], late)
    assert _signals(db) == snapshot


# --- B3 + П1, П2, П3, П6: ключи у подписки на беседу ------------------------------------------------------


async def test_b3_chan_sub_keywords_signals(client, db, world, procs, clock):
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    body = await _chan_sub(client, world, A, g1, keywords=["Штраф"], exclude=["вакансия"])
    chan = body["sub"]["id"]
    assert body["sub"]["keywords"] == {"keys": ["штраф"], "exclude": ["вакансия"], "batch_min": 15}
    plain = (await _chan_sub(client, world, A, g2))["sub"]["id"]
    own_project = await _sub(client, world, A, mode="messages", msg_filter="all")  # та же сессия (П1 Ф26-1)
    other = await _sub(client, world, B, mode="messages", msg_filter="all")
    now = _now(clock)
    _ingest(
        db,
        world,
        [
            _ev("G1", "1", now, text="опять штрафы"),
            _ev("G1", "2", now + 1, text="ни о чём"),
            _ev("G1", "3", now + 2, text="вакансия: штраф"),
            _ev("G1", "4", now + 3, mention=True, text="посмотри"),  # П1: личное без ключа — сигнал
            _ev("G2", "1", now + 4, text="без ключей — каждое"),
        ],
        now + 10,
    )
    assert sorted(_signals(db, g1), key=lambda r: (r[0] is None, r[3])) == [
        (chan, None, g1, "pending", None, 2, "mention", ["штраф"]),
        (chan, None, g1, "skipped", "no_keyword", 2, None, None),
        (None, other, g1, "pending", None, 4, "mention", None),
    ]
    assert _signals(db, g2) == [
        (plain, None, g2, "pending", None, 1, "other", None),
        (None, other, g2, "pending", None, 1, "other", None),
    ]
    assert all(r[1] != own_project for r in _signals(db))
    # П6: выдача несёт ключи, текст ожидания их печатает
    got = await _chan_wait(client, world, A)
    by_channel = {s["channel_id"]: s for s in got}
    assert by_channel[g1]["keys"] == ["штраф"] and "keys" not in by_channel[g2]
    clean = sub_wait.valid_chan_signals({"signals": got})
    text = sub_wait.chan_signals_text(clean, "hub sub wait --channels")
    line = next(x for x in text.splitlines() if f"беседа #{g1} " in x)
    assert "упоминание" in line and "ключи: штраф" in line
    assert [(r, d["keys"]) for r, d in _audit(db, "chan_sub_signal") if d["sub_id"] == chan] == [
        ("delivered", 1)
    ]


async def test_b3_chan_sub_without_keys_regression(client, db, world, procs, clock):
    g1 = world["ch"]["G1"]
    chan = (await _chan_sub(client, world, A, g1))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="раз"), _ev("G1", "2", now + 1, text="два")], now)
    assert _signals(db, g1) == [(chan, None, g1, "pending", None, 2, "other", None)]
    assert _hits(db) == []


async def test_b3_p3_keys_change_in_place(client, db, world, procs, clock):
    g1 = world["ch"]["G1"]
    first = (await _chan_sub(client, world, A, g1))["sub"]
    assert first["keywords"] is None
    body = await _chan_sub(client, world, A, g1, **KEYS)
    assert body["sub"]["id"] == first["id"] and body["created"] is False
    assert body["sub"]["keywords"] == {"keys": ["штраф"], "exclude": ["вакансия"], "batch_min": 15}
    # без ключей в запросе — как есть
    body = await _chan_sub(client, world, A, g1)
    assert body["sub"]["keywords"] == {"keys": ["штраф"], "exclude": ["вакансия"], "batch_min": 15}
    # [] — сняты
    body = await _chan_sub(client, world, A, g1, keywords=[])
    assert body["sub"]["id"] == first["id"] and body["sub"]["keywords"] is None
    assert [(r, d["keys"], d["exclude"]) for r, d in _audit(db, "chan_sub_keys")] == [
        ("ok", 1, 1),
        ("ok", 0, 0),
    ]
    # пределы и проверки — как у подписки на проект
    await _chan_sub(client, world, A, g1, expect=422, exclude=["курс"])
    await _chan_sub(client, world, A, g1, expect=422, keywords=["я"])
    # список подписок показывает ключи
    await _chan_sub(client, world, A, g1, keywords=["бан"])
    r = await client.get("/api/v1/subs", headers=_h(world["tok"]))
    [row] = r.json()["channels"]
    assert row["keywords"] == {"keys": ["бан"], "exclude": [], "batch_min": 15}


async def test_b3_p2_agent_items_pick_for_chan_sub(client, db, world, procs, clock):
    """П2: у подписки на беседу строка отфильтрованного (`skipped no_keyword`) переписывается на месте со
    старым id — выбор сигнала для дежурной как у подписки на проект (`_pick`)."""
    g1 = world["ch"]["G1"]
    await _chan_sub(client, world, A, g1, batch_min=0, **KEYS)  # Ф36-1: без пачки — выдача сразу
    now = _now(clock)

    async def card_signal(ext):
        r = await client.post(
            "/api/v1/agent/items",
            json={
                "channel_id": g1,
                "message_ids": [_msg_id(db, g1, ext)],
                "label": "reply",
                "summary": "сводка",
                "action": None,
                "provider": "test",
                "model": "m",
            },
            headers=_h(world["triage"]),
        )
        assert r.status_code == 200, r.text
        item = r.json()["id"]
        r = await client.get(
            "/api/v1/agent/items", params={"project": world["pid"]}, headers=_h(world["triage"])
        )
        assert r.status_code == 200, r.text
        [card] = [c for c in r.json() if c["id"] == item]
        return card["msg_signal"], card["msg_signals"]

    _ingest(db, world, [_ev("G1", "A", now, text="ни о чём")], now)
    _ingest(db, world, [_ev("G1", "B", now + 1, text="штраф")], now + 1)
    signal, signals = await card_signal("B")
    assert (signal["status"], signal["reason"]) == ("pending", None)  # невыданный не перекрывается
    assert [s["status"] for s in signals] == ["pending"]
    assert len(await _chan_wait(client, world, A)) == 1
    _ingest(db, world, [_ev("G1", "C", now + 2, text="снова ни о чём")], now + 2)
    signal, signals = await card_signal("C")
    assert (signal["status"], signal["reason"]) == ("skipped", "no_keyword")
    assert signal["last_message_id"] == _msg_id(db, g1, "C")
    assert [(s["sub_ref"], s["status"]) for s in signals] == [(signal["sub_ref"], "skipped")]


# --- B4 + П7: пометка `keys` в чтении ---------------------------------------------------------------------


async def test_b4_read_keys_by_transcript_and_chan_sub(client, db, world, procs, clock, hub_now):
    await _sub(client, world, A, mode="personal", **KEYS)
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    await _chan_sub(client, world, B, g2, keywords=["бан"])
    now = _now(clock)
    _mid, media_id = _voice(db, world, "G1", "1", now)
    headers = _h(world["tok"])

    async def keys(channel_id, ext):
        r = await client.get(f"/api/v1/channels/{channel_id}/messages", headers=headers)
        assert r.status_code == 200, r.text
        return {m["ext_id"]: m.get("keys") for m in r.json()["messages"]}[ext]

    assert await keys(g1, "1") is None  # расшифровки ещё нет
    _done(db, media_id, now + 1000, text="тут штраф")
    assert await keys(g1, "1") == ["штраф"]
    # П7: беседа G2 без подписок на проект с её ключами — ключи подписки на беседу
    _ingest(db, world, [_ev("G2", "1", now, text="бан карточки")], now)
    assert await keys(g2, "1") == ["бан"]


# --- B5 + П12: пауза `hub_download` по `failed` ---------------------------------------------------------


async def test_b5_session_refetch_failed_paused(client, db, world, clock):
    now = _now(clock)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    event = {
        "type": "upsert",
        "channel": {"ext_id": "G1", "kind": "group"},
        "ext_id": "1",
        "ts": now,
        "is_me": False,
        "kind": "file",
        "attachments": [{"type": "document", "name": "a.pdf", "size": 10}],
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=world["acc"], events=[event], cursor=None, now_ms=now
        )
    )
    mid = _msg_id(db, world["ch"]["G1"], "1")
    media_id = _media_of(db, mid)

    def set_failed():
        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.media SET status = 'failed', attempts = 5, not_before_ts = NULL WHERE id = ?",
                (media_id,),
            )
        )

    def row():
        with db.reader() as conn:
            return conn.execute(
                "SELECT status, attempts FROM main.media WHERE id = ?", (media_id,)
            ).fetchone()

    set_failed()
    headers = _h(world["tok"])
    r = await client.post(f"/api/v1/media/{media_id}/fetch", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    first = now
    set_failed()  # служба снова не смогла
    clock.advance_ms(5 * MIN_MS)
    r = await client.post(f"/api/v1/media/{media_id}/fetch", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed" and body["retry_after_ts"] == first + media.SESSION_REFETCH_GAP_MS
    assert row() == ("failed", 5)
    clock.advance_ms(6 * MIN_MS)
    r = await client.post(f"/api/v1/media/{media_id}/fetch", headers=headers)
    assert r.json()["status"] == "pending" and "retry_after_ts" not in r.json()
    assert [result for result, _d in _audit(db, "media_fetch")] == ["ok", "paused", "ok"]
    # клик владельца в UI — без паузы
    set_failed()
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert row() == ("pending", 0)


# --- B7: миграция 0044 -----------------------------------------------------------------------------------


def test_b7_migration_0044(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 43}
    )
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts) VALUES (7, 1, 1, 1, 1, 's', '/', 0)"
            )
        )

        # Ревью Ф31-1: сигналы подписки с ключами до миграции -> пометки (первое и последнее сообщение).
        def _old_signals(conn):
            conn.execute(
                "INSERT INTO main.session_sub (id, project_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts, mode, msg_filter, keywords_json) "
                "VALUES (5, 1, 1, 1, 1, 's', '/', 0, 'messages', 'personal', '{\"keys\": [\"штраф\"]}')"
            )
            conn.execute(
                "INSERT INTO main.session_sub (id, project_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts, mode) "
                "VALUES (6, 1, 1, 2, 2, 't', '/', 0, 'messages')"
            )
            for sub_id, first, last, status in ((5, 100, 102, "delivered"), (5, 200, 200, "skipped"),
                                                (6, 300, 300, "delivered")):  # fmt: skip
                conn.execute(
                    "INSERT INTO main.session_chan_signal (project_sub_id, channel_id, first_message_id, "
                    "last_message_id, msg_count, status, created_ts, updated_ts) "
                    "VALUES (?, 7, ?, ?, 1, ?, 1, 1)",
                    (sub_id, first, last, status),
                )

        old.write_sync(_old_signals)
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 43
            work_version = conn.execute("PRAGMA work.user_version").fetchone()[0]
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 44
                # 0044 — только main: work догоняет лишь общие миграции (последняя shared, Ф30-2 — 0045).
                assert work_version == 43
                assert conn.execute("PRAGMA work.user_version").fetchone()[0] == max(original(main=False))
                cols = {r[1] for r in conn.execute("PRAGMA main.table_info(session_chan_sub)")}
                assert "keywords_json" in cols
                assert conn.execute(
                    "SELECT channel_id, keywords_json FROM main.session_chan_sub"
                ).fetchall() == [(7, None)]
                hit_cols = [r[1] for r in conn.execute("PRAGMA main.table_info(sub_keyword_hit)")]
                assert hit_cols == ["sub_kind", "sub_id", "message_id", "ts"]
                indexes = {
                    r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type='index'")
                }
                assert {"sub_keyword_hit_ts", "session_chan_signal_skipped_chan"} <= indexes
                hits = conn.execute(
                    "SELECT sub_kind, sub_id, message_id FROM main.sub_keyword_hit ORDER BY message_id"
                ).fetchall()
                assert hits == [("project", 5, 100), ("project", 5, 102)]
                work_tables = {r[0] for r in conn.execute("SELECT name FROM work.sqlite_master")}
                assert "sub_keyword_hit" not in work_tables
        finally:
            new.close()
    with pytest.raises(Exception, match="CHECK"):
        new = Db(tmp_path)
        new.start()
        try:
            new.write_sync(
                lambda conn: conn.execute(
                    "INSERT INTO main.sub_keyword_hit (sub_kind, sub_id, message_id, ts) "
                    "VALUES ('x', 1, 1, 0)"
                )
            )
        finally:
            new.close()


# --- П8: чистка пометок ---------------------------------------------------------------------------------


async def test_p8_keyword_hits_pruned_on_insert(client, db, world, procs, clock):
    sub = await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="штраф")], now)
    first = _msg_id(db, world["ch"]["G1"], "1")
    assert _hits(db) == [("project", sub, first)]
    later = now + chan_subs.KEYWORD_HIT_KEEP_MS + 1
    clock.advance_ms(chan_subs.KEYWORD_HIT_KEEP_MS + 1)
    _ingest(db, world, [_ev("G2", "1", later, text="штраф снова")], later)
    assert _hits(db) == [("project", sub, _msg_id(db, world["ch"]["G2"], "1"))]


# --- П5: ожидание, разбуженное остановкой, — пусто и без выдачи --------------------------------------------


async def test_p5_closed_notifier_waits_empty_signals_stay_pending(app, client, db, world, procs, clock):
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    sub = await _sub(client, world, A, mode="messages", msg_filter="all")
    await _chan_sub(client, world, B, g2)
    now = _now(clock)
    _ingest(db, world, [_ev("G1", "1", now, text="раз"), _ev("G2", "1", now, text="два")], now)
    assert [r[3] for r in _signals(db)] == ["pending"] * 3
    app.state.notifier.close()
    assert await _wait(client, world, sub, A) == []
    assert await _chan_wait(client, world, B) == []
    assert [r[3] for r in _signals(db)] == ["pending"] * 3  # сигнал не потерян — выдаст следующий запуск
    r = await client.get(f"/api/v1/channels/{g1}/wait", params={"timeout_s": 60}, headers=_h(world["tok"]))
    assert r.status_code == 200 and r.json()["messages"] == []


async def test_review_edit_removing_mention_no_second_signal(client, db, world, procs, clock):
    """Ревью Ф31-1: упоминание с ключом сигналит при приёме как личное; правка, снявшая упоминание (ключ
    остался), второго сигнала о том же сообщении не даёт — пометка на любой сигнал подписке с ключами."""
    sub = await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    g1 = world["ch"]["G1"]
    _ingest(db, world, [_ev("G1", "1", now, text="@владелец штраф", mention=True)], now)
    got = await _wait(client, world, sub, A)
    assert [s["cat"] for s in got] == ["mention"]
    _ingest(db, world, [_ev("G1", "1", now, text="штраф", edited_ts=now + 10, type_="edit")], now + 10)
    assert [r[3] for r in _signals(db, g1)] == ["delivered"]


# --- Ф32-2 п.7 + П7: голосовое в ждущей строке не считается дважды -----------------------------------------


async def test_f322_voice_in_pending_row_not_counted_twice(client, db, world, procs, clock):
    """Подписка на беседу без ключей уже посчитала голосовое в `pending`; ключи добавлены до готовой
    расшифровки — перепроверка ставит строке категорию `keyword` и ключи, число сообщений прежнее. Сообщение
    вне ждущей строки перепроверка по-прежнему дописывает."""
    g1 = world["ch"]["G1"]
    chan = (await _chan_sub(client, world, A, g1))["sub"]["id"]
    now = _now(clock)
    mid, media_id = _voice(db, world, "G1", "1", now)
    assert _signals(db, g1) == [(chan, None, g1, "pending", None, 1, "other", None)]
    await _chan_sub(client, world, A, g1, **KEYS)
    _done(db, media_id, now + 1000, text="штраф пришёл")
    assert _signals(db, g1) == [(chan, None, g1, "pending", None, 1, "keyword", ["штраф"])]
    assert _hits(db) == [("channel", chan, mid)]

    # второе голосовое пришло уже с ключами: при приёме — `no_keyword`, в ждущую строку не входит;
    # расшифровка с ключом дописывает его как раньше
    mid2, media2 = _voice(db, world, "G1", "2", now + 2000)
    _done(db, media2, now + 3000, text="ещё один штраф")
    rows = _signals(db, g1)
    assert (chan, None, g1, "pending", None, 2, "keyword", ["штраф"]) in rows
    assert _hits(db) == [("channel", chan, mid), ("channel", chan, mid2)]
