"""Ф11-9 H2 (docs/architecture.md): перенос беседы (`channel_migrate`) закрывает открытые
карточки разборщика старой беседы (`new`/`seen` -> `done`) в той же транзакции; разовая уборка уже висящих —
миграция `0027_shared.sql`. Стиль — как `test_ingest_phase1.py`: `apply_batch` напрямую, без HTTP."""

import json

import pytest

from hub.core import db as db_module
from hub.core import inbound_rules, mail_reactions
from hub.core import join as join_mod
from hub.core.db import Db
from hub.core.ingest import apply_batch


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


def _insert_account(db, *, source: str, db_name: str = "main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _upsert(ext_id, ts, *, channel_ext, channel_kind):
    return {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": channel_kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": "hi",
        "mentions_me": False,
    }


def _migrate_event(old_ext_id, new_ext_id, *, kind):
    channel = {"ext_id": new_ext_id, "kind": kind}
    return {"type": "channel_migrate", "old_ext_id": old_ext_id, "channel": channel}


def _apply(db, account_id, events, now_ms=1000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _channel_id(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(f"SELECT id FROM {schema}.channel WHERE ext_id = ?", (ext_id,)).fetchone()[0]  # noqa: S608


def _message_ids(db, channel_id, schema="main"):
    with db.reader() as conn:
        return [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM {schema}.message WHERE channel_id = ? ORDER BY id",  # noqa: S608
                (channel_id,),
            ).fetchall()
        ]


def _card(db, channel_id, message_id, status, schema="main"):
    """Карточка напрямую строкой: статусы `seen`/`dismissed` рядом с открытой `new` одной беседы."""
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {schema}.agent_item (channel_id, label, summary, message_ids_json, "  # noqa: S608
                "last_message_id, covered_seq, status, provider, model, created_ts, updated_ts) "
                "VALUES (?, 'reply', 's', ?, ?, 0, ?, 'rules', '-', 1, 1)",
                (channel_id, json.dumps([message_id]), message_id, status),
            ).lastrowid
        )
    )


def _statuses(db, schema="main"):
    with db.reader() as conn:
        return dict(conn.execute(f"SELECT id, status FROM {schema}.agent_item").fetchall())  # noqa: S608


def _cards_of(db, channel_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {schema}.agent_item WHERE channel_id = ?",  # noqa: S608
            (channel_id,),
        ).fetchone()[0]


def test_h2_mail_thread_to_tracker_closes_new_and_seen_cards(db):
    account_id = _insert_account(db, source="mail")
    _apply(
        db,
        account_id,
        [
            _upsert("<a1@x>", 1000, channel_ext="thr", channel_kind="mail_thread"),
            _upsert("<a2@x>", 2000, channel_ext="thr", channel_kind="mail_thread"),
        ],
    )
    old_id = _channel_id(db, "thr")
    m1, m2 = _message_ids(db, old_id)
    seen = _card(db, old_id, m1, "seen")
    dismissed = _card(db, old_id, m1, "dismissed")
    new = _card(db, old_id, m2, "new")

    result = _apply(
        db,
        account_id,
        [
            _upsert("<a1@x>", 1000, channel_ext="jira:d:K-1", channel_kind="issue"),
            _migrate_event("thr", "jira:d:K-1", kind="issue"),
        ],
        now_ms=5000,
    )
    assert result.dead == 0
    assert _statuses(db) == {seen: "done", dismissed: "dismissed", new: "done"}
    assert _cards_of(db, _channel_id(db, "jira:d:K-1")) == 0

    # Повтор события — `noop` по `channel_alias`: ничего не меняет (новую карточку старой беседы не трогает).
    fresh = _card(db, old_id, m2, "new")
    again = _apply(db, account_id, [_migrate_event("thr", "jira:d:K-1", kind="issue")], now_ms=6000)
    assert (again.applied, again.noop) == (0, 1)
    assert _statuses(db)[fresh] == "new"


def test_h2_migrate_in_work_file_closes_card(db):
    account_id = _insert_account(db, source="slack", db_name="work")  # файл work: схема — по беседе
    _apply(db, account_id, [_upsert("1", 1000, channel_ext="-100", channel_kind="group")])
    old_id = _channel_id(db, "-100", "work")
    (m1,) = _message_ids(db, old_id, "work")
    card = _card(db, old_id, m1, "new", "work")

    _apply(db, account_id, [_migrate_event("-100", "-1009", kind="group")], now_ms=5000)
    assert _statuses(db, "work") == {card: "done"}


def test_h2_telegram_group_to_supergroup_closes_card(db):
    account_id = _insert_account(db, source="tg")
    _apply(db, account_id, [_upsert("1", 1000, channel_ext="-100", channel_kind="group")])
    old_id = _channel_id(db, "-100")
    (m1,) = _message_ids(db, old_id)
    card = _card(db, old_id, m1, "new")

    _apply(db, account_id, [_migrate_event("-100", "-1009", kind="group")], now_ms=5000)
    assert _statuses(db) == {card: "done"}
    assert _cards_of(db, _channel_id(db, "-1009")) == 0


# --- миграция 0027: разовая уборка ------------------------------------------------------------------------


def test_h2_migration_0027_closes_only_cards_of_migrated_archived_channels(tmp_path, monkeypatch):
    original = db_module._migrations
    # Нынешний приём на схеме до 0029 (Ф15-1): таблицы `mail_reaction` ещё нет — значки реакций писем в этом
    # тесте не при чём.
    monkeypatch.setattr(mail_reactions, "refresh_target", lambda *_args: 0)
    # Ф17-1: таблицы `join_request` (0030) на этой схеме тоже нет — привязка бесед по заявкам не при чём.
    monkeypatch.setattr(join_mod, "on_channel_created", lambda *_args: None)
    # Ф63-1 (0095): таблицы `inbound_rule` на этой схеме нет — правила входящих не при чём.
    monkeypatch.setattr(inbound_rules, "evaluate", lambda *_args, **_kw: inbound_rules.NO_OUTCOME)
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v < 27}
    )
    old = Db(tmp_path)
    old.start()
    # Ф20-1 (0033): нынешний приём пишет `message.is_bot` — на этой схеме колонку заводим вручную (вторая
    # база ниже мигрирует только до 0027, 0033 к ней не применяется). Ф28-2 (0042) — так же `mentions_group`.
    # Ф31-1 (0044): сигнал приёма читает `session_chan_sub.keywords_json` — тоже вручную.
    # Ф44-1a (0064): приём читает `message.buttons_json` / `buttons_ts` существующей строки — тоже вручную.
    # Ф52-2 (0076): сигнал приёма по беседе без проекта читает `project.unsorted_accounts` — тоже вручную.
    # Ф57-1 (0085): приём пишет `message.bulk` — тоже вручную.
    # Ф63-1 (0094): приём пишет `message.rule_hits` / `rule_attention` — тоже вручную.
    old.write_sync(
        lambda conn: [
            *(
                conn.execute(f"ALTER TABLE {s}.message ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                for s in ("main", "work")
                for col in ("is_bot", "mentions_group", "bulk")
            ),
            *(
                conn.execute(f"ALTER TABLE {s}.message ADD COLUMN {col}")
                for s in ("main", "work")
                for col in (
                    "buttons_json TEXT",
                    "buttons_ts INTEGER",
                    "rule_hits TEXT",
                    "rule_attention TEXT",
                )
            ),
            conn.execute("ALTER TABLE main.session_chan_sub ADD COLUMN keywords_json TEXT"),
            conn.execute("ALTER TABLE main.project ADD COLUMN unsorted_accounts TEXT"),
            # 0100 (Ф33-1): машина, загрузка и последний контакт сессии — читает приём (chan_subs).
            conn.execute("ALTER TABLE main.session_chan_sub ADD COLUMN session_host TEXT"),
            conn.execute("ALTER TABLE main.session_chan_sub ADD COLUMN session_boot TEXT"),
            conn.execute("ALTER TABLE main.session_chan_sub ADD COLUMN session_seen_ts INTEGER"),
            # 0105 (Ф74-2): движок сессии подписки — тоже читает приём.
            conn.execute("ALTER TABLE main.session_chan_sub ADD COLUMN session_engine TEXT"),
        ]
    )
    cards: dict[str, dict[str, int]] = {}
    try:
        for schema in ("main", "work"):
            account_id = _insert_account(old, source="mail", db_name=schema)
            _apply(
                old,
                account_id,
                [
                    _upsert("<a@x>", 1000, channel_ext="migrated", channel_kind="mail_thread"),
                    _upsert("<b@x>", 1000, channel_ext="archived", channel_kind="mail_thread"),
                    _upsert("<c@x>", 1000, channel_ext="live", channel_kind="mail_thread"),
                ],
            )
            ids = {ext: _channel_id(old, ext, schema) for ext in ("migrated", "archived", "live")}
            # Состояние до Ф11-9: беседа перенесена (архив + `migrated_to`), карточки висят.
            old.write_sync(
                lambda conn, schema=schema, ids=ids: (
                    conn.execute(
                        f"UPDATE {schema}.channel SET archived = 1, meta_json = ? WHERE id = ?",  # noqa: S608
                        (json.dumps({"migrated_to": "jira:d:K-1"}), ids["migrated"]),
                    ),
                    conn.execute(
                        f"UPDATE {schema}.channel SET archived = 1 WHERE id = ?",  # noqa: S608
                        (ids["archived"],),
                    ),
                )
            )
            msg = {ext: _message_ids(old, cid, schema)[0] for ext, cid in ids.items()}
            cards[schema] = {
                "migrated_new": _card(old, ids["migrated"], msg["migrated"], "new", schema),
                "migrated_seen": _card(old, ids["migrated"], msg["migrated"], "seen", schema),
                "migrated_dismissed": _card(old, ids["migrated"], msg["migrated"], "dismissed", schema),
                "archived_new": _card(old, ids["archived"], msg["archived"], "new", schema),
                "live_new": _card(old, ids["live"], msg["live"], "new", schema),
            }
        with old.reader() as conn:
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 26
    finally:
        old.close()

    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 27}
    )
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 27
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 27
        for schema in ("main", "work"):
            statuses = _statuses(new, schema)
            got = {name: statuses[item_id] for name, item_id in cards[schema].items()}
            assert got == {
                "migrated_new": "done",
                "migrated_seen": "done",
                "migrated_dismissed": "dismissed",
                "archived_new": "new",
                "live_new": "new",
            }
    finally:
        new.close()
