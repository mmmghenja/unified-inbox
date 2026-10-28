"""Ф10-5 часть A — разбор уведомлений трекеров без сети (`trackers.py`, `convert.tracker_event`): тесты
A-T1…A-T8 спеки `docs/architecture.md`. Образцы — текстовые части писем Atlassian
(`fixtures/trackers/*.txt`), завёрнутые в `multipart/alternative` с `From`/`Subject` из таблицы спеки."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.connectors.mail import convert, textproc, trackers
from hub.connectors.mail.imapwire import FetchedMsg

from .fakegmail import BASE_TS, make_email

FIX = Path(__file__).parent / "fixtures" / "trackers"
ME = convert.MeInfo(address="me@gmail.com")

JIRA_ASSIGNEE = (
    '"Automation for Jira (Jira)" <jira@acme.atlassian.net>',
    "[Jira] (WEB-39) «Утилиты на Tailwind»",
)
JIRA_SPRINT = (
    '"www.example.com (Jira)" <jira@acme.atlassian.net>',
    "[Jira] (APP-27) «Заменить баннер на главном экране»",
)
TRELLO_DIGEST = (
    "Trello <do-not-reply@trello.com>",
    "Новых оповещений в Тест — команда с 18:57 (25 сентября 2026 г.): 3",
)

CARD1_TEXT = """упомянул вас в карточке
@user10000001
По поводу первой части заявки.
Макеты согласованы.
а) принято
б) принято

переместил карточку в список Готово — на разборе"""

CARD2_TEXT = """прокомментировал карточку
1. Про название раздела в отчёте — писать п. 5–6.
Пользователь вводит адрес — это не меняем.

--------------------------------------------------------------------------------
Ниже — старый вариант, для сравнения.
Ответить по электронной почте — так раньше подписывали письма."""


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def letter(text: str, from_: str, subject: str, *, msgid: str = "<n1@mail.example>") -> FetchedMsg:
    raw = make_email(
        msgid=msgid,
        from_=from_,
        subject=subject,
        body=text,
        html="<html><body><p>HTML-версия уведомления</p></body></html>",
    )
    return FetchedMsg(
        uid=1,
        msgid=5001,
        thrid=9001,
        labels=["\\Inbox"],
        flags=[],
        internaldate_ms=BASE_TS * 1000,
        body=raw,
        header=raw,
    )


def route(msg: FetchedMsg) -> list[trackers.TrackerPart] | None:
    return trackers.route(msg, thread_msgs=[msg], known=set())


def only(parts):
    assert parts is not None and len(parts) == 1
    return parts[0]


# --- A-T1 / A-T2: Jira «обновление» ----------------------------------------------------------------------


def test_a_t1_jira_update_assignee():
    """A-T1: ключ, заголовок, `url`, автор, текст — ровно по A2; `mentions_me = false`; нет `http`."""
    msg = letter(fixture("jira_update_assignee.txt"), *JIRA_ASSIGNEE)
    part = only(route(msg))
    assert part.channel == {
        "ext_id": "jira:acme.atlassian.net:WEB-39",
        "kind": "issue",
        "own_id_space": True,
        "title": "WEB-39 · Утилиты на Tailwind",
        "attrs": {
            "labels": ["INBOX"],
            "from_domains": ["acme.atlassian.net"],
            "tracker": "jira",
            "key": "WEB-39",
            "url": "https://acme.atlassian.net/browse/WEB-39",
            "no_reply": True,
        },
    }
    event = convert.tracker_event(msg, part, me=ME)
    assert event["type"] == "upsert"
    assert event["ext_id"] == "<n1@mail.example>"
    assert event["text"] == "Исполнитель: Иван Петров → admin"
    assert "http" not in event["text"]
    assert event["mentions_me"] is False
    assert event["is_me"] is False
    assert event["sender"] == {
        "ext_id": "jira:automation for jira",
        "name": "Automation for Jira",
        "is_me": False,
    }
    assert event["refs"] == ["WEB-39"]
    assert "reply_to_ext_id" not in event


def test_a_t1_title_from_text_when_subject_has_no_title():
    msg = letter(fixture("jira_update_assignee.txt"), JIRA_ASSIGNEE[0], "[Jira] обновление")
    part = only(route(msg))
    assert part.channel["title"] == "WEB-39 · Утилиты на Tailwind"


@pytest.mark.parametrize(
    ("before", "after"),
    [(" ", " "), (" ", " "), (" ", " "), ("​ ", " "), ("", " ")],
)
def test_a_t1_jira_update_with_unicode_spaces_around_colon(before, after):
    """Живые письма Jira (прод 25.09): вокруг двоеточия поля — неразрывные/узкие пробелы, невидимые символы
    или пробела перед двоеточием нет — разбор «обновления» всё равно даёт строку «Поле: было → стало»."""
    text = fixture("jira_update_assignee.txt").replace(
        "Исполнитель : Иван Петров", f"Исполнитель{before}:{after}Иван Петров"
    )
    msg = letter(text, *JIRA_ASSIGNEE)
    event = convert.tracker_event(msg, only(route(msg)), me=ME)
    assert event["text"] == "Исполнитель: Иван Петров → admin"


def test_a_t2_jira_update_sprint_two_changes():
    """A-T2: две строки изменений ровно по A2 (перенос значения, пояс «Лондон» на своей строке)."""
    msg = letter(fixture("jira_update_sprint.txt"), *JIRA_SPRINT)
    part = only(route(msg))
    assert part.channel["ext_id"] == "jira:acme.atlassian.net:APP-27"
    assert part.channel["title"] == "APP-27 · Заменить баннер на главном экране"
    event = convert.tracker_event(msg, part, me=ME)
    assert event["text"] == (
        "Sprint: ACME_Sprint 2, ACME_Sprint 3 → ACME_Sprint 2, ACME_Sprint 3, ACME_Sprint 4\n"
        "Статус: READY FOR TEST → TEST"
    )
    assert event["sender"]["name"] == "www.example.com"
    assert event["mentions_me"] is False


# --- A-T3: Jira неизвестного шаблона — общая чистка ---------------------------------------------------------


def _jira_comment_text() -> str:
    footer = fixture("jira_update_assignee.txt")
    footer = footer[footer.index("Просмотреть задачу") :]
    return (
        "Логотип Jira\n"
        "[https://post-office-rml-frontend.prod-east.frontend.public.atl-paas.net/assets/jiraHeader.315f59f2.png]\n"
        "\n"
        "Анна ТестоваПользователь добавил комментарий\n"
        "\n"
        "AcmeWeb\n"
        "[https://acme.atlassian.net/browse/WEB?atlOrigin=eyJpIjoiYjA5In0] / WEB-39\n"
        "[https://acme.atlassian.net/browse/WEB-39?atlOrigin=eyJpIjoiYjA5In0]\n"
        "\n"
        "Утилиты на Tailwind\n"
        "[https://acme.atlassian.net/browse/WEB-39?atlOrigin=eyJpIjoiYjA5In0]\n"
        "\n"
        "Avatar\n"
        "[https://secure.gravatar.com/avatar/abc?d=https%3A%2F%2Fx.example%2FAT-1.png]  Анна Тестова  10:05\n"
        "\n"
        "@Иван Петров посмотри, пожалуйста, отступы на мобильном.\n"
        "Ссылка на комментарий:\n"
        "[https://acme.atlassian.net/browse/WEB-39?focusedCommentId=10042&atlOrigin=eyJpIjoiYjA5In0]\n"
        "[https://track.atlassian.com/tracking/c18ca1cb?m=eyJhbGciOiJIUzI1NiJ9.x.y&j=1]\n"
        "\n" + footer
    )


def test_a_t3_jira_unknown_template_generic_cleanup():
    """A-T3: комментарий -> общая чистка: нет картинок, `atlOrigin`, слежения, подвала; текст на месте;
    `mentions_me = true`; автор — из `From` без « (Jira)»."""
    msg = letter(_jira_comment_text(), '"Анна Тестова (Jira)" <jira@acme.atlassian.net>', JIRA_ASSIGNEE[1])
    part = only(route(msg))
    text = part.text
    assert "@Иван Петров посмотри, пожалуйста, отступы на мобильном." in text
    assert "https://acme.atlassian.net/browse/WEB-39?focusedCommentId=10042" in text
    for junk in (".png", "atlOrigin", "track.atlassian.com", "gravatar", "Логотип", "Avatar"):
        assert junk not in text, junk
    for footer in ("Просмотреть задачу", "Подсказка", "App Store", "Google Play", "Политика", "отпишитесь"):
        assert footer not in text, footer
    assert part.mentions_me is True
    assert part.actor == "Анна Тестова"
    event = convert.tracker_event(msg, part, me=ME)
    assert event["sender"]["ext_id"] == "jira:анна тестова"


# --- A-T4 / A-T5: дайджест Trello --------------------------------------------------------------------------


def test_a_t4_trello_digest_two_cards():
    """A-T4: ровно два события с одним `ext_id` (Message-ID), беседы, заголовки, отправители, тексты,
    `mentions_me` — ровно по A3."""
    msg = letter(fixture("trello_digest_two_cards.txt"), *TRELLO_DIGEST)
    parts = route(msg)
    assert parts is not None and len(parts) == 2
    events = [convert.tracker_event(msg, p, me=ME) for p in parts]
    assert [e["ext_id"] for e in events] == ["<n1@mail.example>"] * 2

    first, second = events
    assert first["channel"]["ext_id"] == "trello:c:Xy1Ab2Cd"
    assert first["channel"]["kind"] == "card"
    assert first["channel"]["title"] == "Каталог Acme — позиции для проверки (до 29.09)"
    assert first["channel"]["attrs"]["board"] == "Тест — команда"
    assert first["channel"]["attrs"]["url"] == "https://trello.com/c/Xy1Ab2Cd"
    assert first["channel"]["attrs"]["no_reply"] is True
    assert first["channel"]["attrs"]["tracker"] == "trello"
    assert first["channel"]["attrs"]["key"] == "Xy1Ab2Cd"
    assert first["sender"] == {"ext_id": "trello:анна тестова", "name": "Анна Тестова", "is_me": False}
    assert first["mentions_me"] is True
    assert first["text"] == CARD1_TEXT
    assert first["refs"][0] == "https://trello.com/c/Xy1Ab2Cd"

    assert second["channel"]["ext_id"] == "trello:c:Qw3Er4Ty"
    assert second["channel"]["title"] == "Проверить тексты после A-162. Резерв Tyrell, отчёт по складу"
    assert second["channel"]["attrs"]["url"] == "https://trello.com/c/Qw3Er4Ty"
    assert second["sender"]["name"] == "Борис Пробный"
    assert second["mentions_me"] is True
    assert second["text"] == CARD2_TEXT


def test_a_t5_false_starts_do_not_cut_the_body():
    """A-T5: «Пользователь вводит…» в комментарии — не начало уведомления; строка из 80 `-` и «Ответить по
    электронной почте — …» без ссылки `@boards.trello.com` тело не обрезают."""
    text = fixture("trello_digest_two_cards.txt")
    starts = trackers._trello_starts(text.split("\n"))  # noqa: SLF001
    assert [name for _n, _i, name in starts] == ["Анна Тестова", "Анна Тестова", "Борис Пробный"]
    notes = trackers.parse_trello(text)
    assert notes is not None
    body = notes[2].body
    assert "Пользователь вводит адрес — это не меняем." in body
    assert "-" * 80 in body
    assert body.endswith("Ответить по электронной почте — так раньше подписывали письма.")


# --- A-T6: подделка домена ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("acme.atlassian.net", "jira"),
        ("x.y.atlassian.net", "jira"),
        ("evil-atlassian.net", None),
        ("atlassian.net.evil.com", None),
        ("atlassian.net", None),
        ("trello.com", "trello"),
        ("mail.trello.com", "trello"),
        ("trel1o.com", None),
        ("evil-trello.com", None),
        ("trello.com.evil.org", None),
    ],
)
def test_a_t6_domain_label_boundary(domain, expected):
    assert trackers.tracker_of_domain(domain) == expected


@pytest.mark.parametrize(
    ("from_", "name", "subject"),
    [
        ('"Jira" <jira@evil-atlassian.net>', "jira_update_assignee.txt", JIRA_ASSIGNEE[1]),
        ('"Jira" <jira@atlassian.net.evil.com>', "jira_update_assignee.txt", JIRA_ASSIGNEE[1]),
        ("Trello <x@trel1o.com>", "trello_digest_two_cards.txt", TRELLO_DIGEST[1]),
    ],
)
def test_a_t6_spoofed_domain_is_ordinary_letter(from_, name, subject):
    """A-T6: тот же текст от поддельного домена -> обычное письмо (`route` -> `None`, прежний путь)."""
    msg = letter(fixture(name), from_, subject)
    assert route(msg) is None


# --- A-T7: не терять текст ----------------------------------------------------------------------------------

_TRELLO_NO_BOARD_WORDS = """Логотип Trello
[https://trello.com/images/logo-new-sm-2x.png]

Анна Тестова
[https://trello-members.s3.amazonaws.com/5a1b2c3e/0a1b2c3d/30.png]
Пользователь Анна Тестова добавила чеклист в карточку Проверка договора
[https://trello.com/c/AbCdEf12/5-%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D0%BA%D0%B0]
[https://trello.com/b/Zt5Yu6Io/%D1%82%D0%B5%D1%81%D1%82]
Пункт один готов.

Ответить по электронной почте
[user10000001+abc@boards.trello.com?subject=Re%3A]

Это письмо от Trello • Оставить отзыв
[https://track.atlassian.com/tracking/4b8c5468?m=x.y.z&j=1]
"""


def test_a_t7_unparsed_trello_header_goes_by_first_card_link():
    """A-T7: шапка без «на доске» -> беседа по первой ссылке карточки, текст — общая чистка, не пустой."""
    msg = letter(_TRELLO_NO_BOARD_WORDS, *TRELLO_DIGEST)
    part = only(route(msg))
    assert part.channel["ext_id"] == "trello:c:AbCdEf12"
    assert "title" not in part.channel
    assert "Пункт один готов." in part.text
    assert "https://trello.com/c/AbCdEf12" in part.text
    assert "s3.amazonaws.com" not in part.text and "boards.trello.com" not in part.text
    assert "track.atlassian.com" not in part.text
    assert part.actor == "Анна Тестова"


def test_a_t7_trello_board_only_notification():
    text = "Пользователь Анна Тестова закрыла доску\n[https://trello.com/b/Zt5Yu6Io/test]\n"
    part = only(route(letter(text, *TRELLO_DIGEST)))
    assert part.channel["ext_id"] == "trello:b:Zt5Yu6Io"
    assert part.channel["attrs"]["url"] == "https://trello.com/b/Zt5Yu6Io"


def test_a_t7_trello_without_card_or_board_link_is_ordinary():
    text = "Добро пожаловать в Trello!\n[https://trello.com/images/logo.png]\nhttps://trello.com/home\n"
    assert route(letter(text, *TRELLO_DIGEST)) is None


def test_a_t7_generic_cleanup_empty_keeps_original_text():
    """A-T7: общая чистка дала пусто -> исходный текст."""
    text = "Просмотреть задачу\n[https://acme.atlassian.net/browse/WEB-39?atlOrigin=eyJ]\n"
    msg = letter(text, *JIRA_ASSIGNEE)
    part = only(route(msg))
    assert part.channel["ext_id"] == "jira:acme.atlassian.net:WEB-39"
    assert part.text == textproc.extract_body(msg.body, strip_quote=False).text
    assert part.text.startswith("Просмотреть задачу")


def test_a_t7_clean_generic_empty_input():
    assert trackers.clean_generic("") == ""
    assert trackers.clean_generic("Avatar\n[https://secure.gravatar.com/a.png]") == (
        "Avatar\n[https://secure.gravatar.com/a.png]"
    )


# --- A-T8: цитата не режется -------------------------------------------------------------------------------


def test_a_t8_quote_header_inside_comment_is_kept():
    """A-T8: строка «пн, 23 сент. 2026 г. в 10:00, владелец <a@b>:» в комментарии — текст целиком, следующее
    уведомление дайджеста на месте (`strip_quote=False`)."""
    text = fixture("trello_digest_two_cards.txt").replace(
        "б) принято\n",
        "б) принято\nпн, 23 сент. 2026 г. в 10:00, владелец <a@b.example>:\n> прошлый вопрос про тексты\n",
    )
    msg = letter(text, *TRELLO_DIGEST)
    parts = route(msg)
    assert parts is not None
    assert [p.channel["ext_id"] for p in parts] == ["trello:c:Xy1Ab2Cd", "trello:c:Qw3Er4Ty"]
    first = parts[0].text
    assert "пн, 23 сент. 2026 г. в 10:00, владелец <a@b.example>:" in first
    assert "> прошлый вопрос про тексты" in first
    assert first.endswith("переместил карточку в список Готово — на разборе")
    assert parts[1].text == CARD2_TEXT
