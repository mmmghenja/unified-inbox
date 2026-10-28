"""Картинки по запросу сессии через Alibaba Model Studio, qwen-image (Ф91,
docs/architecture.md).

- `config` — ключ и настройки в `<config_dir>/alibaba/` (ключ вводит только владелец: `hub auth alibaba`);
- `dashscope` — один вызов генерации DashScope и скачивание PNG сразу (ссылки живут 24 ч), без повторов;
- `auth` — команда `hub auth alibaba`.

Ключ — только в файле 0600; в журнал, исключения и ответы не попадает. Текст промпта в журнал не пишется.
"""
