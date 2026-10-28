"""Ф10-7 (docs/architecture.md): модуль `core/remote.py` без HTTP — хеш пароля с боевыми
параметрами scrypt, адреса, миграция 0019, перепроверка сессии для SSE (П4), `hub-watch` (решение 4)."""

import asyncio
import base64

import pytest

from hub.core import remote as remote_mod
from hub.core import security
from hub.core.feed import HEARTBEAT_FRAME, Notifier, stream
from hub.core.security import Principal
from hub.watch import Problem, evaluate

NOW = 1_700_000_000_000


def test_hash_password_uses_spec_parameters_and_verifies() -> None:
    stored = remote_mod.hash_password("correct horse battery")
    scheme, log_n, r, p, salt_b64, digest_b64 = stored.split("$")
    assert (scheme, log_n, r, p) == ("scrypt", "15", "8", "1")
    assert len(base64.b64decode(salt_b64)) == 16
    assert len(base64.b64decode(digest_b64)) == 32
    assert remote_mod.verify_password("correct horse battery", stored) is True
    assert remote_mod.verify_password("correct horse batterY", stored) is False
    assert stored != remote_mod.hash_password("correct horse battery")  # соль своя


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "scrypt",
        "bcrypt$15$8$1$AAAA$AAAA",
        "scrypt$x$8$1$AAAA$AAAA",
        "scrypt$15$8$1$!!$AAAA",
        "scrypt$40$8$1$AA==$AA==",
    ],
)
def test_verify_password_rejects_broken_strings(stored) -> None:
    assert remote_mod.verify_password("anything at all", stored) is False


def test_normalize_hosts() -> None:
    assert remote_mod.normalize_hosts(["Hub.example.com:8787", "hub.example.com:8787 "]) == (
        "hub.example.com:8787",
    )
    assert remote_mod.normalize_hosts(["192.168.1.5:9000"]) == ("192.168.1.5:9000",)
    for bad in (["localhost:1"], ["127.0.0.1:1"], ["x.localhost:1"], ["hub.example.com"], []):
        with pytest.raises(remote_mod.RemoteConfigError):
            remote_mod.normalize_hosts(bad)


def test_migration_0019_columns(db) -> None:
    with db.reader() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA main.table_info(api_token)")}
        indexes = {row[1] for row in conn.execute("PRAGMA main.index_list(audit)")}
    assert {"created_ts", "last_used_ts", "via", "user_agent"} <= cols
    assert "audit_action_ts" in indexes
    with pytest.raises(Exception, match="CHECK"):
        db.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.api_token (name, kind, hash, via) VALUES ('ui', 'ui', 'h', 'phone')"
            )
        )


def _ui_session(db, *, via: str) -> int:
    return db.write_sync(
        lambda conn: security.create_ui_session(conn, via=via, user_agent=None, now_ms=NOW, prev_cookie=None)
    )[1]


def test_session_still_valid(db) -> None:
    local_id = _ui_session(db, via="local")
    remote_id = _ui_session(db, via="remote")
    bearer_id = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="b", kind="interactive", scopes=["read"], project_ids=None, expires_ts=None
        )
    ).token_id
    with db.reader() as conn:

        def ok(token_id, remote, now=NOW):
            return security.session_still_valid(conn, token_id=token_id, remote=remote, now_ms=now)

        assert ok(local_id, False) and not ok(local_id, True)
        assert ok(remote_id, True) and not ok(remote_id, False)
        assert ok(bearer_id, False) and not ok(bearer_id, True)
        assert not ok(local_id, False, now=NOW + security.UI_SESSION_TTL_MS)
        assert not ok(999_999, False)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.api_token SET revoked = 1 WHERE id = ?", (remote_id,))
    )
    with db.reader() as conn:
        assert not security.session_still_valid(conn, token_id=remote_id, remote=True, now_ms=NOW)


async def test_stream_closes_when_still_valid_turns_false(db) -> None:
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    db.on_commit(notifier.notify)
    principal = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    valid = {"v": True}
    checks = []

    def still_valid() -> bool:
        checks.append(1)
        return valid["v"]

    gen = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=0.05, still_valid=still_valid)
    assert await asyncio.wait_for(anext(gen), timeout=2) == HEARTBEAT_FRAME
    valid["v"] = False
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(gen), timeout=2)
    assert len(checks) >= 2


async def test_stream_checks_session_before_heartbeat(db) -> None:
    """П4: отозванной сессии не уходит даже heartbeat — проверка стоит и перед ним."""
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    principal = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    calls = []

    def still_valid() -> bool:
        calls.append(1)
        return len(calls) == 1  # годна на входе в цикл, отозвана к моменту heartbeat

    gen = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=0.05, still_valid=still_valid)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(gen), timeout=2)


def test_watch_remote_login_problem() -> None:
    base = {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0}
    assert evaluate({**base, "remote_login_failures_24h": 4}, now_ms=NOW) == []
    assert evaluate({**base, "remote_login_failures_24h": 5}, now_ms=NOW) == [
        Problem("remote_login", "5 неверных паролей входа с телефона за сутки")
    ]
    assert evaluate(base, now_ms=NOW) == []
