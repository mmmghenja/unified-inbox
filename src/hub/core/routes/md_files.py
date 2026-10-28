"""Просмотр MD-файлов в хабе.

`GET /files/md?path=…[&session_id=N][&project_id=N][&from=…]` — только кука интерфейса (`ui_only`). Отдаёт
`{path, text, size, mtime_ms}` файла `.md` / `.markdown` / `.txt` до 2 МиБ.

Путь — как его пишут сессии: абсолютный, `~/…`, `file://…`, в обратных кавычках, с хвостом `:строка`;
относительный ищется по очереди от папки файла `from` (ссылка внутри открытого файла), папки сессии хаба
(папка проекта + подпапка), папки проекта и, если не нашёлся, от папки любого проекта. Читается только внутри
домашнего каталога ядра, мимо каталогов данных и конфига хаба; ссылки разрешаются до проверки.
"""

import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from ..security import Principal
from .write import ui_only

public_router = APIRouter(tags=["md-files"])

MD_SUFFIXES = (".md", ".markdown", ".txt")
MD_MAX_BYTES = 2 * 1024 * 1024
_LINE_TAIL = re.compile(r"(?::\d+(?:-\d+)?)+$|#L\d+(?:-L?\d+)?$")


def _roots(request: Request) -> tuple[Path, list[Path]]:
    """Домашний каталог (корень чтения) и закрытые каталоги хаба; тесты подменяют через `app.state`."""
    state = request.app.state
    home = getattr(state, "md_home", None) or Path(os.path.expanduser("~"))
    closed = getattr(state, "md_closed", None)
    if closed is None:
        from ... import paths

        closed = [
            Path(os.environ.get("HUB_DATA_DIR") or Path.home() / ".local/share/hub"),
            paths.config_dir_path(),
        ]
    return Path(home), [Path(p) for p in closed]


def clean(raw: str, home: Path) -> str:
    """Путь из текста сессии: без кавычек, `file://`, хвоста строки и `#L…`; `~` — домашний каталог."""
    text = raw.strip().strip("`'\"<>").strip()
    if text.startswith("file://"):
        text = text[len("file://") :]
    text = _LINE_TAIL.sub("", text)
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    return text


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve(text: str, bases: list[str], home: Path, closed: list[Path]) -> tuple[Path | None, list[str]]:
    """Первый подходящий файл: `(путь, [])`; не нашёлся — `(None, что пробовали)`."""
    candidates = [Path(text)] if os.path.isabs(text) else [Path(b) / text for b in bases if b]
    tried: list[str] = []
    home_real = home.resolve()
    closed_real = [c.resolve() for c in closed]
    for cand in candidates:
        tried.append(str(cand))
        try:
            real = cand.resolve(strict=True)
        except OSError:
            continue
        if not real.is_file() or real.suffix.lower() not in MD_SUFFIXES:
            continue
        if not _inside(real, home_real) or any(_inside(real, c) for c in closed_real):
            continue
        return real, []
    return None, tried


def _bases(
    conn, session_id: int | None, project_id: int | None, from_path: str | None, home: Path
) -> list[str]:
    bases: list[str] = []
    if from_path:
        parent = os.path.dirname(clean(from_path, home))
        if os.path.isabs(parent):
            bases.append(parent)
    if session_id is not None:
        row = conn.execute(
            "SELECT workdir, subdir, project_id FROM main.hub_session WHERE id = ?", (session_id,)
        ).fetchone()
        if row is not None:
            workdir, subdir, pid = row
            if workdir and subdir:
                bases.append(os.path.join(workdir, subdir))
            if workdir:
                bases.append(workdir)
            project_id = project_id if project_id is not None else pid
    rows = conn.execute(
        "SELECT id, workdir FROM main.project WHERE workdir IS NOT NULL AND workdir != ''"
    ).fetchall()
    own = [w for pid, w in rows if pid == project_id]
    rest = [w for pid, w in rows if pid != project_id]
    for workdir in [*own, *rest, str(home)]:
        if workdir not in bases:
            bases.append(workdir)
    return bases


def _read(path: Path) -> dict:
    st = path.stat()
    if st.st_size > MD_MAX_BYTES:
        raise HTTPException(413, "md_too_big")
    data = path.read_bytes()[: MD_MAX_BYTES + 1]
    return {
        "path": str(path),
        "text": data.decode("utf-8", "replace"),
        "size": st.st_size,
        "mtime_ms": int(st.st_mtime * 1000),
    }


@public_router.get("/files/md")
async def get_md(
    request: Request,
    principal: Principal = ui_only,  # noqa: ARG001
    path: str = Query(..., min_length=1, max_length=4096),
    session_id: int | None = Query(None),
    project_id: int | None = Query(None),
    from_: str | None = Query(None, alias="from", max_length=4096),
) -> dict:
    home, closed = _roots(request)
    text = clean(path, home)
    if not text:
        raise HTTPException(422, "md_bad_path")
    with request.app.state.db.reader() as conn:
        bases = _bases(conn, session_id, project_id, from_, home)
    found, tried = await asyncio.to_thread(_resolve, text, bases, home, closed)
    if found is None:
        raise HTTPException(404, {"code": "md_not_found", "path": text, "tried": tried[:8]})
    return await asyncio.to_thread(_read, found)
