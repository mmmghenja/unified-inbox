"""Ф12-1 B2 (тест 14 + П8): первое сообщение новому человеку в Telegram по @username — сквозь публичный API,
outbox и `TelegramConnector` с `FakeGateway` (`docs/architecture.md`)."""

import json

import pytest
from connectors.telegram.helpers import (
    FakeGateway,
    make_channel,
    make_user,
    peer_of,
)
from telethon import errors

from hub.connectors.fake import ConnectorCrash
from hub.connectors.telegram import connector as tg_connector
from hub.connectors.telegram import convert

from .conftest import create_project, create_token, csrf_headers, get_outbox, login_via_code
from .test_telegram_send import create_tg_account, make_tg_connector


class _NoPause:
    """Нижняя граница случайности графика: первая строка уходит без паузы (24.10 пауза `[0, gap_min]` —
    случайная; эти тесты проверяют не график, а путь первого сообщения)."""

    def randint(self, a: int, b: int) -> int:
        return a


@pytest.fixture(autouse=True)
def _no_first_pause(monkeypatch):
    monkeypatch.setattr("hub.core.outbox._rng", _NoPause())


async def _stand(admin_client, public_client, internal_transport, db, clock):
    account_id, secret = await create_tg_account(admin_client)
    # Ф12-2: окно графика — круглые сутки, первый слот — «сейчас» (график здесь не проверяется).
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = json_set(COALESCE(config_json, '{}'), '$.pace_window', "
            "'00:00-24:00') WHERE id = ?",
            (account_id,),
        )
    )
    project_id = await create_project(admin_client, name="Umbrella")
    token = await create_token(admin_client, scopes=("read", "draft"), project_ids=[project_id])
    gateway = FakeGateway()
    gateway.now_ms = int(clock.value * 1000)
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    return {
        "account_id": account_id,
        "project_id": project_id,
        "token": token,
        "gateway": gateway,
        "connector": connector,
        "db": db,
        "clock": clock,
    }


async def _draft_and_approve(public_client, st, *, to="@Petr_Ivanov", text="Здравствуйте! Пишу по подписи."):
    r = await public_client.post(
        "/api/v1/drafts/new",
        json={"account_id": st["account_id"], "project_id": st["project_id"], "to": to, "text": text},
        headers={"Authorization": f"Bearer {st['token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # вход владельца — после черновика: с кукой сессии клиент — уже UI, а `drafts/new` только для bearer (П7)
    if "cookie" not in st:
        st["cookie"], _csrf = await login_via_code(public_client, st["db"], st["clock"])
    r = await public_client.post(
        f"/api/v1/outbox/{body['outbox_id']}/approve", json={}, headers=csrf_headers(st["cookie"])
    )
    assert r.status_code == 200, r.text
    return body


def _channel(db, channel_id):
    with db.reader() as conn:
        ext_id, title, archived, meta_json = conn.execute(
            "SELECT ext_id, title, archived, meta_json FROM main.channel WHERE id = ?", (channel_id,)
        ).fetchone()
    return {"ext_id": ext_id, "title": title, "archived": archived, "meta": json.loads(meta_json or "{}")}


async def test_14_user_found_sends_to_his_peer_and_binds(
    admin_client, public_client, internal_transport, db, clock
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    gateway = st["gateway"]
    petr = make_user(555, first_name="Пётр", last_name="Иванов", username="Petr_Ivanov")
    gateway.add_username("Petr_Ivanov", petr)
    body = await _draft_and_approve(public_client, st, to="@petr_ivanov")

    assert await st["connector"].step_outbox() == 1
    assert gateway.resolve_calls == ["petr_ivanov"]
    assert gateway.send_calls == 1
    sent = gateway.sent[0]
    assert sent["marked_id"] == 555
    with db.reader() as conn:
        send_token = conn.execute(
            "SELECT send_token FROM main.outbox WHERE id = ?", (body["outbox_id"],)
        ).fetchone()[0]
    assert sent["random_id"] == convert.random_id_from_token(send_token)

    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "sent" and ob["ext_id"] == sent["ext_id"]
    ch = _channel(db, body["channel_id"])
    assert ch["ext_id"] == str(convert.peer_ext_id(peer_of(petr))) == "555"
    assert ch["title"] == "Пётр Иванов"
    assert "new_to" not in (ch["meta"].get("attrs") or {})


async def test_14_bot_is_a_user_too(admin_client, public_client, internal_transport, db, clock):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    st["gateway"].add_username(
        "helper_bot", make_user(556, first_name="Бот", username="helper_bot", bot=True)
    )
    body = await _draft_and_approve(public_client, st, to="t.me/helper_bot")
    await st["connector"].step_outbox()
    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "sent"


@pytest.mark.parametrize(
    ("setup", "error_part"),
    [
        (lambda gw: None, "нет такого @username"),
        (
            lambda gw: gw.add_username("Petr_Ivanov", make_channel(900, title="Канал", broadcast=True)),
            "не человек",
        ),
        (
            lambda gw: gw.add_username("Petr_Ivanov", make_channel(901, title="Чат", megagroup=True)),
            "не человек",
        ),
        (
            lambda gw: gw.resolve_faults.append(errors.UsernameInvalidError(request=None)),
            "нет такого @username",
        ),
    ],
)
async def test_14_not_a_person_or_missing_is_permanent(
    admin_client, public_client, internal_transport, db, clock, setup, error_part
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    setup(st["gateway"])
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "failed"
    assert error_part in ob["error"]
    assert st["gateway"].send_calls == 0
    assert _channel(db, body["channel_id"])["archived"] == 1  # П2


async def test_14_floodwait_on_resolve_is_temporary_not_account_flood(
    admin_client, public_client, internal_transport, db, clock
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    st["gateway"].add_username("Petr_Ivanov", make_user(555, first_name="Пётр", username="Petr_Ivanov"))
    flood = errors.FloodWaitError(request=None, capture=30)
    st["gateway"].resolve_faults.append(flood)
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "queued"
    with db.reader() as conn:
        not_before, flood_until = conn.execute(
            "SELECT o.not_before_ts, a.flood_until FROM main.outbox o "
            "JOIN main.account a ON a.id = o.account_id WHERE o.id = ?",
            (body["outbox_id"],),
        ).fetchone()
    assert not_before >= int(clock.value * 1000) + 30_000
    assert flood_until is None  # пауза только этого черновика, не всего аккаунта
    assert st["gateway"].send_calls == 0


async def test_14_maybe_sent_searches_peer_history_first(
    admin_client, public_client, internal_transport, db, clock
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    gateway = st["gateway"]
    gateway.add_username("Petr_Ivanov", make_user(555, first_name="Пётр", username="Petr_Ivanov"))
    body = await _draft_and_approve(public_client, st)
    gateway.queue_send_faults("crash_after_delivery")
    with pytest.raises(ConnectorCrash):
        await st["connector"].step_outbox()
    assert len(gateway.sent) == 1
    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "sending"

    clock.advance_ms(130_000)  # лиз Telegram — 120 с
    gateway.now_ms = int(clock.value * 1000)
    assert await st["connector"].step_outbox() == 1
    assert gateway.send_calls == 1  # второго запроса не было — нашли в истории ЭТОГО peer
    assert (555, None, 50) in gateway.get_messages_calls
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "sent" and ob["ext_id"] == gateway.sent[0]["ext_id"]
    assert _channel(db, body["channel_id"])["ext_id"] == "555"  # привязка и после поиска в истории


@pytest.mark.parametrize(
    "fault",
    [
        errors.UserPrivacyRestrictedError(request=None),
        errors.ForbiddenError(request=None, message="PRIVACY_PREMIUM_REQUIRED"),
    ],
)
async def test_14_privacy_is_permanent_with_clear_text(
    admin_client, public_client, internal_transport, db, clock, fault
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock)
    st["gateway"].add_username("Petr_Ivanov", make_user(555, first_name="Пётр", username="Petr_Ivanov"))
    st["gateway"].queue_send_faults(fault)
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "failed"
    assert ob["error"] == "человек принимает личку только от контактов/Premium"


# --- П8: временный ext_id не попадает в split_channel_ext_id ни в одном из трёх мест ------------------------


def _lease(**extra) -> dict:
    return {
        "outbox_id": 1,
        "channel_ext_id": "new:" + "a" * 32,
        "text": "привет",
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "b" * 32,
        "mode": "send",
        "send_started_ts": 1_700_000_000_000,
        "attempts": 1,
        "lease_gen": 1,
        "maybe_sent": False,
        "known_own_ext_ids": [],
        "files": None,
        "new_to": {"kind": "tg_username", "username": "Petr_Ivanov"},
        **extra,
    }


@pytest.fixture
def guarded_split(monkeypatch):
    real = convert.split_channel_ext_id

    def _split(channel_ext_id):
        assert not channel_ext_id.startswith("new:"), "временный ext_id попал в split_channel_ext_id"
        return real(channel_ext_id)

    monkeypatch.setattr(tg_connector.convert, "split_channel_ext_id", _split)


async def test_p8_split_never_sees_temp_ext_id(admin_client, internal_transport, clock, guarded_split):
    account_id, secret = await create_tg_account(admin_client)
    gateway = FakeGateway()
    gateway.add_username("Petr_Ivanov", make_user(555, first_name="Пётр", username="Petr_Ivanov"))
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    result = await connector.send(_lease())
    assert result.outcome == "sent" and result.peer_ext_id == "555"

    # Ф14-2 B3: файл в новую беседу идёт обычным путём на разрешённый peer (здесь байтов у ядра нет —
    # `temporary` до отправки); временный ext_id в `split_channel_ext_id` по-прежнему не попадает.
    file = {"size": 3, "mime": "image/png", "name": "a.png", "kind": "photo", "sha256": "x"}
    assert (await connector.send(_lease(files=[file], outbox_id=2))).outcome == "temporary"
    assert (await connector.send(_lease(files=[file, file], outbox_id=3))).outcome == "temporary"

    found = await connector.reconcile(_lease(mode="reconcile"))
    assert (found.outcome, found.peer_ext_id, found.peer_title) == ("sent", "555", "Пётр")
    missing = await connector.reconcile(_lease(mode="reconcile", text="такого не было"))
    assert missing.outcome == "unknown"

    # без `new_to`, но с временным ext_id — `permanent`, а не разбор ext_id
    no_to = await connector.send(_lease(new_to=None))
    assert no_to.outcome == "permanent"
    assert (await connector.reconcile(_lease(new_to=None, mode="reconcile"))).outcome == "unknown"
