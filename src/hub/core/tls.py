"""https для удалённого слушателя (Ф14-9, docs/architecture.md–2, 7).

TLS кончается в ядре, только на удалённом слушателе (туннель с телефона): локальный `127.0.0.1:8787`
остаётся http. Пара файлов — `<config_dir>/tls/fullchain.pem` и `<config_dir>/tls/privkey.pem`; ядро читает
их ТОЛЬКО на старте (продление сертификата = перезапуск ядра из хука certbot, решение 3):

- обоих файлов нет -> `(None, None)`: удалённый слушатель по http, как до Ф14-9;
- оба есть и грузятся -> `(контекст, сведения)`: удалённый слушатель только https;
- есть, но не грузятся -> `(None, сведения с error)`: `runner` удалённый слушатель НЕ поднимает (тихого отката
  на http нет — телефон ждёт https, откат спрятал бы поломку).

Просроченный сертификат ставится всё равно (браузер предупредит) — срок виден в статусе и в `hub watch`.
Секретов в тексте ошибки нет: только имя файла и причина OpenSSL/ОС.
"""

import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509

TLS_DIR_NAME = "tls"
CERT_NAME = "fullchain.pem"
KEY_NAME = "privkey.pem"


@dataclass(frozen=True)
class TlsInfo:
    """Сведения о паре для статуса (`remote.status`, `/admin/watch`). `error` — пара не поднялась."""

    not_after_ms: int | None
    names: tuple[str, ...]
    error: str | None

    def as_dict(self) -> dict:
        return {"not_after_ms": self.not_after_ms, "names": list(self.names), "error": self.error}


def tls_dir(config_dir: Path) -> Path:
    return config_dir / TLS_DIR_NAME


def _failed(reason: str) -> tuple[None, TlsInfo]:
    return None, TlsInfo(not_after_ms=None, names=(), error=reason)


def _ssl_reason(exc: ssl.SSLError) -> str:
    reason = getattr(exc, "reason", None)
    if reason == "KEY_VALUES_MISMATCH":
        return "ключ не от этого сертификата"
    return f"сертификат или ключ не загружаются ({reason or exc.__class__.__name__})"


def _cert_info(cert_path: Path) -> TlsInfo:
    """Срок и имена (SAN) первого сертификата цепочки. Имена — DNS и IP, в нижнем регистре."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names: list[str] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        san = None
    if san is not None:
        names += [name.lower() for name in san.get_values_for_type(x509.DNSName)]
        names += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    not_after_ms = int(cert.not_valid_after_utc.timestamp() * 1000)
    return TlsInfo(not_after_ms=not_after_ms, names=tuple(names), error=None)


def load(config_dir: Path) -> tuple[ssl.SSLContext | None, TlsInfo | None]:
    """Прочитать пару из `<config_dir>/tls/` (Ф14-9, решение 2). Не бросает: любая беда -> `error`."""
    directory = tls_dir(config_dir)
    cert_path = directory / CERT_NAME
    key_path = directory / KEY_NAME
    cert_exists = cert_path.exists()
    key_exists = key_path.exists()
    if not cert_exists and not key_exists:
        return None, None
    if not cert_exists:
        return _failed(f"нет файла {CERT_NAME}")
    if not key_exists:
        return _failed(f"нет файла {KEY_NAME}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    except ssl.SSLError as exc:
        return _failed(_ssl_reason(exc))
    except OSError as exc:
        return _failed(f"файлы не читаются ({exc.strerror or exc.__class__.__name__})")
    try:
        info = _cert_info(cert_path)
    except (OSError, ValueError) as exc:
        return _failed(f"сертификат не разбирается ({exc.__class__.__name__})")
    return context, info
