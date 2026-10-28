"""Ф21-1 (docs/architecture.md): статистика переписки `people_stats` — раздел D и поправки П1, П2,
П7, П8, П10 (обязательные тесты I.7–I.12, I.20, I.21, I.25, I.26, I.28)."""

import statistics
import threading

import pytest

from hub.core import people, people_stats
from hub.core.security import Principal

from .people_helpers import (
    DAY_MS,
    MIN_MS,
    NOW_MS,
    account,
    channel,
    contact,
    link,
    msg,
    outbox_row,
    person_of,
    recompute,
    stats_of,
)

UI = Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
BASE = NOW_MS - 60 * DAY_MS


@pytest.fixture
def tg(db):
    return account(db, "telegram")


# --- I.7: D3 личка ---------------------------------------------------------------------------------------


def test_i7_d3_dm_batches_median_unanswered(db, tg):
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    t = BASE
    msg(db, dm, ts=t, sender=p)  # пачка P: два подряд
    msg(db, dm, ts=t + 1 * MIN_MS, sender=p)
    msg(db, dm, ts=t + 10 * MIN_MS, me=True)  # владелец ответил за 10 мин от начала пачки; пачка владельца «P»
    msg(db, dm, ts=t + 20 * MIN_MS, sender=p)  # P ответил владельцу за 10 мин
    late = t + 20 * MIN_MS + 8 * DAY_MS
    msg(db, dm, ts=late, me=True)  # ответ владельца позже 7 суток — «без ответа»
    msg(db, dm, ts=late + 3 * MIN_MS, sender=p)  # P ответил за 3 мин
    msg(db, dm, ts=late + 9 * MIN_MS, me=True)  # владелец ответил за 6 мин
    msg(db, dm, ts=late + 10 * MIN_MS, me=True)  # та же пачка владельца
    msg(
        db, dm, ts=NOW_MS - DAY_MS, sender=p
    )  # P ответил позже 7 суток; его молодое без ответа — не считается
    link(db)
    recompute(db)
    stats = stats_of(db, p)
    assert sorted(stats["reply_me"]["samples"]) == [6 * MIN_MS, 10 * MIN_MS]
    assert stats["reply_me"]["unanswered"] == 1
    assert sorted(stats["reply_them"]["samples"]) == [3 * MIN_MS, 10 * MIN_MS]
    assert stats["reply_them"]["unanswered"] == 1
    assert stats["in"]["total"] == 5 and stats["in"]["personal"] == 5 and stats["in"]["d30"] == 1
    assert stats["out"]["total"] == 4
    assert stats["row"]["known"] == 1
    summary = people_stats.summary(people_stats.merge([stats]))
    assert summary["reply_me"] == {"median_ms": 8 * MIN_MS, "p75_ms": 9 * MIN_MS, "n": 2, "unanswered": 1}


def test_i7_old_unanswered_counts_young_does_not(db, tg):
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    msg(db, dm, ts=NOW_MS - 10 * DAY_MS, sender=p)
    q = contact(db, tg, "101", "Кира")
    dm2 = channel(db, tg, "101")
    msg(db, dm2, ts=NOW_MS - 2 * DAY_MS, sender=q)
    link(db)
    recompute(db)
    assert stats_of(db, p)["reply_me"] == {"samples": [], "unanswered": 1}
    assert stats_of(db, q)["reply_me"] == {"samples": [], "unanswered": 0}


def test_window_is_one_year(db, tg):
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    msg(db, dm, ts=NOW_MS - 400 * DAY_MS, sender=p)
    link(db)
    recompute(db)
    assert stats_of(db, p) is None


# --- I.8: D1 письма ----------------------------------------------------------------------------------------


def test_i8_d1_mail(db):
    box = account(db, "mail", config={"address": "me@home.ru"})
    p = contact(db, box, "p@x.ru", "Пётр", "p@x.ru")
    q = contact(db, box, "q@x.ru", "Рассылка", "q@x.ru")
    a = channel(db, box, "1", kind="mail_thread")
    msg(db, a, ts=BASE, sender=p)
    msg(db, a, ts=BASE + 5 * MIN_MS, me=True, mail_to=["p@x.ru"])
    b = channel(db, box, "2", kind="mail_thread")
    msg(db, b, ts=BASE, sender=q)  # рассылка без ответа
    c = channel(db, box, "3", kind="mail_thread")
    msg(db, c, ts=BASE, sender=p)
    msg(db, c, ts=BASE + 7 * MIN_MS, me=True)  # «Кому» нет — единственный чужой отправитель
    d = channel(db, box, "4", kind="mail_thread")
    msg(db, d, ts=BASE, sender=p)
    msg(db, d, ts=BASE + MIN_MS, sender=q)
    msg(db, d, ts=BASE + 2 * MIN_MS, me=True)  # «Кому» нет, чужих двое — никому
    e = channel(db, box, "5", kind="mail_thread")
    msg(db, e, ts=BASE, sender=q)
    msg(db, e, ts=BASE + MIN_MS, me=True, mail_to=["p@x.ru"])  # «Кому» важнее единственного чужого
    link(db)
    recompute(db)
    sp, sq = stats_of(db, p), stats_of(db, q)
    assert sp["out"]["total"] == 3
    assert sp["in"]["total"] == 3 and sp["in"]["personal"] == 0
    # ответ владельца — первое его письмо в линии после пачки, адресовано оно P или нет (тред 4 — 2 мин)
    assert sorted(sp["reply_me"]["samples"]) == [2 * MIN_MS, 5 * MIN_MS, 7 * MIN_MS]
    assert sq["out"]["total"] == 0 and sq["in"]["total"] == 3 and sq["in"]["personal"] == 0
    assert sq["row"]["known"] == 0  # письма без ответа знакомым не делают
    assert sp["row"]["known"] == 1  # ответ владельца — делает


# --- I.9: D1 тред Slack ------------------------------------------------------------------------------------


def test_i9_d1_slack_thread(db):
    sl = account(db, "slack", schema="work")
    p = contact(db, sl, "U0P", "Пётр", schema="work")
    q = contact(db, sl, "U0Q", "Кира", schema="work")
    ch = channel(db, sl, "C1", kind="channel", schema="work")
    msg(db, ch, ts=BASE, me=True, ext_id="1000.1", thread="1000.1")  # родитель — владельца
    msg(db, ch, ts=BASE + MIN_MS, sender=p, ext_id="1000.2", thread="1000.1")
    msg(db, ch, ts=BASE + 2 * MIN_MS, sender=p, ext_id="1000.3", thread="1000.1")
    msg(db, ch, ts=BASE + 4 * MIN_MS, me=True, ext_id="1000.4", thread="1000.1")  # -> P
    msg(db, ch, ts=BASE + 10 * MIN_MS, sender=q, ext_id="2000.1", thread="2000.1")  # родитель — Киры
    msg(db, ch, ts=BASE + 11 * MIN_MS, sender=p, ext_id="2000.2", thread="2000.1")  # не владельцу
    msg(db, ch, ts=BASE + 12 * MIN_MS, me=True, ext_id="2000.3", thread="2000.1")  # -> P (предыдущий чужой)
    msg(
        db, ch, ts=BASE + 13 * MIN_MS, sender=p, ext_id="2000.4", thread="2000.1"
    )  # владелец писал, предыдущее его
    msg(db, ch, ts=BASE + 14 * MIN_MS, sender=q, ext_id="2000.5", thread="2000.1")  # предыдущее — не владельца
    msg(db, ch, ts=BASE + 20 * MIN_MS, sender=p, ext_id="3000.1", mentions_me=1, mentions_broadcast=1)
    msg(db, ch, ts=BASE + 21 * MIN_MS, sender=p, ext_id="3000.2", mentions_me=1)
    msg(db, ch, ts=BASE + 22 * MIN_MS, sender=q, ext_id="3000.3")  # просто в канал
    link(db)
    recompute(db)
    sp, sq = stats_of(db, p), stats_of(db, q)
    assert sp["in"]["total"] == 4  # два ответа в треде владельца, ответ после владельца, личное упоминание
    assert sp["in"]["personal"] == 4
    assert sp["out"]["total"] == 2
    assert sq["in"]["total"] == 0 and sq["out"]["total"] == 0
    assert sq["seen"]["total"] == 3 and sq["row"]["known"] == 0
    assert sp["reply_me"]["samples"] == [3 * MIN_MS]  # пачка P в треде 1000.1 -> ответ владельца
    assert sorted(sp["reply_them"]["samples"]) == [MIN_MS]  # владелец «P» в 2000.1 -> P через минуту


def test_i9_reply_to_in_group(db, tg):
    p = contact(db, tg, "100", "Пётр")
    q = contact(db, tg, "101", "Кира")
    grp = channel(db, tg, "-100500", kind="group")
    msg(db, grp, ts=BASE, me=True, ext_id="1")
    msg(db, grp, ts=BASE + MIN_MS, sender=p, ext_id="2", reply_to="1")  # ответ на владельца — владельцу
    msg(db, grp, ts=BASE + 2 * MIN_MS, sender=q, ext_id="3")
    msg(db, grp, ts=BASE + 3 * MIN_MS, me=True, ext_id="4", reply_to="3")  # владелец ответил Кире
    link(db)
    recompute(db)
    assert stats_of(db, p)["in"]["total"] == 1 and stats_of(db, p)["out"]["total"] == 0
    assert stats_of(db, q)["out"]["total"] == 1 and stats_of(db, q)["in"]["total"] == 0


# --- I.10: D5 как владелец пишет ------------------------------------------------------------------------------


def test_i10_d5_style(db, tg):
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    box = account(db, "mail", config={"address": "me@home.ru"})
    pm = contact(db, box, "p@x.ru", "Пётр", "p@x.ru")
    thread = channel(db, box, "1", kind="mail_thread")
    msg(db, dm, ts=BASE, sender=p)
    t = BASE + MIN_MS
    texts = ["Привет, как ты?", "Ты где? 👍", "Hello there :smile:", "Вы получили?"]
    ids = [msg(db, dm, ts=t + i * MIN_MS, me=True, text=x) for i, x in enumerate(texts)]
    api = outbox_row(db, dm, origin="api")
    msg(db, dm, ts=t + 10 * MIN_MS, me=True, text="Черновик сессии без правки", outbox_id=api)
    edited = outbox_row(db, dm, origin="api", draft_text="было")
    ok_edited = msg(db, dm, ts=t + 11 * MIN_MS, me=True, text="Правленый тобой черновик", outbox_id=edited)
    ui = outbox_row(db, dm, origin="ui")
    msg(db, dm, ts=t + 12 * MIN_MS, me=True, text="Из хаба", outbox_id=ui)
    msg(db, dm, ts=t + 13 * MIN_MS, me=True, text="Удалённое ты", deleted=1)
    msg(db, dm, ts=t + 14 * MIN_MS, me=True, text="Скрытое ты", hidden=1)
    msg(db, dm, ts=t + 15 * MIN_MS, me=True, text="Очищенное ты", purged=1)
    msg(db, thread, ts=BASE, sender=pm)
    msg(db, thread, ts=BASE + MIN_MS, me=True, text="Добрый день, Вы получили документы?", mail_to=["p@x.ru"])
    link(db)
    recompute(db)
    chat = stats_of(db, p)["style"]["chat"]
    assert chat["n"] == 6
    assert (chat["ty"], chat["vy"], chat["emoji"], chat["greet"]) == (3, 1, 2, 2)
    assert (chat["cyr"], chat["lat"]) == (5, 1)
    assert stats_of(db, p)["style"]["mail"]["n"] == 0
    assert chat["examples"] == [ids[2], ids[3], ok_edited]  # три последних от 10 знаков
    mail = stats_of(db, pm)["style"]["mail"]
    assert mail["n"] == 1 and mail["vy"] == 1 and mail["greet"] == 1
    summary = people_stats.summary(people_stats.merge([stats_of(db, p)]))
    assert summary["style"]["chat"]["address"] == "ты"
    assert summary["style"]["chat"]["lang"] == "ru"  # 5 из 6 — 0,83
    lens = [len(x) for x in [*texts, "Правленый тобой черновик", "Из хаба"]]
    assert summary["style"]["chat"]["median_len"] == round(statistics.median(lens))
    assert summary["style"]["chat"]["emoji_share"] == round(2 / 6, 2)
    assert summary["style"]["mail"] is None
    mail_summary = people_stats.summary(people_stats.merge([stats_of(db, pm)]))
    assert mail_summary["style"]["mail"]["address"] == "вы" and mail_summary["style"]["chat"] is None


def test_i10_text_features_and_rules():
    f = people_stats.text_features("  Доброе утро, вам и вашим! 🙂 ")
    assert f["greet"] and f["vy"] and not f["ty"] and f["emoji"] and f["cyr"] and f["len"] == 27
    assert not people_stats.text_features("Приветствую").get("greet")
    assert people_stats.text_features("hey :tada:")["emoji"]
    assert not people_stats.text_features("Тывы")["ty"]
    assert people_stats._address(2, 1) == "ты"
    assert people_stats._address(1, 2) == "вы"
    assert people_stats._address(1, 1) == "смешанно"
    assert people_stats._address(0, 0) is None
    assert people_stats._lang(8, 2) == "ru" and people_stats._lang(2, 8) == "en"
    assert people_stats._lang(5, 5) == "mixed" and people_stats._lang(0, 0) is None


# --- I.11, I.25: D6 реакции --------------------------------------------------------------------------------


def test_i11_i25_reactions(db, tg):
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    grp = channel(db, tg, "-100500", kind="group")
    msg(db, grp, ts=BASE, sender=p, reactions=[{"emoji": "👍", "count": 2, "me": True}])
    msg(db, dm, ts=BASE + 1, sender=p, reactions=[{"emoji": "❤️", "count": 1, "me": True}])
    msg(db, grp, ts=BASE + 2, sender=p, reactions=[{"emoji": "❤", "count": 1, "me": True}])
    by_session = msg(db, grp, ts=BASE + 3, sender=p, reactions=[{"emoji": "❤️", "count": 1, "me": True}])
    msg(db, grp, ts=BASE + 4, sender=p, reactions=[{"custom": "555", "count": 1, "me": True}])
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.reaction_by_session (message_id, emoji_key, token_id, ts) "
            "VALUES (?, '❤', 1, 0)",
            (by_session,),
        )
    )
    msg(db, dm, ts=BASE + 5, me=True, reactions=[{"emoji": "😂", "count": 1, "me": False}])
    msg(db, dm, ts=BASE + 6, me=True, reactions=[{"emoji": "😂", "count": 0, "me": False}])
    msg(db, grp, ts=BASE + 7, me=True, reply_to="nope", reactions=[{"emoji": "🔥", "count": 1, "me": False}])
    link(db)
    recompute(db)
    react = stats_of(db, p)["react"]
    assert react["me_to_them"] == {"👍": 1, "❤️": 2}  # ❤️ и ❤ — одна; реакция сессии отфильтрована
    assert react["them_to_me"] == {"😂": 1}  # только личка


def test_f23_reactions_with_by_fields_counted_as_before(db, tg):
    """Ф23-2: новые `by`/`by_more` в `reactions_json` D6 не меняют (считает по `me` и `count`)."""
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    grp = channel(db, tg, "-100500", kind="group")
    mine = {"emoji": "👍", "count": 3, "me": True, "by": ["Анна"], "by_more": 1}
    msg(db, grp, ts=BASE, sender=p, reactions=[mine])
    msg(db, dm, ts=BASE + 1, sender=p)
    theirs = {"emoji": "😂", "count": 1, "me": False, "by": ["Пётр"]}
    msg(db, dm, ts=BASE + 5, me=True, reactions=[theirs])
    link(db)
    recompute(db)
    react = stats_of(db, p)["react"]
    assert react["me_to_them"] == {"👍": 1}
    assert react["them_to_me"] == {"😂": 1}


def test_i11_mail_reaction_from_person(db):
    box = account(db, "mail", config={"address": "me@home.ru"})
    p = contact(db, box, "p@x.ru", "Пётр", "p@x.ru")
    thread = channel(db, box, "1", kind="mail_thread")
    msg(db, thread, ts=BASE, sender=p, ext_id="<a@x>")
    msg(db, thread, ts=BASE + MIN_MS, me=True, ext_id="<b@home>", mail_to=["p@x.ru"])
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.mail_reaction (channel_id, target_ext_id, src_ext_id, emoji, sender_key, "
            "is_me, ts) "
            "VALUES (?, '<b@home>', '<r1@x>', '🙏', 'P@x.ru', 0, 1), (?, '<b@home>', '<r2@x>', '🙏', "
            "'me@home.ru', 1, 2)",
            (thread, thread),
        )
    )
    link(db)
    recompute(db)
    assert stats_of(db, p)["react"]["them_to_me"] == {"🙏": 1}


# --- I.12: D8 слияние двух файлов ---------------------------------------------------------------------


def test_i12_d8_merge_two_files(db, tg):
    sl = account(db, "slack", schema="work")
    p_tg = contact(db, tg, "100", "Пётр")
    p_sl = contact(db, sl, "U0P", "Пётр С", schema="work")
    dm_tg = channel(db, tg, "100")
    dm_sl = channel(db, sl, "D1", schema="work")
    for i, delay in enumerate((1, 2, 3)):
        msg(db, dm_tg, ts=BASE + i * DAY_MS, sender=p_tg)
        msg(db, dm_tg, ts=BASE + i * DAY_MS + delay * MIN_MS, me=True)
    for i, delay in enumerate((10, 20)):
        msg(db, dm_sl, ts=BASE + i * DAY_MS, sender=p_sl)
        msg(db, dm_sl, ts=BASE + i * DAY_MS + delay * MIN_MS, me=True)
    link(db)
    db.write_sync(lambda conn: people.merge(conn, person_of(db, p_tg), [person_of(db, p_sl)]))
    recompute(db)
    with db.reader() as conn:
        card = people.card(conn, UI, person_of(db, p_tg))
    stats = card["stats"]
    assert stats["in"]["total"] == 5 and stats["out"]["total"] == 5
    assert stats["in"]["first_ts"] == BASE and stats["out"]["last_ts"] == BASE + 2 * DAY_MS + 3 * MIN_MS
    # медиана — по объединённой выборке [1, 2, 3, 10, 20] минут
    assert stats["reply_me"]["median_ms"] == 3 * MIN_MS and stats["reply_me"]["n"] == 5
    assert {c["channel_id"] for c in card["channels"]} == {dm_tg, dm_sl}
    assert card["name"] == "Пётр"  # больше всего переписки (3 + 3 против 2 + 2)


# --- I.20: П1 заготовки outbox -----------------------------------------------------------------------------


def test_i20_p1_unsent_is_not_owner(db, tg):
    p = contact(db, tg, "100", "Пётр")
    q = contact(db, tg, "101", "Кира")
    dm = channel(db, tg, "100")
    msg(db, dm, ts=BASE, sender=p)
    for status in ("pending_approval", "queued", "failed"):
        row = outbox_row(db, dm, status=status)
        msg(db, dm, ts=BASE + MIN_MS, me=True, stub=True, outbox_id=row)
    grp = channel(db, tg, "-100500", kind="group")
    msg(db, grp, ts=BASE, sender=q, ext_id="7")
    row = outbox_row(db, grp, status="pending_approval")
    msg(db, grp, ts=BASE + MIN_MS, me=True, stub=True, reply_to="7", outbox_id=row)
    link(db)
    recompute(db)
    sp, sq = stats_of(db, p), stats_of(db, q)
    assert sp["out"]["total"] == 0 and sp["reply_me"]["samples"] == []
    assert sq["out"]["total"] == 0 and sq["row"]["known"] == 0
    assert sp["style"]["chat"]["n"] == 0


# --- I.21: П2 групповая личка Slack ------------------------------------------------------------------------


def test_i21_p2_mpim(db):
    sl = account(db, "slack", schema="work")
    p = contact(db, sl, "U0P", "Пётр", schema="work")
    q = contact(db, sl, "U0Q", "Кира", schema="work")
    mp = channel(db, sl, "G1", kind="group", schema="work", meta={"attrs": {"slack_type": "mpim"}})
    msg(db, mp, ts=BASE, sender=p, reactions=[{"emoji": "👍", "count": 1, "me": True}])
    msg(db, mp, ts=BASE + MIN_MS, sender=q)
    msg(
        db, mp, ts=BASE + 2 * MIN_MS, me=True, reactions=[{"emoji": "🔥", "count": 1, "me": False}]
    )  # -> Кира
    msg(db, mp, ts=BASE + 3 * MIN_MS, sender=p)
    msg(db, mp, ts=BASE + 4 * MIN_MS, me=True)  # -> Пётр
    link(db)
    recompute(db)
    sp, sq = stats_of(db, p), stats_of(db, q)
    assert sp["in"]["personal"] == 2 and sq["in"]["personal"] == 1
    assert sp["out"]["total"] == 1 and sq["out"]["total"] == 1
    assert sq["reply_them"]["samples"] == []  # Кира больше не писала
    assert sorted(sp["reply_me"]["samples"]) == [MIN_MS, 2 * MIN_MS]
    assert sq["reply_me"]["samples"] == [MIN_MS]
    assert sq["reply_them"]["unanswered"] == 1
    assert sq["react"]["them_to_me"] == {}  # реакции P -> владельцу — только из `kind = 'dm'`


# --- I.26: П8 письма между своими ящиками ------------------------------------------------------------------


def test_i26_p8_mail_between_own_boxes(db):
    home = account(db, "mail", config={"address": "me@home.ru"})
    corp = account(db, "mail", schema="work", config={"address": "me@corp.io"})
    me_seen = contact(db, corp, "me@home.ru", "Я", "me@home.ru", schema="work")
    thread = channel(db, corp, "1", kind="mail_thread", schema="work")
    msg(db, thread, ts=BASE, sender=me_seen)
    msg(db, thread, ts=BASE + MIN_MS, me=True, mail_to=["me@home.ru"])
    assert home
    link(db)
    recompute(db)
    assert person_of(db, me_seen) is None
    assert stats_of(db, me_seen) is None
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work.contact_stats").fetchone()[0] == 0


# --- I.28: П10 остановка посреди пересчёта -------------------------------------------------------------


class _StopAfter(threading.Event):
    """Флаг остановки, поднимающийся после N проверок (между беседами)."""

    def __init__(self, checks: int) -> None:
        super().__init__()
        self.left = checks

    def is_set(self) -> bool:
        self.left -= 1
        return self.left < 0 or super().is_set()


def test_i28_p10_stop_midway_writes_nothing(db, tg):
    p = contact(db, tg, "100", "Пётр")
    for i in range(3):
        dm = channel(db, tg, f"10{i}")
        msg(db, dm, ts=BASE, sender=p)
    link(db)
    recompute(db)
    assert stats_of(db, p)["seen"]["total"] == 3
    q = contact(db, tg, "200", "Кира")
    dm = channel(db, tg, "200")
    msg(db, dm, ts=BASE, sender=q)
    link(db)
    assert people_stats.recompute(db, NOW_MS + 1, _StopAfter(2)) is None
    assert stats_of(db, q) is None  # старые строки не тронуты, новых нет
    assert stats_of(db, p)["seen"]["total"] == 3
