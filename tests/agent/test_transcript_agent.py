"""Ф14-6b (docs/architecture.md, 14-6b «Разборщик», П2, П6): разборщик ждёт расшифровку
голосового — кандидат с `transcript_wait_ms > 0` не превращается в карточку, беседа пересматривается после
срока ядра или по бампу готовой расшифровки; бамп не даёт второй карточки; карточка по правилам для
голосового без текста — «[голосовое] <начало расшифровки>».

Ф57-1: модели нет — ожидание расшифровки проверяется на карточке по правилам (`react`).

Обвязка — `tests/agent/conftest.py` (ядро в процессе, одни часы)."""

import hashlib

import pytest

from hub.core import media, transcripts

from .conftest import insert_project, msg, read_event

OGG = b"OggS" + b"\x00" * 60
SPOKEN = "владелец, перезвони насчёт договора до пятницы"


@pytest.fixture
def voice_env(env, monkeypatch):
    """Часы выдачи ядра (`transcript_wait_ms`) — часы теста; служба расшифровки «жива»."""
    monkeypatch.setattr(transcripts, "now_ms", lambda: env.now_ms)
    transcripts.mark_worker_seen()
    env.db.write_sync(lambda conn: media.ensure_start_ts(conn, env.now_ms - 3_600_000))
    return env


def _voice(ext_id: str, ts: int, *, chat: str = "100") -> dict:
    event = msg(ext_id, ts, chat=chat, text="", mkind="file")
    event["text"] = None
    event["attachments"] = [
        {"type": "voice", "mime": "audio/ogg", "size": 64, "duration_s": 12, "src_id": f"v{ext_id}"}
    ]
    return event


def _media_id(env, chat: str, ext_id: str) -> int:
    mid = env.message_id(chat, ext_id)
    with env.db.reader() as conn:
        return conn.execute("SELECT id FROM main.media WHERE message_id = ?", (mid,)).fetchone()[0]


def _ready(env, media_id: int) -> None:
    sha = hashlib.sha256(OGG).hexdigest()
    path = media.file_path(env.db.data_dir, env.account_id, sha)
    media.ensure_dir(path.parent)
    path.write_bytes(OGG)
    now = env.now_ms

    def _fn(conn):
        conn.execute(
            "UPDATE main.media SET status = 'ready', sha256 = ?, stored_size = ?, detected = 'ogg', "
            "fetched_ts = ? WHERE id = ?",
            (sha, len(OGG), now, media_id),
        )
        assert transcripts.on_media_ready(conn, media_id, now_ms=now)

    env.db.write_sync(_fn)


def _done(env, media_id: int, text: str = SPOKEN) -> None:
    now = env.now_ms

    def _fn(conn):
        job = transcripts.lease(conn, now_ms=now)
        transcripts.report(
            conn,
            media_id=media_id,
            lease_gen=job["lease_gen"],
            outcome="done",
            text=text,
            lang="ru",
            now_ms=now,
        )

    env.db.write_sync(_fn)


async def _agent(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


async def test_card_waits_for_transcript_then_one_card_with_text(voice_env):
    env = voice_env
    agent = await _agent(env)
    env.ingest([_voice("1", env.now_ms)])
    media_id = _media_id(env, "100", "1")
    _ready(env, media_id)
    await agent.poll_once()
    cid = env.channel_id("100")
    assert cid in agent.buffers and agent.buffers[cid].hold_until > 0
    env.advance(20)  # личка созрела бы через 15 с
    assert await agent.tick() == 0
    assert env.items() == []

    env.advance(10)
    _done(env, media_id)
    await agent.poll_once()  # бамп `seq` по готовности — ожидание снято, новых кандидатов нет
    assert agent.buffers[cid].hold_until == 0
    assert await agent.tick() == 1
    (item,) = env.items()
    assert item["message_ids"] == [env.message_id("100", "1")]
    assert item["summary"] == f"Пётр Иванов: [голосовое] {SPOKEN}"

    # путь курсора: ещё опрос и тик — второй карточки на то же сообщение нет
    await agent.poll_once()
    env.advance(120)
    await agent.tick()
    assert env.items() == [item]


async def test_card_stops_waiting_after_core_term(voice_env):
    env = voice_env
    agent = await _agent(env)
    env.ingest([_voice("1", env.now_ms)])
    _ready(env, _media_id(env, "100", "1"))
    await agent.poll_once()
    env.advance(170)
    assert await agent.tick() == 0
    env.advance(11)  # 180 с от готовности медиа — ядро больше не просит ждать
    assert await agent.tick() == 1
    (item,) = env.items()
    assert item["summary"] == "Пётр Иванов: [вложение]"  # расшифровки так и нет — карточка без неё


async def test_react_card_label_is_voice_transcript(voice_env):
    env = voice_env
    pid = insert_project(env.db, name="P")
    env.ingest([msg("0", env.now_ms - 7_200_000, text="давно")])
    env.ingest([read_event("100", "0")])
    cid = env.channel_id("100")
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (pid, cid))
    )
    agent = await _agent(env)
    env.ingest([_voice("1", env.now_ms)])
    media_id = _media_id(env, "100", "1")
    _ready(env, media_id)
    await agent.poll_once()
    assert cid in agent.buffers
    env.advance(121)
    assert await agent.tick() == 0  # тишина прошла, но расшифровка ещё идёт
    _done(env, media_id)
    await agent.poll_once()
    assert await agent.tick() == 1
    (item,) = env.items()
    assert item["provider"] == "rules"
    assert item["summary"] == f"Пётр Иванов: [голосовое] {SPOKEN}"

