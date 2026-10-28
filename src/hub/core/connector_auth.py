"""Аутентификация на внутреннем сокете: секрет коннектора (`hubc_`) и секрет админ-CLI (`huba_`).
Спецификация: docs/architecture.md.

Права файла (0600) и каталога (0700) — первая линия защиты (план 1.1); секрет — вторая. Здесь — только
ПРОВЕРКА секретов и утилиты хеширования/файлов; записи (создание аккаунта + секрета коннектора, проекта) —
в `admin.py`, как и в остальных модулях core. Значения секретов никогда не пишутся в исключения/логи.
"""

import hashlib
import hmac
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from .security import Throttle

CONNECTOR_PREFIX = "hubc_"
ADMIN_PREFIX = "huba_"
_SECRET_BYTES = 32  # secrets.token_urlsafe(32) -> 43 символа, как в спеке


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_secret(secret: str) -> str:
    return _sha256(secret)


def generate_connector_secret() -> str:
    return CONNECTOR_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def generate_admin_secret() -> str:
    return ADMIN_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def write_secret_file(path: Path, secret: str) -> None:
    """Записать секрет в НОВЫЙ файл 0600 (падает, если уже существует — секрет не перезаписываем молча).
    Каталог — 0700, создаётся при необходимости.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(secret)
    finally:
        os.chmod(path, 0o600)  # умаск процесса мог ослабить права при open() — выставляем явно


@dataclass(frozen=True)
class ConnectorPrincipal:
    account_id: int
    source: str
    owner: str  # f"hub-{source}-{account_id}" — стабильное имя коннектора для outbox.lease/report


def resolve_connector(conn, *, secret: str, now_ms: int) -> ConnectorPrincipal | None:  # noqa: ARG001
    """`now_ms` пока не используется (секрет коннектора не протухает в v1) — параметр оставлен для
    единообразия с `security.resolve_principal` и на случай будущего TTL.
    """
    row = conn.execute(
        "SELECT cs.account_id, a.source FROM main.connector_secret cs "
        "JOIN main.account a ON a.id = cs.account_id WHERE cs.hash = ?",
        (_sha256(secret),),
    ).fetchone()
    if row is None:
        return None
    account_id, source = row
    return ConnectorPrincipal(account_id=account_id, source=source, owner=f"hub-{source}-{account_id}")


def _extract_bearer(authorization: str | None, *, prefix: str) -> str | None:
    """Достать значение Bearer-заголовка, только если оно начинается с ожидаемого префикса — иначе None.
    Так секрет коннектора не проходит на админ-ручку и наоборот, а bearer публичного API (`hub_`) — ни туда,
    ни туда (раздел «Аутентификация», тест 2 куска 5).
    """
    if authorization is None or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    return token if token.startswith(prefix) else None


def connector_dependency() -> Callable[[Request], Awaitable[ConnectorPrincipal]]:
    """Фабрика FastAPI-зависимости для коннекторных ручек внутреннего сокета."""

    async def _dependency(request: Request) -> ConnectorPrincipal:
        db = request.app.state.db
        throttle: Throttle = request.app.state.throttle
        clock = request.app.state.clock
        now_ms = int(clock() * 1000)

        secret = _extract_bearer(request.headers.get("authorization"), prefix=CONNECTOR_PREFIX)
        if secret is None:
            raise HTTPException(401, "unauthorized")

        with db.reader() as conn:
            principal = resolve_connector(conn, secret=secret, now_ms=now_ms)
        if principal is None:
            # Троттлинг бьёт только по неудачам (как в security.py после критика): один сломанный коннектор
            # мусорным секретом не должен отключать остальные.
            if throttle.blocked("connector", now_ms):
                raise HTTPException(429, "too many attempts, try later")
            throttle.record_failure("connector", now_ms)
            raise HTTPException(401, "unauthorized")
        return principal

    return _dependency


def admin_dependency() -> Callable[[Request], Awaitable[None]]:
    """Фабрика FastAPI-зависимости для CLI-админ-ручек. Секрет сравнивается с хешем, который `runner.py`
    держит в памяти (`app.state.admin_key_hash`) — без обращения к БД."""

    async def _dependency(request: Request) -> None:
        throttle: Throttle = request.app.state.throttle
        clock = request.app.state.clock
        now_ms = int(clock() * 1000)

        secret = _extract_bearer(request.headers.get("authorization"), prefix=ADMIN_PREFIX)
        if secret is None:
            raise HTTPException(401, "unauthorized")

        if not hmac.compare_digest(_sha256(secret), request.app.state.admin_key_hash):
            if throttle.blocked("admin", now_ms):
                raise HTTPException(429, "too many attempts, try later")
            throttle.record_failure("admin", now_ms)
            raise HTTPException(401, "unauthorized")

    return _dependency
