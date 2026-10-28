"""Сквозные сценарии куска 1c фазы 1: отправка в Telegram, лимитер, флуд, `limited`, алерт о новой сессии.
Спецификация: `docs/architecture.md` (13 обязательных сценариев).

Публичный API -> outbox -> `TelegramConnector` c `FakeGateway`, который ведёт «сервер Telegram» (дедуп по
`random_id`, счётчик доставок в `gateway.sent`/`gateway.send_calls`) — тот же стенд (`db`/`clock`/`notifier`/
`public_app`/`internal_app`/`admin_client`/`public_client`), что `tests/e2e/test_scenarios.py` (кусок 5b), и
помощники оттуда (`.conftest`). Живого Telegram нет: `FakeGateway` — целиком в памяти, без сетевых вызовов.
"""

import asyncio
import json
import logging

import pytest
from connectors.telegram.helpers import (
    FakeGateway,
    make_authorization,
    make_channel,
    make_chat,
    make_dialog,
    make_user,
    peer_of,
)
from telethon import errors

from hub.connectors.base import CoreClient, SendResult
from hub.connectors.fake import ConnectorCrash, FakeConnector, FakeSource
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.convert import peer_ext_id
from hub.core.db import next_seq
from hub.watch import evaluate as watch_evaluate

from .conftest import (
    create_account,
    create_project,
    create_token,
    csrf_headers,
    get_outbox,
    insert_channel,
    login_via_code,
    send_bearer,
    send_ui,
    visible_rows,
)

DAY_MS = 24 * 60 * 60 * 1000


def _advancing_sleep(clock):
    """`ConnectorBase.sleep=` для лимитера (тест 9б): ожидание `_acquire_send_slot()` должно двигать те же
    часы, что видит `self._wall()` — иначе «подождал ≥ 1 с» никогда не станет правдой на `FakeClock`."""

    async def _sleep(seconds: float) -> None:
        import asyncio

        clock.advance_ms(int(seconds * 1000) + 1)
        await asyncio.sleep(0)

    return _sleep


async def create_tg_account(admin_client) -> tuple[int, str]:
    return await create_account(
        admin_client, source="telegram", title="Личный", config={"idempotent_send": True}
    )


def make_tg_connector(
    internal_transport, *, secret: str, gateway: FakeGateway, account_id: int, clock, state_path=None
) -> TelegramConnector:
    client = CoreClient(secret=secret, transport=internal_transport)
    return TelegramConnector(
        client,
        gateway,
        account_id=account_id,
        state_path=state_path,
        wall=lambda: clock.value,
        sleep=_advancing_sleep(clock),
    )


def channel_row(db, *, account_id, project_id, ext_id, kind="dm", parent_channel_id=None, now_ms=0):
    """Канал, готовый принимать outbox (доверенный — `my_msg_count>0`, старше 14 суток, как `setup_channel`
    в `.conftest`; здесь — своя версия с контролем `kind`/`parent_channel_id` под топики/группы Telegram."""
    return insert_channel(
        db,
        account_id,
        project_id=project_id,
        ext_id=ext_id,
        kind=kind,
        parent_channel_id=parent_channel_id,
        my_msg_count=5,
        first_seen_ts=now_ms - 20 * DAY_MS,
    )


def seed_dm(gateway: FakeGateway, *, user_id: int = 555, top_message: int = 50) -> str:
    user = make_user(user_id, first_name="Собеседник")
    gateway.add_dialog(make_dialog(user, top_message=top_message))
    return peer_ext_id(peer_of(user))


def seed_group(gateway: FakeGateway, *, chat_id: int = 777, top_message: int = 50) -> str:
    chat = make_chat(chat_id, title="Группа")
    gateway.add_dialog(make_dialog(chat, top_message=top_message))
    return peer_ext_id(peer_of(chat))


def seed_forum(gateway: FakeGateway, *, channel_id: int = 1001, top_message: int = 50) -> str:
    forum = make_channel(channel_id, title="Форум", megagroup=True, forum=True)
    gateway.add_dialog(make_dialog(forum, top_message=top_message))
    return peer_ext_id(peer_of(forum))


# --- Тест 1: личка/группа/топик/General/ответ в топике — peer, reply_to, top_msg_id, ext_id из обоих видов -


async def test_1c_01a_dm_ext_id_from_update_short_sent_message(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "привет в личку"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.send_calls == 1
    assert gateway.sent[0]["marked_id"] == int(chat_ext)  # положительный marked id -> личка
    assert gateway.sent[0]["reply_to"] is None

    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"
    assert body["ext_id"] == gateway.sent[0]["ext_id"]
    rows = [m for m in await visible_rows(public_client, channel_id) if m["text"] == text]
    assert len(rows) == 1


async def test_1c_01b_group_ext_id_from_updates_update_message_id(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_group(gateway)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="group")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "привет в группу"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.sent[0]["marked_id"] == int(chat_ext)  # отрицательный marked id -> группа
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"
    assert body["ext_id"] == gateway.sent[0]["ext_id"]


async def test_1c_01c_topic_reply_sets_reply_to_msg_id_and_top_msg_id(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_forum(gateway)
    topic_id = 5
    forum_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="group")
    topic_channel_id = channel_row(
        db,
        account_id=account_id,
        project_id=project_id,
        ext_id=f"{chat_ext}:{topic_id}",
        kind="topic",
        parent_channel_id=forum_id,
    )
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    r = await public_client.post(
        "/api/v1/messages/send",
        json={
            "channel_id": topic_channel_id,
            "text": "ответ в теме",
            "reply_to": "42",
            "idempotency_key": "k-topic-reply",
        },
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text

    processed = await connector.step_outbox()
    assert processed == 1

    reply_to = gateway.sent[0]["reply_to"]
    assert reply_to.reply_to_msg_id == 42
    assert reply_to.top_msg_id == topic_id


async def test_1c_01d_topic_post_without_reply_uses_topic_id_no_top_msg_id(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_forum(gateway)
    topic_id = 7
    forum_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="group")
    topic_channel_id = channel_row(
        db,
        account_id=account_id,
        project_id=project_id,
        ext_id=f"{chat_ext}:{topic_id}",
        kind="topic",
        parent_channel_id=forum_id,
    )
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    await send_ui(public_client, cookie, channel_id=topic_channel_id, text="пост в теме")
    await connector.step_outbox()

    reply_to = gateway.sent[0]["reply_to"]
    # F6 критика 1c: по документации inputReplyToMessage top_msg_id — ТОЛЬКО у настоящего ответа
    # (reply_to_msg_id != topic_id); пост «в тему» без ответа несёт только reply_to_msg_id=topic_id.
    assert reply_to.reply_to_msg_id == topic_id
    assert reply_to.top_msg_id is None


async def test_1c_01e_general_topic_without_reply_sends_no_reply_to(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_forum(gateway)
    forum_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="group")
    general_channel_id = channel_row(
        db,
        account_id=account_id,
        project_id=project_id,
        ext_id=f"{chat_ext}:1",
        kind="topic",
        parent_channel_id=forum_id,
    )
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    await send_ui(public_client, cookie, channel_id=general_channel_id, text="в General")
    await connector.step_outbox()

    assert gateway.sent[0]["reply_to"] is None


async def test_1c_01f_peer_cache_miss_then_iter_dialogs_warms_and_retries(
    admin_client, public_client, internal_transport, db, clock
):
    """Промах `get_input_entity` -> один проход `iter_dialogs()` наполняет кэш -> повтор находит peer."""
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway)
    gateway.forget_peer(int(chat_ext))  # «кэш сессии ещё не знает» — только процесс sync() знал бы
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="после прогрева")
    processed = await connector.step_outbox()

    assert processed == 1
    assert gateway.iter_dialogs_calls == 1
    assert gateway.send_calls == 1
    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "sent"


async def test_1c_01g_peer_still_unresolved_after_warm_is_temporary_no_request(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()  # ни одного диалога вовсе -> кэш никогда не наполнится
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id="99999999", kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="в никуда")
    processed = await connector.step_outbox()

    assert processed == 1
    assert gateway.send_calls == 0  # без запроса
    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "queued"  # temporary -> назад в очередь с backoff


# --- Тест 2: random_id одинаков на всех попытках одной строки и разный у разных строк ----------------------


async def test_1c_02_random_id_stable_per_row_distinct_across_rows(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=601)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    await send_ui(public_client, cookie, channel_id=channel_id, text="строка A", idempotency_key="row-a")
    await connector.step_outbox()
    random_id_a = gateway.sent[0]["random_id"]

    await send_ui(public_client, cookie, channel_id=channel_id, text="строка B", idempotency_key="row-b")
    await connector.step_outbox()
    random_id_b = gateway.sent[1]["random_id"]

    assert random_id_a != random_id_b
    # «Одинаков на всех попытках одной строки» проверяется структурно тестами 3/4 ниже: FakeGateway дедупит
    # по random_id только при РЕАЛЬНО совпавшем значении — успешный поиск дубля там доказывает стабильность.


# --- Тест 3: падение после доставки, до report -> лиз истёк -> повтор нашёл в истории -> sent, доставок 1 --


async def test_1c_03_crash_after_delivery_found_in_history(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=602)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "упаду после доставки"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    gateway.queue_send_faults("crash_after_delivery")
    with pytest.raises(ConnectorCrash):
        await connector.step_outbox()

    assert len(gateway.sent) == 1  # сервер получил
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sending"  # report не ушёл — процесс «умер»

    clock.advance_ms(130_000)  # лиз Telegram — 120 с
    gateway.now_ms = int(clock.value * 1000)
    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.send_calls == 1  # второй запрос отправки НЕ ушёл — нашли в истории
    assert len(gateway.sent) == 1  # доставок всего 1

    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"
    rows = [m for m in await visible_rows(public_client, channel_id) if m["text"] == text]
    assert len(rows) == 1


# --- Тест 4: история «не видит» -> повтор с тем же random_id -> сервер отвечает дубликатом -> поиск --------


async def test_1c_04a_duplicate_after_blind_history_widens_search_and_finds(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=603)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "дубликат с широким поиском"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    gateway.queue_send_faults("crash_after_delivery")
    with pytest.raises(ConnectorCrash):
        await connector.step_outbox()
    assert len(gateway.sent) == 1

    clock.advance_ms(130_000)
    gateway.now_ms = int(clock.value * 1000)
    # «История не видит» на обычном (узком, limit=50) поиске — только широкий (limit=100, после дубликата)
    # находит; естественное поведение MTProto (RandomIdDuplicateError) отправку всё равно не повторяет.
    gateway.get_messages_min_limit_visible = 100
    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.send_calls == 2  # вторая попытка реально ушла (узкий поиск не нашёл) и словила дубликат
    assert len(gateway.sent) == 1  # но доставка всё равно ровно одна

    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"


async def test_1c_04b_duplicate_and_history_never_finds_stays_unknown(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=604)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "дубликат без истории"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    gateway.queue_send_faults("crash_after_delivery")
    with pytest.raises(ConnectorCrash):
        await connector.step_outbox()
    assert len(gateway.sent) == 1

    clock.advance_ms(130_000)
    gateway.now_ms = int(clock.value * 1000)
    gateway.get_messages_min_limit_visible = 10_000  # история недоступна ни на узком, ни на широком поиске
    processed = await connector.step_outbox()
    assert processed == 1

    assert len(gateway.sent) == 1  # доставка одна — не повторяем вслепую (ушло точно)
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "unknown"


# --- Тест 5: сетевая ошибка посреди запроса -> abandon -> после лиза доставлено 1 раз в обоих случаях ------


async def test_1c_05a_network_error_before_delivery_abandon_then_delivers_once(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=605)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "сеть оборвалась до отправки"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    gateway.queue_send_faults("network_before")
    processed = await connector.step_outbox()  # abandon — не бросает, report просто не уходит
    assert processed == 1
    assert len(gateway.sent) == 0  # сервер не получил

    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sending"  # лиз висит

    clock.advance_ms(130_000)
    gateway.now_ms = int(clock.value * 1000)
    processed = await connector.step_outbox()
    assert processed == 1

    assert len(gateway.sent) == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"


async def test_1c_05b_network_error_after_delivery_abandon_then_delivers_once(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=606)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "сеть оборвалась после отправки"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    gateway.queue_send_faults("network_after")
    processed = await connector.step_outbox()
    assert processed == 1
    assert len(gateway.sent) == 1  # сервер получил, только ответ потерян

    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sending"

    clock.advance_ms(130_000)
    gateway.now_ms = int(clock.value * 1000)
    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.send_calls == 1  # повтор нашёл в истории, второй запрос не ушёл
    assert len(gateway.sent) == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"


# --- Тест 6: FloodWait -> flood_until аккаунта (и другая строка ждёт); SlowModeWait -> только свой чат ------


async def test_1c_06a_flood_wait_sets_account_flood_until_and_blocks_other_channel(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_a = seed_dm(gateway, user_id=701)
    chat_b = seed_dm(gateway, user_id=702)
    channel_a = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_a, kind="dm")
    channel_b = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_b, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result_a = await send_ui(public_client, cookie, channel_id=channel_a, text="A")
    result_b = await send_ui(public_client, cookie, channel_id=channel_b, text="B")

    gateway.queue_send_faults(errors.FloodWaitError(request=None, capture=30))
    processed = await connector.step_outbox()
    # `step_outbox()` считает только лизы, реально прошедшие через `_process_lease()` (раздел 4 плана,
    # находка 17 критика 1a): остаток пачки после `flood` репортится НАПРЯМУЮ, в счётчик не попадает.
    assert processed == 1

    assert len(gateway.sent) == 0
    assert gateway.send_calls == 1  # для B send_message вообще не звался

    body_a = await get_outbox(public_client, result_a["outbox_id"])
    body_b = await get_outbox(public_client, result_b["outbox_id"])
    assert body_a["status"] == "queued"
    assert body_b["status"] == "queued"

    with db.reader() as conn:
        flood_until = conn.execute(
            "SELECT flood_until FROM main.account WHERE id = ?", (account_id,)
        ).fetchone()[0]
    assert flood_until == int(clock.value * 1000) + 30_000


async def test_1c_06b_slow_mode_wait_blocks_only_its_own_chat(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_a = seed_dm(gateway, user_id=703)
    chat_b = seed_dm(gateway, user_id=704)
    channel_a = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_a, kind="dm")
    channel_b = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_b, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result_a = await send_ui(public_client, cookie, channel_id=channel_a, text="A")
    result_b = await send_ui(public_client, cookie, channel_id=channel_b, text="B")

    gateway.queue_send_faults(errors.SlowModeWaitError(request=None, capture=15))
    processed = await connector.step_outbox()
    assert processed == 2

    assert len(gateway.sent) == 1  # B ушло нормально — SlowMode не останавливает пачку
    body_a = await get_outbox(public_client, result_a["outbox_id"])
    body_b = await get_outbox(public_client, result_b["outbox_id"])
    assert body_a["status"] == "queued"
    assert body_b["status"] == "sent"

    with db.reader() as conn:
        flood_until = conn.execute(
            "SELECT flood_until FROM main.account WHERE id = ?", (account_id,)
        ).fetchone()[0]
    assert flood_until is None  # аккаунт целиком не тронут


async def test_1c_06c_five_floods_in_a_row_do_not_fail_the_row(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=705)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="пять флудов подряд")

    for _ in range(5):
        gateway.queue_send_faults(errors.FloodWaitError(request=None, capture=5))
        await connector.step_outbox()
        clock.advance_ms(6_000)
        gateway.now_ms = int(clock.value * 1000)

    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "queued"  # НЕ failed

    processed = await connector.step_outbox()  # шестая попытка — без faults — проходит
    assert processed == 1
    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "sent"


# --- Тест 7: PeerFlood -> limited, ни одного запроса; hub account resume -> отправка пошла -----------------
# Ф69-1 (П3, П4): весь аккаунт встаёт, только если человек уже писал (иначе — стоп новых людей,
# `tests/e2e/test_f69_peer_flood.py`); поймавшая PeerFlood строка — `failed` и сама больше не повторяется.


async def test_1c_07_peer_flood_limits_account_then_resume_unblocks(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=706)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    _inbound_message(db, account_id, channel_id)  # Ф69-1: собеседник уже писал — не «новый человек»
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="peer flood")

    gateway.queue_send_faults(errors.PeerFloodError(request=None))
    await connector.step_outbox()

    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "failed"  # Ф69-1: петли 16.10 нет — строка не повторяется сама
    with db.reader() as conn:
        status = conn.execute("SELECT status FROM main.account WHERE id = ?", (account_id,)).fetchone()[0]
    assert status == "limited"

    # Дальше — ни одного запроса отправки, сколько ни пытайся (core не выдаёт лизы аккаунту limited).
    later = await send_ui(public_client, cookie, channel_id=channel_id, text="после ограничения")
    for _ in range(3):
        processed = await connector.step_outbox()
        assert processed == 0
    assert gateway.send_calls == 1

    r = await admin_client.post(f"/internal/v1/admin/accounts/{account_id}/resume")
    assert r.status_code == 200, r.text

    gateway.now_ms = int(clock.value * 1000)
    processed = await connector.step_outbox()
    assert processed == 1
    assert gateway.send_calls == 2
    assert len(gateway.sent) == 1
    assert (await get_outbox(public_client, later["outbox_id"]))["status"] == "sent"
    assert (await get_outbox(public_client, result["outbox_id"]))["status"] == "failed"


def _inbound_message(db, account_id, channel_id) -> None:
    def _fn(conn):
        seq = next_seq(conn)
        conn.execute(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ts, is_me, kind, "
            "text) VALUES (?, ?, ?, ?, 'in1', 1, 0, 'text', 'привет')",
            (seq, seq, account_id, channel_id),
        )

    db.write_sync(_fn)


# --- Тест 8: постоянные ошибки -> failed с именем; AuthKeyUnregistered -> auth_required, код 78 ------------


async def test_1c_08a_permanent_error_fails_with_class_name(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=707)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="навсегда провалится")

    gateway.queue_send_faults(errors.ChannelPrivateError(request=None))
    processed = await connector.step_outbox()
    assert processed == 1

    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "failed"
    assert body["error"] == "ChannelPrivateError"
    assert len(gateway.sent) == 0


async def test_1c_08b_auth_key_unregistered_sets_auth_required_and_exit_78(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=708)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    await send_ui(public_client, cookie, channel_id=channel_id, text="сессия отозвана")

    gateway.queue_send_faults(errors.AuthKeyUnregisteredError(request=None))
    await connector.step_outbox()

    assert connector.auth_required is True
    assert connector._exit_code == 78  # noqa: SLF001 — тот же приём, что test_lifecycle.py
    with db.reader() as conn:
        status = conn.execute("SELECT status FROM main.account WHERE id = ?", (account_id,)).fetchone()[0]
    assert status == "auth_required"


# --- Тест 9: лимитер — 21-я отправка за минуту temporary без запроса; 1/сек на чат (часы подменены) ---------


async def test_1c_09a_account_window_21st_send_is_deferred_without_request(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=709)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    now_ms = int(clock.value * 1000)
    connector._send_times.extend([now_ms] * 20)  # noqa: SLF001 — 20 «уже отправленных» в текущем окне

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="21-я за минуту")
    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.send_calls == 0  # без запроса
    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "queued"


async def test_1c_acc_account_window_deferrals_never_fail_the_row(
    admin_client, public_client, internal_transport, db, clock
):
    """Приёмка 1c: окно аккаунта (20/мин) откладывает строку исходом `flood` — попытки не тратятся. С
    `temporary` пачка из 25+ сообщений после пяти отсрочек уходила в `failed` без единой ошибки."""
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=711)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="после окна")

    for _ in range(8):  # больше, чем _MAX_ATTEMPTS: окно заполнено на каждом заходе
        gateway.now_ms = int(clock.value * 1000)
        connector._send_times.clear()  # noqa: SLF001
        connector._send_times.extend([int(clock.value * 1000)] * 20)  # noqa: SLF001
        await connector.step_outbox()
        body = await get_outbox(public_client, result["outbox_id"])
        assert body["status"] == "queued"
        clock.value += 61  # отсрочка истекла, строка снова выдаваема

    connector._send_times.clear()  # noqa: SLF001
    gateway.now_ms = int(clock.value * 1000)
    await connector.step_outbox()
    assert len(gateway.sent) == 1
    assert (await get_outbox(public_client, result["outbox_id"]))["status"] == "sent"


async def test_1c_acc_send_timeout_of_idempotent_connector_is_abandon_not_unknown(monkeypatch):
    """Приёмка 1c: таймаут каркаса на send() у идемпотентного коннектора — `abandon` (лиз истечёт, повтор
    безопасен), а не `unknown` с решением за человеком. У неидемпотентного — по-прежнему `unknown`."""
    import asyncio

    from hub.connectors import base

    monkeypatch.setattr(base, "_SEND_TIMEOUT_S", 0.01)

    class _Client:
        def __init__(self):
            self.reports = []

        async def report(self, **kw):
            self.reports.append(kw)
            return {}

    class _Conn(base.ConnectorBase):
        idempotent = True

        async def sync(self, reason="scheduled"):
            return None

        async def send(self, lease):
            await asyncio.Event().wait()

        async def reconcile(self, lease):
            return base.SendResult("unknown")

        async def health(self):
            return None

    lease = {"mode": "send", "outbox_id": 1, "lease_gen": 1}
    client = _Client()
    result = await _Conn(client)._process_lease(lease)  # noqa: SLF001
    assert result.outcome == "abandon"
    assert client.reports == []

    _Conn.idempotent = False
    client = _Client()
    result = await _Conn(client)._process_lease(lease)  # noqa: SLF001
    assert [r["outcome"] for r in client.reports] == ["unknown"]


async def test_1c_09b_chat_gap_at_least_one_second_between_two_sends(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=710)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    await send_ui(public_client, cookie, channel_id=channel_id, text="первая")
    await connector.step_outbox()
    assert len(gateway.sent) == 1

    clock_before = clock.value
    await send_ui(public_client, cookie, channel_id=channel_id, text="вторая")
    gateway.now_ms = int(clock.value * 1000)
    processed = await connector.step_outbox()
    assert processed == 1

    assert len(gateway.sent) == 2
    assert (clock.value - clock_before) >= 1.0  # лимитер подождал ≥ 1 с (инжектированный sleep двигал часы)


# --- Тест 10: эхо — sent + перечитывание окна доводит текст (обрезка пробелов), одна строка -----------------


async def test_1c_10_echo_via_window_reread_updates_text_single_row(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    user = make_user(711, first_name="Собеседник")
    gateway.add_dialog(make_dialog(user, top_message=1, last_message_date_ms=int(clock.value * 1000)))
    chat_ext = peer_ext_id(peer_of(user))
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    # `_reread_window()` (сверка) нуждается в `self._me_id`/подписке — как в реальной жизни, после
    # `start_source()`, а не голым `sync()` без старта.
    await connector.start_source()
    try:
        cookie, _csrf = await login_via_code(public_client, db, clock)
        raw_text = "  привет с обрезкой  "
        stripped = raw_text.strip()
        result = await send_ui(public_client, cookie, channel_id=channel_id, text=raw_text)
        await connector.step_outbox()
        assert len(gateway.sent) == 1

        # Telegram сам обрезает пробелы по краям — правим сохранённую «сырую» историю так же (реалистично
        # для get_messages()/upsert куска 1b, не только для дедупа отправки).
        delivered_ext_id = int(gateway.sent[0]["ext_id"])
        for msg in gateway.history[int(chat_ext)]:
            if msg.id == delivered_ext_id:
                msg.message = stripped

        with db.reader() as conn:
            placeholder_id, seq_before = conn.execute(
                "SELECT id, seq FROM main.message WHERE outbox_id = ?", (result["outbox_id"],)
            ).fetchone()

        # `emit()` кладёт события в буфер `run()` (толкатель — отдельная фоновая задача, здесь не запущена,
        # `sync()` вызван напрямую); перехватываем и проталкиваем сами — тот же приём, что `_wired_connector`
        # в тестах куска 1b (`tests/connectors/telegram/test_window.py`), но с реальным `push_events()` в
        # конце — нужен настоящий эффект в БД, а не только список событий.
        collected: list[dict] = []
        connector.emit = collected.append  # noqa: SLF001
        await connector.sync("scheduled")
        if collected:
            r = await connector._client.push_events(collected)  # noqa: SLF001
            assert r["applied"] >= 1, r

        rows = [m for m in await visible_rows(public_client, channel_id) if m["text"] == stripped]
        assert len(rows) == 1
        all_rows = await visible_rows(public_client, channel_id)
        assert len(all_rows) == 1  # НЕ задвоилось

        with db.reader() as conn:
            seq_after = conn.execute(
                "SELECT seq FROM main.message WHERE id = ?", (placeholder_id,)
            ).fetchone()[0]
        assert seq_after > seq_before
    finally:
        await connector.stop_source()


# --- Тест 11: abandon от НЕидемпотентного коннектора -> unknown, ошибка в логе -----------------------------


async def test_1c_11_abandon_from_non_idempotent_connector_becomes_unknown_and_logs_error(
    admin_client, public_client, internal_transport, db, clock, caplog
):
    class _AbandonSendConnector(FakeConnector):
        """`ConnectorBase.idempotent` по умолчанию `False` (не переопределён) — ровно то, что тестируем:
        `abandon` у НЕидемпотентного коннектора каркас превращает в `unknown` (раздел «Отправка» куска 1c)."""

        async def send(self, lease):
            return SendResult("abandon")

    account_id, secret = await create_account(admin_client, source="fake")
    project_id = await create_project(admin_client)
    channel_id = insert_channel(
        db, account_id, project_id=project_id, ext_id="c-abandon", my_msg_count=5, first_seen_ts=0
    )
    client = CoreClient(secret=secret, transport=internal_transport)
    connector = _AbandonSendConnector(client, FakeSource())
    assert connector.idempotent is False

    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="abandon неидемпотентного")

    with caplog.at_level(logging.ERROR, logger="hub.connectors.base"):
        processed = await connector.step_outbox()
    assert processed == 1

    assert any("abandon" in r.message and "unknown" in r.message for r in caplog.records)
    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "unknown"


# --- Тест 12: алерт о новой сессии — первый прогон молчит, новая сессия шумит, повтор без новых — тишина ----


async def test_1c_12_new_session_alert_first_run_silent_then_audit_and_watch_problem(
    admin_client, internal_transport, db, clock, tmp_path
):
    account_id, secret = await create_tg_account(admin_client)
    gateway = FakeGateway()
    gateway.authorized = True
    gateway.authorizations = [
        make_authorization(1, current=True),
        make_authorization(2, current=False, device_model="Old Phone"),
    ]
    state_path = tmp_path / "state.json"
    connector = make_tg_connector(
        internal_transport,
        secret=secret,
        gateway=gateway,
        account_id=account_id,
        clock=clock,
        state_path=str(state_path),
    )

    await connector.start_source()  # «на старте» — первый прогон, должен молчать
    try:
        with db.reader() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM main.audit WHERE action = 'connector_alert'"
            ).fetchone()[0]
        assert count == 0
        assert state_path.exists()

        r = await admin_client.get("/internal/v1/admin/watch")
        assert r.status_code == 200, r.text
        problems = watch_evaluate(r.json(), now_ms=int(clock.value * 1000))
        assert f"account:{account_id}:alert" not in [p.key for p in problems]

        # Появилась НОВАЯ (не текущая) сессия -> алерт; уже известная (hash=2) молчит.
        clock.advance_ms(DAY_MS + 1)
        gateway.authorizations = [
            make_authorization(1, current=True),
            make_authorization(2, current=False, device_model="Old Phone"),
            make_authorization(3, current=False, device_model="Новый ноутбук"),
        ]
        await connector.health()

        with db.reader() as conn:
            rows = conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'connector_alert'"
            ).fetchall()
        assert len(rows) == 1
        import json as json_mod

        detail = json_mod.loads(rows[0][0])
        assert detail["account_id"] == account_id
        assert "ip" not in detail
        assert detail.get("device_model") == "Новый ноутбук"

        r = await admin_client.get("/internal/v1/admin/watch")
        problems = watch_evaluate(r.json(), now_ms=int(clock.value * 1000))
        assert f"account:{account_id}:alert" in [p.key for p in problems]

        # Повтор без новых сессий — тишина (audit не растёт).
        clock.advance_ms(DAY_MS + 1)
        await connector.health()
        with db.reader() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM main.audit WHERE action = 'connector_alert'"
            ).fetchone()[0]
        assert count == 1
    finally:
        await connector.stop_source()


# --- Тест 13: агентский путь — bearer -> pending_approval, ничего не уходит; approve с правкой -> ушёл текст


async def test_1c_13_agent_pending_approval_then_approve_delivers_corrected_text(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=712)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    # Недоверенный канал (my_msg_count=0, свежий) — принудительный pending_approval для bearer.
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET my_msg_count = 0, first_seen_ts = ? WHERE id = ?",
            (int(clock.value * 1000), channel_id),
        )
    )
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)

    token = await create_token(admin_client, kind="interactive", scopes=("read", "send"))
    draft_text = "агентский черновик с опечаткай"
    result = await send_bearer(public_client, token, channel_id=channel_id, text=draft_text)
    outbox_id = result["outbox_id"]
    assert result["status"] == "pending_approval"

    for _ in range(3):
        processed = await connector.step_outbox()
        assert processed == 0
    assert gateway.send_calls == 0

    cookie, _csrf = await login_via_code(public_client, db, clock)
    corrected_text = "агентский черновик с опечаткой"
    r = await public_client.post(
        f"/api/v1/outbox/{outbox_id}/approve",
        json={"text": corrected_text},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text

    processed = await connector.step_outbox()
    assert processed == 1

    assert gateway.delivered_count(draft_text) == 0
    assert gateway.delivered_count(corrected_text) == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"
    rows = [m for m in await visible_rows(public_client, channel_id) if m["text"] == corrected_text]
    assert len(rows) == 1


# ============================================================================================================
# Критик куска 1c (opus, второй круг): 6 находок сверх правок архитектора. Воспроизведения —
# `.runs/critic-1c/test_critic_1c.py` (не в git, не в тестовом дереве); здесь — постоянные регрессионные
# тесты на КАЖДУЮ находку, имена `test_1c_crit_NN_...`, проверяют ИСПРАВЛЕННОЕ поведение.
# ============================================================================================================


# --- F1: maybe_sent (не attempts>1) гейтит поиск в истории; known_own_ext_ids исключает чужие совпадения ----


@pytest.mark.parametrize("fault", ["network_before", "slowmode"])
async def test_1c_crit_01_retry_after_clean_failure_never_steals_earlier_identical_message(
    admin_client, public_client, internal_transport, db, clock, fault
):
    """F1(а)+(б): два сообщения с ОДИНАКОВЫМ текстом «+» подряд. Второе спотыкается (abandon ИЛИ честный
    temporary), повторная попытка НЕ присваивает себе доставку первого — обе уходят по-настоящему, разными
    ext_id, видимых строк 2. До правки: `attempts>1` включал поиск в истории даже после честного temporary
    (запрос точно не уходил), а без `known_own_ext_ids` поиск после abandon находил ЧУЖОЕ «+» и присваивал
    его себе (тихая потеря — второе сообщение никогда не уходило на самом деле)."""
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=901)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    cookie, _csrf = await login_via_code(public_client, db, clock)

    a = await send_ui(public_client, cookie, channel_id=channel_id, text="+")
    b = await send_ui(public_client, cookie, channel_id=channel_id, text="+")

    await connector.step_outbox()  # A уходит нормально
    assert len(gateway.sent) == 1
    gateway.now_ms = int(clock.value * 1000)

    if fault == "network_before":
        gateway.queue_send_faults("network_before")  # сервер B не получил -> abandon, maybe_sent станет True
        await connector.step_outbox()
        clock.advance_ms(130_000)
    else:
        gateway.queue_send_faults(errors.SlowModeWaitError(request=None, capture=10))  # честный temporary
        await connector.step_outbox()
        clock.advance_ms(11_000)
    gateway.now_ms = int(clock.value * 1000)
    await connector.step_outbox()  # повтор B

    body_a = await get_outbox(public_client, a["outbox_id"])
    body_b = await get_outbox(public_client, b["outbox_id"])

    assert body_a["status"] == "sent"
    assert body_b["status"] == "sent"
    assert body_a["ext_id"] != body_b["ext_id"]  # каждая строка — СВОЁ сообщение, не одно на двоих
    assert gateway.delivered_count("+") == 2  # обе РЕАЛЬНО доставлены
    rows = [m for m in await visible_rows(public_client, channel_id) if m["text"] == "+"]
    assert len(rows) == 2  # ни задвоения, ни (что было багом) потери одной из строк


async def test_1c_crit_02_ext_id_conflict_guard_does_not_steal_other_outboxs_row(admin_client, db, clock):
    """F1(в): `report(outcome='sent', ext_id=X)` находит `(channel_id, ext_id)` уже занятым ЧУЖИМ outbox —
    не перевешивает чужую строку ленты на себя. Свой outbox -> `unknown` (решает человек), чужой не тронут,
    audit `outbox_ext_id_conflict` несёт оба outbox_id."""
    from hub.core.outbox import enqueue, lease, report

    account_id, _secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id="c-crit02", kind="dm")
    token_id = db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('crit02', 'ui', 'x')"
        ).lastrowid
    )

    e1 = db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="A",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="crit02-a",
            now_ms=1000,
        )
    )
    leases1 = db.write_sync(lambda conn: lease(conn, account_id=account_id, owner="w1", now_ms=1000))
    assert len(leases1) == 1
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=leases1[0].outbox_id,
            owner="w1",
            lease_gen=leases1[0].lease_gen,
            outcome="sent",
            ext_id="100",
            now_ms=1000,
        )
    )
    with db.reader() as conn:
        assert conn.execute(
            "SELECT status, ext_id FROM main.outbox WHERE id = ?", (e1.outbox_id,)
        ).fetchone() == ("sent", "100")

    e2 = db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="B",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="crit02-b",
            now_ms=2000,
        )
    )
    leases2 = db.write_sync(lambda conn: lease(conn, account_id=account_id, owner="w1", now_ms=2000))
    assert len(leases2) == 1
    # Симулируем ошибочный «нашли в истории» для СОВСЕМ другой строки — ext_id="100" уже занят outbox e1.
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=leases2[0].outbox_id,
            owner="w1",
            lease_gen=leases2[0].lease_gen,
            outcome="sent",
            ext_id="100",
            now_ms=2000,
        )
    )

    with db.reader() as conn:
        row2 = conn.execute("SELECT status, ext_id FROM main.outbox WHERE id = ?", (e2.outbox_id,)).fetchone()
        row1_after = conn.execute(
            "SELECT status, ext_id FROM main.outbox WHERE id = ?", (e1.outbox_id,)
        ).fetchone()
        msg1 = conn.execute(
            "SELECT outbox_id, text FROM main.message WHERE channel_id = ? AND ext_id = ?",
            (channel_id, "100"),
        ).fetchone()
        conflict_rows = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'outbox_ext_id_conflict'"
        ).fetchall()

    assert row2 == ("unknown", None)  # НЕ sent, себе чужой ext_id не присвоило
    assert row1_after == ("sent", "100")  # владелец не тронут
    assert msg1 == (e1.outbox_id, "A")  # строка ленты и текст остались за первым outbox
    assert len(conflict_rows) == 1
    detail = json.loads(conflict_rows[0][0])
    assert detail == {"outbox_id": e2.outbox_id, "conflict_outbox_id": e1.outbox_id, "ext_id": "100"}


# --- F2: чужая (не своя) отмена (Telethon disconnect) не убивает цикл отправки ------------------------------


async def test_1c_crit_03_stray_cancelled_error_from_send_does_not_kill_outbox(
    admin_client, public_client, internal_transport, db, clock
):
    """F2: `asyncio.CancelledError`, всплывший из середины `send()` (Telethon `disconnect()` изнутри
    `on_resume()` отменяет свои pending-future), не убивает `step_outbox()`/`_process_lease` — превращается в
    `abandon` (идемпотентный коннектор), строка доставляется на следующей попытке."""
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=902)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    cookie, _csrf = await login_via_code(public_client, db, clock)
    result = await send_ui(public_client, cookie, channel_id=channel_id, text="в полёте при пробуждении")

    gateway.queue_send_faults(asyncio.CancelledError())  # так выглядит _call после Telethon disconnect()
    processed = await connector.step_outbox()  # НЕ должно бросить исключение наружу
    assert processed == 1
    assert len(gateway.sent) == 0

    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "sending"  # abandon — report не ушёл, лиз висит до истечения

    clock.advance_ms(130_000)
    gateway.now_ms = int(clock.value * 1000)
    processed = await connector.step_outbox()  # цикл/каркас живы — доставка проходит нормально
    assert processed == 1
    assert len(gateway.sent) == 1
    body = await get_outbox(public_client, result["outbox_id"])
    assert body["status"] == "sent"


async def test_1c_crit_03b_real_own_task_cancellation_still_propagates():
    """F2 наоборот: НАСТОЯЩАЯ отмена своей задачи (`task.cancel()`, как при штатном SIGTERM) обязана
    пробрасываться как есть — `_own_task_cancelling()` не должен проглатывать реальную остановку."""
    from hub.connectors import base

    class _HangConnector(base.ConnectorBase):
        idempotent = True

        async def sync(self, reason="scheduled"):
            return None

        async def send(self, lease):
            await asyncio.Event().wait()  # никогда не завершится сам

        async def reconcile(self, lease):
            return base.SendResult("unknown")

        async def health(self):
            return None

    class _Client:
        async def report(self, **kw):
            return {}

    conn = _HangConnector(_Client())
    lease = {"mode": "send", "outbox_id": 1, "lease_gen": 1}
    task = asyncio.ensure_future(conn._process_lease(lease))  # noqa: SLF001
    await asyncio.sleep(0)
    task.cancel()  # настоящая отмена СВОЕЙ задачи
    with pytest.raises(asyncio.CancelledError):
        await task


# --- F3: ошибки ДО отправки (прогрев кэша/поиск в истории) классифицируются, никогда не дают unknown --------


async def test_1c_crit_04a_network_error_during_history_search_is_abandon_not_unknown(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=903)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    cookie, _csrf = await login_via_code(public_client, db, clock)
    r = await send_ui(public_client, cookie, channel_id=channel_id, text="сеть легла")

    gateway.queue_send_faults("network_before")
    await connector.step_outbox()  # abandon, ничего не ушло, maybe_sent станет True
    clock.advance_ms(130_000)
    gateway.now_ms = int(clock.value * 1000)

    async def _boom(*_a, **_k):
        raise ConnectionError("сеть всё ещё лежит")

    gateway.get_messages = _boom  # поиск в истории (maybe_sent=True) упадёт с сетевой ошибкой
    await connector.step_outbox()

    body = await get_outbox(public_client, r["outbox_id"])
    assert body["status"] == "sending"  # abandon (не unknown!) — лиз просто повис, попробуем снова
    assert len(gateway.sent) == 0


async def test_1c_crit_04b_floodwait_during_peer_cache_warm_is_flood_not_unknown(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()  # ни одного диалога -> _resolve_peer промахнётся и попробует iter_dialogs()
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id="88888888", kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    cookie, _csrf = await login_via_code(public_client, db, clock)
    r = await send_ui(public_client, cookie, channel_id=channel_id, text="промах кэша")

    async def _dialogs_flood():
        raise errors.FloodWaitError(request=None, capture=120)
        yield  # pragma: no cover — делает функцию генератором, до сюда не доходит

    gateway.iter_dialogs = _dialogs_flood
    await connector.step_outbox()

    body = await get_outbox(public_client, r["outbox_id"])
    assert body["status"] == "queued"  # flood (не unknown!) — обычная отсрочка, попытка не потрачена
    assert len(gateway.sent) == 0
    with db.reader() as conn:
        flood_until = conn.execute(
            "SELECT flood_until FROM main.account WHERE id = ?", (account_id,)
        ).fetchone()[0]
    assert flood_until is not None


# --- F4: 403 (кроме именных) -> permanent с message; ServerError/TimedOutError (кроме дубликата) -> abandon -


@pytest.mark.parametrize(
    ("exc_factory", "expected_status"),
    [
        (lambda: errors.ChatSendPlainForbiddenError(request=None), "failed"),
        (lambda: errors.UserRestrictedError(request=None), "failed"),
        (lambda: errors.rpcbaseerrors.ForbiddenError(request=None, message="PRIVACY_PREMIUM_REQUIRED"),
         "failed"),
        (lambda: errors.rpcbaseerrors.TimedOutError(request=None, message="Timeout"), "sending"),
        (lambda: errors.rpcbaseerrors.ServerError(request=None, message="INTERNAL"), "sending"),
    ],
    ids=["chat_send_plain_forbidden", "user_restricted", "generic_forbidden", "timed_out", "server_error"],
)
async def test_1c_crit_05_forbidden_permanent_server_timeout_abandon(
    admin_client, public_client, internal_transport, db, clock, exc_factory, expected_status
):
    """F4: именные и общие 403 (`ForbiddenError`) -> `permanent` (`failed`, не `unknown`); `ServerError`/
    `TimedOutError` (кроме `RandomIdDuplicateError`, у него своя ветка раньше) -> `abandon` (строка висит в
    `sending` до истечения лиза, НЕ `unknown` — повтор безопасен, идемпотентный источник)."""
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=904)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    cookie, _csrf = await login_via_code(public_client, db, clock)
    r = await send_ui(public_client, cookie, channel_id=channel_id, text="классификация F4")

    gateway.queue_send_faults(exc_factory())
    await connector.step_outbox()

    body = await get_outbox(public_client, r["outbox_id"])
    assert body["status"] == expected_status
    assert body["status"] != "unknown"


# --- F5: алерт о новой сессии — запоминаем только после успеха; белый список detail; битый state.json -------


async def test_1c_crit_06a_alert_hash_remembered_only_after_successful_send(
    admin_client, internal_transport, db, clock, tmp_path
):
    """F5(а): ядро отклонило алерт (сеть/рестарт) -> хеш НЕ запоминается, следующий прогон повторяет
    попытку и она доходит до audit. До правки хеш запоминался независимо от исхода — второго шанса не было."""
    account_id, secret = await create_tg_account(admin_client)
    gateway = FakeGateway()
    gateway.authorizations = [make_authorization(1, current=True), make_authorization(2)]
    state_path = tmp_path / "state.json"
    connector = make_tg_connector(
        internal_transport,
        secret=secret,
        gateway=gateway,
        account_id=account_id,
        clock=clock,
        state_path=str(state_path),
    )
    await connector._check_session_alert()  # noqa: SLF001 — первый прогон, запомнить
    gateway.authorizations.append(make_authorization(3, device_model="Чужой"))

    async def _down(**_kw):
        raise ConnectionError("core недоступен (рестарт/деплой)")

    connector._client.account_alert = _down  # noqa: SLF001
    await connector._check_session_alert()  # noqa: SLF001 — алерт не ушёл
    del connector._client.account_alert  # noqa: SLF001 — ядро «вернулось», используем настоящий метод
    clock.advance_ms(24 * 3600 * 1000 + 1)
    await connector._check_session_alert()  # noqa: SLF001 — второй шанс ДОЛЖЕН случиться

    known = json.loads(state_path.read_text())["known_hashes"]
    with db.reader() as conn:
        n = conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'connector_alert'").fetchone()[0]
    assert 3 in known
    assert n == 1  # алерт реально ушёл на третьем прогоне (не 0 — второй шанс не потерян)


async def test_1c_crit_06b_alert_detail_whitelist_account_id_not_overridable(
    admin_client, internal_transport, db, clock
):
    """F5(б): `account_id` в audit всегда из аутентификации (`principal`), тело запроса не может его
    подменить своим ключом `detail.account_id`; лишние (небелые) ключи в `detail` отбрасываются."""
    account_id, secret = await create_tg_account(admin_client)
    gateway = FakeGateway()
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    await connector._client.account_alert(  # noqa: SLF001
        kind="tg_new_session",
        text="x",
        detail={"account_id": 999, "device_model": "iPhone", "ip": "1.2.3.4", "junk_key": "нет такого"},
    )
    with db.reader() as conn:
        detail = json.loads(
            conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'connector_alert'"
            ).fetchone()[0]
        )
    assert detail["account_id"] == account_id  # НЕ 999
    assert detail["device_model"] == "iPhone"  # белый ключ — прошёл
    assert "ip" not in detail  # правило логов: без IP
    assert "junk_key" not in detail  # не белый ключ — отброшен


async def test_1c_crit_06c_corrupt_state_is_not_treated_as_first_run(
    admin_client, internal_transport, db, clock, tmp_path
):
    """F5(в): битый `state.json` (обрыв записи/порча) — НЕ «первый прогон»: новая сессия всё равно алертится
    (плюс отдельный алерт `tg_state_corrupt`), а не молча проглатывается как «просто запоминаем»."""
    account_id, secret = await create_tg_account(admin_client)
    gateway = FakeGateway()
    gateway.authorizations = [make_authorization(1, current=True), make_authorization(2)]
    state_path = tmp_path / "state.json"
    connector = make_tg_connector(
        internal_transport,
        secret=secret,
        gateway=gateway,
        account_id=account_id,
        clock=clock,
        state_path=str(state_path),
    )
    await connector._check_session_alert()  # noqa: SLF001 — настоящий первый прогон (файла ещё не было)
    state_path.write_text("{оборван")  # порча
    gateway.authorizations.append(make_authorization(3, device_model="Чужой"))
    await connector._check_session_alert()  # noqa: SLF001

    with db.reader() as conn:
        n = conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'connector_alert'").fetchone()[0]
        kinds = {
            r[0]
            for r in conn.execute(
                "SELECT result FROM main.audit WHERE action = 'connector_alert'"
            ).fetchall()
        }
    assert n >= 1  # алерт про новую сессию (3) реально ушёл — не проглочен как «первый прогон»
    assert "tg_new_session" in kinds
    assert "tg_state_corrupt" in kinds  # и про саму порчу тоже сообщили
    assert 3 in json.loads(state_path.read_text())["known_hashes"]


async def test_1c_crit_06d_failed_check_retried_on_next_heartbeat_not_a_day_later(
    admin_client, internal_transport, db, clock, tmp_path
):
    """F5(г): проверка провалилась (сеть ещё не поднялась) -> `_last_alert_check_wall` НЕ трогаем, чтобы
    `health()` попробовал снова на следующем пульсе, а не молчал целые сутки."""
    account_id, secret = await create_tg_account(admin_client)
    gateway = FakeGateway()
    connector = make_tg_connector(
        internal_transport,
        secret=secret,
        gateway=gateway,
        account_id=account_id,
        clock=clock,
        state_path=str(tmp_path / "state.json"),
    )

    async def _fail():
        raise ConnectionError("сеть ещё не поднялась на старте")

    gateway.get_authorizations = _fail
    await connector._check_session_alert()  # noqa: SLF001
    clock.advance_ms(23 * 3600 * 1000)
    # _last_alert_check_wall остался на исходном float("-inf") — разница огромная, гейт health() сработает
    # на следующем же тике, а не будет молчать почти сутки.
    assert connector._wall() - connector._last_alert_check_wall >= 24 * 3600  # noqa: SLF001


# --- F6: top_msg_id — ТОЛЬКО у настоящего ответа (reply_to_msg_id != topic_id И topic_id != 1) --------------


def test_1c_crit_07_reply_to_top_msg_id_only_for_real_non_general_reply():
    """F6: по документации `inputReplyToMessage` (core.telegram.org/constructor/inputReplyToMessage):
    `top_msg_id` — ТОЛЬКО когда `reply_to_msg_id != topic_id` И `topic_id != 1`. Пост «в тему» без ответа
    (в т.ч. General) -> только `reply_to_msg_id`, БЕЗ `top_msg_id`; ответ ВНУТРИ General — тоже без
    `top_msg_id` (topic_id == 1); настоящий ответ в НЕ-General теме — оба поля."""
    from hub.connectors.telegram import convert

    post_in_topic = convert.build_reply_to(topic_id=5, reply_to_ext_id=None)
    assert post_in_topic.reply_to_msg_id == 5
    assert post_in_topic.top_msg_id is None

    reply_in_general = convert.build_reply_to(topic_id=1, reply_to_ext_id="40")
    assert reply_in_general.reply_to_msg_id == 40
    assert reply_in_general.top_msg_id is None

    real_reply_in_topic = convert.build_reply_to(topic_id=5, reply_to_ext_id="42")
    assert real_reply_in_topic.reply_to_msg_id == 42
    assert real_reply_in_topic.top_msg_id == 5

    general_no_reply = convert.build_reply_to(topic_id=1, reply_to_ext_id=None)
    assert general_no_reply is None  # обычное сообщение, как не в форуме вовсе
