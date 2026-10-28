"""Проверка текста запуска (`POST /term/{id}/run`, К8): никаких управляющих символов, кроме `\\t` и `\\n`.

Запрещено: C0 (кроме таба и перевода строки, в том числе `\\r` и ESC), DEL, C1, знаки направления текста
(U+202A–202E, U+2066–2069) — показанное в блоке кода должно быть ровно тем, что исполнится."""

import re

TEXT_MAX = 16_000
_FORBIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
SHELLS = frozenset({"bash", "zsh", "sh", "fish", "dash"})


def check(text: str) -> str | None:
    """Причина отказа или `None`."""
    if not text.strip():
        return "empty"
    if len(text) > TEXT_MAX:
        return "too_long"
    if _FORBIDDEN.search(text):
        return "control_chars"
    return None


def prepare(text: str) -> bytes:
    """Хвостовые переводы строки убираются: Enter ядро шлёт само, лишний дал бы пустую команду."""
    return text.rstrip("\n").encode("utf-8")
