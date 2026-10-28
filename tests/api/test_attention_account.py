"""Ф13-1 A (docs/architecture.md): внимание по аккаунту — ключ `account:<id>` политики
проекта сильнее вида и `default` (в т.ч. задач трекеров), слабее ручного режима; действует только в своём
проекте; счётчики `unread_react` и главная — тем же предикатом; PATCH принимает и удаляет ключ.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import admin, attention, queries, security
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from .conftest import insert_account, insert_channel, insert_message, insert_project

UI = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


def _policy(db, pid: int, changes: dict) -> dict | None:
    return db.write_sync(lambda conn: admin.set_project_attention(conn, pid, changes=changes))


def _modes(db, pid: int) -> dict[int, str]:
    with db.reader() as conn:
        return {c["id"]: c["attention"] for c in queries.list_channels(conn, UI, pid)}


@pytest.fixture
def world(db):
    """Проект 3-подобный: почта Timeweb (аккаунт `mail5`), Gmail (другой аккаунт), Telegram."""
    pid = insert_project(db, name="Umbrella")
    other = insert_project(db, name="Другой")
    mail5 = insert_account(db, source="mail")
    gmail = insert_account(db, source="mail")
    tg = insert_account(db, source="telegram")
    ch = {
        "mail5": insert_channel(db, mail5, project_id=pid, ext_id="t5", kind="mail_thread"),
        "gmail": insert_channel(db, gmail, project_id=pid, ext_id="tg1", kind="mail_thread"),
        "card": insert_channel(db, gmail, project_id=pid, ext_id="trello:c:1", kind="card"),
        "dm": insert_channel(db, tg, project_id=pid, ext_id="u1", kind="dm"),
        "mail5_card": insert_channel(db, mail5, project_id=pid, ext_id="trello:c:2", kind="card"),
        "other_mail5": insert_channel(db, mail5, project_id=other, ext_id="t5b", kind="mail_thread"),
    }
    _policy(db, pid, {"default": "info", f"account:{mail5}": "react"})
    return {"pid": pid, "other": other, "mail5": mail5, "gmail": gmail, "tg": tg, "ch": ch}


def test_a_account_key_beats_kind_and_default(db, world):
    modes = _modes(db, world["pid"])
    ch = world["ch"]
    assert modes[ch["mail5"]] == "react"  # ключ аккаунта
    assert modes[ch["gmail"]] == "info"  # другой аккаунт — `default`
    assert modes[ch["card"]] == "info"  # задача трекера Gmail — встроенное `info`
    assert modes[ch["dm"]] == "react"  # личка — встроенное
    assert modes[ch["mail5_card"]] == "react"  # ключ аккаунта сильнее и для задач трекеров


def test_a_account_key_beats_explicit_kind_key(db, world):
    _policy(db, world["pid"], {"mail_thread": "info"})
    modes = _modes(db, world["pid"])
    assert modes[world["ch"]["mail5"]] == "react"
    assert modes[world["ch"]["gmail"]] == "info"


def test_b_manual_mode_beats_account_key(db, world):
    cid = world["ch"]["mail5"]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET attention = 'info' WHERE id = ?", (cid,))
    )
    assert _modes(db, world["pid"])[cid] == "info"


def test_c_account_key_of_other_project_does_not_apply(db, world):
    # Ключ аккаунта задан в проекте `pid`; беседа того же аккаунта в другом проекте — встроенное почты.
    _policy(db, world["other"], {"default": "info"})
    assert _modes(db, world["other"])[world["ch"]["other_mail5"]] == "info"
    _policy(db, world["other"], {"default": None})
    assert _modes(db, world["other"])[world["ch"]["other_mail5"]] == "react"
    # Муть и Неразобранное — сильнее ключа.
    policy = json.dumps({"attention": {f"account:{world['mail5']}": "info", "default": "info"}})
    # Неразобранное (Ф57-1: почта — react) — политика проекта не читается вовсе
    assert attention.effective_mode(0, 0, None, None, "mail_thread", None, policy, world["mail5"]) == "react"
    assert attention.effective_mode(1, 0, None, 3, "mail_thread", None, policy, world["mail5"]) == "muted"
    assert attention.effective_mode(0, 0, None, 3, "mail_thread", None, policy, world["mail5"]) == "info"
    assert attention.effective_mode(0, 0, None, 3, "mail_thread", None, policy, world["gmail"]) == "info"
    assert attention.effective_mode(0, 0, None, 3, "mail_thread", None, policy) == "info"


def test_d_unread_react_counters_and_home_follow_account_key(db, world):
    pid = world["pid"]
    insert_message(db, world["ch"]["mail5"], ext_id="1", ts=1000)
    insert_message(db, world["ch"]["gmail"], ext_id="1", ts=1000)
    with db.reader() as conn:
        projects = {p["id"]: p for p in queries.list_projects(conn, UI)}
        channels = {c["id"]: c for c in queries.list_channels(conn, UI, pid)}
        home = {row["channel"]["id"] for row in queries.home_react(conn, UI)}
    assert channels[world["ch"]["mail5"]]["unread_react"] == 1
    assert channels[world["ch"]["gmail"]]["unread_react"] == 0
    assert projects[pid]["unread_react"] == 1
    assert world["ch"]["mail5"] in home and world["ch"]["gmail"] not in home
    # Снять ключ — почта Timeweb уходит в `default=info`, счётчики сходятся.
    _policy(db, pid, {f"account:{world['mail5']}": None})
    with db.reader() as conn:
        projects = {p["id"]: p for p in queries.list_projects(conn, UI)}
        home = {row["channel"]["id"] for row in queries.home_react(conn, UI)}
    assert projects[pid]["unread_react"] == 0
    assert world["ch"]["mail5"] not in home


def test_e_normalize_accepts_account_keys_only_positive_ids():
    assert attention.normalize_policy_changes({"account:5": "react", "account:12": None}) == {
        "account:5": "react",
        "account:12": None,
    }
    for bad in ("account:0", "account:-1", "account:x", "account:", "account:05", "acc:5", "account:5:1"):
        with pytest.raises(ValueError):
            attention.normalize_policy_changes({bad: "react"})
    with pytest.raises(ValueError):
        attention.normalize_policy_changes({"account:5": "loud"})


async def test_e_admin_patch_sets_and_removes_account_key(db):
    pid = insert_project(db, name="P")
    notifier = Notifier()
    app = create_internal_app(
        db, clock=lambda: 1_700_000_000.0, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        r = await c.patch(
            f"/internal/v1/admin/projects/{pid}",
            json={"attention": {"default": "info", "account:5": "react"}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["attention_policy"] == {"default": "info", "account:5": "react"}
        listed = {p["id"]: p for p in (await c.get("/internal/v1/admin/projects", headers=headers)).json()}
        assert listed[pid]["attention_policy"]["account:5"] == "react"
        r = await c.patch(
            f"/internal/v1/admin/projects/{pid}", json={"attention": {"account:5": None}}, headers=headers
        )
        assert r.json()["attention_policy"] == {"default": "info"}
        bad = await c.patch(
            f"/internal/v1/admin/projects/{pid}", json={"attention": {"account:0": "react"}}, headers=headers
        )
        assert bad.status_code == 422
