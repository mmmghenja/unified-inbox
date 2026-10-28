"""Ф14-6 (docs/architecture.md, 14-6a, раздел F): ядро расшифровки без HTTP — постановка при
готовности медиа (решения 5, П9), ручная (решение 6), лиз и отчёт (П10), бамп `seq`, уборка (П4, каскад,
ретенция), ожидание для потребителей (П6), поиск (П13), сводка hub-watch."""

import hashlib
import json

import pytest

from hub.core import hiding, media, queries, retention, transcripts
from hub.core.db import WORK_ID_BASE
from hub.core.ingest import apply_batch
from hub.core.security import Principal

START = 1_000_000
NOW = START + 60_000
DAY = 24 * 60 * 60 * 1000
UI = Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)


def _account(db, *, source="telegram", db_name="main"):
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _setup(db, *, source="telegram", db_name="main"):
    account_id = _account(db, source=source, db_name=db_name)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, START))
    return account_id


def _ingest(db, account_id, ext_id, attachments, *, ts=START + 1000, text=None, channel_kind="dm", **extra):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": channel_kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "file",
        "text": text,
        "attachments": attachments,
        "mentions_me": False,
        **extra,
    }
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=ts)
    )
    assert result.dead == 0
    schema = "work" if db_is_work(db, account_id) else "main"
    with db.reader() as conn:
        return conn.execute(f"SELECT id FROM {schema}.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def db_is_work(db, account_id):
    with db.reader() as conn:
        return conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()[0] == "work"


def _media_row(db, message_id, idx=0):
    schema = "work" if message_id >= WORK_ID_BASE else "main"
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, status, priority FROM {schema}.media WHERE message_id = ? AND idx = ?",
            (message_id, idx),
        ).fetchone()


def _download(db, account_id, message_id, *, now_ms=NOW, body=b"OggS" + b"\x00" * 60, idx=0):
    """Настоящий путь загрузчика: лиз медиа -> временный файл -> `commit_upload` (в нём — постановка)."""
    media_id = _media_row(db, message_id, idx)[0]

    def _lease(conn):
        jobs = media.lease(conn, account_id=account_id, now_ms=now_ms, limit=10)
        return {j["media_id"]: j["lease_gen"] for j in jobs}

    # Лиз выдаёт пачкой: выданные раньше, но не залитые строки помним на объекте БД теста.
    gens = db.__dict__.setdefault("_test_media_leases", {})
    gens.update(db.write_sync(_lease))
    assert media_id in gens, "медиа не выдалось загрузчику"
    target_dir = media.account_dir(db.data_dir, account_id)
    media.ensure_dir(target_dir)
    tmp = target_dir / f"{media.TMP_PREFIX}-{media_id}"
    tmp.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    db.write_sync(
        lambda conn: media.commit_upload(
            conn,
            media_id=media_id,
            account_id=account_id,
            lease_gen=gens[media_id],
            tmp_path=tmp,
            sha256=sha,
            size=len(body),
            detected=media.detect(body[:64]),
            now_ms=now_ms,
        )
    )
    return media_id


def _transcript(db, media_id):
    schema = "work" if media_id >= WORK_ID_BASE else "main"
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.transcript WHERE media_id = ?", (media_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row, strict=True))


def _seq(db, message_id):
    schema = "work" if message_id >= WORK_ID_BASE else "main"
    with db.reader() as conn:
        return conn.execute(f"SELECT seq FROM {schema}.message WHERE id = ?", (message_id,)).fetchone()[0]


VOICE = {"type": "voice", "mime": "audio/ogg", "size": 1000, "duration_s": 42, "src_id": "v1"}


# --- постановка (решение 5, П9) ------------------------------------------------------------------------


def test_voice_is_queued_when_ready(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE])
    media_id = _download(db, account_id, mid)
    row = _transcript(db, media_id)
    assert row is not None and row["status"] == "queued" and row["manual"] == 0
    assert row["queued_ts"] == NOW


def test_round_video_and_slack_clip_are_queued(db):
    account_id = _setup(db)
    round_video = {"type": "video", "mime": "video/mp4", "size": 5000, "duration_s": 10, "round": True}
    mid = _ingest(db, account_id, "1", [round_video])
    assert _transcript(db, _download(db, account_id, mid))["status"] == "queued"

    slack = _setup(db, source="slack")
    clip = {"type": "audio", "mime": "audio/webm", "size": 5000, "clip": True, "src_id": "F1"}
    mid2 = _ingest(db, slack, "1.1", [clip])
    assert _transcript(db, _download(db, slack, mid2))["status"] == "queued"


@pytest.mark.parametrize(
    "item",
    [
        {"type": "audio", "mime": "audio/mpeg", "size": 5000, "duration_s": 60},  # аудиофайл
        {"type": "video", "mime": "video/mp4", "size": 5000, "duration_s": 10},  # обычное видео
        {"type": "voice", "mime": "audio/ogg", "size": 1000, "duration_s": 601},  # длиннее 10 мин
        {"type": "photo", "mime": "image/jpeg", "size": 1000},
    ],
)
def test_not_queued_automatically(db, item):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [item])
    if _media_row(db, mid)[1] == "deferred":
        db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=NOW))
    media_id = _download(db, account_id, mid)
    assert _transcript(db, media_id) is None


def test_unknown_duration_is_queued(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "voice", "mime": "audio/ogg", "size": 1000}])
    assert _transcript(db, _download(db, account_id, mid))["status"] == "queued"


def test_older_than_week_not_queued(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE], ts=START + 1000)
    media_id = _download(db, account_id, mid, now_ms=START + 1000 + 7 * DAY + 1)
    assert _transcript(db, media_id) is None


def test_click_download_is_not_automatic(db):
    """П9: заглушённое/архив/старт-точка — `deferred`; «Загрузить» (`priority=1`) — расшифровка по кнопке."""
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE], ts=START - 10)  # до старт-точки -> deferred
    assert _media_row(db, mid)[1] == "deferred"
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=NOW))
    media_id = _download(db, account_id, mid)
    assert _transcript(db, media_id) is None


def test_own_voice_is_queued(db):
    """Решение 5: свои голосовые тоже (с телефона — эхо `is_me`)."""
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE], is_me=True)
    assert _transcript(db, _download(db, account_id, mid))["status"] == "queued"


def test_work_schema(db):
    account_id = _setup(db, source="slack", db_name="work")
    clip = {"type": "audio", "mime": "audio/webm", "size": 5000, "clip": True, "src_id": "F1"}
    mid = _ingest(db, account_id, "1.1", [clip])
    assert mid >= WORK_ID_BASE
    media_id = _download(db, account_id, mid)
    assert _transcript(db, media_id)["status"] == "queued"
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    assert job["media_id"] == media_id and job["message_id"] == mid


# --- ручная (решение 6) ----------------------------------------------------------------------------------


def test_manual_any_age_and_audio_file(db):
    account_id = _setup(db)
    audio = {"type": "audio", "mime": "audio/mpeg", "size": 5000, "duration_s": 60}
    mid = _ingest(db, account_id, "1", [audio], ts=START + 1000)
    media_id = _download(db, account_id, mid)
    seq_before = _seq(db, mid)
    result = db.write_sync(lambda conn: transcripts.request(conn, media_id, now_ms=START + 30 * DAY))
    assert result["status"] == "queued" and result["manual"] is True
    assert _transcript(db, media_id)["manual"] == 1
    assert _seq(db, mid) > seq_before


def test_manual_rejects(db):
    account_id = _setup(db)
    video = {"type": "video", "mime": "video/mp4", "size": 5000, "duration_s": 10}
    mid = _ingest(db, account_id, "1", [video, VOICE])
    video_id = _download(db, account_id, mid, idx=0)
    with pytest.raises(transcripts.NotTranscribable):
        db.write_sync(lambda conn: transcripts.request(conn, video_id, now_ms=NOW))
    # не `ready` -> NotReady
    voice_id = _media_row(db, mid, 1)[0]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.media SET status = 'deferred' WHERE id = ?", (voice_id,))
    )
    with pytest.raises(transcripts.NotReady):
        db.write_sync(lambda conn: transcripts.request(conn, voice_id, now_ms=NOW))
    with pytest.raises(transcripts.NotFound):
        db.write_sync(lambda conn: transcripts.request(conn, 999_999, now_ms=NOW))


def test_manual_working_is_busy_done_and_failed_requeue(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE])
    media_id = _download(db, account_id, mid)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    with pytest.raises(transcripts.Busy):
        db.write_sync(lambda conn: transcripts.request(conn, media_id, now_ms=NOW))
    db.write_sync(
        lambda conn: transcripts.report(
            conn,
            media_id=media_id,
            lease_gen=job["lease_gen"],
            outcome="done",
            now_ms=NOW,
            text="раз два",
            lang="ru",
        )
    )
    db.write_sync(lambda conn: transcripts.request(conn, media_id, now_ms=NOW + 1))
    row = _transcript(db, media_id)
    assert (row["status"], row["manual"], row["attempts"], row["text"]) == ("queued", 1, 0, "раз два")


# --- лиз и отчёт (П10) -------------------------------------------------------------------------------------


def _queued(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE])
    return account_id, mid, _download(db, account_id, mid)


def test_lease_report_done_and_seq(db):
    _a, mid, media_id = _queued(db)
    seq0 = _seq(db, mid)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    assert job["media_id"] == media_id and job["manual"] is False and job["max_s"] == transcripts.AUTO_MAX_S
    assert job["duration_s"] == 42 and job["kind"] == "voice" and job["detected"] == "ogg"
    row = _transcript(db, media_id)
    assert row["status"] == "working" and row["lease_expires_ts"] == NOW + 3 * 42 * 1000
    seq1 = _seq(db, mid)
    assert seq1 > seq0
    status = db.write_sync(
        lambda conn: transcripts.report(
            conn,
            media_id=media_id,
            lease_gen=job["lease_gen"],
            outcome="done",
            now_ms=NOW + 5,
            text="привет",
            lang="ru",
            lang_prob=0.97,
            model="large-v3-turbo",
            device="cuda",
            elapsed_ms=1500,
        )
    )
    assert status == "done"
    row = _transcript(db, media_id)
    assert (row["text"], row["lang"], row["done_ts"], row["device"]) == ("привет", "ru", NOW + 5, "cuda")
    assert _seq(db, mid) > seq1
    assert db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW + 10)) is None


def test_lease_min_120s_for_short_and_unknown(db):
    assert transcripts.lease_ms(10) == 120_000
    assert transcripts.lease_ms(None) == 120_000
    assert transcripts.lease_ms(100) == 300_000


def test_stale_lease_gen_409(db):
    _a, _mid, media_id = _queued(db)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    with pytest.raises(transcripts.StaleLease):
        db.write_sync(
            lambda conn: transcripts.report(
                conn, media_id=media_id, lease_gen=job["lease_gen"] + 1, outcome="done", now_ms=NOW, text="x"
            )
        )


def test_expired_lease_requeues_and_counts_attempt(db):
    _a, _mid, media_id = _queued(db)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    later = NOW + transcripts.lease_ms(42) + 1
    with db.reader() as conn:
        assert transcripts.has_leasable(conn, now_ms=later)
    job2 = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=later))
    assert job2["media_id"] == media_id and job2["lease_gen"] == job["lease_gen"] + 1
    assert _transcript(db, media_id)["attempts"] == 1
    # отчёт по прежнему лизу — устарел
    with pytest.raises(transcripts.StaleLease):
        db.write_sync(
            lambda conn: transcripts.report(
                conn, media_id=media_id, lease_gen=job["lease_gen"], outcome="done", now_ms=later, text="x"
            )
        )


def test_three_expired_leases_fail(db):
    _a, _mid, media_id = _queued(db)
    now = NOW
    for _ in range(3):
        assert db.write_sync(lambda conn, now=now: transcripts.lease(conn, now_ms=now)) is not None
        now += transcripts.lease_ms(42) + 1
    assert db.write_sync(lambda conn: transcripts.lease(conn, now_ms=now)) is None
    row = _transcript(db, media_id)
    assert (row["status"], row["attempts"], row["error"]) == ("failed", 3, "lease expired")


def test_three_failures_then_failed_with_pause(db):
    _a, _mid, media_id = _queued(db)
    now = NOW
    for attempt in range(1, 4):
        job = db.write_sync(lambda conn, now=now: transcripts.lease(conn, now_ms=now))
        assert job is not None, attempt
        status = db.write_sync(
            lambda conn, job=job, now=now: transcripts.report(
                conn,
                media_id=media_id,
                lease_gen=job["lease_gen"],
                outcome="failed",
                now_ms=now,
                error="engine",
            )
        )
        row = _transcript(db, media_id)
        if attempt < 3:
            assert status == "queued" and row["not_before_ts"] > now
            # пауза: сразу не выдаётся
            assert db.write_sync(lambda conn, now=now: transcripts.lease(conn, now_ms=now + 1)) is None
            now = row["not_before_ts"]
        else:
            assert status == "failed" and row["attempts"] == 3 and row["error"] == "engine"


def test_skip_fails_at_once(db):
    _a, _mid, media_id = _queued(db)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    status = db.write_sync(
        lambda conn: transcripts.report(
            conn, media_id=media_id, lease_gen=job["lease_gen"], outcome="skip", now_ms=NOW, error="нет звука"
        )
    )
    assert status == "failed" and _transcript(db, media_id)["attempts"] == 0


def test_report_validates_lang_and_clips_text(db):
    _a, _mid, media_id = _queued(db)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    with pytest.raises(ValueError):
        db.write_sync(
            lambda conn: transcripts.report(
                conn, media_id=media_id, lease_gen=job["lease_gen"], outcome="done", now_ms=NOW, lang="de"
            )
        )
    db.write_sync(
        lambda conn: transcripts.report(
            conn,
            media_id=media_id,
            lease_gen=job["lease_gen"],
            outcome="done",
            now_ms=NOW,
            text="а" * (transcripts.MAX_TEXT + 50),
        )
    )
    text = _transcript(db, media_id)["text"]
    assert text.startswith("а" * transcripts.MAX_TEXT) and "обрезано" in text
    assert len(text) < transcripts.MAX_TEXT + 100


def test_manual_first_then_oldest(db):
    account_id = _setup(db)
    m1 = _ingest(db, account_id, "1", [VOICE])
    m2 = _ingest(db, account_id, "2", [{**VOICE, "src_id": "v2"}])
    a1 = _download(db, account_id, m1, now_ms=NOW)
    audio = {"type": "audio", "mime": "audio/mpeg", "size": 5000, "duration_s": 60}
    m3 = _ingest(db, account_id, "3", [audio])
    a3 = _download(db, account_id, m3, now_ms=NOW + 2)
    _download(db, account_id, m2, now_ms=NOW + 1)
    db.write_sync(lambda conn: transcripts.request(conn, a3, now_ms=NOW + 3))
    jobs = [db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW + 10)) for _ in range(3)]
    assert [j["media_id"] for j in jobs][:2] == [a3, a1]
    assert jobs[0]["manual"] is True and jobs[0]["max_s"] == transcripts.MANUAL_MAX_S


def test_lease_skips_deleted_message_and_missing_file(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE])
    media_id = _download(db, account_id, mid)
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET deleted = 1 WHERE id = ?", (mid,)))
    with db.reader() as conn:
        assert not transcripts.has_leasable(conn, now_ms=NOW)
    assert db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW)) is None
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET deleted = 0 WHERE id = ?", (mid,)))
    db.write_sync(lambda conn: media.mark_missing(conn, media_id=media_id))
    assert db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW)) is None


def test_extend_by_real_duration(db):
    _a, _mid, media_id = _queued(db)
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=NOW))
    expires = db.write_sync(
        lambda conn: transcripts.extend(
            conn, media_id=media_id, lease_gen=job["lease_gen"], duration_s=500, now_ms=NOW
        )
    )
    assert expires == NOW + 1_500_000 == _transcript(db, media_id)["lease_expires_ts"]
    with pytest.raises(transcripts.StaleLease):
        db.write_sync(
            lambda conn: transcripts.extend(conn, media_id=media_id, lease_gen=99, duration_s=500, now_ms=NOW)
        )


# --- выдача, ожидание (П6), поиск (П13) ---------------------------------------------------------------------


def _done(db, media_id, text, *, now=NOW, lang="ru"):
    job = db.write_sync(lambda conn: transcripts.lease(conn, now_ms=now))
    assert job["media_id"] == media_id
    db.write_sync(
        lambda conn: transcripts.report(
            conn,
            media_id=media_id,
            lease_gen=job["lease_gen"],
            outcome="done",
            now_ms=now,
            text=text,
            lang=lang,
        )
    )


def test_media_output_has_transcript(db):
    _a, mid, media_id = _queued(db)
    with db.reader() as conn:
        (entry,) = media.message_media(conn, mid)
    assert entry["transcript"] == {
        "status": "queued",
        "text": None,
        "lang": None,
        "problem": None,
        "manual": False,
    }
    _done(db, media_id, "съешь ещё этих мягких булок")
    with db.reader() as conn:
        (entry,) = media.message_media(conn, mid)
    assert (
        entry["transcript"]["status"] == "done"
        and entry["transcript"]["text"] == "съешь ещё этих мягких булок"
    )


def test_wait_ms_pending_download_then_queue_then_done(db, monkeypatch):
    monkeypatch.setattr(transcripts, "WORKER_SEEN_S", 3600.0)  # окно ожидания, не живость службы
    transcripts.mark_worker_seen()
    account_id = _setup(db)
    ts = START + 1000
    mid = _ingest(db, account_id, "1", [VOICE], ts=ts)
    other = _ingest(db, account_id, "2", [], ts=ts, text="просто текст")

    def waits(now):
        with db.reader() as conn:
            return transcripts.wait_ms_for_messages(conn, "main", [mid, other], now_ms=now)

    # медиа ещё качается: отсчёт от ts сообщения
    assert waits(ts + 10_000) == {mid: 170_000}
    assert waits(ts + 180_000) == {}
    media_id = _download(db, account_id, mid, now_ms=ts + 20_000)
    # в очереди: отсчёт от готовности медиа
    assert waits(ts + 30_000) == {mid: 170_000}
    with db.reader() as conn:
        assert transcripts.pending_for_messages(conn, "main", [mid, other], now_ms=ts + 30_000) == {mid}
    assert waits(ts + 200_001) == {}
    _done(db, media_id, "текст", now=ts + 40_000)
    assert waits(ts + 41_000) == {}
    # поле выдачи ленты
    monkeypatch.setattr(transcripts, "now_ms", lambda: ts + 41_000)
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, _channel(db, mid), before=None, limit=10)
    assert {m["id"]: m["transcript_wait_ms"] for m in page.messages} == {mid: 0, other: 0}


def _channel(db, message_id):
    with db.reader() as conn:
        return conn.execute("SELECT channel_id FROM main.message WHERE id = ?", (message_id,)).fetchone()[0]


def test_wait_ms_in_feed_while_queued(db, monkeypatch):
    monkeypatch.setattr(transcripts, "WORKER_SEEN_S", 3600.0)  # окно ожидания, не живость службы
    transcripts.mark_worker_seen()
    account_id = _setup(db)
    ts = START + 1000
    mid = _ingest(db, account_id, "1", [VOICE], ts=ts)
    _download(db, account_id, mid, now_ms=ts + 5_000)
    monkeypatch.setattr(transcripts, "now_ms", lambda: ts + 15_000)
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, _channel(db, mid), before=None, limit=10)
        changes = queries.changes_since(conn, UI, since=0, limit=100)
    assert page.messages[0]["transcript_wait_ms"] == 170_000
    assert [m["transcript_wait_ms"] for m in changes.messages if m["id"] == mid] == [170_000]


def test_wait_not_for_ineligible(db):
    account_id = _setup(db)
    ts = START + 1000
    audio = {"type": "audio", "mime": "audio/mpeg", "size": 5000, "duration_s": 60}
    mid = _ingest(db, account_id, "1", [audio], ts=ts)
    with db.reader() as conn:
        assert transcripts.wait_ms_for_messages(conn, "main", [mid], now_ms=ts + 1) == {}


def test_search_finds_by_transcript_once(db):
    account_id = _setup(db)
    mid = _ingest(db, account_id, "1", [VOICE, {**VOICE, "src_id": "v2"}], text="подпись")
    a0 = _download(db, account_id, mid, idx=0)
    a1 = _download(db, account_id, mid, idx=1)
    _done(db, a0, "договор подписан сегодня")
    _done(db, a1, "договор ещё раз")
    with db.reader() as conn:
        found = queries.search(conn, UI, q="договор", project_id=None)
        both = queries.search(conn, UI, q="подпись", project_id=None)
    assert [r["message_id"] for r in found] == [mid]
    assert found[0]["text"].startswith("подпись\n" + queries.TRANSCRIPT_SEARCH_MARK)
    assert "договор" in found[0]["text"]
    assert [r["message_id"] for r in both] == [mid] and both[0]["text"] == "подпись"


def test_search_voice_without_text(db):
    _a, _mid, media_id = _queued(db)
    _done(db, media_id, "встреча в пятницу")
    with db.reader() as conn:
        (hit,) = queries.search(conn, UI, q="пятницу", project_id=None)
    assert hit["text"] == "голосовое: встреча в пятницу"


# --- уборка (П4, каскад, ретенция) -------------------------------------------------------------------------


def test_hide_message_removes_transcript_and_search(db):
    _a, mid, media_id = _queued(db)
    _done(db, media_id, "секретное слово")
    db.write_sync(lambda conn: hiding.hide_message(conn, mid, now_ms=NOW))
    assert _transcript(db, media_id) is None
    with db.reader() as conn:
        assert queries.search(conn, UI, q="секретное", project_id=None) == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM main.transcript_fts WHERE transcript_fts MATCH 'секретное'"
            ).fetchone()[0]
            == 0
        )


def test_replaced_file_drops_transcript(db):
    """П4: правка сменила файл в той же строке `media` — расшифровка прежнего прочь (новая — при новом
    `ready`)."""
    account_id, mid, media_id = _queued(db)
    _done(db, media_id, "старое")
    _ingest(db, account_id, "1", [{**VOICE, "src_id": "v-new"}], edited_ts=START + 2000)
    assert _media_row(db, mid)[0] == media_id  # строка та же
    assert _transcript(db, media_id) is None
    with db.reader() as conn:
        assert queries.search(conn, UI, q="старое", project_id=None) == []


def test_removed_attachment_drops_transcript(db):
    account_id, mid, media_id = _queued(db)
    _ingest(db, account_id, "1", [], text="без вложения", edited_ts=START + 2000)
    assert _media_row(db, mid) is None
    assert _transcript(db, media_id) is None


def test_retention_purge_drops_transcript(db, monkeypatch):
    account_id = _setup(db, source="slack", db_name="work")
    clip = {"type": "audio", "mime": "audio/webm", "size": 5000, "clip": True, "src_id": "F1"}
    mid = _ingest(db, account_id, "1.1", [clip])
    media_id = _download(db, account_id, mid)
    _done(db, media_id, "рабочая тайна")
    cleaned = db.write_sync(
        lambda conn: retention._purge_message_portion(conn, after_id=0, cutoff=START + 10 * DAY)
    )
    assert cleaned[0] == 1
    assert _transcript(db, media_id) is None


def test_watch_summary(db):
    _a, _mid, media_id = _queued(db)
    with db.reader() as conn:
        summary = transcripts.watch_summary(conn, NOW)
    assert summary == {
        "configured": False,
        "queued": 1,
        "working": 0,
        "failed_24h": 0,
        "oldest_queued_ts": NOW,
    }
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.api_token (name, kind, hash, scopes, revoked) "
            "VALUES ('transcribe:worker', 'background', 'h', 'read transcribe', 0)"
        )
    )
    with db.reader() as conn:
        assert transcripts.watch_summary(conn, NOW)["configured"] is True


def test_attachment_item_helper():
    assert transcripts.attachment_item(json.dumps([{"a": 1}, {"round": True}]), 1) == {"round": True}
    assert transcripts.attachment_item("битый", 0) == {}
    assert transcripts.attachment_item(None, 0) == {}
