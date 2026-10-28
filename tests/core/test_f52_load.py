"""Ф52-1 (docs/architecture.md): учёт нагрузки ноутбука — `core/load.py`.
Главное: зависших записей не бывает — чистка по сроку, по мёртвому pid, по несовпавшему времени старта.
`/proc` процессов подменяется (`load.proc_start`); снимок машины читает настоящий `/proc`."""

import pytest

from hub.core import load

NOW = 1_760_000_000_000
MIN = 60_000


@pytest.fixture
def procs(monkeypatch):
    """Живые процессы: pid -> время старта."""
    table = {100: 5000, 200: 6000}
    monkeypatch.setattr(load, "proc_start", lambda pid: table.get(pid))
    monkeypatch.setattr(load, "machine_limits", lambda: (16, 32 * 1024))  # «машина» теста
    return table


def _add(db, *, now=NOW, **over):
    fields = {"who": "Umbrella / оркестратор", "what": "сборка образов", "cores": 4, "mem_mb": 6144}
    fields.update({"pid": 100, "minutes": 30})
    fields.update(over)
    return db.write_sync(lambda conn: load.add(conn, **fields, now_ms=now))


def _snapshot(db, now=NOW):
    return db.write_sync(lambda conn: load.snapshot(conn, now))


def _ids(db) -> list[int]:
    with db.reader() as conn:
        return [row[0] for row in conn.execute("SELECT id FROM main.load_claim ORDER BY id")]


def test_add_and_snapshot(db, procs):
    claim = _add(db)
    assert claim["minutes"] == 30
    assert claim["started_ts"] == NOW
    # срок: минуты × 1,5 + 5 минут запаса
    assert claim["expires_ts"] == NOW + 45 * MIN + 5 * MIN
    _add(db, pid=200, who="hub", what="полный прогон", cores=8, mem_mb=2048, minutes=25)
    body = _snapshot(db, NOW + 3 * MIN)
    assert body["now_ts"] == NOW + 3 * MIN
    assert [c["what"] for c in body["claims"]] == ["сборка образов", "полный прогон"]
    assert body["claimed"] == {"cores": 12, "mem_mb": 8192}
    assert body["claims"][1]["minutes"] == 25
    with db.reader() as conn:
        assert conn.execute("SELECT pid, pid_start FROM main.load_claim ORDER BY id").fetchall() == [
            (100, 5000),
            (200, 6000),
        ]


def test_system_reads_proc():
    system = load.system()
    assert isinstance(system["cores"], int) and system["cores"] >= 1
    assert isinstance(system["load1"], float) and isinstance(system["load5"], float)
    assert system["mem_total_mb"] > 0
    assert 0 < system["mem_available_mb"] <= system["mem_total_mb"]


def test_purge_by_deadline(db, procs):
    claim = _add(db, minutes=10)  # срок: 15 + 5 = 20 минут
    assert _snapshot(db, claim["expires_ts"] - 1)["claims"]
    assert _snapshot(db, claim["expires_ts"])["claims"] == []
    assert _ids(db) == []


def test_purge_by_dead_pid(db, procs):
    _add(db)
    _add(db, pid=200)
    del procs[100]
    body = _snapshot(db)
    assert [c["pid"] for c in body["claims"]] == [200]
    assert len(_ids(db)) == 1


def test_purge_by_changed_start_time(db, procs):
    """pid жив, но время старта другое — это уже чужой процесс, занявший тот же номер."""
    _add(db)
    procs[100] = 9999
    assert _snapshot(db)["claims"] == []
    assert _ids(db) == []


def test_add_purges_too(db, procs):
    _add(db)
    del procs[100]
    _add(db, pid=200)
    assert len(_ids(db)) == 1


def test_stale_ids_is_read_only_and_read_skips(db, procs):
    first = _add(db)
    second = _add(db, pid=200)
    del procs[100]
    with db.reader() as conn:
        stale = load.stale_ids(conn, NOW)
        body = load.read(conn, NOW, skip=stale)
    assert stale == [first["id"]]
    assert [c["id"] for c in body["claims"]] == [second["id"]]
    assert body["claimed"] == {"cores": 4, "mem_mb": 6144}
    assert len(_ids(db)) == 2  # чтение ничего не удалило


def test_done_removes_and_missing_is_ok(db, procs):
    claim = _add(db)
    assert db.write_sync(lambda conn: load.done(conn, claim["id"])) is True
    assert db.write_sync(lambda conn: load.done(conn, claim["id"])) is False
    assert _ids(db) == []


def test_pid_must_be_alive(db, procs):
    with pytest.raises(load.LoadError) as err:
        _add(db, pid=777)
    assert err.value.status == 422 and "777" in err.value.detail
    assert _ids(db) == []


@pytest.mark.parametrize(
    "over",
    [
        {"minutes": 0},
        {"minutes": 241},
        {"minutes": 2.5},
        {"minutes": True},
        {"cores": 0},
        {"cores": "4"},
        {"mem_mb": -1},
        {"cores": 17},  # ревью: больше ядер, чем у машины
        {"cores": 4096},
        {"mem_mb": 32 * 1024 + 1},  # ревью: больше всей памяти машины
        {"pid": 0},
        {"who": ""},
        {"who": "к" * 81},
        {"what": "   "},
        {"what": "ч" * 121},
        {"what": None},
    ],
)
def test_limits_refused(db, procs, over):
    with pytest.raises(load.LoadError) as err:
        _add(db, **over)
    assert err.value.status == 422
    assert _ids(db) == []


def test_limits_accepted_at_edges(db, procs):
    _add(db, minutes=1, cores=1, mem_mb=0, who="к" * 80, what="ч" * 120)
    claim = _add(db, minutes=240)
    assert claim["expires_ts"] == NOW + 360 * MIN + 5 * MIN
    # перевод строки в названии не ломает таблицу
    assert _add(db, what="сборка\nобразов")["what"] == "сборка образов"


def test_machine_edges_accepted(db, procs):
    """Ревью Ф52-1: ровно столько, сколько есть у машины, заявить можно."""
    claim = _add(db, cores=16, mem_mb=32 * 1024)
    assert (claim["cores"], claim["mem_mb"]) == (16, 32 * 1024)


def test_machine_limits_read_this_machine(monkeypatch):
    import os

    cores, mem_mb = load.machine_limits()
    assert cores == os.cpu_count() and 0 < mem_mb <= load.MEM_MB_MAX
    assert mem_mb == load.system()["mem_total_mb"]
    monkeypatch.setattr(load, "_meminfo", lambda: (None, None))
    monkeypatch.setattr(load.os, "cpu_count", lambda: None)
    assert load.machine_limits() == (load.CORES_MAX, load.MEM_MB_MAX)  # не прочитать — общий потолок


def test_owned_limit_per_token(db, procs):
    """Ревью Ф52-1: `owned` — записи одного токена; живых уже 10 — отказ 409, чужие и отжившие не в счёт."""
    mine = [_add(db, owned=[])["id"] for _ in range(load.TOKEN_MAX_CLAIMS)]
    with pytest.raises(load.LoadError) as err:
        _add(db, owned=mine)
    assert err.value.status == 409 and "токена" in err.value.detail
    assert len(_ids(db)) == load.TOKEN_MAX_CLAIMS
    _add(db, owned=[])  # другой токен
    _add(db)  # админ-сокет: без предела на токен
    load_done = db.write_sync(lambda conn: load.done(conn, mine[0]))
    assert load_done
    _add(db, owned=mine)  # одну сняли — место есть


def test_no_more_than_50_claims(db, procs):
    for _ in range(load.MAX_CLAIMS):
        _add(db)
    with pytest.raises(load.LoadError) as err:
        _add(db)
    assert err.value.status == 409
    assert len(_ids(db)) == load.MAX_CLAIMS
    # отжившие место освобождают
    del procs[100]
    _add(db, pid=200)
    assert len(_ids(db)) == 1


# --- Ф33-6: записи другой машины ---------------------------------------------------------------------------

FOREIGN = "f" * 16


def test_foreign_host_skips_proc(db, procs, monkeypatch):
    """Чужая машина: pid не ищем в /proc ядра, пределы — общие, запись живёт по сроку и `done`."""
    monkeypatch.setenv("HUB_HOST_ID", "1" * 16)  # машина ядра
    claim = _add(db, pid=424242, host=FOREIGN, cores=64, mem_mb=200 * 1024)
    assert claim["host"] == FOREIGN
    assert _snapshot(db, NOW + MIN)["claims"][0]["pid"] == 424242  # чистка pid не тронула
    assert _snapshot(db, NOW + 60 * MIN)["claims"] == []  # срок прошёл
    claim = _add(db, pid=424242, host=FOREIGN)
    db.write_sync(lambda conn: load.done(conn, claim["id"]))
    assert _ids(db) == []


def test_local_host_checks_proc_as_before(db, procs, monkeypatch):
    monkeypatch.setenv("HUB_HOST_ID", "1" * 16)
    with pytest.raises(load.LoadError) as err:
        _add(db, pid=424242, host="1" * 16)  # своя метка = машина ядра
    assert err.value.status == 422
    claim = _add(db, host="1" * 16)
    assert claim["host"] == "1" * 16
    del procs[100]
    assert _snapshot(db)["claims"] == []
    with pytest.raises(load.LoadError):
        _add(db, host="not-hex")


def test_localize_shows_own_machine_only():
    from hub import load_text

    body = {
        "host": "a" * 16,
        "system": {"cores": 64},
        "now_ts": 0,
        "claims": [
            {"id": 1, "host": "a" * 16, "cores": 4, "mem_mb": 1024},
            {"id": 2, "host": FOREIGN, "cores": 2, "mem_mb": 2048},
        ],
        "claimed": {"cores": 6, "mem_mb": 3072},
    }
    mine = load_text.localize(body, FOREIGN, {"cores": 8})
    assert mine["system"] == {"cores": 8} and [c["id"] for c in mine["claims"]] == [2]
    assert mine["claimed"] == {"cores": 2, "mem_mb": 2048} and mine["other_host"] == 1
    assert "на другой машине: 1" in load_text.render(mine)
    assert load_text.localize(body, "a" * 16, {"cores": 8}) is body
    assert load_text.localize(body, None, {"cores": 8}) is body
