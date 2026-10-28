"""Ф63-2 П2, П3 сквозь outbox, `ConnectorBase` и `TelegramConnector` с `FakeGateway`: первое сообщение боту
по @нику ставит новой беседе `attrs.tg_type = "bot"`; ник на «bot» у человека — `failed` «не бот»,
отправки нет."""

import pytest
from connectors.telegram.helpers import make_user

from .conftest import get_outbox
from .test_new_recipient_tg import _channel, _draft_and_approve, _NoPause, _stand


@pytest.fixture(autouse=True)
def _no_first_pause(monkeypatch):
    """Первая строка графика — без случайной паузы (24.10), как в `test_new_recipient_tg.py`."""
    monkeypatch.setattr("hub.core.outbox._rng", _NoPause())


async def test_bot_gets_tg_type_on_bind(admin_client, public_client, internal_transport, db, clock):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    st["gateway"].add_username(
        "helper_bot", make_user(556, first_name="Бот", username="helper_bot", bot=True)
    )
    body = await _draft_and_approve(public_client, st, to="@helper_bot", text="/start")
    assert await st["connector"].step_outbox() == 1
    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "sent"
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"] == "556" and ch["meta"]["attrs"]["tg_type"] == "bot"


async def test_person_with_bot_nick_is_not_sent(admin_client, public_client, internal_transport, db, clock):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    st["gateway"].add_username("fake_bot", make_user(557, first_name="Пётр", username="fake_bot"))
    body = await _draft_and_approve(public_client, st, to="@fake_bot", text="/start")
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "failed" and "не бот" in (ob.get("error") or ob.get("last_error") or "")
    assert st["gateway"].send_calls == 0
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"].startswith("new:") and "tg_type" not in (ch["meta"].get("attrs") or {})
