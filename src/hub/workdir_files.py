"""Каталог `hub-files` в рабочей папке проекта: безопасное открытие и запись файлов, чьи имена и содержимое
пришли извне. Общий код MCP-сервера сессии (`hub_download`, Ф22-1 — `mcp_server/download.py`) и хоста
постоянных сессий (файлы, приложенные владельцем к реплике, Ф41-3в — `work/say_files.py`).

Вынесено из `mcp_server/download.py` без изменения поведения: хост не должен зависеть от MCP-сервера.

Граница безопасности: вся запись — через дескриптор каталога (`dir_fd`, `O_NOFOLLOW`, `O_EXCL`). Подменённый
симлинком `hub-files` — отказ; по симлинку с конечным именем ничего не пишется. Файл не открывается на чтение,
не распаковывается и не запускается; права 0600. В каталоге — `.gitignore` с `*`.

Модуль не импортирует ни ядро, ни MCP-сервер, ни хост."""

import contextlib
import os
import re
import stat
import time
import unicodedata

DIR_NAME = "hub-files"
GITIGNORE = ".gitignore"
PART_MAX_AGE_S = 3600  # остатки `.part` старше часа
CHUNK_WRITE = 1024 * 1024
NAME_MAX_CHARS = 120
NAME_MAX_BYTES = 200
EXT_MAX = 16
SUFFIX_TRIES = 200
DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
ALLOWED_PUNCT = frozenset(" .-_()+,")
_PART_RE = re.compile(r"^\.\d+\..+\.part$")
SUBSTITUTED_TEXT = "каталог hub-files подменён (симлинк или не каталог) — скажи владельцу"


class Refused(Exception):
    """Записать нельзя — текст причины (без данных отправителя)."""


def keep_char(ch: str) -> bool:
    """Буквы, цифры и немного пунктуации; прочее в имени на диске заменяется на `_`."""
    return unicodedata.category(ch)[0] in ("L", "N") or ch in ALLOWED_PUNCT


def plain_name(raw: str | None) -> str:
    """Имя файла на диске из чужого имени, без префикса: базовое имя (оба вида разделителей), без управляющих
    и невидимых символов, только буквы, цифры и ` .-_()+` (прочее, включая запятую, — `_`), без точек и
    пробелов по краям (имя не начинается с точки), не длиннее 120 символов и 200 байт UTF-8 (режется основа,
    расширение до 16 символов остаётся); пустое — `file`."""
    text = unicodedata.normalize("NFC", raw or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if unicodedata.category(ch) not in DROP_CATEGORIES)
    cleaned = "".join(ch if keep_char(ch) and ch != "," else "_" for ch in text).strip(". ")
    stem, ext = cleaned, ""
    if "." in cleaned:
        head, tail = cleaned.rsplit(".", 1)
        if head.strip(". ") and 1 <= len(tail) <= EXT_MAX:
            stem, ext = head.strip(". "), tail
    suffix = f".{ext}" if ext else ""
    while stem and (
        len(stem) + len(suffix) > NAME_MAX_CHARS or len((stem + suffix).encode("utf-8")) > NAME_MAX_BYTES
    ):
        stem = stem[:-1]
    stem = stem.strip(". ")
    return (stem or "file") + suffix


def fd_path(fd: int) -> str:
    return os.path.realpath(f"/proc/self/fd/{fd}")


def open_root(path: str) -> tuple[int, str]:
    real = os.path.realpath(path)
    if not os.path.isabs(real) or not os.path.isdir(real):
        raise Refused("рабочая папка проекта не найдена — скажи владельцу")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise Refused("рабочая папка проекта не открывается — скажи владельцу") from exc
    return fd, real


def sub_dir(parent_fd: int, name: str, expected: str, *, real=fd_path) -> int:
    """Подкаталог `name` (0700, если его нет) по дескриптору родителя: без следования по ссылке, обязан быть
    каталогом, и `realpath` открытого дескриптора (`real`) — ровно `expected`."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise Refused("не удалось создать каталог hub-files — скажи владельцу") from exc
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as exc:
        raise Refused(SUBSTITUTED_TEXT) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or real(fd) != expected:
            raise Refused(SUBSTITUTED_TEXT)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view[:CHUNK_WRITE])
        view = view[written:]


class Target:
    """Открытый каталог назначения: `fd` и `path` (для ответа). Закрыть — `close()`."""

    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def file_path(self, name: str) -> str:
        return os.path.join(self.path, name)

    def ensure_gitignore(self) -> None:
        """`.gitignore` с `*`: нет — создать (`O_EXCL|O_NOFOLLOW`), есть (или симлинк) — не трогать."""
        try:
            fd = os.open(
                GITIGNORE,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self.fd,
            )
        except FileExistsError:
            return
        try:
            write_all(fd, b"*\n")
        finally:
            os.close(fd)

    def cleanup_parts(self, now_s: float | None = None) -> None:
        """Свои остатки `.<id>.<…>.part` старше часа — обычные файлы, без следования по ссылкам."""
        now_s = time.time() if now_s is None else now_s
        try:
            names = os.listdir(self.fd)
        except OSError:
            return
        for name in names:
            if _PART_RE.match(name) is None:
                continue
            with contextlib.suppress(OSError):
                st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and st.st_mtime < now_s - PART_MAX_AGE_S:
                    os.unlink(name, dir_fd=self.fd)

    def existing_size(self, name: str) -> int | None:
        """Размер уже лежащего обычного файла с этим именем (без следования по ссылке); иначе `None`."""
        try:
            st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError:
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else None

    def free_bytes(self) -> int:
        st = os.fstatvfs(self.fd)
        return st.f_bavail * st.f_frsize

    def write_new(self, name: str, data: bytes) -> str:
        """Ф41-3в: новый файл с содержимым `data` (0600). Создаётся только `O_EXCL|O_NOFOLLOW`: существующее
        не перезаписывается, по симлинку не пишется. Имя занято (файлом, каталогом или симлинком) — суффикс
        `-2`, `-3`… перед расширением. `name` обязано быть простым именем (без разделителей, не с точки).
        Возвращает имя, под которым файл лёг. Сбой записи — недописанный файл удаляется."""
        if not name or name != os.path.basename(name) or name.startswith(".") or "\x00" in name:
            raise Refused("недопустимое имя файла")
        stem, dot, ext = name.rpartition(".")
        if not stem:
            stem, dot, ext = name, "", ""
        for n in range(1, SUFFIX_TRIES + 1):
            candidate = name if n == 1 else f"{stem}-{n}{dot}{ext}"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=self.fd,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise Refused("файл в hub-files не создаётся") from exc
            try:
                write_all(fd, data)
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(candidate, dir_fd=self.fd)
                raise
            os.close(fd)
            return candidate
        raise Refused("в hub-files слишком много файлов с таким именем")


def open_workdir(workdir: str) -> Target:
    """`<realpath(workdir)>/hub-files/` с `.gitignore` (`*`). Подменённый каталог — `Refused`. Пустой или
    относительный путь — отказ: `realpath("")` — текущий каталог процесса, писать туда нельзя."""
    if not workdir or not os.path.isabs(workdir):
        raise Refused("рабочая папка проекта не задана")
    root_fd, root = open_root(workdir)
    try:
        path = os.path.join(root, DIR_NAME)
        fd = sub_dir(root_fd, DIR_NAME, path)
    finally:
        os.close(root_fd)
    target = Target(fd, path)
    try:
        target.ensure_gitignore()
    except BaseException:
        target.close()
        raise
    return target
