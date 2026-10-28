"""Ф62-6 (docs/architecture.md и поправки П2, П12, П14): вступление и выход для групп
WhatsApp на прежних очередях `join_request` / `leave_request`, отказ поиска Telegram аккаунту WhatsApp.

- цель вступления WhatsApp — только ссылка-приглашение `chat.whatsapp.com/<код>`; аккаунт выбирается по сети
  цели (П14), `@имя` аккаунтом WhatsApp — отказ с понятной причиной;
- выключатель проекта «вступать сразу» на WhatsApp не действует (П2);
- вступившая группа WhatsApp в проект заявки — НЕ заглушена (П12); Telegram — как раньше;
- суточные потолки WhatsApp — свои (30 предпросмотров, 10 вступлений);
- выход из группы WhatsApp — та же заявка, что у Telegram."""

import json

import pytest

from hub.core import join as join_mod

from .conftest import insert_channel, insert_project
from .test_joins import (
    SESSION,
    _account,
    _audit,
    _bearer,
    _channel,
    _create,
    _decide,
    _events,
    _lease,
    _now_ms,
    _peek,
    _policy,
    _report,
    _req,
    _sub,
    _token,
    _ui,
    _up,
    ic,
    internal_app,
    procs,
)

__all__ = ["ic", "internal_app", "procs"]  # фикстуры из test_joins

API = "/api/v1"
LINK = "https://chat.whatsapp.com/AbCdEfGh12345678"
GROUP = "120363000000000001@g.us"


@pytest.fixture
async def wa_env(db, ic, clock):
    """Проект, аккаунт WhatsApp с секретом коннектора (единственный в хабе), токен сессии `mcp:all`."""
    project = insert_project(db, name="Hooli")
    acc, h = _account(db, source="whatsapp")
    created = _token(db)
    return {"project": project, "acc": acc, "h": h, "token": created.token, "token_id": created.token_id}


def test_parse_wa_target():
    assert join_mod.parse_wa_target(LINK) == ("invite", "AbCdEfGh12345678")
    assert join_mod.parse_wa_target("chat.whatsapp.com/AbCdEfGh12345678") == ("invite", "AbCdEfGh12345678")
    assert join_mod.parse_wa_target("https://www.chat.whatsapp.com/AbCdEfGh12345678/") == (
        "invite",
        "AbCdEfGh12345678",
    )
    for other in (
        "@durov",
        "https://t.me/+AbCdEfGh",
        "https://example.com/x",
        None,
        5,
        "chat.whatsapp.com x",
    ):
        assert join_mod.parse_wa_target(other) is None
    for broken in (
        "https://chat.whatsapp.com/",
        "https://chat.whatsapp.com/a/b",
        "https://chat.whatsapp.com/!!",
    ):
        with pytest.raises(ValueError, match="WhatsApp"):
            join_mod.parse_wa_target(broken)
    assert join_mod.parse_any(LINK) == ("whatsapp", "invite", "AbCdEfGh12345678")
    assert join_mod.parse_any("@durov") == ("telegram", "username", "durov")


async def test_account_is_picked_by_network_of_the_target(client, db, wa_env):
    item = await _create(client, wa_env["token"], wa_env["project"], LINK)
    assert (item["account_id"], item["status"]) == (wa_env["acc"], "checking")
    # Telegram-цель при единственном аккаунте WhatsApp — «нет аккаунта Telegram»; с явным аккаунтом WhatsApp —
    # прямая причина.
    r = await _create(client, wa_env["token"], wa_env["project"], "@durov", status=422)
    assert r["detail"] == "account_id: в хабе нет аккаунта Telegram"
    r = await _create(
        client, wa_env["token"], wa_env["project"], "@durov", status=422, account_id=wa_env["acc"]
    )
    assert r["detail"] == join_mod._NOT_WA_TARGET  # noqa: SLF001
    tg, _h = _account(db)
    r = await _create(client, wa_env["token"], wa_env["project"], LINK, status=422, account_id=tg)
    assert "аккаунт WhatsApp" in r["detail"]
    # Кривая ссылка WhatsApp — отказ разбора.
    r = await _create(client, wa_env["token"], wa_env["project"], "https://chat.whatsapp.com/a/b", status=422)
    assert "WhatsApp" in r["detail"]


async def test_project_auto_join_does_not_apply_to_whatsapp(client, ic, db, clock, wa_env, procs):
    _policy(db, wa_env["project"], "auto")
    _sub(db, project_id=wa_env["project"], token_id=wa_env["token_id"])
    item = await _create(client, wa_env["token"], wa_env["project"], LINK, session=SESSION)
    [leased] = await _lease(ic, wa_env["h"])
    assert leased["id"] == item["id"] and leased["step"] == "peek" and leased["target_kind"] == "invite"
    out = await _report(ic, wa_env["h"], leased, peek=_peek(GROUP, kind="wa_group", title="hub test"))
    assert out["result"] == "pending"  # П2: на Telegram был бы `approved` (auto)
    assert json.loads(_req(db, item["id"]).peek_json)["kind"] == "wa_group"


async def test_joined_whatsapp_group_is_bound_unmuted(client, ic, db, clock, wa_env):
    ui = await _ui(client, db, clock)
    item = await _create(client, wa_env["token"], wa_env["project"], LINK)
    [peek] = await _lease(ic, wa_env["h"])
    await _report(ic, wa_env["h"], peek, peek=_peek(None, kind="wa_group", title="hub test"))
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, wa_env["h"])
    assert job["step"] == "join"
    clock.advance_ms(1000)
    await _report(ic, wa_env["h"], job, result="joined", ext_id=GROUP, title="hub test")
    # Беседа группы есть сразу по отчёту, до первого сообщения (WhatsApp истории не шлёт).
    ch = _channel(db, GROUP)
    assert ch is not None and ch["kind"] == "group"
    assert (ch["project_id"], ch["map_source"], ch["muted"]) == (wa_env["project"], "manual", 0)
    assert _req(db, item["id"]).channel_id == ch["id"]
    clock.advance_ms(1000)
    await _events(ic, wa_env["h"], [_up(GROUP, "1", _now_ms(clock), kind="group")])
    assert _channel(db, GROUP)["id"] == ch["id"]  # первое сообщение — в ту же беседу
    ch = _channel(db, GROUP)
    assert (ch["project_id"], ch["map_source"], ch["muted"]) == (wa_env["project"], "manual", 0)
    assert _req(db, item["id"]).channel_id == ch["id"]
    audit = [d for _r, _a, d in _audit(db, "channel_update") if d.get("join_id") == item["id"]]
    assert audit and audit[0]["muted"] is False


def _leased_joins(db, account_id, now, n):
    def _fn(conn):
        for i in range(n):
            conn.execute(
                "INSERT INTO main.join_request (account_id, project_id, target_kind, target, status, "
                "created_ts, updated_ts, peek_leased_ts, join_leased_ts, seq) "
                "VALUES (?, 1, 'invite', ?, 'joined', ?, ?, ?, ?, 1)",
                (account_id, f"code{i}", now, now, now - (n - i) * 1000, now - (n - i) * 1000),
            )

    db.write_sync(_fn)


def test_whatsapp_daily_caps_are_own(db, clock):
    now = _now_ms(clock)
    insert_project(db, name="P")
    wa, _ = _account(db, source="whatsapp")
    tg, _ = _account(db)
    _leased_joins(db, wa, now, 10)
    _leased_joins(db, tg, now, 10)
    with db.reader() as conn:
        wa_pace = join_mod._pace(conn, wa, now + 3 * join_mod.JOIN_GAP_MS)  # noqa: SLF001
        tg_pace = join_mod._pace(conn, tg, now + 3 * join_mod.JOIN_GAP_MS)  # noqa: SLF001
    assert wa_pace.join_due > now + join_mod.DAY_MS - 20_000  # 10 за сутки — потолок WhatsApp
    assert tg_pace.join_due <= now + 3 * join_mod.JOIN_GAP_MS  # у Telegram потолок 20
    assert wa_pace.peek_due <= now + 3 * join_mod.JOIN_GAP_MS  # 10 предпросмотров из 30


async def test_leave_whatsapp_group(client, db, clock, wa_env):
    ch = insert_channel(db, wa_env["acc"], project_id=wa_env["project"], ext_id=GROUP, kind="group")
    r = await client.post(f"{API}/leaves", json={"channel_id": ch}, headers=_bearer(wa_env["token"]))
    assert r.status_code == 201 and r.json()["status"] == "pending", r.text
    dm = insert_channel(
        db, wa_env["acc"], project_id=wa_env["project"], ext_id="79000001122@s.whatsapp.net", kind="dm"
    )
    r = await client.post(f"{API}/leaves", json={"channel_id": dm}, headers=_bearer(wa_env["token"]))
    assert r.status_code == 422
    assert r.json()["detail"] == "выйти можно только из группы или канала Telegram или группы WhatsApp"


async def test_tg_search_refuses_whatsapp_account(client, db, clock, wa_env):
    r = await client.post(
        f"{API}/tg/search",
        json={"query": "abc", "account_id": wa_env["acc"]},
        headers=_bearer(wa_env["token"]),
    )
    assert (
        r.status_code == 422
        and r.json()["detail"] == "account_id: поиск и чтение — только аккаунтом Telegram"
    )


async def test_already_member_whatsapp_gets_channel_at_peek(client, ic, db, clock, wa_env):
    """Предпросмотр: владелец уже в группе (`already`) — беседа тоже создаётся отчётом, одна."""
    item = await _create(client, wa_env["token"], wa_env["project"], LINK)
    [peek] = await _lease(ic, wa_env["h"])
    await _report(ic, wa_env["h"], peek, peek=_peek(GROUP, member=True, kind="wa_group", title="hub test"))
    assert _req(db, item["id"]).status == "already"
    ch = _channel(db, GROUP)
    assert ch is not None and _req(db, item["id"]).channel_id == ch["id"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = ?", (GROUP,)).fetchone()[0] == 1


async def test_telegram_join_still_waits_for_history(client, ic, db, clock):
    """Решение 11.5 для Telegram не меняется: отчёт `joined` беседу не создаёт — она приходит с историей."""
    project = insert_project(db, name="Маркетинг")
    tg, h = _account(db)
    created = _token(db)
    ui = await _ui(client, db, clock)
    item = await _create(client, created.token, project, "@some_group", account_id=tg)
    [peek] = await _lease(ic, h)
    await _report(ic, h, peek, peek=_peek(None, kind="megagroup", title="Группа"))
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, h)
    await _report(ic, h, job, result="joined", ext_id="-1001234567", title="Группа")
    assert _channel(db, "-1001234567") is None
