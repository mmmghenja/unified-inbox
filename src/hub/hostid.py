"""Ф33-1 (docs/architecture.md и 12а К9–К10): какая машина — первые 16 hex sha256
`/etc/machine-id` (нет файла — `/var/lib/dbus/machine-id`; не прочитать ни один — `None`).

Одна функция на клиента и ядро: клиент (`session_registry`, MCP-сервер, `hub sub wait`) кладёт её значение в
тело `session` поле `host`, ядро сравнивает со своим. Совпало или поля нет — сессия на машине ядра, живость
по `/proc`, как раньше. Переопределение — переменная `HUB_HOST_ID` (тесты, стенд): 16 hex — как есть, иначе
первые 16 hex её sha256. Сам `machine-id` наружу не уходит — только его хеш."""

import functools
import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

ENV = "HUB_HOST_ID"
HOST_RE = re.compile(r"^[0-9a-f]{16}$")
_MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")
BOOT_ENV = "HUB_BOOT_ID"
BOOT_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
BOOT_RE = re.compile(BOOT_PATTERN)
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@functools.cache
def _machine() -> str | None:
    for name in _MACHINE_ID_FILES:
        try:
            text = Path(name).read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        if text:
            return _digest(text)
    return None


def boot_id(environ: Mapping[str, str] | None = None) -> str | None:
    """Метка ЗАГРУЗКИ этой машины (`/proc/sys/kernel/random/boot_id`, uuid) или `None` (не прочитать).
    Ядро на другой машине по ней узнаёт, что машина сессии перезагрузилась (крышка ноутбука = выключение) и
    подписки прежней загрузки мертвы. Переопределение — `HUB_BOOT_ID` (тесты, стенд): uuid — как есть, иначе
    uuid из sha256 значения."""
    env = os.environ if environ is None else environ
    raw = (env.get(BOOT_ENV) or "").strip().lower()
    if raw:
        if BOOT_RE.fullmatch(raw):
            return raw
        h = hashlib.sha256(raw.encode()).hexdigest()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    try:
        text = Path(BOOT_ID_FILE).read_text(encoding="ascii", errors="replace").strip().lower()
    except OSError:
        return None
    return text if BOOT_RE.fullmatch(text) else None


def host_id(environ: Mapping[str, str] | None = None) -> str | None:
    """Метка этой машины (16 hex) или `None` (машину не назвать)."""
    env = os.environ if environ is None else environ
    raw = (env.get(ENV) or "").strip().lower()
    if raw:
        return raw if HOST_RE.fullmatch(raw) else _digest(raw)
    return _machine()
