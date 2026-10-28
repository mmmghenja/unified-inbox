"""Диктовка через Yandex SpeechKit (Ф59-1, docs/architecture.md).

- `config` — ключ и настройки в `<config_dir>/speechkit/` (ключ вводит только владелец: `hub auth speechkit`);
- `recognize` — один вызов SpeechKit v1 REST (короткое аудио), без повторов;
- `limits` — пределы в памяти ядра (в минуту и в сутки);
- `auth` — команда `hub auth speechkit`.

Звук и текст нигде не сохраняются и не пишутся в журнал; ключ — только в файле 0600.
"""
