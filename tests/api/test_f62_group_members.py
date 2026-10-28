"""Ф62-6, просьба интерфейса (docs/architecture.md):

- `account_id` в строке беседы `/sync` (в списке и `GET /channels/{id}` он уже был);
- `GET /channels/{id}/group-members`: последний снимок состава группы WhatsApp (итог задания `members`);
  `?refresh=1` — только кука интерфейса, задание `members` коннектору без карточки (одно открытое на беседу),
  темп чтения, как у `link`; кука — номер целиком, bearer `draft` — только маска и без номерного jid;
  имена — из хаба (контакт, личка), иначе из моста; не группа — 422, не видна — 404."""

from hub.core.db import next_seq

from .test_f62_group_ops import (  # noqa: F401 — фикстуры
    API,
    GROUP,
    _create_body,
    _lease,
    _op,
    _report,
    _token,
    ic,
    internal_app,
    procs,
    ui,
    wa,
    world,
)
from .test_hub_sessions import _h

__all__ = ["ic", "internal_app", "procs", "ui", "wa", "world"]  # фикстуры из test_f62_group_ops

LID = "123456789012345@lid"
ME = "79000010001@s.whatsapp.net"
PETR = "79000001122@s.whatsapp.net"  # личка «Пётр» фикстуры `wa`
ANNA = "79000004455@s.whatsapp.net"


async def _uget(ui, path, params=None):
    return await ui.c.get(path, params=params, headers=ui.headers)


def _members_result() -> dict:
    return {
        "members": [
            {"jid": ME, "pn": "79000010001", "name": "Я", "admin": True, "me": True},
            {"jid": PETR, "pn": "79000001122", "name": "Пётр (мост)", "admin": False, "me": False},
            {"jid": LID, "pn": "79000004455", "name": "Анна", "admin": False, "me": False},
            {"jid": LID},  # дубль — мимо
            {"jid": "кривой", "pn": "1"},  # кривой jid — мимо
            "не словарь",
        ]
    }


async def test_sync_channel_row_carries_account_id(ui, db, clock, world, wa):
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET seq = ? WHERE id = ?", (next_seq(conn), wa["group"])
        )
    )
    r = await _uget(ui, f"{API}/sync", params={"since": 0})
    assert r.status_code == 200, r.text
    rows = {c["id"]: c for c in r.json()["channels"]}
    assert rows[wa["group"]]["account_id"] == wa["acc"]
    r = await _uget(ui, f"{API}/channels/{wa['group']}")
    assert r.json()["account_id"] == wa["acc"]
    body = (await _uget(ui, f"{API}/projects/{world['p3']}/channels")).json()
    rows = body["channels"] if isinstance(body, dict) else body
    assert {c["id"]: c["account_id"] for c in rows}[wa["group"]] == wa["acc"]


async def test_group_members_refresh_and_snapshot(client, ui, db, clock, world, wa, ic):
    w = world
    url = f"{API}/channels/{wa['group']}/group-members"
    r = await _uget(ui, url)
    assert r.status_code == 200 and r.json() == {
        "channel_id": wa["group"],
        "members": [],
        "updated_ts": None,
        "refresh": None,
    }
    r = await client.get(url, params={"refresh": 1}, headers=_h(w["tok"]))
    assert r.status_code == 403  # обновить — только интерфейс
    r = await _uget(ui, url, params={"refresh": 1})
    assert r.status_code == 200, r.text
    refresh = r.json()["refresh"]
    assert refresh["status"] == "approved"
    r = await _uget(ui, url, params={"refresh": 1})
    assert r.json()["refresh"]["op_id"] == refresh["op_id"]  # одно открытое задание на беседу
    [job] = await _lease(ic, wa)
    assert (job["id"], job["kind"], job["ext_id"]) == (refresh["op_id"], "members", GROUP)
    assert "exclude" not in job
    insert_contact = db.write_sync
    insert_contact(
        lambda conn: conn.execute(
            "INSERT INTO main.contact (account_id, ext_id, name) VALUES (?, ?, 'Анна Петрова')",
            (wa["acc"], LID),
        )
    )
    clock.advance_ms(1000)
    await _report(ic, wa, job, result=_members_result())
    full = (await _uget(ui, url)).json()
    assert full["refresh"] is None and full["updated_ts"] is not None
    assert full["members"] == [
        {
            "jid": ME,
            "name": "Я",
            "phone_masked": "+7 ••• ••• 00 01",
            "is_admin": True,
            "is_me": True,
            "phone": "+79000010001",
        },
        {
            "jid": PETR,
            "name": "Пётр",  # имя лички хаба важнее имени моста
            "phone_masked": "+7 ••• ••• 11 22",
            "is_admin": False,
            "is_me": False,
            "phone": "+79000001122",
        },
        {
            "jid": LID,
            "name": "Анна Петрова",  # контакт хаба
            "phone_masked": "+7 ••• ••• 44 55",
            "is_admin": False,
            "is_me": False,
            "phone": "+79000004455",
        },
    ]
    r = await client.get(url, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    masked = r.json()["members"]
    assert "79000001122" not in r.text and "79000010001" not in r.text and "79000004455" not in r.text
    assert [m["jid"] for m in masked] == [None, None, LID]  # номерной jid — это номер
    assert all("phone" not in m for m in masked)


async def test_group_members_refusals(client, ui, db, clock, world, wa):
    w = world
    r = await _uget(ui, f"{API}/channels/{wa['dm']}/group-members")
    assert r.status_code == 422  # личка — не группа
    r = await _uget(ui, f"{API}/channels/{w['ch']}/group-members")
    assert r.status_code == 422  # группа Telegram — не WhatsApp
    r = await _uget(ui, f"{API}/channels/999999/group-members")
    assert r.status_code == 404
    scoped, _ = _token(db, name="mcp:iip", project_ids=[w["p3"]])
    r = await client.get(f"{API}/channels/{wa['loose']}/group-members", headers=_h(scoped))
    assert r.status_code == 404  # группа без проекта токену проекта не видна
    r = await client.get(f"{API}/channels/{wa['group']}/group-members", headers=_h(scoped))
    assert r.status_code == 200
    r = await client.post(
        f"{API}/group-ops", json={"kind": "members", "channel_id": wa["group"]}, headers=_h(w["tok"])
    )
    assert r.status_code == 422  # `members` через /group-ops не заводится


async def test_members_read_is_not_held_by_write_gap(client, ui, db, clock, world, wa, ic):
    """Чтение состава ждёт только `LINK_GAP_MS` после пишущей операции, а не минуту `GAP_MS`."""
    w = world
    made = await _op(client, w, _create_body(w))
    await ui.post(f"{API}/group-ops/{made['id']}/approve")
    [job] = await _lease(ic, wa)
    await _report(ic, wa, job, result={"chat": "120363000000000999@g.us", "participants": []})
    await _uget(ui, f"{API}/channels/{wa['group']}/group-members", params={"refresh": 1})
    clock.advance_ms(11_000)
    [job2] = await _lease(ic, wa)
    assert job2["kind"] == "members"
