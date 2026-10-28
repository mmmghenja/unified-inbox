"""Ф11-2 (docs/architecture.md), раздел B: карточка делится по ответу и прочтению владельца,
отвеченные карточки закрываются по моему отправленному сообщению в `/sync` (B2), сквозной сценарий «две
пачки, между ними мой ответ». Тесты 7–9 спеки.

Ф57-1: модели нет — сценарии перенесены на карточку по правилам (`react`); проверка «пересказ открытой
карточки в промпте» заменена проверкой слияния и разделения самих карточек.
"""

import logging

import pytest

from hub.agent.core_api import CoreError, CoreUnavailable

from .conftest import _api_conftest, msg, read_event


async def _started(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


async def _cycle(env, agent, seconds: float = 16) -> int:
    await agent.poll_once()
    env.advance(seconds)
    return await agent.tick()


def _spy_post(monkeypatch, agent) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    original = agent.core.post

    async def post(path, json=None):
        calls.append((path, json))
        return await original(path, json=json)

    monkeypatch.setattr(agent.core, "post", post)
    return calls


def _closes(calls) -> list[int]:
    return [body["channel_id"] for path, body in calls if path == "/agent/items/close-answered"]


# --- Тест 7: открытая карточка — слияние, пока она живая; прочитанная и отвеченная — новая ------------------


async def test_f11_2_7_live_open_card_is_merged(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="100", text="первое")])
    await _cycle(env, agent)
    env.ingest([msg("2", env.now_ms - 500, chat="100", text="второе")])
    await _cycle(env, agent)
    (item,) = env.items()  # слияние, как раньше
    assert item["message_ids"] == [env.message_id("100", "1"), env.message_id("100", "2")]
    assert item["summary"] == "Пётр Иванов: второе"


async def test_f11_2_7_read_open_card_closed_new_card(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="100", text="первое")])
    await _cycle(env, agent)
    env.ingest([read_event("100", "1")])
    env.ingest([msg("2", env.now_ms - 500, chat="100", text="второе")])
    await _cycle(env, agent)
    first, second = env.items()
    assert first["status"] == "seen" and second["status"] == "new"
    assert first["summary"] == "Пётр Иванов: первое"
    assert second["message_ids"] == [env.message_id("100", "2")]


async def test_f11_2_7_answered_open_card_closed_new_card(env, monkeypatch):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="100", text="первое")])
    await _cycle(env, agent)

    async def no_close(channel_id):  # B1 отдельно от B2: карточку закрывает только разделение в ядре
        return None

    monkeypatch.setattr(agent, "_close_answered", no_close)
    env.ingest([msg("9", env.now_ms - 800, chat="100", is_me=True, text="ответил")])
    env.ingest([msg("2", env.now_ms - 500, chat="100", text="второе")])
    await _cycle(env, agent)
    first, second = env.items()
    assert first["status"] == "done" and second["status"] == "new"
    assert second["message_ids"] == [env.message_id("100", "2")]


# --- Тест 8: close-answered по моему отправленному сообщению -----------------------------------------------


async def test_f11_2_8_one_close_answered_per_channel_per_poll(env, monkeypatch):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="100", text="вопрос")])
    await _cycle(env, agent)
    (card,) = env.items()
    calls = _spy_post(monkeypatch, agent)

    env.ingest(
        [
            msg("9", env.now_ms - 800, chat="100", is_me=True, text="ответ 1"),
            msg("10", env.now_ms - 700, chat="100", is_me=True, text="ответ 2"),
        ]
    )
    await agent.poll_once()
    assert _closes(calls) == [env.channel_id("100")]
    assert env.items()[0]["id"] == card["id"] and env.items()[0]["status"] == "done"
    assert agent.state["counters"]["closed_answered"] == 1


async def test_f11_2_8_draft_and_foreign_message_no_call(env, monkeypatch):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="100", text="вопрос")])
    await _cycle(env, agent)
    calls = _spy_post(monkeypatch, agent)

    # Заготовка (моё, без ext_id) и чужое сообщение — не ответ.
    _api_conftest.insert_message(env.db, env.channel_id("100"), ext_id=None, ts=env.now_ms, is_me=1)
    env.ingest([msg("2", env.now_ms - 500, chat="100", text="ещё вопрос")])
    await agent.poll_once()
    assert _closes(calls) == []
    assert env.items()[0]["status"] == "new"


@pytest.mark.parametrize("error", [CoreError(404), CoreUnavailable("HTTP 503")])
async def test_f11_2_8_core_error_on_close_does_not_break_poll(env, monkeypatch, caplog, error):
    agent = await _started(env)
    original = agent.core.post

    async def post(path, json=None):
        if path == "/agent/items/close-answered":
            raise error
        return await original(path, json=json)

    monkeypatch.setattr(agent.core, "post", post)
    env.ingest([msg("9", env.now_ms - 800, chat="100", is_me=True, text="моё")])
    env.ingest([msg("2", env.now_ms - 500, chat="100", text="вопрос")])
    with caplog.at_level(logging.WARNING, logger="hub.agent"):
        added = await agent.poll_once()
    assert added == 1
    with env.db.reader() as conn:
        max_seq = conn.execute("SELECT MAX(seq) FROM main.message").fetchone()[0]
    assert agent.sync_pos >= max_seq  # курсор не держит
    assert any("закрыть отвеченную карточку не удалось" in r.getMessage() for r in caplog.records)
    env.advance(16)
    assert await agent.tick() == 1  # карточки ставятся дальше


# --- Тест 9: сквозной — две пачки, между ними мой ответ -----------------------------------------------------


async def test_f11_2_9_two_batches_with_my_reply_two_cards(env):
    agent = await _started(env)
    env.ingest([msg(str(i), env.now_ms - 3000 + i, chat="100", text=f"пачка 1: {i}") for i in (1, 2)])
    await _cycle(env, agent)
    (first,) = env.items()
    assert first["summary"] == "Пётр Иванов: пачка 1: 2"

    env.ingest([msg("9", env.now_ms - 900, chat="100", is_me=True, text="мой ответ")])
    env.ingest([msg(str(i), env.now_ms - 500 + i, chat="100", text=f"пачка 2: {i}") for i in (3, 4)])
    await _cycle(env, agent)

    old, new = env.items()
    assert old["id"] == first["id"] and old["status"] == "done"
    assert old["summary"] == "Пётр Иванов: пачка 1: 2"  # закрытую карточку новая пачка не трогает
    assert new["status"] == "new" and new["summary"] == "Пётр Иванов: пачка 2: 4"
    assert new["message_ids"] == [env.message_id("100", "3"), env.message_id("100", "4")]


# --- Ф11-9 H4: карточка, рождённая уже отвеченной, сразу закрыта --------------------------------------------


def _stale_snapshot(agent, monkeypatch) -> None:
    """Гонка: кандидаты собраны до моего ответа — снимок беседы у разборщика устарел, свой фильтр
    «отвечено» (`_seen_by_me`) их не снимает; карточка по ним приходит в ядро уже после ответа."""

    async def keep_all(channel, to_triage):
        return to_triage

    monkeypatch.setattr(agent, "_seen_by_me", keep_all)


async def test_f11_9_h4_card_born_answered_is_done(env, monkeypatch):
    agent = await _started(env)
    _stale_snapshot(agent, monkeypatch)
    env.ingest([msg("1", env.now_ms - 1000, chat="901", title="Личка", text="вопрос")])
    env.ingest([msg("9", env.now_ms - 800, chat="901", is_me=True, text="ответил")])
    await _cycle(env, agent, 100)
    (card,) = env.items()
    assert (card["provider"], card["status"]) == ("rules", "done")


async def test_f11_9_h4_unanswered_card_stays_new(env, monkeypatch):
    agent = await _started(env)
    _stale_snapshot(agent, monkeypatch)
    env.ingest([msg("9", env.now_ms - 1000, chat="100", is_me=True, text="моё раньше")])
    env.ingest([msg("1", env.now_ms - 800, chat="100", text="вопрос")])
    await _cycle(env, agent)
    (card,) = env.items()
    assert card["status"] == "new"
    assert [n[0] for n in env.notes] == ["Хаб · Неразобранное: Пётр"]
