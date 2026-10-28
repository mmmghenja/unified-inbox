"""Скачивание вложения сессией к себе на диск (Ф22-1, `docs/architecture.md`, C2, C3 и
поправки П1, П4–П8). Здесь — каталог, имя и запись; ход (meta, запрос загрузки, ожидание) и ответ — в
`server.py` (`Hub.download`).

Граница безопасности: имя и содержимое файла — от посторонних людей. Сессия не передаёт ни пути, ни имени:
каталог — по проекту из ядра, имя — из `meta.name`, очищенное здесь. Вся запись — через дескриптор каталога
(`dir_fd`, `O_NOFOLLOW`, `O_EXCL`): подменённый симлинком `hub-files` — отказ, симлинк с конечным именем
заменяется сам (`os.replace`), по нему ничего не пишется. Файл не открывается на чтение, не распаковывается и
не запускается; права 0600.

Префикс `<media_id>-` — инвариант безопасности (П7): имя никогда не начинается с точки и не совпадает с
`CLAUDE.md`, `AGENTS.md`, `conftest.py`, `.gitignore`; файлам с расширениями кода дописывается `.txt`, чтобы
их не подхватили тест-раннеры и сборщики.

Модуль не импортирует ядро (MCP-процесс сессии живёт отдельно): потолки продублированы, равенство с
`hub.core.media` проверяет тест.

Ф41-3в: открытие каталога и запись по дескриптору вынесены в `hub.workdir_files` (ими же пользуется хост
постоянных сессий); здесь — прежние имена, поведение `hub_download` не менялось.
"""

import contextlib
import os
import secrets
import unicodedata
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from hub import workdir_files
from hub.engine import secret_names as names_guard

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_BYTES = 300 * MIB  # = `hub.core.media.CLICK_MAX_BYTES`: больше хаб не качает
MIN_FREE_BYTES = 5 * GIB  # = `hub.core.media.MIN_FREE_BYTES` (П6)
DIR_NAME = workdir_files.DIR_NAME
UNSORTED_DIR = "unsorted"  # П4: беседа без проекта
GITIGNORE = workdir_files.GITIGNORE
NAME_MAX_CHARS = 120  # C2: имя без префикса
NAME_MAX_BYTES = 200  # П5: всё имя вместе с `<id>-`, UTF-8
EXT_MAX = 16
PART_MAX_AGE_S = workdir_files.PART_MAX_AGE_S  # П8: остатки `.part` старше часа
# П7: расширения кода и скриптов — к имени дописывается `.txt`.
CODE_EXTS = frozenset(
    {
        "py",
        "pyw",
        "js",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "jsx",
        "go",
        "rs",
        "sh",
        "bash",
        "zsh",
        "rb",
        "php",
        "pl",
        "ps1",
        "bat",
        "cmd",
        "java",
        "kt",
        "cs",
        "c",
        "cpp",
        "h",
        "swift",
        "lua",
        # Ревью Ф22-1: маска vitest `*.{test,spec}.?(c|m)[jt]s?(x)`, deno, `node --test`; блокноты и SFC.
        "mts",
        "cts",
        "ipynb",
        "vue",
        "svelte",
    }
)
# Нет расширения в имени — по сигнатуре байт (`media.detected`, только узнанные типы).
DETECTED_EXT = {
    "jpeg": "jpg",
    "png": "png",
    "webp": "webp",
    "gif": "gif",
    "mp4": "mp4",
    "webm": "webm",
    "ogg": "ogg",
    "mp3": "mp3",
    "m4a": "m4a",
    "pdf": "pdf",
}
_DROP_CATEGORIES = workdir_files.DROP_CATEGORIES
SUBSTITUTED_TEXT = workdir_files.SUBSTITUTED_TEXT

# Скачать нельзя — текст причины для сессии (без данных отправителя). Тот же класс, что у общего модуля.
DownloadRefused = workdir_files.Refused


class CleanName(NamedTuple):
    name: str  # имя на диске: `<id>-…` или `<id>.bin`
    secret: bool  # исходное имя похоже на секрет по встроенному списку гарда


# --- C2: имя ---------------------------------------------------------------------------------------------


_keep = workdir_files.keep_char


def _fit(prefix: str, stem: str, suffix: str) -> str:
    """Стем режется посимвольно (по границе символа), пока имя без префикса ≤ 120 символов и всё имя
    ≤ 200 байт UTF-8."""
    while stem and (
        len(stem) + len(suffix) > NAME_MAX_CHARS
        or len((prefix + stem + suffix).encode("utf-8")) > NAME_MAX_BYTES
    ):
        stem = stem[:-1]
    return stem


def clean_name(media_id: int, raw: str | None, detected: str | None) -> CleanName:
    """C2 + П5, П7: имя на диске из имени отправителя. Итог — всегда `<media_id>-<имя>` (или `<media_id>.bin`,
    если имя похоже на секрет по встроенному списку гарда дежурной), без `/` и `\\`."""
    prefix = f"{int(media_id)}-"
    text = unicodedata.normalize("NFC", raw or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    segment = "".join(ch for ch in text if unicodedata.category(ch) not in _DROP_CATEGORIES)
    cleaned = "".join(ch if _keep(ch) else "_" for ch in segment).strip(". ")

    stem, ext = cleaned, ""
    if "." in cleaned:
        head, tail = cleaned.rsplit(".", 1)
        if head and 1 <= len(tail) <= EXT_MAX:
            stem, ext = head, tail
    if not stem and not ext:
        stem = "file"
    if not ext:
        ext = DETECTED_EXT.get(detected or "", "")
    if ext.casefold() in CODE_EXTS:
        ext += ".txt"
    suffix = f".{ext}" if ext else ""
    stem = _fit(prefix, stem, suffix)
    final = prefix + stem + suffix
    if any(names_guard.is_secret_name(n) for n in (segment.strip(), cleaned, stem + suffix, final)):
        return CleanName(f"{int(media_id)}.bin", True)
    return CleanName(final, False)


# --- A + C3: каталог -------------------------------------------------------------------------------------


_open_root = workdir_files.open_root
Target = workdir_files.Target


def _fd_path(fd: int) -> str:
    return workdir_files.fd_path(fd)


def _sub_dir(parent_fd: int, name: str, expected: str) -> int:
    # `_fd_path` берётся из этого модуля в момент вызова (второй замок каталога проверяется тестом отдельно).
    return workdir_files.sub_dir(parent_fd, name, expected, real=lambda fd: _fd_path(fd))


def open_target(*, project_id: int | None, workdir: str | None, home: str) -> Target:
    """A + П4: `<realpath(workdir)>/hub-files/`; без рабочей папки — `~/hub-files/project-<id>/`; беседа без
    проекта — `~/hub-files/unsorted/`. Каждый уровень — `_sub_dir`; `.gitignore` с `*` в итоговом каталоге."""
    if workdir:
        root_fd, root = _open_root(workdir)
        try:
            path = os.path.join(root, DIR_NAME)
            fd = _sub_dir(root_fd, DIR_NAME, path)
        finally:
            os.close(root_fd)
    else:
        root_fd, root = _open_root(home)
        try:
            top_path = os.path.join(root, DIR_NAME)
            top_fd = _sub_dir(root_fd, DIR_NAME, top_path)
        finally:
            os.close(root_fd)
        leaf = UNSORTED_DIR if project_id is None else f"project-{int(project_id)}"
        try:
            path = os.path.join(top_path, leaf)
            fd = _sub_dir(top_fd, leaf, path)
        finally:
            os.close(top_fd)
    target = Target(fd, path)
    try:
        target.ensure_gitignore()
    except BaseException:
        target.close()
        raise
    return target


# --- C3: запись --------------------------------------------------------------------------------------------


_write_all = workdir_files.write_all


async def save(
    target: Target,
    *,
    media_id: int,
    name: str,
    size: int | None,
    fetch: Callable[[Callable[[bytes], None], int], Awaitable[int]],
) -> int:
    """Тело во временный `.<id>.<случайное>.part` (`O_EXCL|O_NOFOLLOW`, 0600) потоком из `fetch(sink,
    max_bytes)`, затем `os.replace` в `name` в том же каталоге. Потолок — `size` (фактический, П1) и 300 МиБ;
    получено не ровно `size` — отказ. Ошибка на любом шаге (и отмена) — временный файл удаляется."""
    limit = MAX_BYTES if size is None else min(int(size), MAX_BYTES)
    part = f".{int(media_id)}.{secrets.token_hex(8)}.part"
    pfd = os.open(
        part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=target.fd
    )
    done = False
    try:

        def sink(chunk: bytes) -> None:
            _write_all(pfd, chunk)

        got = await fetch(sink, limit)
        if got > limit or (size is not None and got != size):
            raise DownloadRefused("получено не столько байт, сколько в хабе — повтори hub_download")
        os.fsync(pfd)
        os.fchmod(pfd, 0o600)
        os.close(pfd)
        pfd = -1
        os.replace(part, name, src_dir_fd=target.fd, dst_dir_fd=target.fd)
        done = True
        return got
    finally:
        if pfd >= 0:
            os.close(pfd)
        if not done:
            with contextlib.suppress(OSError):
                os.unlink(part, dir_fd=target.fd)
