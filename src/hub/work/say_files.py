"""Файлы реплики владельца — во вход модели (Ф41-3в, docs/architecture.md).

Хост забрал у ядра байты файлов, приложенных к реплике; здесь решается, как каждый подать:
- картинка (до 3,75 МиБ: 5 МБ в base64) — блоком самой модели: у Claude Code `image` (base64), у OpenCode —
  часть `file` с data:-URL и только если модель принимает картинки;
- PDF (до 10 МиБ) у Claude Code — блоком `document` (base64);
- всё прочее (и слишком большие картинка и PDF, и PDF у OpenCode) — файлом в `<рабочая папка>/hub-files/`,
  а к тексту реплики дописывается строка с путями. Блоки одной реплики в сумме — не больше 20 МБ base64:
  что не влезло, тоже идёт файлом.

Граница: имя файла пришло из запроса интерфейса и попадает в текст реплики и в путь на диске. Ядро его уже
чистило; здесь оно чистится ещё раз (`workdir_files.plain_name`: одно простое имя из букв, цифр и
` .-_()+`, без перевода строки и разделителей), а запись идёт только через дескриптор каталога
(`workdir_files.Target.write_new`: `O_EXCL|O_NOFOLLOW`, подменённый каталог — отказ, занятое имя — суффикс).
В события и журнал — только имена и размеры, не содержимое."""

import base64
import os
import re
from typing import NamedTuple

from hub import workdir_files

MIB = 1024 * 1024
# Ревью Ф41-3в: предел картинки у модели — 5 МБ уже в base64, то есть 3,75 МиБ сырых байт.
IMAGE_B64_MAX = 5 * MIB
CC_IMAGE_MAX = IMAGE_B64_MAX // 4 * 3
CC_PDF_MAX = 10 * MIB
OC_IMAGE_MAX = IMAGE_B64_MAX // 4 * 3
# Общий бюджет одной реплики: сумма base64 всех блоков (картинки, документы; у OpenCode — части-файлы). Что
# не влезло (по порядку загрузки) — файлом в `hub-files` с путём в тексте.
BLOCKS_B64_MAX = 20 * 1000 * 1000
FILE_MAX = 20 * MIB  # предел ядра на один файл: больше хост не забирает
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
PDF_MIME = "application/pdf"
SAVED_NOTE = "Приложены файлы (лежат в папке проекта): "
UNSAVED_NOTE = "Файлы не удалось положить в папку проекта: "
LOST_NOTE = "Файлы реплики не получены (хаб не отдал их): "


class Prepared(NamedTuple):
    """Реплика, готовая к подаче: текст (с дописанными строками про файлы), блоки модели и события-ошибки
    `(текст,)` для ленты сессии."""

    text: str
    blocks: list[dict]
    errors: list[str]


def well_formed(files) -> list[dict]:
    """Список файлов реплики из ответа ядра — только записи с ожидаемыми полями (хост ядру доверяет, но
    падать на кривой записи не должен)."""
    found = []
    for item in files if isinstance(files, list) else []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("size"), int)
            and not isinstance(item.get("size"), bool)
            and 0 < item["size"] <= FILE_MAX
        ):
            found.append(item)
    return found


def label(item: dict) -> str:
    """Имя и размер для ленты и текста реплики — имя уже очищенное."""
    return f"{workdir_files.plain_name(item.get('name'))} ({item.get('size')} Б)"


def inline(engine: str, item: dict, *, oc_image: bool) -> str | None:
    """Чем подать файл модели напрямую: `image`, `document` или `None` (положить в `hub-files`)."""
    kind, mime, size = item.get("kind"), item.get("mime"), item["size"]
    if engine == "cursor":
        return None  # Ф71-1 (П8): у Cursor блоков во входе нет — файлом в `hub-files`, путь в тексте
    if kind == "image" and mime in IMAGE_MIMES:
        if engine == "opencode":
            return "image" if oc_image and size <= OC_IMAGE_MAX else None
        return "image" if size <= CC_IMAGE_MAX else None
    if kind == "pdf" and mime == PDF_MIME and engine not in ("opencode", "codex") and size <= CC_PDF_MAX:
        return "document"
    return None


def b64_len(size: int) -> int:
    return (size + 2) // 3 * 4


def block(engine: str, how: str, item: dict, data: bytes) -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    name = workdir_files.plain_name(item.get("name"))
    if engine == "opencode":
        # Часть `file` OpenCode (проба `.runs/f41-probe/oc`): data:-URL, имя — для показа.
        return {
            "type": "file",
            "mime": item["mime"],
            "filename": name,
            "url": f"data:{item['mime']};base64,{encoded}",
        }
    if engine == "codex":
        # Ф74-5: `UserInput` Codex — картинка data:-URL (`{"type": "image", "url"}`); PDF блоком не идёт.
        return {"type": "image", "url": f"data:{item['mime']};base64,{encoded}"}
    if how == "image":
        return {"type": "image", "source": {"type": "base64", "media_type": item["mime"], "data": encoded}}
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": PDF_MIME, "data": encoded},
        "title": name,
    }


def _save(workdir: str, pairs: list[tuple[dict, bytes]]) -> tuple[list[str], list[str], str | None]:
    """Файлы в `<workdir>/hub-files/`: `(имена, под которыми легли; ярлыки не записанных; причина отказа)`."""
    saved: list[str] = []
    failed: list[str] = []
    why: str | None = None
    try:
        target = workdir_files.open_workdir(workdir)
    except (workdir_files.Refused, OSError) as exc:
        why = str(exc) if isinstance(exc, workdir_files.Refused) else exc.__class__.__name__
        return saved, [label(item) for item, _data in pairs], why
    try:
        for item, data in pairs:
            try:
                saved.append(target.write_new(workdir_files.plain_name(item.get("name")), data))
            except (workdir_files.Refused, OSError) as exc:
                why = str(exc) if isinstance(exc, workdir_files.Refused) else exc.__class__.__name__
                failed.append(label(item))
    finally:
        target.close()
    return saved, failed, why


def _with_notes(text: str, notes: list[str]) -> str:
    return "\n\n".join(part for part in (text.rstrip() if notes else text, *notes) if part)


def is_text_of(text: str, base: str) -> bool:
    """Текст `text` — это реплика `base` с дописанным абзацем про файлы (сверка «реплика уже подана»)."""
    head = base.rstrip()
    if not text.startswith(head):
        return False
    return text[len(head) :].lstrip("\n").startswith((SAVED_NOTE, UNSAVED_NOTE, LOST_NOTE))


def blocks_b64(blocks: list[dict]) -> int:
    """Ф55-1 (2.1): сколько base64 в блоках реплики — для общего бюджета пачки реплик."""
    total = 0
    for item in blocks:
        source = item.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            total += len(source["data"])
        elif isinstance(item.get("url"), str):
            total += len(item["url"])
    return total


BATCH_HEAD = "Реплика {n} из {m}:"
BATCH_EMPTY = "(без текста — файлы приложены)"
_BATCH_SPLIT = re.compile(r"(?:\A|\n\n)Реплика \d+ из \d+:\n")


def batch_text(texts: list[str]) -> str:
    """Ф55-1 (2.1): текст пачки реплик владельца — один ввод пользователя. Одна реплика — как есть, без
    обёртки; несколько — подряд, у каждой строка-шапка «Реплика N из M:»."""
    if len(texts) == 1:
        return texts[0]
    total = len(texts)
    return "\n\n".join(
        f"{BATCH_HEAD.format(n=n, m=total)}\n{text.strip() or BATCH_EMPTY}" for n, text in enumerate(texts, 1)
    )


def in_batch(text: str, base: str) -> bool:
    """Текст `text` — пачка реплик, и реплика `base` в ней есть (сверка «реплика уже подана»)."""
    if not _BATCH_SPLIT.match(text):
        return False
    want = base.strip() or BATCH_EMPTY
    return any(piece == want or is_text_of(piece, want) for piece in _BATCH_SPLIT.split(text)[1:])


def prepare(
    *,
    engine: str,
    text: str,
    files: list[dict],
    data: dict[str, bytes],
    workdir: str,
    oc_image: bool,
    absolute: bool = False,
) -> Prepared:
    """Реплика с файлами: `data` — байты по id. Порядок содержимого — текст, затем блоки (4.2). Отказ записи
    в `hub-files` — событие-ошибка и пометка в тексте; реплика подаётся всё равно. Ф50-7 (П6): `absolute` —
    сессия работает в подпапке проекта: путь файла в пометке — полный (относительный от её папки неверен)."""
    blocks: list[dict] = []
    to_disk: list[tuple[dict, bytes]] = []
    budget = BLOCKS_B64_MAX
    for item in files:
        body = data[item["id"]]
        how = inline(engine, item, oc_image=oc_image)
        if how is None or b64_len(len(body)) > budget:
            to_disk.append((item, body))
        else:
            budget -= b64_len(len(body))
            blocks.append(block(engine, how, item, body))
    notes: list[str] = []
    errors: list[str] = []
    if to_disk:
        saved, failed, why = _save(workdir, to_disk)
        if saved:
            base = os.path.join(workdir, workdir_files.DIR_NAME) if absolute else workdir_files.DIR_NAME
            notes.append(SAVED_NOTE + ", ".join(f"{base}/{name}" for name in saved))
        if failed:
            notes.append(UNSAVED_NOTE + ", ".join(failed))
            errors.append(
                f"файлы реплики не записаны в {workdir_files.DIR_NAME}: {', '.join(failed)} — {why}"
            )
    return Prepared(_with_notes(text, notes), blocks, errors)


def lost(text: str, files: list[dict]) -> Prepared:
    """Байты не получены после всех попыток: реплика подаётся без файлов, с пометкой в тексте."""
    names = ", ".join(label(item) for item in files)
    return Prepared(_with_notes(text, [LOST_NOTE + names]), [], [f"файлы реплики не получены: {names}"])
