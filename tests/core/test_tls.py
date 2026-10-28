"""Ф14-9, решение 2 (docs/architecture.md): `hub.core.tls.load` — пары нет -> http; пара
грузится -> контекст TLS 1.2+ и сведения (срок, имена из SAN); битая пара -> ошибка без контекста.
Просроченный сертификат грузится (браузер предупредит, срок виден в статусе)."""

import datetime
import os
import ssl

from hub.core import tls as tls_mod
from hub.dev.selfsigned import generate, write_pair


def test_no_files_is_http(tmp_path) -> None:
    assert tls_mod.load(tmp_path) == (None, None)
    (tmp_path / "tls").mkdir()
    assert tls_mod.load(tmp_path) == (None, None)


def test_valid_pair_gives_context_and_info(tmp_path) -> None:
    not_after = datetime.datetime(2026, 12, 27, 12, 0, tzinfo=datetime.UTC)
    write_pair(tmp_path / "tls", ["hub.test", "127.0.0.1"], not_after=not_after)
    context, info = tls_mod.load(tmp_path)
    assert isinstance(context, ssl.SSLContext)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert info is not None
    assert info.error is None
    assert info.names == ("hub.test", "127.0.0.1")
    assert info.not_after_ms == int(not_after.timestamp() * 1000)
    assert info.as_dict() == {
        "not_after_ms": int(not_after.timestamp() * 1000),
        "names": ["hub.test", "127.0.0.1"],
        "error": None,
    }


def test_expired_certificate_still_loads(tmp_path) -> None:
    now = datetime.datetime.now(datetime.UTC)
    write_pair(
        tmp_path / "tls",
        ["hub.test"],
        not_before=now - datetime.timedelta(days=100),
        not_after=now - datetime.timedelta(days=1),
    )
    context, info = tls_mod.load(tmp_path)
    assert context is not None
    assert info is not None and info.error is None
    assert info.not_after_ms < now.timestamp() * 1000


def test_only_one_file_is_error(tmp_path) -> None:
    write_pair(tmp_path / "tls", ["hub.test"])
    os.unlink(tmp_path / "tls" / "privkey.pem")
    context, info = tls_mod.load(tmp_path)
    assert context is None
    assert info is not None and info.error == "нет файла privkey.pem"
    assert info.not_after_ms is None and info.names == ()

    write_pair(tmp_path / "tls", ["hub.test"])
    os.unlink(tmp_path / "tls" / "fullchain.pem")
    context, info = tls_mod.load(tmp_path)
    assert context is None
    assert info is not None and info.error == "нет файла fullchain.pem"


def test_key_from_other_certificate_is_error(tmp_path) -> None:
    write_pair(tmp_path / "tls", ["hub.test"])
    _other_chain, other_key, _ca = generate(["hub.test"])
    (tmp_path / "tls" / "privkey.pem").write_bytes(other_key)
    context, info = tls_mod.load(tmp_path)
    assert context is None
    assert info is not None and info.error == "ключ не от этого сертификата"


def test_garbage_is_error(tmp_path) -> None:
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "fullchain.pem").write_text("не PEM\n")
    (tls_dir / "privkey.pem").write_text("тоже не PEM\n")
    context, info = tls_mod.load(tmp_path)
    assert context is None
    assert info is not None and info.error is not None
    assert info.error.startswith("сертификат или ключ не загружаются")


def test_unreadable_is_error(tmp_path) -> None:
    write_pair(tmp_path / "tls", ["hub.test"])
    key = tmp_path / "tls" / "privkey.pem"
    key.chmod(0o000)
    try:
        if os.access(key, os.R_OK):  # root читает всё — проверять нечего
            return
        context, info = tls_mod.load(tmp_path)
    finally:
        key.chmod(0o600)
    assert context is None
    assert info is not None and info.error is not None
    assert info.error.startswith("файлы не читаются")
