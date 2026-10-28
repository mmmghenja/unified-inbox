"""Тест 9 куска 1b: перечитывание окна (правки/удаления, пропущенные во сне). Стенд — настоящее ядро в
процессе (`admin_client`/`internal_transport` из `conftest.py`), эмиссия событий коннектора перехватывается
и прогоняется через `push_events`, как это делает буфер `ConnectorBase.run()` в проде (здесь — вручную, тест
гоняет `sync()` напрямую).
"""

from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway

# Совпадает с умолчанием `helpers.make_dialog(last_message_date_ms=...)` — без фиксированных часов активность
# диалога (2 ч/30 дней от РЕАЛЬНОГО текущего времени теста) не признавалась бы «недавней» никогда.
_FAKE_NOW_S = 1_700_000_000.5


async def _wired_connector(admin_client, internal_transport):
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = FakeGateway()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = gw.me.id  # noqa: SLF001

    collected: list[dict] = []
    connector.emit = lambda ev: collected.append(ev)  # noqa: SLF001 — перехват вместо буфера run()
    return connector, gw, client, collected


async def _seed_known_message(client, *, channel_ext="7", title="Пётр", ext_id="1", text="старый текст"):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": "dm", "title": title},
        "ext_id": ext_id,
        "ts": 1000,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    r = await client.push_events([event])
    assert r["applied"] == 1


async def test_1b_09_edit_during_sleep_is_visible_after_window_reread(admin_client, internal_transport, db):
    connector, gw, client, collected = await _wired_connector(admin_client, internal_transport)
    other = h.make_user(7, first_name="Пётр")
    await _seed_known_message(client, text="старый текст")

    # top_message == last (никаких «новостей» с точки зрения heads) — правка того же ext_id="1" видна ТОЛЬКО
    # через перечитывание окна.
    gw.add_dialog(h.make_dialog(other, top_message=1, last_message_date_ms=1_700_000_000_000))
    gw.add_message(7, h.make_message(1, h.peer_of(other), message="ПРАВКА", from_id=None))

    with db.reader() as conn:
        seq_before = conn.execute("SELECT seq FROM main.message WHERE ext_id = '1'").fetchone()[0]

    await connector.sync("start")
    if collected:
        await client.push_events(collected)

    with db.reader() as conn:
        text, seq_after = conn.execute("SELECT text, seq FROM main.message WHERE ext_id = '1'").fetchone()
    assert text == "ПРАВКА"
    assert seq_after > seq_before


async def test_1b_09_delete_during_sleep_is_detected(admin_client, internal_transport, db):
    connector, gw, client, collected = await _wired_connector(admin_client, internal_transport)
    other = h.make_user(7, first_name="Пётр")
    await _seed_known_message(client, ext_id="1", text="будет удалено")

    # Диалог активен (last_message_date свежий), но история у источника для ext_id="1" ПУСТА — сообщение
    # удалено, пока хаб спал; топ диалога по-прежнему "1" (без новостей), окно должно поймать удаление.
    gw.add_dialog(h.make_dialog(other, top_message=1, last_message_date_ms=1_700_000_000_000))
    # ничего не добавляем в gw.history[7] — источник не отдаёт ext_id="1" вовсе

    await connector.sync("start")
    if collected:
        await client.push_events(collected)

    with db.reader() as conn:
        deleted = conn.execute("SELECT deleted FROM main.message WHERE ext_id = '1'").fetchone()[0]
    assert deleted == 1


async def test_1b_09_unchanged_window_produces_no_new_seq(admin_client, internal_transport, db):
    """Главный тест против шума: окно, идентичное источнику, не должно давать НИ ОДНОГО нового seq (иначе
    сверка раз в 10 мин шумела бы правками в ленту, спека)."""
    connector, gw, client, collected = await _wired_connector(admin_client, internal_transport)
    other = h.make_user(7, first_name="Пётр")
    await _seed_known_message(client, ext_id="1", text="неизменный текст")

    gw.add_dialog(h.make_dialog(other, top_message=1, last_message_date_ms=1_700_000_000_000))
    gw.add_message(7, h.make_message(1, h.peer_of(other), message="неизменный текст", from_id=None))

    # Первая сверка ставит attrs (папки/tg_type) ВПЕРВЫЕ — это законный рост seq, не шум. «Против шума»
    # проверяем на ВТОРОЙ сверке, когда attrs уже стабильны и меняться им больше не с чего.
    await connector.sync("start")
    if collected:
        await client.push_events(collected)
    collected.clear()

    with db.reader() as conn:
        max_seq_before = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]

    await connector.sync("scheduled")
    if collected:
        r = await client.push_events(collected)
        assert r["applied"] == 0  # всё no-op: upsert идентичен, attrs/account_meta не изменились

    with db.reader() as conn:
        max_seq_after = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    assert max_seq_after == max_seq_before
