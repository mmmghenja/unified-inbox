"""Ф57-1 (ревью): письмо человека через группу Google несёт `List-Unsubscribe` и `List-Post` — не рассылка."""

from hub.connectors.mail import convert

GOOGLE_GROUP_HUMAN = {
    "from": "Пётр Иванов <petr@globex.example>",
    "to": "dev@globex.example",
    "precedence": "list",
    "mailing-list": "list dev@globex.example; contact dev+owners@globex.example",
    "list-id": "<dev.globex.example>",
    "list-post": "<mailto:dev@globex.example>",
    "list-help": "<https://support.google.com/a/globex.example/bin/topic.py?topic=25838>, "
    "<mailto:dev+help@globex.example>",
    "list-archive": "<https://groups.google.com/a/globex.example/group/dev/>",
    "list-unsubscribe": "<mailto:googlegroups-manage+123+unsubscribe@googlegroups.com>, "
    "<https://groups.google.com/a/globex.example/group/dev/subscribe>",
}


def test_human_letter_via_google_group_is_not_bulk():
    assert convert.is_bulk(GOOGLE_GROUP_HUMAN, "petr@globex.example") is False
