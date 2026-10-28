"""Ф16-4 (docs/architecture.md): лимит реакций сессии 60/ч — у OpenCode ключ
`(pid, proc_start, session_id)`: две сессии `ses_…` одного процесса считаются раздельно; у Claude Code — как
раньше, по процессу. Отказ мёртвой сессии OpenCode — без «Claude Code».

Обвязка — помощники `test_session_reactions.py` (Ф15-1), фикстуры — свои (Slack-беседа проекта с реакциями).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import reactions as reactions_mod
from hub.core import subs
from hub.core.app_internal import create_internal_app
from hub.core.audit import write_audit
from hub.core.connector_auth import hash_secret
from hub.core.feed import Notifier

from .conftest import insert_project
from .test_session_reactions import (
    ADMIN_SECRET,
    PID,
    START,
    FixedRng,
    _account,
    _audit,
    _channel_id,
    _events,
    _mid,
    _now_ms,
    _policy,
    _set_project,
    _sreact,
    _sub,
    _token,
    _up,
)

OC_A, OC_B = "ses_AAAAAAAAAA1", "ses_BBBBBBBBBB2"


@pytest.fixture
def world_procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    monkeypatch.setattr(reactions_mod, "_rng", FixedRng())
    return table


@pytest.fixture
async def ic(db, clock):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        yield c


@pytest.fixture
async def oc(db, ic, clock, world_procs):
    """Проект с реакциями сессий, беседа Slack; сессия A подписана на проект, сессия B того же процесса — на
    беседу."""
    project = insert_project(db, name="Globex")
    _policy(db, project)
    _acc, h = _account(db, source="slack", db_name="work")
    now = _now_ms(clock)
    await _events(ic, h, [_up("C1", "1790000000.000100", now - 10_000, kind="group")])
    ch = _channel_id(db, "C1", "work")
    _set_project(db, ch, project)
    created = _token(db)
    _sub(db, project_id=project, token_id=created.token_id, sid=OC_A)
    _sub(db, project_id=project, token_id=created.token_id, channel_id=ch, sid=OC_B)
    return {
        "ch": ch,
        "m": _mid(db, ch, "1790000000.000100"),
        "token": created.token,
        "token_id": created.token_id,
    }


def _fill(db, oc, now, *, session_id):
    def fn(conn):
        for i in range(reactions_mod.SESSION_RATE_PER_HOUR):
            detail = {"message_id": oc["m"], "on": True, "origin": "session", "pid": PID, "proc_start": START}
            if session_id is not None:
                detail["session_id"] = session_id
            write_audit(
                conn,
                now_ms=now - 1000 - i,
                actor="interactive",
                api_token_id=oc["token_id"],
                client_name=None,
                request_id=f"r{i}",
                action="reaction_set",
                result="queued",
                channel_id=oc["ch"],
                detail=detail,
            )

    db.write_sync(fn)


def _session(sid):
    return {"pid": PID, "proc_start": START, "session_id": sid}


async def test_rate_limit_is_per_oc_session(client, db, clock, oc):
    _fill(db, oc, _now_ms(clock), session_id=OC_A)
    await _sreact(client, oc["token"], oc["m"], "👀", session=_session(OC_A), status=429)
    # Вторая сессия того же процесса — свой счёт.
    r = await _sreact(client, oc["token"], oc["m"], "👀", session=_session(OC_B))
    assert r["status"] == "queued"
    # В аудите реакции OpenCode — `session_id` (ключ лимита).
    [(result, detail)] = _audit(db, "reaction_set")[-1:]
    assert result == "queued" and detail["session_id"] == OC_B and detail["pid"] == PID


def test_rate_count_without_session_id_counts_by_process(db, clock, oc):
    """Claude Code (без `session_id`) — счёт по процессу, как раньше; с `session_id` — только своей сессии."""
    now = _now_ms(clock)
    _fill(db, oc, now, session_id=OC_A)
    with db.reader() as conn:
        by_proc = reactions_mod.session_rate_count(
            conn, token_id=oc["token_id"], pid=PID, proc_start=START, now_ms=now
        )
        a = reactions_mod.session_rate_count(
            conn, token_id=oc["token_id"], pid=PID, proc_start=START, now_ms=now, session_id=OC_A
        )
        b = reactions_mod.session_rate_count(
            conn, token_id=oc["token_id"], pid=PID, proc_start=START, now_ms=now, session_id=OC_B
        )
    assert (by_proc, a, b) == (reactions_mod.SESSION_RATE_PER_HOUR, reactions_mod.SESSION_RATE_PER_HOUR, 0)


async def test_dead_oc_session_refusal_without_claude_code(client, db, clock, oc, world_procs):
    world_procs.clear()
    r = await _sreact(client, oc["token"], oc["m"], "👀", session=_session(OC_A), status=403)
    assert "Claude Code" not in r["detail"] and "живой сессии" in r["detail"]
