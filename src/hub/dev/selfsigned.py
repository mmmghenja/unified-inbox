"""Самоподписанная пара https для стенда и тестов (Ф14-9, решение 9): свой маленький УЦ и сертификат
сервера на заданные имена (DNS и IP). Ядро этот модуль не импортирует — боевую пару кладёт certbot.

`write_pair(tls_dir, names)` пишет в `tls_dir` то же, что хук certbot (`fullchain.pem`, `privkey.pem` 0600),
и рядом `ca.pem` — для `curl --cacert` / `ssl.create_default_context(cafile=…)`.
"""

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_NAME = "ca.pem"


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def generate(
    names: list[str],
    *,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
) -> tuple[bytes, bytes, bytes]:
    """`(fullchain PEM, ключ сервера PEM, сертификат УЦ PEM)`. По умолчанию срок — 30 дней от «сейчас»."""
    now = datetime.datetime.now(datetime.UTC)
    not_before = not_before or now - datetime.timedelta(minutes=5)
    not_after = not_after or now + datetime.timedelta(days=30)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hub dev CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(_san(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    pem = serialization.Encoding.PEM
    ca_pem = ca_cert.public_bytes(pem)
    fullchain = cert.public_bytes(pem) + ca_pem
    key_pem = key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return fullchain, key_pem, ca_pem


def _write(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def write_pair(tls_dir: Path, names: list[str], **kwargs) -> Path:
    """Пара в `tls_dir` (каталог 0700, ключ 0600) плюс `ca.pem`. Возвращает путь к `ca.pem`."""
    fullchain, key_pem, ca_pem = generate(names, **kwargs)
    tls_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(tls_dir / "privkey.pem", key_pem, 0o600)
    _write(tls_dir / "fullchain.pem", fullchain, 0o600)
    _write(tls_dir / CA_NAME, ca_pem, 0o644)
    return tls_dir / CA_NAME
