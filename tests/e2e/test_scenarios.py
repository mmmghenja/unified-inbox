"""Сквозные сценарии приёмки фазы 0 (кусок 5b): публичный API -> outbox -> внутреннее приложение ->
FakeConnector -> FakeSource -> эхо -> лента через публичный API. Спецификация:
docs/architecture.md, тесты 10-22.

Каждый тест — своя связка account/channel (через `conftest.create_account`/`setup_channel`), чтобы сценарии
не влияли друг на друга.
"""

import asyncio

from httpx import ASGITransport, AsyncClient

from hub.connectors.fake import FakeSource, FaultPlan
from hub.core.feed import stream
from hub.core.security import Principal

from .conftest import (
    PUBLIC_BASE_URL,
    assert_outcome,
    create_account,
    create_project,
    create_token,
    csrf_headers,
    get_outbox,
    login_via_code,
    make_connector,
    restart_core,
    send_bearer,
    send_ui,
    setup_channel,
    visible_rows,
)

# --- Тест 10: упал ДО отправки (идемпотентный и нет) -> после истечения лиза доставлено ровно 1 раз -------


async def test_10_crash_before_send_idempotent_delivers_once(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client, config={"idempotent_send": True})
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "crash-before-send-idempotent"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=True)
    plan = FaultPlan().queue_outbox("crash_before_send")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()
    assert processed == 0
    assert source.delivered_count(text) == 0  # доставки не было вовсе, лиз просто повис

    clock.advance_ms(70_000)  # лиз (60с по умолчанию) истёк по настенным часам
    processed = await connector.step_outbox()  # идемпотентный аккаунт -> повтор с тем же send_token
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 11: упал ПОСЛЕ доставки, до report, неидемпотентный -----------------------------------------


async def test_11_crash_after_delivery_reconcile_finds_history(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "crash-after-delivery-history-ok"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("crash_after_delivery")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # доставил в источник, "упал" до report -> лиз висит
    assert processed == 0
    assert source.delivered_count(text) == 1  # доставка УЖЕ случилась, просто не отчитались

    clock.advance_ms(70_000)
    processed = await connector.step_outbox()  # reconcile находит в history -> sent, без человека
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


async def test_11_crash_after_delivery_history_unavailable_stays_unknown(
    admin_client, public_client, internal_transport, db, clock
):
    """История канала недоступна -> `reconcile` не может подтвердить отправку -> `unknown`; дальше без
    человека НИЧЕГО не уходит (лиз снят, `unknown` не выдаётся `lease()` вовсе)."""
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-hist-unavail")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "crash-after-delivery-history-unavailable"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("crash_after_delivery")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()
    assert processed == 0
    assert source.delivered_count(text) == 1  # доставка случилась...

    source.set_history_unavailable("c-hist-unavail")
    clock.advance_ms(70_000)
    processed = await connector.step_outbox()  # ...но подтвердить это reconcile не может
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="unknown",
    )

    # Без человека — ничего не уходит: несколько шагов подряд не меняют ни доставок, ни статуса.
    for _ in range(3):
        processed = await connector.step_outbox()
        assert processed == 0
    assert source.delivered_count(text) == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "unknown"


async def test_10_crash_before_send_nonidempotent_needs_reconcile_then_retry(
    admin_client, public_client, internal_transport, db, clock
):
    """Неидемпотентный аккаунт: лиз истекает -> `reconcile` (не blind resend); упавший ДО отправки коннектор
    ничего не доставил, `reconcile` честно находит пустую историю -> `unknown`. Дальше — только решение
    человека (`resolve-unknown`, раздел outbox-спеки «вслепую не повторяем никогда»); `retry` возвращает
    строку в очередь, и уже БЕЗ отказов она уходит ровно один раз."""
    account_id, secret = await create_account(admin_client)  # без idempotent_send
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "crash-before-send-nonidempotent"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("crash_before_send")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()
    assert processed == 0
    assert source.delivered_count(text) == 0

    clock.advance_ms(70_000)
    processed = await connector.step_outbox()  # неидемпотентный -> reconcile; ничего не найдено -> unknown
    assert processed == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "unknown"

    r = await public_client.post(
        f"/api/v1/outbox/{outbox_id}/resolve-unknown",
        json={"decision": "retry"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"

    processed = await connector.step_outbox()  # без отказов -> нормальная отправка
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 12: упал после доставки, идемпотентный -> повтор с тем же send_token, источник схлопнул ---------


async def test_12_crash_after_delivery_idempotent_collapses_retry(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client, config={"idempotent_send": True})
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "crash-after-delivery-idempotent"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=True)
    plan = FaultPlan().queue_outbox("crash_after_delivery")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # доставил (записал send_token в источник), упал до report
    assert processed == 0
    assert source.delivered_count(text) == 1

    clock.advance_ms(70_000)
    # Идемпотентный аккаунт -> повтор с ТЕМ ЖЕ send_token; источник узнаёт токен и схлопывает повтор в ту
    # же доставку (deliver() возвращает старый ext_id, новой записи не создаёт).
    processed = await connector.step_outbox()
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 14: эхо раньше/позже результата, без токена после рестарта, с искажённым текстом ----------------


async def test_14_echo_before_report_is_quiet_success(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-echo-before")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "echo-before-report"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("echo_before_report")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    # send() доставляет, ПРОТАЛКИВАЕТ эхо в core напрямую, и только потом step_outbox() зовёт report() —
    # тот застаёт строку уже 'sent' с тем же ext_id (тихий успех, outbox.report_dup).
    processed = await connector.step_outbox()
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


async def test_14_result_before_echo_merges_without_duplicate(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-result-before")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "result-before-echo"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    connector = make_connector(internal_transport, secret=secret, source=source)

    processed = await connector.step_outbox()  # без отказов -> report(sent) сразу же
    assert processed == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"

    await connector.sync()  # эхо приходит ПОЗЖЕ результата -> тот же ext_id, содержимое не меняется, noop

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


async def test_14_echo_without_token_after_restart_matches_by_text(
    admin_client, public_client, internal_transport, db, clock
):
    """«Рестарт коннектора» = новый экземпляр `FakeConnector` (спека, раздел «Стенд»); карта токенов не
    переживает рестарт по условию сценария — здесь это выражено явным `lose_token_map` на sync() НОВОГО
    экземпляра, читающего события ТОГО ЖЕ `FakeSource` (внешний мир не перезапускался)."""
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-lose-token")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "echo-without-token"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan_a = FaultPlan().queue_outbox("crash_after_delivery")
    connector_a = make_connector(internal_transport, secret=secret, source=source, plan=plan_a)

    processed = await connector_a.step_outbox()  # доставил, "упал" до report -> лиз висит
    assert processed == 0
    assert source.delivered_count(text) == 1

    # "Рестарт": новый экземпляр без памяти о токенах, эхо теряет send_token на sync().
    plan_b = FaultPlan().queue_sync("lose_token_map")
    connector_b = make_connector(internal_transport, secret=secret, source=source, plan=plan_b)
    await connector_b.sync()  # эхо без токена, но текст точный и в окне 60с -> match_echo по тексту

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


async def test_14_mangled_echo_text_still_merges_via_token(
    admin_client, public_client, internal_transport, db, clock
):
    """Источник переформатировал текст в эхе — эвристика по тексту не сработала бы, но `send_token`
    склеивает независимо от текста при любом статусе, кроме `cancelled` (outbox-спека)."""
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-mangle")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "mangle-echo-text"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan()
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # доставлено и отчитано нормально -> sent сразу
    assert processed == 1

    plan.queue_sync("mangle_echo_text")
    await connector.sync()  # эхо с искажённым текстом, но тем же ext_id -> обновление той же строки

    rows = await visible_rows(public_client, channel_id)
    assert len(rows) == 1, rows  # ровно одна видимая строка, независимо от того, что стало с текстом

    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"
    assert source.delivered_count(text) == 1


# --- Тест 15: temporary, а сообщение на деле ушло (врущий коннектор) + эхо -> sent, без повторной доставки


async def test_15_temporary_but_delivered_lying_connector_merges_via_echo(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id)

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "temporary-but-delivered"
    result = await send_ui(public_client, cookie, channel_id=channel_id, text=text)
    outbox_id = result["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("temporary_but_delivered")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # доставил, но отчитался temporary -> queued с backoff
    assert processed == 1
    assert source.delivered_count(text) == 1
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "queued"

    await connector.sync()  # эхо со send_token склеивает НЕЗАВИСИМО от статуса (не cancelled) -> sent
    body = await get_outbox(public_client, outbox_id)
    assert body["status"] == "sent"

    # Раз статус sent, lease() больше не выдаёт эту строку — сколько шагов ни делай, повторной доставки нет.
    for _ in range(3):
        processed = await connector.step_outbox()
        assert processed == 0

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 13: FLOOD_WAIT — блокирует весь аккаунт, переживает рестарт core, 5 подряд не приводят к failed


async def test_13_flood_blocks_whole_account_and_survives_restart(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_a = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-flood-a")
    channel_b = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-flood-b")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text_a, text_b = "flood-channel-a", "flood-channel-b"
    outbox_a = (await send_ui(public_client, cookie, channel_id=channel_a, text=text_a))["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox(("flood", 60_000))
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # msg_a -> flood: account.flood_until выставлен на 60с
    assert processed == 1
    body = await get_outbox(public_client, outbox_a)
    assert body["status"] == "queued"

    # Только ТЕПЕРЬ заводим второе сообщение — flood_until уже висит на аккаунте, канал B ни разу не лизился.
    outbox_b = (await send_ui(public_client, cookie, channel_id=channel_b, text=text_b))["outbox_id"]
    processed = await connector.step_outbox()  # аккаунт зафлужен целиком -> ни msg_a, ни msg_b не лизятся
    assert processed == 0
    assert source.delivered_count(text_a) == 0
    assert source.delivered_count(text_b) == 0

    # «Рестарт core»: flood_until — состояние в БД, переживает пересборку Db/приложений.
    new_db, new_internal_app, new_public_app = await restart_core(db, clock)
    try:
        new_internal_transport = ASGITransport(app=new_internal_app)
        new_connector = make_connector(new_internal_transport, secret=secret, source=source)
        new_public_transport = ASGITransport(app=new_public_app)
        async with AsyncClient(transport=new_public_transport, base_url=PUBLIC_BASE_URL) as new_pc:
            new_pc.cookies.set("hub_session", cookie)  # сессия — строка в БД, файлы те же, cookie та же
            processed = await new_connector.step_outbox()  # ещё до срока -> по-прежнему ничего
            assert processed == 0
            assert source.delivered_count(text_a) == 0
            assert source.delivered_count(text_b) == 0

            clock.advance_ms(65_000)  # срок flood_until прошёл
            processed = await new_connector.step_outbox()  # оба канала разлочены, id разный -> оба уходят
            assert processed == 2

            await assert_outcome(
                new_pc,
                source,
                outbox_id=outbox_a,
                channel_id=channel_a,
                text=text_a,
                deliveries=1,
                visible=1,
                status="sent",
            )
            await assert_outcome(
                new_pc,
                source,
                outbox_id=outbox_b,
                channel_id=channel_b,
                text=text_b,
                deliveries=1,
                visible=1,
                status="sent",
            )
    finally:
        new_db.close()


async def test_13_five_floods_in_a_row_do_not_fail(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-flood-5x")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text = "flood-five-times"
    outbox_id = (await send_ui(public_client, cookie, channel_id=channel_id, text=text))["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox(*[("flood", 10_000) for _ in range(5)])
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    for _ in range(5):
        processed = await connector.step_outbox()
        assert processed == 1
        body = await get_outbox(public_client, outbox_id)
        assert body["status"] == "queued"  # НИКОГДА failed, сколько FLOOD подряд ни приходило бы
        clock.advance_ms(11_000)  # переживаем и flood_until, и not_before_ts этой же строки

    processed = await connector.step_outbox()  # без отказов -> нормальная отправка
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=text,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 20: FIFO по каналу — второе сообщение не обгоняет первое ни при temporary, ни при unknown -------


async def test_20_fifo_holds_across_temporary(admin_client, public_client, internal_transport, db, clock):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-fifo-temp")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text1, text2 = "fifo-temp-first", "fifo-temp-second"
    outbox1 = (await send_ui(public_client, cookie, channel_id=channel_id, text=text1))["outbox_id"]
    outbox2 = (await send_ui(public_client, cookie, channel_id=channel_id, text=text2))["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("temporary")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # только msg1 лизится (msg2 блокирован FIFO) -> temporary
    assert processed == 1
    assert source.delivered_count(text1) == 0
    assert source.delivered_count(text2) == 0
    body1 = await get_outbox(public_client, outbox1)
    assert body1["status"] == "queued"

    processed = await connector.step_outbox()  # backoff msg1 не прошёл, msg1 всё ещё 'queued' блокирует msg2
    assert processed == 0
    assert source.delivered_count(text2) == 0

    clock.advance_ms(11_000)  # backoff msg1 (5с*2^0=5с) точно прошёл
    processed = await connector.step_outbox()  # msg1 уходит нормально; msg2 в ЭТОМ же вызове ещё блокирован
    assert processed == 1
    assert source.delivered_count(text2) == 0

    processed = await connector.step_outbox()  # msg1 теперь 'sent' -> msg2 разлочен
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox1,
        channel_id=channel_id,
        text=text1,
        deliveries=1,
        visible=1,
        status="sent",
    )
    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox2,
        channel_id=channel_id,
        text=text2,
        deliveries=1,
        visible=1,
        status="sent",
    )
    d1 = next(d for d in source.deliveries if d.text == text1)
    d2 = next(d for d in source.deliveries if d.text == text2)
    assert int(d1.ext_id) < int(d2.ext_id)  # msg1 доставлено строго раньше msg2 — не обогнало


async def test_20_fifo_holds_while_unknown(admin_client, public_client, internal_transport, db, clock):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-fifo-unknown")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text1 = "fifo-unknown-first"
    outbox1 = (await send_ui(public_client, cookie, channel_id=channel_id, text=text1))["outbox_id"]

    source = FakeSource(idempotent=False)
    plan = FaultPlan().queue_outbox("crash_after_delivery")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # доставил, "упал" до report -> лиз висит
    assert processed == 0
    assert source.delivered_count(text1) == 1

    source.set_history_unavailable("c-fifo-unknown")
    clock.advance_ms(70_000)
    processed = await connector.step_outbox()  # reconcile: история недоступна -> unknown
    assert processed == 1
    body1 = await get_outbox(public_client, outbox1)
    assert body1["status"] == "unknown"

    # msg2 заводится ПОСЛЕ того, как msg1 уже unknown — 'unknown' держит очередь канала (outbox-спека).
    text2 = "fifo-unknown-second"
    outbox2 = (await send_ui(public_client, cookie, channel_id=channel_id, text=text2))["outbox_id"]
    processed = await connector.step_outbox()  # msg1 не лизится (unknown не выдаётся), msg2 блокирован им
    assert processed == 0
    assert source.delivered_count(text2) == 0

    # Человек разрешает unknown ("на деле не было отправлено" — по факту неверно, но это его решение, а не
    # догадка системы): failed уже не входит в блокирующий набор статусов канала.
    r = await public_client.post(
        f"/api/v1/outbox/{outbox1}/resolve-unknown",
        json={"decision": "not_sent"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"

    processed = await connector.step_outbox()  # msg2 теперь разлочен
    assert processed == 1

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox1,
        channel_id=channel_id,
        text=text1,
        deliveries=1,
        visible=1,
        status="failed",
    )
    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox2,
        channel_id=channel_id,
        text=text2,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 21: агентский путь — pending_approval, approve с правкой текста, bearer на approve -> 403 -------


async def test_21_agent_draft_needs_approval_then_delivers_corrected_text(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-agent", trusted=False)
    token = await create_token(admin_client, kind="interactive", scopes=("read", "send"))

    draft_text = "агентский черновик с опечаткай"
    result = await send_bearer(public_client, token, channel_id=channel_id, text=draft_text)
    outbox_id = result["outbox_id"]
    assert result["status"] == "pending_approval"

    source = FakeSource(idempotent=False)
    connector = make_connector(internal_transport, secret=secret, source=source)

    # pending_approval не лизится вообще — сколько шагов ни делай, в источник ничего не уходит.
    for _ in range(3):
        processed = await connector.step_outbox()
        assert processed == 0
    assert source.delivered_count(draft_text) == 0

    bearer_headers = {"Authorization": f"Bearer {token}"}
    r = await public_client.post(f"/api/v1/outbox/{outbox_id}/approve", json={}, headers=bearer_headers)
    assert r.status_code == 403, r.text  # bearer на approve -> 403, состояние не меняется
    body = await get_outbox(public_client, outbox_id, headers=bearer_headers)
    assert body["status"] == "pending_approval"

    cookie, _csrf = await login_via_code(public_client, db, clock)
    corrected_text = "агентский черновик с опечаткой"
    r = await public_client.post(
        f"/api/v1/outbox/{outbox_id}/approve",
        json={"text": corrected_text},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"

    processed = await connector.step_outbox()  # текст на лизе — уже ИСПРАВЛЕННЫЙ (approve обновил outbox)
    assert processed == 1

    assert source.delivered_count(draft_text) == 0  # черновик так и не ушёл
    await assert_outcome(  # клиент уже несёт UI cookie (после login_via_code) -> явный bearer не нужен
        public_client,
        source,
        outbox_id=outbox_id,
        channel_id=channel_id,
        text=corrected_text,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 18: сон ноутбука — скачок часов +8ч при строке в sending и при отложенной not_before_ts --------


async def test_18_laptop_sleep_wakes_hanging_and_delayed_rows(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client, config={"idempotent_send": True})
    project_id = await create_project(admin_client)
    channel_sleeping = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-sleep-a")
    channel_delayed = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-sleep-b")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text_sleeping = "sleep-lease-hanging"
    outbox_sleeping = (await send_ui(public_client, cookie, channel_id=channel_sleeping, text=text_sleeping))[
        "outbox_id"
    ]

    source = FakeSource(idempotent=True)
    plan = FaultPlan().queue_outbox("crash_before_send")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)

    processed = await connector.step_outbox()  # строка осталась в 'sending' — лиз повис (комп "заснул")
    assert processed == 0
    assert source.delivered_count(text_sleeping) == 0

    # Второе сообщение — уже ПОСЛЕ того, как первый лиз повис, чтобы не задеть его FIFO-блокировкой
    # (разные каналы — она и не действует, но так сценарий читается яснее): temporary откладывает его на
    # несколько секунд вперёд, задолго до 8-часового скачка.
    text_delayed = "sleep-not-before-ts"
    outbox_delayed = (await send_ui(public_client, cookie, channel_id=channel_delayed, text=text_delayed))[
        "outbox_id"
    ]
    plan.queue_outbox("temporary")
    processed = await connector.step_outbox()
    assert processed == 1
    body = await get_outbox(public_client, outbox_delayed)
    assert body["status"] == "queued"

    clock.advance_ms(8 * 3600 * 1000)  # ноутбук проспал 8 часов
    processed = await connector.step_outbox()  # оба лиза считаются истёкшими по настенным часам
    assert processed == 2

    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_sleeping,
        channel_id=channel_sleeping,
        text=text_sleeping,
        deliveries=1,
        visible=1,
        status="sent",
    )
    await assert_outcome(
        public_client,
        source,
        outbox_id=outbox_delayed,
        channel_id=channel_delayed,
        text=text_delayed,
        deliveries=1,
        visible=1,
        status="sent",
    )


# --- Тест 16: правка/удаление/реакция/прочтение видны через /sync с ростом seq; unread верный -------------


async def _inbox_channel(public_client, ext_id: str) -> dict | None:
    r = await public_client.get("/api/v1/inbox")
    assert r.status_code == 200, r.text
    return next((c for c in r.json() if c["ext_id"] == ext_id), None)


async def test_16_incoming_edit_delete_reaction_read_visible_via_sync(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    cookie, _csrf = await login_via_code(public_client, db, clock)

    source = FakeSource(idempotent=False)
    connector = make_connector(internal_transport, secret=secret, source=source)
    channel_ext_id = "peer-chan-16"

    # 1. Два входящих в НОВОМ (ingest сам создаёт) канале — одной пачкой.
    source.incoming(channel_ext_id, ext_id="m1", text="hello1", ts=1000)
    source.incoming(channel_ext_id, ext_id="m2", text="hello2", ts=1100)
    await connector.sync()

    r = await public_client.get("/api/v1/sync?since=0")
    assert r.status_code == 200, r.text
    changes1 = r.json()
    assert {m["text"] for m in changes1["messages"]} == {"hello1", "hello2"}
    seqs1 = [m["seq"] for m in changes1["messages"]]
    assert seqs1 == sorted(seqs1) and len(set(seqs1)) == len(seqs1)  # растёт, без дублей
    s1 = changes1["max_seq"]
    channel_row = next(c for c in changes1["channels"] if c["ext_id"] == channel_ext_id)
    channel_id = channel_row["id"]

    inbox_row = await _inbox_channel(public_client, channel_ext_id)
    assert inbox_row is not None and inbox_row["unread"] == 2

    # 2. Правка m1 — видна через /sync, unread НЕ растёт (сообщение уже посчитано).
    source.edit(channel_ext_id, ext_id="m1", text="hello1-edited", ts=1200)
    await connector.sync()

    r = await public_client.get(f"/api/v1/sync?since={s1}")
    changes2 = r.json()
    assert any(m["ext_id"] == "m1" and m["text"] == "hello1-edited" for m in changes2["messages"])
    s2 = changes2["max_seq"]
    assert s2 > s1
    inbox_row = await _inbox_channel(public_client, channel_ext_id)
    assert inbox_row["unread"] == 2

    # 3. Реакция на m1 — видна через /sync, unread не растёт.
    source.reaction(channel_ext_id, ext_id="m1", reactions=[{"emoji": "+1", "count": 1}])
    await connector.sync()

    r = await public_client.get(f"/api/v1/sync?since={s2}")
    changes3 = r.json()
    assert any(m["ext_id"] == "m1" and m["reactions_json"] for m in changes3["messages"])
    s3 = changes3["max_seq"]
    assert s3 > s2
    inbox_row = await _inbox_channel(public_client, channel_ext_id)
    assert inbox_row["unread"] == 2

    # 4. Третье входящее -> unread растёт до 3, затем удаление -> unread обратно 2, видимость через /sync.
    source.incoming(channel_ext_id, ext_id="m3", text="hello3", ts=1300)
    await connector.sync()
    inbox_row = await _inbox_channel(public_client, channel_ext_id)
    assert inbox_row["unread"] == 3
    s_before_delete = (await public_client.get(f"/api/v1/sync?since={s3}")).json()["max_seq"]

    source.delete(channel_ext_id, ext_ids=["m3"])
    await connector.sync()

    r = await public_client.get(f"/api/v1/sync?since={s_before_delete}")
    changes4 = r.json()
    assert any(m["ext_id"] == "m3" and m["deleted"] for m in changes4["messages"])
    s4 = changes4["max_seq"]
    assert s4 > s_before_delete
    inbox_row = await _inbox_channel(public_client, channel_ext_id)
    assert inbox_row["unread"] == 2  # m3 удалено -> не считается

    # 5. Прочтение «на телефоне» до m2 -> накрывает m1 И m2 (более ранний ts) -> unread 0, видно через /sync
    #    (last_read_seq трогает channel.seq, поэтому изменение канала попадает в тот же changed-set).
    source.read_on_phone(channel_ext_id, up_to_ext_id="m2")
    await connector.sync()

    r = await public_client.get(f"/api/v1/sync?since={s4}")
    changes5 = r.json()
    channel_change = next(c for c in changes5["channels"] if c["id"] == channel_id)
    assert channel_change["unread"] == 0
    assert (await _inbox_channel(public_client, channel_ext_id)) is None  # unread=0 -> не в inbox


# --- Тест 17: дыра в середине пачки + ядовитое событие -> dead_letter+1, курсор сдвинут; повтор пачки -----
# после "падения коннектора" -> без лишних строк, seq не растёт.


async def test_17_gap_and_poison_event_dead_letter_cursor_advances(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    cookie, _csrf = await login_via_code(public_client, db, clock)

    source = FakeSource(idempotent=False)
    connector = make_connector(internal_transport, secret=secret, source=source)
    channel_ext_id = "peer-chan-17"

    # 1. Дыра в середине пачки: m1, m2, m3 -> m2 теряется источником безвозвратно, курсор всё равно
    #    продвигается мимо неё (не переотправляется на следующем sync()).
    source.incoming(channel_ext_id, ext_id="m1", text="gap-1", ts=1000)
    source.incoming(channel_ext_id, ext_id="m2", text="gap-2", ts=1100)
    source.incoming(channel_ext_id, ext_id="m3", text="gap-3", ts=1200)
    connector._plan.queue_sync("gap_in_batch")  # noqa: SLF001 — доступ к очереди отказов теста ради
    await connector.sync()

    rows = await visible_rows(public_client, (await _channel_id_by_ext(public_client, channel_ext_id)))
    texts = {m["text"] for m in rows}
    assert texts == {"gap-1", "gap-3"}  # m2 потеряна безвозвратно, m1/m3 в ленте

    body = await public_client.get("/api/v1/status")
    assert body.json()["dead_letter"].get(str(account_id), 0) == 0  # дыра — не яд, dead_letter не растёт

    await connector.sync()  # повторный заход НЕ возвращает то, что уже было послано (курсор сдвинут)
    rows = await visible_rows(public_client, (await _channel_id_by_ext(public_client, channel_ext_id)))
    assert {m["text"] for m in rows} == {"gap-1", "gap-3"}  # ничего лишнего не появилось

    # 2. Ядовитое событие: одно валидное входящее + одно невалидное (без обязательных полей) в одной пачке.
    source.incoming(channel_ext_id, ext_id="m4", text="poison-neighbour", ts=1300)
    connector._plan.queue_sync("poison_event")  # noqa: SLF001
    r_sync = await public_client.get("/api/v1/sync?since=0")
    s_before = r_sync.json()["max_seq"]
    await connector.sync()

    rows = await visible_rows(public_client, (await _channel_id_by_ext(public_client, channel_ext_id)))
    assert "poison-neighbour" in {m["text"] for m in rows}  # соседи применены

    body = await public_client.get("/api/v1/status")
    assert body.json()["dead_letter"][str(account_id)] == 1  # ядовитое событие -> dead_letter+1

    r_sync = await public_client.get(f"/api/v1/sync?since={s_before}")
    s_after = r_sync.json()["max_seq"]
    assert s_after > s_before  # курсор (max_seq ленты) сдвинулся несмотря на яд

    # 3. "Коннектор упал между push_events и сохранением своего состояния" -> та же пачка приходит снова.
    #    Идемпотентный upsert по (channel_id, ext_id) с неизменным содержимым -> noop, seq не растёт.
    source.incoming(channel_ext_id, ext_id="m5", text="crash-after-push", ts=1400)
    connector._plan.queue_sync("crash_after_push")  # noqa: SLF001
    await connector.sync()
    r_sync = await public_client.get("/api/v1/sync?since=0")
    s_first = r_sync.json()["max_seq"]

    await connector.sync()  # повтор ТОЙ ЖЕ пачки (offset не продвинулся из-за "падения")
    r_sync = await public_client.get(f"/api/v1/sync?since={s_first}")
    assert r_sync.json()["messages"] == []  # ни одной лишней строки
    assert r_sync.json()["max_seq"] == s_first  # seq не вырос

    rows = await visible_rows(public_client, (await _channel_id_by_ext(public_client, channel_ext_id)))
    assert sum(1 for m in rows if m["text"] == "crash-after-push") == 1  # всё равно ровно одна строка

    # Курсор коннектора (внутренний сокет) отражает прогресс несмотря на "падение" — core его получил.
    cursor = await connector.get_cursor()
    assert cursor is not None and int(cursor["value"]) == source.log_len


async def _channel_id_by_ext(public_client, ext_id: str) -> int:
    r = await public_client.get("/api/v1/sync?since=0")
    assert r.status_code == 200, r.text
    row = next(c for c in r.json()["channels"] if c["ext_id"] == ext_id)
    return row["id"]


# --- Тест 22: падающий коннектор одного аккаунта не мешает отправке и приёму другого --------------------


async def test_22_failing_account_does_not_block_another_account(
    admin_client, public_client, internal_transport, db, clock
):
    account_a, secret_a = await create_account(admin_client, source="fake-a")
    account_b, secret_b = await create_account(admin_client, source="fake-b")
    project_id = await create_project(admin_client)
    channel_a = setup_channel(db, clock, account_a, project_id=project_id, ext_id="c-account-a")
    channel_b = setup_channel(db, clock, account_b, project_id=project_id, ext_id="c-account-b")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    text_a, text_b = "account-a-stuck", "account-b-fine"
    outbox_a = (await send_ui(public_client, cookie, channel_id=channel_a, text=text_a))["outbox_id"]
    outbox_b = (await send_ui(public_client, cookie, channel_id=channel_b, text=text_b))["outbox_id"]

    source_a = FakeSource(idempotent=False)
    plan_a = FaultPlan().queue_outbox("auth_required")
    connector_a = make_connector(internal_transport, secret=secret_a, source=source_a, plan=plan_a)

    source_b = FakeSource(idempotent=False)
    connector_b = make_connector(internal_transport, secret=secret_b, source=source_b)

    # Источник A отказал в авторизации -> отправка встала. Лиз каркас взял и обработал (итог — auth_required),
    # поэтому настоящий ConnectorBase возвращает 1; report() при этом не уходит, строка остаётся в sending.
    assert await connector_a.step_outbox() == 1
    assert connector_a.auth_required is True
    assert source_a.delivered_count(text_a) == 0

    # Дальше коннектор A даже не берёт лизы вовсе (auth_required) — сколько шагов ни делай.
    for _ in range(3):
        assert await connector_a.step_outbox() == 0

    # Аккаунт B работает независимо: отправка проходит нормально.
    processed_b = await connector_b.step_outbox()
    assert processed_b == 1
    await assert_outcome(
        public_client,
        source_b,
        outbox_id=outbox_b,
        channel_id=channel_b,
        text=text_b,
        deliveries=1,
        visible=1,
        status="sent",
    )

    # И приём B тоже работает независимо от состояния A.
    source_b.incoming("peer-b", ext_id="mb1", text="incoming-for-b", ts=2000)
    await connector_b.sync()
    rows_b = await visible_rows(public_client, (await _channel_id_by_ext(public_client, "peer-b")))
    assert any(m["text"] == "incoming-for-b" for m in rows_b)

    # Аккаунт A по-прежнему ничего не доставил и не отчитался: лиз просто повис в 'sending' — auth_required
    # останавливает цикл БЕЗ report() (контракт ConnectorBase), в 'queued' строку никто не возвращает.
    body_a = await get_outbox(public_client, outbox_a)
    assert body_a["status"] == "sending"
    assert source_a.delivered_count(text_a) == 0


# --- Тест 19: SSE сквозь всё — клиент с Last-Event-ID, «проспавший» серию событий, догоняет без дыр/дублей


async def test_19_sse_client_resumes_after_sleeping_through_activity(
    admin_client, public_client, internal_transport, db, clock, notifier
):
    """`feed.stream` — напрямую с `app.state.notifier` (спека, «Стенд»): HTTP-поток через `ASGITransport`
    буферизует ответ целиком и зависает на бесконечном SSE-генераторе (то же ограничение, что в
    `tests/api/test_sync_stream.py`)."""
    account_id, secret = await create_account(admin_client)
    project_id = await create_project(admin_client)
    channel_id = setup_channel(db, clock, account_id, project_id=project_id, ext_id="c-sse")

    cookie, _csrf = await login_via_code(public_client, db, clock)
    source = FakeSource(idempotent=False)
    connector = make_connector(internal_transport, secret=secret, source=source)

    # Активность ДО того, как клиент уснул: клиент это увидел, запомнил last_seq и заснул.
    text_before = "sse-before-sleep"
    outbox_before = (await send_ui(public_client, cookie, channel_id=channel_id, text=text_before))[
        "outbox_id"
    ]
    assert await connector.step_outbox() == 1
    body_before = await get_outbox(public_client, outbox_before)
    assert body_before["status"] == "sent"
    r = await public_client.get("/api/v1/healthz")
    last_seq = r.json()["max_seq"]

    # Активность, ПРОСПАННАЯ клиентом: отправка (с эхом) + входящее от собеседника.
    text_asleep = "sse-slept-through-send"
    outbox_asleep = (await send_ui(public_client, cookie, channel_id=channel_id, text=text_asleep))[
        "outbox_id"
    ]
    assert await connector.step_outbox() == 1
    source.incoming("c-sse", ext_id="peer-sse-1", text="sse-slept-through-incoming", ts=5000)
    await connector.sync()

    r = await public_client.get("/api/v1/healthz")
    current_max_seq = r.json()["max_seq"]
    assert current_max_seq > last_seq

    principal = Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    gen = stream(db, principal, last_seq=last_seq, notifier=notifier, heartbeat_s=5.0)
    seen_seqs: list[int] = []
    try:
        while True:
            frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
            seq_ = int(frame.splitlines()[0].removeprefix("id: "))
            seen_seqs.append(seq_)
            if seq_ >= current_max_seq:
                break
    finally:
        await gen.aclose()

    assert all(s > last_seq for s in seen_seqs)  # ничего из "увиденного до сна" не повторилось
    assert len(seen_seqs) == len(set(seen_seqs))  # без дублей
    assert seen_seqs == sorted(seen_seqs)  # без дыр в порядке (монотонно по seq)

    body = await get_outbox(public_client, outbox_asleep)
    assert body["status"] == "sent"  # отправка, проспанная клиентом, всё-таки дошла и попала в догон
