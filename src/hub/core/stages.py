"""Стадии бесед (Ф34-1, docs/architecture.md). Стадия — свободная короткая метка беседы, одна на
беседу: колонки `channel.stage` (как записано), `stage_key` (ключ сравнения), `stage_note`, `stage_ts`
(миграция `0046_shared.sql`). Словаря стадий хаб не держит и сам стадию не меняет — её ставит владелец в UI или
сессия со скоупом `organize` (`channels.update_channel`). Здесь — нормализация и наследование стадии при
слиянии бесед; чтение (список стадий проекта, фильтр) — в `queries.py`.
"""

import unicodedata

STAGE_MAX = 40
NOTE_MAX = 200


class StageError(ValueError):
    """Недопустимая стадия или заметка (длина, перевод строки, заметка без стадии) — 422 у ручки."""


def _sql(schema: str, template: str) -> str:
    """Тот же приём, что в channels.py/queries.py: схема подставляется здесь (ruff S608)."""
    return template.format(schema=schema)


def _visible(value: str) -> str:
    """Ревью Ф34-1: NFC и без невидимых символов (категории Cc/Cf: нулевой ширины, управляющие, смена
    направления) — иначе стадия из одних невидимых даёт пустой чип, а «Мой клиент» в NFC и NFD — разные
    стадии."""
    text = unicodedata.normalize("NFC", value)
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")).strip()


def _one_line(value: str, what: str, limit: int) -> str:
    if len(value.strip().splitlines()) > 1:
        raise StageError(f"{what}: без переводов строк")
    text = _visible(value)
    if len(text) > limit:
        raise StageError(f"{what}: не длиннее {limit} символов, получено {len(text)}")
    return text


def stage_key(stage: str) -> str:
    """П3: ключ сравнения стадий — `casefold` видимого текста в NFC («В работе» и «в работе » — одна
    стадия)."""
    return _visible(stage).casefold()


def stage_form(stage: str | None) -> tuple[str | None, str | None]:
    """Стадия запроса -> `(stage, stage_key)`; `None`, `""` и одни пробелы — `(None, None)`, стадию снять."""
    if stage is None:
        return None, None
    text = _one_line(stage, "stage", STAGE_MAX)
    if not text:
        return None, None
    return text, stage_key(text)


def note_form(note: str | None) -> str | None:
    """Заметка запроса; `None`, `""` и одни пробелы — `None`, заметку снять."""
    if note is None:
        return None
    return _one_line(note, "stage_note", NOTE_MAX) or None


def inherit(conn, schema: str, from_id: int, to_id: int) -> bool:
    """Решение 4 + П2: беседа `from_id` вливается в `to_id` (заготовка -> настоящая беседа, `channel_migrate`)
    — её стадия (с заметкой и временем) переезжает, если у `to_id` стадии нет; иначе остаётся стадия `to_id`.
    `channel.seq` двигает вызывающий. Возвращает True, если стадия переехала."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT stage, stage_key, stage_note, stage_ts FROM {schema}.channel "
            "WHERE id = ? AND stage_key IS NOT NULL",
        ),
        (from_id,),
    ).fetchone()
    if row is None:
        return False
    cur = conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.channel SET stage = ?, stage_key = ?, stage_note = ?, stage_ts = ? "
            "WHERE id = ? AND stage_key IS NULL",
        ),
        (*row, to_id),
    )
    return cur.rowcount > 0
