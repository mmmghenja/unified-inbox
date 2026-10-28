"""Находка: B3 выбирает цель группы по наименьшему id КОНТАКТА. Контакты главного файла всегда младше рабочих
(id work > 2^40), поэтому новый почтовый контакт в main с той же почтой, что у уже названного владельцем
человека из
Slack (work), «перетягивает» Slack-контакт к себе: имя, заданное владельцем, стирается (person_name = цель = NULL),
старый человек удаляется уборкой B4 вместе с его парами «Нет, разные» — ручные решения владельца теряются молча."""

from hub.core import people

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
    slack_people,
)


def test_new_mail_contact_steals_named_slack_person(db):
    tg = account(db, "telegram")
    sl = account(db, "slack", schema="work")
    box = account(db, "mail", config={"address": "me@home.ru"})
    s = contact(db, sl, "U1", "Иван", "@ivan", schema="work")
    z = contact(db, tg, "200", "Иван Другой", "@ivan_other")
    slack_people(db, sl, [{"id": "U1", "real_name": "Иван", "email": "ivan@x.ru"}])
    msg(db, channel(db, sl, "D1", schema="work"), ts=NOW_MS - 1000, sender=s)
    msg(db, channel(db, tg, "200"), ts=NOW_MS - 1000, sender=z)
    link(db)
    a = person_of(db, s)
    zp = person_of(db, z)
    db.write_sync(lambda conn: people.rename(conn, a, "Иван — клиент"))
    db.write_sync(lambda conn: people.mark_not_same(conn, a, zp, NOW_MS))
    assert contact_row(db, s)["person_name"] == "Иван — клиент"

    # Позже приходит письмо с того же адреса (главный файл, контакт младше по id).
    m = contact(db, box, "ivan@x.ru", "Ivan", "ivan@x.ru")
    msg(db, channel(db, box, "t1", kind="mail_thread"), ts=NOW_MS - 500, sender=m)
    link(db, NOW_MS + 1)

    # Ожидаемо: письмо пришло к уже существующему человеку, имя и «разные» на месте.
    assert person_of(db, m) == person_of(db, s)
    assert contact_row(db, s)["person_name"] == "Иван — клиент", "имя, заданное владельцем, стёрто"
    assert person_of(db, s) == a, "Slack-контакт уехал к новому человеку, старый удалён B4"
    assert tuple(sorted((a, zp))) in not_same(db), "пара «Нет, разные» потеряна"
