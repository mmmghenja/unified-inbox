"""Ф44-1c (docs/architecture.md): исполнитель нажатий кнопок ботов `Presser` —
тело отчёта по исходу, таймаут шага и любое исключение `push` — `unknown` (никогда не `retry`), темп 2 с,
`AuthRequired` — без отчёта; и сквозь настоящее внутреннее ядро (`CoreClient` поверх ASGI)."""

from __future__ import annotations

import asyncio
import base64

import httpx
from httpx import ASGITransport

from hub.connectors import press as press_conn
from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.fake import FakeConnector
from hub.connectors.press import Presser, PressResult
from hub.core import connector_auth, ingest, presses, security
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier


def _item(press_id=5, lease_gen=2):
    return {
        "id": press_id,
        "lease_gen": lease_gen,
        "channel_ext_id": "777",
        "message_ext_id": "500",
        "data_b64": base64.b64encode(b"cb").decode(),
    }


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/button-presses/5/report")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeCore:
    def __init__(self, clock=None) -> None:
        self.items: list[dict] = []
        self.reports: list[tuple[int, dict]] = []
        self.report_errors: list[Exception] = []
        self.leased_at: list[float] = []
        self.clock = clock

    async def wait_presses(self, timeout_s: float) -> bool:
        del timeout_s
        await asyncio.sleep(0)
        return bool(self.items)

    async def lease_presses(self, *, limit: int = 1) -> list[dict]:
        if self.clock is not None:
            self.leased_at.append(self.clock.t)
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def report_press(self, press_id: int, body: dict) -> dict:
        if self.report_errors:
            raise self.report_errors.pop(0)
        self.reports.append((press_id, body))
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
    state = {"auth": 0}

    async def on_auth():
        state["auth"] += 1

    presser = Presser(
        core,
        push,
        mono=clock.mono,
        sleep=clock.sleep,
        is_blocked=lambda: False,
        on_auth_required=on_auth,
        on_unauthorized=lambda: None,
    )
    return presser, state


async def _one(result_or_exc):
    core = FakeCore()
    core.items = [_item()]
    calls = []

    async def push(item):
        calls.append(item["id"])
        if isinstance(result_or_exc, BaseException):
            raise result_or_exc
        return result_or_exc

    presser, state = make(core, push)
    assert await presser.step() == 1
    assert calls == [5]  # одно нажатие — один вызов `push`
    return core.reports, state


async def test_report_bodies_by_outcome():
    answer = {"message": "ок", "alert": True}
    assert (await _one(PressResult("done", result=answer)))[0] == [
        (5, {"lease_gen": 2, "outcome": "done", "result": answer})
    ]
    assert (await _one(PressResult("done")))[0] == [(5, {"lease_gen": 2, "outcome": "done", "result": {}})]
    assert (await _one(PressResult("failed", reason="stale_button")))[0] == [
        (5, {"lease_gen": 2, "outcome": "failed", "reason": "stale_button"})
    ]
    assert (await _one(PressResult("unknown", reason="reconnected")))[0] == [
        (5, {"lease_gen": 2, "outcome": "unknown", "reason": "reconnected"})
    ]
    assert (await _one(PressResult("retry", reason="flood", retry_after_s=12.5)))[0] == [
        (5, {"lease_gen": 2, "outcome": "retry", "reason": "flood", "retry_after_ms": 12500})
    ]
    assert (await _one(PressResult("retry", reason="OSError")))[0] == [
        (5, {"lease_gen": 2, "outcome": "retry", "reason": "OSError"})
    ]


async def test_exception_or_garbage_is_unknown_never_retry():
    """Что бы ни случилось внутри `push` — запрос мог уйти: повторять нельзя."""
    for bad in (RuntimeError("x"), ValueError("y")):
        reports, _ = await _one(bad)
        assert reports == [(5, {"lease_gen": 2, "outcome": "unknown", "reason": type(bad).__name__})]
    reports, _ = await _one("мусор")
    assert reports == [(5, {"lease_gen": 2, "outcome": "unknown", "reason": "bad_result"})]


async def test_step_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(press_conn, "PUSH_TIMEOUT_S", 0.02)
    core = FakeCore()
    core.items = [_item()]
    started = []

    async def push(item):
        started.append(item["id"])
        await asyncio.sleep(5)

    presser, _state = make(core, push)
    await presser.step()
    assert core.reports == [(5, {"lease_gen": 2, "outcome": "unknown", "reason": "step_timeout"})]
    assert started == [5] and core.items == []


async def test_auth_required_means_no_report():
    reports, state = await _one(AuthRequired("вход потерян"))
    assert reports == [] and state["auth"] == 1


async def test_rejected_or_unreachable_report_is_swallowed_and_not_repeated():
    for error in (_http_error(409), httpx.ConnectError("нет ядра")):
        core = FakeCore()
        core.items = [_item()]
        core.report_errors = [error]
        calls = []

        async def push(item, calls=calls):
            calls.append(1)
            return PressResult("done", result={})

        presser, _state = make(core, push)
        await presser.step()
        assert core.reports == [] and calls == [1]


async def test_pace_is_one_press_per_two_seconds():
    clock = Clock()
    core = FakeCore(clock)
    core.items = [_item(1), _item(2), _item(3)]

    async def push(item):
        return PressResult("done", result={})

    presser, _state = make(core, push, clock=clock)
    for _ in range(3):
        await presser.step()
    assert [t - 1000.0 for t in core.leased_at] == [0.0, 2.0, 4.0]
    assert press_conn.PACE_S == 2.0 and press_conn.PUSH_TIMEOUT_S == 25.0
    # Пауза прошла сама — ждать нечего.
    clock.t += 10
    core.items = [_item(4)]
    await presser.step()
    assert core.leased_at[-1] == clock.t


async def test_base_push_press_default_is_failed_unsupported():
    connector = FakeConnector.__new__(FakeConnector)
    assert await connector.push_press(_item()) == PressResult("failed", reason="unsupported")
    assert FakeConnector.press_enabled is False


# --- сквозь настоящее внутреннее ядро ---------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value


async def test_through_real_core_press_and_unknown(db):
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
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (acc, connector_auth.hash_secret(secret)),
        )
    )
    now_ms = int(clock() * 1000)
    data = base64.b64encode(b"cb-1").decode()
    event = {
        "type": "upsert",
        "channel": {"ext_id": "777", "kind": "dm"},
        "ext_id": "500",
        "ts": now_ms - 1000,
        "sender": {"ext_id": "777", "name": "Бот", "is_bot": True},
        "is_me": False,
        "kind": "text",
        "text": "Выбери",
        "mentions_me": False,
        "buttons": {
            "kind": "inline",
            "rows": [[{"t": "Да", "k": "callback", "d": data}, {"t": "Нет", "k": "callback", "d": data}]],
        },
        "buttons_ts": now_ms,
    }
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=acc, events=[event], cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0
    with db.reader() as conn:
        message_id, channel_id = conn.execute("SELECT id, channel_id FROM main.message").fetchone()
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="ui", kind="interactive", scopes=["read", "draft"], project_ids=None, expires_ts=None
        )
    )
    ui = security.Principal(
        token_id=token.token_id, kind="ui", scopes=frozenset({"read", "draft"}), project_ids=None
    )

    def press(col, text):
        return db.write_sync(
            lambda conn: presses.create(
                conn,
                principal=ui,
                message_id=message_id,
                channel_id=channel_id,
                project_id=None,
                row=1,
                col=col,
                text=text,
                session=None,
                session_key=None,
                now_ms=now_ms,
            )
        ).press

    first, second = press(1, "Да"), press(2, "Нет")
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))
    seen = []

    async def push(item):
        seen.append(item)
        if item["id"] == first.id:
            return PressResult("done", result={"message": "принято"})
        raise RuntimeError("обрыв после отправки")

    presser, _state = make(client, push)
    assert await client.wait_presses(1) is True
    assert await presser.step() == 1 and await presser.step() == 1
    assert [(i["channel_ext_id"], i["message_ext_id"], i["data_b64"]) for i in seen] == [
        ("777", "500", data)
    ] * 2
    with db.reader() as conn:
        done, lost = presses.get(conn, first.id), presses.get(conn, second.id)
        shown = presses.press_dict(conn, done, result=True)
    assert done.status == "done" and shown["result"] == {"message": "принято"}
    assert (lost.status, lost.reason) == ("unknown", "RuntimeError")
    assert await presser.step() == 0  # `unknown` в очередь не возвращается
    await client.aclose()
