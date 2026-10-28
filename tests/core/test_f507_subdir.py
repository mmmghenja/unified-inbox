"""Ф50-7 (docs/architecture.md, Р3, П4, П6, П8): подпапка сессии хаба — форма пути,
настоящий путь на диске, проверка «в папке нет своих настроек Claude Code» и заголовок блока файла роли."""

import pytest

from hub import roles


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "proekty" / "z1").mkdir(parents=True)
    (root / "proekty" / "z 2 заказ").mkdir()
    return root


# --- форма --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", ".", "./", "./."])
def test_form_root_is_none(raw):
    assert roles.subdir_form(raw) is None


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("proekty/z1", "proekty/z1"),
        ("  proekty/z1/  ", "proekty/z1"),
        ("./proekty/./z1", "proekty/z1"),
        ("proekty/z 2 заказ", "proekty/z 2 заказ"),
    ],
)
def test_form_is_normalised(raw, clean):
    assert roles.subdir_form(raw) == clean


@pytest.mark.parametrize(
    "raw, word",
    [
        ("/abs/path", "без начального"),
        ("~/x", "без начального"),
        ("a/../b", "без «..»"),
        ("..", "без «..»"),
        ("a//b", "без пустых сегментов"),
        ("a\\b", "обратной косой черты"),
        ("a\nb", "управляющих символов"),
        ("x" * (roles.FILE_PATH_MAX + 1), "символов"),
        (".git/hooks", ".git"),
        ("a/.claude", ".claude"),
        ("a/.claude/agents", ".claude"),
        (5, "строка"),
    ],
)
def test_form_refuses(raw, word):
    with pytest.raises(ValueError) as err:
        roles.subdir_form(raw)
    assert str(err.value).startswith("subdir: ") and word in str(err.value)


def test_form_allows_names_that_only_look_like_service_ones():
    assert roles.subdir_form("a/.github/b.git") == "a/.github/b.git"


# --- диск ---------------------------------------------------------------------------------------------------


def test_path_is_the_real_folder_inside_the_project(project):
    assert roles.subdir_path(project, None) == str(project.resolve())
    assert roles.subdir_path(project, "proekty/z1") == str((project / "proekty/z1").resolve())
    assert roles.subdir_path(str(project), "proekty/z 2 заказ/") == str(project / "proekty" / "z 2 заказ")


def test_path_refuses_missing_file_and_no_workdir(project):
    (project / "proekty" / "plan.md").write_text("x")
    for sub, text in (
        ("proekty/net", "папки proekty/net нет в проекте"),
        ("proekty/plan.md", "proekty/plan.md в проекте — не папка"),
    ):
        with pytest.raises(ValueError) as err:
            roles.subdir_path(project, sub)
        assert str(err.value) == text
    with pytest.raises(ValueError) as err:
        roles.subdir_path(None, "proekty/z1")
    assert str(err.value) == "у проекта нет рабочей папки"


def test_path_follows_links_inside_and_refuses_links_outside(project, tmp_path):
    """П8: ссылка внутри проекта — настоящий путь цели; ссылка наружу — отказ."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "inside-link").symlink_to(project / "proekty" / "z1")
    (project / "out-link").symlink_to(outside)
    assert roles.subdir_path(project, "inside-link") == str(project / "proekty" / "z1")
    with pytest.raises(ValueError) as err:
        roles.subdir_path(project, "out-link")
    assert str(err.value) == "папка out-link выходит за рабочую папку проекта"
    # сосед с общим началом имени — не «внутри»
    (tmp_path / "project-2").mkdir()
    (project / "near").symlink_to(tmp_path / "project-2")
    with pytest.raises(ValueError):
        roles.subdir_path(project, "near")


# --- П4: свои настройки движка -------------------------------------------------------------------------


@pytest.mark.parametrize("name", roles.SUBDIR_SETTINGS)
def test_clean_finds_settings_in_the_folder_and_above_it(project, name):
    z1 = project / "proekty" / "z1"
    assert roles.subdir_clean(project, z1) is None
    for holder, rel in ((z1, f"proekty/z1/{name}"), (project / "proekty", f"proekty/{name}")):
        target = holder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
        assert roles.subdir_clean(project, z1) == rel
        with pytest.raises(ValueError) as err:
            roles.subdir_check(project, "proekty/z1")
        assert str(err.value) == f"в папке свои настройки движка: {rel}"
        target.unlink()
    assert roles.subdir_check(project, "proekty/z1") == str(z1)


def test_clean_ignores_the_project_root_other_files_and_foreign_folders(project, tmp_path):
    z1 = project / "proekty" / "z1"
    for name in roles.SUBDIR_SETTINGS:  # настройки корня проекта — обычное дело, не помеха
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    (z1 / ".claude" / "agents").mkdir(parents=True)  # каталог без файлов настроек — не помеха
    (z1 / ".claude" / "hub.md").write_text("x")
    (project / "proekty" / "z 2 заказ" / ".mcp.json").write_text("{}")  # сосед — не на пути
    assert roles.subdir_clean(project, z1) is None
    assert roles.subdir_clean(project, project) is None
    assert roles.subdir_clean(project, tmp_path) is None and roles.subdir_clean(None, z1) is None
    assert roles.subdir_check(project, None) == str(project)


def test_clean_sees_a_dangling_link_as_settings(project):
    z1 = project / "proekty" / "z1"
    (z1 / ".mcp.json").symlink_to(project / "net-takogo")
    assert roles.subdir_clean(project, z1) == "proekty/z1/.mcp.json"


# --- П6: заголовок блока файла роли -------------------------------------------------------------------------


def test_file_block_header_names_the_project_folder_only_for_a_subdir_session():
    assert roles.file_block("roli/a.md", "ТЕЛО") == "Инструкция роли (файл проекта roli/a.md):\nТЕЛО"
    assert roles.file_block("roli/a.md", "ТЕЛО", None) == roles.file_block("roli/a.md", "ТЕЛО")
    assert roles.file_block("roli/a.md", "ТЕЛО", "/work/p") == (
        "Инструкция роли (файл проекта roli/a.md; путь — от папки проекта /work/p):\nТЕЛО"
    )


def test_subdir_clean_sees_opencode_settings(tmp_path):
    """Настройки OpenCode в подпапке применяются, как и настройки Claude Code (проба 4) — тот же отказ."""
    from hub import roles

    for name in ("opencode.json", "opencode.jsonc"):
        root = tmp_path / name.replace(".", "_")
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / name).write_text("{}")
        assert roles.subdir_clean(root, root / "a" / "b") == f"a/{name}"
    root = tmp_path / "dir"
    (root / "a" / ".opencode").mkdir(parents=True)
    assert roles.subdir_clean(root, root / "a") == "a/.opencode"
