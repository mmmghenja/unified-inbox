"""Тест 5 куска 1a фазы 1 (раздел B спеки `docs/architecture.md`): раскладка по проектам —
приоритеты, ручная привязка, топик следует за форумом, удаление правила, `PATCH {map_source:"auto"}`,
`map_reason` с `label`. Стиль — `tests/core/test_ingest.py`/`tests/core/test_outbox.py` (свой `db`, прямой
доступ к `Db.write_sync`/`Db.reader`, без HTTP)."""

import json

import pytest

from hub.core import channels, mapping
from hub.core.db import Db
from hub.core.ingest import apply_batch


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


def _insert_account(db, *, source: str = "tg", db_name: str = "main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _insert_project(db, *, name: str = "P") -> int:
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES (?)", (name,)).lastrowid
    )


def _upsert_channel(db, account_id, *, ext_id, kind="dm", attrs=None, parent_ext_id=None, ts=1000):
    channel = {"ext_id": ext_id, "kind": kind}
    if attrs is not None:
        channel["attrs"] = attrs
    if parent_ext_id is not None:
        channel["parent_ext_id"] = parent_ext_id
    event = {
        "type": "upsert",
        "channel": channel,
        "ext_id": f"msg-{ext_id}",
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": "hi",
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=ts)
    )


def _channel(db, ext_id, *, schema="main"):
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT id, project_id, map_reason, map_source, seq FROM {schema}.channel WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()
    return row


def test_1a_05_priority_order_first_match_wins(db):
    account_id = _insert_account(db)
    low = _insert_project(db, name="low")
    high = _insert_project(db, name="high")
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"attr": "tg_folders", "has": 1}), low, 0),
        )
    )
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"attr": "tg_folders", "has": 1}), high, 10),
        )
    )

    _upsert_channel(db, account_id, ext_id="c1", attrs={"tg_folders": [1]})
    row = _channel(db, "c1")
    assert row[1] == high


def test_1a_05_manual_binding_not_touched_by_rules(db):
    account_id = _insert_account(db)
    project_a = _insert_project(db, name="a")
    project_b = _insert_project(db, name="b")
    _upsert_channel(db, account_id, ext_id="c1", attrs={"tg_folders": [1]})
    channel_id = _channel(db, "c1")[0]

    def _fn(conn):
        channels.update_channel(conn, channel_id, project_id=project_a, now_ms=1000)

    db.write_sync(_fn)
    assert _channel(db, "c1")[1] == project_a
    assert _channel(db, "c1")[3] == "manual"

    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"attr": "tg_folders", "has": 1}), project_b, 0),
        )
    )
    db.write_sync(lambda conn: mapping.recompute_account(conn, "main", account_id, 2000))
    row = _channel(db, "c1")
    assert row[1] == project_a  # manual — правило его не тронуло
    assert row[3] == "manual"


def test_1a_05_topic_follows_forum_including_manual_binding(db):
    account_id = _insert_account(db)
    project_forum = _insert_project(db, name="forum")

    _upsert_channel(db, account_id, ext_id="forum1", kind="group")
    _upsert_channel(db, account_id, ext_id="forum1:t1", kind="topic", parent_ext_id="forum1")

    forum_id = _channel(db, "forum1")[0]
    topic_row = _channel(db, "forum1:t1")
    assert topic_row[2] == "как у форума"
    assert topic_row[1] is None  # форум пока не разложен

    def _fn(conn):
        channels.update_channel(conn, forum_id, project_id=project_forum, now_ms=3000)

    db.write_sync(_fn)

    topic_row2 = _channel(db, "forum1:t1")
    assert topic_row2[1] == project_forum
    assert topic_row2[2] == "как у форума"


def test_1a_05_rule_removal_returns_channels_to_unsorted(db):
    account_id = _insert_account(db)
    project_id = _insert_project(db)
    rule_id = db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"attr": "tg_folders", "has": 1}), project_id, 0),
        ).lastrowid
    )
    _upsert_channel(db, account_id, ext_id="c1", attrs={"tg_folders": [1]})
    assert _channel(db, "c1")[1] == project_id

    def _fn(conn):
        return mapping.remove_rule(conn, rule_id, 4000)

    removed_account_id, changed = db.write_sync(_fn)
    assert removed_account_id == account_id
    assert changed == 1

    row = _channel(db, "c1")
    assert row[1] is None
    assert row[2] == "нет правил"


def test_1a_05_patch_map_source_auto_recomputes_immediately(db):
    account_id = _insert_account(db)
    project_rule = _insert_project(db, name="rule")
    project_manual = _insert_project(db, name="manual")
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"channel_ext_id": "c1"}), project_rule, 0),
        )
    )
    _upsert_channel(db, account_id, ext_id="c1")
    channel_id = _channel(db, "c1")[0]
    assert _channel(db, "c1")[1] == project_rule

    db.write_sync(
        lambda conn: channels.update_channel(conn, channel_id, project_id=project_manual, now_ms=5000)
    )
    assert _channel(db, "c1")[1] == project_manual
    assert _channel(db, "c1")[3] == "manual"

    db.write_sync(lambda conn: channels.update_channel(conn, channel_id, map_source="auto", now_ms=6000))
    row = _channel(db, "c1")
    assert row[1] == project_rule
    assert row[3] == "auto"
    assert "правило #" in row[2]


def test_1a_05_map_reason_uses_label_when_present(db):
    account_id = _insert_account(db)
    project_id = _insert_project(db)
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (
                account_id,
                json.dumps({"attr": "tg_folders", "has": 3, "label": "папка «Работа»"}),
                project_id,
                0,
            ),
        )
    )
    _upsert_channel(db, account_id, ext_id="c1", attrs={"tg_folders": [3]})
    row = _channel(db, "c1")
    assert row[2].endswith("папка «Работа»")


def test_1a_05_project_change_visible_via_seq_growth(db):
    """`/sync`/SSE читают `channel.seq` — проверяем сам рост seq при смене проекта (пересчёт правилом)."""
    account_id = _insert_account(db)
    project_a = _insert_project(db, name="a")
    project_b = _insert_project(db, name="b")
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"attr": "tg_folders", "has": 1}), project_a, 0),
        )
    )
    _upsert_channel(db, account_id, ext_id="c1", attrs={"tg_folders": [1]})
    seq_before = _channel(db, "c1")[4]

    rule_b = db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
            (account_id, json.dumps({"attr": "tg_folders", "has": 1}), project_b, 10),
        ).lastrowid
    )
    del rule_b
    db.write_sync(lambda conn: mapping.recompute_account(conn, "main", account_id, 9000))
    row = _channel(db, "c1")
    assert row[1] == project_b
    assert row[4] > seq_before


# --- Тест 10 спеки 2b: раскладка «весь аккаунт» ({"all": true}) --------------------------------------------


def test_2b_10_all_match_default_priority_and_rejects_false(db):
    account_id = _insert_account(db)
    project_all = _insert_project(db, name="all")
    rule_id, changed = db.write_sync(
        lambda conn: mapping.add_rule(
            conn, account_id=account_id, project_id=project_all, match={"all": True}, priority=None, now_ms=1
        )
    )
    del rule_id, changed
    with db.reader() as conn:
        priority = conn.execute("SELECT priority FROM main.map_rule").fetchone()[0]
    assert priority == mapping.DEFAULT_ALL_PRIORITY

    with pytest.raises(mapping.InvalidMatch):
        mapping.validate_match({"all": False})


def test_2b_10_all_matches_every_channel_but_loses_to_specific_rule_and_manual(db):
    account_id = _insert_account(db)
    project_catchall = _insert_project(db, name="catchall")
    project_specific = _insert_project(db, name="specific")

    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=account_id,
            project_id=project_catchall,
            match={"all": True},
            priority=None,
            now_ms=1,
        )
    )
    # каналы аккаунта -> все в catchall
    _upsert_channel(db, account_id, ext_id="c1")
    _upsert_channel(db, account_id, ext_id="c2")
    assert _channel(db, "c1")[1] == project_catchall
    assert _channel(db, "c2")[1] == project_catchall

    # частное правило с приоритетом 0 (умолчание) побеждает над {"all"} (-100)
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=account_id,
            project_id=project_specific,
            match={"channel_ext_id": "c1"},
            priority=None,
            now_ms=2,
        )
    )
    assert _channel(db, "c1")[1] == project_specific
    assert _channel(db, "c2")[1] == project_catchall  # остался под {"all"}

    # ручная привязка побеждает над {"all"} и не переезжает при пересчёте
    project_manual = _insert_project(db, name="manual")
    channel_id_2 = _channel(db, "c2")[0]
    db.write_sync(
        lambda conn: channels.update_channel(conn, channel_id_2, project_id=project_manual, now_ms=3)
    )
    db.write_sync(lambda conn: mapping.recompute_account(conn, "main", account_id, 4))
    assert _channel(db, "c2")[1] == project_manual
    assert _channel(db, "c2")[3] == "manual"


# --- Ф14-1 A3: предпросмотр — тот же резолвер, что пересчёт -------------------------------------------------


def _projects_by_channel(db) -> dict[int, int | None]:
    with db.reader() as conn:
        return dict(conn.execute("SELECT id, project_id FROM main.channel").fetchall())


def test_f141_preview_matches_real_add_and_remove_step_by_step(db):
    """Цепочка добавлений и удалений правил разной формы и приоритета: перед каждым шагом предпросмотр
    (`mapping.preview`) обязан назвать РОВНО те каналы (и их новые проекты), которые затем сменит настоящая
    запись (`add_rule`/`remove_rule`) — включая топики за форумом и не трогая ручные."""
    account_id = _insert_account(db, source="telegram")
    p1, p2, p3 = (_insert_project(db, name=n) for n in ("p1", "p2", "p3"))
    _upsert_channel(db, account_id, ext_id="a", attrs={"tg_folders": [1]})
    _upsert_channel(db, account_id, ext_id="b", attrs={"tg_folders": [1, 2]})
    _upsert_channel(db, account_id, ext_id="c", attrs={"tg_folders": [2]})
    _upsert_channel(db, account_id, ext_id="d")
    _upsert_channel(db, account_id, ext_id="forum", kind="group", attrs={"tg_folders": [2]})
    _upsert_channel(db, account_id, ext_id="forum:1", kind="topic", parent_ext_id="forum")
    _upsert_channel(db, account_id, ext_id="forum:2", kind="topic", parent_ext_id="forum")
    _upsert_channel(db, account_id, ext_id="m", attrs={"tg_folders": [1]})
    manual_id, topic2_id = _channel(db, "m")[0], _channel(db, "forum:2")[0]
    db.write_sync(lambda conn: channels.update_channel(conn, manual_id, project_id=p3, now_ms=1))
    db.write_sync(lambda conn: channels.update_channel(conn, topic2_id, project_id=p3, now_ms=1))

    steps = [
        ("add", {"attr": "tg_folders", "has": 1}, p1, None),
        ("add", {"attr": "tg_folders", "has": 2}, p2, None),  # равный приоритет: b остаётся у p1
        ("add", {"all": True}, p3, None),
        ("add", {"attr": "tg_folders", "has": 2}, p3, 5),  # выше — перебивает b, c, форум и его авто-топик
        ("add", {"channel_ext_id": "a"}, p2, 0),
        ("remove", 4),
        ("remove", 1),
        ("remove", 3),
    ]
    total_moved = 0
    for step in steps:
        before = _projects_by_channel(db)
        if step[0] == "add":
            _kind, match, project_id, priority = step
            preview = db.write_sync(
                lambda conn, m=match, p=project_id, pr=priority: mapping.preview(
                    conn, account_id, add=(m, p, pr)
                )
            )
            db.write_sync(
                lambda conn, m=match, p=project_id, pr=priority: mapping.add_rule(
                    conn, account_id=account_id, project_id=p, match=m, priority=pr, now_ms=2
                )
            )
        else:
            rule_id = step[1]
            preview = db.write_sync(lambda conn, r=rule_id: mapping.preview(conn, account_id, remove=r))
            db.write_sync(lambda conn, r=rule_id: mapping.remove_rule(conn, r, 3))
        after = _projects_by_channel(db)
        changed = {cid: after[cid] for cid in after if after[cid] != before[cid]}
        assert preview["moved"] == len(changed), step
        assert {e["channel_id"]: e["to_project_id"] for e in preview["examples"]} == changed, step
        assert manual_id not in changed and topic2_id not in changed
        total_moved += len(changed)
    # каналы реально двигались на каждом шаге цепочки — иначе проверка пустая
    assert total_moved >= len(steps)
