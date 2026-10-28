"""Ф41-8: системный текст постоянной сессии хаба несёт правила роли из каталога (`held_rules`)."""

from hub import roles
from hub.work import held as held_mod

HEADER = "Роль и взаимодействие (правила хаба):"


def _rec(role):
    return {"id": 5, "role": role, "role_no": 2, "engine": "claude"}


def test_role_block_with_role_text():
    text = held_mod.held_rules(_rec("программист"), "Проект")
    assert HEADER in text
    block = text.split(f"\n\n{HEADER}\n", 1)[1]
    # Ф48-3: встроенного текста роли нет — в блоке только общий текст хаба.
    assert block == roles.prompt("программист") == roles.common_text()
    assert "Ты — программист проекта." not in text


def test_role_block_after_extra():
    text = held_mod.held_rules(_rec("программист"), "Проект", "ДОП-СТРОКА")
    assert text.index("ДОП-СТРОКА") < text.index(HEADER)


def test_no_role_no_block():
    for role in (None, ""):
        assert HEADER not in held_mod.held_rules(_rec(role), "Проект")


def test_unknown_role_gets_common_only():
    text = held_mod.held_rules(_rec("маркетолог"), "Проект")
    assert text.endswith(roles.common_text())
