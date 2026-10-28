"""Ф79-1, разделы A и B (docs/architecture.md): `POST /dictate` — свой whisper первым
(быстрый работник долгим опросом `POST /speech/lease`), сбой или срок — SpeechKit, нет ключа — 503; пределы;
`GET /dictate`; ручки работника (скоуп `transcribe`, только bearer)."""

import asyncio
import base64
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from hub.speech import route as route_mod
from hub.speech.local import LocalResult

from .conftest import BASE_URL, ORIGIN, configure, login, make_bearer

API = "/api/v1/dictate"
SEC = 32_000


def pcm(seconds: float) -> bytes:
    return b"\x01\x00" * (int(seconds * SEC) // 2)


def hdrs(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-Hub-CSRF": csrf, "Content-Type": "application/octet-stream"}


@pytest.fixture
async def csrf(client, db, clock):
    return await login(client, db, clock)


@pytest.fixture
def worker_token(db) -> str:
    return make_bearer(db, scopes=("read", "transcribe"), kind="background")


@pytest.fixture
async def wclient(app):
    """Свой клиент работника: без куки интерфейса (у `client` она есть после входа)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


def wauth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeWorker:
    """Быстрый работник: долгий опрос ядра и ответ по `reply(job) -> тело | None` (`None` — молчит)."""

    def __init__(self, client, token, reply) -> None:
        self.client, self.token, self.reply = client, token, reply
        self.jobs: list[dict] = []
        self.results: list[int] = []
        self.task: asyncio.Task | None = None

    async def _run(self, n: int) -> None:
        for _ in range(n):
            r = await self.client.post("/api/v1/speech/lease", json={"wait_s": 5}, headers=wauth(self.token))
            if r.status_code != 200:
                continue
            job = r.json()
            self.jobs.append(job)
            body = self.reply(job)
            if body is not None:
                rr = await self.client.post(
                    f"/api/v1/speech/{job['job_id']}/result", json=body, headers=wauth(self.token)
                )
                self.results.append(rr.status_code)

    async def start(self, n: int = 1) -> FakeWorker:
        self.task = asyncio.create_task(self._run(n))
        await asyncio.sleep(0.05)  # вошёл в опрос — ядро видит работника
        return self

    async def done(self) -> None:
        assert self.task is not None
        await asyncio.wait_for(self.task, 10)


async def test_whisper_path_without_speechkit_key(wclient, client, csrf, worker_token, fake_sk):
    worker = await FakeWorker(wclient, worker_token, lambda job: {"text": "свой whisper"}).start()
    assert (await client.get(API)).json() == {"enabled": True}
    body = pcm(1)
    r = await client.post(API, content=body, headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 200 and r.json() == {"text": "свой whisper"}
    assert base64.b64decode(worker.jobs[0]["audio_b64"]) == body and worker.jobs[0]["lang"] == "ru"
    assert worker.results == [204]
    assert fake_sk.requests == []


async def test_whisper_first_even_with_key(wclient, client, csrf, config_dir, worker_token, fake_sk):
    configure(config_dir)
    worker = await FakeWorker(wclient, worker_token, lambda job: {"text": "ноутбук"}).start()
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    await worker.done()
    assert r.json() == {"text": "ноутбук"} and fake_sk.requests == []


async def test_silence_from_whisper_is_empty_text_not_speechkit(
    wclient, client, csrf, config_dir, worker_token, fake_sk
):
    configure(config_dir)
    worker = await FakeWorker(wclient, worker_token, lambda job: {"text": ""}).start()
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 200 and r.json() == {"text": ""} and fake_sk.requests == []


async def test_worker_error_falls_back_to_speechkit(wclient, client, csrf, config_dir, worker_token, fake_sk):
    configure(config_dir)
    worker = await FakeWorker(wclient, worker_token, lambda job: {"error": "engine_failed"}).start()
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 200 and r.json() == {"text": "привет мир"} and len(fake_sk.requests) == 1


async def test_worker_timeout_falls_back_and_late_answer_is_409(
    wclient, client, csrf, config_dir, worker_token, fake_sk, monkeypatch
):
    monkeypatch.setattr(route_mod, "LOCAL_TIMEOUT_S", 0.2)
    configure(config_dir)
    worker = await FakeWorker(wclient, worker_token, lambda job: None).start()
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 200 and r.json() == {"text": "привет мир"}
    late = await wclient.post(
        f"/api/v1/speech/{worker.jobs[0]['job_id']}/result",
        json={"text": "поздно"},
        headers=wauth(worker_token),
    )
    assert late.status_code == 409 and late.json() == {"detail": "stale"}


async def test_no_key_and_worker_error_is_503(wclient, client, csrf, worker_token, fake_sk):
    worker = await FakeWorker(wclient, worker_token, lambda job: {"error": "engine_failed"}).start()
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 503 and r.json() == {"detail": "dictate_unavailable"}


async def test_no_key_no_worker_is_dictate_off_and_disabled(client, csrf, fake_sk):
    assert (await client.get(API)).json() == {"enabled": False}
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    assert r.status_code == 404 and r.json() == {"detail": "dictate_off"}


async def test_worker_counts_alive_30s_after_poll(wclient, client, csrf, worker_token, app, fake_sk):
    r = await wclient.post("/api/v1/speech/lease", json={"wait_s": 0}, headers=wauth(worker_token))
    assert r.status_code == 204
    assert (await client.get(API)).json() == {"enabled": True}
    app.state.speech_broker._seen -= 31  # noqa: SLF001 — 31 с без опроса
    assert (await client.get(API)).json() == {"enabled": False}


async def test_unknown_lang_goes_straight_to_speechkit(
    wclient, client, csrf, config_dir, worker_token, fake_sk
):
    configure(config_dir, lang="kk-KZ")
    worker = await FakeWorker(wclient, worker_token, lambda job: {"text": "не должен"}).start()
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    assert r.json() == {"text": "привет мир"} and len(fake_sk.requests) == 1
    assert worker.jobs == []
    worker.task.cancel()


async def test_daily_cap_only_on_speechkit_path(wclient, client, csrf, config_dir, worker_token, fake_sk):
    configure(config_dir, daily_chunks=1)
    worker = await FakeWorker(wclient, worker_token, lambda job: {"text": "whisper"}).start(3)
    for _ in range(3):
        assert (await client.post(API, content=pcm(0.5), headers=hdrs(csrf))).json() == {"text": "whisper"}
    await worker.done()
    worker = await FakeWorker(wclient, worker_token, lambda job: {"error": "engine_failed"}).start(2)
    assert (await client.post(API, content=pcm(0.5), headers=hdrs(csrf))).json() == {"text": "привет мир"}
    r = await client.post(API, content=pcm(0.5), headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 429 and r.json() == {"detail": "dictate_daily"}
    assert len(fake_sk.requests) == 1


async def test_minute_limit_covers_whisper_path(client, csrf, app, fake_sk, monkeypatch):
    async def instant(pcm_, lang, *, timeout_s):
        return LocalResult(text="w", elapsed_ms=1)

    broker = app.state.speech_broker
    broker.mark_seen()
    monkeypatch.setattr(broker, "recognize", instant)
    for _ in range(60):
        assert (await client.post(API, content=pcm(0.5), headers=hdrs(csrf))).status_code == 200
    r = await client.post(API, content=pcm(0.5), headers=hdrs(csrf))
    assert r.status_code == 429 and r.json() == {"detail": "dictate_rate"}


async def test_log_line_names_path_without_text(wclient, client, csrf, worker_token, caplog, fake_sk):
    worker = await FakeWorker(wclient, worker_token, lambda job: {"text": "секретная фраза"}).start()
    with caplog.at_level(logging.DEBUG):
        r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    await worker.done()
    assert r.status_code == 200
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "hub.core.routes.dictate"]
    assert len(lines) == 1 and lines[0].startswith(f"диктовка: {SEC} байт, whisper, ")
    assert lines[0].endswith(", 200")
    assert "секретная фраза" not in "\n".join(rec.getMessage() for rec in caplog.records)


# --- ручки работника --------------------------------------------------------------------------------------


async def test_worker_routes_need_transcribe_bearer(client, csrf, db):
    r = await client.post(
        "/api/v1/speech/lease", json={"wait_s": 0}, headers={"Origin": ORIGIN, "X-Hub-CSRF": csrf}
    )
    assert r.status_code == 403
    other = make_bearer(db, scopes=("read",), kind="background")
    assert (await client.post("/api/v1/speech/lease", json={}, headers=wauth(other))).status_code == 403
    r = await client.post("/api/v1/speech/abc/result", json={"text": "x"}, headers=wauth(other))
    assert r.status_code == 403


async def test_result_validation_and_unknown_job(client, worker_token):
    h = wauth(worker_token)
    assert (await client.post("/api/v1/speech/abc/result", json={"text": "x"}, headers=h)).status_code == 409
    both = await client.post("/api/v1/speech/abc/result", json={"text": "x", "error": "y"}, headers=h)
    assert both.status_code == 422
    assert (await client.post("/api/v1/speech/abc/result", json={}, headers=h)).status_code == 422
    extra = await client.post("/api/v1/speech/abc/result", json={"text": "x", "z": 1}, headers=h)
    assert extra.status_code == 422
    r = await client.post("/api/v1/speech/lease", json={"wait_s": 0}, headers=h)
    assert r.status_code == 204
