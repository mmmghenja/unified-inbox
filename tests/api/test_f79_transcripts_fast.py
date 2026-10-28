"""Ф79-1, раздел C (docs/architecture.md): голосовые — быстрому работнику (ноутбук, cuda)
первым. Пока он опрашивал `lease` с `fast=True` последние 60 с, медленный (`fast=False`, cpu стационарника)
получает 204 — кроме заданий, ждущих в очереди дольше 180 с."""

import pytest

from hub.core import transcripts

from .test_transcripts_api import VOICE, _bearer, _ready, _setup, _token

LEASE = "/api/v1/transcripts/lease"


class Mono:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def mono(monkeypatch) -> Mono:
    m = Mono()
    monkeypatch.setattr(transcripts, "_mono", m)
    return m


async def _lease(client, token, **body):
    return await client.post(LEASE, json={"wait_s": 0, **body}, headers=_bearer(token))


async def test_old_client_without_fast_field_still_leases(client, db, clock, mono):
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, media_id, int(clock() * 1000))
    r = await _lease(client, _token(db))
    assert r.status_code == 200 and r.json()["media_id"] == media_id
    assert not transcripts.fast_alive()


async def test_slow_gets_204_while_fast_polls_then_job_after_180s(client, db, clock, mono):
    token = _token(db)
    assert (await _lease(client, token, fast=True)).status_code == 204  # быстрый на связи, очередь пуста
    assert transcripts.fast_alive() and transcripts.worker_alive()
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, media_id, int(clock() * 1000))
    assert (await _lease(client, token, fast=False)).status_code == 204
    clock.advance_ms(179_000)
    mono.t += 30  # быстрый опрашивал 30 с назад — ещё жив
    assert (await _lease(client, token)).status_code == 204
    clock.advance_ms(2_000)  # ждёт 181 с: быстрый занят или завис — отдаётся и медленному
    r = await _lease(client, token)
    assert r.status_code == 200 and r.json()["media_id"] == media_id


async def test_fast_gets_fresh_job_at_once(client, db, clock, mono):
    token = _token(db)
    await _lease(client, token, fast=True)
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, media_id, int(clock() * 1000))
    r = await _lease(client, token, fast=True)
    assert r.status_code == 200 and r.json()["media_id"] == media_id


async def test_slow_gets_job_when_fast_silent_60s(client, db, clock, mono):
    token = _token(db)
    await _lease(client, token, fast=True)
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, media_id, int(clock() * 1000))
    mono.t += 59
    assert (await _lease(client, token)).status_code == 204
    mono.t += 2  # быстрый молчит 61 с — ноутбук выключен
    assert not transcripts.fast_alive()
    r = await _lease(client, token)
    assert r.status_code == 200 and r.json()["media_id"] == media_id


async def test_slow_lease_still_expires_stale_working_rows(client, db, clock, mono):
    """Пауза и лизы — как были: просроченный лиз быстрого снимается и опросом медленного (задание старое —
    уходит медленному)."""
    token = _token(db)
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, media_id, int(clock() * 1000))
    r = await _lease(client, token, fast=True)
    assert r.status_code == 200
    clock.advance_ms(transcripts.lease_ms(VOICE["duration_s"]) + 1)  # быстрый взял и пропал
    assert (await _lease(client, token)).status_code == 204  # лиз снят, но задание ждёт меньше 180 с
    clock.advance_ms(transcripts.SLOW_AFTER_MS)
    r = await _lease(client, token)
    assert r.status_code == 200 and r.json()["media_id"] == media_id
    assert r.json()["lease_gen"] == 2
