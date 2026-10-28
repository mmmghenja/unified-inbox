"""Позиция сообщений по `ext_id`: числовой порядок (Telegram) и время Slack (`ts` вида `"1699999999.123456"`).
Спецификация: `docs/architecture.md` ОДИН модуль — `ingest.py`, `outbox.py`,
`queries.py` (и коннектор 2c) обязаны пользоваться ИМЕННО этими функциями, не переписывать локально: иначе
`(ts, ext_sort)` разъедется между путями приёма и отправки.
"""

import re

# Telegram: числовой ext_id (id сообщения), не длиннее 18 знаков — как было в ingest.py/outbox.py.
_NUMERIC_MAX_LEN = 18
# Slack: `ts` вида "<секунды>.<микросекунды>" — секунд до 12 цифр (с запасом), дробная часть строго 6 знаков
# (реальный формат Slack). Поправки после opus-критика кода (E1): `re.ASCII` — без него `\d` берёт ЛЮБЫЕ
# юникод-цифры (например, арабские-индийские), которые не парсятся `int()` в ожидаемом смысле; `fullmatch`
# вместо `match()` с `^…$` — без него `$` пропускает хвостовой `\n` (питоновская особенность `$`).
_SLACK_TS_RE = re.compile(r"(\d{1,12})\.(\d{6})", re.ASCII)
# Ф40-1 B: позиция «прочитано до ВРЕМЕНИ» (Mattermost) — `(up_to_ts, EXT_SORT_MAX, "")`. Больше любого
# настоящего `ext_sort` (числовой id — до 18 знаков, `ts` Slack — до 12 цифр секунд * 10^6: оба меньше
# 10^18 < 2^62), поэтому любое сообщение с `ts == up_to_ts` лежит не дальше маркера, а с `ts` больше — дальше.
EXT_SORT_MAX = 2**62


def is_slack_ts(ext_id: str) -> bool:
    """True, если `ext_id` имеет вид `ts` Slack (`"<секунды>.<микросекунды>"`)."""
    return _SLACK_TS_RE.fullmatch(ext_id) is not None


def ext_sort(ext_id: str | None) -> int:
    """Числовая позиция `ext_id` для порядка показа `(ts, ext_sort, ext_id)`:
    - чистые ASCII-цифры длиной <= 18 (Telegram) -> число;
    - `ts` Slack (`^\\d{1,12}\\.\\d{6}$`, `re.ASCII`) -> `int(сек) * 1_000_000 + int(дробь)`;
    - иначе (в т.ч. `None` — заготовка outbox) -> 0.
    """
    if ext_id is None:
        return 0
    if ext_id.isascii() and ext_id.isdigit() and len(ext_id) <= _NUMERIC_MAX_LEN:
        return int(ext_id)
    m = _SLACK_TS_RE.fullmatch(ext_id)
    if m:
        return int(m.group(1)) * 1_000_000 + int(m.group(2))
    return 0


def slack_ts_to_ms(ts: str) -> int:
    """`ts` Slack (`"<секунды>.<микросекунды>"`) -> миллисекунды с эпохи. Единственная функция перевода —
    ей обязан пользоваться и коннектор Slack (2c), иначе `(ts, ext_sort)` разъедется между путями приёма и
    отправки (раздел A спеки)."""
    sec_str, frac_str = ts.split(".", 1)
    return int(sec_str) * 1000 + int(frac_str[:3])
