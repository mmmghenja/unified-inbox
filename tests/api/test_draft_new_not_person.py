"""24.10: `hub_draft_new(to="@ник")` канала или группы Telegram — отказ 422 сразу, до заготовки и
черновика: ядро спрашивает коннектор (`TgLookup`, вид `resolve`). Человек, бот, ник не найден, коннектор
молчит, не ответил или старый — заготовка как раньше. Стенд (общий посредник публичного и внутреннего
приложений) — `test_tg_lookup`."""

# ruff: noqa: F811 — фикстуры `lookup`, `app`, `internal_app`, `ic` взяты из соседнего модуля

import asyncio

from hub.core import tg_lookup as lookup_mod

from .conftest import insert_project
from .test_tg_lookup import (  # noqa: F401 — фикстуры стенда
    API,
    INTERNAL,
    _account,
    _answer,
    _audit,
    _bearer,
    _token,
    app,
    ic,
    internal_app,
    lookup,
)


def _counts(db) -> tuple[int, int]:
    with db.reader() as conn:
        drafts = conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id LIKE 'new:%'").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0]
    return drafts, rows


def _body(account_id, project_id, to):
    return {"account_id": account_id, "project_id": project_id, "to": to, "text": "Здравствуйте!"}


async def _draft_new(client, clock, ic, h, token, body, **answer):
    """Ручка ждёт коннектор: он берёт заявку `resolve` и отвечает `answer`. Интервал между выдачами аккаунта
    (`GAP_MS`) часы стенда проходят до запроса."""
    clock.advance_ms(lookup_mod.GAP_MS)
    task = asyncio.ensure_future(client.post(f"{API}/drafts/new", json=body, headers=_bearer(token)))
    item = await _answer(ic, h, **answer)
    return await task, item


async def test_channel_and_group_refused_before_draft(client, ic, db, clock):
    project = insert_project(db, name="Hooli")
    acc, h = _account(db, clock)
    token = _token(db)
    r, item = await _draft_new(
        client, clock, ic, h, token, _body(acc, project, "@news_chan"), payload={"kind": "broadcast"}
    )
    assert item["kind"] == "resolve" and item["params"] == {"username": "news_chan"}
    assert r.status_code == 422, r.text
    assert "@news_chan — канал, а не человек" in r.json()["detail"]
    assert _counts(db) == (0, 0)
    [(result, _actor, detail)] = _audit(db, "draft_new")
    assert result == "not_person" and detail["to_kind"] == "broadcast"
    r, _item = await _draft_new(
        client, clock, ic, h, token, _body(acc, project, "t.me/talk_group"), payload={"kind": "megagroup"}
    )
    assert r.status_code == 422 and "@talk_group — группа, а не человек" in r.json()["detail"]
    # Ответ помнится: повтор того же ника — отказ без нового запроса к Telegram.
    r = await client.post(f"{API}/drafts/new", json=_body(acc, project, "@News_Chan"), headers=_bearer(token))
    assert r.status_code == 422
    nxt = await ic.post(f"{INTERNAL}/lookup/next", headers=h, json={"timeout_s": 0})
    assert nxt.status_code == 204
    assert _counts(db) == (0, 0)


async def test_user_and_bot_go_on(client, ic, db, clock):
    project = insert_project(db, name="Hooli")
    acc, h = _account(db, clock)
    token = _token(db)
    r, _item = await _draft_new(
        client, clock, ic, h, token, _body(acc, project, "@petr_ivanov"), payload={"kind": "user"}
    )
    assert r.status_code == 200, r.text
    r, _item = await _draft_new(
        client, clock, ic, h, token, _body(acc, project, "@help_bot"), payload={"kind": "bot"}
    )
    assert r.status_code == 200, r.text
    assert _counts(db) == (2, 2)


async def test_unknown_answers_do_not_block(client, ic, db, clock):
    """Ник не найден, старый коннектор (`bad_item`), FloodWait — заготовка как раньше."""
    project = insert_project(db, name="Hooli")
    acc, h = _account(db, clock)
    token = _token(db)
    for n, answer in enumerate(
        [
            {"outcome": "error", "reason": "not_found"},
            {"outcome": "error", "reason": "bad_item"},
            {"outcome": "error", "reason": "flood", "retry_after_s": 30},
        ]
    ):
        r, _item = await _draft_new(
            client, clock, ic, h, token, _body(acc, project, f"@person_{n:04d}"), **answer
        )
        assert r.status_code == 200, r.text
    # После FloodWait аккаунт на паузе — Telegram не спрашиваем вовсе, заготовка создаётся.
    r = await client.post(
        f"{API}/drafts/new", json=_body(acc, project, "@person_9999"), headers=_bearer(token)
    )
    assert r.status_code == 200, r.text
    assert _counts(db) == (4, 4)


async def test_connector_silent_or_slow_does_not_block(client, ic, db, clock, lookup, monkeypatch):
    project = insert_project(db, name="Hooli")
    silent, _h = _account(db, clock, alive=False)
    token = _token(db)
    r = await client.post(
        f"{API}/drafts/new", json=_body(silent, project, "@news_chan"), headers=_bearer(token)
    )
    assert r.status_code == 200, r.text  # коннектор не запущен — не спрашиваем, проверка при отправке
    assert lookup._queues.get(silent) is None  # noqa: SLF001
    monkeypatch.setattr(lookup_mod, "RESOLVE_TIMEOUT_S", 0.05)
    acc, _h2 = _account(db, clock)
    r = await client.post(
        f"{API}/drafts/new", json=_body(acc, project, "@other_chan"), headers=_bearer(token)
    )
    assert r.status_code == 200, r.text  # коннектор не ответил вовремя — заготовка как раньше


async def test_phone_and_mail_not_resolved(client, ic, db, clock, lookup):
    project = insert_project(db, name="Hooli")
    acc, _h = _account(db, clock)
    token = _token(db)
    r = await client.post(
        f"{API}/drafts/new", json=_body(acc, project, "+7 900 000-00-00"), headers=_bearer(token)
    )
    assert r.status_code == 200, r.text
    assert not lookup._queues.get(acc)  # noqa: SLF001 — номер телефона коннектору не уходил
