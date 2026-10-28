"""Переключатель движка (Ф16, docs/architecture.md; Ф78-2,
docs/architecture.md и 8).

Ф78-2: переключатель решает две вещи — мозг агента владельца и службу фактов `hub-facts`; движков четыре
(`claude`, `opencode`, `codex`, `cursor`). Рядом с движком — выбор модели и усилия ПО КАЖДОМУ движку (ключ
`engine_choice` в `main.meta`: `{"<движок>": {"model": str|null, "effort": str|null}, "ts": мс}`; `null` —
умолчание движка, `defaults`). Переключил туда-обратно — выбор вернулся. Пока записи нет, выбор каждый раз
выводится из старых ключей (П3): `opencode` — `owner_agent_model` (`{model, variant}`), `claude` —
`claude_model` из `facts.json` службы фактов (только если не умолчание). Код сам движок не переключает.

Один глобальный переключатель (решение владельца, план 11.22): он меняет ВСЁ, что запускает хаб сам;
частичной смеси нет (задачи по «Го» убраны в Ф78-1, оркестратор 9c — в Ф57-2; что решает переключатель
теперь — Ф78-2). Значение — ключ `session_engine` в `main.meta` (приём `core/remote.py`), нет строки —
`claude`; рядом `session_engine_ts` (когда переключили). Миграций нет.

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`. `engine_of_session` — единственная
функция хаба, которая определяет движок сессии по её данным (П15: живёт в ядре, чтобы ядро не зависело от
драйвера; `hub/engine/choice.py` её реэкспортирует). Пакет драйвера `hub.engine` здесь НЕ импортируется на
уровне модуля: умолчания, пути и модели движков читаются внутри функций. Исключение — профиль
движков `hub.engine.registry` (модуль без зависимостей; Ф74-2: свойства движков внешних сессий).

П18 (владелец 30.09): смена движка подписок НЕ трогает — подписанная сессия проекта может быть и Claude Code, и
OpenCode при любом положении переключателя. Причина `engine_switched` осталась только для старых строк.
"""

import json
import secrets
from pathlib import Path
from typing import NamedTuple

from ..engine import registry as engine_registry
from .audit import write_audit

ENGINES = ("claude", "opencode", "codex", "cursor")
DEFAULT_ENGINE = "claude"
META_ENGINE = "session_engine"
META_CHANGED = "session_engine_ts"
META_CHOICE = "engine_choice"  # Ф78-2: модель и усилие по каждому движку
META_LEGACY_AGENT = "owner_agent_model"  # Ф75-1: старый выбор модели агента (OpenCode)
# Ф78-2 (3.1): умолчания движков без своего источника. OpenCode — роль `agent` из `engine.json`, Cursor —
# `choice.CURSOR_DEFAULT_MODEL` (их берёт `defaults`).
CLAUDE_DEFAULT_MODEL = "sonnet"
CLAUDE_BELOW_MIN = frozenset({"haiku"})  # агенту владельца не предлагается (фактам можно — владелец 22.10)
# Владелец 22.10: фактам о людях по умолчанию — самая быстрая модель движка.
CLAUDE_FACTS_MODEL = "haiku"
CODEX_FACTS_MODEL = "gpt-6-luna"
CODEX_DEFAULT_MODEL = "gpt-6.1-sol"  # владелец 22.10: Codex — на gpt-6.1-sol
CODEX_DEFAULT_EFFORT = "low"
AUDIT_ACTION = "engine.set"
# Ф78-2 (раздел 12, слово владельца): у службы фактов свой выбор `{engine, model, effort, ts}`; нет записи —
# факты идут за выбором агента (верхние поля `GET /engine`).
META_FACTS = "facts_engine"
FACTS_AUDIT_ACTION = "facts_engine.set"
FACTS_ROLE = "facts"  # умолчание OpenCode для своего выбора фактов — роль `facts` из `engine.json`
END_REASON = "engine_switched"
SETUP_FILE = "engine.json"
# Идентификаторы сессий OpenCode — `ses_…` (F3), у Claude Code — UUID.
OC_SESSION_PREFIX = "ses_"

# П13: окно частичной смеси. OpenCode нельзя было выбрать, пока кусок 16-6 не поставил `True` — иначе владелец
# переключил бы хаб на движок, которым ещё нечего запускать. С 16-6 дежурная, задачи и оркестратор умеют оба
# движка; остаётся проверка `engine.json` (`hub engine setup`).
OPENCODE_READY = True

NOT_READY_TEXT = "OpenCode в хабе ещё не готов"
NO_SETUP_TEXT = "сначала hub engine setup"
KEEP = object()  # `set_engine`: поле не передано — прежний выбор движка


class EngineError(Exception):
    """Отказ ручки движка: `status` — HTTP-код, `detail` — текст ответа (без секретов)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class EngineChange(NamedTuple):
    """Итог `set_engine`: старый движок и новый (П18: подписок смена не трогает)."""

    old: str
    new: str


# --- meta -------------------------------------------------------------------------------------------------


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# --- отметка «паузу лимита снял владелец» (Ф66-3 F) -----------------------------------------------------------

META_PAUSE_CLEARED = "work_pause_cleared_ts"


def get_pause_cleared(conn) -> int:
    """Время (мс), когда владелец снял паузу лимита, — 0, если не снимал. Хост берёт его из снимка ожидания."""
    return _int_or_none(_meta_get(conn, META_PAUSE_CLEARED)) or 0


def set_pause_cleared(conn, now_ms: int) -> None:
    """Запись отметки; хост сверяет её с началом своей паузы. Ожидание хоста будит сама запись."""
    _meta_set(conn, META_PAUSE_CLEARED, str(int(now_ms)))


# --- движок -----------------------------------------------------------------------------------------------


def engine_of_session(kind: str | None, session_id: str | None) -> str:
    """Движок сессии по её данным: `kind == "opencode"` (реестр/плагин) или `session_id` вида `ses_…`; Ф74-2 —
    внешние Codex/Cursor: `kind` = имя движка или `session_id` вида `<движок>:…`. У сохранённой подписки
    движок — столбец `session_engine` (`subs.rec_engine`); этот разбор — для запросов и старых строк.
    Условная тройка сессии хаба (`hub:<id>`) движка не называет — её движок в записи `hub_session`."""
    if kind == "opencode":
        return "opencode"
    sid = session_id or ""
    if sid.startswith(OC_SESSION_PREFIX):
        return "opencode"
    ext = engine_registry.ext_engine_of(sid)
    if ext is not None:
        return ext
    if kind in engine_registry.EXTERNAL:
        return kind
    return DEFAULT_ENGINE


def many_convs(engine: str | None) -> bool:
    """Ф74-2 (Б2): один процесс движка держит много разговоров (профиль `many_convs_per_proc`)."""
    profile = engine_registry.get(engine)
    return bool(profile is not None and profile.many_convs_per_proc)


def idle_grace_ms(engine: str | None) -> int | None:
    """Ф74-2 (В2): срок брошенной подписки внешней сессии движка (`None` — живость только по процессу)."""
    profile = engine_registry.get(engine)
    return profile.idle_grace_ms if profile is not None else None


def ext_kind_error(kind: str | None, session_id: str | None) -> str | None:
    """Ф74-2: `kind` внешнего Codex/Cursor и приставка `session_id` (`<движок>:…`) — только вместе."""
    ext = engine_registry.ext_engine_of(session_id)
    if kind in engine_registry.EXTERNAL and ext != kind:
        return f"kind={kind} — только с session_id вида {kind}{engine_registry.EXT_SEP}<id разговора>"
    if ext is not None and kind is not None and kind != ext:
        return f"session_id {ext}{engine_registry.EXT_SEP}… — только с kind={ext}"
    return None


def get_engine(conn) -> str:
    """Текущий движок. Мусор в `main.meta` (например, после отката версии) — как отсутствие строки."""
    value = _meta_get(conn, META_ENGINE)
    return value if value in ENGINES else DEFAULT_ENGINE


def get_changed_ts(conn) -> int | None:
    return _int_or_none(_meta_get(conn, META_CHANGED))


def setup_path(config_dir: Path | None) -> Path:
    """`<config_dir>/engine.json` (П8: пишет `hub engine setup` из терминала владельца). Без `config_dir` —
    каталог конфига хаба из окружения."""
    if config_dir is None:
        from .. import paths  # здесь: ядро не тянет `paths` на уровне модуля

        config_dir = paths.config_dir()
    return config_dir / SETUP_FILE


def opencode_available(config_dir: Path | None = None) -> tuple[bool, str | None]:
    """Готов ли OpenCode к переключению и почему нет (П13). Текст — для UI и для отказа 409."""
    if not OPENCODE_READY:
        return False, NOT_READY_TEXT
    if not setup_path(config_dir).is_file():
        return False, NO_SETUP_TEXT
    return True, None


def _home() -> Path:
    return Path.home()


def available(engine: str, config_dir: Path | None = None) -> tuple[bool, str | None]:
    """Ф78-2 (раздел 2): можно ли выбрать движок на машине ядра и почему нет. `claude` — всегда (умолчание;
    нет бинаря или входа — скажет первый ход, П8); `opencode` — `engine.json` (П13, как было); `codex`,
    `cursor` — исполняемый файл найден (`choice.codex_path` / `choice.cursor_path`, секции `engine.json`)."""
    if engine == "claude":
        return True, None
    if engine == "opencode":
        return opencode_available(config_dir)
    from hub.engine import choice  # здесь: ядро не тянет пакет драйвера на уровне модуля

    if config_dir is None:
        # Ревью Ф83: как у OpenCode — без каталога из запроса берётся каталог машины (секции `engine.json`).
        from hub import paths

        config_dir = paths.config_dir_path()
    try:
        if engine == "codex":
            choice.codex_path(choice.load_codex(config_dir), _home())
        elif engine == "cursor":
            choice.cursor_path(choice.load_cursor(config_dir), _home())
        else:
            return False, "неизвестный движок"
    except choice.EngineUnavailable as exc:
        return False, str(exc)
    return True, None


def defaults(engine: str, config_dir: Path | None = None) -> dict:
    """Ф78-2 (3.1): `{"model", "effort"}` движка, когда владелец ничего не выбирал. `config_dir = None` — без
    машинных файлов (умолчания таблицы)."""
    from hub.engine import choice

    if engine == "opencode":
        try:
            overrides = choice.load_overrides(config_dir) if config_dir is not None else {}
            provider, model, variant = choice.role_model("agent", overrides)
        except choice.EngineUnavailable:
            provider, model, variant = choice.role_model("agent", {})
        return {"model": f"{provider}/{model}", "effort": variant}
    if engine == "codex":
        return {"model": CODEX_DEFAULT_MODEL, "effort": CODEX_DEFAULT_EFFORT}
    if engine == "cursor":
        return {"model": choice.CURSOR_DEFAULT_MODEL, "effort": choice.CURSOR_EFFORT_DEFAULT}
    return {"model": CLAUDE_DEFAULT_MODEL, "effort": None}


def _json_meta(conn, key: str) -> dict:
    raw = _meta_get(conn, key)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _str_or_none(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _legacy_claude_model(config_dir: Path | None) -> str | None:
    """П3: модель Claude службы фактов (`facts.json`, поле `claude_model`) — только записанная в файле и не
    умолчание (`sonnet` и так умолчание движка); ниже минимума (`haiku`) — тоже не выбор (владелец 22.10)."""
    if config_dir is None:
        return None
    stored = None
    value = _str_or_none(stored.get("claude_model")) if isinstance(stored, dict) else None
    return value if value != CLAUDE_DEFAULT_MODEL and value not in CLAUDE_BELOW_MIN else None


def remembered(conn, config_dir: Path | None = None) -> dict[str, dict]:
    """Выбор владельца по каждому движку `{движок: {"model", "effort"}}` (`None` — умолчание движка). Нет записи
    `engine_choice` — из старых ключей (П3: переводится при каждом чтении, пока нет записи)."""
    out: dict[str, dict] = {name: {"model": None, "effort": None} for name in ENGINES}
    stored = _json_meta(conn, META_CHOICE)
    if stored:
        for name in ENGINES:
            entry = stored.get(name)
            if isinstance(entry, dict):
                out[name] = {
                    "model": _str_or_none(entry.get("model")),
                    "effort": _str_or_none(entry.get("effort")),
                }
        return out
    legacy = _json_meta(conn, META_LEGACY_AGENT)
    model = _str_or_none(legacy.get("model"))
    if model is not None:
        out["opencode"] = {"model": model, "effort": _str_or_none(legacy.get("variant"))}
    out["claude"]["model"] = _legacy_claude_model(config_dir)
    return out


def choice_ts(conn) -> int | None:
    """Когда владелец последний раз менял модель или усилие (`engine_choice.ts`; до записи — `ts` старого
    ключа)."""
    stored = _json_meta(conn, META_CHOICE) or _json_meta(conn, META_LEGACY_AGENT)
    ts = stored.get("ts")
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else None


def get_choice(conn, engine: str | None = None, *, config_dir: Path | None = None) -> dict:
    """Ф78-2 (3.1): `{engine, model, effort}` — выбор движка (по умолчанию — текущего) с подставленным
    умолчанием: модель не бывает `None`; усилие `None` — только у движка без умолчания усилия (claude)."""
    name = engine if engine in ENGINES else get_engine(conn)
    own = remembered(conn, config_dir)[name]
    base = defaults(name, config_dir)
    return {"engine": name, "model": own["model"] or base["model"], "effort": own["effort"] or base["effort"]}


# --- модели движков ---------------------------------------------------------------------------------------


def _agent_models(conn, engine: str) -> list[dict]:
    """Список, присланный агентом владельца (`PUT /owner_channel/agent-models`), в форме `models_of`."""
    from . import owner_agent_model as agent_model_mod

    return [
        {"id": m["id"], "name": m["name"], "images": m["images"], "efforts": list(m["variants"])}
        for m in agent_model_mod.agent_models(conn, engine)["items"]
    ]


def models_of(conn, engine: str, *, role: str = "agent") -> list[dict]:
    """Ф78-2 (раздел 2, П4): модели движка `[{id, name, images, efforts}]` для роли `agent` или `facts`
    (у фактов Claude — и `haiku`, владелец 22.10). `opencode` — список агента;
    `codex` — живой список хоста (`engine_models`), нет его — список агента; `cursor` — семейства
    `choice.CURSOR_EFFORT_FAMILIES` с усилиями `choice.cursor_efforts` (живой список Cursor не собирается,
    П4);
    `claude` — подсказки профиля без `haiku` (владелец 22.10: «Claude минимум на Sonnet»). Пустой список — модель
    любая по правилу имени."""
    from . import engine_models as engine_models_mod

    if engine == "opencode":
        return _agent_models(conn, engine)
    if engine == "codex":
        live = engine_models_mod.load(conn).get("codex") or []
        if live:
            return [
                {"id": m["id"], "name": None, "images": True, "efforts": list(m["efforts"])} for m in live
            ]
        return _agent_models(conn, engine)
    if engine == "cursor":
        from hub.engine import choice

        return [
            {"id": family, "name": None, "images": True, "efforts": list(choice.cursor_efforts(family))}
            for family in choice.CURSOR_EFFORT_FAMILIES
        ]
    profile = engine_registry.get(engine)
    if engine == "claude" and profile is not None:
        efforts = list(profile.efforts)
        return [
            {"id": m, "name": None, "images": True, "efforts": efforts}
            for m in profile.model_hints
            if role == "facts" or m not in CLAUDE_BELOW_MIN
        ]
    return []


def check_choice(
    conn, engine: str, model, effort, *, config_dir: Path | None = None, role: str = "agent"
) -> tuple[str | None, str | None]:
    """Проверка выбора владельца для движка `engine` -> очищенные `(model, effort)`; отказ — `EngineError(422)`.
    Модель: правило имени (`owner_agent_model.check_model`); есть список движка — только из него, кроме
    `opencode` (у него список агента — подсказка: модели нет у сервера — агент сам уходит на запасную, Ф75-1).
    Усилие: из `efforts` модели в списке, иначе — из профиля движка; у OpenCode без списка — по правилу
    имени."""
    from . import owner_agent_model as agent_model_mod

    try:
        model = agent_model_mod.check_model(model, engine)
        effort = agent_model_mod.check_variant(effort, field="effort")
    except agent_model_mod.SettingsError as exc:
        raise EngineError(exc.status, exc.detail) from None
    listed = models_of(conn, engine, role=role)
    base = facts_defaults(engine, config_dir) if role == "facts" else defaults(engine, config_dir)
    wanted = model if model is not None else base["model"]
    entry = next((m for m in listed if m["id"] == wanted), None)
    if model is not None and listed and entry is None and engine != "opencode":
        names = ", ".join(m["id"] for m in listed[:20])
        raise EngineError(422, f"model: у движка {engine} нет «{model}» (есть: {names})")
    if effort is not None:
        allowed = list(entry["efforts"]) if entry is not None and entry["efforts"] else None
        if allowed is None and engine != "opencode":
            profile = engine_registry.get(engine)
            allowed = list(profile.efforts) if profile is not None else None
        if allowed is not None and effort not in allowed:
            raise EngineError(422, f"effort: у движка {engine} — {', '.join(allowed)}")
    return model, effort


def write_choice(
    conn, engine: str, model: str | None, effort: str | None, *, now_ms: int, config_dir: Path | None
) -> None:
    """Записать выбор движка (уже проверенный `check_choice`); остальные движки — как были (в том числе
    выведенные из старых ключей: первая запись их закрепляет)."""
    value: dict = {name: dict(entry) for name, entry in remembered(conn, config_dir).items()}
    value[engine] = {"model": model, "effort": effort}
    value["ts"] = now_ms
    _meta_set(conn, META_CHOICE, json.dumps(value, ensure_ascii=False, sort_keys=True))


def status(conn, *, config_dir: Path | None = None) -> dict:
    """Тело `GET /api/v1/engine` и `GET /internal/v1/admin/engine` (Ф78-2, 3.2): текущий движок с моделью и
    усилием (умолчание подставлено), по каждому движку — доступность, модели, усилия, умолчания и выбор владельца
    (`model`/`effort`, `null` — умолчание). Таблицы ролей больше нет (она была про убранные задачи)."""
    oc_ok, oc_reason = opencode_available(config_dir)
    current = get_choice(conn, config_dir=config_dir)
    own = remembered(conn, config_dir)
    engines = []
    for name in ENGINES:
        ok, reason = available(name, config_dir)
        profile = engine_registry.get(name)
        base = defaults(name, config_dir)
        facts_base = facts_defaults(name, config_dir)
        engines.append(
            {
                "name": name,
                "label": engine_registry.label(name),
                "available": ok,
                "reason": reason,
                "models": models_of(conn, name),
                "facts_models": models_of(conn, name, role="facts"),
                "facts_default_model": facts_base["model"],
                "facts_default_effort": facts_base["effort"],
                "efforts": list(profile.efforts) if profile is not None else [],
                "default_model": base["model"],
                "default_effort": base["effort"],
                "model": own[name]["model"],
                "effort": own[name]["effort"],
            }
        )
    body = {
        "engine": current["engine"],
        "model": current["model"],
        "effort": current["effort"],
        "changed_ts": get_changed_ts(conn),
        "choice_ts": choice_ts(conn),
        "engines": engines,
        # Совместимость: старый интерфейс (до выкладки 78-2c) читает эти два поля.
        "opencode_available": oc_ok,
        "opencode_reason": oc_reason,
        # Ф78-2 (раздел 12): свой выбор службы фактов; записи нет — тот же, что у агента.
        "facts": facts_choice(conn, config_dir=config_dir),
    }
    # Ф83-1 (П10): запасные — новыми полями; прежние поля и блок `facts` — основной выбор, как были.
    from . import engine_fallback  # noqa: PLC0415 — модуль опирается на этот

    extra = engine_fallback.status(conn, config_dir=config_dir)
    body["facts"].update(extra.pop("facts_extra"))
    body.update(extra)
    return body


# --- запись (внутри Db.write) -------------------------------------------------------------------------------


def set_engine(
    conn,
    engine: str,
    *,
    now_ms: int,
    actor: str,
    config_dir: Path | None = None,
    model=KEEP,
    effort=KEEP,
) -> EngineChange:
    """3.6 одной транзакцией: движок и `changed_ts`, выбор модели и усилия движка (Ф78-2: не передано —
    прежний выбор этого движка, `None` — умолчание), аудит `engine.set` (П18: подписки не трогает). Ничего
    не изменилось — без записи и без аудита. Отказ — `EngineError` (422 — негодное, 409 — движок
    недоступен)."""
    if engine not in ENGINES:
        raise EngineError(422, f"engine: {' или '.join(ENGINES)}")
    old = get_engine(conn)
    if engine != old:
        ok, reason = available(engine, config_dir)
        if not ok:
            raise EngineError(409, reason or NOT_READY_TEXT)
    before = remembered(conn, config_dir)[engine]
    new_model = before["model"] if model is KEEP else model
    new_effort = before["effort"] if effort is KEEP else effort
    choice_changed = False
    if model is not KEEP or effort is not KEEP:
        new_model, new_effort = check_choice(conn, engine, new_model, new_effort, config_dir=config_dir)
        choice_changed = (new_model, new_effort) != (before["model"], before["effort"])
    if engine == old and not choice_changed:
        return EngineChange(old, old)
    if engine != old:
        _meta_set(conn, META_ENGINE, engine)
        _meta_set(conn, META_CHANGED, str(now_ms))
        # Ф83-1 (П10): смена движка основного снимает «на запасном» агента — и фактов, идущих за агентом.
        from . import engine_fallback  # noqa: PLC0415

        engine_fallback.clear_on(conn, "agent")
        if facts_stored(conn) is None:
            engine_fallback.clear_on(conn, "facts")
    if choice_changed:
        write_choice(conn, engine, new_model, new_effort, now_ms=now_ms, config_dir=config_dir)
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=None,
        client_name=None,
        request_id=secrets.token_hex(16),
        action=AUDIT_ACTION,
        result="ok",
        detail={"old": old, "new": engine, "model": new_model, "effort": new_effort},
    )
    return EngineChange(old, engine)


# --- выбор службы фактов (Ф78-2, раздел 12) -----------------------------------------------------------------


def facts_defaults(engine: str, config_dir: Path | None = None) -> dict:
    """Умолчание своего выбора фактов — самая быстрая модель движка (владелец 22.10): у OpenCode — роль `facts` из
    `engine.json` (не `agent`), у Claude — `haiku`, у Codex — `gpt-6-luna`, у Cursor — как `defaults`."""
    if engine == "claude":
        return {"model": CLAUDE_FACTS_MODEL, "effort": None}
    if engine == "codex":
        return {"model": CODEX_FACTS_MODEL, "effort": CODEX_DEFAULT_EFFORT}
    if engine != "opencode":
        return defaults(engine, config_dir)
    from hub.engine import choice

    try:
        overrides = choice.load_overrides(config_dir) if config_dir is not None else {}
        provider, model, variant = choice.role_model(FACTS_ROLE, overrides)
    except choice.EngineUnavailable:
        provider, model, variant = choice.role_model(FACTS_ROLE, {})
    return {"model": f"{provider}/{model}", "effort": variant}


def facts_stored(conn) -> dict | None:
    """Запись `facts_engine` `{engine, model, effort, ts}` (`model`/`effort` — `None`: умолчание движка) или
    `None` — записи нет (или мусор: неизвестный движок), факты за агентом."""
    stored = _json_meta(conn, META_FACTS)
    engine = stored.get("engine")
    if engine not in ENGINES:
        return None
    ts = stored.get("ts")
    return {
        "engine": engine,
        "model": _str_or_none(stored.get("model")),
        "effort": _str_or_none(stored.get("effort")),
        "ts": ts if isinstance(ts, int) and not isinstance(ts, bool) else None,
    }


def facts_choice(conn, *, config_dir: Path | None = None) -> dict:
    """Блок `facts` в `GET /engine`: `{engine, model, effort, follows_agent, ts}` с подставленным
    умолчанием (модель не бывает `None`). Записи нет — выбор агента (`get_choice`), `follows_agent = True`,
    `ts = None`."""
    own = facts_stored(conn)
    if own is None:
        agent = get_choice(conn, config_dir=config_dir)
        return {**agent, "follows_agent": True, "ts": None}
    base = facts_defaults(own["engine"], config_dir)
    return {
        "engine": own["engine"],
        "model": own["model"] or base["model"],
        "effort": own["effort"] or base["effort"],
        "follows_agent": False,
        "ts": own["ts"],
    }


def set_facts_engine(
    conn,
    engine: str | None,
    *,
    now_ms: int,
    actor: str,
    config_dir: Path | None = None,
    model=KEEP,
    effort=KEEP,
) -> bool:
    """Записать свой выбор фактов (внутри `Db.write()`); `engine = None` — снять запись (снова за агентом).
    Поле не передано (`KEEP`) — прежнее значение записи фактов, если в ней тот же движок, иначе `None`
    (умолчание движка). Проверка — `check_choice` (тот же список моделей и минимум Sonnet у Claude), движок
    недоступен — 409 (кроме того, на котором факты уже идут). Ничего не изменилось — без записи и аудита;
    иначе аудит `facts_engine.set` `{old, new, model, effort}` (`old`/`new` — движок записи или `None`).
    Возвращает, было ли изменение."""
    before = facts_stored(conn)
    old_engine = before["engine"] if before is not None else None
    main_before = facts_choice(conn, config_dir=config_dir)["engine"]
    if engine is None:
        if before is None:
            return False
        conn.execute("DELETE FROM main.meta WHERE key = ?", (META_FACTS,))
        new_model = new_effort = None
    else:
        if engine not in ENGINES:
            raise EngineError(422, f"engine: {' или '.join(ENGINES)}")
        if engine != facts_choice(conn, config_dir=config_dir)["engine"]:
            ok, reason = available(engine, config_dir)
            if not ok:
                raise EngineError(409, reason or NOT_READY_TEXT)
        same = before is not None and before["engine"] == engine
        new_model = (before["model"] if same else None) if model is KEEP else model
        new_effort = (before["effort"] if same else None) if effort is KEEP else effort
        new_model, new_effort = check_choice(
            conn, engine, new_model, new_effort, config_dir=config_dir, role="facts"
        )
        if same and (new_model, new_effort) == (before["model"], before["effort"]):
            return False
        value = {"engine": engine, "model": new_model, "effort": new_effort, "ts": now_ms}
        _meta_set(conn, META_FACTS, json.dumps(value, ensure_ascii=False, sort_keys=True))
    if facts_choice(conn, config_dir=config_dir)["engine"] != main_before:
        # Ф83-1 (П10): смена движка основного фактов снимает «на запасном» фактов.
        from . import engine_fallback  # noqa: PLC0415

        engine_fallback.clear_on(conn, "facts")
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=None,
        client_name=None,
        request_id=secrets.token_hex(16),
        action=FACTS_AUDIT_ACTION,
        result="ok",
        detail={"old": old_engine, "new": engine, "model": new_model, "effort": new_effort},
    )
    return True
