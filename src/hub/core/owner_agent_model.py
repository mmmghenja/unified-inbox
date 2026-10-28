"""Модель агента владельца — выбор владельца (Ф75-1, docs/architecture.md; Ф78-2,
docs/architecture.md).

Ф78-2: выбор модели агента — это выбор ТЕКУЩЕГО движка переключателя (`core/engine.py`, ключ `engine_choice`):
«Модель ▾» в «Мой агент» и панель движка показывают и меняют одну запись. Старый ключ `owner_agent_model`
(`{"model": "провайдер/модель", "variant", "ts"}`) больше не пишется — он читается как выбор `opencode`, пока
нет записи `engine_choice` (`engine.remembered`). Ядро только хранит:

- выбор владельца — `engine_choice` (пишет только кука интерфейса: ручки `/engine` и `/owner_channel/agent-settings`);
- список моделей, присланный службой агента (`PUT /owner_channel/agent-models`, поле `engine`, умолчание
  `opencode`): ключ `owner_agent_models` (OpenCode, как было) или `owner_agent_models:<движок>` —
  `{"items": [{id, name, images, variants}], "ts": мс}`. Модель вне списка OpenCode ядро не отвергает
  (свободный ввод владельца): нет её у сервера — служба сама переходит на запасную.

Ф76-3 (план Ф76, П15): замена по проекту — ключ `agent_model` политики проекта (`project.agent_policy_json`):
Ф78-2 — `{"engine", "model", "effort", "ts"}` (пишется с текущим движком; старая `{"model", "variant"}` —
`opencode`). Действует, только когда её движок равен текущему (решает агент). Пишет только кука интерфейса.

Миграций нет: всё в `main.meta` и политике проекта. Запись — только внутри `Db.write()`, время — параметром
`now_ms`."""

import json
import re
from pathlib import Path

from . import engine as engine_mod
from .audit import write_audit

META_CHOICE = engine_mod.META_LEGACY_AGENT  # старый ключ: только чтение (Ф78-2)
PROJECT_KEY = "agent_model"  # Ф76-3: в `project.agent_policy_json`
META_MODELS = "owner_agent_models"
ROLE = "agent"

MODEL_MAX = 200
NAME_MAX = 120
VARIANT_MAX = 32
VARIANTS_MAX = 16
MODELS_MAX = 500
PLAIN_MODEL_MAX = 128  # Ф78-2: модель без слэша (claude, codex, cursor)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SettingsError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _load(conn, key: str) -> dict:
    raw = engine_mod._meta_get(conn, key)  # noqa: SLF001
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _plain_name_ok(value: str) -> bool:
    return 0 < len(value) <= PLAIN_MODEL_MAX and value.isprintable() and not any(c.isspace() for c in value)


def model_name_ok(value: str, engine: str = "opencode") -> bool:
    """Правило имени модели (Ф78-2, раздел 2): `opencode` — «провайдер/модель» (до 200 знаков, `MODEL_RE`);
    остальные движки — 1–128 печатных символов без пробелов."""
    if engine == "opencode":
        return len(value) <= MODEL_MAX and MODEL_RE.match(value) is not None
    return _plain_name_ok(value)


def check_model(value, engine: str = "opencode") -> str | None:
    """Имя модели движка `engine` или `None` (выбор снят — умолчание движка); иначе 422."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(422, "model: строка или null")
    value = value.strip()
    if not value:
        return None
    if not model_name_ok(value, engine):
        if engine == "opencode":
            raise SettingsError(422, "model: нужно «провайдер/модель» (латиница, цифры, . _ - : @ +)")
        raise SettingsError(422, f"model: 1–{PLAIN_MODEL_MAX} печатных символов без пробелов")
    return value


def check_variant(value, *, field: str = "variant") -> str | None:
    """Уровень (усилие, `variant` OpenCode): латиница, цифры, `. _ -`, до 32 знаков; `None` — умолчание."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(422, f"{field}: строка или null")
    value = value.strip()
    if not value:
        return None
    if len(value) > VARIANT_MAX or not VARIANT_RE.match(value):
        raise SettingsError(422, f"{field}: латиница, цифры, . _ -, до 32 знаков")
    return value


def models_key(engine: str) -> str:
    """Ключ `main.meta` списка моделей агента: OpenCode — прежний `owner_agent_models`."""
    return META_MODELS if engine == "opencode" else f"{META_MODELS}:{engine}"


def _clean_models(raw, engine: str = "opencode") -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_name_ok(model_id, engine):
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        name = entry.get("name")
        images = entry.get("images")
        variants = entry.get("variants")
        if not variants and isinstance(entry.get("efforts"), list):
            variants = entry["efforts"]
        items.append(
            {
                "id": model_id,
                "name": name[:NAME_MAX] if isinstance(name, str) and name.strip() else None,
                "images": images if isinstance(images, bool) else None,
                "variants": [
                    v
                    for v in (variants if isinstance(variants, list) else [])
                    if isinstance(v, str) and len(v) <= VARIANT_MAX and VARIANT_RE.match(v)
                ][:VARIANTS_MAX],
            }
        )
        if len(items) >= MODELS_MAX:
            break
    return items


def agent_models(conn, engine: str = "opencode") -> dict:
    """Список моделей движка, присланный службой агента: `{"items": [{id, name, images, variants}], "ts"}`."""
    listed = _load(conn, models_key(engine))
    ts = listed.get("ts")
    return {
        "items": _clean_models(listed.get("items"), engine),
        "ts": ts if isinstance(ts, int) and not isinstance(ts, bool) else None,
    }


def default(config_dir: Path | None, engine: str = "opencode") -> dict:
    """Умолчание движка (как его увидит служба на этой машине): `{"model", "effort", "variant"}`
    (`variant` = `effort`, совместимость со старым агентом). Битый `engine.json` — умолчание таблицы ролей."""
    base = engine_mod.defaults(engine, config_dir)
    return {"model": base["model"], "effort": base["effort"], "variant": base["effort"]}


def _policy(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def _choice_view(project_id: int, raw) -> dict:
    """Элемент `projects` (П5): `{project_id, engine, model, effort, variant, set_ts}`; старая запись без
    `engine` — `opencode`, `effort` = её `variant`; `variant` = `effort` (совместимость со старым агентом)."""
    value = raw if isinstance(raw, dict) else {}
    model = value.get("model") if isinstance(value.get("model"), str) else None
    engine = value.get("engine") if value.get("engine") in engine_mod.ENGINES else "opencode"
    effort = value.get("effort") if isinstance(value.get("effort"), str) else None
    if effort is None and isinstance(value.get("variant"), str):
        effort = value["variant"]
    effort = effort if model is not None else None
    return {
        "project_id": project_id,
        "engine": engine if model is not None else None,
        "model": model,
        "effort": effort,
        "variant": effort,
        "set_ts": value.get("ts") if isinstance(value.get("ts"), int) else None,
    }


def project_choice(conn, project_id: int) -> dict | None:
    """Ф76-3: замена модели агента в проекте `{project_id, engine, model, effort, variant, set_ts}` (`model =
    null` — общий выбор); нет проекта — `None`."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    return _choice_view(project_id, _policy(row[0]).get(PROJECT_KEY))


def project_choices(conn) -> list[dict]:
    """Ф76-3: проекты, где владелец заменил модель агента (только с моделью), по id — СПИСКОМ (П5)."""
    out = []
    for pid, raw in conn.execute("SELECT id, agent_policy_json FROM main.project ORDER BY id"):
        view = _choice_view(pid, _policy(raw).get(PROJECT_KEY))
        if view["model"] is not None:
            out.append(view)
    return out


def _checked(conn, engine: str, model, effort, config_dir: Path | None) -> tuple[str | None, str | None]:
    try:
        return engine_mod.check_choice(conn, engine, model, effort, config_dir=config_dir)
    except engine_mod.EngineError as exc:
        raise SettingsError(exc.status, exc.detail) from None


def set_project_choice(
    conn, project_id: int, *, model, effort, now_ms: int, audit: dict, config_dir: Path | None = None
) -> dict | None:
    """Ф76-3: владелец заменил модель агента в проекте (`model = null` — снять замену, ключ убирается); Ф78-2 —
    запись с текущим движком переключателя. Прочие ключи политики не трогаются. Нет проекта — `None`;
    кривое — 422."""
    engine = engine_mod.get_engine(conn)
    model, effort = _checked(conn, engine, model, effort, config_dir)
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy(row[0])
    if model is None:
        policy.pop(PROJECT_KEY, None)
        effort = None
    else:
        policy[PROJECT_KEY] = {"engine": engine, "model": model, "effort": effort, "ts": now_ms}
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_agent_model",
        result="ok",
        detail={"project_id": project_id, "engine": engine, "model": model, "effort": effort},
        **audit,
    )
    return project_choice(conn, project_id)


def settings(conn, *, config_dir: Path | None) -> dict:
    """Тело `GET /owner_channel/agent-settings` (Ф78-2, 3.3): `{engine, model, effort, variant, set_ts, default:
    {model, effort, variant}, models: [{id, name, images, efforts, variants}], models_ts, projects}` — всё про
    ТЕКУЩИЙ движок переключателя. `model = null` — владелец не выбирал (умолчание движка — `default`); `models`
    пуст — списка нет (свободный ввод по правилу имени). `variant` = `effort` (старый агент до `install.sh`).
    `projects` — список замен по проектам (П5; агенту маршрут оставляет только проекты его токена)."""
    from . import engine_fallback  # noqa: PLC0415 — Ф83-1; модуль тянет записи сессий хаба

    engine = engine_mod.get_engine(conn)
    own = engine_mod.remembered(conn, config_dir)[engine]
    model = own["model"]
    effort = own["effort"]
    models = [{**m, "variants": list(m["efforts"])} for m in engine_mod.models_of(conn, engine)]
    return {
        "engine": engine,
        "model": model,
        "effort": effort,
        "variant": effort,
        "set_ts": engine_mod.choice_ts(conn),
        "default": default(config_dir, engine),
        "models": models,
        "models_ts": agent_models(conn, engine)["ts"],
        "projects": project_choices(conn),
        # Ф83-1 (П10): действующий выбор агента (на запасном — запасной; замена модели по проекту на запасном
        # не действует), состояние «на запасном» и сам запасной (`null` — не задан).
        "effective": engine_fallback.effective(conn, "agent", config_dir=config_dir),
        "on_fallback": engine_fallback.on_state(conn, "agent"),
        "fallback": engine_fallback.resolved(conn, "agent", config_dir=config_dir),
    }


def set_choice(conn, *, model, effort, now_ms: int, audit: dict, config_dir: Path | None) -> dict:
    """Владелец выбрал модель и усилие агента — выбор ТЕКУЩЕГО движка (та же запись, что `PUT /engine`);
    `model = null` — умолчание модели, `effort = null` — умолчание усилия. -> то же, что `settings`."""
    engine = engine_mod.get_engine(conn)
    model, effort = _checked(conn, engine, model, effort, config_dir)
    engine_mod.write_choice(conn, engine, model, effort, now_ms=now_ms, config_dir=config_dir)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_agent_model",
        result="ok",
        detail={"engine": engine, "model": model, "effort": effort},
        **audit,
    )
    return settings(conn, config_dir=config_dir)


def set_models(conn, items, *, now_ms: int, engine: str = "opencode") -> dict:
    """Служба агента прислала модели движка `engine` -> `{engine, count, models_ts}`. Больше `MODELS_MAX` —
    хвост отбрасывается; негодные записи пропускаются."""
    if engine not in engine_mod.ENGINES:
        raise SettingsError(422, f"engine: {' или '.join(engine_mod.ENGINES)}")
    if not isinstance(items, list):
        raise SettingsError(422, "models: список")
    clean = _clean_models(items, engine)
    engine_mod._meta_set(  # noqa: SLF001
        conn, models_key(engine), json.dumps({"items": clean, "ts": now_ms}, ensure_ascii=False)
    )
    return {"engine": engine, "count": len(clean), "models_ts": now_ms}
