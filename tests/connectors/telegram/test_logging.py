"""Тест 14 куска 1b: логи сверки и приёма (caplog) не содержат текстов сообщений и названий чатов. Секретный
маркер — в тексте сообщения И в названии канала/отправителя; после `sync()` (включая догон, Ф42-1) и живых
апдейтов маркер не должен встретиться ни в одной строке лога ни одного логгера пакета `hub.connectors`.
"""

import logging

from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import NewMessageGE

from . import helpers as h
from .helpers import FakeGateway

_SECRET_TEXT = "СЕКРЕТНЫЙ-ТЕКСТ-СООБЩЕНИЯ-МАРКЕР"
_SECRET_TITLE = "СЕКРЕТНОЕ-НАЗВАНИЕ-ЧАТА-МАРКЕР"


class _Client:
    def __init__(self):
        self.statuses = []

    async def get_cursor(self):  # Ф42-1: курсор аккаунта (дыры догона) — пусто
        return None

    async def push_events(self, events, cursor=None):
        return {"ok": True}

    async def channels_heads(self):
        return []

    async def channels_window(self, *, ext_id, limit=30):
        return []

    async def set_activity(self, *, backfill):
        return {"ok": True}

    async def set_status(self, status, *, last_error=None):
        self.statuses.append((status, last_error))
        return {"ok": True}


def _assert_no_leak(caplog) -> None:
    assert _SECRET_TEXT not in caplog.text
    assert _SECRET_TITLE not in caplog.text
    for record in caplog.records:
        assert _SECRET_TEXT not in record.getMessage()
        assert _SECRET_TITLE not in record.getMessage()


async def test_1b_14_sync_logs_do_not_contain_message_text_or_channel_title(caplog):
    other = h.make_user(7, first_name=_SECRET_TITLE)
    gw = FakeGateway()
    gw.add_dialog(h.make_dialog(other, top_message=600, last_message_date_ms=1_700_000_000_000))
    for i in range(1, 601):
        gw.add_message(7, h.make_message(i, h.peer_of(other), message=_SECRET_TEXT, from_id=None))

    client = _Client()

    async def _heads():
        return [
            {
                "ext_id": "7",
                "kind": "dm",
                "parent_ext_id": None,
                "own_id_space": False,
                "max_ext_sort": 1,
                "last_msg_ts": 0,
                "archived": False,
            }
        ]

    client.channels_heads = _heads

    connector = TelegramConnector(client, gw, account_id=1, wall=lambda: 1_700_000_000.5)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector.emit = lambda ev: None  # noqa: SLF001 — интересует только журнал, не события

    async def _no_sleep(_seconds):
        return None

    connector._sleep = _no_sleep  # noqa: SLF001 — паузы между страницами догона

    with caplog.at_level(logging.DEBUG, logger="hub"):
        await connector.sync("start")  # 599 пропущенных -> догон страницами (Ф42-1), строка «догон» в журнале

    assert any("догон chat=7" in r.getMessage() for r in caplog.records)  # ветка правда сработала
    _assert_no_leak(caplog)


async def test_1b_14_live_update_logs_do_not_contain_message_text(caplog):
    other = h.make_user(7, first_name=_SECRET_TITLE)
    gw = FakeGateway()
    client = _Client()
    connector = TelegramConnector(client, gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector._entities[int(h.peer_of(other).user_id)] = other  # noqa: SLF001
    connector.emit = lambda ev: None  # noqa: SLF001

    msg = h.make_message(1, h.peer_of(other), message=_SECRET_TEXT, from_id=None)
    with caplog.at_level(logging.DEBUG, logger="hub"):
        await connector._handle_gateway_event(NewMessageGE(message=msg))  # noqa: SLF001
        # Неизвестный диалог (нет в `_entities`) — тоже частый лог-путь, тоже без утечки.
        ghost = h.make_user(999, first_name=_SECRET_TITLE)
        ghost_msg = h.make_message(2, h.peer_of(ghost), message=_SECRET_TEXT, from_id=None)
        await connector._handle_gateway_event(NewMessageGE(message=ghost_msg))  # noqa: SLF001

    _assert_no_leak(caplog)
