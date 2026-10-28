"""Ф17-1 (docs/architecture.md): исполнитель заявок на вступление `Joiner` —
отчёт по шагу (`peek` — предпросмотр, `join` — исход и беседа), исключение `push` -> `retry`, пауза всего
цикла после `retry_after_s`, `AuthRequired` — без отчёта, `after_join` — только после принятого ядром
`done`; и сквозь настоящее внутреннее ядро (`CoreClient` поверх ASGI): тело отчёта принимает
`/join/report`."""

from __future__ import annotations

import asyncio

import httpx
from httpx import ASGITransport

from hub.connectors import join as join_conn
from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.fake import FakeConnector
from hub.connectors.join import Joiner, JoinResult
from hub.core import connector_auth, security
from hub.core import join as join_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier


def _item(step="peek", join_id=5, lease_gen=2, ext_id=None):
    return {
        "id": join_id,
        "lease_gen": lease_gen,
        "step": step,
        "target_kind": "username",
        "target": "news_chan",
        "ext_id": ext_id,
        "project_id": 3,
    }


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/join/report")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


class FakeCore:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.reports: list[dict] = []
        self.report_errors: list[Exception] = []

    async def wait_joins(self, timeout_s: float) -> bool:
        del timeout_s
        await asyncio.sleep(0)
        return bool(self.items)

    async def lease_joins(self, *, limit: int = 1) -> list[dict]:
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def report_join(self, body: dict) -> dict:
        if self.report_errors:
            raise self.report_errors.pop(0)
        self.reports.append(body)
        return {"ok": True}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def mono(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        await asyncio.sleep(0)


def make(core, push, *, clock=None):
    clock = clock or Clock()
    state = {"auth": 0, "after": []}

    async def after(item, result):
        state["after"].append((item["id"], result.outcome, len(getattr(core, "reports", []))))

    async def on_auth():
        state["auth"] += 1

    joiner = Joiner(
        core,
        push,
        after=after,
        mono=clock.mono,
        sleep=clock.sleep,
        is_blocked=lambda: False,
        on_auth_required=on_auth,
        on_unauthorized=lambda: None,
    )
    return joiner, state


PEEK = {"title": "Канал", "kind": "broadcast", "participants": 3, "member": False, "ext_id": "-1001"}


async def test_peek_done_body_and_after_after_report():
    core = FakeCore()
    core.items = [_item("peek")]

    async def push(item):
        return JoinResult("done", peek=PEEK)

    joiner, state = make(core, push)
    assert await joiner.step() == 1
    assert core.reports == [{"id": 5, "lease_gen": 2, "outcome": "done", "peek": PEEK}]
    assert state["after"] == [(5, "done", 1)]  # П4: после принятого отчёта


async def test_join_done_body_with_result_ext_id_title():
    core = FakeCore()
    core.items = [_item("join")]

    async def push(item):
        return JoinResult("done", result="joined", ext_id="-1001", title="Канал")

    joiner, _ = make(core, push)
    await joiner.step()
    assert core.reports == [
        {"id": 5, "lease_gen": 2, "outcome": "done", "result": "joined", "ext_id": "-1001", "title": "Канал"}
    ]


async def test_push_exception_and_bad_result_become_retry_no_after():
    core = FakeCore()
    core.items = [_item(join_id=1), _item(join_id=2)]
    results = [RuntimeError("x"), "garbage"]

    async def push(item):
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    joiner, state = make(core, push)
    await joiner.step()
    await joiner.step()
    assert [(r["id"], r["outcome"], r["reason"]) for r in core.reports] == [
        (1, "retry", "RuntimeError"),
        (2, "retry", "bad_result"),
    ]
    assert state["after"] == []


async def test_timeout_becomes_retry(monkeypatch):
    monkeypatch.setattr(join_conn, "PUSH_TIMEOUT_S", 0.01)
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        await asyncio.sleep(1)

    joiner, _ = make(core, push)
    await joiner.step()
    assert core.reports[0]["outcome"] == "retry" and core.reports[0]["reason"] == "timeout"


async def test_flood_wait_pauses_loop_and_goes_to_core_in_ms():
    core = FakeCore()
    core.items = [_item()]
    clock = Clock()

    async def push(item):
        return JoinResult("retry", retry_after_s=12.5, reason="FloodWait")

    joiner, _ = make(core, push, clock=clock)
    await joiner.step()
    assert core.reports == [
        {"id": 5, "lease_gen": 2, "outcome": "retry", "retry_after_ms": 12_500, "reason": "FloodWait"}
    ]
    assert joiner.paused_until == clock.t + 12.5


async def test_drop_reason_and_auth_required_without_report():
    core = FakeCore()
    core.items = [_item(join_id=1), _item(join_id=2)]

    async def push(item):
        if item["id"] == 1:
            return JoinResult("drop", reason="not_found")
        raise AuthRequired("lost")

    joiner, state = make(core, push)
    await joiner.step()
    await joiner.step()
    assert core.reports == [{"id": 1, "lease_gen": 2, "outcome": "drop", "reason": "not_found"}]
    assert state["auth"] == 1 and state["after"] == []


async def test_report_rejected_no_after():
    core = FakeCore()
    core.items = [_item("join")]
    core.report_errors = [_http_error(409)]

    async def push(item):
        return JoinResult("done", result="joined", ext_id="-1001")

    joiner, state = make(core, push)
    await joiner.step()
    assert core.reports == [] and state["after"] == []


async def test_base_push_join_default_is_drop_unsupported():
    connector = FakeConnector.__new__(FakeConnector)
    assert await connector.push_join(_item()) == JoinResult("drop", reason="unsupported")
    assert FakeConnector.join_enabled is False


# --- сквозь настоящее внутреннее ядро ---------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value


async def test_through_real_core_peek_then_join(db):
    clock = _Clock()
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_PREFIX + "x" * 43)
    )
    acc = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    project = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (acc, connector_auth.hash_secret(secret)),
        )
    )
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    )
    principal = security.Principal(
        token_id=token.token_id, kind="interactive", scopes=frozenset({"read", "draft"}), project_ids=None
    )
    now_ms = int(clock() * 1000)
    req, _ = db.write_sync(
        lambda conn: join_mod.create(
            conn, principal=principal, project_id=project, target="@news_chan", reason=None, session=None,
            now_ms=now_ms,
        )
    )
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))

    async def push(item):
        if item["step"] == "peek":
            return JoinResult("done", peek=PEEK)
        return JoinResult("done", result="joined", ext_id="-1001", title="Канал")

    joiner, state = make(client, push)
    assert await joiner.step() == 1
    with db.reader() as conn:
        assert join_mod.get(conn, req.id).status == "pending"
    db.write_sync(lambda conn: join_mod.decide(conn, req.id, "approve", now_ms=now_ms))
    assert await joiner.step() == 1
    with db.reader() as conn:
        done = join_mod.get(conn, req.id)
    assert (done.status, done.ext_id, done.title) == ("joined", "-1001", "Канал")
    assert [s[1] for s in state["after"]] == ["done", "done"]
    await client.aclose()
