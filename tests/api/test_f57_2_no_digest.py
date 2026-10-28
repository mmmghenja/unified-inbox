"""Ф57-2 (docs/architecture.md): режима «сводка» (`digest`) больше нет.

- `attention.MODES` без `digest`; значение `digest` в данных читается как отсутствующее;
- миграция 0087 (ручной режим беседы `digest` -> `info`, оба файла) и 0088 (политика проектов: `digest`
  -> `info`, ключ `digest_at` удалён) — по образцу `test_migrations_0085_and_0086`;
- API и CLI: `digest` — отказ, `--digest-at`/`--no-digest` нет;
- старая строка `agent_run` с видом `digest` читается: `test_home.py`, `test_t1_old_digest_kind...`."""

import json

import pytest

from hub import cli as hub_cli
from hub.core import attention
from hub.core import db as db_module
from hub.core.db import Db

POLICIES = {
    "full": '{"attention": {"mail_thread": "digest", "default": "digest", "account:4": "digest", '
    '"dm": "react", "group": "info"}, "digest_at": "10:00", "other": [1, {"a": "digest"}]}',
    "digest_at_only": '{"attention": {"dm": "react"}}',
    "with_at": '{"attention": {"dm": "react", "digest_at": "09:30"}}',
    "clean": '{"attention": {"group": "info", "default": "react"}, "x": 1}',
    "broken": "{битый",
    "attention_string": '{"attention": "digest"}',
    "not_object": "[1, 2]",
    "empty": "",
    "odd_values": '{"attention": {"group": null, "topic": 5, "card": "digest", "issue": {"a": "digest"}, '
    '"task": [1, "digest"], "dm": true, "channel": "a\\"b"}}',
}


@pytest.fixture
def stepped(monkeypatch):
    """`upto(N)` — миграции только до версии N включительно."""
    original = db_module._migrations

    def upto(version: int) -> None:
        monkeypatch.setattr(
            db_module,
            "_migrations",
            lambda *, main: {v: f for v, f in original(main=main).items() if v <= version},
        )

    return upto


def test_modes_have_no_digest():
    assert attention.MODES == ("react", "info")
    # значение, оставшееся в данных, читается как отсутствующее
    policy = '{"attention": {"group": "digest", "default": "digest"}}'
    assert attention.effective_mode(0, 0, "digest", 1, "group", None, policy) == "info"
    assert attention.effective_mode(0, 0, "digest", 1, "mail_thread", None, policy) == "react"
    with pytest.raises(ValueError, match="режим"):
        attention.normalize_policy_changes({"group": "digest"})


def test_migrations_0087_and_0088(tmp_path, stepped):
    stepped(86)
    old = Db(tmp_path)
    old.start()
    try:

        def fill(conn):
            ids = {}
            for name, policy in POLICIES.items():
                ids[name] = conn.execute(
                    "INSERT INTO main.project (name, agent_policy_json) VALUES (?, ?)", (name, policy)
                ).lastrowid
            ids["null"] = conn.execute("INSERT INTO main.project (name) VALUES ('null')").lastrowid
            for schema, source in (("main", "telegram"), ("work", "slack")):
                acc = conn.execute(
                    "INSERT INTO main.account (source, db) VALUES (?, ?)", (source, schema)
                ).lastrowid
                for ext, mode in (("d", "digest"), ("r", "react"), ("i", "info"), ("a", None)):
                    conn.execute(
                        f"INSERT INTO {schema}.channel (account_id, ext_id, kind, attention) "  # noqa: S608
                        "VALUES (?, ?, 'group', ?)",
                        (acc, ext, mode),
                    )
            return ids

        ids = old.write_sync(fill)
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 86
    finally:
        old.close()

    stepped(88)
    for _ in range(2):  # повторный запуск — без ошибок и без изменений
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 88
                assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 87
                for schema in ("main", "work"):
                    modes = dict(
                        conn.execute(f"SELECT ext_id, attention FROM {schema}.channel").fetchall()  # noqa: S608
                    )
                    assert modes == {"d": "info", "r": "react", "i": "info", "a": None}, schema
                got = dict(conn.execute("SELECT id, agent_policy_json FROM main.project").fetchall())
            # ключи вне `attention` целы (в т.ч. `digest` внутри чужого массива); значения-строки остались
            # строками; `digest_at` удалён из `attention`, а верхнего уровня не тронут
            assert json.loads(got[ids["full"]]) == {
                "attention": {
                    "mail_thread": "info",
                    "default": "info",
                    "account:4": "info",
                    "dm": "react",
                    "group": "info",
                },
                "digest_at": "10:00",
                "other": [1, {"a": "digest"}],
            }
            assert json.loads(got[ids["with_at"]]) == {"attention": {"dm": "react"}}
            assert json.loads(got[ids["odd_values"]]) == {
                "attention": {
                    "group": None,
                    "topic": 5,
                    "card": "info",
                    "issue": {"a": "digest"},
                    "task": [1, "digest"],
                    "dm": True,
                    "channel": 'a"b',
                }
            }
            # не тронуты байт в байт: без `digest`, битый JSON, `attention` строкой, не объект, пусто, NULL
            for name in ("digest_at_only", "clean", "broken", "attention_string", "not_object", "empty"):
                assert got[ids[name]] == POLICIES[name], name
            assert got[ids["null"]] is None
            # смигрированная политика читается расчётом режима
            assert attention.effective_mode(0, 0, None, 1, "mail_thread", None, got[ids["full"]]) == "info"
        finally:
            new.close()


def test_cli_has_no_digest_options(capsys):
    with pytest.raises(SystemExit):
        hub_cli.main(["project", "set", "1", "--digest-at", "10:00"])
    with pytest.raises(SystemExit):
        hub_cli.main(["channel", "attention", "5", "digest"])
    capsys.readouterr()
