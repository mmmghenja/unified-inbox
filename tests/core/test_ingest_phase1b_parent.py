"""Тест 5 (часть «через ядро») куска 1b фазы 1 (`docs/architecture.md`): `ChannelRef.parent` —
форум без своих сообщений создаётся из вложенной ссылки топика, топик раскладывается «как у форума». Стиль и
фикстуры — `tests/core/test_ingest_phase1.py` (тот же локальный `db`, `apply_batch` напрямую, без HTTP)."""

import pytest

from hub.core.db import Db
from hub.core.ingest import apply_batch


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


def _insert_account(db, *, source: str = "telegram") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, 'main')", (source,)).lastrowid
        )
    )


def _insert_project(db, *, name: str = "P") -> int:
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES (?)", (name,)).lastrowid
    )


def _add_rule(db, *, account_id: int, project_id: int, match: dict, priority: int = 0) -> None:
    import json

    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps(match), project_id, priority),
        )
    )


def _topic_upsert(ext_id: str, ts: int, *, chat_ext: str = "-100", topic_id: int = 42, parent_attrs=None):
    parent_ref = {"ext_id": chat_ext, "kind": "group", "title": "Форум", "own_id_space": True}
    if parent_attrs is not None:
        parent_ref["attrs"] = parent_attrs
    channel = {
        "ext_id": f"{chat_ext}:{topic_id}",
        "kind": "topic",
        "title": "Обсуждение",
        "parent_ext_id": chat_ext,
        "own_id_space": True,
        "parent": parent_ref,
    }
    return {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": "hi",
        "mentions_me": False,
    }


def _apply(db, account_id, events, now_ms=1000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def test_1b_05_forum_without_own_messages_is_created_via_channel_ref_parent(db):
    account_id = _insert_account(db)
    result = _apply(db, account_id, [_topic_upsert("m1", 1000)])
    assert result.applied == 1
    assert result.dead == 0

    with db.reader() as conn:
        forum = conn.execute("SELECT id, kind, title FROM main.channel WHERE ext_id = '-100'").fetchone()
        topic = conn.execute(
            "SELECT id, parent_channel_id, kind FROM main.channel WHERE ext_id = '-100:42'"
        ).fetchone()
    assert forum is not None
    assert forum[1] == "group"
    assert forum[2] == "Форум"
    assert topic is not None
    assert topic[1] == forum[0]
    assert topic[2] == "topic"


def test_1b_05_topic_follows_forum_project_and_manual_reassignment(db):
    account_id = _insert_account(db)
    project_id = _insert_project(db, name="Работа")
    _add_rule(db, account_id=account_id, project_id=project_id, match={"channel_ext_id": "-100"})

    _apply(db, account_id, [_topic_upsert("m1", 1000)])

    with db.reader() as conn:
        forum_row = conn.execute("SELECT id, project_id FROM main.channel WHERE ext_id = '-100'").fetchone()
        topic_row = conn.execute(
            "SELECT project_id, map_reason FROM main.channel WHERE ext_id = '-100:42'"
        ).fetchone()
    assert forum_row[1] == project_id
    assert topic_row == (project_id, "как у форума")

    # Ручная перепривязка форума -> топик следует за ней (раздел B спеки куска 1a): в проде это делает
    # `PATCH /channels/{id}` (`channels.update_channel`, вне объёма куска 1b) — здесь тот же эффект
    # воспроизводится напрямую через `mapping.recompute_topics`, ту же функцию, которую он вызывает.
    from hub.core import mapping

    other_project = _insert_project(db, name="Другое")

    def _reassign(conn):
        conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual', map_reason = 'manual' "
            "WHERE id = ?",
            (other_project, forum_row[0]),
        )
        mapping.recompute_topics(conn, "main", forum_row[0], 3000)

    db.write_sync(_reassign)
    with db.reader() as conn:
        topic_row2 = conn.execute(
            "SELECT project_id, map_reason FROM main.channel WHERE ext_id = '-100:42'"
        ).fetchone()
    assert topic_row2 == (other_project, "как у форума")


def test_1b_05_parent_ext_id_mismatch_is_poison_event(db):
    account_id = _insert_account(db)
    event = _topic_upsert("m1", 1000)
    event["channel"]["parent"]["ext_id"] = "-999"  # не совпадает с parent_ext_id ("-100")

    result = _apply(db, account_id, [event])
    assert result.dead == 1
    assert result.applied == 0

    with db.reader() as conn:
        forum = conn.execute("SELECT 1 FROM main.channel WHERE ext_id = '-100'").fetchone()
        bad_parent = conn.execute("SELECT 1 FROM main.channel WHERE ext_id = '-999'").fetchone()
    assert forum is None
    assert bad_parent is None


def test_1b_05_existing_parent_is_reused_not_recreated(db):
    account_id = _insert_account(db)
    # Форум уже существует (создан своим сообщением ранее).
    forum_event = {
        "type": "upsert",
        "channel": {"ext_id": "-100", "kind": "group", "title": "Форум"},
        "ext_id": "root",
        "ts": 500,
        "is_me": False,
        "kind": "text",
        "text": "первое",
        "mentions_me": False,
    }
    _apply(db, account_id, [forum_event])
    with db.reader() as conn:
        forum_id_before = conn.execute("SELECT id FROM main.channel WHERE ext_id = '-100'").fetchone()[0]

    _apply(db, account_id, [_topic_upsert("m1", 1000)])
    with db.reader() as conn:
        forum_id_after = conn.execute("SELECT id FROM main.channel WHERE ext_id = '-100'").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = '-100'").fetchone()[0]
    assert forum_id_after == forum_id_before
    assert count == 1
