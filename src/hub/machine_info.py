"""Ф77-1 (docs/architecture.md): что клиент (MCP-сервер сессии) сообщает ядру о своей
машине — поле `machine` в `POST /subs`, `POST /chan-subs` и пульсе `POST /subs/beat`.

- `hostname` — `socket.gethostname()` (не длиннее 64, только печатные);
- `route` — путь до ядра: имя маршрута, которым поднят туннель `hub-link` на этой машине (файл
  `${XDG_RUNTIME_DIR:-/tmp}/hub-link.route` пишет `scripts/hub-link.sh` перед `ssh`), только `lan` / `s1` /
  `s1-socks`; нет файла или мусор — `None` (машина ядра или путь неизвестен — ядро своё отличит само);
- `version` — `buildinfo.commit()`.

Новых секретов нет: `machine-id` наружу не уходит (хеш машины — поле `host` тела `session`)."""

import os
import socket
from collections.abc import Mapping
from pathlib import Path

from . import buildinfo

ROUTE_FILE = "hub-link.route"
ROUTES = ("lan", "s1", "s1-socks")
HOSTNAME_MAX = 64


def route_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("XDG_RUNTIME_DIR") or "/tmp") / ROUTE_FILE  # noqa: S108 — как в hub-link.sh


def read_route(environ: Mapping[str, str] | None = None) -> str | None:
    """Маршрут `hub-link` этой машины или `None` (файла нет, не прочитать, не из перечня)."""
    try:
        text = route_path(environ).read_text(encoding="ascii", errors="replace")
    except OSError, ValueError:
        return None
    text = text.strip()
    return text if text in ROUTES else None


def clean_hostname(raw) -> str | None:
    """Имя хоста — печатные символы, не длиннее 64; пусто — `None`."""
    if not isinstance(raw, str):
        return None
    text = "".join(ch for ch in raw if ch.isprintable()).strip()[:HOSTNAME_MAX]
    return text or None


def local_machine(environ: Mapping[str, str] | None = None) -> dict:
    """`{hostname, route, version}` этой машины для поля `machine`."""
    try:
        name = socket.gethostname()
    except OSError:
        name = None
    return {"hostname": clean_hostname(name), "route": read_route(environ), "version": buildinfo.commit()}
