"""Ф63-2, коннектор Telegram (`docs/architecture.md`): первое сообщение по @нику —
`sent` несёт `peer_bot`, когда адресат — бот; ник на «bot», разрешившийся в человека, — отправки нет,
`permanent` «@… — не бот», на каждой попытке (и после `maybe_sent`). Сети нет."""

from __future__ import annotations

import pytest

from .helpers import make_user
from .test_new_chat_files import _lease, _setup

BOT_ID = 4242
USER_ID = 555


def _to(username: str) -> dict:
    return {"kind": "tg_username", "username": username}


async def test_bot_nick_sends_with_peer_bot(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.add_username("x_bot", make_user(BOT_ID, first_name="Икс", username="x_bot", bot=True))
    result = await connector.send(_lease(_to("x_bot"), text="/start"))
    assert (result.outcome, result.peer_ext_id, result.peer_bot) == ("sent", str(BOT_ID), True)
    assert gw.sent[0]["text"] == "/start"


@pytest.mark.parametrize("maybe_sent", [False, True])
@pytest.mark.parametrize("nick", ["fake_bot", "Fake_BOT"])
async def test_bot_nick_of_a_person_is_permanent_without_send(tmp_path, maybe_sent, nick) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.add_username(nick, make_user(USER_ID, first_name="Пётр", username=nick))
    result = await connector.send(_lease(_to(nick), text="/start", maybe_sent=maybe_sent))
    assert (result.outcome, result.error) == ("permanent", f"@{nick} — не бот")
    assert gw.sent == [] and gw.send_calls == 0


async def test_person_nick_has_no_peer_bot(tmp_path) -> None:
    """Обычный ник человека — прежний путь, признака бота нет."""
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.add_username("petr_i", make_user(USER_ID, first_name="Пётр", username="petr_i"))
    result = await connector.send(_lease(_to("petr_i")))
    assert (result.outcome, result.peer_ext_id, result.peer_bot) == ("sent", str(USER_ID), None)
