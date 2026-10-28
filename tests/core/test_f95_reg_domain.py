"""Ф95-1 (A.2, A.3): домен регистрации заявки «нужен человек» — из `url` или `site`; запретный список."""

from types import SimpleNamespace

import pytest

from hub.core import reg_mail_rule as rmr


@pytest.mark.parametrize(
    ("url", "site", "expected"),
    [
        ("https://www.signup.example/login/", None, "signup.example"),
        ("https://WWW.Example.COM:8443/a?b=c", None, "example.com"),
        ("https://mail.youdo.com/x", None, "youdo.com"),
        ("https://a.b.shop.spb.ru/", None, "shop.spb.ru"),
        ("https://shop.co.uk/", None, "shop.co.uk"),
        ("https://user:pass@my.site.com.ua/", None, "site.com.ua"),
        ("jobs.example/login/", None, "jobs.example"),
        ("https://пример.рф/вход", None, "xn--e1afmkfd.xn--p1ai"),
        ("http://192.168.0.1/admin", None, None),
        ("http://[::1]:8080/", None, None),
        ("http://localhost:8080/", None, None),
        ("https://com.ru/", None, None),
        ("https://intranet/", None, None),
        ("about:blank", "jobs.example", None),  # адрес назван — сайт не подставляется
        ("https://bad_host.ru/", None, None),
        ("https://-bad.ru/", None, None),
        ("http://1.2.3.999/", None, None),
        ("", "Shop.Example.org", "example.org"),
        (None, "www.jobs.example", "jobs.example"),
        ("", "FL", None),
        ("", "jobs.example вход", None),
        ("", "https://jobs.example", None),
        ("", "my_site.ru", None),
        ("", None, None),
    ],
)
def test_request_domain(url, site, expected):
    assert rmr.request_domain(url, site) == expected


def test_blocked_list_covers_services():
    for domain in ("mail.ru", "gmail.com", "google.com", "gosuslugi.ru", "t.me", "wildberries.ru"):
        assert domain in rmr.BLOCKED_DOMAINS
    # Поддомен сервиса сводится к домену из списка.
    assert rmr.request_domain("https://e.mail.ru/inbox", None) in rmr.BLOCKED_DOMAINS
    assert rmr.request_domain("https://accounts.google.com/", None) in rmr.BLOCKED_DOMAINS


def test_no_project_no_rules():
    """Заявка без проекта — правил нет (до чтения базы)."""
    req = SimpleNamespace(id=1, project_id=None, url="https://jobs.example/", site=None)
    assert rmr.add_for_request(None, req, now_ms=0) is None
