"""Ф41-1: срок кэша промпта и размер контекста сессии Claude Code — по стенограмме
`<каталог Claude>/projects/<слаг>/<session-id>.jsonl`. Только чтение файлов; тексты стенограммы в журнал не
пишутся.

Слаг папки в `find` не используется: id сессии — UUID, файл ищется по имени.

Ф99-1 (docs/architecture.md, П3, П4): разговоры папки проекта (`conversations`, `conv_path`) — по
слагу папки. Интерфейсу (только кука) отдаётся НАЧАЛО первой настоящей реплики человека (без служебных
записей), через `mask_secrets`, до 160 символов; остальное — числа."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from ..session_registry import claude_dir
from ..work.session import mask_secrets

log = logging.getLogger(__name__)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TAIL_BYTES = 512 * 1024
TTL_HOUR_S = 3600
TTL_5M_S = 300
CACHE_LIMIT = 256

# (путь, mtime_ns, размер) -> результат `cache_state`; путь по session_id — отдельно.
_STATE: dict[tuple[str, int, int], dict | None] = {}
_PATHS: dict[str, Path] = {}


def find(session_id: str, claude_root: Path | None = None) -> Path | None:
    """Файл стенограммы по UUID сессии; не UUID — `None` без обращения к диску. Несколько совпадений — самый
    свежий по mtime."""
    if not isinstance(session_id, str) or not _UUID.match(session_id):
        return None
    root = claude_root if claude_root is not None else claude_dir()
    best: tuple[int, Path] | None = None
    for path in (root / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return best[1] if best else None


def _num(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _ts_ms(value) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _entry(line: str) -> dict | None:
    """Подходящая запись ассистента -> числа; всё остальное (битая строка, синтетика, подагент) — `None`."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant" or obj.get("isSidechain"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    last_ts = _ts_ms(obj.get("timestamp"))
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    tokens = [_num(usage.get(k)) for k in keys]
    if last_ts is None or any(t is None for t in tokens):
        return None
    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}
    one_h = _num(creation.get("ephemeral_1h_input_tokens")) or 0
    five_m = _num(creation.get("ephemeral_5m_input_tokens")) or 0
    ttl = TTL_HOUR_S if one_h > 0 else TTL_5M_S if five_m > 0 else None
    model = message.get("model")
    return {
        "last_ts": last_ts,
        "ttl_s": ttl,
        "context_tokens": sum(tokens),  # type: ignore[arg-type]
        "model": model if isinstance(model, str) else "",
    }


def cache_state(path: Path) -> dict | None:
    """Состояние кэша по хвосту файла (последние 512 КиБ) или `None`."""
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").split("\n")
    if start > 0:
        lines = lines[1:]  # первая строка хвоста может быть неполной
    last: dict | None = None
    ttl: int | None = None
    for line in reversed(lines):
        if not line.strip():
            continue
        entry = _entry(line)
        if entry is None:
            continue
        if last is None:
            last = entry
        if entry["ttl_s"] is not None:
            ttl = entry["ttl_s"]
            break
    if last is None:
        return None
    ttl = ttl if ttl is not None else TTL_HOUR_S
    return {
        "last_ts": last["last_ts"],
        "ttl_s": ttl,
        "expires_ts": last["last_ts"] + ttl * 1000,
        "context_tokens": last["context_tokens"],
        "model": last["model"],
    }


def _trim(store: dict) -> None:
    while len(store) > CACHE_LIMIT:
        store.pop(next(iter(store)))


def state_for(session_id: str, claude_root: Path | None = None) -> dict | None:
    """`find` + `cache_state` с кэшем в памяти по `(путь, mtime_ns, размер)`. Ошибка ввода-вывода — `None`."""
    try:
        path = _PATHS.get(session_id)
        if path is None or not path.is_file():
            path = find(session_id, claude_root)
            if path is None:
                _PATHS.pop(session_id, None)
                return None
            _PATHS[session_id] = path
            _trim(_PATHS)
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key in _STATE:
            return _STATE[key]
        result = cache_state(path)
        _STATE[key] = result
        _trim(_STATE)
        return result
    except OSError as exc:
        log.debug("стенограмма сессии не прочитана: %s", type(exc).__name__)
        return None


# --- Ф99-1: разговоры папки проекта ------------------------------------------------------------------------

CONV_LIST_MAX = 50
PREVIEW_MAX = 160
PREVIEW_SCAN_BYTES = 2 * 1024 * 1024  # первая реплика ищется в начале файла, не глубже
# Служебные записи `user`, которые не реплика человека: команды и их вывод, напоминания, ввод оболочки.
_SERVICE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-",
    "<system-reminder>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-memory-input>",
    "Caveat: The messages below were generated",
)


def is_conv_id(value) -> bool:
    """Id разговора Claude Code — UUID в нижнем регистре."""
    return isinstance(value, str) and bool(_UUID.match(value))


def project_slugs(workdir: str) -> list[str]:
    """Слаги каталога записей Claude Code для папки проекта: путь как записан и после `realpath` (процесс
    получает настоящий путь `getcwd`); всё, кроме латиницы и цифр, — «-»."""
    out: list[str] = []
    for path in (workdir, os.path.realpath(workdir)):
        slug = re.sub(r"[^A-Za-z0-9]", "-", path)
        if slug not in out:
            out.append(slug)
    return out


def conv_path(workdir: str, conv_id: str, claude_root: Path | None = None) -> Path | None:
    """Файл разговора `conv_id` (UUID) в каталоге записей папки проекта; нет или не UUID — `None`."""
    if not workdir or not is_conv_id(conv_id):
        return None
    root = (claude_root if claude_root is not None else claude_dir()) / "projects"
    for slug in project_slugs(workdir):
        path = root / slug / f"{conv_id}.jsonl"
        if path.is_file():
            return path
    return None


def _human_text(obj) -> str | None:
    """Текст настоящей реплики человека из записи стенограммы; служебная запись или не `user` — `None`."""
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return None
    if obj.get("isMeta") or obj.get("isSidechain") or obj.get("isCompactSummary"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None  # результат инструмента — не реплика
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
    for text in texts:
        stripped = text.strip()
        if stripped and not stripped.startswith(_SERVICE_PREFIXES):
            return stripped
    return None


def first_human(path: Path) -> str:
    """Начало первой настоящей реплики человека (маскированное, одной строкой, до 160 символов); нет — «»."""
    read = 0
    with path.open("rb") as fh:
        for raw in fh:
            read += len(raw)
            if read > PREVIEW_SCAN_BYTES:
                break
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            text = _human_text(obj)
            if text is None:
                continue
            flat = " ".join(mask_secrets(text).split())
            return flat if len(flat) <= PREVIEW_MAX else flat[: PREVIEW_MAX - 1] + "…"
    return ""


def conversations(workdir: str, claude_root: Path | None = None, *, limit: int = CONV_LIST_MAX) -> list[dict]:
    """Разговоры Claude Code папки проекта: `{id, last_ts, size, preview}`, свежие сверху, не больше `limit`.
    `last_ts` — время последней записи (mtime файла, мс)."""
    if not workdir:
        return []
    root = (claude_root if claude_root is not None else claude_dir()) / "projects"
    found: dict[str, tuple[int, int, Path]] = {}
    for slug in project_slugs(workdir):
        try:
            entries = list((root / slug).glob("*.jsonl"))
        except OSError:
            continue
        for path in entries:
            conv_id = path.stem
            if not _UUID.match(conv_id):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if conv_id not in found or st.st_mtime_ns > found[conv_id][0]:
                found[conv_id] = (st.st_mtime_ns, st.st_size, path)
    items = sorted(found.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
    out = []
    for conv_id, (mtime_ns, size, path) in items:
        try:
            preview = first_human(path)
        except OSError:
            preview = ""
        out.append({"id": conv_id, "last_ts": mtime_ns // 1_000_000, "size": size, "preview": preview})
    return out


# --- Ф99-7.3: хвост стенограммы для ленты «только смотреть» ------------------------------------------------

TAIL_MAX = 200
TAIL_BEAT = 50  # событий в пульсе MCP
TAIL_SCAN_MAX = 4 * 1024 * 1024
TEXT_LIMIT = 8000
STEP_LIMIT = 300
_INPUT_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "text")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _step(text: str) -> str:
    return _clip(" ".join(mask_secrets(text).split()), STEP_LIMIT)


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if isinstance(t, str))
    return ""


def _input_summary(data) -> str:
    if not isinstance(data, dict):
        return ""
    for key in _INPUT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return _step(value)
    return ""


def _events_of(obj: dict, base_id: int) -> list[dict]:
    """Одна запись стенограммы -> события ленты (id — смещение строки * 100 + номер блока)."""
    kind = obj.get("type")
    if kind not in ("user", "assistant"):
        return []
    if obj.get("isSidechain") or obj.get("isMeta") or obj.get("isCompactSummary"):
        return []
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    ts = _ts_ms(obj.get("timestamp")) or 0
    content = message.get("content")
    out: list[dict] = []

    def add(ev_kind: str, summary: str, *, tool=None, is_error=False, ref=None) -> None:
        out.append(
            {
                "id": base_id * 100 + len(out),
                "ts": ts,
                "kind": ev_kind,
                "tool": tool,
                "summary": summary,
                "is_error": is_error,
                "ref": ref,
            }
        )

    if kind == "user":
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
        else:
            return []
        for block in blocks:
            if block.get("type") == "tool_result":
                ref = block.get("tool_use_id")
                add(
                    "tool_done",
                    _step(_block_text(block.get("content"))) or "готово",
                    is_error=block.get("is_error") is True,
                    ref=ref if isinstance(ref, str) else None,
                )
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"].strip()
                if text and not text.startswith(_SERVICE_PREFIXES):
                    add("say", "владелец (next): " + _clip(mask_secrets(text), TEXT_LIMIT))
        return out
    if message.get("model") == "<synthetic>" or not isinstance(content, list):
        return []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            add("text", _clip(mask_secrets(block["text"].strip()), TEXT_LIMIT))
        elif block.get("type") == "tool_use":
            name = block.get("name")
            ref = block.get("id")
            add(
                "tool",
                _input_summary(block.get("input")),
                tool=name if isinstance(name, str) else "?",
                ref=ref if isinstance(ref, str) else None,
            )
    return out


def tail_events(path: Path, limit: int = TAIL_BEAT) -> list[dict]:
    """Последние `limit` (≤ 200) событий стенограммы, старые сверху. Читается хвост окнами 256 КиБ…4 МиБ."""
    limit = max(1, min(int(limit), TAIL_MAX))
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        window = 256 * 1024
        while True:
            start = max(0, size - window)
            fh.seek(start)
            data = fh.read(size - start)
            events: list[dict] = []
            offset = start
            for i, raw in enumerate(data.split(b"\n")):
                line_at = offset
                offset += len(raw) + 1
                if i == 0 and start > 0:
                    continue  # первая строка окна может быть неполной
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    events.extend(_events_of(obj, line_at))
            if len(events) >= limit or start == 0 or window >= TAIL_SCAN_MAX:
                return events[-limit:]
            window *= 2


def tail_for(session_id: str, limit: int = TAIL_BEAT, claude_root: Path | None = None) -> list[dict]:
    """`find` + `tail_events`; нет файла или ошибка чтения — пустой список."""
    try:
        path = find(session_id, claude_root)
        return tail_events(path, limit) if path is not None else []
    except OSError as exc:
        log.debug("хвост стенограммы не прочитан: %s", type(exc).__name__)
        return []
