"""`POST /api/v1/owner_channel/bot-dictate`: только bearer ровно `human_bot`; дальше — общая часть с `POST /dictate`
(те же проверки, пределы и коды)."""

import pytest

from hub.core.routes import dictate as dictate_mod
from hub.speech.route import ChunkResult

from .conftest import configure, make_bearer
from .test_dictate_routes import pcm

API = "/api/v1/owner_channel/bot-dictate"


def bot_auth(db) -> dict:
    return {"Authorization": f"Bearer {make_bearer(db, scopes=('human_bot',), kind='background')}"}


async def test_human_bot_gets_text(client, db, config_dir, fake_sk):
    configure(config_dir)
    r = await client.post(API, content=pcm(1), headers=bot_auth(db))
    assert r.status_code == 200 and r.json() == {"text": "привет мир"}
    assert len(fake_sk.requests) == 1


async def test_recognize_chunk_is_the_shared_one(client, db, config_dir, monkeypatch):
    configure(config_dir)
    seen = []

    async def fake(app_state, body, config, lang, **kw):
        seen.append(len(body))
        return ChunkResult(ok=True, text="подмена")

    monkeypatch.setattr(dictate_mod, "recognize_chunk", fake)
    r = await client.post(API, content=pcm(2), headers=bot_auth(db))
    assert r.json() == {"text": "подмена"} and seen == [64_000]


@pytest.mark.parametrize(
    ("scopes", "kind"),
    [(("read",), "interactive"), (("human",), "background"), (("human_bot", "read"), "interactive")],
)
async def test_other_bearer_is_403(client, db, config_dir, fake_sk, scopes, kind):
    configure(config_dir)
    try:
        token = make_bearer(db, scopes=scopes, kind=kind)
    except ValueError:  # скоуп human_bot с чужими скоупами ядро не выдаёт — отказ раньше ручки
        return
    r = await client.post(API, content=pcm(1), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and fake_sk.requests == []


async def test_ui_session_is_403(client, db, clock, config_dir, fake_sk):
    from .conftest import ORIGIN, login

    configure(config_dir)
    csrf = await login(client, db, clock)
    r = await client.post(
        API,
        content=pcm(1),
        headers={"Origin": ORIGIN, "X-Hub-CSRF": csrf, "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 403 and fake_sk.requests == []


async def test_off_and_bad_audio_codes_like_dictate(client, db, config_dir, fake_sk):
    r = await client.post(API, content=pcm(1), headers=bot_auth(db))
    assert r.status_code == 404 and r.json() == {"detail": "dictate_off"}
    configure(config_dir)
    r = await client.post(API, content=pcm(0.1), headers=bot_auth(db))
    assert r.status_code == 400 and r.json() == {"detail": "bad_audio"}
    r = await client.post(API, content=pcm(31), headers=bot_auth(db))
    assert r.status_code == 400


async def test_upstream_errors_like_dictate(client, db, config_dir, fake_sk):
    configure(config_dir)
    fake_sk.status = 401
    fake_sk.body = {"message": "x"}
    r = await client.post(API, content=pcm(1), headers=bot_auth(db))
    assert r.status_code == 502 and r.json() == {"detail": "dictate_key"}


async def test_minute_limit_shared(client, db, config_dir, fake_sk):
    configure(config_dir)
    for _ in range(60):
        assert (await client.post(API, content=pcm(1), headers=bot_auth(db))).status_code == 200
    r = await client.post(API, content=pcm(1), headers=bot_auth(db))
    assert r.status_code == 429 and r.json() == {"detail": "dictate_rate"}
