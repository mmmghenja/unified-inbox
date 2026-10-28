"""Ф46-1 (`docs/architecture.md`): личка автору сообщения группы Telegram — сквозь публичный
API, outbox и `TelegramConnector` с `FakeGateway`: заготовка -> подтверждение -> доступ через сообщение ->
отправка -> привязка заготовки (переименование или слияние с пришедшей за это время личкой); отказ «сообщение
недоступно» виден причиной в заготовке. Имя и id автора не попадают в журнал."""

import json
import logging

import pytest
from connectors.telegram.helpers import FakeGateway, make_channel, make_dialog, make_user

from hub.core import ingest

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


AUTHOR = 5550001
GROUP_ID = 4242
GROUP_EXT = "-1000000004242"
MSG_EXT = "812"
MSG_GONE = "сообщение группы недоступно (удалено или нас нет в группе) — написать автору через него нельзя"


def _now(clock) -> int:
    return int(clock.value * 1000)


def _ingest(db, clock, account_id, event) -> None:
    db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=[event], cursor=None, now_ms=_now(clock)
        )
    )


async def _stand(admin_client, public_client, internal_transport, db, clock, tmp_path):
    account_id, secret = await create_tg_account(admin_client)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = json_set(COALESCE(config_json, '{}'), '$.pace_window', "
            "'00:00-24:00') WHERE id = ?",
            (account_id,),
        )
    )
    project_id = await create_project(admin_client, name="Umbrella")
    token = await create_token(admin_client, scopes=("read", "draft"), project_ids=[project_id])
    _ingest(
        db,
        clock,
        account_id,
        {
            "type": "upsert",
            "channel": {"ext_id": GROUP_EXT, "kind": "group", "title": "Продавцы"},
            "ext_id": MSG_EXT,
            "ts": _now(clock),
            "sender": {"ext_id": str(AUTHOR), "name": "Пенопласт"},
            "is_me": False,
            "kind": "text",
            "text": "кто делает подписи?",
            "mentions_me": False,
        },
    )
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET project_id = ?", (project_id,)))
    with db.reader() as conn:
        (message_id,) = conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (MSG_EXT,)).fetchone()
    gateway = FakeGateway()
    gateway.now_ms = _now(clock)
    gateway.add_dialog(make_dialog(make_channel(GROUP_ID, title="Продавцы", megagroup=True), top_message=812))
    connector = make_tg_connector(
        internal_transport,
        secret=secret,
        gateway=gateway,
        account_id=account_id,
        clock=clock,
        state_path=tmp_path / "state.json",
    )
    return {
        "account_id": account_id,
        "project_id": project_id,
        "token": token,
        "message_id": message_id,
        "gateway": gateway,
        "connector": connector,
        "db": db,
        "clock": clock,
    }


async def _draft_and_approve(public_client, st) -> dict:
    r = await public_client.post(
        "/api/v1/drafts/new",
        json={
            "account_id": st["account_id"],
            "project_id": st["project_id"],
            "to": {"kind": "tg_msg_author", "message_id": st["message_id"]},
            "text": "Здравствуйте!",
        },
        headers={"Authorization": f"Bearer {st['token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    st["cookie"], _csrf = await login_via_code(public_client, st["db"], st["clock"])
    r = await public_client.post(
        f"/api/v1/outbox/{body['outbox_id']}/approve", json={}, headers=csrf_headers(st["cookie"])
    )
    assert r.status_code == 200, r.text
    return body


def _channel(db, channel_id) -> dict:
    with db.reader() as conn:
        ext_id, title, archived, meta_json = conn.execute(
            "SELECT ext_id, title, archived, meta_json FROM main.channel WHERE id = ?", (channel_id,)
        ).fetchone()
    return {"ext_id": ext_id, "title": title, "archived": archived, "meta": json.loads(meta_json or "{}")}


def _author(*, min=False):
    user = make_user(AUTHOR, first_name="Пено", last_name="Пласт")
    user.access_hash = 777
    user.min = min
    return user


async def test_author_through_message_sends_and_renames_stub(
    admin_client, public_client, internal_transport, db, clock, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG, logger="hub.connectors")
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    gateway = st["gateway"]
    gateway.add_message_author(int(GROUP_EXT), int(MSG_EXT), _author(min=True))
    body = await _draft_and_approve(public_client, st)
    assert _channel(db, body["channel_id"])["title"] == "Пенопласт"

    assert await st["connector"].step_outbox() == 1
    assert gateway.user_from_message_calls == [(int(GROUP_EXT), int(MSG_EXT), AUTHOR)]
    assert gateway.send_calls == 1 and gateway.sent[0]["marked_id"] == AUTHOR

    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "sent"
    ch = _channel(db, body["channel_id"])
    assert (ch["ext_id"], ch["title"], ch["archived"]) == (str(AUTHOR), "Пено Пласт", 0)
    # `new_draft.to` переживает привязку (по нему считается предел и сверяется повтор по ключу).
    assert ch["meta"]["new_draft"]["to"]["kind"] == "tg_msg_author"
    connector_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("hub.connectors"))
    assert "автор сообщения группы — min" in connector_log
    for secret in (str(AUTHOR), "Пено", "Пласт"):
        assert secret not in connector_log


async def test_dm_appeared_meanwhile_stub_merges_into_it(
    admin_client, public_client, internal_transport, db, clock, tmp_path
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    st["gateway"].add_message_author(int(GROUP_EXT), int(MSG_EXT), _author())
    body = await _draft_and_approve(public_client, st)
    # Пока черновик ждал, автор сам написал в личку — настоящая беседа уже есть.
    clock.advance_ms(1000)
    _ingest(
        db,
        clock,
        st["account_id"],
        {
            "type": "upsert",
            "channel": {"ext_id": str(AUTHOR), "kind": "dm", "title": "Пено Пласт"},
            "ext_id": "1",
            "ts": _now(clock),
            "sender": {"ext_id": str(AUTHOR), "name": "Пено Пласт"},
            "is_me": False,
            "kind": "text",
            "text": "добрый день",
            "mentions_me": False,
        },
    )
    assert await st["connector"].step_outbox() == 1
    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "sent"
    stub = _channel(db, body["channel_id"])
    assert stub["archived"] == 1 and stub["meta"]["merged_into"] == str(AUTHOR)
    with db.reader() as conn:
        real, outbox_channel = conn.execute(
            "SELECT c.id, o.channel_id FROM main.channel c, main.outbox o WHERE c.ext_id = ? AND o.id = ?",
            (str(AUTHOR), body["outbox_id"]),
        ).fetchone()
    assert outbox_channel == real != body["channel_id"]


async def test_message_gone_fails_with_reason_in_stub(
    admin_client, public_client, internal_transport, db, clock, tmp_path
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    body = await _draft_and_approve(public_client, st)  # поддельный сервер автора сообщения не знает
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert (ob["status"], ob["error"]) == ("failed", MSG_GONE)
    assert st["gateway"].send_calls == 0
    assert _channel(db, body["channel_id"])["archived"] == 1
