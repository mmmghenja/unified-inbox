"""Присутствие: «Владелец смотрит эту беседу в окне хаба». Спецификация: docs/architecture.md и
поправка П8.

Только в ПАМЯТИ процесса ядра (не в БД): рестарт ядра присутствие обнуляет — UI присылает его заново каждые
20 с. Ключ — `(token_id UI-сессии, client_id вкладки)`: у каждой вкладки своя запись, `channel_id: null`
снимает только её. Запись — `(channel_id, thread_ext_id | None)` и срок `now + 45 с`.

Правило «видно» (П8): сообщение верхнего уровня — есть любая запись этого канала; ответ в треде — есть запись
`(канал, этот тред)` (панель треда открыта). Проверки разборщика и hub-wake — `is_visible` у них же по
выдаче `GET /presence`.
"""

from typing import NamedTuple

TTL_MS = 45_000
MAX_ENTRIES = 256  # предохранитель: случайные client_id не раздувают память (старейшие вытесняются)


class _Entry(NamedTuple):
    channel_id: int
    thread_ext_id: str | None
    expires_ms: int


class Presence:
    def __init__(self) -> None:
        self._entries: dict[tuple[int, str], _Entry] = {}

    def set(
        self, *, token_id: int, client_id: str, channel_id: int | None, thread_ext_id: str | None, now_ms: int
    ) -> None:
        key = (token_id, client_id)
        self._entries.pop(key, None)
        if channel_id is None:
            return
        self._purge(now_ms)
        while len(self._entries) >= MAX_ENTRIES:
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_ms)
            del self._entries[oldest]
        self._entries[key] = _Entry(channel_id, thread_ext_id, now_ms + TTL_MS)

    def _purge(self, now_ms: int) -> None:
        for key in [k for k, e in self._entries.items() if e.expires_ms <= now_ms]:
            del self._entries[key]

    def items(self, now_ms: int) -> list[tuple[int, str | None]]:
        """Действующие записи всех вкладок — объединение, без повторов, в стабильном порядке."""
        self._purge(now_ms)
        return sorted(
            {(e.channel_id, e.thread_ext_id) for e in self._entries.values()},
            key=lambda item: (item[0], item[1] or ""),
        )


def is_visible(items: list[dict], channel_id: int, thread_ext_id: str | None) -> bool:
    """П8 на выдаче `GET /presence` (`items`): верхний уровень — любая запись канала; ответ в треде — запись
    `(канал, этот тред)`. Зовут разборщик и hub-wake."""
    for item in items:
        if item.get("channel_id") != channel_id:
            continue
        if thread_ext_id is None or item.get("thread_ext_id") == thread_ext_id:
            return True
    return False
