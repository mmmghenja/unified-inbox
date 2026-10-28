"""Текст письма (спека 5a, D1–D3, тест 9 и М14): фикстуры реальных форматов в `fixtures/`."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.connectors.mail import convert, textproc

FIX = Path(__file__).parent / "fixtures"


def body(name: str) -> textproc.BodyResult:
    return textproc.extract_body((FIX / name).read_bytes())


@pytest.mark.parametrize(
    ("name", "kept", "cut"),
    [
        ("gmail_ru.eml", "Смету посмотрел, всё ок.", "Посмотри, пожалуйста, смету"),
        ("gmail_en.eml", "Sounds good, let's meet on Friday.", "Can we meet this week?"),
        ("outlook_ru.eml", "Согласовано, можно запускать.", "согласуйте запуск"),
        ("outlook_en.eml", "Approved. Please proceed.", "please approve the launch"),
        ("apple_mail.eml", "Да, давай в пятницу.", "Встретимся на неделе?"),
    ],
)
def test_9_quote_cut_signature_kept(name, kept, cut):
    r = body(name)
    assert kept in r.text
    assert cut not in r.text
    assert "wrote:" not in r.text and "написал" not in r.text


def test_9_signature_not_cut():
    assert "Максим\n+7 900 000-00-00" in body("gmail_ru.eml").text
    assert "ООО «Ромашка»" in body("outlook_ru.eml").text
    assert "Отправлено с iPhone" in body("apple_mail.eml").text
    assert "Best,\nOwner" in body("gmail_en.eml").text


def test_9_html_only_to_text():
    t = body("html_only.eml").text
    assert "<" not in t.replace("<boss", "")
    assert "alert" not in t and "color:red" not in t and "Рассылка" not in t
    assert "Новости недели" in t
    assert "Цены «ниже» на 10% & больше." in t
    assert "на сайте (https://example.com/news?id=5)" in t
    assert "https://example.com/ (" not in t  # текст ссылки = url -> без повтора
    assert "Строка 1\nСтрока 2" in t
    assert "\n\n\n" not in t
    assert "track.example.com" not in t


def test_9_attachments():
    r = body("with_attachment.eml")
    assert r.text == "Счёт во вложении."
    names = {a["name"]: a for a in r.attachments}
    assert names["счет.pdf"]["mime"] == "application/pdf" and names["счет.pdf"]["size"] > 0
    assert names["logo.png"]["mime"] == "image/png"  # непоказываемое inline — тоже вложение


def test_9_only_attachment_has_no_text():
    r = body("only_attachment.eml")
    assert r.text is None
    assert [a["name"] for a in r.attachments] == ["scan.pdf"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("koi8r.eml", "Привет из старого почтовика!"),
        ("cp1251.eml", "Документы в порядке."),
        ("gmail_ru.eml", "Отправлю договор завтра."),  # quoted-printable
    ],
)
def test_9_encodings(name, text):
    assert text in body(name).text


def test_9_subject_encodings():
    raw = (FIX / "cp1251.eml").read_bytes()
    assert convert.thread_title(raw) == "Документы"
    raw = (FIX / "koi8r.eml").read_bytes()
    assert convert.thread_title(raw) == "Привет"


def test_9_only_quote_not_empty():
    t = body("only_quote.eml").text
    assert "old text" in t


def test_m14_forward_not_cut():
    t = body("forward.eml").text
    assert "Смотри, что пришло." in t
    assert "Смета на 100 000 руб." in t


def test_m14_outlook_block_only_after_reply_text():
    text = "From: Peter <p@example.com>\nSent: Monday\nTo: me\nSubject: x\n\nbody of letter"
    assert textproc.strip_quote(text) == text


def test_mailru_header_cut_with_gt_tail():
    """Живая почта 24.09: заголовок цитаты mail.ru — адрес без скобок и « :» в конце — уходит с цитатой;
    строка с адресом и двоеточием, но без «от … адрес :» после времени, — не заголовок."""
    text = (
        "Здравствуйте. Вопрос не решился.\n\n--\nОтправлено из Mail для Android\n\n"
        "вторник, 22 сентября 2026г., 20:33 +03:00 от Umbrella support@umbrella.example.com :\n\n"
        "> Добрый день!\n> Как успехи?"
    )
    assert textproc.strip_quote(text) == (
        "Здравствуйте. Вопрос не решился.\n\n--\nОтправлено из Mail для Android"
    )
    english = "Ok\n\nTuesday, 22 September 2026, 20:33 +03:00 from Support support@umbrella.example.com :\n\n> hi"
    assert textproc.strip_quote(english) == "Ok"
    body_line = "Звоните до 18:00 или пишите на help@example.com:\nсписок документов"
    assert textproc.strip_quote(body_line) == body_line


def test_d2_gt_tail_cut_but_inline_quotes_kept():
    text = "Ответ\n\n> цитата\n> ещё"
    assert textproc.strip_quote(text) == "Ответ"
    interleaved = "> вопрос 1\nответ 1\n> вопрос 2\nответ 2"
    assert textproc.strip_quote(interleaved) == interleaved


def test_d1_long_text_truncated():
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + ("я" * 25_000).encode()
    t = textproc.extract_body(raw).text
    assert len(t) == textproc.MAX_TEXT_LEN + len(textproc.TRUNCATED_MARK)
    assert t.endswith("…[обрезано]")


def test_d1_unknown_charset_does_not_crash():
    raw = b"Content-Type: text/plain; charset=x-unknown-42\r\n\r\nhello \xff"
    t = textproc.extract_body(raw).text
    assert t.startswith("hello")


def test_d1_partial_message_note():
    raw = (FIX / "with_attachment.eml").read_bytes()[:900]
    r = textproc.extract_body(raw, partial=True)
    assert textproc.BIG_MESSAGE_NOTE in r.text


@pytest.mark.parametrize(
    ("subject", "title"),
    [
        ("Re: Смета", "Смета"),
        ("RE: Re[2]: FW: Смета", "Смета"),
        ("Fwd: AW: Отв: Пересл: смета", "смета"),
        ("отв:  ответ: Тема", "Тема"),
        ("", "(без темы)"),
        ("Re:", "(без темы)"),
        ("Прочее: не префикс", "Прочее: не префикс"),
    ],
)
def test_m14_subject_prefixes(subject, title):
    assert convert.normalize_subject(subject) == title


@pytest.mark.parametrize(
    ("subject", "reply"),
    [("Смета", "Re: Смета"), ("Re: Смета", "Re: Смета"), ("RE[2]: x", "RE[2]: x"), ("Fwd: x", "Re: Fwd: x")],
)
def test_e2_reply_subject_no_double_re(subject, reply):
    assert convert.reply_subject(subject) == reply
