"""Доступ к хабу с телефона (Ф10-7, docs/architecture.md–6 и П1, П5, П6).

Состояние удалённого доступа — ОДИН объект `RemoteState` на процесс `hub core` (П1): его создаёт `runner` и
отдаёт и публичному, и внутреннему приложению — иначе `hub remote off` через админ-сокет отзывал бы сессии в
БД, а публичное приложение продолжало бы пускать пароль. Храним в `main.meta`:

- `remote_enabled` (`0|1`), `remote_hosts` (JSON-список `host:port`);
- `remote_password` = `scrypt$<log2 n>$<r>$<p>$<соль b64>$<хеш b64>`, `remote_password_set_ts`;
- `remote_pw_failures` (неверных подряд), `remote_pw_block_until` (мс) — счётчик подбора переживает рестарт.

Функции, трогающие БД на запись, вызываются только внутри `Db.write()`. Время — параметром `now_ms`. Пароль
не логируется и не попадает в audit НИГДЕ: в audit — только `remote_login` с `ok|invalid|blocked`.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field

from . import security
from .audit import write_audit
from .tls import TlsInfo

META_ENABLED = "remote_enabled"
META_HOSTS = "remote_hosts"
META_PASSWORD = "remote_password"  # noqa: S105 — ключ main.meta, не секрет
META_PASSWORD_TS = "remote_password_set_ts"  # noqa: S105 — ключ main.meta, не секрет
META_FAILURES = "remote_pw_failures"
META_BLOCK_UNTIL = "remote_pw_block_until"

PASSWORD_MIN_LEN = 10
PASSWORD_MAX_LEN = 200
MAX_HOSTS = 5

# Решение 3: scrypt n=2**15, r=8, p=1, dklen=32 (~60 мс, 32 МиБ памяти), соль 16 байт.
SCRYPT_LOG_N = 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16

# Решение 4: подряд 5 неверных -> блок 1 мин, каждая следующая неверная удваивает (до 60 мин).
FAIL_THRESHOLD = 5
BLOCK_BASE_MS = 60_000
BLOCK_MAX_MS = 60 * 60_000
QUEUE_MAX_WAITING = 3  # ожидающих замок попыток больше этого -> 429 сразу
FAILURES_WINDOW_MS = 24 * 60 * 60 * 1000

AUDIT_ACTION = "remote_login"

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?:[0-9]{1,5}$")


class RemoteConfigError(ValueError):
    """Неверные настройки удалённого доступа (-> 422). Текст — для ответа, без секретов."""


@dataclass(frozen=True)
class RemoteConfig:
    enabled: bool
    hosts: tuple[str, ...]
    password: str | None  # хранимая строка scrypt, не сам пароль
    password_set_ts: int | None
    failures: int
    block_until: int | None


@dataclass
class RemoteState:
    """Состояние в памяти процесса: его читает `SecurityMiddleware` на каждом запросе удалённого слушателя.
    Меняется только ручками (после коммита; выключение — ДО записи, закрыто по умолчанию) и на старте.
    `listening` ставит `runner`: удалённый сокет поднят. Ф14-9 решение 7: `tls` — сведения о паре https,
    прочитанные `runner` один раз на старте (`None` — файлов нет, удалённый слушатель по http)."""

    port: int = 0
    listening: bool = False
    tls: TlsInfo | None = None
    enabled: bool = False
    hosts: tuple[str, ...] = ()
    host_set: frozenset[bytes] = frozenset()
    password_set: bool = False
    password_set_ts: int | None = None
    # Решение 4: попытки пароля сериализованы в процессе; `pw_waiting` — сколько ждут замка.
    pw_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pw_waiting: int = 0

    def apply(self, config: RemoteConfig) -> None:
        self.hosts = config.hosts
        self.host_set = frozenset(host.encode("ascii") for host in config.hosts)
        self.password_set = config.password is not None
        self.password_set_ts = config.password_set_ts
        # Последним: middleware смотрит на `enabled` — к моменту включения адреса уже на месте.
        self.enabled = config.enabled

    def load(self, conn) -> None:
        self.apply(read_config(conn))


# --- meta -------------------------------------------------------------------------------------------------


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def _meta_set(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM main.meta WHERE key = ?", (key,))
        return
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def read_config(conn) -> RemoteConfig:
    hosts_raw = _meta_get(conn, META_HOSTS)
    hosts: tuple[str, ...] = ()
    if hosts_raw:
        try:
            parsed = json.loads(hosts_raw)
        except ValueError:
            parsed = []
        if isinstance(parsed, list):
            hosts = tuple(h for h in parsed if isinstance(h, str) and _host_ok(h))
    return RemoteConfig(
        enabled=_meta_get(conn, META_ENABLED) == "1",
        hosts=hosts,
        password=_meta_get(conn, META_PASSWORD),
        password_set_ts=_int_or_none(_meta_get(conn, META_PASSWORD_TS)),
        failures=_int_or_none(_meta_get(conn, META_FAILURES)) or 0,
        block_until=_int_or_none(_meta_get(conn, META_BLOCK_UNTIL)),
    )


# --- адреса ------------------------------------------------------------------------------------------------


def _host_ok(host: str) -> bool:
    if not _HOST_RE.match(host):
        return False
    name, _, port = host.rpartition(":")
    if not 1 <= int(port) <= 65535:
        return False
    if name == "localhost" or name.endswith(".localhost") or name.startswith("127."):
        return False
    return ".." not in name


def normalize_hosts(hosts: list[str]) -> tuple[str, ...]:
    """1–5 адресов `host:port` в нижнем регистре; не `localhost`, не `127.*`, не `*.localhost`."""
    result: list[str] = []
    for raw in hosts:
        host = raw.strip().lower()
        if not _host_ok(host):
            raise RemoteConfigError("invalid host")
        if host not in result:
            result.append(host)
    if not 1 <= len(result) <= MAX_HOSTS:
        raise RemoteConfigError(f"hosts: from 1 to {MAX_HOSTS}")
    return tuple(result)


# --- пароль -------------------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    """Строка для `main.meta.remote_password`. Тяжёлая (~60 мс) — звать через `asyncio.to_thread`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**SCRYPT_LOG_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_LOG_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Сравнение — `hmac.compare_digest`. Параметры scrypt — из хранимой строки. Битая строка -> False."""
    try:
        scheme, log_n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        log_n_i, r_i, p_i = int(log_n), int(r), int(p)
    except ValueError:
        return False
    if not (1 <= log_n_i <= 20 and 1 <= r_i <= 16 and 1 <= p_i <= 4 and expected):
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**log_n_i,
            r=r_i,
            p=p_i,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, expected)


def check_password_length(password: str) -> None:
    if not PASSWORD_MIN_LEN <= len(password) <= PASSWORD_MAX_LEN:
        raise RemoteConfigError(f"password: from {PASSWORD_MIN_LEN} to {PASSWORD_MAX_LEN} characters")


# --- статус ------------------------------------------------------------------------------------------------


def failures_24h(conn, now_ms: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM main.audit WHERE action = ? AND result = 'invalid' AND ts > ?",
        (AUDIT_ACTION, now_ms - FAILURES_WINDOW_MS),
    ).fetchone()
    return int(row[0])


def status(conn, state: RemoteState, now_ms: int) -> dict:
    """Тело `GET /api/v1/remote` и `GET /internal/v1/admin/remote`."""
    config = read_config(conn)
    block_until = config.block_until if blocked_for_ms(config, now_ms) else None
    return {
        "enabled": config.enabled,
        "hosts": list(config.hosts),
        "password_set": config.password is not None,
        "password_set_ts": config.password_set_ts,
        "listening": state.listening,
        "failures_24h": failures_24h(conn, now_ms),
        "blocked_until": block_until,
        # Ф14-9 решение 7: `null` — http (пары нет); иначе срок, имена и ошибка (слушатель не поднят).
        "tls": state.tls.as_dict() if state.tls is not None else None,
    }


def blocked_for_ms(config: RemoteConfig, now_ms: int) -> int:
    """Сколько ещё длится блок (0 — не заблокировано)."""
    if config.block_until is None or config.block_until <= now_ms:
        return 0
    return config.block_until - now_ms


# --- запись (внутри Db.write) -------------------------------------------------------------------------------


def _audit(conn, *, now_ms: int, action: str, result: str, detail: dict | None = None, token_id=None) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        actor="auth",
        api_token_id=token_id,
        client_name=None,
        request_id=secrets.token_hex(16),
        action=action,
        result=result,
        detail=detail,
    )


def update_config(
    conn, *, enabled: bool | None, hosts: tuple[str, ...] | None, now_ms: int, actor: str
) -> tuple[RemoteConfig, int]:
    """`PUT /remote`: адреса (уже нормализованные) и/или включение. Включить — только при заданном пароле и
    хотя бы одном адресе (иначе `RemoteConfigError`). Выключение отзывает все удалённые сессии.
    Возвращает `(новая конфигурация, отозвано)`."""
    current = read_config(conn)
    new_hosts = hosts if hosts is not None else current.hosts
    new_enabled = current.enabled if enabled is None else enabled
    if new_enabled and (current.password is None or not new_hosts):
        raise RemoteConfigError("password and at least one host required to enable")
    if hosts is not None:
        _meta_set(conn, META_HOSTS, json.dumps(list(hosts)))
    revoked = 0
    if enabled is not None:
        _meta_set(conn, META_ENABLED, "1" if enabled else "0")
        if not enabled:
            revoked = security.revoke_ui_sessions_via(conn, via=security.VIA_REMOTE)
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=None,
        client_name=None,
        request_id=secrets.token_hex(16),
        action="remote_set",
        result="ok",
        detail={"enabled": new_enabled, "hosts": list(new_hosts), "revoked_remote": revoked},
    )
    return read_config(conn), revoked


def turn_off(conn, *, now_ms: int, actor: str) -> int:
    """`hub remote off`: выключить и отозвать удалённые сессии. Возвращает число отозванных."""
    _config, revoked = update_config(conn, enabled=False, hosts=None, now_ms=now_ms, actor=actor)
    return revoked


def set_password(conn, *, stored: str, now_ms: int) -> int:
    """`PUT /remote/password`: новая строка scrypt; смена пароля отзывает все удалённые сессии (локальные не
    трогает) и сбрасывает счётчик подбора. Возвращает число отозванных."""
    _meta_set(conn, META_PASSWORD, stored)
    _meta_set(conn, META_PASSWORD_TS, str(now_ms))
    _meta_set(conn, META_FAILURES, None)
    _meta_set(conn, META_BLOCK_UNTIL, None)
    revoked = security.revoke_ui_sessions_via(conn, via=security.VIA_REMOTE)
    write_audit(
        conn,
        now_ms=now_ms,
        actor="ui",
        api_token_id=None,
        client_name=None,
        request_id=secrets.token_hex(16),
        action="remote_password",
        result="ok",
        detail={"revoked_remote": revoked},
    )
    return revoked


def record_failure(conn, *, now_ms: int) -> int | None:
    """Неверный пароль: +1 к счётчику подряд; с пятой — блок `1 мин * 2^(n-5)` (до 60 мин). В audit —
    `invalid` и, если с этой попытки начался блок, одна строка `blocked` (П6). Возвращает `block_until`."""
    config = read_config(conn)
    failures = config.failures + 1
    _meta_set(conn, META_FAILURES, str(failures))
    _audit(conn, now_ms=now_ms, action=AUDIT_ACTION, result="invalid")
    if failures < FAIL_THRESHOLD:
        return None
    block_ms = min(BLOCK_BASE_MS * 2 ** min(failures - FAIL_THRESHOLD, 16), BLOCK_MAX_MS)
    block_until = now_ms + block_ms
    _meta_set(conn, META_BLOCK_UNTIL, str(block_until))
    _audit(conn, now_ms=now_ms, action=AUDIT_ACTION, result="blocked", detail={"block_s": block_ms // 1000})
    return block_until


@dataclass(frozen=True)
class PasswordLogin:
    outcome: str  # 'ok' | 'disabled' | 'changed'
    token: str | None = None
    token_id: int | None = None
    csrf: str | None = None
    expires_ts: int | None = None


def complete_password_login(
    conn, *, checked_password: str, now_ms: int, user_agent: str | None, prev_cookie: str | None
) -> PasswordLogin:
    """Верный пароль -> удалённая UI-сессия. П5: в той же транзакции перечитать, что доступ всё ещё включён и
    что хранимый пароль — ровно та строка, против которой считался scrypt (выключение или смена пароля между
    проверкой и записью не должны пропустить вход)."""
    config = read_config(conn)
    if not config.enabled:
        return PasswordLogin("disabled")
    if config.password is None or not hmac.compare_digest(config.password, checked_password):
        return PasswordLogin("changed")
    _meta_set(conn, META_FAILURES, None)
    _meta_set(conn, META_BLOCK_UNTIL, None)
    token, token_id, csrf, expires_ts = security.create_ui_session(
        conn, via=security.VIA_REMOTE, user_agent=user_agent, now_ms=now_ms, prev_cookie=prev_cookie
    )
    _audit(conn, now_ms=now_ms, action=AUDIT_ACTION, result="ok", token_id=token_id)
    return PasswordLogin("ok", token, token_id, csrf, expires_ts)
