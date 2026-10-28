"""Ф99-2 (docs/architecture.md, поправки П5–П8): `POST /channels/{id}/mute` — подписанная сессия
глушит беседу и снимает свою заглушку.

- П7: доступ — подписка сессии с ролью на проект беседы или на проект, слышащий «Неразобранное» её аккаунта;
  чужой проект — 404 (токен беседу не видит) или 403 (видит, но подписки на тот проект нет); без роли — 403.
- П6: ручной режим владельца — 409. П5: заглушку владельца сессия не снимает (409) и не перехватывает; любая правка
  `muted` мимо `hub_mute` сбрасывает `muted_by`.
- Предел 100 в час на подписку (429), аудит `channel_mute` без текста причины.

Стиль — как test_f37_session_handled.py."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import channels, mute, security, subs
from hub.core.audit import write_audit

from .conftest import BASE_URL, csrf_headers, insert_channel, insert_project, login_via_code

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
ROLE = "маркетолог"
NOTE = "рассылка курса, к проекту не относится"


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "4"}


def _token(db, *, name="mcp:all", scopes=("read", "draft", "organize"), project_ids=None):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind="interactive", scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _account(db):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', '{}')"
            ).lastrowid
        )
    )


@pytest.fixture
def world(db, tmp_path, procs):
    dir3, dir4 = tmp_path / "p3", tmp_path / "p4"
    dir3.mkdir()
    dir4.mkdir()
    p3 = insert_project(db, name="Umbrella", workdir=str(dir3))
    p4 = insert_project(db, name="Initech", workdir=str(dir4))
    for pid in (p3, p4):
        db.write_sync(lambda conn, pid=pid: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = _account(db)
    acc2 = _account(db)
    ch = insert_channel(db, acc, project_id=p3, ext_id="g1", kind="group", title="Рассылка")
    loose = insert_channel(db, acc2, project_id=None, ext_id="g2", kind="group", title="Без проекта")
    token, token_id = _token(db)
    return {
        "p3": p3,
        "p4": p4,
        "dir3": str(dir3),
        "dir4": str(dir4),
        "acc2": acc2,
        "ch": ch,
        "loose": loose,
        "tok": token,
        "tok_id": token_id,
    }


@pytest.fixture
async def ui(app, db, clock):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        headers = csrf_headers(cookie)

        class _Ui:
            async def get(self, path):
                return await c.get(path)

            async def post(self, path, body):
                return await c.post(path, json=body, headers=headers)

            async def patch(self, path, body):
                return await c.patch(path, json=body, headers=headers)

        yield _Ui()


def _sess() -> dict:
    return {"pid": A[0], "proc_start": A[1], "session_id": A[2]}


async def _sub(client, w, *, role=ROLE, cwd=None, token=None) -> int:
    session = {**_sess(), "cwd": cwd or w["dir3"], "kind": "interactive", "name": None, "name_source": None}
    body: dict = {"session": session}
    if role is not None:
        body["role"] = role
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _mute(client, w, *, channel=None, on=True, note=NOTE, token=None, session=True):
    body: dict = {"on": on, "note": note}
    if session:
        body["session"] = _sess()
    return await client.post(
        f"/api/v1/channels/{channel or w['ch']}/mute", json=body, headers=_h(token or w["tok"])
    )


def _state(db, channel_id) -> tuple:
    with db.reader() as conn:
        return conn.execute("SELECT muted, muted_by FROM main.channel WHERE id = ?", (channel_id,)).fetchone()


def _audits(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, result, channel_id, detail_json FROM main.audit WHERE action = 'channel_mute' "
            "ORDER BY id"
        ).fetchall()


def _refused(db, r, status, channel_id, words=None, muted=0):
    assert r.status_code == status, r.text
    if words is not None:
        assert words in r.json()["detail"], r.text
    assert _state(db, channel_id)[0] == muted
    assert _audits(db) == []


# --- своя заглушка: поставить, увидеть, снять ---------------------------------------------------------------


async def test_mute_own_then_unmute_with_audit_and_ui_fields(client, ui, db, world):
    w = world
    sub_id = await _sub(client, w)
    r = await _mute(client, w)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["before"] == {"muted": False, "muted_by_role": None}
    assert body["after"] == {"muted": True, "muted_by_role": ROLE}
    muted, raw = _state(db, w["ch"])
    by = json.loads(raw)
    assert muted == 1 and by["sub"] == sub_id and by["role"] == ROLE and by["note"] == NOTE

    channel = (await ui.get(f"/api/v1/channels/{w['ch']}")).json()
    assert channel["muted"] is True
    assert channel["muted_by_role"] == ROLE and channel["muted_by_note"] == NOTE
    sync = (await client.get("/api/v1/sync?since=0&limit=500", headers=_h(w["tok"]))).json()
    synced = {c["id"]: c for c in sync["channels"]}
    assert synced[w["ch"]]["muted_by_role"] == ROLE

    r = await _mute(client, w, on=False, note="снова пишут по делу")
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True and r.json()["after"] == {"muted": False, "muted_by_role": None}
    assert _state(db, w["ch"]) == (0, None)
    assert (await ui.get(f"/api/v1/channels/{w['ch']}")).json()["muted_by_role"] is None

    audits = _audits(db)
    assert [a[1] for a in audits] == ["changed", "changed"]
    detail = json.loads(audits[0][3])
    assert detail == {"on": True, "sub_id": sub_id, "role": ROLE, "note_len": len(NOTE)}
    assert NOTE not in audits[0][3]  # причина — в `muted_by`, не в аудите


def test_second_line_of_role_is_labelled():
    rec = subs.SubRec(*([0] * 13), role=ROLE, role_key=ROLE, role_no=2)
    assert mute.role_label(rec) == f"{ROLE} 2"
    assert mute.role_label(rec._replace(role_no=1)) == ROLE


# --- П7: доступ ---------------------------------------------------------------------------------------------


async def test_ui_session_refused(ui, db, world):
    r = await ui.post(f"/api/v1/channels/{world['ch']}/mute", {"on": True, "note": NOTE})
    _refused(db, r, 403, world["ch"], "только для сессий")


async def test_foreign_project_token_gets_404(client, db, world):
    w = world
    tok4, _ = _token(db, name="mcp:initech", scopes=("read", "draft"), project_ids=[w["p4"]])
    await _sub(client, w, cwd=w["dir4"], token=tok4)
    r = await _mute(client, w, token=tok4)
    _refused(db, r, 404, w["ch"])


async def test_subscription_on_other_project_gets_403(client, db, world):
    w = world
    await _sub(client, w, cwd=w["dir4"])  # подписка на Initech, беседа — Umbrella
    r = await _mute(client, w)
    _refused(db, r, 403, w["ch"], "подписка сессии на проект беседы с ролью")


async def test_subscription_without_role_gets_403(client, db, world):
    w = world
    await _sub(client, w, role=None)
    r = await _mute(client, w)
    _refused(db, r, 403, w["ch"])


async def test_no_session_gets_403(client, db, world):
    w = world
    await _sub(client, w)
    r = await _mute(client, w, session=False)
    _refused(db, r, 403, w["ch"], "сессия не определена")


async def test_unsorted_of_heard_account_allowed(client, db, world):
    w = world
    await _sub(client, w)
    r = await _mute(client, w, channel=w["loose"])
    _refused(db, r, 403, w["loose"], "ни один проект не слышит")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET unsorted_accounts = ? WHERE id = ?", (json.dumps([w["acc2"]]), w["p3"])
        )
    )
    r = await _mute(client, w, channel=w["loose"])
    assert r.status_code == 200, r.text
    assert _state(db, w["loose"])[0] == 1 and json.loads(_state(db, w["loose"])[1])["role"] == ROLE


async def test_unsorted_heard_by_other_project_only_gets_403(client, db, world):
    w = world
    await _sub(client, w)  # подписка на p3, а «Неразобранное» аккаунта слышит p4
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET unsorted_accounts = ? WHERE id = ?", (json.dumps([w["acc2"]]), w["p4"])
        )
    )
    r = await _mute(client, w, channel=w["loose"])
    _refused(db, r, 403, w["loose"], "слышит")


async def test_archived_and_short_note_refused(client, db, world):
    w = world
    await _sub(client, w)
    r = await _mute(client, w, note="ок")
    _refused(db, r, 422, w["ch"], "note")
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (w["ch"],)))
    r = await _mute(client, w)
    _refused(db, r, 422, w["ch"], "архив")


# --- П6 и П5: ручной режим и заглушка владельца ------------------------------------------------------------------


async def test_manual_attention_gets_409(client, ui, db, world):
    w = world
    await _sub(client, w)
    assert (await ui.patch(f"/api/v1/channels/{w['ch']}", {"attention": "info"})).status_code == 200
    r = await _mute(client, w)
    _refused(db, r, 409, w["ch"], "режим беседы задал владелец")


async def test_owner_mute_not_removed_nor_taken_over(client, ui, db, world):
    w = world
    await _sub(client, w)
    assert (await ui.patch(f"/api/v1/channels/{w['ch']}", {"muted": True})).status_code == 200
    r = await _mute(client, w, on=False)
    _refused(db, r, 409, w["ch"], "заглушил владелец", muted=1)
    assert _state(db, w["ch"]) == (1, None)

    r = await _mute(client, w, on=True)  # уже заглушена владельцем — пометка сессии не ставится
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is False and r.json()["after"] == {"muted": True, "muted_by_role": None}
    assert _state(db, w["ch"]) == (1, None)
    assert (await _mute(client, w, on=False)).status_code == 409


async def test_ui_patch_resets_muted_by(client, ui, db, world):
    w = world
    await _sub(client, w)
    assert (await _mute(client, w)).status_code == 200
    assert _state(db, w["ch"])[1] is not None
    # Владелец в интерфейсе подтвердил заглушку — теперь она его, сессия её не снимет.
    assert (await ui.patch(f"/api/v1/channels/{w['ch']}", {"muted": True})).status_code == 200
    assert _state(db, w["ch"]) == (1, None)
    r = await _mute(client, w, on=False)
    assert r.status_code == 409 and "заглушил владелец" in r.json()["detail"]


def test_update_channel_resets_muted_by(db, world):
    """П5: общий путь правки `muted` (интерфейс, админ, заявки) сбрасывает пометку сессии в любую сторону."""
    w = world
    for value in (True, False):
        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.channel SET muted = 1, muted_by = '{\"sub\": 1, \"role\": \"x\"}' WHERE id = ?",
                (w["ch"],),
            )
        )
        db.write_sync(lambda conn, v=value: channels.update_channel(conn, w["ch"], muted=v, now_ms=1))
        assert _state(db, w["ch"]) == (1 if value else 0, None)


# --- предел ------------------------------------------------------------------------------------------------


async def test_rate_limit_per_subscription(client, db, clock, world):
    w = world
    sub_id = await _sub(client, w)
    now_ms = int(clock() * 1000)

    def _fill(conn):
        for i in range(mute.MUTE_PER_HOUR):
            write_audit(
                conn,
                now_ms=now_ms - 1000,
                actor="interactive",
                api_token_id=w["tok_id"],
                client_name=None,
                request_id=f"r{i}",
                action="channel_mute",
                result="unchanged",
                channel_id=w["ch"],
                detail={"on": True, "sub_id": sub_id, "role": ROLE, "note_len": 5},
            )

    db.write_sync(_fill)
    r = await _mute(client, w)
    assert r.status_code == 429, r.text
    assert _state(db, w["ch"])[0] == 0
    assert len(_audits(db)) == mute.MUTE_PER_HOUR
