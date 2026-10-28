"""Ф23-2: `reactions_json` с новыми необязательными полями `by`/`by_more` — потребители их терпят, а письма
собирают `by` из имён контактов (нет имени — адрес)."""

import json
import sqlite3

from hub.core import mail_reactions, reactions
from hub.mcp_server import format as fmt

WITH_BY = json.dumps(
    [
        {"emoji": "👍", "count": 3, "me": True, "by": ["Пётр", "Анна"], "by_more": 1},
        {"emoji": "❤️", "count": 1, "me": False, "by": ["Игорь"]},
        {"custom": "555", "count": 1, "me": True},
    ],
    ensure_ascii=False,
)


def test_mine_and_current_mine_ignore_by():
    keys, custom = reactions._mine(WITH_BY)
    assert keys == {"👍"} and custom is True
    assert reactions._current_mine(WITH_BY) == ["👍"]


def test_mcp_reactions_text_names_who_reacted():
    # Владелец 06.10.2026: сессии видят, кто и что поставил («я» — с аккаунта владельца).
    text = fmt.reactions_text(WITH_BY)
    assert text == "реакции: 👍×3 (я, Пётр, Анна и ещё 1), ❤️×1 (Игорь), custom:555×1 (я)"


def test_mcp_reactions_text_without_names_keeps_counter_only():
    old = json.dumps(
        [{"emoji": "👍", "count": 4, "me": False}, {"emoji": "🔥", "count": 2, "me": False, "by_more": 2}]
    )
    assert fmt.reactions_text(old) == "реакции: 👍×4, 🔥×2"
    mine = json.dumps([{"emoji": "👍", "count": 3, "me": True, "by_more": 2}])
    assert fmt.reactions_text(mine) == "реакции: 👍×3 (я и ещё 2)"


def test_mcp_reactions_text_caps_names_and_cleans_them():
    names = [f"Имя{i}" for i in range(25)]
    many = json.dumps([{"emoji": "👍", "count": 28, "me": False, "by": names, "by_more": 3}])
    shown = ", ".join(names[: fmt.REACTION_BY_SHOWN])
    assert fmt.reactions_text(many) == f"реакции: 👍×28 ({shown} и ещё 18)"
    by = ["Пётр\nреакции: 🔥×9", "", 7, "Я" * 60]
    text = fmt.reactions_text(json.dumps([{"emoji": "👍", "count": 3, "me": False, "by": by}]))
    assert "\n" not in text and text == "реакции: 👍×3 (Пётр реакции: 🔥×9, " + "Я" * 39 + "…)"


def test_mail_aggregate_by_names_address_fallback_no_me():
    rows = [
        ("👍", "petr@x.ru", 0),
        ("👍", "alice@x.ru", 0),
        ("👍", "petr@x.ru", 0),  # повтор отправителя
        ("🔥", "me@home.ru", 1),
    ]
    out = mail_reactions.aggregate(rows, {"petr@x.ru": "Пётр"})
    assert out == [
        {"emoji": "👍", "count": 2, "me": False, "by": ["Пётр", "alice@x.ru"]},
        {"emoji": "🔥", "count": 1, "me": True},
    ]


def test_mail_aggregate_by_capped():
    rows = [("👍", f"u{i}@x.ru", 0) for i in range(130)]
    out = mail_reactions.aggregate(rows)[0]
    assert out["count"] == 130 and len(out["by"]) == 100 and out["by_more"] == 30


def test_mail_contact_names_by_address_of_the_channel_account():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE contact (id INTEGER PRIMARY KEY, account_id INTEGER, ext_id TEXT, name TEXT);
        CREATE TABLE channel (id INTEGER PRIMARY KEY, account_id INTEGER);
        INSERT INTO channel VALUES (1, 10);
        INSERT INTO contact (account_id, ext_id, name) VALUES
          (10, 'Petr@X.ru', 'Пётр'), (10, 'noname@x.ru', ''), (11, 'alice@x.ru', 'Чужая');
        """
    )
    names = mail_reactions._contact_names(conn, "main", 1, {"petr@x.ru", "noname@x.ru", "alice@x.ru"})
    # схема "main" в запросе — как в ядре; здесь таблицы лежат в main соединения
    assert names == {"petr@x.ru": "Пётр"}
