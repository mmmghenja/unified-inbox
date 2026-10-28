"""Ф11-9b (H3): письмо Jira «обновления + комментарий» — plain-часть содержит только текст комментария,
шапка, «Обновления» и «Комментарии» — в HTML-части (`docs/architecture.md`). Образцы —
обезличенные копии двух настоящих писем (`fixtures/trackers/*.eml`: структура MIME и разметка как в
оригинале, домен, люди, ключи и тексты — выдуманные)."""

from __future__ import annotations

import email
import re
from email import policy
from pathlib import Path

from hub.connectors.mail import convert, trackers
from hub.connectors.mail.imapwire import FetchedMsg
from hub.core import attention

from .fakegmail import BASE_TS, make_email

FIX = Path(__file__).parent / "fixtures" / "trackers"
ME = convert.MeInfo(address="me@gmail.com")
COMMENT_FROM = '"admin (Jira)" <jira@acme.atlassian.net>'
COMMENT_SUBJECT = "[Jira] (WEB-40) «Перевести формы на Tailwind»"


def _fetched(raw: bytes) -> FetchedMsg:
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


def _raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _parts(raw: bytes) -> tuple[str, str]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    markup = msg.get_body(preferencelist=("html",)).get_content()
    return plain, markup


def _letter(plain: str, markup: str) -> FetchedMsg:
    return _fetched(
        make_email(
            msgid="<n1@mail.example>", from_=COMMENT_FROM, subject=COMMENT_SUBJECT, body=plain, html=markup
        )
    )


def _only(msg: FetchedMsg) -> trackers.TrackerPart:
    parts = trackers.route(msg, thread_msgs=[msg], known=set())
    assert parts is not None and len(parts) == 1
    return parts[0]


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text)


COMMENT_PLAIN, COMMENT_HTML = _parts(_raw("jira_comment_update.eml"))
COMMENT_TEXT = (
    "Проверка на стенде: формы всё ещё на старых стилях.\n"
    "\n"
    "Что посмотрел:\n"
    "\n"
    "- кнопка «Сохранить»: отступы старые;\n"
    "\n"
    "- поле поиска: рамка браузерная.\n"
    "\n"
    "Итог: верну в работу, после выкладки проверю ещё раз."
)


def test_plain_of_comment_letter_is_only_the_comment():
    """Предпосылка (как в исходнике): в plain нет ни ссылок `/browse/`, ни шапки — только комментарий."""
    assert "/browse/" not in COMMENT_PLAIN
    assert "Пользователь" not in COMMENT_PLAIN
    assert not trackers.plain_is_full_jira(COMMENT_PLAIN, COMMENT_SUBJECT, "acme.atlassian.net")


def test_updates_and_comment_from_html():
    """«2 обновления + комментарий»: одна часть; строки обновлений, пустая строка, комментарий; автор — автор
    комментария; `mentions_me = True`; беседа-задача `info` поднимается до `react`."""
    msg = _fetched(_raw("jira_comment_update.eml"))
    part = _only(msg)
    assert part.channel["ext_id"] == "jira:acme.atlassian.net:WEB-40"
    assert part.channel["title"] == "WEB-40 · Перевести формы на Tailwind"
    assert part.text == ("Статус: TEST → Открыто повторно\nИсполнитель: admin → —\n\n" + COMMENT_TEXT)
    assert part.actor == "admin"
    assert part.mentions_me is True
    event = convert.tracker_event(msg, part, me=ME)
    assert event["text"] == part.text
    assert event["mentions_me"] is True
    assert event["sender"]["name"] == "admin"
    assert event["refs"] == ["WEB-40"]
    mode = attention.effective_mode(0, 0, None, 2, "issue", None, None, 3)
    assert mode == "info"
    assert attention.message_attention(mode, event["mentions_me"], "issue") == "react"
    for junk in ("http", "Ответить", "Просмотреть задачу", "Thumbs", "Fire", "Avatar", "Подсказка", "19:24"):
        assert junk not in part.text, junk
    assert not re.search("[-]", part.text)
    # ни одно слово комментария из plain не потеряно
    assert _words(COMMENT_PLAIN) == _words(COMMENT_TEXT)


def test_comment_only_letter():
    """Письмо только с комментарием (без раздела «Обновления») — текст комментария, `mentions_me = True`."""
    markup = re.sub(
        r"<div[^>]*><br><span[^>]*>Обновления</span></div>.*?(?=<div[^>]*><br><span[^>]*>Комментарии</span>)",
        "",
        COMMENT_HTML,
        flags=re.DOTALL,
    )
    markup = re.sub(r"admin и Automation for Jira <b[^>]*> выполнили 2\s+обновления</b>, ", "", markup)
    assert "Обновления" not in markup
    assert "выполнили" not in markup
    part = _only(_letter(COMMENT_PLAIN, markup))
    assert part.text == COMMENT_TEXT
    assert part.actor == "admin"
    assert part.mentions_me is True


def test_full_plain_update_unchanged():
    """Письмо «обновление» с полным plain (как ABC-102): разбор по plain, как раньше; `mentions_me = False`."""
    raw = _raw("jira_update_full.eml")
    plain, _markup = _parts(raw)
    subject = "[Jira] (OPS-17) «Ограничить частоту рассылки»"
    assert trackers.plain_is_full_jira(plain, subject, "acme.atlassian.net")
    msg = _fetched(raw)
    part = _only(msg)
    assert part.channel["ext_id"] == "jira:acme.atlassian.net:OPS-17"
    assert part.text == "Статус: В работе → В процессе проверки"
    assert part.actor == "Oleg Testov"
    assert part.mentions_me is False
    assert convert.tracker_event(msg, part, me=ME)["mentions_me"] is False


def test_broken_html_keeps_plain_text():
    """HTML без разделов — прежний путь (plain, общая чистка): ни одно слово не потеряно; шапка HTML
    читается — `mentions_me` по ней."""
    head_only = (
        '<html><body><p><span>Пользователь admin <b style="font-weight:600"> добавил 1 новый комментарий</b>.'
        "</span></p><div>что-то пошло не так</div></body></html>"
    )
    part = _only(_letter(COMMENT_PLAIN, head_only))
    assert part.text == trackers.clean_generic(COMMENT_PLAIN)
    assert _words(part.text) == _words(COMMENT_PLAIN)
    assert part.mentions_me is True

    part = _only(_letter(COMMENT_PLAIN, "<html><body><p>HTML-версия <b>уведомления"))
    assert _words(part.text) == _words(COMMENT_PLAIN)
    assert part.mentions_me is False


def _synthetic(comments: str, head: str) -> str:
    return (
        f"<html><body><p><span>{head}</span></p>"
        '<div><a href="https://acme.atlassian.net/browse/WEB-40?atlOrigin=x">WEB-40</a></div>'
        f"<div><br><span>Comments</span></div>{comments}"
        '<div><a href="https://acme.atlassian.net/browse/WEB-40"> View issue </a></div>'
        '<div data-testid="jira-email-alt-footer"><span>This message was sent by Atlassian Jira</span></div>'
        "</body></html>"
    )


def _comment(author: str, paragraphs: list[str]) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        '<div><table><tr><td><img src="https://secure.gravatar.com/avatar/x.png" alt="Avatar">&nbsp;&nbsp;'
        f"<span><strong>{author}</strong></span>&nbsp;&nbsp;<span>10:05 Лондон</span></td></tr></table>"
        f'<div>{body}</div><table data-testid="comment-ctas"><tr><td>'
        '<a href="https://acme.atlassian.net/browse/WEB-40?focusedCommentId=1&action=REPLY_TO_COMMENT">Reply</a>'
        '</td><td><a href="https://acme.atlassian.net/browse/WEB-40?action=QUICK_REACTION">'
        '<img src="https://cdn.example/1f44d.png" alt="Thumbs Up"></a></td></tr></table></div>'
    )


def test_two_comments_each_with_author_and_lines_like_markers_kept():
    """Несколько комментариев — каждый с «<автор>:»; строки комментария, похожие на кнопки/подвал/поле
    («Ответить …», «Просмотреть задачу …», «Чтобы настроить …», «Итог: …»), текст не обрывают."""
    comments = _comment("Анна Тестова", ["Ответить заказчику надо сегодня.", "Итог: ждём макет."]) + _comment(
        "Иван Петров", ["Просмотреть задачу можно завтра.", "Чтобы настроить отступы, нужен макет."]
    )
    markup = _synthetic(comments, "Анна Тестова и Иван Петров <b> added 2 new comments</b>")
    notes = trackers.parse_jira(
        "только текст первого комментария",
        COMMENT_SUBJECT,
        "acme.atlassian.net",
        from_name="Анна Тестова (Jira)",
        html_text=trackers.jira_html_text(markup),
    )
    assert notes is not None and len(notes) == 1
    note = notes[0]
    assert note.key == "WEB-40"
    assert note.actor == "Анна Тестова"
    assert note.mentions_me is True
    assert note.body == (
        "Анна Тестова: Ответить заказчику надо сегодня.\n\nИтог: ждём макет.\n\n"
        "Иван Петров: Просмотреть задачу можно завтра.\n\nЧтобы настроить отступы, нужен макет."
    )


def test_plain_with_link_to_other_issue_is_not_full():
    """Комментарий со ссылкой на ДРУГУЮ задачу — plain всё равно «только комментарий», ключ — не из неё."""
    plain = "Смотри https://acme.atlassian.net/browse/WEB-39 — там то же."
    assert not trackers.plain_is_full_jira(plain, COMMENT_SUBJECT, "acme.atlassian.net")
    part = _only(_letter(plain, COMMENT_HTML))
    assert part.channel["ext_id"] == "jira:acme.atlassian.net:WEB-40"
    assert part.text.startswith("Статус: TEST → Открыто повторно\n")
