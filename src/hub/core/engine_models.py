"""Ф74-5б: живой список моделей движка сессий хаба (у Codex — `model/list`, сообщает хост).

Хост присылает `POST /work/host/engine-models` `{engine, models: [{id, efforts, default_effort}]}`; ядро
хранит последнее присланное в `main.meta` (ключ `engine_models`, JSON `{движок: {"models": […], "ts": мс}}`),
`/status` кладёт его в профиль движка полем `models` (контракт — `docs/architecture.md`).
Поля нет или список пуст — интерфейс берёт `model_hints` и `efforts` профиля. Хаб модели не проверяет —
проверяет движок при запуске (`codex_process.check_model`)."""

import json
import re

META_KEY = "engine_models"
MAX_MODELS = 100
MAX_EFFORTS = 20
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def normalize(models) -> list[dict]:
    """Список моделей в форме контракта; чужое (не строка, неверное имя, повтор) отбрасывается."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not _ID_RE.match(model_id) or model_id in seen:
            continue
        efforts: list[str] = []
        for effort in item.get("efforts") if isinstance(item.get("efforts"), list) else []:
            if isinstance(effort, str) and _EFFORT_RE.match(effort) and effort not in efforts:
                efforts.append(effort)
        efforts = efforts[:MAX_EFFORTS]
        default = item.get("default_effort")
        out.append(
            {
                "id": model_id,
                "efforts": efforts,
                "default_effort": default if isinstance(default, str) and default in efforts else None,
            }
        )
        seen.add(model_id)
        if len(out) >= MAX_MODELS:
            break
    return out


def load(conn) -> dict[str, list[dict]]:
    """`{движок: модели}` из `main.meta` (битая запись — пусто)."""
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (META_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for engine, entry in data.items():
        if isinstance(engine, str) and isinstance(entry, dict):
            models = normalize(entry.get("models"))
            if models:
                out[engine] = models
    return out


def store(conn, engine: str, models, *, now_ms: int) -> list[dict]:
    """Запомнить список движка `engine` (пустой — убрать). Возвращает сохранённое."""
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (META_KEY,)).fetchone()
    try:
        data = json.loads(row[0]) if row is not None else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    clean = normalize(models)
    if clean:
        data[engine] = {"models": clean, "ts": now_ms}
    else:
        data.pop(engine, None)
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (META_KEY, json.dumps(data, ensure_ascii=False)),
    )
    return clean


def load_for_ui(conn) -> dict[str, list[dict]]:
    """`load` для `/status`: у OpenCode хост своего списка не шлёт — берётся список, который служба агента
    владельца прислала со своего OpenCode (`PUT /owner_channel/agent-models`; варианты модели = «Усилие» сессии
    OpenCode).
    Импорт внутри: модуль агента тянет `engine`, а тот — этот модуль."""
    from . import owner_agent_model  # noqa: PLC0415

    out = load(conn)
    if "opencode" not in out:
        items = owner_agent_model.agent_models(conn, "opencode")["items"]
        listed = normalize([{"id": m["id"], "efforts": m.get("variants") or []} for m in items])
        if listed:
            out["opencode"] = listed
    if "cursor" not in out:
        # Живого списка Cursor нет — семейства с усилиями по семейству (Grok — от high, Claude — как в Claude
        # Code; слово владельца).
        from hub.engine import choice  # noqa: PLC0415

        default = choice.CURSOR_EFFORT_DEFAULT
        out["cursor"] = normalize(
            [
                {"id": f, "efforts": list(choice.cursor_efforts(f)), "default_effort": default}
                for f in choice.CURSOR_EFFORT_FAMILIES
            ]
        )
    return out
