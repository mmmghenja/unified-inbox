"""Ф79-1, раздел B (docs/architecture.md): куски интервью — свой whisper первым (язык
собеседника `en-US` -> `en`, свой — `ru`), сбой — SpeechKit; без ключа при живом работнике кусок принимается;
суточный предел интервью — только путь SpeechKit."""

# ruff: noqa: F811 — фикстуры импортированы из test_f60_interview.py и test_f41_held_work.py

import asyncio

from hub.core import security
from hub.speech import config as speech_config

from .test_f41_held_work import API, _mk, ui, world  # noqa: F401 — фикстуры
from .test_f60_interview import (  # noqa: F401 — фикстуры
    _chunk,
    _start,
    app,
    config_dir,
    fake_sk,
    listen_token,
    sleeper,
)


def _worker_token(db) -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="transcribe:worker",
            kind="background",
            scopes=["read", "transcribe"],
            project_ids=None,
            expires_ts=None,
        )
    ).token


async def _worker(client, token, replies: list[dict | None], jobs: list[dict]) -> None:
    h = {"Authorization": f"Bearer {token}"}
    for reply in replies:
        r = await client.post(f"{API}/speech/lease", json={"wait_s": 5}, headers=h)
        assert r.status_code == 200, r.text
        jobs.append(r.json())
        if reply is not None:
            rr = await client.post(f"{API}/speech/{r.json()['job_id']}/result", json=reply, headers=h)
            assert rr.status_code == 204


async def test_chunks_go_to_whisper_with_language_code(client, ui, db, world, listen_token, fake_sk):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid, lang="en-US"))["interview"]["id"]
    jobs: list[dict] = []
    task = asyncio.create_task(
        _worker(client, _worker_token(db), [{"text": "Hello"}, {"text": "Привет"}], jobs)
    )
    await asyncio.sleep(0.05)
    them = await _chunk(client, listen_token, iid, "them")
    me = await _chunk(client, listen_token, iid, "me", start_ms=1_700_000_002_000)
    await asyncio.wait_for(task, 10)
    assert [j["lang"] for j in jobs] == ["en", "ru"]
    assert them["line"]["chars"] == len("Hello") and me["line"]["chars"] == len("Привет")
    assert fake_sk.langs == []


async def test_worker_error_falls_back_to_speechkit(client, ui, db, world, listen_token, fake_sk):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    task = asyncio.create_task(_worker(client, _worker_token(db), [{"error": "engine_failed"}], []))
    await asyncio.sleep(0.05)
    fake_sk.texts = ["из SpeechKit"]
    out = await _chunk(client, listen_token, iid, "them")
    await asyncio.wait_for(task, 10)
    assert out["line"]["chars"] == len("из SpeechKit") and fake_sk.langs == ["ru-RU"]


async def test_without_key_whisper_still_works_then_unavailable(
    client, ui, db, world, listen_token, config_dir, fake_sk
):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    speech_config.key_path(config_dir).unlink()
    task = asyncio.create_task(
        _worker(client, _worker_token(db), [{"text": "только ноутбук"}, {"error": "engine_failed"}], [])
    )
    await asyncio.sleep(0.05)
    out = await _chunk(client, listen_token, iid, "them")
    assert out["line"]["chars"] == len("только ноутбук")
    out = await _chunk(client, listen_token, iid, "them", start_ms=1_700_000_002_000, expect=503)
    assert out == {"detail": "listen_unavailable"}
    await asyncio.wait_for(task, 10)
    assert fake_sk.langs == []


async def test_daily_cap_does_not_stop_whisper(client, ui, db, world, listen_token, app, fake_sk):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    await _chunk(client, listen_token, iid)  # завести пределы
    app.state.interview_limits._day_count = 4000  # noqa: SLF001 — суточный потолок SpeechKit исчерпан
    task = asyncio.create_task(_worker(client, _worker_token(db), [{"text": "whisper"}], []))
    await asyncio.sleep(0.05)
    out = await _chunk(client, listen_token, iid, start_ms=1_700_000_002_000)
    await asyncio.wait_for(task, 10)
    assert out["line"]["chars"] == len("whisper")
    assert (await _chunk(client, listen_token, iid, start_ms=1_700_000_004_000, expect=429)) == {
        "detail": "listen_daily"
    }
