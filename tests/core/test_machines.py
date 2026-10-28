"""Ф77-1 (docs/architecture.md, E): реестр машин ядра — upsert, троттлинг последнего
контакта, своя машина, имя от владельца, машина подписки; клиент — файл маршрута `hub-link` и версия без git."""

import subprocess

import pytest

from hub import buildinfo, hostid, machine_info
from hub.core import machines, session_host

CORE = "c0" * 8
LAPTOP = "1a" * 8
OTHER = "0e" * 8


@pytest.fixture(autouse=True)
def core_host(monkeypatch):
    monkeypatch.setenv(hostid.ENV, CORE)
    session_host.reset()
    yield
    session_host.reset()


def _row(db, host):
    with db.reader() as conn:
        return conn.execute(
            "SELECT hostname, name, route, version, first_ts, last_ts FROM main.machine WHERE host = ?",
            (host,),
        ).fetchone()


def _reg(db, host, now, **kw):
    return db.write_sync(lambda conn: machines.register(conn, host, now_ms=now, **kw))


def test_register_inserts_and_updates_on_change(db):
    assert _reg(db, LAPTOP, 1_000, hostname="laptop", route="s1", version="abcdef12")
    assert _row(db, LAPTOP) == ("laptop", None, "s1", "abcdef12", 1_000, 1_000)
    # Поменялся путь — пишется сразу, хотя минута не прошла.
    assert _reg(db, LAPTOP, 2_000, hostname="laptop", route="lan", version="abcdef12")
    assert _row(db, LAPTOP) == ("laptop", None, "lan", "abcdef12", 1_000, 2_000)


def test_last_ts_throttled_once_a_minute(db):
    _reg(db, LAPTOP, 1_000, hostname="laptop", route="s1", version=None)
    assert not _reg(db, LAPTOP, 1_000 + machines.TOUCH_MS - 1, hostname="laptop", route="s1", version=None)
    assert _row(db, LAPTOP)[5] == 1_000
    assert _reg(db, LAPTOP, 1_000 + machines.TOUCH_MS, hostname="laptop", route="s1", version=None)
    assert _row(db, LAPTOP)[5] == 1_000 + machines.TOUCH_MS


def test_old_client_without_machine_keeps_values(db):
    """Старый клиент (`reported=False`): строка появляется без имени хоста; известное не стирается."""
    _reg(db, OTHER, 1_000, reported=False)
    assert _row(db, OTHER)[:4] == (None, None, None, None)
    _reg(db, LAPTOP, 1_000, hostname="laptop", route="s1", version="abcdef12")
    _reg(db, LAPTOP, 1_000 + machines.TOUCH_MS, reported=False)
    assert _row(db, LAPTOP) == ("laptop", None, "s1", "abcdef12", 1_000, 1_000 + machines.TOUCH_MS)


def test_register_keeps_name_and_ignores_bad_host(db):
    _reg(db, LAPTOP, 1_000, hostname="laptop", route="s1", version=None)
    db.write_sync(lambda conn: machines.rename(conn, LAPTOP, "ноутбук", now_ms=1_000))
    _reg(db, LAPTOP, 99_000, hostname="laptop2", route="lan", version=None)
    assert _row(db, LAPTOP)[:3] == ("laptop2", "ноутбук", "lan")
    assert not _reg(db, "not-a-host", 1_000, hostname="x")
    assert not _reg(db, None, 1_000, hostname="x")


def test_core_host_always_route_core(db):
    """Своя машина ядра — `route = core`, что бы ни прислал клиент; имя хоста и версия — не от клиента."""
    db.write_sync(lambda conn: machines.register_core(conn, now_ms=1_000))
    hostname, _name, route, version, _first, _last = _row(db, CORE)
    assert route == "core"
    _reg(db, CORE, 2_000, hostname="подделка", route="s1", version="0000000")
    assert _row(db, CORE)[:4] == (hostname, None, "core", version)


def test_register_core_moves_core_mark(db):
    """Переезд ядра: у прежней машины ядра отметка `core` снимается при старте ядра на новой."""
    db.write_sync(lambda conn: machines.register_core(conn, now_ms=1_000, host=LAPTOP))
    assert _row(db, LAPTOP)[2] == "core"
    db.write_sync(lambda conn: machines.register_core(conn, now_ms=2_000))
    assert _row(db, LAPTOP)[2] is None
    assert _row(db, CORE)[2] == "core"
    with db.reader() as conn:
        assert machines.label(conn, LAPTOP)["core"] is False


def test_rename_rules(db):
    _reg(db, LAPTOP, 1_000, hostname="my-laptop", route="s1", version=None)
    with db.reader() as conn:
        assert machines.label(conn, LAPTOP)["display"] == "my-laptop"
    out = db.write_sync(lambda conn: machines.rename(conn, LAPTOP, "  ноутбук ", now_ms=1_000))
    assert out["name"] == "ноутбук" and out["display"] == "ноутбук"
    out = db.write_sync(lambda conn: machines.rename(conn, LAPTOP, "", now_ms=1_000))
    assert out["name"] is None and out["display"] == "my-laptop"
    with pytest.raises(machines.MachineError) as exc:
        db.write_sync(lambda conn: machines.rename(conn, LAPTOP, "x" * 33, now_ms=1_000))
    assert exc.value.status == 422
    with pytest.raises(machines.MachineError) as exc:
        db.write_sync(lambda conn: machines.rename(conn, LAPTOP, "a\x07b", now_ms=1_000))
    assert exc.value.status == 422
    with pytest.raises(machines.MachineError) as exc:
        db.write_sync(lambda conn: machines.rename(conn, OTHER, "сервер", now_ms=1_000))
    assert exc.value.status == 404
    assert (
        db.write_sync(lambda conn: machines.rename(conn, LAPTOP, "x" * 32, now_ms=1_000))["name"] == "x" * 32
    )


def test_rename_core_without_row_creates_it(db):
    out = db.write_sync(lambda conn: machines.rename(conn, CORE, "стационарник", now_ms=1_000))
    assert out["core"] is True and out["display"] == "стационарник" and out["route"] == "core"


def test_of_session():
    assert machines.of_session(None, 4242) == CORE  # старый клиент без машины — машина ядра
    assert machines.of_session("", 4242) == CORE
    assert machines.of_session(LAPTOP, -7) == CORE  # сессия хаба — всегда машина ядра
    assert machines.of_session(LAPTOP, 4242) == LAPTOP


def test_label_shapes(db):
    _reg(db, LAPTOP, 1_000, hostname="laptop", route="s1", version="abcdef12")
    with db.reader() as conn:
        far = machines.label(conn, LAPTOP, now_ms=1_000 + machines.ONLINE_MS)
        stale = machines.label(conn, LAPTOP, now_ms=2_000 + machines.ONLINE_MS)
        unknown = machines.label(conn, OTHER, now_ms=1_000)
        core = machines.label(conn, None)
    assert far == {
        "host": LAPTOP,
        "name": None,
        "hostname": "laptop",
        "display": "laptop",
        "route": "s1",
        "core": False,
        "online": True,
        "last_ts": 1_000,
        "version": "abcdef12",
    }
    assert stale["online"] is False
    assert unknown["display"] == "машина 0e0e0e" and unknown["hostname"] is None
    assert (
        core["host"] == CORE and core["core"] is True and core["online"] is True and core["route"] == "core"
    )


def test_list_all_core_first(db):
    _reg(db, LAPTOP, 5_000, hostname="laptop", route="s1", version=None)
    _reg(db, OTHER, 9_000, hostname="server", route="lan", version=None)
    with db.reader() as conn:
        items = machines.list_all(conn, now_ms=9_000)
    assert [m["host"] for m in items] == [CORE, OTHER, LAPTOP]


# --- клиент: файл маршрута и версия ------------------------------------------------------------------------


def test_local_machine_reads_route_file(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    assert machine_info.local_machine(env)["route"] is None  # туннеля нет
    (tmp_path / "hub-link.route").write_text("s1-socks\n")
    info = machine_info.local_machine(env)
    assert info["route"] == "s1-socks"
    assert set(info) == {"hostname", "route", "version"}
    (tmp_path / "hub-link.route").write_text("rm -rf /\n")
    assert machine_info.read_route(env) is None  # мусор в файле — неизвестно


def test_clean_hostname():
    assert machine_info.clean_hostname("desktop") == "desktop"
    assert machine_info.clean_hostname("a\nb\x00c") == "abc"
    assert machine_info.clean_hostname("x" * 80) == "x" * 64
    assert machine_info.clean_hostname("") is None


def test_buildinfo_without_git(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert buildinfo._run(tmp_path) is None


def test_buildinfo_not_a_repo(tmp_path):
    assert buildinfo._run(tmp_path) is None


def test_buildinfo_in_repo():
    value = buildinfo._run(buildinfo.Path(buildinfo.__file__).resolve().parent)
    assert value is None or buildinfo.COMMIT_RE.fullmatch(value)
