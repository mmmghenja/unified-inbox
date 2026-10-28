"""Выключатели автопилота в ядре (Ф41-5, docs/architecture.md). Каталог ключей — `hub/autopilot.py`.

Где лежат: общие — ключ `autopilot_hold` в `main.meta` (JSON `{ключ: bool}`), по проекту — ключ
`autopilot_hold` в `project.agent_policy_json` (только переопределения). `True` — выключатель СТОИТ; нет
записи, кривое значение и неизвестный ключ — стоит. Пишет ТОЛЬКО этот модуль (слиянием по ключу), зовут его
ручки `routes/autopilot.py`: владелец из интерфейса (ноутбук и телефон, 24.10) и CLI через админ-сокет.

`auto_free` сессии — отсортированные ключи, снятые для её проекта; у сессии в `manual` и у закрытой — пусто.
Изменился `auto_free` незакрытой сессии — её `seq` растёт: хост увидит в ожидании."""

import hashlib
import hmac
import json

from .. import autopilot as catalog
from . import held as held_mod
from .audit import write_audit
from .db import next_seq

META_KEY = "autopilot_hold"
POLICY_KEY = "autopilot_hold"
KEY_ENV = "HUB_SESSION_KEY"  # ключ запуска процесса сессии хаба (П1); ядро хранит только его SHA-256
KEY_HASH_LEN = 64


class AutopilotError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _clean(value) -> dict[str, bool]:
    """Только ключи каталога с `bool`; всё прочее — как отсутствие (стоит)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {key: hold for key, hold in value.items() if key in catalog.BY_KEY and isinstance(hold, bool)}


def global_hold(conn) -> dict[str, bool]:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (META_KEY,)).fetchone()
    return _clean(row[0]) if row is not None else {}


def _policy(policy_json: str | None) -> dict:
    try:
        value = json.loads(policy_json) if policy_json else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def project_hold(conn, project_id: int) -> dict[str, bool]:
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return _clean(_policy(row[0]).get(POLICY_KEY)) if row is not None else {}


def free_for(conn, project_id: int) -> list[str]:
    """Ключи каталога, снятые для проекта (действующее значение — проект, иначе общее, иначе стоит)."""
    return catalog.free_keys(global_hold(conn), project_hold(conn, project_id))


def auto_free(conn, rec: held_mod.HubSession) -> list[str]:
    """Снятые для сессии правила: только у незакрытой сессии в авто-режиме (раздел 1, решение 1)."""
    if rec.mode != "auto" or rec.closed_ts is not None:
        return []
    return free_for(conn, rec.project_id)


def state(conn, *, local: bool) -> dict:
    """`GET /autopilot`: каталог, общие значения по всему каталогу, переопределения проектов. Ф66-1: делегат
    карточек Ф49 и «головная закрывает проект» убраны (поля `delegates`, `delegate_wait_ms` больше нет)."""
    shared = global_hold(conn)
    projects: dict[str, dict[str, bool]] = {}
    for project_id, policy_json in conn.execute(
        "SELECT id, agent_policy_json FROM main.project ORDER BY id"
    ).fetchall():
        own = _clean(_policy(policy_json).get(POLICY_KEY))
        if own:
            projects[str(project_id)] = own
    return {
        "switches": catalog.public(),
        "global": {key: shared.get(key, True) for key in catalog.KEYS},
        "projects": projects,
        "local": bool(local),
    }


def _free_map(conn) -> dict[int, tuple[str, ...]]:
    return {rec.id: tuple(auto_free(conn, rec)) for rec in held_mod.list_sessions(conn)}


def bump_changed(conn, before: dict[int, tuple[str, ...]], *, now_ms: int) -> list[int]:
    """`seq` незакрытых сессий, у которых изменился `auto_free` (ровно у них)."""
    changed = [sid for sid, free in _free_map(conn).items() if before.get(sid, ()) != free]
    for sid in changed:
        conn.execute(
            "UPDATE main.hub_session SET updated_ts = ?, seq = ? WHERE id = ?", (now_ms, next_seq(conn), sid)
        )
    return changed


def _write_global(conn, value: dict[str, bool]) -> None:
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (META_KEY, json.dumps(value, sort_keys=True)),
    )


def _write_project(conn, project_id: int, value: dict[str, bool]) -> None:
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise AutopilotError(404, "not found")
    policy = _policy(row[0])
    if value:
        policy[POLICY_KEY] = value
    else:
        policy.pop(POLICY_KEY, None)
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False) if policy else None, project_id),
    )


def set_hold(conn, scope, key: str, hold: bool | None, *, now_ms: int, audit: dict) -> list[int]:
    """`PUT /autopilot`: `scope` — `"global"` или id проекта; `hold: None` — только проекту: снять
    переопределение. Неизвестный ключ — 422. Аудит `autopilot_set`. Возвращает id сессий, чей `auto_free`
    изменился."""
    if key not in catalog.BY_KEY:
        raise AutopilotError(422, "key: нет такого выключателя")
    before = _free_map(conn)
    if scope == "global":
        if not isinstance(hold, bool):
            raise AutopilotError(422, "hold: true или false (null — только у проекта)")
        value = global_hold(conn)
        old = value.get(key, True)
        value[key] = hold
        _write_global(conn, value)
    elif isinstance(scope, int) and not isinstance(scope, bool):
        row = conn.execute("SELECT 1 FROM main.project WHERE id = ?", (scope,)).fetchone()
        if row is None:
            raise AutopilotError(404, "not found")
        value = project_hold(conn, scope)
        old = value.get(key)
        if hold is None:
            value.pop(key, None)
        else:
            value[key] = bool(hold)
        _write_project(conn, scope, value)
    else:
        raise AutopilotError(422, 'scope: "global" или номер проекта')
    changed = bump_changed(conn, before, now_ms=now_ms)
    write_audit(
        conn,
        now_ms=now_ms,
        action="autopilot_set",
        result="ok",
        detail={"scope": scope, "key": key, "old": old, "new": hold, "sessions": changed},
        **audit,
    )
    return changed


def hold_all(conn, *, now_ms: int, audit: dict) -> list[int]:
    """Поставить все выключатели: общие — все `true`, переопределения проектов сняты (аварийная кнопка и
    `hub autopilot hold-all`). Аудит `autopilot_hold_all`."""
    before = _free_map(conn)
    _write_global(conn, dict.fromkeys(catalog.KEYS, True))
    projects = []
    for project_id, policy_json in conn.execute(
        "SELECT id, agent_policy_json FROM main.project ORDER BY id"
    ).fetchall():
        if POLICY_KEY in _policy(policy_json):
            _write_project(conn, project_id, {})
            projects.append(project_id)
    changed = bump_changed(conn, before, now_ms=now_ms)
    write_audit(
        conn,
        now_ms=now_ms,
        action="autopilot_hold_all",
        result="ok",
        detail={"projects": projects, "sessions": changed},
        **audit,
    )
    return changed


# --- личность авто-сессии (П1) ------------------------------------------------------------------------------


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def valid_key_hash(value) -> bool:
    if not isinstance(value, str) or len(value) != KEY_HASH_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value)


def ident(session, key: str | None) -> dict | None:
    """Личность запроса для `session_free`: тройка сессии (модель ручки или словарь) и ключ запуска, который
    MCP-сервер сессии хаба кладёт в запрос ОТДЕЛЬНЫМ полем (`session_key`), — ключ не попадает ни в какие
    другие пути ядра (подписки, авторство, заявки) и нигде не сохраняется."""
    if session is None:
        return None
    if isinstance(session, dict):
        triple = (session.get("pid"), session.get("proc_start"), session.get("session_id"))
    else:
        triple = (
            getattr(session, "pid", None),
            getattr(session, "proc_start", None),
            getattr(session, "session_id", None),
        )
    return {"pid": triple[0], "proc_start": triple[1], "session_id": triple[2], "key": key}


def auto_session(conn, session) -> held_mod.HubSession | None:
    """П1: запрос пришёл от сессии хаба В АВТО — условная тройка `hub:N` и ключ запуска (`session` — словарь
    `ident`); сессия не закрыта и в состоянии `running`/`idle` (процесс идёт); SHA-256 присланного ключа
    равен сохранённому при привязке процесса (сравнение постоянного времени); `mode == "auto"`. Выключатели
    и проект здесь не проверяются (П5: «любой перенос сессией в авто» — заявка, если выключатель стоит)."""
    if not isinstance(session, dict):
        return None
    pid, proc_start, session_id = session.get("pid"), session.get("proc_start"), session.get("session_id")
    hub_id = held_mod.held_id(pid)
    if hub_id is None or (pid, proc_start, session_id) != held_mod.triple(hub_id):
        return None
    rec = held_mod.get_open(conn, hub_id)
    if rec is None or rec.state not in ("running", "idle") or rec.mode != "auto":
        return None
    given = session.get("key")
    if not isinstance(given, str) or not given or not rec.launch_key_hash:
        return None
    if not hmac.compare_digest(key_hash(given), rec.launch_key_hash):
        return None
    return rec


def claims_auto(conn, session) -> bool:
    """Ревью 41-5: запрос называет себя сессией хаба (условная тройка `hub:N`), и эта сессия не закрыта
    и в режиме `auto` — ключ и состояние НЕ проверяются. Прав такая тройка не даёт (их даёт только
    `auto_session`), но ужесточает: действие, которое в авто регулирует выключатель, обычным путём не
    идёт (ключ не дошёл, хеш стёрт сном — а карточку инструмента хост у сессии в авто уже снял)."""
    if not isinstance(session, dict):
        return False
    pid = session.get("pid")
    hub_id = held_mod.held_id(pid)
    given = (pid, session.get("proc_start"), session.get("session_id"))
    if hub_id is None or given != held_mod.triple(hub_id):
        return False
    rec = held_mod.get_open(conn, hub_id)
    return rec is not None and rec.mode == "auto"


def switch_free(conn, key: str, project_id: int | None) -> bool:
    """Действующий выключатель `key` для проекта снят (нет проекта, неизвестный ключ — стоит)."""
    if key not in catalog.BY_KEY or project_id is None:
        return False
    return not catalog.effective(key, global_hold(conn), project_hold(conn, project_id))


def session_free(conn, session, key: str, project_id: int | None) -> held_mod.HubSession | None:
    """Раздел 5.4 + П1, П12: вызов пришёл от сессии хаба, которой действие `key` в проекте `project_id`
    разрешено без владельца. Требуется РАЗОМ: личность запроса — условная тройка `hub:N` и ключ запуска (`session`
    — словарь личности с полями `pid`, `proc_start`, `session_id`, `key`; собирает `ident`); сессия не
    закрыта и в состоянии `running`/`idle` (процесс идёт); SHA-256 присланного ключа равен сохранённому при
    привязке процесса (сравнение постоянного времени); `mode == "auto"`; проект сессии равен проекту
    действия; действующий выключатель `key` снят. Иначе `None` — действие идёт обычным путём (черновик,
    заявка). ЕДИНСТВЕННАЯ проверка «сессии можно без владельца» в ядре (П1)."""
    if key not in catalog.BY_KEY or project_id is None:
        return None
    rec = auto_session(conn, session)
    if rec is None:
        return None
    if rec.project_id != project_id:
        return None
    if catalog.effective(key, global_hold(conn), project_hold(conn, rec.project_id)):
        return None
    return rec
