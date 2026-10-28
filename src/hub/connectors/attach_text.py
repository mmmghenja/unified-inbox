"""Вложения сообщения «в формате Slack» (`attachments`: `pretext`, `title`, `text`, `fallback` …) — общий
отбор полей для коннекторов Slack и Mattermost (Ф40-2a: вынесен из `slack/convert._bot_attachment_part`).

Разметку полей каждый коннектор разбирает сам: у Slack это mrkdwn, у Mattermost — Markdown как есть.
"""

from __future__ import annotations


def attachment_values(a: dict, keys: tuple[str, ...], *, fallback: bool = True) -> list[tuple[str, str]]:
    """Непустые строковые поля вложения `a` в порядке `keys` — пары `(ключ, значение)`. Ни одного нет и
    `fallback=True` — единственная пара `("fallback", …)`, если у вложения есть `fallback`."""
    out: list[tuple[str, str]] = []
    for key in keys:
        value = a.get(key)
        if value:
            out.append((key, value))
    if not out and fallback and a.get("fallback"):
        out.append(("fallback", a["fallback"]))
    return out
