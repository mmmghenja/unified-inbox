"""Ф34-3 (`docs/architecture.md`, B3–B5): первое сообщение в Telegram по номеру телефона — сквозь
публичный API, outbox и `TelegramConnector` с `FakeGateway`. Номер не остаётся ни в БД, ни в аудите, ни в
журналах; импорт контактов не вызывается. Номера — выдуманные."""

import json
import logging

import pytest
from connectors.telegram.helpers import FakeGateway, make_user
from telethon import errors

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


PHONE = "+79000000001"
DIGITS = "79000000001"
TAIL = "9000000001"
MASK = "+7 ••• ••• 00 01"


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
    gateway = FakeGateway()
    gateway.now_ms = int(clock.value * 1000)
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
        "gateway": gateway,
        "connector": connector,
        "db": db,
        "clock": clock,
    }


async def _draft_and_approve(public_client, st, *, to="+7 (900) 000-00-01"):
    r = await public_client.post(
        "/api/v1/drafts/new",
        json={
            "account_id": st["account_id"],
            "project_id": st["project_id"],
            "to": to,
            "text": "Здравствуйте!",
        },
        headers={"Authorization": f"Bearer {st['token']}"},
    )
    assert r.status_code == 200, r.text
    assert TAIL not in r.text
    body = r.json()
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


def _where_number(db) -> list[str]:
    found = []
    with db.reader() as conn:
        for schema in ("main", "work"):
            tables = [
                r[0]
                for r in conn.execute(
                    f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%' "
                    "AND name NOT LIKE '%_fts%'"
                ).fetchall()
            ]
            for table in tables:
                cur = conn.execute(f'SELECT * FROM {schema}."{table}"')
                cols = [c[0] for c in cur.description]
                for row in cur.fetchall():
                    for col, value in zip(cols, row, strict=True):
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", "replace")
                        if isinstance(value, str) and TAIL in value:
                            found.append(f"{schema}.{table}.{col}")
    return sorted(set(found))


async def test_phone_found_sends_binds_and_erases_number(
    admin_client, public_client, internal_transport, db, clock, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    gateway = st["gateway"]
    gateway.add_phone(DIGITS, make_user(555, first_name="Пётр", last_name="Иванов"))
    body = await _draft_and_approve(public_client, st)
    assert _channel(db, body["channel_id"])["title"] == MASK
    assert _where_number(db) == ["main.channel.meta_json"]

    assert await st["connector"].step_outbox() == 1
    assert gateway.resolve_phone_calls == [DIGITS] and gateway.resolve_calls == []
    assert gateway.send_calls == 1 and gateway.sent[0]["marked_id"] == 555

    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "sent"
    ch = _channel(db, body["channel_id"])
    assert (ch["ext_id"], ch["title"], ch["archived"]) == ("555", "Пётр Иванов", 0)
    assert "attrs" not in ch["meta"] and ch["meta"]["new_draft"]["to"] == {"kind": "tg_phone"}
    assert _where_number(db) == []  # ни в `meta_json`, ни в аудите, ни в `contact`, ни в outbox
    assert TAIL not in caplog.text
    assert TAIL not in (tmp_path / "state.json").read_text(encoding="utf-8")


async def test_phone_not_found_fails_with_reason_and_erases_number(
    admin_client, public_client, internal_transport, db, clock, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "failed"
    assert ob["error"] == "номер не найден в Telegram или скрыт настройками приватности"
    ch = _channel(db, body["channel_id"])
    assert ch["archived"] == 1 and ch["title"] == MASK
    assert st["gateway"].send_calls == 0
    assert _where_number(db) == [] and TAIL not in caplog.text


async def test_phone_floodwait_pauses_only_this_draft(
    admin_client, public_client, internal_transport, db, clock, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    st["gateway"].add_phone(DIGITS, make_user(555, first_name="Пётр"))
    st["gateway"].resolve_phone_faults.append(errors.FloodWaitError(request=None, capture=30))
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "queued"
    with db.reader() as conn:
        not_before, flood_until = conn.execute(
            "SELECT o.not_before_ts, a.flood_until FROM main.outbox o "
            "JOIN main.account a ON a.id = o.account_id WHERE o.id = ?",
            (body["outbox_id"],),
        ).fetchone()
    assert not_before >= int(clock.value * 1000) + 30_000
    assert flood_until is None  # пауза только этого черновика, обычная переписка аккаунта идёт
    assert st["gateway"].send_calls == 0 and TAIL not in caplog.text


async def test_connector_limit_holds_draft_without_request_or_account_pause(
    admin_client, public_client, internal_transport, db, clock, tmp_path
):
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    now_ms = int(clock.value * 1000)
    (tmp_path / "state.json").write_text(
        json.dumps({"phone_resolve": {"calls": [now_ms - 1000] * 5, "pause_until_ms": 0}}), encoding="utf-8"
    )
    st["gateway"].add_phone(DIGITS, make_user(555, first_name="Пётр"))
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    ob = await get_outbox(public_client, body["outbox_id"])
    assert ob["status"] == "queued"
    assert st["gateway"].resolve_phone_calls == [] and st["gateway"].send_calls == 0
    with db.reader() as conn:
        flood_until = conn.execute("SELECT flood_until FROM main.account").fetchone()[0]
    assert flood_until is None


async def test_unhandled_send_exception_log_has_no_number(
    admin_client, public_client, internal_transport, db, clock, tmp_path, caplog, monkeypatch
):
    """Необработанное исключение отправки уходит в журнал каркаса с трассировкой — номера там нет."""
    caplog.set_level(logging.DEBUG)
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    st["gateway"].add_phone(DIGITS, make_user(555, first_name="Пётр"))

    async def _boom(*args, **kwargs):
        raise RuntimeError("сервер ответил непонятно")

    monkeypatch.setattr(st["gateway"], "send_message", _boom)
    body = await _draft_and_approve(public_client, st)
    await st["connector"].step_outbox()
    assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "unknown"
    assert "упал без результата" in caplog.text
    assert TAIL not in caplog.text


async def test_limit_waiting_spends_no_attempts_and_all_drafts_go_out(
    admin_client, public_client, internal_transport, db, clock, tmp_path
):
    """Ревью Ф34-3: лимит выбран, подтверждены ещё три черновика — каждый ждёт своей очереди сколько угодно
    выдач (`postponed`), попытки не тратятся, аккаунт не останавливается, все уходят."""
    st = await _stand(admin_client, public_client, internal_transport, db, clock, tmp_path)
    now_ms = int(clock.value * 1000)
    calls = [now_ms - (5 - i) * 2 * 60 * 60 * 1000 for i in range(5)]
    (tmp_path / "state.json").write_text(
        json.dumps({"phone_resolve": {"calls": calls, "pause_until_ms": 0}}), encoding="utf-8"
    )
    bodies = []
    for i in range(3):
        st["gateway"].add_phone(f"7900000010{i}", make_user(600 + i, first_name=f"P{i}"))
        r = await public_client.post(
            "/api/v1/drafts/new",
            json={
                "account_id": st["account_id"],
                "project_id": st["project_id"],
                "to": f"+7900000010{i}",
                "text": "Здравствуйте!",
            },
            headers={"Authorization": f"Bearer {st['token']}"},
        )
        assert r.status_code == 200, r.text
        bodies.append(r.json())
    cookie, _csrf = await login_via_code(public_client, db, clock)  # вход владельца — после черновиков сессии
    for body in bodies:
        r = await public_client.post(
            f"/api/v1/outbox/{body['outbox_id']}/approve", json={}, headers=csrf_headers(cookie)
        )
        assert r.status_code == 200, r.text
    for _ in range(40):
        with db.reader() as conn:
            rows = conn.execute("SELECT status, not_before_ts, attempts FROM main.outbox").fetchall()
            assert conn.execute("SELECT flood_until FROM main.account").fetchone()[0] is None
        waiting = [r[1] or 0 for r in rows if r[0] == "queued"]
        assert all(r[2] <= 1 for r in rows)
        if not waiting:
            break
        clock.advance_ms(max(min(waiting) - int(clock.value * 1000), 0) + 1000)
        st["gateway"].now_ms = int(clock.value * 1000)
        await st["connector"].step_outbox()
    for body in bodies:
        assert (await get_outbox(public_client, body["outbox_id"]))["status"] == "sent"
    assert len(st["gateway"].resolve_phone_calls) == 3
