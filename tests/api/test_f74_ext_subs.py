"""Ф74-2 (docs/architecture.md; план 6.2 Б2, В2): подписки внешних Codex и Cursor
в ядре. Личность — `(pid, proc_start, "<движок>:<разговор>")` процесса движка, `kind` — имя движка. Свойства —
из профиля: один процесс держит много разговоров (другой разговор — не «сменившийся»), брошенная подписка —
по сроку профиля (Codex 10 мин, Cursor 6 ч: ожидание у Cursor идёт только между ходами), движок — в столбце
`session_engine`.

Обвязка — как в `test_oc_subs.py`."""

from pathlib import Path

from hub.core import chan_subs, runner, subs
from hub.core import engine as engine_mod

from . import test_oc_subs as oc_tests
from .test_engine import API, PID, START, _bearer, _chan_subscribe, _sub_rows, _subscribe
from .test_oc_subs import _sess

procs = oc_tests.procs  # фикстуры: живые процессы и два проекта с подписками
world = oc_tests.world

MIN_MS = 60_000
CX_A, CX_B = "codex:019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a51", "codex:019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a52"
CU_A = "cursor:5c2e9a7b-1d3f-4e6a-8b9c-0d1e2f3a4b5c"


def _engines(db, table="session_sub") -> list[tuple]:
    with db.reader() as conn:
        sql = f"SELECT session_id, session_engine FROM main.{table} ORDER BY id"  # noqa: S608
        return conn.execute(sql).fetchall()


async def test_two_codex_conversations_of_one_process_live_side_by_side(client, db, world, procs):
    a = await _subscribe(client, world, sid=CX_A, kind="codex", project_id=world["first"])
    assert a.status_code == 200, a.text
    b = await _subscribe(
        client, world, sid=CX_B, kind="codex", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert b.status_code == 200, b.text
    # Тот же процесс, другой разговор: у Claude Code это `conversation_changed`, у Codex — соседняя сессия.
    assert [r[3:] for r in _sub_rows(db)] == [(None, None), (None, None)]
    assert _engines(db) == [(CX_A, "codex"), (CX_B, "codex")]
    assert a.json()["sub"]["engine"] == "codex"
    # Снять чужую подписку тем же процессом нельзя (403), её не трогают.
    r = await client.post(
        f"{API}/subs/{a.json()['sub']['id']}/end",
        json={"session": _sess(CX_B), "reason": "unsubscribed"},
        headers=_bearer(world["sess"]),
    )
    assert r.status_code == 403, r.text
    assert all(row[3] is None for row in _sub_rows(db))


async def test_cursor_chan_sub_engine_and_kind_prefix_must_match(client, db, world, procs):
    r = await _chan_subscribe(client, world, world["chan"], sid=CU_A, kind="cursor")
    assert r.status_code == 200, r.text
    assert _engines(db, "session_chan_sub") == [(CU_A, "cursor")]
    # `kind` движка без приставки разговора и приставка без своего `kind` — 422.
    for sid, kind in ((CU_A.removeprefix("cursor:"), "cursor"), (CU_A, "codex"), (CX_A, "interactive")):
        bad = await _subscribe(client, world, sid=sid, kind=kind, project_id=world["first"])
        assert bad.status_code == 422, (sid, kind, bad.text)


async def test_ext_idle_grace_from_profile(client, db, clock, world, procs, app):
    """В2: Codex — 10 мин без ожидания (его ожидатель живёт отдельно от хода), Cursor — 6 ч (ожидание только
    в хуке `stop` между ходами: ход дольше 10 мин подписку не снимает)."""
    cx = await _subscribe(client, world, sid=CX_A, kind="codex", project_id=world["first"])
    cu = await _subscribe(
        client, world, sid=CU_A, kind="cursor", project_id=world["second"], cwd=world["dirs"][1]
    )
    assert cx.status_code == 200 and cu.status_code == 200, (cx.text, cu.text)
    t0 = int(clock() * 1000)

    def cleanup(now_ms):
        return db.write_sync(
            lambda conn: runner._cleanup_all(conn, now_ms, app.state.sub_waiters, app.state.chan_sub_waiters)
        )

    assert cleanup(t0 + 9 * MIN_MS) == []
    assert cleanup(t0 + 11 * MIN_MS) == [(str(cx.json()["sub"]["id"]), "session_gone")]
    assert cleanup(t0 + 5 * 60 * MIN_MS) == []  # Cursor: ход в 5 ч без ожидания — подписка жива
    assert cleanup(t0 + 6 * 60 * MIN_MS + 1) == [(str(cu.json()["sub"]["id"]), "session_gone")]


def test_rec_engine_and_props_from_profile():
    base = subs.SubRec(1, 1, 1, PID, START, CX_A, "/", None, 0, None, None, None, None)
    assert subs.rec_engine(base) == "codex"  # строка без разметки — по приставке
    assert subs.rec_engine(base._replace(session_engine="cursor")) == "cursor"  # столбец главнее
    assert subs.rec_many_convs(base)
    assert not subs.rec_many_convs(base._replace(session_pid=-3, session_id="hub:3", session_engine="codex"))
    assert not subs.rec_many_convs(base._replace(session_id="0f6f6d6a-9c3d", session_engine="claude"))
    # Сессия хаба на Codex: брошенной по сроку не бывает (её живость держит хост).
    held = base._replace(session_pid=-3, session_proc_start=0, session_id="hub:3", session_engine="codex")
    assert not subs.oc_stale(held, 10**9, waiting=False)
    assert subs.oc_stale(base, 11 * MIN_MS, waiting=False)
    assert not subs.oc_stale(base._replace(session_engine="cursor"), 11 * MIN_MS, waiting=False)
    assert engine_mod.engine_of_session(None, CX_A) == "codex"
    assert engine_mod.engine_of_session("cursor", "x") == "cursor"
    assert engine_mod.engine_of_session(None, "hub:4") == "claude"
    assert subs.SESSION_KINDS[-2:] == ("cursor", "codex")


async def test_migration_0105_backfills_engine(client, db, world, procs):
    """Старые строки (столбец пуст): `ses_…` — OpenCode, прочее — Claude Code; сессия хаба — движок её записи
    (подзапрос к `hub_session`). Проверяются UPDATE-ы файла миграции на строках без разметки."""
    for sid, kind in (
        ("ses_AAAAAAAAAA1", "opencode"),
        ("0f6f6d6a-9c3d-4d3f-9c9c-2f1a0e5d4c3b", "interactive"),
    ):
        r = await _subscribe(client, world, sid=sid, kind=kind, project_id=world["first"])
        assert r.status_code == 200, r.text
        r = await _chan_subscribe(client, world, world["chan"], sid=sid, kind=kind)
        assert r.status_code == 200, r.text
    sql = Path(chan_subs.__file__).parent / "migrations" / "0105_main.sql"
    updates = [s.strip() for s in sql.read_text(encoding="utf-8").split(";") if "UPDATE" in s]
    assert len(updates) == 2

    def backfill(conn):
        for table in ("session_sub", "session_chan_sub"):
            conn.execute(f"UPDATE main.{table} SET session_engine = NULL")  # noqa: S608
        for statement in updates:
            body = "\n".join(ln for ln in statement.splitlines() if not ln.lstrip().startswith("--"))
            conn.execute(body)

    db.write_sync(backfill)
    expected = [("ses_AAAAAAAAAA1", "opencode"), ("0f6f6d6a-9c3d-4d3f-9c9c-2f1a0e5d4c3b", "claude")]
    assert _engines(db) == expected
    assert _engines(db, "session_chan_sub") == expected
