"""Ф61-1, находки ревью: повтор bot-reply — номер сообщения Telegram на тот же вопрос; asks — свежие."""

from api import test_hub_sessions as base
from api.test_f56_queue import _agent, _bot
from api.test_f61_agent_mail_bot import API, _asks, _reply, _rows, _say  # noqa: F401

procs = base.procs
ui = base.ui
world = base.world


async def test_same_tg_id_other_question_is_new_reply(client, db, clock):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    q1 = await _say(client, {"text": "логин?", "ask": True}, token=agent)
    first = await _reply(client, bot, q1["id"], "старый ответ", tg=5)
    clock.advance_ms(1000)
    q2 = await _say(client, {"text": "код из письма?", "ask": True}, token=agent)
    second = await _reply(client, bot, q2["id"], "123456", tg=5)
    # Ожидание: новая реплика владельца к q2 (или отказ). Факт: возвращена старая строка к q1.
    assert second["reply_to"] == q2["id"], (second, first)


async def test_newest_open_question_listed_after_200(client, db, clock):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    for i in range(200):
        await _say(client, {"text": f"старый {i}", "ask": True, "ask_ttl_s": 60}, token=agent)
    clock.advance_ms(61_000)
    fresh = await _say(client, {"text": "новый вопрос", "ask": True}, token=agent)
    ids = [a["id"] for a in await _asks(client, bot)]
    assert fresh["id"] in ids, (len(ids), ids[-1], fresh["id"])
