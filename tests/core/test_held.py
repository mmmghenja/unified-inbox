"""Ф41-2а (docs/architecture.md П1–П4, раздел 5 п.1, 8, 10): сессия хаба —
личность, реестр и живость на уровне модулей ядра (без приложения)."""

import pytest

from hub.core import chan_subs, held, hub_sessions, peer_msgs, security, subs
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db
from hub.core.feed import Notifier

AUDIT = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r"}
NOW = 1_700_000_000_000


def _project(db, tmp_path, name="P", *, subs_on=True) -> int:
    workdir = tmp_path / name
    workdir.mkdir(exist_ok=True)
    pid = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.project (name, workdir) VALUES (?, ?)", (name, str(workdir))
            ).lastrowid
        )
    )
    if subs_on:
        db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    return pid


def _create(db, project_id, **kw) -> held.HubSession:
    kw.setdefault("engine", "claude")
    return db.write_sync(
        lambda conn: hub_sessions.create(conn, project_id=project_id, now_ms=NOW, audit=AUDIT, **kw)
    )


def _token_id(db) -> int:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    ).token_id


def _raw_sub(db, hub_id, project_id, token_id, *, role=None, created_ts=NOW) -> int:
    """Живая подписка на проект условной тройки — строкой (без `subscribe`)."""
    pid, start, sid = held.triple(hub_id)
    key = role.casefold() if role else None
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts, role, role_key) VALUES (?, ?, ?, ?, ?, '/w', ?, ?, ?)",
                (project_id, token_id, pid, start, sid, created_ts, role, key),
            ).lastrowid
        )
    )


# --- тройка -------------------------------------------------------------------------------------------------


def test_triple_helpers():
    assert held.held_pid(7) == -7
    assert held.held_id(-7) == 7
    assert held.held_id(7) is None and held.held_id(0) is None and held.held_id(True) is None
    assert held.triple(7) == (-7, 0, "hub:7")
    # П1: метка не начинается с `ses_` — правило брошенной подписки OpenCode к ней не относится.
    assert not subs.is_oc(held.held_sid(7))


@pytest.mark.parametrize(
    ("triple", "kind", "ok"),
    [
        ((4242, 777, "sess-a"), None, True),
        ((4242, 777, "sess-a"), "interactive", True),
        ((4242, 777, "sess-a"), "hub", False),  # `kind == "hub"` с настоящим pid
        ((4242, 0, "sess-a"), None, False),  # ноль — только у условной тройки
        ((4242, 0, "hub:4242"), "interactive", False),
        ((-5, 0, "hub:5"), None, True),
        ((-5, 0, "hub:5"), "hub", True),
        ((-5, 0, "hub:5"), "interactive", False),  # условная тройка без `kind == "hub"`
        ((-5, 0, "hub:5"), "opencode", False),
        ((-5, 1, "hub:5"), "hub", False),  # `proc_start` не ноль
        ((-5, 0, "hub:6"), "hub", False),  # метка чужой сессии
        ((-5, 0, "sess-a"), "hub", False),  # id разговора движка в тройку не входит
        ((0, 0, "hub:0"), "hub", False),
    ],
)
def test_triple_error(triple, kind, ok):
    error = held.triple_error(*triple) if kind is None else held.triple_error(*triple, kind)
    assert (error is None) is ok


# --- реестр и `is_alive` ------------------------------------------------------------------------------------


def test_registry_empty_without_app_negative_pid_is_dead(db, tmp_path):
    project = _project(db, tmp_path)
    rec = _create(db, project)
    # Приложения нет — реестр пуст: быстрый ответ «мертва»; по базе сессия жива.
    assert not subs.is_alive(*held.triple(rec.id)[:2])
    with db.reader() as conn:
        assert held.alive_db(conn, -rec.id, 0)
        assert subs.session_alive(conn, -rec.id, 0)
        assert not held.alive_db(conn, -rec.id, 5)
        assert not held.alive_db(conn, -(rec.id + 100), 0)


def test_registry_loaded_on_install_and_is_alive(db, tmp_path):
    project = _project(db, tmp_path)
    one = _create(db, project)
    two = _create(db, project)
    db.write_sync(lambda conn: hub_sessions.close(conn, two.id, now_ms=NOW, audit=AUDIT))
    registry = held.install(db)
    assert one.id in registry and two.id not in registry and len(registry) == 1
    assert subs.is_alive(-one.id, 0)
    assert not subs.is_alive(-one.id, 5)  # `proc_start` условной тройки — только ноль
    assert not subs.is_alive(-two.id, 0)  # закрыта
    assert not subs.is_alive(-(one.id + 100), 0)  # неизвестной сессии нет
    assert held.install(db) is registry  # приложение над той же базой получает тот же объект


def test_positive_pid_unchanged(db, monkeypatch):
    held.install(db)
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: {4242: 777}.get(pid))
    assert subs.is_alive(4242, 777)
    assert not subs.is_alive(4242, 778)
    assert not subs.is_alive(4243, 777)
    assert subs.read_proc_start(-5) is None


def test_rollback_of_create_does_not_touch_registry(db, tmp_path):
    project = _project(db, tmp_path)
    registry = held.install(db)

    def _fn(conn):
        hub_sessions.create(conn, project_id=project, engine="claude", now_ms=NOW, audit=AUDIT)
        raise RuntimeError("откат")

    with pytest.raises(RuntimeError):
        db.write_sync(_fn)
    assert len(registry) == 0
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0] == 0


def test_registry_does_not_leak_between_databases(tmp_path):
    """Два приложения подряд с разными базами: реестр первой базы во вторую не течёт (П2б)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = Db(tmp_path / "a"), Db(tmp_path / "b")
    first.start()
    second.start()
    try:
        project = _project(first, tmp_path, "PA")
        rec = _create(first, project)
        create_public_app(first, clock=lambda: NOW / 1000)
        assert subs.is_alive(-rec.id, 0)
        create_public_app(second, clock=lambda: NOW / 1000)
        assert not subs.is_alive(-rec.id, 0)  # текущий реестр — второй базы, в ней такой сессии нет
        # Публичное и внутреннее приложение одной базы делят один объект.
        public = create_public_app(first, clock=lambda: NOW / 1000)
        internal = create_internal_app(
            first,
            clock=lambda: NOW / 1000,
            notifier=Notifier(),
            admin_key_hash=hash_secret(ADMIN_PREFIX + "x" * 43),
        )
        assert public.state.held is internal.state.held
        assert subs.is_alive(-rec.id, 0)
    finally:
        first.close()
        second.close()


# --- разрушающие решения — только по базе (П2) --------------------------------------------------------------


def test_cleanup_with_empty_registry_keeps_subs_of_open_session(db, tmp_path):
    """Пустой (незагруженный) реестр не завершает подписки спящей сессии: `session_gone` невосстановим."""
    project = _project(db, tmp_path)
    token_id = _token_id(db)
    rec = _create(db, project)
    sub_id = _raw_sub(db, rec.id, project, token_id)
    pid, start, sid = held.triple(rec.id)
    chan_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts) VALUES (1, ?, ?, ?, ?, ?, '/w', ?)",
                (project, token_id, pid, start, sid, NOW),
            ).lastrowid
        )
    )
    held.reset()
    assert not subs.is_alive(pid, start)
    later = NOW + 30 * 60_000  # и через полчаса без ожидания: правило OpenCode к метке `hub:` не относится
    assert db.write_sync(lambda conn: subs.cleanup(conn, now_ms=later)) == []
    assert db.write_sync(lambda conn: chan_subs.cleanup(conn, now_ms=later)) == []
    with db.reader() as conn:
        assert subs.get_sub(conn, sub_id).ended_ts is None
        assert chan_subs.get(conn, chan_id).ended_ts is None
        assert not subs.rec_dead(conn, subs.get_sub(conn, sub_id), later, None)

    # Закрытая запись — мертва по базе: чистка снимает оставшееся (подписка, заведённая мимо `close`).
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET closed_ts = ? WHERE id = ?", (later, rec.id))
    )
    assert db.write_sync(lambda conn: subs.cleanup(conn, now_ms=later)) == [(sub_id, "session_gone")]
    assert db.write_sync(lambda conn: chan_subs.cleanup(conn, now_ms=later)) == [(chan_id, "session_gone")]


def test_opencode_engine_session_not_stale_after_ten_minutes(db, tmp_path):
    """Сессия хаба на движке OpenCode жива через 10 минут без ожидания: `oc_stale` смотрит на метку тройки."""
    project = _project(db, tmp_path)
    token_id = _token_id(db)
    rec = _create(db, project, engine="opencode", role="маркетолог")
    db.write_sync(
        lambda conn: hub_sessions.bind(
            conn,
            rec.id,
            engine_session_id="ses_AAAAAAAAAA1",
            proc_pid=None,
            proc_start=None,
            state="asleep",
            now_ms=NOW,
            audit=AUDIT,
        )
    )
    sub_id = _raw_sub(db, rec.id, project, token_id, role="маркетолог")
    held.install(db)
    later = NOW + subs.OC_LIVE_GRACE_MS + 60_000
    with db.reader() as conn:
        sub = subs.get_sub(conn, sub_id)
        assert not subs.oc_stale(sub, later, waiting=False)
        assert not subs._sub_dead(sub, later, None)
        assert not subs.rec_dead(conn, sub, later, None)
        assert peer_msgs._sub_live(sub, later, None)  # адресат сообщений агентов — в сети
        row = subs.one_json(conn, sub, now_ms=later, waiters=None)
    assert row["live"] is True and row["engine"] == "opencode"
    assert (row["held"], row["hub_session_id"], row["state"]) == (True, rec.id, "asleep")
    assert db.write_sync(lambda conn: subs.cleanup(conn, now_ms=later)) == []


# --- запись: состояния, хост, миграция ----------------------------------------------------------------------


def test_host_stale_moves_to_asleep_without_touching_subs(db, tmp_path):
    project = _project(db, tmp_path)
    token_id = _token_id(db)
    rec = _create(db, project)
    sub_id = _raw_sub(db, rec.id, project, token_id)
    db.write_sync(
        lambda conn: hub_sessions.set_state(
            conn, rec.id, state="running", proc_pid=4242, proc_start=777, now_ms=NOW
        )
    )
    fresh = NOW + held.HOST_STALE_MS
    assert db.write_sync(lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=fresh)) == []
    stale = NOW + held.HOST_STALE_MS + 1
    assert db.write_sync(lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=stale)) == [rec.id]
    with db.reader() as conn:
        after = held.get(conn, rec.id)
        assert (after.state, after.proc_pid, after.proc_start) == ("asleep", None, None)
        assert after.closed_ts is None
        assert subs.get_sub(conn, sub_id).ended_ts is None
    assert db.write_sync(lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=stale)) == []


def test_migration_table_indexes_and_no_id_reuse(db, tmp_path):
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0] == 0
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type = 'index' AND tbl_name = 'hub_session'"
            )
        }
    assert {"hub_session_open", "hub_session_open_role", "hub_session_open_engine_sid"} <= names
    project = _project(db, tmp_path)
    first = _create(db, project)
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.hub_session WHERE id = ?", (first.id,)))
    second = _create(db, project)
    assert second.id > first.id  # AUTOINCREMENT: тройка закрытой или удалённой сессии не достаётся новой
    assert (second.state, second.mode, second.engine_session_id, second.created_by) == (
        "asleep",
        "manual",
        None,
        "ui",
    )
