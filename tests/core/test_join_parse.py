"""Ф17-1 (docs/architecture.md): разбор цели заявки на вступление
`join.parse_target` — все формы A2 и отказы (`t.me/c/…`, папки `addlist`, номер телефона `t.me/+цифры`,
мусор, длины)."""

import pytest

from hub.core.join import parse_target


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@Durov_Channel", ("username", "durov_channel")),
        ("durov_channel", ("username", "durov_channel")),
        ("  @durov_channel  ", ("username", "durov_channel")),
        ("t.me/Durov_Channel", ("username", "durov_channel")),
        ("https://t.me/durov_channel", ("username", "durov_channel")),
        ("http://t.me/durov_channel/", ("username", "durov_channel")),
        ("https://www.t.me/durov_channel", ("username", "durov_channel")),
        ("t.me/durov_channel/123", ("username", "durov_channel")),
        ("https://t.me/durov_channel/5/123", ("username", "durov_channel")),
        ("https://t.me/durov_channel/123?single", ("username", "durov_channel")),
        ("telegram.me/durov_channel", ("username", "durov_channel")),
        ("https://telegram.me/durov_channel", ("username", "durov_channel")),
        ("tg://resolve?domain=durov_channel", ("username", "durov_channel")),
        ("tg://resolve?domain=Durov_Channel&post=5", ("username", "durov_channel")),
        ("t.me/+AbCdEf_12-34xyz", ("invite", "AbCdEf_12-34xyz")),
        ("https://t.me/+AbCdEf_12-34xyz", ("invite", "AbCdEf_12-34xyz")),
        ("https://t.me/joinchat/AbCdEf1234", ("invite", "AbCdEf1234")),
        ("t.me/joinchat/AbCdEf1234/", ("invite", "AbCdEf1234")),
        ("tg://join?invite=AbCdEf1234", ("invite", "AbCdEf1234")),
    ],
)
def test_f1_forms(raw, expected):
    assert parse_target(raw) == expected


def test_f1_invite_hash_case_kept():
    kind, value = parse_target("t.me/+AbCdEfGh")
    assert kind == "invite" and value == "AbCdEfGh"


@pytest.mark.parametrize(
    "raw, needle",
    [
        ("https://t.me/c/1234567890/55", "закрытого чата"),
        ("t.me/c/1234567890", "закрытого чата"),
        ("https://t.me/addlist/AbCdEf1234", "папка"),
        ("t.me/+79000014567", "номер телефона"),
        ("https://t.me/+12345678", "номер телефона"),
        ("t.me/+abc", "неверная"),
        ("t.me/+" + "a" * 65, "неверная"),
        ("tg://join?invite=abc", "неверная"),
    ],
)
def test_f1_refusals_with_reason(raw, needle):
    with pytest.raises(ValueError, match=needle):
        parse_target(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "@",
        "@abc",  # короче 5 символов
        "@1channel",  # с цифры
        "@" + "a" * 33,  # длиннее 32
        "@name with space",
        "https://example.com/durov_channel",
        "https://t.me.evil.com/durov_channel",
        "https://t.me:8443/durov_channel",
        "https://user@t.me/durov_channel",
        "t.me/",
        "t.me/durov_channel/abc",
        "t.me/durov_channel/1/2/3",
        "t.me/share/url",
        "t.me/joinchat",
        "t.me/addstickers/Pack",
        "@joinchat",
        "tg://msg?to=durov_channel",
        "tg://resolve?domain=a&domain=b",
        "a" * 301,
        None,
        123,
    ],
)
def test_f1_garbage_refused(raw):
    with pytest.raises(ValueError):
        parse_target(raw)
