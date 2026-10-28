"""24.10: подписка сессии на беседу-заготовку (первое сообщение новому человеку) — разрешена и живёт, когда
заготовка становится настоящей беседой: `_rename` (тот же `channel_id`) и `_merge` (подписка переезжает на
найденную беседу вместе с лентой). Ответ человека сигналит подписке сразу."""

import pytest

from hub.core import subs
from hub.core.ingest import apply_batch

from .conftest import insert_channel, insert_project, make_bearer_token
from .test_drafts_new import _account, _approve_and_lease, _now, _report

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4343, 778_000, "sess-dddddddd-4444"


class _NoPause:
    def randint(self, a: int, b: int) -> int:
        return a


@pytest.fixture(autouse=True)
def _no_first_pause(monkeypatch):
    monkeypatch.setattr("hub.core.outbox._rng", _NoPause())


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def env(db, tmp_path):
    project = insert_project(db, name="Hooli", workdir=str(tmp_path))
    tg = _account(db, "telegram", config={"idempotent_send": True, "pace_window": "00:00-24:00"})
    token = make_bearer_token(db, scopes=("read", "draft"), name="mcp:all")
    return {"project": project, "tg": tg, "token": token, "cwd": str(tmp_path)}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "2"}


async def _draft_and_sub(client, env):
    r = await client.post(
        "/api/v1/drafts/new",
        json={
            "account_id": env["tg"],
            "project_id": env["project"],
            "to": "@Petr_Ivanov",
            "text": "Здравствуйте",
        },
        headers={"Authorization": f"Bearer {env['token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    session = {
        "pid": PID,
        "proc_start": START,
        "session_id": SID,
        "cwd": env["cwd"],
        "kind": "interactive",
        "name": None,
        "name_source": None,
    }
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": body["channel_id"], "session": session},
        headers=_h(env["token"]),
    )
    assert r.status_code == 200, r.text
    return body, r.json()["sub"]["id"]


def _sub_row(db, sub_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT channel_id, ended_ts, end_reason FROM main.session_chan_sub WHERE id = ?", (sub_id,)
        ).fetchone()


def _incoming(db, clock, account_id, peer_ext, ext_id="in-1"):
    db.write_sync(
        lambda conn: apply_batch(
            conn,
            account_id=account_id,
            cursor=None,
            now_ms=_now(clock),
            events=[
                {
                    "type": "upsert",
                    "channel": {"ext_id": peer_ext, "kind": "dm"},
                    "ext_id": ext_id,
                    "ts": _now(clock) + 1000,
                    "is_me": False,
                    "kind": "text",
                    "text": "Да, интересно",
                    "mentions_me": False,
                }
            ],
        )
    )


def _pending_signals(db, sub_id) -> list[int]:
    with db.reader() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT channel_id FROM main.session_chan_signal "
                "WHERE chan_sub_id = ? AND status = 'pending'",
                (sub_id,),
            )
        ]


async def test_rename_keeps_subscription_and_reply_signals(client, db, clock, env, procs):
    body, sub_id = await _draft_and_sub(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="777")
    with db.reader() as conn:
        assert conn.execute(
            "SELECT ext_id FROM main.channel WHERE id = ?", (body["channel_id"],)
        ).fetchone() == ("777",)
    assert _sub_row(db, sub_id) == (body["channel_id"], None, None)
    clock.advance_ms(5_000)
    _incoming(db, clock, env["tg"], "777")
    assert _pending_signals(db, sub_id) == [body["channel_id"]]


async def test_merge_moves_subscription_to_found_channel(client, db, clock, env, procs):
    target = insert_channel(
        db, env["tg"], ext_id="555", kind="dm", project_id=env["project"], first_seen_ts=_now(clock) + 1
    )
    body, sub_id = await _draft_and_sub(client, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555")
    with db.reader() as conn:
        assert conn.execute(
            "SELECT archived FROM main.channel WHERE id = ?", (body["channel_id"],)
        ).fetchone() == (1,)
    assert _sub_row(db, sub_id) == (target, None, None)
    clock.advance_ms(5_000)
    _incoming(db, clock, env["tg"], "555")
    assert _pending_signals(db, sub_id) == [target]
