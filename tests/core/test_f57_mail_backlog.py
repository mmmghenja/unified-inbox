"""Ревью Ф57-1. Шаг 1а миграции 0085 (раздел 10 спеки) закрывает вал старых писем только у бесед с РУЧНЫМ
`triage` и у почты без проекта. Почта проекта, которая была в «разобрать» по ПОЛИТИКЕ проекта
(`mail_thread=triage` — так стоит у Globex с 08.10, `default=triage`, `account:<id>=triage`), после 0086
становится `react`, а её старые непрочитанные письма рассылкой не помечены -> разом в «Нужна реакция»."""

import pytest

from hub.core import attention
from hub.core import db as db_module
from hub.core.db import Db


@pytest.fixture
def stepped(monkeypatch):
    original = db_module._migrations

    def upto(version: int) -> None:
        monkeypatch.setattr(
            db_module,
            "_migrations",
            lambda *, main: {v: f for v, f in original(main=main).items() if v <= version},
        )

    return upto


def _seq(conn, schema):
    return conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {schema}.message").fetchone()[0]


POLICY_CASES = {
    "by_kind": '{"attention": {"mail_thread": "triage"}}',  # Globex (проект 1) на проде
    "by_default": '{"attention": {"default": "triage"}}',
    "by_account": None,  # заполняется после создания аккаунта
}


def _counts(conn):
    out = {}
    for schema in ("main", "work"):
        got = attention.unread_react_counts(conn, schema, where="1", params=[])
        names = dict(conn.execute(f"SELECT id, ext_id FROM {schema}.channel").fetchall())
        for cid, n in got.items():
            out[(schema, names[cid])] = n
    return out


def test_old_mail_of_policy_triage_projects_does_not_flood_unread_react(tmp_path, stepped):
    stepped(84)
    old = Db(tmp_path)
    old.start()
    try:

        def fill(conn):
            for schema, source in (("main", "mail"), ("work", "mail")):
                acc = conn.execute(
                    "INSERT INTO main.account (source, db) VALUES (?, ?)", (source, schema)
                ).lastrowid
                cases = dict(POLICY_CASES)
                cases["by_account"] = f'{{"attention": {{"account:{acc}": "triage"}}}}'
                rows = [("unsorted", None)]
                for name, policy in cases.items():
                    pid = conn.execute(
                        "INSERT INTO main.project (name, agent_policy_json) VALUES (?, ?)",
                        (f"{schema}-{name}", policy),
                    ).lastrowid
                    rows.append((name, pid))
                for ext, pid in rows:
                    chan = conn.execute(
                        f"INSERT INTO {schema}.channel (account_id, ext_id, kind, project_id) "
                        "VALUES (?, ?, 'mail_thread', ?)",
                        (acc, ext, pid),
                    ).lastrowid
                    for n in range(3):  # три старых непрочитанных чужих письма
                        seq = _seq(conn, schema)
                        conn.execute(
                            f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "
                            "is_me, kind, text, ts) VALUES (?, ?, ?, ?, ?, 0, 'text', 'x', ?)",
                            (seq, seq, acc, chan, f"{ext}-{n}", 1000 + n),
                        )

        old.write_sync(fill)
    finally:
        old.close()
    # до миграции (код 1c378ea): режим всех четырёх бесед — `triage`, внимание писем не `react`,
    # `unread_react` = 0 (новым кодом на схеме 84 не посчитать: нет столбца `bulk`).

    # Ф63-1: до конца (не до 86) — счётчик нового кода читает `message.rule_attention` (миграция 0094);
    # миграции после 0086 старые письма не трогают.
    stepped(10**6)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            after = _counts(conn)
    finally:
        new.close()
    print("unread_react после миграции:", after)
    # цель шага 1а (раздел 10): старые письма бывших «разобрать» НЕ встают разом в «Нужна реакция»
    assert after == {}, after
