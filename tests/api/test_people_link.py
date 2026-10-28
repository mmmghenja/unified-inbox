"""Ф21-1 (docs/architecture.md): миграции 0035/0036 и проход связывания `people.link_all` — разделы
A, B и поправки П3, П5, П8, П9 (обязательные тесты I.1–I.5, I.22, I.23, I.26, I.27)."""

import pytest

from hub.core import db as db_module
from hub.core import people
from hub.core.db import Db

from .people_helpers import (
    NOW_MS,
    account,
    channel,
    contact,
    contact_row,
    link,
    msg,
    not_same,
    person_of,
    persons,
    slack_people,
)


@pytest.fixture
def accs(db):
    return {
        "tg": account(db, "telegram"),
        "mail": account(db, "mail", config={"address": "me@home.ru", "aliases": ["alias@home.ru"]}),
        "slack": account(db, "slack", schema="work"),
        "wmail": account(db, "mail", schema="work", config={"address": "me@corp.io"}),
    }


# --- I.1: миграции -----------------------------------------------------------------------------------------


def test_i1_migrations_0035_0036_on_previous_version(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 34}
    )
    old = Db(tmp_path)
    old.start()
    try:
        acc = account(old, "telegram")
        cid = contact(old, acc, "100", "Иван")
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 34
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 33
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 36
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] >= 36
            main_tables = {
                r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table'")
            }
            work_tables = {
                r[0] for r in conn.execute("SELECT name FROM work.sqlite_master WHERE type='table'")
            }
            assert {"person", "person_not_same", "contact_stats"} <= main_tables
            assert "contact_stats" in work_tables and "person" not in work_tables
            for schema in ("main", "work"):
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(contact)")}
                assert {"person_id", "person_manual", "email", "person_name"} <= cols
                indexes = {
                    r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='index'")
                }
                assert {"contact_person", "contact_email", "message_sender_ts"} <= indexes
            row = conn.execute(
                "SELECT person_id, person_manual, email, person_name FROM main.contact WHERE id = ?", (cid,)
            ).fetchone()
            assert row == (None, 0, None, None)
    finally:
        new.close()


# --- I.2: B1 -----------------------------------------------------------------------------------------------


def test_i2_b1_people_and_not_people(db, accs):
    ivan = contact(db, accs["tg"], "100", "Иван", "@ivan")
    me = contact(db, accs["tg"], "1", "Я", is_me=1)
    bot = contact(db, accs["tg"], "200", "Бот без ника")
    chat = contact(db, accs["tg"], "-1001", "Канал")
    ch = channel(db, accs["tg"], "200")
    msg(db, ch, ts=NOW_MS - 1000, sender=bot, is_bot=1)
    link(db)
    assert person_of(db, ivan) is not None
    assert person_of(db, me) is None
    assert person_of(db, bot) is None
    assert person_of(db, chat) is None
    # Повтор — ничего не меняется.
    before = persons(db)
    assert link(db) == {"created": 0, "cleared": 0, "moved": 0, "removed": 0}
    assert persons(db) == before


# --- I.3: B2 -----------------------------------------------------------------------------------------------


def test_i3_b2_email(db, accs):
    mail = contact(db, accs["mail"], "Petr@Client.RU", "Пётр", "Petr@Client.RU")
    jira = contact(db, accs["mail"], "jira:ivan", "Иван Jira")
    tg = contact(db, accs["tg"], "100", "Иван", "@ivan")
    slack = contact(db, accs["slack"], "U0AAA", "Аня", "@anya", schema="work")
    slack_gone = contact(db, accs["slack"], "U0BBB", "Боря", "@borya", schema="work")
    slack_people(
        db, accs["slack"], [{"id": "U0AAA", "email": "Anya@Corp.io"}, {"id": "U0BBB", "email": "b@corp.io"}]
    )
    link(db)
    assert contact_row(db, mail)["email"] == "petr@client.ru"
    assert contact_row(db, jira)["email"] is None
    assert contact_row(db, tg)["email"] is None
    assert contact_row(db, slack)["email"] == "anya@corp.io"
    assert contact_row(db, slack_gone)["email"] == "b@corp.io"
    # Пропал из справочника — почта не стирается.
    slack_people(db, accs["slack"], [{"id": "U0AAA", "email": "anya@corp.io"}])
    link(db)
    assert contact_row(db, slack_gone)["email"] == "b@corp.io"


# --- I.4: B3 -----------------------------------------------------------------------------------------------


def test_i4_b3_merge_by_email_and_idempotent(db, accs):
    mail = contact(db, accs["mail"], "anya@corp.io", "Анна", "anya@corp.io")
    slack = contact(db, accs["slack"], "U0AAA", "Аня", "@anya", schema="work")
    wmail = contact(db, accs["wmail"], "anya@corp.io", "Anna", "anya@corp.io", schema="work")
    slack_people(db, accs["slack"], [{"id": "U0AAA", "email": "anya@corp.io"}])
    link(db)
    target = person_of(db, mail)  # контакт с наименьшим id — в главном файле
    assert person_of(db, slack) == target and person_of(db, wmail) == target
    assert persons(db) == {target}
    snapshot = {c: contact_row(db, c) for c in (mail, slack, wmail)}
    assert link(db) == {"created": 0, "cleared": 0, "moved": 0, "removed": 0}
    assert {c: contact_row(db, c) for c in (mail, slack, wmail)} == snapshot


def test_i4_b3_manual_contact_does_not_move(db, accs):
    mail = contact(db, accs["mail"], "anya@corp.io", "Анна")
    slack = contact(db, accs["slack"], "U0AAA", "Аня", schema="work")
    tg = contact(db, accs["tg"], "100", "Аня")
    link(db)
    # Владелец объединил Slack с Telegram — человек целиком ручной; затем Slack получил почту письма.
    db.write_sync(lambda conn: people.merge(conn, person_of(db, tg), [person_of(db, slack)]))
    manual_person = person_of(db, tg)
    slack_people(db, accs["slack"], [{"id": "U0AAA", "email": "anya@corp.io"}])
    link(db)
    # Цель группы — человек ручного контакта: переезжает почтовый (не ручной), ручной стоит.
    assert person_of(db, slack) == manual_person
    assert person_of(db, mail) == manual_person
    assert contact_row(db, mail)["person_manual"] == 0


def test_i4_b3_manual_in_different_people_untouched(db, accs):
    mail = contact(db, accs["mail"], "anya@corp.io", "Анна")
    slack = contact(db, accs["slack"], "U0AAA", "Аня", schema="work")
    link(db)
    pm, ps = person_of(db, mail), person_of(db, slack)
    # Владелец руками держит почту и Slack разными людьми (разъединение делает обе стороны ручными).
    db.write_sync(lambda conn: people.merge(conn, pm, [ps]))
    db.write_sync(lambda conn: people.split(conn, [slack], NOW_MS))
    ps2 = person_of(db, slack)
    assert ps2 != pm
    # Потом пришло письмо с того же адреса на рабочий ящик — новый, не ручной контакт со своим человеком.
    wmail = contact(db, accs["wmail"], "anya@corp.io", "Anna", schema="work")
    slack_people(db, accs["slack"], [{"id": "U0AAA", "email": "anya@corp.io"}])
    link(db)
    pw = person_of(db, wmail)
    assert pw not in (pm, ps2) and contact_row(db, wmail)["person_manual"] == 0
    assert person_of(db, mail) == pm and person_of(db, slack) == ps2
    assert person_of(db, wmail) == pw  # группа с ручными в разных людях не трогается вовсе


def test_i4_b3_not_same_respected(db, accs):
    mail = contact(db, accs["mail"], "anya@corp.io", "Анна")
    wmail = contact(db, accs["wmail"], "anya@corp.io", "Anna", schema="work")
    # Люди заведены раньше (по отдельности), владелец сказал «разные» — склейка по почте их не трогает.
    first = db.write_sync(lambda conn: people._new_person(conn, NOW_MS))
    second = db.write_sync(lambda conn: people._new_person(conn, NOW_MS))
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.contact SET person_id = ? WHERE id = ?", (first, mail))
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE work.contact SET person_id = ? WHERE id = ?", (second, wmail))
    )
    db.write_sync(lambda conn: people.mark_not_same(conn, first, second, NOW_MS))
    link(db)
    assert person_of(db, mail) == first and person_of(db, wmail) == second


# --- I.5: B4 -----------------------------------------------------------------------------------------------


def test_i5_b4_cleanup_after_work_contacts_gone(db, accs):
    main_a = contact(db, accs["tg"], "100", "Иван")
    main_b = contact(db, accs["tg"], "101", "Пётр")
    work_a = contact(db, accs["slack"], "U0AAA", "Аня", schema="work")
    work_b = contact(db, accs["slack"], "U0BBB", "Боря", schema="work")
    link(db)
    pa, pb, wa, wb = (person_of(db, c) for c in (main_a, main_b, work_a, work_b))
    db.write_sync(lambda conn: people.mark_not_same(conn, pa, wa, NOW_MS))
    db.write_sync(lambda conn: people.mark_not_same(conn, wa, wb, NOW_MS))
    db.write_sync(lambda conn: people.mark_not_same(conn, pa, pb, NOW_MS))
    db.write_sync(lambda conn: conn.execute("DELETE FROM work.contact"))
    link(db)
    assert persons(db) == {pa, pb}
    assert not_same(db) == {(min(pa, pb), max(pa, pb))}


# --- I.22: П3 старые боты ----------------------------------------------------------------------------------


def test_i22_p3_old_bots_lose_person(db, accs):
    slack_bot = contact(db, accs["slack"], "B0BOT", "Бот", schema="work")
    slackbot = contact(db, accs["slack"], "USLACKBOT", "Slackbot", schema="work")
    tg_bot = contact(db, accs["tg"], "300", "Помощник", "@HelperBot")
    service = contact(db, accs["tg"], "777000", "Telegram")
    later_bot = contact(db, accs["tg"], "301", "Сначала человек")
    people_ids = (slack_bot, slackbot, tg_bot, service, later_bot)
    # Как будто раньше все получили людей (до П3), один — ручной и с именем.
    for cid in people_ids:
        pid = db.write_sync(lambda conn: people._new_person(conn, NOW_MS))
        schema = "work" if cid >= 2**40 else "main"
        db.write_sync(
            lambda conn, cid=cid, pid=pid, schema=schema: conn.execute(
                f"UPDATE {schema}.contact SET person_id = ?, person_manual = 1, person_name = 'X' "
                "WHERE id = ?",
                (pid, cid),
            )
        )
    ch = channel(db, accs["tg"], "301")
    msg(db, ch, ts=NOW_MS, sender=later_bot, is_bot=1)
    link(db)
    for cid in people_ids:
        row = contact_row(db, cid)
        assert (row["person_id"], row["person_manual"], row["person_name"]) == (None, 0, None), cid
    assert persons(db) == set()  # опустевшие люди убраны


# --- I.23: П5 имя в файле контакта -------------------------------------------------------------------------


def test_i23_p5_manual_name_lives_in_contact_file(db, accs):
    work = contact(db, accs["slack"], "U0AAA", "Аня", schema="work")
    work2 = contact(db, accs["wmail"], "a@corp.io", "Анна", schema="work")
    main = contact(db, accs["tg"], "100", "Иван")
    link(db)
    pw, pw2, pm = person_of(db, work), person_of(db, work2), person_of(db, main)
    db.write_sync(lambda conn: people.rename(conn, pw, "  Анна Корп  "))
    assert contact_row(db, work)["person_name"] == "Анна Корп"
    with db.reader() as conn:
        assert [r[0] for r in conn.execute("SELECT * FROM main.person WHERE id = ?", (pw,))] == [pw]
        cols = [r[1] for r in conn.execute("PRAGMA main.table_info(person)")]
        assert cols == ["id", "created_ts"]
    # Объединение переносит имя на все контакты итогового человека.
    db.write_sync(lambda conn: people.merge(conn, pw2, [pw]))
    assert contact_row(db, work2)["person_name"] == "Анна Корп"
    assert contact_row(db, work)["person_name"] == "Анна Корп"
    db.write_sync(lambda conn: conn.execute("DELETE FROM work.contact"))
    link(db)
    assert persons(db) == {pm}
    with db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM main.contact WHERE person_name IS NOT NULL").fetchone()[0] == 0
        )


def test_i23_rename_validation_and_reset(db, accs):
    cid = contact(db, accs["tg"], "100", "Иван")
    link(db)
    pid = person_of(db, cid)
    with pytest.raises(people.Invalid):
        db.write_sync(lambda conn: people.rename(conn, pid, "   "))
    with pytest.raises(people.Invalid):
        db.write_sync(lambda conn: people.rename(conn, pid, "x" * 101))
    db.write_sync(lambda conn: people.rename(conn, pid, "Иван Иванович"))
    db.write_sync(lambda conn: people.rename(conn, pid, None))
    assert contact_row(db, cid)["person_name"] is None


# --- B5 на уровне ядра: объединить, разъединить, «разные» ----------------------------------------------


def test_b5_merge_moves_not_same_and_removes_pair(db, accs):
    a = contact(db, accs["tg"], "100", "Иван")
    b = contact(db, accs["slack"], "U0AAA", "Ivan", schema="work")
    c = contact(db, accs["tg"], "101", "Пётр")
    link(db)
    pa, pb, pc = (person_of(db, x) for x in (a, b, c))
    db.write_sync(lambda conn: people.mark_not_same(conn, pa, pb, NOW_MS))
    db.write_sync(lambda conn: people.mark_not_same(conn, pb, pc, NOW_MS))
    db.write_sync(lambda conn: people.merge(conn, pa, [pb]))
    assert person_of(db, b) == pa
    assert contact_row(db, a)["person_manual"] == 1 and contact_row(db, b)["person_manual"] == 1
    assert pb not in persons(db)
    assert not_same(db) == {(min(pa, pc), max(pa, pc))}


def test_b5_split_rules(db, accs):
    a = contact(db, accs["tg"], "100", "Иван")
    b = contact(db, accs["slack"], "U0AAA", "Ivan", schema="work")
    c = contact(db, accs["tg"], "101", "Пётр")
    link(db)
    pa, pb, pc = (person_of(db, x) for x in (a, b, c))
    db.write_sync(lambda conn: people.merge(conn, pa, [pb]))
    with pytest.raises(people.Invalid):
        db.write_sync(lambda conn: people.split(conn, [a, b], NOW_MS))  # все контакты
    with pytest.raises(people.Invalid):
        db.write_sync(lambda conn: people.split(conn, [a, c], NOW_MS))  # разные люди
    new = db.write_sync(lambda conn: people.split(conn, [b], NOW_MS))
    assert person_of(db, b) == new and person_of(db, a) == pa
    assert contact_row(db, a)["person_manual"] == 1 and contact_row(db, b)["person_manual"] == 1
    assert (min(pa, new), max(pa, new)) in not_same(db)
    assert pc in persons(db)


# --- I.26: П8 свои адреса ----------------------------------------------------------------------------------


def test_i26_p8_own_addresses_are_not_people(db, accs):
    home_alias = contact(db, accs["wmail"], "alias@home.ru", "Я из дома", schema="work")
    corp = contact(db, accs["mail"], "ME@corp.io", "Я с работы")
    me_mail = contact(db, accs["mail"], "me@home.ru", "Я", is_me=1)
    other_box = account(db, "mail", config={"address": "third@box.ru"})
    contact(db, other_box, "third@box.ru", "Я третий", is_me=1)
    third_seen = contact(db, accs["mail"], "third@box.ru", "Я третий снаружи")
    stranger = contact(db, accs["mail"], "petr@client.ru", "Пётр")
    link(db)
    for cid in (home_alias, corp, me_mail, third_seen):
        assert person_of(db, cid) is None, cid
    assert person_of(db, stranger) is not None


# --- I.27: П9 висящий person_id и последовательность на старте --------------------------------------------


def test_i27_p9_dangling_person_id_gets_new_person(db, accs):
    a = contact(db, accs["slack"], "U0AAA", "Аня", schema="work")
    b = contact(db, accs["slack"], "U0BBB", "Боря", schema="work")
    c = contact(db, accs["tg"], "100", "Иван")
    # work закоммитился со ссылкой на человека 500, main — нет.
    db.write_sync(
        lambda conn: conn.execute("UPDATE work.contact SET person_id = 500 WHERE id IN (?, ?)", (a, b))
    )
    link(db)
    pa, pb, pc = person_of(db, a), person_of(db, b), person_of(db, c)
    assert pa == pb and pa != 500 and pa in persons(db)
    assert pc not in (500, pa)  # новый человек главного контакта не совпал с висящей ссылкой


def test_i27_p9_sequence_raised_on_start(tmp_path):
    first = Db(tmp_path)
    first.start()
    try:
        acc = account(first, "slack", schema="work")
        cid = contact(first, acc, "U0AAA", "Аня", schema="work")
        first.write_sync(
            lambda conn: conn.execute("UPDATE work.contact SET person_id = 777 WHERE id = ?", (cid,))
        )
    finally:
        first.close()
    second = Db(tmp_path)
    second.start()
    try:
        with second.reader() as conn:
            assert (
                conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name = 'person'").fetchone()[0]
                == 777
            )
        new = second.write_sync(lambda conn: people._new_person(conn, NOW_MS))
        assert new == 778
    finally:
        second.close()
