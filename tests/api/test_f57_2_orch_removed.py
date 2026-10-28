"""Ф57-2 (docs/architecture.md): чата оркестратора 9c больше нет, а старые данные
читаются без падения:

- база с таблицами `orch_conversation`/`orch_message` и строками в них открывается ядром (таблицы остаются);
- токен со старым скоупом `orchestrate` в базе не роняет проверку, а новым такой скоуп не выдаётся;
- ручек `/orch*` нет."""

import pytest

from hub.core import security
from hub.core.db import Db

ORIGIN = "http://127.0.0.1:8787"


def test_db_with_old_orch_tables_and_rows_opens(tmp_path):
    first = Db(tmp_path)
    first.start()
    try:

        def fill(conn):
            cid = conn.execute(
                "INSERT INTO work.orch_conversation (id, started_ts, updated_ts, seq) VALUES (?, 1, 1, 5000)",
                (2**40 + 1,),
            ).lastrowid
            conn.execute(
                "INSERT INTO work.orch_message (id, conversation_id, role, text, created_ts, seq) "
                "VALUES (?, ?, 'me', 'старая реплика', 1, 5001)",
                (2**40 + 2, cid),
            )

        first.write_sync(fill)
    finally:
        first.close()

    second = Db(tmp_path)  # повторное открытие: проверка целостности `seq` читает и эти таблицы
    second.start()
    try:
        with second.reader() as conn:
            assert conn.execute("SELECT COUNT(*) FROM work.orch_conversation").fetchone()[0] == 1
            assert conn.execute("SELECT text FROM work.orch_message").fetchone()[0] == "старая реплика"
    finally:
        second.close()


def _old_orchestrate_token(db) -> str:
    """Токен, выданный до Ф57-2: имя `mcp:orchestrator`, скоуп `orchestrate` — записью в базу напрямую."""
    token = "hub_oldorch_" + "a" * 40

    def _fn(conn):
        conn.execute(
            "INSERT INTO main.api_token (name, kind, hash, scopes, project_scope_json, expires_ts, revoked) "
            "VALUES ('mcp:orchestrator', 'interactive', ?, 'read draft organize orchestrate', NULL, NULL, 0)",
            (security._sha256(token),),  # noqa: SLF001
        )

    db.write_sync(_fn)
    return token


async def test_old_orchestrate_token_does_not_break_auth(db, client):
    token = _old_orchestrate_token(db)
    r = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
    assert r.status_code == 200, r.text


def test_orchestrate_scope_is_not_issued_any_more(db, monkeypatch):
    monkeypatch.setattr(security, "BEARER_SCOPES", security.BEARER_SCOPES - {"orchestrate", "runs"})
    with pytest.raises(ValueError, match="orchestrate"):
        db.write_sync(
            lambda conn: security.create_api_token(
                conn,
                name="mcp:orchestrator",
                kind="interactive",
                scopes=["read", "draft", "organize", "orchestrate"],
                project_ids=None,
                expires_ts=None,
            )
        )


async def test_no_orch_routes(db, client):
    token = _old_orchestrate_token(db)
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    assert (await client.get("/api/v1/orch", headers=headers)).status_code == 404
    assert (await client.get("/api/v1/orch/work/lanes", headers=headers)).status_code == 404
