"""Хотфикс 21.10: ядро падало при старте — загрузка истории решений (`decisions.backfill`) споткнулась о
команду, у которой после нормализации остался только пробел (`first_command`: IndexError)."""

from hub.core import decision_print as dp


def test_first_command_blank_segments():
    assert dp.first_command("   ") == ""
    assert dp.first_command("; ;") in ("", ";")
    for cmd in ("cd x &&  ", "  && git push", "| \t |", "( )", "$( )"):
        dp.first_command(cmd)  # не падает
