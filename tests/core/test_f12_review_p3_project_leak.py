"""П3: проект заготовки получает только беседа, созданная эхом ЭТОЙ отправки. Реализация сравнивает
first_seen_ts с outbox.created_ts (временем создания черновика), а не со временем отправки: беседа, которую
человек начал сам, пока черновик ждал подтверждения, уезжает в проект токена вместе с его сообщениями."""

import json

import pytest

from hub.core import ingest, new_recipient, outbox


@pytest.fixture
def env(db):
    """Стенд теста ревьюера (`.runs/critic-f12-1/conftest.py`): два проекта, Telegram, почта, токен."""

    def _fn(conn):
        a = conn.execute("INSERT INTO main.project (name) VALUES ('IIP')").lastrowid
        b = conn.execute("INSERT INTO main.project (name) VALUES ('Initech')").lastrowid
        tg = conn.execute(
            "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
            # Ф12-2: окно графика — круглые сутки; первая отправка — через случайную паузу до `pace_gap_min_s`
            # (24.10), здесь не дольше секунды (график здесь не проверяется).
            (
                json.dumps(
                    {
                        "idempotent_send": True,
                        "pace_window": "00:00-24:00",
                        "pace_gap_min_s": 1,
                        "pace_gap_max_s": 1,
                    }
                ),
            ),
        ).lastrowid
        mail = conn.execute(
            "INSERT INTO main.account (source, db, config_json) VALUES ('mail', 'main', ?)",
            (json.dumps({"address": "me@gmail.com"}),),
        ).lastrowid
        tok = conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('mcp:iip', 'interactive', 'x')"
        ).lastrowid
        return {"a": a, "b": b, "tg": tg, "mail": mail, "tok": tok}

    return db.write_sync(_fn)


def test_conversation_started_by_peer_while_draft_pending_keeps_its_project(db, env):
    created = db.write_sync(
        lambda conn: new_recipient.create_draft(
            conn,
            account_id=env["tg"],
            project_id=env["a"],
            to="@petr_ivanov",
            text="Здравствуйте",
            subject=None,
            from_address=None,
            api_token_id=env["tok"],
            idempotency_key=None,
            now_ms=1_000,
        )
    )
    # Пётр пишет сам (черновик ещё ждёт подтверждения); беседа разложена правилом в Initech.
    db.write_sync(
        lambda conn: ingest.apply_batch(
            conn,
            account_id=env["tg"],
            cursor=None,
            now_ms=2_000,
            events=[
                {
                    "type": "upsert",
                    "channel": {"ext_id": "555", "kind": "dm", "title": "Пётр"},
                    "ext_id": "1",
                    "ts": 2_000,
                    "is_me": False,
                    "kind": "text",
                    "text": "секрет Initech",
                    "mentions_me": False,
                }
            ],
        )
    )
    target = db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'auto' WHERE ext_id = '555' RETURNING id",
            (env["b"],),
        ).fetchone()[0]
    )
    db.write_sync(lambda conn: outbox.approve(conn, created.outbox_id, now_ms=3_000))
    lease = db.write_sync(lambda conn: outbox.lease(conn, account_id=env["tg"], owner="c", now_ms=4_000))[0]
    db.write_sync(
        lambda conn: outbox.report(
            conn,
            outbox_id=lease.outbox_id,
            owner="c",
            lease_gen=lease.lease_gen,
            outcome="sent",
            ext_id="2",
            peer_ext_id="555",
            peer_title="Пётр",
            now_ms=4_000,
        )
    )
    with db.reader() as conn:
        project_id, map_source = conn.execute(
            "SELECT project_id, map_source FROM main.channel WHERE id = ?", (target,)
        ).fetchone()
    # Прошлая переписка (сообщение Петра до отправки) не должна переезжать к проектному токену.
    assert (project_id, map_source) == (env["b"], "auto")
