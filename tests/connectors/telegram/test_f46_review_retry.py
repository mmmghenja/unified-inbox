"""Ф46-1 (ревью): первая попытка автору-min доставлена, ответ потерян; к повтору сообщение группы удалено.
Адресата уже не разрешить — исход неясен (`unknown`), а не «не ушло» (`permanent`)."""
from connectors.telegram import test_tg_msg_author as t


async def test_delivered_then_group_message_deleted_reports_permanent(tmp_path):
    connector, gw, _core = t._setup(tmp_path)
    gw.add_message_author(t.GROUP_MARKED, t.MSG_ID, t._author(min=True))
    gw.queue_send_faults("network_after")
    first = await connector.send(t._lease())
    assert first.outcome == "abandon"
    assert len(gw.sent) == 1  # у человека сообщение есть
    gw.message_authors.clear()  # сообщение группы удалили (или нас убрали из группы)
    second = await connector.send(t._lease(maybe_sent=True, attempts=2))
    assert second.outcome == "unknown", second
