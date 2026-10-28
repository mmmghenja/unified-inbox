"""Запасные движки (Ф83-1, docs/architecture.md: слова владельца — 3а, поправки критика
П1–П13 — 3г; спека docs/architecture.md).

Агент владельца и служба фактов (ключи `main.meta`, П3; `engine_choice` и `facts_engine` не трогаются):
- `agent_fallback`, `facts_fallback` — запасной `{engine, model, effort, ts}` (`model`/`effort` `None` —
  умолчание движка для роли). Нет записи у фактов, которые идут «как у агента», — запасной агента;
- `agent_on_fallback`, `facts_on_fallback` — состояние «на запасном» `{main, kind, reason, since}`; ставит
  отказ основного (`failed`, повод — `failure.BACKGROUND_REASONS`), снимает только владелец (`return_main`) или
  смена движка основного (`clear_on`). Без мерцания: обратно сам не переходит.

Сессии хаба (столбцы 0108_main.sql): `hub_session.fallback_json` (`None` — как у проекта, `{"engine": null}` —
нет, иначе свой), `project.session_fallback_json` (умолчание проекта), `hub_session.on_fallback_json` —
пометка «на запасном» `{main, kind, reason, since, by}`. Переход — «Новая с передачей дел» (`handoff.handoff`)
на запасной движок с его моделью и усилием (П8); без цепочек (П1): пометка стоит — второго перехода нет,
запасной — всегда другой движок (П2). Повод у сессий — только `limit` и `balance` (П4).

Паузы движков хоста — ключ `work_host_pauses` (`{движок: {until, kind, since}}`, мс): хост шлёт их в каждом
`/work/host/wait` (П9); «Снять паузу лимита» владельца (`engine.get_pause_cleared`) гасит записи не новее отметки.
У OpenCode ключ — `opencode:<провайдер>` (`failure.pause_key`): пауза провайдера держит только сессии на его
моделях; запись без провайдера (старый хост) держит все сессии движка.

Функции записи — только внутри `Db.write()`, время — параметром `now_ms`. Отказы — `EngineError` (как у
`core/engine.py`) и `Refused` (переход не состоялся — текст для строки в окне сессии)."""

import json
import secrets
from datetime import datetime
from pathlib import Path

from hub.engine import failure
from hub.engine import registry as engine_registry

from . import engine as engine_mod
from . import held as held_mod
from .audit import write_audit
from .hub_sessions import SessionError

WHO = ("agent", "facts")
META_SETTING = {"agent": "agent_fallback", "facts": "facts_fallback"}
META_ON = {"agent": "agent_on_fallback", "facts": "facts_on_fallback"}
META_HOST_PAUSES = "work_host_pauses"
AUDIT_SET = "engine_fallback.set"
AUDIT_SWITCH = "engine_fallback.switch"
AUDIT_RETURN = "engine_fallback.return"
AUDIT_SESSION_SET = "hub_session_fallback"
AUDIT_SESSION_SWITCH = "hub_session_fallback_switch"
AUDIT_SESSION_RETURN = "hub_session_fallback_return"
AUDIT_PROJECT_SET = "project_session_fallback"
REASON_MAX = 300
WHO_LABEL = {"agent": "Агент владельца", "facts": "Факты о людях"}
BY = ("auto", "owner_channel")


class Refused(Exception):
    """Переход сессии на запасной не состоялся: `why` — готовый текст (строка в окне сессии, ответ ручки)."""

    def __init__(self, why: str, code: str = "refused") -> None:
        super().__init__(why)
        self.why = why
        self.code = code


# --- мелочи -------------------------------------------------------------------------------------------------


def _str(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _ts(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _hhmm(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M")


def _loads(raw) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    return value if isinstance(value, dict) else None


def _dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clean_reason(reason) -> str | None:
    text = " ".join(str(reason or "").split())
    return text[:REASON_MAX] or None


def choice_label(choice: dict | None) -> str:
    """«OpenCode · deepseek/deepseek-flash» (усилие — через « · », если есть)."""
    if not choice:
        return "—"
    parts = [engine_registry.label(choice.get("engine"))]
    if choice.get("model"):
        parts.append(str(choice["model"]))
    if choice.get("effort"):
        parts.append(str(choice["effort"]))
    return " · ".join(parts)


def _triple(engine: str, model, effort) -> dict:
    return {"engine": engine, "model": _str(model), "effort": _str(effort)}


def _main_of(state: dict) -> dict | None:
    main = state.get("main")
    if not isinstance(main, dict) or not isinstance(main.get("engine"), str):
        return None
    return _triple(main["engine"], main.get("model"), main.get("effort"))


# --- агент владельца и факты: настройка ------------------------------------------------------------------------


def _check_who(who: str) -> None:
    if who not in WHO:
        raise engine_mod.EngineError(404, "not found")


def setting(conn, who: str) -> dict | None:
    """Запись запасного `{engine, model, effort, ts}` (`model`/`effort` `None` — умолчание движка) или `None`
    (нет записи, мусор)."""
    stored = engine_mod._json_meta(conn, META_SETTING[who])  # noqa: SLF001
    engine = stored.get("engine")
    if engine not in engine_mod.ENGINES:
        return None
    return {**_triple(engine, stored.get("model"), stored.get("effort")), "ts": _ts(stored.get("ts"))}


def _facts_follow(conn) -> bool:
    return engine_mod.facts_stored(conn) is None


def _role_defaults(who: str, engine: str, config_dir: Path | None) -> dict:
    return (
        engine_mod.facts_defaults(engine, config_dir)
        if who == "facts"
        else engine_mod.defaults(engine, config_dir)
    )


def resolved(conn, who: str, *, config_dir: Path | None = None) -> dict | None:
    """Запасной с подставленным умолчанием модели `{engine, model, effort}` или `None`. Факты без своей записи
    запасного, идущие «как у агента», — запасной агента (умолчания — агента)."""
    own = setting(conn, who)
    whose = who
    if own is None and who == "facts" and _facts_follow(conn):
        own, whose = setting(conn, "agent"), "agent"
    if own is None:
        return None
    base = _role_defaults(whose, own["engine"], config_dir)
    return {
        "engine": own["engine"],
        "model": own["model"] or base["model"],
        "effort": own["effort"] or base["effort"],
    }


def main_choice(conn, who: str, *, config_dir: Path | None = None) -> dict:
    """Основной выбор `{engine, model, effort}` (П10: как его отдают прежние поля `GET /engine`)."""
    if who == "agent":
        return engine_mod.get_choice(conn, config_dir=config_dir)
    facts = engine_mod.facts_choice(conn, config_dir=config_dir)
    return {"engine": facts["engine"], "model": facts["model"], "effort": facts["effort"]}


def on_state(conn, who: str) -> dict | None:
    """Состояние «на запасном» `{main, kind, reason, since}` или `None`."""
    stored = engine_mod._json_meta(conn, META_ON[who])  # noqa: SLF001
    since = _ts(stored.get("since"))
    if since is None:
        return None
    main = _main_of(stored)
    return {
        "main": main,
        "kind": stored.get("kind") if stored.get("kind") in failure.KINDS else "other",
        "reason": _str(stored.get("reason")),
        "since": since,
    }


def effective(conn, who: str, *, config_dir: Path | None = None) -> dict:
    """Действующий выбор (П10): на запасном — запасной; факты «как у агента» — действующий агента."""
    if on_state(conn, who) is not None:
        fb = resolved(conn, who, config_dir=config_dir)
        if fb is not None:
            return fb
    if who == "facts" and _facts_follow(conn):
        return effective(conn, "agent", config_dir=config_dir)
    return main_choice(conn, who, config_dir=config_dir)


def status(conn, *, config_dir: Path | None = None) -> dict:
    """Новые поля `GET /engine` (П10): верх — агент, `facts` — факты. Прежние поля не меняются."""
    return {
        "agent_fallback": _public_setting(setting(conn, "agent")),
        "facts_fallback": _public_setting(setting(conn, "facts")),
        "effective": effective(conn, "agent", config_dir=config_dir),
        "on_fallback": on_state(conn, "agent"),
        "fallback": resolved(conn, "agent", config_dir=config_dir),
        "facts_extra": {
            "effective": effective(conn, "facts", config_dir=config_dir),
            "on_fallback": on_state(conn, "facts"),
            "fallback": resolved(conn, "facts", config_dir=config_dir),
        },
    }


def _public_setting(value: dict | None) -> dict | None:
    return None if value is None else {k: value[k] for k in ("engine", "model", "effort", "ts")}


def set_fallback(
    conn,
    who: str,
    engine: str | None,
    *,
    model=None,
    effort=None,
    now_ms: int,
    actor: str,
    config_dir: Path | None = None,
) -> bool:
    """Записать запасной (`engine = None` — снять; тогда снимается и «на запасном»). Проверки: движок из
    `engine.ENGINES`, не основной (П2; у агента — Ф85-1: не совпадает с основным целиком) — 422, недоступен —
    409, модель и усилие — `check_choice` роли.
    Ничего не изменилось — без записи и аудита. Возвращает, было ли изменение."""
    _check_who(who)
    before = setting(conn, who)
    if engine is None:
        if before is None:
            return False
        conn.execute("DELETE FROM main.meta WHERE key = ?", (META_SETTING[who],))
        conn.execute("DELETE FROM main.meta WHERE key = ?", (META_ON[who],))
        new_model = new_effort = None
    else:
        if engine not in engine_mod.ENGINES:
            raise engine_mod.EngineError(422, f"engine: {' или '.join(engine_mod.ENGINES)}")
        main = main_choice(conn, who, config_dir=config_dir)
        if who != "agent" and engine == main["engine"]:
            raise engine_mod.EngineError(422, "engine: запасной — другой движок, не основной")
        ok, reason = engine_mod.available(engine, config_dir)
        if not ok:
            raise engine_mod.EngineError(409, reason or engine_mod.NOT_READY_TEXT)
        role = "facts" if who == "facts" else "agent"
        new_model, new_effort = engine_mod.check_choice(
            conn, engine, model, effort, config_dir=config_dir, role=role
        )
        if who == "agent" and engine == main["engine"]:
            # Ф85-1 (C.3): у агента запасной — тот же движок с другой моделью или усилием; 422 — только если
            # совпадает с основным целиком (умолчания роли подставлены, как у `resolved`).
            base = _role_defaults("agent", engine, config_dir)
            fb = {
                "engine": engine,
                "model": new_model or base["model"],
                "effort": new_effort or base["effort"],
            }
            if _same_as_main("agent", fb, main):
                raise engine_mod.EngineError(
                    422, "engine: запасной совпадает с основным — нужна другая модель"
                )
        if before is not None and (before["engine"], before["model"], before["effort"]) == (
            engine,
            new_model,
            new_effort,
        ):
            return False
        value = {"engine": engine, "model": new_model, "effort": new_effort, "ts": now_ms}
        engine_mod._meta_set(conn, META_SETTING[who], _dumps(value))  # noqa: SLF001
    _audit(
        conn,
        now_ms,
        actor,
        AUDIT_SET,
        {
            "who": who,
            "old": before["engine"] if before else None,
            "new": engine,
            "model": new_model,
            "effort": new_effort,
        },
    )
    return True


def _audit(conn, now_ms: int, actor: str, action: str, detail: dict, token_id: int | None = None) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=token_id,
        client_name=None,
        request_id=secrets.token_hex(16),
        action=action,
        result="ok",
        detail=detail,
    )


def failed(
    conn,
    who: str,
    *,
    kind: str,
    reason=None,
    now_ms: int,
    actor: str,
    token_id: int | None = None,
    config_dir: Path | None = None,
) -> dict:
    """Отказ основного (сообщает агент или служба фактов): `{switched, why?, on_fallback, effective,
    notify?}`. Переход — если `kind` — повод (`failure.BACKGROUND_REASONS`; у агента —
    `AGENT_REASONS`, Ф85-1), запасной задан, другой движок (П1/П2; у агента — Ф85-1: не совпадает с
    основным целиком), доступен, а состояния ещё нет; иначе `switched: false` и `why` — почему."""
    _check_who(who)
    if kind not in failure.KINDS:
        raise engine_mod.EngineError(422, f"kind: {' | '.join(failure.KINDS)}")

    def _no(why: str) -> dict:
        return {
            "switched": False,
            "why": why,
            "on_fallback": on_state(conn, who),
            "effective": effective(conn, who, config_dir=config_dir),
        }

    if on_state(conn, who) is not None:
        return _no("уже на запасном — обратно переводит только владелец")
    reasons = failure.AGENT_REASONS if who == "agent" else failure.BACKGROUND_REASONS
    if kind not in reasons:
        return _no(f"не повод для запасного: {failure.label(kind)}")
    fb = resolved(conn, who, config_dir=config_dir)
    if fb is None:
        return _no("запасной не задан")
    main = main_choice(conn, who, config_dir=config_dir)
    if _same_as_main(who, fb, main):
        return _no(_SAME_TEXT[who])
    if who == "facts" and _facts_follow(conn) and on_state(conn, "agent") is not None:
        return _no("факты уже на запасном вместе с агентом")
    ok, unavailable = engine_mod.available(fb["engine"], config_dir)
    if not ok:
        return _no(f"запасной недоступен: {unavailable}")
    state = {"main": main, "kind": kind, "reason": clean_reason(reason), "since": now_ms}
    engine_mod._meta_set(conn, META_ON[who], _dumps(state))  # noqa: SLF001
    _audit(
        conn,
        now_ms,
        actor,
        AUDIT_SWITCH,
        {"who": who, "kind": kind, "main": main["engine"], "fallback": fb["engine"]},
        token_id,
    )
    text = f"{engine_registry.label(main['engine'])} — {failure.label(kind)}, работаю на {choice_label(fb)}"
    return {
        "switched": True,
        "on_fallback": on_state(conn, who),
        "effective": fb,
        "notify": (f"Хаб: {WHO_LABEL[who]} на запасном", text),
    }


_SAME_TEXT = {
    "agent": "запасной совпадает с основным (движок, модель и усилие)",
    "facts": "запасной — тот же движок, что основной",
}


def _same_as_main(who: str, fb: dict, main: dict) -> bool:
    """Запасной не годится (П1/П2 Ф83): у фактов — тот же движок; у агента владельца (Ф85-1, C.3) — тот же движок,
    модель и усилие (тот же движок с другой моделью — законный запасной)."""
    if fb["engine"] != main["engine"]:
        return False
    if who != "agent":
        return True
    return (fb.get("model"), fb.get("effort")) == (main.get("model"), main.get("effort"))


def return_main(conn, who: str, *, now_ms: int, actor: str) -> bool:
    """«Вернуть на основной» (только владелец): снять состояние. Не было — `False`, без аудита."""
    _check_who(who)
    state = on_state(conn, who)
    if state is None:
        return False
    conn.execute("DELETE FROM main.meta WHERE key = ?", (META_ON[who],))
    _audit(conn, now_ms, actor, AUDIT_RETURN, {"who": who, "since": state["since"]})
    return True


def clear_on(conn, who: str) -> None:
    """Смена движка основного снимает «на запасном» (П10). Без аудита: аудит пишет сама смена."""
    conn.execute("DELETE FROM main.meta WHERE key = ?", (META_ON[who],))


# --- паузы движков хоста (П9) ----------------------------------------------------------------------------


def parse_host_pauses(raw) -> dict[str, dict] | None:
    """`pauses` из `/work/host/wait` (JSON `{движок: {until, kind, since}}`) -> словарь; мусор — `None`
    (запись не трогается)."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    if not isinstance(value, dict):
        return None
    out: dict[str, dict] = {}
    for engine, entry in value.items():
        if engine_registry.held(failure.key_engine(engine)) is None or not isinstance(entry, dict):
            continue
        until, since = _ts(entry.get("until")), _ts(entry.get("since"))
        if until is None:
            continue
        kind = entry.get("kind")
        out[engine] = {
            "until": until,
            "kind": kind if kind in failure.KINDS else "other",
            "since": since or 0,
        }
    return out


def same_host_pauses(conn, pauses: dict[str, dict]) -> bool:
    return engine_mod._json_meta(conn, META_HOST_PAUSES) == pauses  # noqa: SLF001


def set_host_pauses(conn, pauses: dict[str, dict]) -> bool:
    """Записать паузы хоста, если отличаются от записанных. Возвращает, была ли запись."""
    if engine_mod._json_meta(conn, META_HOST_PAUSES) == pauses:  # noqa: SLF001
        return False
    engine_mod._meta_set(conn, META_HOST_PAUSES, _dumps(pauses))  # noqa: SLF001
    return True


def host_pauses(conn, now_ms: int) -> dict[str, dict]:
    """Действующие паузы движков: срок не вышел и начаты позже отметки «паузу снял владелец»."""
    cleared = engine_mod.get_pause_cleared(conn)
    out: dict[str, dict] = {}
    for engine, entry in (parse_host_pauses(engine_mod._meta_get(conn, META_HOST_PAUSES)) or {}).items():  # noqa: SLF001
        if entry["until"] > now_ms and entry["since"] > cleared:
            out[engine] = entry
    return out


def pause_of(conn, engine: str, now_ms: int, *, model=None, config_dir: Path | None = None) -> dict | None:
    """Действующая пауза, которая держит сессию движка `engine` с моделью `model`: у OpenCode — запись
    провайдера модели (`opencode:<провайдер>`; модели нет — провайдер модели по умолчанию) или запись без
    провайдера; из подходящих — с дальним сроком."""
    pauses = host_pauses(conn, now_ms)
    keys = failure.pause_keys(engine, session_provider(engine, model, config_dir))
    found = [pauses[k] for k in keys if k in pauses]
    return max(found, key=lambda p: p["until"]) if found else None


# Роль OpenCode сессий хаба (`work.held.OC_ROLE`): её модель — у сессии без своей.
OC_SESSION_ROLE = "project"


def session_provider(engine: str, model, config_dir: Path | None = None) -> str | None:
    """Провайдер модели сессии для ключа паузы: из `провайдер/модель`; у OpenCode без своей модели — модель
    роли по умолчанию (`engine.json` машины ядра); не вышло или движок без провайдеров — `None`."""
    if engine not in failure.PROVIDER_ENGINES:
        return None
    provider = failure.model_provider(engine, model)
    if provider is not None:
        return provider
    from hub.engine import choice  # noqa: PLC0415 — ядро не тянет пакет драйвера на уровне модуля

    if config_dir is None:
        from hub import paths  # noqa: PLC0415

        config_dir = paths.config_dir_path()
    try:
        return choice.role_model(OC_SESSION_ROLE, choice.load_overrides(config_dir))[0]
    except choice.EngineUnavailable, OSError:
        return None


def paused_text(engine: str, pause: dict) -> str:
    return f"{engine_registry.label(engine)} на паузе до {_hhmm(pause['until'])}"


# --- сессии хаба: настройка ------------------------------------------------------------------------------


def parse_session_value(value) -> tuple[str, dict | None]:
    """`fallback_json` сессии -> (`inherit` | `none` | `own`, запасной или `None`)."""
    if value is None:
        return "inherit", None
    stored = value if isinstance(value, dict) else _loads(value)
    if stored is None:
        return "inherit", None
    engine = stored.get("engine")
    if engine is None:
        return "none", None
    if engine_registry.held(engine) is None:
        return "inherit", None
    return "own", _triple(engine, stored.get("model"), stored.get("effort"))


def project_value(conn, project_id: int) -> dict | None:
    row = conn.execute(
        "SELECT session_fallback_json FROM main.project WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    how, value = parse_session_value(row[0])
    return value if how == "own" else None


def check_session_value(body, *, current_engine: str | None) -> dict | None:
    """Тело настройки запасного сессии или проекта -> что записать (`None` — снять/как у проекта,
    `{"engine": None}` — нет запасного, иначе `{engine, model, effort}`). 422 — `SessionError`."""
    if body is None:
        return None
    if not isinstance(body, dict) or set(body) - {"engine", "model", "effort"}:
        raise SessionError(422, "fallback: {engine, model?, effort?} или null")
    engine = body.get("engine")
    if engine is None:
        if body.get("model") is not None or body.get("effort") is not None:
            raise SessionError(422, "fallback: без движка модели и усилия нет")
        return {"engine": None}
    if not isinstance(engine, str) or engine_registry.held(engine) is None:
        raise SessionError(422, f"fallback.engine: {' или '.join(held_mod.ENGINES)}")
    if current_engine is not None and engine == current_engine:
        raise SessionError(422, "fallback.engine: запасной — другой движок, не тот, на котором сессия")
    from .hub_sessions import _clean  # noqa: PLC0415 — та же проверка строк, что у модели сессии

    for name in ("model", "effort"):
        if body.get(name) is not None and not isinstance(body.get(name), str):
            raise SessionError(422, f"fallback.{name}: строка или null")
    return {
        "engine": engine,
        "model": _clean(body.get("model"), "fallback.model"),
        "effort": _clean(body.get("effort"), "fallback.effort"),
    }


def set_session_value(conn, hub_session_id: int, body, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """`PATCH /sessions/{id}` `fallback`: `null` — как у проекта, `{"engine": null}` — нет, иначе свой.
    Закрытая — 409. Модель и пометку «на запасном» не трогает (П13)."""
    from . import hub_sessions  # noqa: PLC0415

    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    value = check_session_value(body, current_engine=rec.engine)
    raw = None if value is None else _dumps(value)
    if raw != rec.fallback_json:
        hub_sessions.touch(conn, rec.id, now_ms, fallback_json=raw)
        write_audit(
            conn,
            now_ms=now_ms,
            action=AUDIT_SESSION_SET,
            result="ok",
            detail={"hub_session_id": rec.id, "old": _loads(rec.fallback_json), "new": value},
            **audit,
        )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def set_project_value(conn, project_id: int, body, *, now_ms: int, audit: dict) -> None:
    """`PATCH /projects/{id}` `session_fallback`: `null` или `{"engine": null}` — снять умолчание проекта."""
    value = check_session_value(body, current_engine=None)
    raw = None if value is None or value.get("engine") is None else _dumps(value)
    row = conn.execute(
        "SELECT session_fallback_json FROM main.project WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None or row[0] == raw:
        return
    conn.execute("UPDATE main.project SET session_fallback_json = ? WHERE id = ?", (raw, project_id))
    write_audit(
        conn,
        now_ms=now_ms,
        action=AUDIT_PROJECT_SET,
        result="ok",
        detail={"project_id": project_id, "old": _loads(row[0]), "new": _loads(raw)},
        **audit,
    )


def marker(rec: held_mod.HubSession) -> dict | None:
    """Пометка «на запасном» сессии `{main, kind, reason, since, by}` или `None`."""
    stored = _loads(rec.on_fallback_json)
    if stored is None or _ts(stored.get("since")) is None:
        return None
    return {
        "main": _main_of(stored),
        "kind": stored.get("kind") if stored.get("kind") in failure.KINDS else "other",
        "reason": _str(stored.get("reason")),
        "since": stored["since"],
        "by": stored.get("by") if stored.get("by") in BY else "auto",
    }


def target(conn, rec: held_mod.HubSession) -> dict | None:
    """Куда переходить `{engine, model, effort, source: session|project}` или `None`: запасного нет, он
    того же движка (П2) или сессия уже на запасном (П1)."""
    if marker(rec) is not None:
        return None
    how, value = parse_session_value(rec.fallback_json)
    source = "session"
    if how == "none":
        return None
    if how == "inherit":
        value, source = project_value(conn, rec.project_id), "project"
    if value is None or value["engine"] == rec.engine:
        return None
    return {**value, "source": source}


def session_fields(conn, rec: held_mod.HubSession) -> dict:
    """Поля выдачи сессии (все три формы `session_json`): своя настройка, куда перейдёт, пометка."""
    how, value = parse_session_value(rec.fallback_json)
    own = None if how == "inherit" else (value if how == "own" else {"engine": None})
    on = marker(rec)
    return {
        "fallback": own,
        "fallback_target": target(conn, rec),
        "on_fallback": on,
        "on_fallback_text": marker_text(on, rec) if on is not None else None,
    }


def marker_text(on: dict, rec: held_mod.HubSession) -> str:
    """«на запасном: OpenCode · deepseek/… — лимит Claude Code с 14:20» (3а)."""
    here = choice_label({"engine": rec.engine, "model": rec.model, "effort": rec.effort})
    main = on.get("main") or {}
    cause = f"{failure.label(on.get('kind'))} {engine_registry.label(main.get('engine'))}".strip()
    by = "" if on.get("by") == "auto" else " (перевёл владелец)"
    return f"на запасном: {here} — {cause} с {_hhmm(on['since'])}{by}"


def offer(conn, rec: held_mod.HubSession, now_ms: int, *, config_dir: Path | None = None) -> dict | None:
    """Ответ «Перейти на …» у карточки паузы (П6): сессия держится паузой своего движка с поводом
    (`SESSION_REASONS`), запасной задан, другой движок, доступен; пометки нет."""
    goal = target(conn, rec)
    if goal is None:
        return None
    pause = pause_of(conn, rec.engine, now_ms, model=rec.model, config_dir=config_dir)
    if pause is None or pause["kind"] not in failure.SESSION_REASONS:
        return None
    if not engine_mod.available(goal["engine"], config_dir)[0]:
        return None
    return goal


# --- сессии хаба: переход и возврат ----------------------------------------------------------------------


def switch_session(
    conn,
    hub_session_id: int,
    *,
    by: str,
    kind: str | None,
    reason=None,
    since: int | None = None,
    now_ms: int,
    audit: dict,
    waiting=None,
    config_dir: Path | None = None,
    closed_status: int = 410,
) -> tuple[held_mod.HubSession, held_mod.HubSession, dict]:
    """Переход сессии на запасной «Новой с передачей дел» -> `(новая, прежняя закрытая, запасной)`.

    `by = auto` — хост (только сессия в авто), `owner_channel` — ответ «Перейти» карточки паузы (кукой или агентом
    владельца через канал). `kind` `None` — причина из паузы движка, записанной хостом. Свои проверки — до любой
    записи: не прошло — `Refused`. Отказ `handoff` (идёт ход, интервью, потолок сессий, папка) —
    `SessionError` наружу: транзакция откатывается целиком, переход не состоялся."""
    from . import (
        handoff as handoff_mod,  # noqa: PLC0415 — handoff опирается на hub_sessions, тот — на этот модуль
    )
    from . import (
        held_work,  # noqa: PLC0415
        hub_sessions,  # noqa: PLC0415
    )

    if by not in BY:
        raise ValueError(f"switch_session: by={by!r}")
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(closed_status, "сессия хаба закрыта")
    if by == "auto" and rec.mode != "auto":
        raise Refused("ручная сессия — переход только по ответу владельца на карточке паузы", "manual")
    if marker(rec) is not None:
        raise Refused("сессия уже на запасном — второго перехода нет", "on_fallback")
    pause = pause_of(conn, rec.engine, now_ms, model=rec.model, config_dir=config_dir)
    if kind is None:
        kind = pause["kind"] if pause is not None else None
    if kind not in failure.SESSION_REASONS:
        raise Refused(f"не повод для запасного: {failure.label(kind)}", "not_reason")
    goal = target(conn, rec)
    if goal is None:
        raise Refused("запасной движок не задан", "no_fallback")
    ok, unavailable = engine_mod.available(goal["engine"], config_dir)
    if not ok:
        raise Refused(
            f"запасной {engine_registry.label(goal['engine'])} недоступен: {unavailable}", "unavailable"
        )
    since = since if since is not None else (pause["since"] if pause is not None else now_ms)
    if not reason and pause is not None:
        reason = f"{engine_registry.label(rec.engine)}: {failure.label(kind)} до {_hhmm(pause['until'])}"
    new, closed = handoff_mod.handoff(
        conn,
        rec.id,
        engine=goal["engine"],
        model=goal["model"],
        effort=goal["effort"],
        given=frozenset({"model", "effort"}),
        now_ms=now_ms,
        audit=audit,
        waiting=waiting,
    )
    state = {
        "main": _triple(rec.engine, rec.model, rec.effort),
        "kind": kind,
        "reason": clean_reason(reason),
        "since": since if since and since > 0 else now_ms,
        "by": by,
    }
    hub_sessions.touch(conn, new.id, now_ms, on_fallback_json=_dumps(state))
    new = held_mod.get(conn, new.id)
    assert new is not None  # noqa: S101 — строка этой транзакции
    held_work._event_and_touch(conn, new, now_ms, "status", marker_text(marker(new) or state, new))  # noqa: SLF001
    write_audit(
        conn,
        now_ms=now_ms,
        action=AUDIT_SESSION_SWITCH,
        result="ok",
        detail={
            "hub_session_id": rec.id,
            "new_session_id": new.id,
            "by": by,
            "kind": kind,
            "main": rec.engine,
            "fallback": goal["engine"],
        },
        **audit,
    )
    final = held_mod.get(conn, new.id)
    assert final is not None  # noqa: S101
    return final, closed, goal


def return_session(
    conn,
    hub_session_id: int,
    *,
    now_ms: int,
    audit: dict,
    waiting=None,
) -> tuple[held_mod.HubSession, held_mod.HubSession]:
    """«Вернуть на основной» (только владелец): передача дел на движок, модель и усилие из пометки; пометка
    снимается (`handoff` на движок `main`). Закрытая — 409; не на запасном — 409; основной на паузе — 409
    «<движок> на паузе до ЧЧ:ММ»."""
    from . import handoff as handoff_mod  # noqa: PLC0415

    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    on = marker(rec)
    main = on.get("main") if on is not None else None
    if not main or engine_registry.held(main.get("engine")) is None:
        raise SessionError(409, "сессия не на запасном")
    pause = pause_of(conn, main["engine"], now_ms, model=main.get("model"))
    if pause is not None:
        raise SessionError(409, paused_text(main["engine"], pause))
    new, closed = handoff_mod.handoff(
        conn,
        rec.id,
        engine=main["engine"],
        model=main["model"],
        effort=main["effort"],
        given=frozenset({"model", "effort"}),
        now_ms=now_ms,
        audit=audit,
        waiting=waiting,
    )
    write_audit(
        conn,
        now_ms=now_ms,
        action=AUDIT_SESSION_RETURN,
        result="ok",
        detail={"hub_session_id": rec.id, "new_session_id": new.id, "engine": main["engine"]},
        **audit,
    )
    return new, closed


def carry_marker(rec: held_mod.HubSession, new_engine: str) -> str | None:
    """П8: пометка при передаче дел — переезжает, кроме передачи на движок `main` (возврат снимает)."""
    on = marker(rec)
    if on is None:
        return None
    main = on.get("main") or {}
    return None if main.get("engine") == new_engine else rec.on_fallback_json
