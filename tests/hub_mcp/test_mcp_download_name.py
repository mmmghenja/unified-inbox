"""Ф22-1 C2 (docs/architecture.md и поправки П5, П7, П8): имя файла на диске из
имени отправителя. Итог всегда `<media_id>-…` (или `<media_id>.bin` для имён, похожих на секрет по встроенному
списку гарда дежурной), без `/` и `\\`, не длиннее 200 байт UTF-8 вместе с префиксом и не длиннее 120 символов
без него; расширения кода получают `.txt`."""

import unicodedata

import pytest

from hub.core import media as core_media
from hub.engine import secret_names as guard
from hub.mcp_server import download

MID = 57


def _name(raw, detected=None, media_id=MID):
    return download.clean_name(media_id, raw, detected)


def _assert_safe(result, media_id=MID):
    name = result.name
    assert name.startswith(f"{media_id}-") or name == f"{media_id}.bin"
    assert "/" not in name and "\\" not in name and "\x00" not in name
    assert name not in (".", "..") and not name.startswith(".")
    assert len(name.encode("utf-8")) <= download.NAME_MAX_BYTES
    assert not any(unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp") for ch in name)
    assert not guard.is_secret_name(name)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Отчёт за март.docx", "57-Отчёт за март.docx"),
        ("../../etc/passwd", "57-passwd"),
        ("/etc/passwd", "57-passwd"),
        ("C:\\Users\\alisa\\табель.xlsx", "57-табель.xlsx"),
        ("..\\..\\boot.ini", "57-boot.ini"),
        ("строка\nвторая\t.pdf", "57-строкавторая.pdf"),
        ('a<b>c|d?e*f:g"h.txt', "57-a_b_c_d_e_f_g_h.txt"),
        ("  ..скрытый..  ", "57-скрытый"),
        ("архив.tar.gz", "57-архив.tar.gz"),
        ("Счёт (1), итог+НДС.pdf", "57-Счёт (1), итог+НДС.pdf"),
    ],
)
def test_c2_paths_and_symbols(raw, expected):
    result = _name(raw)
    assert result == download.CleanName(expected, False)
    _assert_safe(result)


def test_c2_rtl_override_removed():
    # «отчёт‮fdp.exe» выглядит как «отчётexe.pdf»: невидимый символ направления (Cf) убирается
    raw = "отчёт" + chr(0x202E) + "fdp.exe"
    result = _name(raw)
    assert result.name == "57-отчётfdp.exe"
    assert chr(0x202E) not in result.name
    _assert_safe(result)
    zero_width = _name("счёт" + chr(0x200B) + ".pdf")
    assert zero_width.name == "57-счёт.pdf"


def test_c2_nfc():
    decomposed = unicodedata.normalize("NFD", "Ёлка.pdf")
    assert _name(decomposed).name == "57-Ёлка.pdf"


@pytest.mark.parametrize("raw", [None, "", "   ", ".", "..", "....", "/", "\\", "../..", "\x00\x01"])
def test_c2_empty_and_dots(raw):
    assert _name(raw) == download.CleanName("57-file", False)
    assert _name(raw, "pdf") == download.CleanName("57-file.pdf", False)
    assert _name(raw, "jpeg").name == "57-file.jpg"
    assert _name(raw, "other").name == "57-file"


def test_c2_extension_from_detected_only_when_missing():
    assert _name("скан", "pdf").name == "57-скан.pdf"
    assert _name("скан.docx", "pdf").name == "57-скан.docx"  # своё расширение не подменяется


@pytest.mark.parametrize(
    "raw",
    [".env", ".env.local", "prod.env", "id_rsa", "id_ed25519.pub", "secret.pdf", "My_Secrets.xlsx",
     "server.key", "cert.pem", "api.token", "aws_credentials.csv", ".git", ".claude", ".netrc",
     "../.ssh/id_rsa", "store.p12"],
)  # fmt: skip
def test_c2_secret_like_names_become_bin(raw):
    result = _name(raw, "pdf")
    assert result == download.CleanName("57.bin", True)
    _assert_safe(result)


def test_c2_long_name_keeps_extension_and_limits():
    raw = "очень длинное название документа " * 20 + ".xlsx"
    result = _name(raw)
    assert result.name.endswith(".xlsx") and result.name.startswith("57-очень длинное")
    assert len(result.name) - len("57-") <= download.NAME_MAX_CHARS
    _assert_safe(result)


def test_p5_long_cjk_name_fits_200_bytes_on_char_boundary():
    raw = "報告書" * 60 + ".pdf"  # 3 байта на символ: 120 символов — уже 360 байт
    result = _name(raw, media_id=2**40 + 12345)
    encoded = result.name.encode("utf-8")
    assert len(encoded) <= download.NAME_MAX_BYTES
    assert len(encoded) > download.NAME_MAX_BYTES - 4  # обрезано по символу, а не с запасом
    encoded.decode("utf-8")  # граница символа
    assert result.name.startswith(f"{2**40 + 12345}-報告書") and result.name.endswith(".pdf")
    _assert_safe(result, media_id=2**40 + 12345)


def test_c2_extension_longer_than_16_is_part_of_stem():
    raw = "data." + "x" * 17
    assert _name(raw).name == "57-data." + "x" * 17
    long_tail = "a" * 200 + "." + "b" * 17
    result = _name(long_tail)
    assert len(result.name) - 3 == download.NAME_MAX_CHARS
    assert not result.name.endswith("b" * 17)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x_test.py", "57-x_test.py.txt"),
        ("conftest.py", "57-conftest.py.txt"),
        ("x.test.mts", "57-x.test.mts.txt"),
        ("x.spec.cts", "57-x.spec.cts.txt"),
        ("build.SH", "57-build.SH.txt"),
        ("index.mjs", "57-index.mjs.txt"),
        ("run.ps1", "57-run.ps1.txt"),
        ("main.c", "57-main.c.txt"),
        ("notes.txt", "57-notes.txt"),
        ("table.csv", "57-table.csv"),
    ],
)
def test_p7_code_extensions_get_txt(raw, expected):
    assert _name(raw).name == expected


@pytest.mark.parametrize("raw", ["CLAUDE.md", "AGENTS.md", ".gitignore", "conftest.py", "pyproject.toml"])
def test_p7_prefix_never_gives_special_names(raw):
    result = _name(raw)
    assert result.name.startswith("57-") or result.name == "57.bin"
    assert result.name not in ("CLAUDE.md", "AGENTS.md", ".gitignore", "conftest.py", "pyproject.toml")


def test_guard_public_secret_name_uses_builtin_list():
    assert guard.is_secret_name(".ENV") and guard.is_secret_name("id_rsa")
    assert not guard.is_secret_name("57-отчёт.pdf")
    assert not guard.is_secret_name(download.DIR_NAME)


def test_limits_match_core():
    assert download.MAX_BYTES == core_media.CLICK_MAX_BYTES
    assert download.MIN_FREE_BYTES == core_media.MIN_FREE_BYTES
    assert set(download.DETECTED_EXT) == set(core_media.DETECTED) - {"other"}
