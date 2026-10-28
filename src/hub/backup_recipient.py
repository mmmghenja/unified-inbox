"""Публичный recipient `age` для бэкапа: `<config_dir>/backup.recipient`. Отдельный модуль БЕЗ `sqlite3`:
его читают и CLI-клиенты (`hub backup --set-recipient`, `hub watch`), которым по общему правилу CLI слой БД
импортировать нельзя (docs/architecture.md), и сам `core/backup.py`.

Приватный ключ хаб не видит, не хранит и не просит. Recipient — не секрет, но файл всё равно 0600.
"""

import os
import string
from pathlib import Path

RECIPIENT_FILE_NAME = "backup.recipient"
_BECH32ISH = frozenset(string.ascii_lowercase + string.digits)


def _valid(line: str) -> str | None:
    """Ровно одно поле вида `age1…` (bech32 — строчные буквы и цифры). Хвост после ключа (`age1… # моё`)
    уехал бы в argv `age -r` одним получателем и дал бы голый отказ — такую строку считаем ненастроенной."""
    fields = line.split()
    if len(fields) != 1:
        return None
    value = fields[0]
    ok = value.startswith("age1") and len(value) > 4 and set(value) <= _BECH32ISH
    return value if ok else None


def read_recipient(config_dir: Path) -> str | None:
    """Первая содержательная (не пустая, не `#`-комментарий) строка файла, если она начинается с `age1` —
    иначе `None` (файла нет, файл пуст, файл битый: все три случая — «recipient не настроен»)."""
    path = config_dir / RECIPIENT_FILE_NAME
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        return _valid(line)
    return None


def write_recipient(config_dir: Path, recipient: str) -> None:
    """`hub backup --set-recipient age1...` — файл 0600, перезаписывается (recipient — не секрет, смена
    ключа получателя должна быть возможна без ручного `rm`). Невалидное значение -> `ValueError`, файл не
    трогается."""
    value = _valid(recipient.strip())
    if value is None:
        raise ValueError("recipient должен быть одним публичным ключом age1…")
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config_dir / RECIPIENT_FILE_NAME
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
    finally:
        os.chmod(path, 0o600)  # умаск процесса мог ослабить права при open()
