"""Ф41-8: каталог ролей хаба (`hub.roles`)."""

import pytest

from hub import roles

LIMIT = 6000


def test_catalog_reads_roles_and_common():
    want = {"duty", "programmer", "marketer", "researcher", "strategist", "product", "network"}
    assert {r.key for r in roles.catalog()} == want
    assert roles.common_text()
    for r in roles.catalog():
        # Ф48-3: встроенных текстов ролей нет — в файле роли только шапка.
        assert r.title and r.names and r.sub in ("direct", "project") and r.text == ""


@pytest.mark.parametrize(
    ("given", "key"),
    [
        ("программист", "programmer"),
        ("Программист 2", "programmer"),
        ("РАЗРАБОТЧИК", "programmer"),
        ("дежурный", "duty"),
        ("Дежурная 3", "duty"),
        ("маркетолог", "marketer"),
        ("Сетевик 2", "network"),
    ],
)
def test_find_by_names_number_case(given, key):
    found = roles.find(given)
    assert found is not None and found.key == key


def test_yo_equals_ye():
    assert roles._norm("Ёж") == roles._norm("еж")


@pytest.mark.parametrize("given", ["тестер", "оркестратор", "", "   ", "2", None])
def test_find_unknown_is_none(given):
    assert roles.find(given) is None


def test_prompt_common_only_for_none_and_unknown():
    assert roles.prompt(None) == roles.common_text()
    assert roles.prompt("тестер") == roles.common_text()


def test_prompt_of_catalog_role_is_common_only():
    """Ф48-3: текст роли пуст — `prompt` отдаёт только общий текст, без хвоста из пустых строк."""
    assert roles.prompt("программист 2") == roles.common_text()


def test_prompt_common_plus_role_text(monkeypatch):
    """Механика «общий + текст роли» жива для роли с непустым текстом (каталог подменён)."""
    role = roles._parse("<!-- key: x; names: икс -->\nтекст роли икс\n")
    monkeypatch.setattr(roles, "_load", lambda: ("ОБЩИЙ", (role,)))
    assert roles.prompt("Икс 2") == "ОБЩИЙ\n\nтекст роли икс"


def test_limits():
    assert len(roles.common_text()) <= LIMIT
    for r in roles.catalog():
        assert len(r.text) <= LIMIT, r.key


def test_header_not_in_output():
    for r in roles.catalog():
        assert "<!--" not in r.text
    assert "<!--" not in roles.prompt("программист")


# --- Ф48-3: файл роли из проекта ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "want"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("ROLE.md", "ROLE.md"),
        ("  docs/marketing/researcher/ROLE.md ", "docs/marketing/researcher/ROLE.md"),
        ("docs/Роль исследователя.MD", "docs/Роль исследователя.MD"),
        ("a" * 297 + ".md", "a" * 297 + ".md"),
    ],
)
def test_file_form_accepts(given, want):
    assert roles.file_form(given) == want


@pytest.mark.parametrize(
    "given",
    [
        "/etc/passwd.md",
        "~/ROLE.md",
        "~user/ROLE.md",
        "../ROLE.md",
        "docs/../../ROLE.md",
        "docs/..",
        "ROLE.txt",
        "ROLE",
        ".md",
        "docs/.md",
        "a" * 298 + ".md",
        "docs\\ROLE.md",
        "docs/RO\nLE.md",
        "docs/RO\x00LE.md",
        5,
    ],
)
def test_file_form_refuses(given):
    with pytest.raises(ValueError, match="role_file"):
        roles.file_form(given)


def test_read_file_verbatim(tmp_path):
    body = "\n# Роль\n\n  отступ и хвост  \n\n"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROLE.md").write_text(body, encoding="utf-8")
    assert roles.read_file(tmp_path, "docs/ROLE.md") == roles.RoleFile(body, None)
    assert roles.read_file(str(tmp_path), "docs/ROLE.md").text == body
    assert roles.file_block("docs/ROLE.md", body) == f"Инструкция роли (файл проекта docs/ROLE.md):\n{body}"


def test_read_file_bad_utf8_is_replaced(tmp_path):
    (tmp_path / "R.md").write_bytes(b"ok \xff\xfe end")
    assert roles.read_file(tmp_path, "R.md").text == "ok �� end"


def test_read_file_size_limit(tmp_path):
    (tmp_path / "ok.md").write_bytes(b"a" * roles.FILE_MAX_BYTES)
    (tmp_path / "big.md").write_bytes(b"a" * (roles.FILE_MAX_BYTES + 1))
    assert roles.read_file(tmp_path, "ok.md").reason is None
    big = roles.read_file(tmp_path, "big.md")
    assert big.text is None and "больше 64 КиБ" in big.reason


def test_read_file_stays_inside_workdir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("СЕКРЕТ", encoding="utf-8")
    (work / "inner.md").write_text("внутри", encoding="utf-8")
    (work / "out.md").symlink_to(outside)  # симлинк на файл снаружи
    (work / "dir").symlink_to(tmp_path)  # симлинк на папку снаружи
    (work / "in.md").symlink_to(work / "inner.md")  # симлинк внутри папки — можно
    for path in ("out.md", "dir/secret.md"):
        got = roles.read_file(work, path)
        assert got == roles.RoleFile(None, "путь выходит за рабочую папку проекта"), path
    for path in ("../secret.md", str(outside), "~/secret.md"):
        got = roles.read_file(work, path)
        assert got.text is None and got.reason.startswith("role_file:"), path
    assert roles.read_file(work, "in.md").text == "внутри"
    # Рабочая папка сама задана через симлинк — граница считается по настоящему пути.
    (tmp_path / "link").symlink_to(work)
    assert roles.read_file(tmp_path / "link", "inner.md").text == "внутри"
    assert roles.read_file(tmp_path / "link", "out.md").text is None


def test_read_file_reasons(tmp_path):
    (tmp_path / "empty.md").write_text(" \n", encoding="utf-8")
    (tmp_path / "dir.md").mkdir()
    (tmp_path / "loop.md").symlink_to(tmp_path / "loop.md")
    assert roles.read_file(tmp_path, "нет.md") == roles.RoleFile(None, "нет файла")
    assert roles.read_file(tmp_path, "empty.md") == roles.RoleFile(None, "файл пуст")
    assert roles.read_file(tmp_path, "dir.md") == roles.RoleFile(None, "это не обычный файл")
    assert roles.read_file(tmp_path, None) == roles.RoleFile(None, "файл роли не задан")
    assert roles.read_file(None, "R.md") == roles.RoleFile(None, "у проекта нет рабочей папки")
    assert roles.read_file("", "R.md").text is None
    loop = roles.read_file(tmp_path, "loop.md")
    assert loop.text is None and loop.reason
