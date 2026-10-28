"""Ф10-5, находки ревью части A (перенесены из `.runs/critic-f10-5/`): тексты комментариев Trello не режутся
маркерами подвала; `\\Seen` Gmail не двигает маркеры бесед трекера; пересортировка берёт тред, в который уже
пришло новое письмо."""

from __future__ import annotations

import json

from hub.connectors.mail import connector as connector_mod
from hub.connectors.mail import trackers
from hub.connectors.mail.connector import MailConnector

from .conftest import make_connector, make_gateway
from .fakegmail import BASE_TS
from .helpers import channels, core_cursor, init_cursor, messages
from .test_trackers import fixture
from .test_trackers_connector import JIRA_CH, jira, trello

CARD_X = "trello:c:Xy1Ab2Cd"
CARD_Y = "trello:c:Qw3Er4Ty"


def test_trello_comment_line_starting_with_footer_phrase_truncates_comment():
    """Строка комментария «Чтобы настроить …» режет тело карточки (A3 такого конца тела не знает)."""
    text = fixture("trello_digest_two_cards.txt").replace(
        "Пользователь вводит адрес — это не меняем.",
        "Чтобы настроить вход, откройте админку.\nВАЖНО: пароль не менять.",
    )
    notes = trackers.parse_trello(text)
    card2 = next(n for n in notes if n.key == "Qw3Er4Ty")
    assert "Чтобы настроить вход" in card2.body
    assert "ВАЖНО: пароль не менять." in card2.body


async def test_newer_seen_digest_marks_older_unread_digest_read(admin_client, internal_transport, gm, db):
    """Карточка X приходит в двух дайджестах (два треда Gmail). Старый не прочитан, новый прочитан ->
    маркер X уезжает за непрочитанное письмо старого дайджеста."""
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    try:
        await init_cursor(connector)
        old = trello(gm, date_ts=BASE_TS + 60)  # не прочитан
        new = trello(gm, date_ts=BASE_TS + 600, flags=("\\Seen",))
        assert old.thrid != new.thrid
        await connector.sync("live")
        assert channels(db)[CARD_X]["last_read"] is None, "письмо старого дайджеста не прочитано"
    finally:
        await gw.close()


async def test_push_read_one_card_marks_other_card_of_digest_read(admin_client, internal_transport, gm, db):
    """Владелец прочёл в хабе карточку X -> `\\Seen` всему письму-дайджесту -> следующая сверка ставит
    прочитанной карточку Y, которую он не открывал."""
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    try:
        await init_cursor(connector)
        d = trello(gm, date_ts=BASE_TS + 60)
        await connector.sync("live")
        assert channels(db)[CARD_Y]["last_read"] is None
        item = {
            "channel_id": 7,
            "gen": 1,
            "lease_gen": 1,
            "channel": {"ext_id": CARD_X, "kind": "card", "parent_ext_id": None},
            "message": {"ext_id": d.message_id, "ts": d.date_ts * 1000, "ext_sort": 0, "thread_ext_id": None},
        }
        assert (await connector.push_read(item)).outcome == "done"
        await connector.sync("scheduled")
        assert channels(db)[CARD_Y]["last_read"] is None, "карточку Y владелец не читал"
    finally:
        await gw.close()


async def test_resort_skips_old_thread_if_new_letter_came_first(
    admin_client, internal_transport, gm, db, monkeypatch
):
    """Старая беседа-тред Jira; до первого прохода пересортировки в тред пришло новое письмо (догон той же
    сверки кладёт тред в `_tracker_threads`) -> пересортировка тред пропускает: старые письма не
    перенесены, беседа-тред не закрыта."""
    connector, gw, client, account_id = await make_connector(admin_client, internal_transport, gm)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(connector_mod.trackers, "route", lambda *_a, **_k: None)
            await init_cursor(connector)
            a1 = jira(gm, date_ts=BASE_TS + 60, flags=("\\Seen",))
            await connector.sync("live")
        assert str(a1.thrid) in channels(db)
        cur = await core_cursor(client)
        cur.pop("trackers")
        await client.push_events([], cursor={"value": json.dumps(cur, sort_keys=True), "ts": 0})

        # во время деплоя пришло новое письмо в тот же тред
        a2 = jira(gm, thrid=a1.thrid, date_ts=BASE_TS + 120, replace=("Иван Петров", "Пётр Сидоров"))
        gw2 = make_gateway(gm)
        try:
            c2 = MailConnector(
                client, gw2, account_id=account_id, config={"address": "me@gmail.com"},
                wall=lambda: BASE_TS + 300,
            )
            await c2.sync("scheduled")
            assert (await core_cursor(client))["trackers"] == {"done": 1}
            in_issue = {r["ext_id"] for r in messages(db) if r["thread"] == JIRA_CH}
            assert a2.message_id in in_issue
            assert a1.message_id in in_issue, "старое письмо Jira не перенесено в беседу задачи"
        finally:
            await gw2.close()
    finally:
        await gw.close()
