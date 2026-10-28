"""Ф15-1 (docs/architecture.md): реакции от сессий и реакции в письмах — ядро и
ручка, раздел F пункты 1–8, с поправками П1–П7 (они главнее текста спеки).

Сессия — bearer `interactive` со скоупом `draft` и `session` (pid + `starttime` + разговор) в теле; живость
процесса подменяется (`subs.read_proc_start`), случайная пауза — `reactions._rng`. Постановка идёт через
публичное приложение, выдача и отчёт — через внутреннее (секрет коннектора) на той же БД и тех же часах."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth, security, subs
from hub.core import db as db_module
from hub.core import reactions as reactions_mod
from hub.core.app_internal import create_internal_app
from hub.core.audit import write_audit
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db, db_of
from hub.core.feed import Notifier

from .conftest import ORIGIN, csrf_headers, insert_account, insert_project, login_via_code

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
SESSION = {"pid": PID, "proc_start": START, "session_id": SID}
T0 = 1_700_000_000_000  # часы `FakeClock` в мс


# --- обвязка ------------------------------------------------------------------------------------------------


class FixedRng:
    """Подменная случайность: `randint` отдаёт `lo + offset` (не больше `hi`) и помнит границы."""

    def __init__(self, offset_ms: int = 60_000) -> None:
        self.offset_ms = offset_ms
        self.calls: list[tuple[int, int]] = []

    def randint(self, lo: int, hi: int) -> int:
        self.calls.append((lo, hi))
        return min(lo + self.offset_ms, hi)


@pytest.fixture
def rng(monkeypatch) -> FixedRng:
    fixed = FixedRng()
    monkeypatch.setattr(reactions_mod, "_rng", fixed)
    return fixed


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _account(db, *, source="telegram", db_name="main", config=None) -> tuple[int, dict]:
    account_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, ?, ?)",
                (source, db_name, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


def _up(ch, ext_id, ts, *, kind="dm", is_me=False, sender=None, attrs=None, mail_to=None):
    channel: dict = {"ext_id": ch, "kind": kind}
    if attrs is not None:
        channel["attrs"] = attrs
    event = {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "t",
        "mentions_me": False,
    }
    if sender is not None:
        event["sender"] = {"ext_id": sender, "name": sender, "handle": sender, "is_me": is_me}
    if mail_to is not None:
        event["mail_to"] = mail_to
    return event


async def _events(ic, h, events, *, dead=0):
    r = await ic.post(f"{INTERNAL}/events", headers=h, json={"events": events})
    assert r.status_code == 200 and r.json()["dead"] == dead, r.text
    return r.json()


def _channel_id(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (ext_id,),
        ).fetchone()[0]


def _mid(db, channel_id, ext_id):
    schema = db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext_id),
        ).fetchone()[0]


def _set_project(db, channel_id, project_id):
    schema = db_of(channel_id)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {schema}.channel SET project_id = ? WHERE id = ?",  # noqa: S608
            (project_id, channel_id),
        )
    )


def _set_reactions_json(db, message_id, reactions):
    schema = db_of(message_id)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {schema}.message SET reactions_json = ? WHERE id = ?",  # noqa: S608
            (json.dumps(reactions, ensure_ascii=False), message_id),
        )
    )


def _row(db, message_id):
    schema = db_of(message_id)
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.reaction_ack WHERE message_id = ?", (message_id,))  # noqa: S608
        row = cur.fetchone()
        if row is None:
            return None
        out = dict(zip([c[0] for c in cur.description], row, strict=True))
        out["ops"] = json.loads(out["ops_json"])
        return out


def _marks(db, message_id):
    with db.reader() as conn:
        return reactions_mod.session_emojis(conn, message_id)


def _now_ms(clock) -> int:
    return int(clock() * 1000)


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    )


def _sub(db, *, project_id, token_id, channel_id=None, pid=PID, start=START, sid=SID):
    """Живая подписка сессии на проект (или на беседу — `channel_id`)."""
    if channel_id is None:
        sql = (
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts) VALUES (?, ?, ?, ?, ?, '/tmp', 0)"
        )
        params = (project_id, token_id, pid, start, sid)
    else:
        sql = (
            "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
            "session_proc_start, session_id, session_cwd, created_ts) VALUES (?, ?, ?, ?, ?, ?, '/tmp', 0)"
        )
        params = (channel_id, project_id, token_id, pid, start, sid)
    db.write_sync(lambda conn: conn.execute(sql, params))


def _policy(db, project_id, *, sessions=True, delay_s=None):
    db.write_sync(
        lambda conn: reactions_mod.set_project_reactions(conn, project_id, sessions=sessions, delay_s=delay_s)
    )


async def _ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


async def _react(client, ui, message_id, emoji, on=True, *, status=200):
    r = await client.post(
        f"{API}/messages/{message_id}/reactions", json={"emoji": emoji, "on": on}, headers=ui
    )
    assert r.status_code == status, r.text
    return r.json()


async def _sreact(client, token, message_id, emoji, on=True, *, session=SESSION, status=200):
    body: dict = {"emoji": emoji, "on": on}
    if session is not None:
        body["session"] = session
    r = await client.post(
        f"{API}/messages/{message_id}/reactions",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
    )
    assert r.status_code == status, r.text
    return r.json()


async def _lease(ic, h):
    r = await ic.post(f"{INTERNAL}/reactions/lease", headers=h, json={})
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, message_id, lease_gen, outcome, *, reason=None, status=200):
    body: dict = {"lease_gen": lease_gen, "outcome": outcome}
    if reason is not None:
        body["reason"] = reason
    r = await ic.post(f"{INTERNAL}/reactions/{message_id}/report", headers=h, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], json.loads(r[1])) for r in rows]


@pytest.fixture
async def tg(db, ic, clock, procs, rng):
    """Проект с включёнными реакциями сессий, беседа Telegram в нём, сессия с подпиской на проект.
    Сообщения: `new` — 10 с назад, `old` — сутки назад."""
    project = insert_project(db, name="Umbrella")
    _policy(db, project)
    acc, h = _account(db)
    now = _now_ms(clock)
    await _events(ic, h, [_up("c1", "1", now - 86_400_000), _up("c1", "2", now - 10_000)])
    ch = _channel_id(db, "c1")
    _set_project(db, ch, project)
    created = _token(db)
    _sub(db, project_id=project, token_id=created.token_id)
    return {
        "project": project,
        "acc": acc,
        "h": h,
        "ch": ch,
        "old": _mid(db, ch, "1"),
        "new": _mid(db, ch, "2"),
        "new_ts": now - 10_000,
        "token": created.token,
        "token_id": created.token_id,
    }


@pytest.fixture
async def sl(db, ic, clock, procs, rng):
    """То же для Slack (несколько своих реакций)."""
    project = insert_project(db, name="Globex")
    _policy(db, project)
    acc, h = _account(db, source="slack", db_name="work")
    now = _now_ms(clock)
    await _events(ic, h, [_up("C1", "1790000000.000100", now - 10_000, kind="group")])
    ch = _channel_id(db, "C1", "work")
    _set_project(db, ch, project)
    created = _token(db)
    _sub(db, project_id=project, token_id=created.token_id)
    return {
        "project": project,
        "acc": acc,
        "h": h,
        "ch": ch,
        "m": _mid(db, ch, "1790000000.000100"),
        "ts": now - 10_000,
        "token": created.token,
        "token_id": created.token_id,
    }


# --- F1: кто может и пауза ------------------------------------------------------------------------------


async def test_f1_project_off_403_on_queued_with_pause(client, db, clock, tg, rng):
    _policy(db, tg["project"], sessions=False)
    r = await _sreact(client, tg["token"], tg["new"], "👀", status=403)
    assert r["detail"] == "session reactions off"
    assert _row(db, tg["new"]) is None

    _policy(db, tg["project"], sessions=True, delay_s=(120, 300))
    r = await _sreact(client, tg["token"], tg["new"], "👀")
    assert r == {"status": "queued", "not_before_ts": tg["new_ts"] + 120_000 + 60_000}
    assert rng.calls == [(120_000, 300_000)]  # пауза — в [min, max] проекта
    row = _row(db, tg["new"])
    assert row["origin"] == "session" and row["token_id"] == tg["token_id"] and row["ops"] == [["👀", True]]
    audit = _audit(db, "reaction_set")
    assert audit[-1][0] == "queued"
    assert audit[-1][1] == {
        "message_id": tg["new"],
        "on": True,
        "origin": "session",
        "emoji": "👀",  # Ф19-1 D
        "not_before_ts": row["not_before_ms"],
        "pid": PID,
        "proc_start": START,
    }


async def test_f1_old_message_now_plus_delay(client, db, clock, tg):
    r = await _sreact(client, tg["token"], tg["old"], "👀")
    assert r["not_before_ts"] == _now_ms(clock) + 1500


@pytest.mark.parametrize(
    "name, kind, scopes, detail",
    [
        # Ф57-2: дежурной больше нет; старый токен `mcp:wake-<id>` реагировать не может.
        ("mcp:wake-3", "interactive", ("read", "draft"), "не дежурная"),
        ("mcp:orchestrator", "interactive", ("read", "draft", "organize", "orchestrate"), "не дежурная"),
        ("svc", "background", ("read", "draft"), "не дежурная"),
        ("mcp:all", "interactive", ("read",), "draft"),
    ],
)
async def test_f1_wake_orchestrator_background_no_draft_403(client, db, tg, name, kind, scopes, detail):
    created = _token(db, name=name, kind=kind, scopes=scopes)
    _sub(db, project_id=tg["project"] + 100, token_id=created.token_id)  # подписка не спасает
    r = await _sreact(client, created.token, tg["new"], "👀", status=403)
    assert detail in r["detail"]
    assert _row(db, tg["new"]) is None
    assert _audit(db, "reaction_denied")[-1][1]["message_id"] == tg["new"]


async def test_p7_subscription_required(client, db, tg, procs):
    other = insert_project(db, name="Другой")
    _policy(db, other)
    # без session в теле
    r = await _sreact(client, tg["token"], tg["new"], "👀", session=None, status=403)
    assert "сессии" in r["detail"]
    # чужая сессия того же токена (другой разговор) и мёртвый процесс
    await _sreact(
        client, tg["token"], tg["new"], "👀", session={**SESSION, "session_id": "other"}, status=403
    )
    procs.pop(PID)
    r = await _sreact(client, tg["token"], tg["new"], "👀", status=403)
    procs[PID] = START
    # подписка того же токена на другой проект — не годится
    created = _token(db, name="mcp:x")
    _sub(db, project_id=other, token_id=created.token_id)
    r = await _sreact(client, created.token, tg["new"], "👀", status=403)
    assert r["detail"] == "реакции — только из сессии, подписанной на этот проект"
    # подписка на саму беседу — годится
    _sub(db, project_id=None, token_id=created.token_id, channel_id=tg["ch"])
    assert (await _sreact(client, created.token, tg["new"], "👀"))["status"] == "queued"


async def test_p7_ui_with_session_422(client, db, clock, tg):
    ui = await _ui(client, db, clock)
    r = await client.post(
        f"{API}/messages/{tg['new']}/reactions",
        json={"emoji": "👍", "on": True, "session": SESSION},
        headers=ui,
    )
    assert r.status_code == 422


# --- F2: вторая операция сессии и клик владельца ----------------------------------------------------------------


async def test_f2_second_session_op_not_earlier_and_ui_click_wins(client, ic, db, clock, sl, rng):
    first = await _sreact(client, sl["token"], sl["m"], "👀")
    point = first["not_before_ts"]
    rng.offset_ms = 0  # новая случайность не должна сдвинуть строку раньше
    second = await _sreact(client, sl["token"], sl["m"], "✅")
    assert second["not_before_ts"] == point
    assert _row(db, sl["m"])["ops"] == [["👀", True], ["✅", True]]
    assert len(rng.calls) == 1  # случайная точка — одна на строку

    # операции дошли: пометки 👀 и ✅
    clock.advance_ms(point - _now_ms(clock))
    (item,) = await _lease(ic, sl["h"])
    assert item["origin"] == "session" and item["session_emojis"] == sorted(["👀", "✅"])
    await _report(ic, sl["h"], sl["m"], item["lease_gen"], "done")
    assert _marks(db, sl["m"]) == {"👀", "✅"}

    # сессия снова ставит — и владелец кликает: операции сессии выброшены, задержка клика, пометка снята
    await _sreact(client, sl["token"], sl["m"], "🔥")
    ui = await _ui(client, db, clock)
    await _react(client, ui, sl["m"], "👀", on=False)
    row = _row(db, sl["m"])
    assert row["origin"] == "ui" and row["token_id"] is None
    assert row["ops"] == [["👀", False]]
    assert row["not_before_ms"] == _now_ms(clock) + 1500
    assert _marks(db, sl["m"]) == {"✅"}


async def test_p1_session_refused_while_owner_pending(client, db, clock, sl):
    ui = await _ui(client, db, clock)
    await _react(client, ui, sl["m"], "👍")
    client.cookies.clear()  # дальше — только bearer
    r = await _sreact(client, sl["token"], sl["m"], "👀", status=409)
    assert r["detail"] == "owner pending"
    assert _row(db, sl["m"])["ops"] == [["👍", True]]


# --- F3: реакции владельца не трогаются -----------------------------------------------------------------------


async def test_f3_telegram_owner_reaction_blocks_session(client, db, tg):
    _set_reactions_json(db, tg["new"], [{"emoji": "👍", "count": 1, "me": True}])
    r = await _sreact(client, tg["token"], tg["new"], "👀", status=409)
    assert r["detail"] == "owner reaction"
    # кастомная моя (без эмодзи) — тоже владельца
    _set_reactions_json(db, tg["new"], [{"custom": "555", "count": 1, "me": True}])
    await _sreact(client, tg["token"], tg["new"], "👀", status=409)


async def test_f3_telegram_session_mark_allows_switch(client, ic, db, clock, tg):
    await _sreact(client, tg["token"], tg["old"], "👀")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, tg["h"])
    assert item["session_emojis"] == ["👀"] and item["current_mine"] == []
    await _report(ic, tg["h"], tg["old"], item["lease_gen"], "done")
    _set_reactions_json(db, tg["old"], [{"emoji": "👀", "count": 1, "me": True}])
    assert (await _sreact(client, tg["token"], tg["old"], "👌"))["status"] == "queued"
    clock.advance_ms(1500)
    (item,) = await _lease(ic, tg["h"])
    await _report(ic, tg["h"], tg["old"], item["lease_gen"], "done")
    assert _marks(db, tg["old"]) == {"👌"}  # Telegram держит одну реакцию


async def test_f3_off_foreign_409_own_ok(client, ic, db, clock, sl):
    _set_reactions_json(db, sl["m"], [{"emoji": "👍", "count": 1, "me": True}])
    r = await _sreact(client, sl["token"], sl["m"], "👍", on=False, status=409)
    assert r["detail"] == "not own"
    # своя ждущая `on` — снимать можно
    await _sreact(client, sl["token"], sl["m"], "👀")
    assert (await _sreact(client, sl["token"], sl["m"], "👀", on=False))["status"] == "queued"
    assert _row(db, sl["m"])["ops"] == [["👀", False]]


async def test_f3_telegram_off_own_marked_ok(client, db, tg):
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.reaction_by_session (message_id, emoji_key, token_id, ts) "
            "VALUES (?, '👀', ?, 0)",
            (tg["old"], tg["token_id"]),
        )
    )
    _set_reactions_json(db, tg["old"], [{"emoji": "👀", "count": 1, "me": True}])
    assert (await _sreact(client, tg["token"], tg["old"], "👀", on=False))["status"] == "queued"


async def test_p3_already_mine_without_mark(client, db, sl):
    _set_reactions_json(db, sl["m"], [{"emoji": "👍", "count": 2, "me": True}])
    assert await _sreact(client, sl["token"], sl["m"], "👍") == {"status": "already"}
    assert _row(db, sl["m"]) is None and _marks(db, sl["m"]) == set()


@pytest.mark.parametrize("outcome, reason", [("done", "owner_reacted"), ("drop", None)])
async def test_p3_no_marks_without_done(client, ic, db, clock, tg, outcome, reason):
    await _sreact(client, tg["token"], tg["old"], "👀")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, tg["h"])
    await _report(ic, tg["h"], tg["old"], item["lease_gen"], outcome, reason=reason)
    assert _marks(db, tg["old"]) == set() and _row(db, tg["old"]) is None


async def test_p3_gave_up_and_changed_gen_leave_no_marks(client, ic, db, clock, sl):
    await _sreact(client, sl["token"], sl["m"], "👀")
    clock.advance_ms(10**7)
    (item,) = await _lease(ic, sl["h"])
    await _sreact(client, sl["token"], sl["m"], "✅")  # gen сменился во время лиза
    await _report(ic, sl["h"], sl["m"], item["lease_gen"], "done")
    assert _marks(db, sl["m"]) == set()
    for _ in range(reactions_mod.MAX_ATTEMPTS):
        clock.advance_ms(10**7)
        (item,) = await _lease(ic, sl["h"])
        await _report(ic, sl["h"], sl["m"], item["lease_gen"], "retry")
    assert _row(db, sl["m"]) is None and _marks(db, sl["m"]) == set()


# --- F4: выдача коннектору ------------------------------------------------------------------------------


async def test_f4_lease_pairs_and_old_triples_read(client, ic, db, clock, sl):
    await _sreact(client, sl["token"], sl["m"], "👀")
    clock.advance_ms(10**7)
    (item,) = await _lease(ic, sl["h"])
    assert item["ops"] == [["👀", True]]
    await _report(ic, sl["h"], sl["m"], item["lease_gen"], "retry")
    # строка, записанная тройками, читается парами
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE work.reaction_ack SET ops_json = ?, pause_until_ms = 0, not_before_ms = 0 "
            "WHERE message_id = ?",
            (json.dumps([["👀", True, "s"], ["🔥", False]], ensure_ascii=False), sl["m"]),
        )
    )
    (item,) = await _lease(ic, sl["h"])
    assert item["ops"] == [["👀", True], ["🔥", False]]


async def test_p1_lease_drops_session_row_when_project_off(client, ic, db, clock, sl):
    await _sreact(client, sl["token"], sl["m"], "👀")
    _policy(db, sl["project"], sessions=False)
    clock.advance_ms(10**7)
    assert await _lease(ic, sl["h"]) == []
    assert _row(db, sl["m"]) is None


# --- F5: почта ------------------------------------------------------------------------------------------


GMAIL = {"address": "me@gmail.com", "provider": "gmail"}


async def _mail(
    db, ic, *, config=None, sender="x@gmail.com", is_me=False, mail_to=None, attrs=None, ch="777"
):
    acc, h = _account(db, source="mail", config=config if config is not None else GMAIL)
    await _events(
        ic,
        h,
        [
            _up(
                ch,
                "<a@x>",
                T0 - 5_000,
                kind="mail_thread",
                is_me=is_me,
                sender=sender,
                mail_to=mail_to,
                attrs=attrs,
            )
        ],
    )
    cid = _channel_id(db, ch)
    return acc, h, cid, _mid(db, cid, "<a@x>")


async def test_f5_mail_gmail_sender_accepted_ui_delay_10s(client, ic, db, clock):
    _acc, h, _ch, m = await _mail(db, ic)
    ui = await _ui(client, db, clock)
    assert await _react(client, ui, m, "👍") == {"status": "queued"}
    assert _row(db, m)["not_before_ms"] == _now_ms(clock) + 10_000
    clock.advance_ms(10_000)
    (item,) = await _lease(ic, h)
    assert item["ops"] == [["👍", True]] and item["current_mine"] == []


@pytest.mark.parametrize(
    "kwargs, detail",
    [
        ({"sender": "x@mail.ru"}, "not google"),
        ({"is_me": True, "sender": "me@gmail.com", "mail_to": ["a@gmail.com", "b@mail.ru"]}, "not google"),
        ({"is_me": True, "sender": "me@gmail.com"}, "not google"),  # адресатов нет
        ({"attrs": {"no_reply": True}}, "no reply"),
        ({"config": {**GMAIL, "send_enabled": False}}, "receive only"),
    ],
)
async def test_f5_mail_refusals(client, ic, db, clock, kwargs, detail):
    _acc, _h, _ch, m = await _mail(db, ic, **kwargs)
    ui = await _ui(client, db, clock)
    r = await _react(client, ui, m, "👍", status=409)
    assert r["detail"] == detail
    assert _row(db, m) is None


async def test_f5_mail_workspace_domain_of_gmail_account(client, ic, db, clock):
    _account(db, source="mail", config={"address": "e@globex.example.com", "provider": "gmail"})
    _account(db, source="mail", config={"address": "e@umbrella.example.com", "provider": "imap", "flavor": "imap"})
    _acc, _h, _ch, m = await _mail(db, ic, sender="boss@globex.example.com", ch="778")
    ui = await _ui(client, db, clock)
    assert (await _react(client, ui, m, "👍"))["status"] == "queued"
    # домен ящика вида imap — не Google
    _acc2, _h2, _ch2, m2 = await _mail(db, ic, sender="x@umbrella.example.com", ch="779")
    await _react(client, ui, m2, "👍", status=409)
    # моё письмо — адресаты из `mail_to`, все на Google
    _acc3, _h3, _ch3, m3 = await _mail(
        db, ic, is_me=True, sender="me@gmail.com", mail_to=["a@gmail.com", "b@googlemail.com"], ch="780"
    )
    assert (await _react(client, ui, m3, "🔥"))["status"] == "queued"


async def test_f5_mail_already_and_too_many(client, ic, db, clock):
    _acc, _h, _ch, m = await _mail(db, ic)
    ui = await _ui(client, db, clock)
    _set_reactions_json(db, m, [{"emoji": "👍", "count": 1, "me": True}])
    assert await _react(client, ui, m, "👍") == {"status": "already"}
    assert _row(db, m) is None
    many = [{"emoji": e, "count": 1, "me": True} for e in "😀😁😂🤣😃😄😅😆😉😊😋😎😍😘🥰😗😙🥲😚🙂"]
    assert len(many) == 20
    _set_reactions_json(db, m, many)
    r = await _react(client, ui, m, "🔥", status=409)
    assert r["detail"] == "too many"


async def test_f5_mail_off_cancels_pending_else_409(client, ic, db, clock):
    _acc, h, _ch, m = await _mail(db, ic)
    ui = await _ui(client, db, clock)
    r = await _react(client, ui, m, "👍", on=False, status=409)
    assert r["detail"] == "sent, cannot undo"
    await _react(client, ui, m, "👍")
    await _react(client, ui, m, "🔥")
    assert (await _react(client, ui, m, "👍", on=False))["status"] == "queued"
    assert _row(db, m)["ops"] == [["🔥", True]]
    assert (await _react(client, ui, m, "🔥", on=False))["status"] == "queued"
    assert _row(db, m) is None  # отменять больше нечего — строки нет
    # П4: письмо уже отправляется — отмена 409
    await _react(client, ui, m, "👍")
    clock.advance_ms(10_000)
    await _lease(ic, h)
    r = await _react(client, ui, m, "👍", on=False, status=409)
    assert r["detail"] == "sending"


async def test_f5_mail_session_uses_pause(client, ic, db, clock, procs, rng):
    project = insert_project(db, name="P")
    _policy(db, project, delay_s=(120, 300))
    _acc, _h, ch, m = await _mail(db, ic)
    _set_project(db, ch, project)
    created = _token(db)
    _sub(db, project_id=project, token_id=created.token_id)
    r = await _sreact(client, created.token, m, "👀")
    assert r["not_before_ts"] == T0 - 5_000 + 180_000


async def test_f5_default_no_pause_session_decides(client, ic, db, clock, procs, rng):
    # Владелец 29.09: паузу держит сессия по правилам проекта — у хаба по умолчанию её нет
    project = insert_project(db, name="P")
    _policy(db, project)
    _acc, _h, ch, m = await _mail(db, ic)
    _set_project(db, ch, project)
    created = _token(db)
    _sub(db, project_id=project, token_id=created.token_id)
    r = await _sreact(client, created.token, m, "👀")
    assert r["not_before_ts"] == int(clock() * 1000) + reactions_mod.DELAY_MS


# --- F6: входящие реакции писем -----------------------------------------------------------------------


def _mr(src, target, emoji, sender, *, is_me=False, ts=T0, ch="777"):
    return {
        "type": "mail_reaction",
        "channel": {"ext_id": ch, "kind": "mail_thread"},
        "target_ext_id": target,
        "src_ext_id": src,
        "emoji": emoji,
        "sender": {"ext_id": sender, "name": sender, "is_me": is_me},
        "ts": ts,
    }


def _msg(db, message_id):
    schema = db_of(message_id)
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT reactions_json, seq FROM {schema}.message WHERE id = ?",  # noqa: S608
            (message_id,),
        ).fetchone()
    return (json.loads(row[0]) if row[0] else None), row[1]


def _count_messages(db, channel_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM main.message WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]


async def test_f6_reaction_after_target(client, ic, db, clock):
    _acc, h, ch, m = await _mail(db, ic)
    _, seq0 = _msg(db, m)
    r = await _events(ic, h, [_mr("<r1@x>", "<a@x>", "👍", "p@gmail.com")])
    assert r["applied"] == 1
    reactions, seq1 = _msg(db, m)
    assert reactions == [{"emoji": "👍", "count": 1, "me": False, "by": ["p@gmail.com"]}] and seq1 > seq0
    assert _count_messages(db, ch) == 1  # не строка ленты
    # повтор — no-op; второй человек — count 2; моя — me
    r = await _events(ic, h, [_mr("<r1@x>", "<a@x>", "👍", "p@gmail.com")])
    assert r["noop"] == 1
    await _events(
        ic,
        h,
        [
            _mr("<r2@x>", "<a@x>", "👍", "q@gmail.com"),
            _mr("<r3@x>", "<a@x>", "🔥", "me@gmail.com", is_me=True),
            _mr("<r4@x>", "<a@x>", "❤️", "p@gmail.com"),
            _mr("<r5@x>", "<a@x>", "❤", "q@gmail.com"),  # без U+FE0F — та же реакция
        ],
    )
    reactions, _ = _msg(db, m)
    assert reactions == [
        {"emoji": "👍", "count": 2, "me": False, "by": ["p@gmail.com", "q@gmail.com"]},
        {"emoji": "🔥", "count": 1, "me": True},
        {"emoji": "❤️", "count": 2, "me": False, "by": ["p@gmail.com", "q@gmail.com"]},
    ]
    ui = await _ui(client, db, clock)
    r = await client.get(f"{API}/channels/{ch}/messages", headers=ui)
    assert r.status_code == 200
    (msg,) = r.json()["messages"]
    assert msg["unread"] is True  # непрочитанным стало только само письмо — реакции не в счёт
    assert msg["can_react"] is True


async def test_f6_reaction_before_target_applied_on_upsert(ic, db):
    _acc, h = _account(db, source="mail", config=GMAIL)
    await _events(ic, h, [_up("777", "<first@x>", T0 - 9_000, kind="mail_thread", sender="p@gmail.com")])
    ch = _channel_id(db, "777")
    await _events(ic, h, [_mr("<r1@x>", "<late@x>", "❤️", "p@gmail.com")])
    await _events(ic, h, [_up("777", "<late@x>", T0 - 1_000, kind="mail_thread", sender="p@gmail.com")])
    reactions, _ = _msg(db, _mid(db, ch, "<late@x>"))
    assert reactions == [{"emoji": "❤️", "count": 1, "me": False, "by": ["p@gmail.com"]}]


async def test_f6_no_channel_noop_hidden_target_nothing_bad_emoji_dead(client, ic, db, clock):
    _acc, h, ch, m = await _mail(db, ic)
    r = await _events(ic, h, [_mr("<r0@x>", "<a@x>", "👍", "p@gmail.com", ch="nope")])
    assert r["noop"] == 1
    ui = await _ui(client, db, clock)
    r = await client.delete(f"{API}/messages/{m}", headers=ui)
    assert r.status_code in (200, 204), r.text
    r = await _events(ic, h, [_mr("<r1@x>", "<a@x>", "👍", "p@gmail.com")])
    assert r["noop"] == 1
    with db.reader() as conn:
        # ревью 3: цель скрыта — только позиция для маркера прочтения, без эмодзи и отправителя
        rows = conn.execute("SELECT emoji, sender_key, is_me FROM main.mail_reaction").fetchall()
        assert rows == [("", "", 0)]
    await _events(ic, h, [_mr("<r2@x>", "<a@x>", "👍👍", "p@gmail.com")], dead=1)


async def test_p6_read_marker_on_reaction_letter(ic, db):
    _acc, h = _account(db, source="mail", config=GMAIL)
    await _events(ic, h, [_up("777", "<a@x>", T0 - 9_000, kind="mail_thread", sender="p@gmail.com")])
    ch = _channel_id(db, "777")
    await _events(ic, h, [_mr("<r1@x>", "<a@x>", "👍", "p@gmail.com", ts=T0 - 1_000)])
    r = await _events(
        ic,
        h,
        [{"type": "read", "channel": {"ext_id": "777", "kind": "mail_thread"}, "up_to_ext_id": "<r1@x>"}],
    )
    assert r["applied"] == 1
    with db.reader() as conn:
        assert (
            conn.execute("SELECT last_read_ts FROM main.channel WHERE id = ?", (ch,)).fetchone()[0]
            == T0 - 1_000
        )


async def test_review3_read_marker_on_reaction_to_hidden_target(client, ic, db, clock):
    """Ревью 3: цель реакции удалена у себя — маркер прочтения на письме-реакции всё равно двигается."""
    _acc, h = _account(db, source="mail", config=GMAIL)
    await _events(
        ic,
        h,
        [
            _up("777", "<a@x>", T0 - 9_000, kind="mail_thread", sender="p@gmail.com"),
            _up("777", "<b@x>", T0 - 8_000, kind="mail_thread", sender="p@gmail.com"),
        ],
    )
    ch = _channel_id(db, "777")
    ui = await _ui(client, db, clock)
    r = await client.delete(f"{API}/messages/{_mid(db, ch, '<b@x>')}", headers=ui)
    assert r.status_code in (200, 204), r.text
    await _events(ic, h, [_mr("<r1@x>", "<b@x>", "👍", "p@gmail.com", ts=T0 - 1_000)])
    r = await _events(
        ic,
        h,
        [{"type": "read", "channel": {"ext_id": "777", "kind": "mail_thread"}, "up_to_ext_id": "<r1@x>"}],
    )
    assert r["applied"] == 1
    with db.reader() as conn:
        assert (
            conn.execute("SELECT last_read_ts FROM main.channel WHERE id = ?", (ch,)).fetchone()[0]
            == T0 - 1_000
        )


# --- F7: лимит и политика --------------------------------------------------------------------------------


async def test_f7_rate_limit_60_per_hour_per_session(client, db, clock, sl):
    now = _now_ms(clock)

    def _fill(conn):
        for i in range(60):
            write_audit(
                conn,
                now_ms=now - 1000 - i,
                actor="interactive",
                api_token_id=sl["token_id"],
                client_name=None,
                request_id=f"r{i}",
                action="reaction_set",
                result="queued",
                channel_id=sl["ch"],
                detail={
                    "message_id": sl["m"],
                    "on": True,
                    "origin": "session",
                    "pid": PID,
                    "proc_start": START,
                },
            )

    db.write_sync(_fill)
    r = await _sreact(client, sl["token"], sl["m"], "👀", status=429)
    assert _row(db, sl["m"]) is None
    # час прошёл — снова можно
    clock.advance_ms(3_600_000)
    assert (await _sreact(client, sl["token"], sl["m"], "👀"))["status"] == "queued"
    del r


async def test_f7_project_delay_and_crooked_default(client, db, clock, sl, rng):
    _policy(db, sl["project"], delay_s=(60, 60))
    r = await _sreact(client, sl["token"], sl["m"], "👀")
    assert rng.calls[-1] == (60_000, 60_000) and r["not_before_ts"] == sl["ts"] + 60_000
    with db.reader() as conn:
        assert reactions_mod.session_policy(
            '{"reactions": {"sessions": true, "delay_min_s": 9, "delay_max_s": 3}}'
        ) == {
            "sessions": True,
            "delay_min_s": 0,
            "delay_max_s": 0,
        }
        assert reactions_mod.session_policy("не json")["sessions"] is False
        del conn
    with pytest.raises(ValueError, match="reaction_delay"):
        reactions_mod.parse_delay_minutes("5-2")
    assert reactions_mod.parse_delay_minutes("2-5") == (120, 300)


async def test_f7_admin_patch_merges_policy(db, ic, clock):
    project = insert_project(db, name="P")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?", ('{"subs": true}', project)
        )
    )
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    r = await ic.patch(
        f"{INTERNAL}/admin/projects/{project}",
        headers=headers,
        json={"session_reactions": True, "reaction_delay": "1-2"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_reactions"] == {"sessions": True, "delay_min_s": 60, "delay_max_s": 120}
    with db.reader() as conn:
        policy = json.loads(
            conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project,)).fetchone()[0]
        )
    assert policy["subs"] is True and policy["reactions"]["sessions"] is True
    r = await ic.patch(
        f"{INTERNAL}/admin/projects/{project}", headers=headers, json={"reaction_delay": "9-1"}
    )
    assert r.status_code == 422


# --- F8: миграция и скрытие -----------------------------------------------------------------------------


async def test_f8_hide_message_and_channel_clear_new_tables(client, ic, db, clock):
    _acc, h, ch, m = await _mail(db, ic)
    await _events(ic, h, [_up("777", "<b@x>", T0 - 4_000, kind="mail_thread", sender="p@gmail.com")])
    m2 = _mid(db, ch, "<b@x>")
    await _events(
        ic, h, [_mr("<r1@x>", "<a@x>", "👍", "p@gmail.com"), _mr("<r2@x>", "<b@x>", "👍", "p@gmail.com")]
    )
    await _events(ic, h, [_mr("<r3@x>", "<later@x>", "👍", "p@gmail.com")])  # к письму, которого ещё нет
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.reaction_by_session (message_id, emoji_key, token_id, ts) "
            "VALUES (?, '👍', 1, 0)",
            (m,),
        )
    )
    ui = await _ui(client, db, clock)
    r = await client.delete(f"{API}/messages/{m}", headers=ui)
    assert r.status_code in (200, 204), r.text
    with db.reader() as conn:
        left = conn.execute("SELECT target_ext_id FROM main.mail_reaction ORDER BY target_ext_id").fetchall()
        assert left == [("<b@x>",), ("<later@x>",)]
        assert conn.execute("SELECT COUNT(*) FROM main.reaction_by_session").fetchone()[0] == 0
    r = await client.delete(f"{API}/channels/{ch}", headers=ui)
    assert r.status_code in (200, 204), r.text
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.mail_reaction").fetchone()[0] == 0
    del m2


def test_f8_migration_0029_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_28(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 28}

    monkeypatch.setattr(db_module, "_migrations", _up_to_28)
    old = Db(tmp_path)
    old.start()
    try:
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                ).lastrowid
                mid = conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "  # noqa: S608
                    "ts, is_me, kind, text, reactions_json) "
                    "VALUES (?, ?, ?, ?, '1', 1000, 0, 'text', 'привет', '[]')",
                    (1 if schema == "main" else 2, 1 if schema == "main" else 2, acc, cid),
                ).lastrowid
                conn.execute(
                    f"INSERT INTO {schema}.reaction_ack (message_id, channel_id, account_id, ops_json, gen, "  # noqa: S608
                    "not_before_ms) VALUES (?, ?, ?, '[[\"👍\", true]]', 1, 0)",
                    (mid, cid, acc),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 28
                names = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master")}  # noqa: S608
                assert "mail_reaction" not in names and "reaction_by_session" not in names
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 29
                row = conn.execute(f"SELECT ops_json, origin, token_id FROM {schema}.reaction_ack").fetchone()  # noqa: S608
                assert row == ('[["👍", true]]', "ui", None)  # строки на момент деплоя — клики владельца
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(mail_reaction)")}
                assert cols == {
                    "channel_id",
                    "target_ext_id",
                    "src_ext_id",
                    "emoji",
                    "sender_key",
                    "is_me",
                    "ts",
                }
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(reaction_by_session)")}
                assert cols == {"message_id", "emoji_key", "token_id", "ts"}
                idx = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(mail_reaction)")}
                assert "mail_reaction_target" in idx
                assert conn.execute(f"SELECT text FROM {schema}.message").fetchall() == [("привет",)]  # noqa: S608
    finally:
        new.close()


# --- ревью 1: реакция владельца тем же эмодзи за паузу -------------------------------------------------------


async def test_review1_slack_owner_same_emoji_during_pause(client, ic, db, clock, sl):
    first = await _sreact(client, sl["token"], sl["m"], "👀")
    _set_reactions_json(db, sl["m"], [{"emoji": "👀", "count": 1, "me": True}])  # владелец сам, за паузу
    clock.advance_ms(first["not_before_ts"] - _now_ms(clock))
    assert await _lease(ic, sl["h"]) == []
    assert _row(db, sl["m"]) is None
    assert _marks(db, sl["m"]) == set()
    r = await _sreact(client, sl["token"], sl["m"], "👀", on=False, status=409)
    assert r["detail"] == "not own"


async def test_review1_slack_other_session_ops_kept(client, ic, db, clock, sl):
    first = await _sreact(client, sl["token"], sl["m"], "👀")
    await _sreact(client, sl["token"], sl["m"], "✅")
    _set_reactions_json(db, sl["m"], [{"emoji": "👀", "count": 1, "me": True}])
    clock.advance_ms(first["not_before_ts"] - _now_ms(clock))
    (item,) = await _lease(ic, sl["h"])
    assert item["ops"] == [["✅", True]]
    await _report(ic, sl["h"], sl["m"], item["lease_gen"], "done")
    assert _marks(db, sl["m"]) == {"✅"}


async def test_review1_telegram_owner_same_emoji_during_pause(client, ic, db, clock, tg):
    first = await _sreact(client, tg["token"], tg["new"], "👍")
    _set_reactions_json(db, tg["new"], [{"emoji": "👍", "count": 1, "me": True}])
    clock.advance_ms(first["not_before_ts"] - _now_ms(clock))
    assert await _lease(ic, tg["h"]) == []
    assert _marks(db, tg["new"]) == set()
    r = await _sreact(client, tg["token"], tg["new"], "🔥", status=409)
    assert r["detail"] == "owner reaction"


# --- Ф20-2: `channel_id` в теле реакции сессии ----------------------------------------------------------


async def _wreact(client, token, message_id, emoji, on=True, *, channel_id=None, extra=None, status=200):
    body: dict = {"emoji": emoji, "on": on}
    if channel_id is not None:
        body["channel_id"] = channel_id
    body.update(extra or {})
    r = await client.post(
        f"{API}/messages/{message_id}/reactions",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
    )
    assert r.status_code == status, r.text
    return r.json()


async def test_f20_session_channel_id_checked(client, db, clock, ic, tg):
    """Интерактивная сессия — без изменений (подписка обязательна); `channel_id` по желанию, но верный."""
    now = _now_ms(clock)
    await _events(ic, tg["h"], [_up("c4", "1", now - 5_000)])
    ch4 = _channel_id(db, "c4")
    r = await _sreact(client, tg["token"], tg["new"], "👀", status=200)
    assert r["status"] == "queued"
    body = {"emoji": "🔥", "on": True, "session": SESSION, "channel_id": ch4}
    r = await client.post(
        f"{API}/messages/{tg['new']}/reactions",
        json=body,
        headers={"Authorization": f"Bearer {tg['token']}", "Origin": ORIGIN},
    )
    assert r.status_code == 422 and r.json()["detail"] == "message not in channel"
    # без session — по-прежнему 403, и channel_id его не заменяет
    r = await _sreact(client, tg["token"], tg["new"], "👀", session=None, status=403)
