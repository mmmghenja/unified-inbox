"""Сессии хаба: создание и закрытие (только владелец из интерфейса), привязка разговора и состояние (только хост).
Ф41-2а, docs/architecture.md (П1, П3, П6, П8, П10).

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`. Реестр в памяти (`held.Held`) здесь не
трогается: его меняет маршрут после коммита (П2в). Самого хоста постоянных сессий в этом куске нет — ручки
хоста проверяются тестами и стендом с поддельным хостом.

Чего здесь нет намеренно: смены роли, модели, движка и режима существующей сессии (режим `auto` только
хранится, создаётся всегда `manual`); сессии вне проектов; записей для внешних сессий (П11). Позже добавлены:
режим (Ф41-5), модель и усилие (Ф41-7а), роль и признак «без подтверждения» (`set_role`, `set_autosend`,
09.10.2026)."""

import hashlib
import json
import os
import sqlite3

from hub.engine import registry as engine_registry

from . import agent_runs
from . import chan_subs as chan_subs_mod
from . import held as held_mod
from . import machines as machines_mod
from . import subs as subs_mod
from . import transcript as transcript_mod
from . import workday as workday_mod
from .audit import write_audit
from .db import next_seq

FIELD_MAX = 64  # `model`, `effort`, `title`
ENGINE_SID_MAX = 128
CLOSE_REASON = "closed_by_owner"


class SessionError(Exception):
    """Отказ ручки сессий хаба: `status` — HTTP-код, `detail` — текст ответа."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def touch(conn, hub_session_id: int, now_ms: int, **sets) -> None:
    """Ф41-2б (2.2, П1): запись в сессию, которую должны увидеть интерфейс или хост, — двигает `seq` и
    `updated_ts`. Подтверждение без изменений сюда не приходит (иначе ожидание хоста будило бы само себя)."""
    sets["updated_ts"] = now_ms
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.hub_session SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), hub_session_id],
    )


def state_sets(rec: held_mod.HubSession, state: str, *, now_ms: int | None = None) -> dict:
    """Ф41-2б: что меняет переход записи в `state`, кроме самого состояния. У `asleep` процесса нет — некого
    ни усыплять, ни прерывать: обе просьбы владельца снимаются; просьба «прервать» относится к идущему ходу и
    снимается при любом уходе из `running`. Возвращает только то, что действительно изменится."""
    sets: dict = {}
    if state != rec.state:
        sets["state"] = state
    if state == "asleep" and rec.sleep_requested_ts is not None:
        sets["sleep_requested_ts"] = None
    if state != "running" and rec.interrupt_requested_ts is not None:
        sets["interrupt_requested_ts"] = None
    if state == "running" and rec.wait_reason is not None:
        sets["wait_reason"] = None  # П13: ход пошёл — причины ожидания больше нет
    # Ф41-4 (2.5): «сжать» относится к живому процессу — у уснувшей снимается; «поднять» исполнена началом
    # хода (у спящей остаётся: её и просили поднять).
    if state == "asleep" and rec.compact_requested_ts is not None:
        sets["compact_requested_ts"] = None
    if state == "running" and rec.wake_requested_ts is not None:
        sets["wake_requested_ts"] = None
    # Ф41-5 П1: ключ запуска живёт, пока идёт процесс.
    if state == "asleep" and rec.launch_key_hash is not None:
        sets["launch_key_hash"] = None
    # Ф48-1: фоновые задачи живут в процессе — у уснувшей их нет.
    if state == "asleep" and rec.bg_json is not None:
        sets["bg_json"] = None
    # Ф50-4: начало идущего хода — с переходом в `running` (`now_ms` дают пути хоста); уход из `running`
    # снимает начало и источник. Строка, поданная посреди хода, живёт в очереди процесса — у уснувшей её нет
    # (зазор после `result` — `idle`, а строка ещё в очереди: при `idle` отметка остаётся, П4 Ф50-1).
    if state == "running" and rec.turn_ts is None and now_ms is not None:
        sets["turn_ts"] = now_ms
    if state != "running":
        if rec.turn_ts is not None:
            sets["turn_ts"] = None
        if rec.turn_origin is not None:
            sets["turn_origin"] = None
    if state == "asleep" and rec.mid_queued_ts is not None:
        sets["mid_queued_ts"] = None
    return sets


# Ф48-1 (контракт раздела 3 спеки): поля записи фоновой задачи и пределы — их же проверяет модель ручки.
BG_ITEMS_MAX = 40
BG_KEYS = (
    "id",
    "kind",
    "title",
    "agent_type",
    "parent",
    "state",
    "started_ts",
    "ended_ts",
    "doing",
    "last_tool",
    "tool_uses",
    "tokens",
    "note",
)


def bg_sets(rec: held_mod.HubSession, state: str, bg: dict | None) -> dict:
    """Ф48-1: хост прислал снимок фоновых задач `{"items": [...]}` (Ф56-1в: и `wake_at_ts` — объявленное
    пробуждение движка; ключ пишется, только когда оно есть). `None` — «не менять»; пустой список —
    «задач нет» (у записи, где их и не было, столбец остаётся `NULL`). У `asleep` снимок не пишется (процесса
    нет, прежний стирает `state_sets`). Возвращает изменение, только когда столбец действительно изменится."""
    if bg is None or state == "asleep":
        return {}
    items = [{key: item.get(key) for key in BG_KEYS} for item in bg.get("items") or []]
    wake_at = bg.get("wake_at_ts")
    wake_at = wake_at if isinstance(wake_at, int) and not isinstance(wake_at, bool) else None
    if not items and wake_at is None and rec.bg_json is None:
        return {}
    snapshot: dict = {"items": items}
    if wake_at is not None:
        snapshot["wake_at_ts"] = wake_at  # Ф56-1в (2.4): объявленное пробуждение движка (`ScheduleWakeup`)
    value = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return {} if value == rec.bg_json else {"bg_json": value}


def bg_of(rec: held_mod.HubSession) -> dict | None:
    """Ф48-1: фоновые задачи сессии `{"items": [...]}` или `None` (нет и не было; нечитаемый столбец)."""
    if not rec.bg_json:
        return None
    try:
        value = json.loads(rec.bg_json)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return None
    wake_at = value.get("wake_at_ts")
    if isinstance(wake_at, int) and not isinstance(wake_at, bool):
        return {"items": value["items"], "wake_at_ts": wake_at}  # Ф56-1в (2.4)
    return {"items": value["items"]}


def signal_sets(rec: held_mod.HubSession, state: str, now_ms: int) -> dict:
    """Ф41-4 П5: сессия уходит в `asleep`, а ход по выданным сигналам не кончился (`signal_turn_ts`) —
    выключение, хост замолчал, процесс умер, запись во ввод не удалась. Сигналы уже отмечены выданными,
    поэтому ставится признак `signal_lost_ts`: хост подаст сессии постоянную строку «проверь входящее».
    Усыпил сам владелец («Усыпить») посреди хода — признака нет: ход оборвал он. Ревью 41-4 (в): признак
    стоит, а хода нет (`idle`: сигнал выдан, но во ввод не записан или ход отклонён движком) — `signal_lost`
    при любой причине сна, и по просьбе владельца тоже."""
    if state != "asleep" or rec.signal_turn_ts is None:
        return {}
    sets: dict = {"signal_turn_ts": None}
    if rec.sleep_requested_ts is None or rec.state != "running":
        sets["signal_lost_ts"] = now_ms
    return sets


def want_json(conn, want_sub) -> str | None:
    """Ф41-4 (2.1, П9): желаемый фильтр подписки -> `want_sub_json` (нормализован кодом подписки); `None` —
    подписка не нужна. Негодный — `SessionError` 422 с текстом проверки подписки."""
    if want_sub is None:
        return None
    try:
        return json.dumps(subs_mod.want_form(conn, want_sub), ensure_ascii=False, sort_keys=True)
    except subs_mod.SubError as exc:
        raise SessionError(exc.status, str(exc.detail)) from exc


def want_of(rec: held_mod.HubSession) -> dict | None:
    """Желаемый фильтр записи (объект) или `None` — и при нечитаемом значении столбца."""
    if not rec.want_sub_json:
        return None
    try:
        value = json.loads(rec.want_sub_json)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def autosend_count(conn, project_id: int, role_key: str | None) -> int:
    """Ф41-4 П15: сколько бесед проекта разрешено роли «без подтверждения» (список Ф37-1, только чтение):
    сессия хаба с этой ролью отвечает в них сама. Без роли — 0; беседы в архиве и вне проекта не считаются."""
    if role_key is None:
        return 0
    from .db import db_of

    count = 0
    for (channel_id,) in conn.execute(
        "SELECT channel_id FROM main.channel_autosend WHERE role_key = ?", (role_key,)
    ).fetchall():
        row = conn.execute(
            f"SELECT project_id, archived FROM {db_of(channel_id)}.channel WHERE id = ?",  # noqa: S608
            (channel_id,),
        ).fetchone()
        if row is not None and row[0] == project_id and not row[1]:
            count += 1
    return count


def _clean(value: str | None, name: str) -> str | None:
    """Необязательная строка записи: без крайних пробелов, пустая — `None`; управляющие символы — 422."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > FIELD_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SessionError(422, f"{name}: до {FIELD_MAX} символов, без управляющих")
    return text


def _sub_holder(conn, project_id: int, role_key: str, role_no: int, *, now_ms: int, waiting):
    """Живая подписка проекта на линии `(role_key, role_no)` — по базе (`subs.rec_dead`); мёртвая, ещё не
    снятая чисткой, линию не держит."""
    for rec in subs_mod.live_subs(conn, project_id):
        if rec.role_key == role_key and rec.role_no == role_no:
            if not subs_mod.rec_dead(conn, rec, now_ms, waiting):
                return rec
    return None


def create(
    conn,
    *,
    project_id: int,
    engine: str,
    role: str | None = None,
    number: int | None = None,
    model: str | None = None,
    effort: str | None = None,
    title: str | None = None,
    created_by: str = "ui",
    now_ms: int,
    audit: dict,
    waiting=None,
    want_sub=None,
    mode: str = "manual",
    role_file: str | None = None,
    subdir: str | None = None,
    resume_conv: str | None = None,
) -> held_mod.HubSession:
    """3.1: новая сессия хаба (`asleep`, без разговора движка; режим — `manual`, Ф41-5: или
    `auto`). Проверки: проект есть, с рабочей
    папкой и включёнными подписками; роль — `subs.role_form` (та же форма и запрет зарезервированных); линия
    `(проект, роль, номер)` не занята ни живой подпиской, ни другой сессией хаба (номер не задан — наименьший
    свободный с учётом обоих); не больше `MAX_HUB_SESSIONS_PER_PROJECT` незакрытых на проект. Аудит
    `hub_session_create`. `waiting(sub_id)` — идёт ли ожидание (брошенная подписка OpenCode линию не
    держит). Ф50-7: `subdir` — подпапка процесса внутри рабочей папки проекта (форма — `roles.subdir_form`;
    папка обязана существовать, лежать внутри проекта и не нести своих настроек Claude Code — иначе 422).

    Ф99-1 (П2, П3): `resume_conv` — id прошлого разговора Claude Code папки проекта: сессия продолжает его.
    Только `engine=claude` и без подпапки (422); стенограмма есть в каталоге записей папки проекта (422);
    разговор не у другой незакрытой сессии хаба (409). Разговор пишется в `engine_session_id` сразу, снимок
    системного текста — пометкой `IMPORTED_PIN`: хост пойдёт `--resume`, первой подачей отдаст весь
    системный текст хаба и запишет настоящий снимок."""
    if engine not in held_mod.ENGINES:
        raise SessionError(422, f"engine: {' или '.join(held_mod.ENGINES)}")
    if created_by not in held_mod.CREATED_BY:
        raise ValueError(f"неизвестный created_by {created_by!r}")
    if mode not in held_mod.MODES:
        raise SessionError(422, f"mode: {' или '.join(held_mod.MODES)}")
    row = conn.execute(
        "SELECT workdir, agent_policy_json FROM main.project WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    workdir, policy_json = row
    if not workdir:
        raise SessionError(409, "у проекта нет рабочей папки — сессии хаба негде работать")
    if not subs_mod.subs_enabled(policy_json):
        raise SessionError(409, "подписки в проекте выключены")
    want = want_json(conn, want_sub)  # Ф41-4 (2.1): желаемый фильтр подписки — допустим и без роли
    role_path = role_file_form(role_file)  # Ф48-3: файл роли из проекта (существования не требуем)
    sub_path = subdir_checked(workdir, subdir)  # Ф50-7: подпапка процесса (Р2, Р3, П4)
    conv = _resume_conv_checked(conn, workdir, engine, sub_path, resume_conv)  # Ф99-1
    try:
        role_text, role_key = subs_mod.role_form(role) if role is not None else (None, None)
    except subs_mod.SubError as exc:
        raise SessionError(exc.status, exc.detail) from exc
    if number is not None and not (isinstance(number, int) and 1 <= number <= subs_mod.ROLE_NO_MAX):
        raise SessionError(422, f"number: 1–{subs_mod.ROLE_NO_MAX}")
    if number is not None and role_key is None:
        raise SessionError(422, "number — только вместе с role")
    opened = held_mod.list_sessions(conn, project_id=project_id)
    if len(opened) >= held_mod.MAX_HUB_SESSIONS_PER_PROJECT:
        raise SessionError(
            409, f"в проекте уже {held_mod.MAX_HUB_SESSIONS_PER_PROJECT} сессий хаба — закрой лишнюю"
        )
    role_no: int | None = None
    if role_key is not None:
        held_lines = held_mod.role_lines(conn, project_id)
        if number is not None:
            role_no = number
            other = held_lines.get((role_key, role_no))
            if other is not None:
                raise SessionError(
                    409,
                    f"{subs_mod.line_name(role_text, role_no)}: линию держит сессия хаба "
                    f"«{held_mod.shown_name(other)}»",
                )
            holder = _sub_holder(conn, project_id, role_key, role_no, now_ms=now_ms, waiting=waiting)
            if holder is not None:
                raise SessionError(
                    409, f"{subs_mod.line_name(role_text, role_no)} уже слушает сессия #{holder.id}"
                )
        else:
            alive = [
                rec
                for rec in subs_mod.live_subs(conn, project_id)
                if not subs_mod.rec_dead(conn, rec, now_ms, waiting)
            ]
            role_no = subs_mod._free_no([*alive, *held_lines.values()], role_key)
    try:
        cur = conn.execute(
            "INSERT INTO main.hub_session (project_id, role, role_key, role_no, engine, model, effort, mode, "
            "workdir, title, state, created_ts, updated_ts, created_by, store, seq, want_sub_json, "
            "role_file, subdir, engine_session_id, sys_pin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'asleep', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                role_text,
                role_key,
                role_no,
                engine,
                _clean(model, "model"),
                _clean(effort, "effort"),
                mode,
                workdir,
                _clean(title, "title"),
                now_ms,
                now_ms,
                created_by,
                # Ф41-2б П2: файл событий, запросов и реплик фиксируется здесь и больше не меняется (правило
                # «в проекте есть беседа рабочего аккаунта» со временем даёт другой ответ).
                agent_runs.schema_for_project(conn, project_id),
                next_seq(conn),
                want,
                role_path,
                sub_path,
                conv,
                IMPORTED_PIN_JSON if conv is not None else None,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SessionError(409, "линия роли уже занята другой сессией хаба — повтори") from exc
    # Ф48-2 (раздел D): умолчания сжатия роли из каталога (`hub.roles`) — новой сессии этой роли.
    from hub import roles as roles_mod  # импорт внутри: каталог нужен только здесь

    known = roles_mod.find(role_text)
    if known is not None and (known.compact_tokens is not None or known.compact_auto is not None):
        conn.execute(
            "UPDATE main.hub_session SET compact_tokens = ?, compact_auto = ? WHERE id = ?",
            (known.compact_tokens, 0 if known.compact_auto is False else 1, cur.lastrowid),
        )
    rec = held_mod.get(conn, cur.lastrowid)
    assert rec is not None  # noqa: S101 — только что вставлена в этой транзакции
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_create",
        result="ok",
        detail={
            "hub_session_id": rec.id,
            "project_id": project_id,
            "engine": engine,
            **({"role": True, "role_no": role_no} if role_key is not None else {}),
            **({"want_sub": _want_audit(want_of(rec))} if want is not None else {}),
            **({"mode": mode} if mode != "manual" else {}),
            **({"role_file": True} if role_path is not None else {}),
            **({"subdir": True} if sub_path is not None else {}),
            **({"resume_conv": True} if conv is not None else {}),
        },
        **audit,
    )
    return rec


def _want_audit(want: dict | None) -> dict | None:
    """Аудит желаемого фильтра — без текстов ключей: режим, фильтр и числа."""
    if want is None:
        return None
    return {
        "mode": want.get("mode"),
        "msg_filter": want.get("msg_filter"),
        "keys": len(want.get("keywords") or []),
        "exclude": len(want.get("exclude") or []),
        "batch_min": want.get("batch_min"),
        "accounts": want.get("accounts"),
    }


def set_want(conn, hub_session_id: int, want_sub, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """Ф41-4 (2.1): `PATCH /sessions/{id}` — заменить желаемый фильтр подписки (и `null` — «не подписывать»;
    живую подписку это не снимает). Применит MCP-сервер сессии при следующем подъёме процесса. `seq` растёт
    (хост и интерфейс видят `sub_stale`). Закрытая — 409, нет записи — 404."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    want = want_json(conn, want_sub)
    touch(conn, rec.id, now_ms, want_sub_json=want)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_want_sub",
        result="ok",
        detail={"hub_session_id": rec.id, "want_sub": _want_audit(json.loads(want) if want else None)},
        **audit,
    )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def drop_want_self(conn, hub_session_id: int, *, now_ms: int, audit: dict) -> bool:
    """Ф64-1 (раздел 5, П13): сессия хаба САМА отписалась от проекта (`hub_unsubscribe`, её токен) — желаемый
    фильтр снимается («не подписывать», как `set_want(None)`): хост больше не поднимает её ради подписки до
    слова владельца («Подписка…» в окне) или её собственного `hub_subscribe`. Прежнее значение — в аудите
    `hub_session_want_sub` (результат `self_unsubscribed`). Нечего снимать или сессия закрыта — `False`."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None or rec.closed_ts is not None or rec.want_sub_json is None:
        return False
    before = want_of(rec)
    touch(conn, rec.id, now_ms, want_sub_json=None)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_want_sub",
        result="self_unsubscribed",
        detail={"hub_session_id": rec.id, "want_sub": None, "before": _want_audit(before)},
        **audit,
    )
    return True


def set_mode(conn, hub_session_id: int, mode: str, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """Ф41-5 (3.1): режим сессии — `manual` или `auto` (к авто-сессии применяются выключатели автопилота).
    Смена — `seq` растёт (хост увидит новый `auto_free`), событие ленты `status` с `brief` `{"type": "mode",
    "mode"}`, аудит `hub_session_mode` (было, стало). Тот же режим — ничего не пишется. Закрытая — 409."""
    if mode not in held_mod.MODES:
        raise SessionError(422, f"mode: {' или '.join(held_mod.MODES)}")
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    if rec.mode != mode:
        from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

        text = "режим: авто — действуют выключатели автопилота" if mode == "auto" else "режим: ручной"
        held_work._event_and_touch(  # noqa: SLF001
            conn, rec, now_ms, "status", text, brief={"type": "mode", "mode": mode}, mode=mode
        )
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_mode",
            result="ok",
            detail={"hub_session_id": rec.id, "old": rec.mode, "new": mode},
            **audit,
        )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def set_model(
    conn, hub_session_id: int, *, given: frozenset[str], model: str | None, effort: str | None,
    now_ms: int, audit: dict,
) -> held_mod.HubSession:  # fmt: skip
    """Ф41-7а: модель и усилие сессии (`None` — умолчание движка); меняются только заданные (`given`).
    Смена — `seq` растёт (хост перезапустит живой процесс при простое), событие ленты `status` с `brief`
    `{"type": "model", "model", "effort"}`, аудит `hub_session_model` (было, стало). То же значение —
    ничего не пишется. Закрытая — 409."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    new_model = _clean(model, "model") if "model" in given else rec.model
    new_effort = _clean(effort, "effort") if "effort" in given else rec.effort
    if (new_model, new_effort) != (rec.model, rec.effort):
        from . import held_work, interview  # импорт внутри: оба опираются на этот модуль

        interview.check_free(conn, rec.id)  # Ф60-1 (П8): идёт интервью — 409

        text = f"модель: {new_model or 'по умолчанию'}, усилие: {new_effort or 'по умолчанию'}"
        held_work._event_and_touch(  # noqa: SLF001
            conn,
            rec,
            now_ms,
            "status",
            f"{text} — со следующего входа",
            brief={"type": "model", "model": new_model, "effort": new_effort},
            model=new_model,
            effort=new_effort,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_model",
            result="ok",
            detail={
                "hub_session_id": rec.id,
                "old": {"model": rec.model, "effort": rec.effort},
                "new": {"model": new_model, "effort": new_effort},
            },
            **audit,
        )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def compact_tokens_form(value) -> int | None:
    """Ф48-2 (D): свой порог сжатия — целое в границах или `None` (умолчание хоста); иначе 422."""
    if value is None:
        return None
    low, high = held_mod.COMPACT_TOKENS_MIN, held_mod.COMPACT_TOKENS_MAX
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise SessionError(422, f"compact_tokens: целое {low}–{high} или null")
    return value


def set_compact(
    conn, hub_session_id: int, *, given: frozenset[str], compact_tokens: int | None,
    compact_auto: bool | None, now_ms: int, audit: dict,
) -> held_mod.HubSession:  # fmt: skip
    """Ф48-2 (раздел D): свой порог авто-сжатия сессии (`None` — умолчание хоста) и выключатель авто-сжатия;
    меняются только заданные (`given`). Смена — `seq` растёт (хост читает поля из записи, процесс не
    перезапускается), событие ленты `status` с `brief` `{"type": "compact_settings", "compact_tokens",
    "compact_auto"}`, аудит `hub_session_compact_settings`. То же значение — ничего не пишется. Закрытая —
    409; порог вне границ или `compact_auto: null` — 422."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    tokens = compact_tokens_form(compact_tokens) if "compact_tokens" in given else rec.compact_tokens
    auto = bool(rec.compact_auto)
    if "compact_auto" in given:
        if not isinstance(compact_auto, bool):
            raise SessionError(422, "compact_auto: true или false")
        auto = compact_auto
    if (tokens, auto) != (rec.compact_tokens, bool(rec.compact_auto)):
        from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

        limit = f"{round(tokens / 1000)} тыс." if tokens is not None else "по умолчанию"
        held_work._event_and_touch(  # noqa: SLF001
            conn,
            rec,
            now_ms,
            "status",
            f"сжатие: {'автоматически' if auto else 'только по просьбе'}, порог {limit}",
            brief={"type": "compact_settings", "compact_tokens": tokens, "compact_auto": auto},
            compact_tokens=tokens,
            compact_auto=1 if auto else 0,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_compact_settings",
            result="ok",
            detail={
                "hub_session_id": rec.id,
                "old": {"compact_tokens": rec.compact_tokens, "compact_auto": bool(rec.compact_auto)},
                "new": {"compact_tokens": tokens, "compact_auto": auto},
            },
            **audit,
        )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def role_file_form(value) -> str | None:
    """Ф48-3: путь файла роли для записи (`roles.file_form`); негодный — 422 с готовым текстом."""
    from hub import roles as roles_mod  # импорт внутри: каталог нужен только здесь

    try:
        return roles_mod.file_form(value)
    except ValueError as exc:
        raise SessionError(422, str(exc)) from exc


def subdir_form(value) -> str | None:
    """Ф50-7: подпапка сессии для записи (`roles.subdir_form`); негодная — 422 с готовым текстом."""
    from hub import roles as roles_mod  # импорт внутри: каталог нужен только здесь

    try:
        return roles_mod.subdir_form(value)
    except ValueError as exc:
        raise SessionError(422, str(exc)) from exc


# Ф99-1 (П1): пометка «разговор начат вне хаба, снимка системного текста ещё нет» — вместо снимка у сессии,
# созданной из прошлого разговора. Хост по ней первой подачей отдаёт весь системный текст и пишет снимок.
IMPORTED_PIN = {"imported": True}
IMPORTED_PIN_JSON = json.dumps(IMPORTED_PIN, ensure_ascii=False, sort_keys=True)
# Ф99-1 (П2): разговор менялся недавно — подсказка «закрой другое окно» в ответе создания (не отказ).
RESUME_FRESH_MS = 2 * 60 * 1000


def pin_imported(pin) -> bool:
    """Ф99-1: снимок — пометка импортированного разговора без настоящего снимка (сессия ещё не запускалась).
    Настоящий снимок такого разговора несёт `imported: true` вместе с составляющими (`mode` и прочие)."""
    return isinstance(pin, dict) and pin.get("imported") is True and "mode" not in pin


def _resume_conv_checked(conn, workdir: str, engine: str, sub_path: str | None, value) -> str | None:
    """Ф99-1 (П2, П3): `resume_conv` -> id разговора или `None` (не задан); отказ — `SessionError`."""
    if value is None or value == "":
        return None
    if engine != "claude":
        raise SessionError(422, "продолжить прошлый разговор можно только на движке Claude Code")
    if sub_path is not None:
        raise SessionError(422, "продолжить прошлый разговор можно только в корне проекта, без подпапки")
    if not transcript_mod.is_conv_id(value):
        raise SessionError(422, "resume_conv: id разговора Claude Code (UUID)")
    if transcript_mod.conv_path(workdir, value) is None:
        raise SessionError(422, "такого разговора Claude Code в папке проекта нет")
    taken = conn.execute(
        "SELECT id FROM main.hub_session WHERE engine_session_id = ? AND closed_ts IS NULL", (value,)
    ).fetchone()
    if taken is not None:
        raise SessionError(409, f"этот разговор уже ведёт сессия хаба #{taken[0]}")
    return value


def subdir_checked(workdir: str, value) -> str | None:
    """Ф50-7 (Р3, П4): форма подпапки плюс проверка на диске — папка есть, лежит внутри проекта после
    разрешения ссылок, на пути от неё до корня проекта нет своих настроек Claude Code. Отказ — 422 с готовым
    текстом. Подпапка не задана — `None`, диск не трогается."""
    from hub import roles as roles_mod  # импорт внутри: каталог нужен только здесь

    path = subdir_form(value)
    if path is None:
        return None
    try:
        roles_mod.subdir_check(workdir, path)
    except ValueError as exc:
        raise SessionError(422, str(exc)) from exc
    return path


def session_cwd(rec: held_mod.HubSession) -> str:
    """Ф50-7 (Р1): папка процесса сессии — `<workdir>/<subdir>`; без подпапки — папка проекта."""
    return os.path.join(rec.workdir, rec.subdir) if rec.subdir else rec.workdir


def set_role_file(
    conn, hub_session_id: int, role_file: str | None, *, now_ms: int, audit: dict
) -> held_mod.HubSession:
    """Ф48-3: файл роли сессии — путь к MD-файлу относительно рабочей папки проекта; `None` или пустая
    строка — снять. Существование файла не требуется (может появиться позже). Процесс не перезапускается:
    хост читает файл при следующем запуске процесса. Смена — `seq` растёт, событие ленты `status` с `brief`
    `{"type": "role_file", "role_file"}`, аудит `hub_session_role_file` (без пути). То же значение — ничего
    не пишется. Закрытая — 409; негодный путь — 422."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    path = role_file_form(role_file)
    if path != rec.role_file:
        from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

        text = f"файл роли: {path}" if path is not None else "файл роли снят"
        held_work._event_and_touch(  # noqa: SLF001
            conn,
            rec,
            now_ms,
            "status",
            f"{text} — со следующего запуска процесса",
            brief={"type": "role_file", "role_file": path},
            role_file=path,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_role_file",
            result="ok",
            detail={
                "hub_session_id": rec.id,
                "old": rec.role_file is not None,
                "new": path is not None,
            },
            **audit,
        )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def role_label(role: str | None, role_no: int | None) -> str | None:
    """Имя линии сессии хаба для ленты и интерфейса — как его пишет хост: «тестер 2»; без роли — `None`."""
    return f"{role} {role_no or 1}" if role is not None else None


def _own_live_subs(conn, rec: held_mod.HubSession) -> list:
    """Живые подписки на проект условной тройки сессии (любого токена)."""
    pid, proc_start, session_id = held_mod.triple(rec.id)
    return [
        sub
        for sub in subs_mod.live_subs(conn, rec.project_id)
        if subs_mod.same_session(sub, pid, proc_start, session_id)
    ]


def set_role(
    conn,
    hub_session_id: int,
    *,
    role: str | None,
    number: int | None = None,
    keep_autosend: bool = False,
    now_ms: int,
    audit: dict,
    waiting=None,
) -> held_mod.HubSession:
    """Смена роли существующей сессии (слово владельца; только он, из интерфейса). `role` — новая роль,
    `None` или пустая — снять; `number` — номер линии (только с ролью; не задан — наименьший свободный, а
    при той же роли в другом написании — прежний). Проверки линии — как в `create`: форма и запрет
    зарезервированных (`subs.role_form`), линию не держит другая сессия хаба и не слушает ЧУЖАЯ живая
    подписка (своя — не в счёт); занято — 409. Закрытая — 409. Та же роль и номер — ничего не пишется.

    Живая подписка сессии на проект меняет линию НА МЕСТЕ, как у `hub_subscribe(role=…)` вторым вызовом
    (Ф36-2): номер подписки тот же, поэтому её ждущие сигналы, подписки на беседы и сообщения агентов,
    привязанные к подписке (диалог владельца), остаются у сессии; старая линия свободна сразу. Адресованное
    ЛИНИИ (сообщения агентов прежней роли, её напоминания) остаётся на прежней линии — следующей сессии
    этой роли. Мёртвая подписка, державшая новую линию, завершается `replaced`.

    Признак «отвечает в беседы „без подтверждения“ своей роли» (`autosend_ok`) на другую роль молча не
    переносится: при смене КЛЮЧА роли сбрасывается, если нет `keep_autosend` (явное `autosend_ok: true` того
    же запроса). Событие ленты `status` с `brief` `{"type": "role", "role": <имя линии | null>}`, аудит
    `hub_session_role` (было, стало — без текста роли), `seq` растёт: хост перезапустит живой процесс при
    простое — системная вставка роли читается при запуске."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    try:
        role_text, role_key = subs_mod.role_form(role) if role is not None else (None, None)
    except subs_mod.SubError as exc:
        raise SessionError(exc.status, exc.detail) from exc
    if number is not None and not (isinstance(number, int) and 1 <= number <= subs_mod.ROLE_NO_MAX):
        raise SessionError(422, f"number: 1–{subs_mod.ROLE_NO_MAX}")
    if number is not None and role_key is None:
        raise SessionError(422, "number — только вместе с role")
    if role_text == rec.role and (number is None or number == rec.role_no):
        return rec
    own = _own_live_subs(conn, rec)
    own_ids = {sub.id for sub in own}
    lives = subs_mod.live_subs(conn, rec.project_id)
    alive = [
        sub for sub in lives if sub.id not in own_ids and not subs_mod.rec_dead(conn, sub, now_ms, waiting)
    ]
    role_no: int | None = None
    if role_key is not None:
        held_lines = held_mod.role_lines(conn, rec.project_id, except_id=rec.id)
        if number is not None or role_key == rec.role_key:
            role_no = number if number is not None else rec.role_no
            other = held_lines.get((role_key, role_no))
            if other is not None:
                raise SessionError(
                    409,
                    f"{subs_mod.line_name(role_text, role_no)}: линию держит сессия хаба "
                    f"«{held_mod.shown_name(other)}»",
                )
            holder = subs_mod._line_holder(alive, role_key, role_no)  # noqa: SLF001
            if holder is not None:
                raise SessionError(
                    409, f"{subs_mod.line_name(role_text, role_no)} уже слушает сессия #{holder.id}"
                )
        else:
            role_no = subs_mod._free_no([*alive, *held_lines.values()], role_key)  # noqa: SLF001
    key_changed = role_key != rec.role_key
    reset = key_changed and bool(rec.autosend_ok) and not keep_autosend
    from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

    label = role_label(role_text, role_no)
    text = f"роль: {label}" if label is not None else "роль снята"
    sets: dict = {"role": role_text, "role_key": role_key, "role_no": role_no}
    if reset:
        sets["autosend_ok"] = 0
    try:
        held_work._event_and_touch(  # noqa: SLF001
            conn,
            rec,
            now_ms,
            "status",
            f"{text} — со следующего входа",
            brief={"type": "role", "role": label},
            **sets,
        )
    except sqlite3.IntegrityError as exc:
        raise SessionError(409, "линия роли уже занята другой сессией хаба — повтори") from exc
    moved: list[int] = []
    for sub in own:
        # Та же правка, что у `hub_subscribe(role=…)` вторым вызовом: роль своей живой подписки — на месте.
        if role_key is not None:
            sub_no = role_no
            try:
                subs_mod._take_role(  # noqa: SLF001
                    conn, lives, sub, role_key, sub_no, now_ms=now_ms, waiting=waiting, audit=audit
                )
            except subs_mod.SubError as exc:
                raise SessionError(exc.status, exc.detail) from exc
        elif sub.role_key is None:
            sub_no = sub.role_no  # и была без роли — номер-адрес прежний
        else:
            sub_no = subs_mod._free_no(alive, None)  # noqa: SLF001 — без роли номер — только адрес
        conn.execute(
            "UPDATE main.session_sub SET role = ?, role_key = ?, role_no = ? WHERE id = ?",
            (role_text, role_key, sub_no, sub.id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            action="sub_mode",
            result="ok",
            detail={"sub_id": sub.id, "role": role_text is not None},
            **audit,
        )
        moved.append(sub.id)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_role",
        result="ok",
        detail={
            "hub_session_id": rec.id,
            "old": {"role": rec.role_key is not None, "role_no": rec.role_no},
            "new": {"role": role_key is not None, "role_no": role_no},
            "role_changed": key_changed,
            "subs": moved,
            "autosend_reset": reset,
        },
        **audit,
    )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def set_autosend(conn, hub_session_id: int, ok: bool, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """Признак «сессия отвечает в беседы „без подтверждения“ своей роли» (`autosend_ok`; список бесед Ф37-1
    ведёт владелец, условия `autosend.try_send` здесь не меняются — признак только закрывает или открывает сессии
    хаба ветку списка). Ставит и снимает только владелец из интерфейса. Смена — `seq` растёт, аудит
    `hub_session_autosend` (было, стало). То же значение — ничего не пишется. Закрытая — 409."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    if bool(rec.autosend_ok) != bool(ok):
        touch(conn, rec.id, now_ms, autosend_ok=int(bool(ok)))
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_autosend",
            result="ok",
            detail={"hub_session_id": rec.id, "old": bool(rec.autosend_ok), "new": bool(ok)},
            **audit,
        )
    updated = held_mod.get(conn, rec.id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def close(
    conn, hub_session_id: int, *, now_ms: int, audit: dict, reason: str = CLOSE_REASON
) -> tuple[held_mod.HubSession, bool]:
    """3.1 + П8: закрыть насовсем — `closed_ts`, `close_reason`; подписки на проект и на беседы условной
    тройки этой сессии (любого токена) завершаются В ТОЙ ЖЕ транзакции причиной `ended_by_owner` (владелец закрыл
    — ждущее не возвращается; идущее ожидание ответит 410 `ended_by_owner`); при передаче дел — `handoff`
    (беседы и ждущее достаются новой сессии линии). Идемпотентно: уже закрытая —
    `(запись, False)`, ничего не пишется. Нет записи — `SessionError` 404."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        return rec, False
    # Ф41-2б (3.1, П8): неподанные реплики — `dropped`, запросы без доставленного решения — `expired`, просьбы
    # «прервать» и «усыпить» снимаются. `seq` двигается: хост узнаёт о закрытии из ожидания и гасит процесс.
    from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

    held_work.on_close(conn, rec, now_ms=now_ms)
    from . import interview  # Ф60-1 (П8): открытое интервью сессии кончается само, без итога

    interview.end_for_session(conn, hub_session_id, now_ms=now_ms, audit=audit)
    from . import owner_queue  # Ф56-1б (3.6): открытая карточка `turn` закрытой сессии — снята `system`

    owner_queue.close_turns(conn, hub_session_id, by="system", action="closed", now_ms=now_ms)
    # Ф56-1в: и карточки состояний сессии (в том числе «Пусть ждёт»).
    owner_queue.close_states(
        conn, hub_session_id, owner_queue.STATE_KINDS, by="system", action="closed", now_ms=now_ms
    )
    conn.execute(
        "UPDATE main.hub_session SET closed_ts = ?, close_reason = ?, state = 'asleep', proc_pid = NULL, "
        "proc_start = NULL, interrupt_requested_ts = NULL, sleep_requested_ts = NULL, wait_reason = NULL, "
        "launch_key_hash = NULL, bg_json = NULL, updated_ts = ?, seq = ? WHERE id = ? AND closed_ts IS NULL",
        (now_ms, reason, now_ms, next_seq(conn), hub_session_id),
    )
    pid, proc_start, session_id = held_mod.triple(hub_session_id)
    # 21.10: при передаче дел подписки кончаются «сами» — новая сессия линии получает беседы и ждущее.
    end_reason = subs_mod.HANDOFF_END_REASON if reason == subs_mod.HANDOFF_END_REASON else "ended_by_owner"
    ended_subs: list[int] = []
    for (sub_id,) in conn.execute(
        "SELECT id FROM main.session_sub WHERE session_pid = ? AND session_proc_start = ? AND session_id = ? "
        "AND ended_ts IS NULL ORDER BY id",
        (pid, proc_start, session_id),
    ).fetchall():
        if subs_mod.end_sub(conn, sub_id, end_reason, now_ms=now_ms, **audit):
            ended_subs.append(sub_id)
    ended_chans: list[int] = []
    for chan in chan_subs_mod.live_of_process(conn, pid, proc_start):
        if chan.session_id == session_id and chan_subs_mod.end(
            conn, chan.id, end_reason, now_ms=now_ms, **audit
        ):
            ended_chans.append(chan.id)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_close",
        result="ok",
        detail={
            "hub_session_id": hub_session_id,
            "project_id": rec.project_id,
            "subs": ended_subs,
            "chan_subs": ended_chans,
        },
        **audit,
    )
    closed = held_mod.get(conn, hub_session_id)
    assert closed is not None  # noqa: S101 — строка этой транзакции
    return closed, True


def _open_for_host(conn, hub_session_id: int) -> held_mod.HubSession:
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(410, "сессия хаба закрыта")
    return rec


def _proc(state: str, proc_pid: int | None, proc_start: int | None) -> tuple[int | None, int | None]:
    """Настоящий процесс записи: у `asleep` его нет (обнуляется), у `running`/`idle` — как сообщил хост."""
    if state not in held_mod.STATES:
        raise SessionError(422, f"state: {', '.join(held_mod.STATES)}")
    if state == "asleep":
        return None, None
    return proc_pid, proc_start


def sys_pin_of(rec: held_mod.HubSession) -> dict | None:
    """Ф48-5: снимок закреплённого системного текста сессии (`None` — снимка нет или он не читается)."""
    if not rec.sys_pin:
        return None
    try:
        pin = json.loads(rec.sys_pin)
    except ValueError:
        return None
    return pin if isinstance(pin, dict) else None


def file_sha(text: str | None) -> str | None:
    """Ф48-5: отпечаток текста файла роли в снимке (`None` — блока файла в системном тексте нет)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def role_file_pinned(rec: held_mod.HubSession, file_text: str | None) -> bool | None:
    """Ф48-5: совпадает ли текущий файл роли (путь и отпечаток текста) с закреплённым при создании разговора
    (Claude Code; Ф74-5 — любой движок с `pins_system` профиля, Codex). `None` — снимка нет или движок
    системный текст не закрепляет."""
    profile = engine_registry.get(rec.engine)
    pin = sys_pin_of(rec) if profile is not None and profile.pins_system else None
    if pin is None or pin_imported(pin):  # Ф99-1: настоящего снимка ещё нет
        return None
    return pin.get("role_file") == rec.role_file and pin.get("file_sha") == file_sha(file_text)


def bind(
    conn,
    hub_session_id: int,
    *,
    engine_session_id: str,
    proc_pid: int | None,
    proc_start: int | None,
    state: str,
    now_ms: int,
    audit: dict,
    launch_key_hash: str | None = None,
    sys_pin: dict | None = None,
) -> held_mod.HubSession:
    """3.3 + П1: хост запустил процесс или сменил разговор движка — пишутся `engine_session_id`, настоящий
    процесс, состояние и `host_seen_ts`. Личность сессии в подписках (`hub:<id>`) от разговора не зависит:
    подписки не трогаются, ждущее цело. Занятый разговор (у другой незакрытой сессии хаба) — 409; закрытая —
    410. Аудит `hub_session_bind` со старым и новым id — только при смене разговора.

    Ф48-5: `sys_pin` — снимок закреплённого системного текста нового разговора Claude Code; пишется как
    есть. Разговор сменился, а снимка нет (OpenCode) — прежний снимок стирается: он про другой разговор."""
    rec = _open_for_host(conn, hub_session_id)
    if not engine_session_id or len(engine_session_id) > ENGINE_SID_MAX:
        raise SessionError(422, f"engine_session_id: 1–{ENGINE_SID_MAX} символов")
    pid, start = _proc(state, proc_pid, proc_start)
    taken = conn.execute(
        "SELECT id FROM main.hub_session WHERE engine_session_id = ? AND closed_ts IS NULL AND id != ?",
        (engine_session_id, hub_session_id),
    ).fetchone()
    if taken is not None:
        raise SessionError(409, f"этот разговор движка уже у сессии хаба #{taken[0]}")
    # Ф41-2б П1: `seq` — только при настоящем изменении (разговор, процесс, состояние); повтор той же привязки
    # лишь подтверждает `host_seen_ts`.
    sets = state_sets(rec, state, now_ms=now_ms)
    if rec.engine_session_id != engine_session_id:
        sets["engine_session_id"] = engine_session_id
    if sys_pin is not None or rec.engine_session_id != engine_session_id:
        pin = None if sys_pin is None else json.dumps(sys_pin, ensure_ascii=False, sort_keys=True)
        if pin != rec.sys_pin:
            sets["sys_pin"] = pin
    if (rec.proc_pid, rec.proc_start) != (pid, start):
        sets.update(proc_pid=pid, proc_start=start)
    sets.update(signal_sets(rec, state, now_ms))
    sets.update(_key_sets(rec, state, launch_key_hash))
    try:
        conn.execute("UPDATE main.hub_session SET host_seen_ts = ? WHERE id = ?", (now_ms, hub_session_id))
        if sets:
            touch(conn, hub_session_id, now_ms, **sets)
    except sqlite3.IntegrityError as exc:
        raise SessionError(409, "этот разговор движка уже у другой сессии хаба") from exc
    if rec.engine_session_id != engine_session_id:
        write_audit(
            conn,
            now_ms=now_ms,
            action="hub_session_bind",
            result="ok",
            detail={
                "hub_session_id": hub_session_id,
                "old": rec.engine_session_id,
                "new": engine_session_id,
            },
            **audit,
        )
    bound = held_mod.get(conn, hub_session_id)
    assert bound is not None  # noqa: S101 — строка этой транзакции
    return bound


def set_state(
    conn,
    hub_session_id: int,
    *,
    state: str,
    proc_pid: int | None = None,
    proc_start: int | None = None,
    now_ms: int,
    launch_key_hash: str | None = None,
    bg: dict | None = None,
) -> held_mod.HubSession:
    """3.3: хост подтвердил состояние (`running`/`idle`/`asleep`) — пишется оно и `host_seen_ts`; `asleep`
    обнуляет настоящий процесс, у прочих процесс меняется, только если назван. Подписки не трогаются: все три
    состояния для них — «жива». Закрытая — 410."""
    rec = _open_for_host(conn, hub_session_id)
    if state not in held_mod.STATES:
        raise SessionError(422, f"state: {', '.join(held_mod.STATES)}")
    if state == "asleep":
        pid, start = None, None
    else:
        pid = proc_pid if proc_pid is not None else rec.proc_pid
        start = proc_start if proc_start is not None else rec.proc_start
    # Ф41-2б П1: хост подтверждает состояние живых сессий на каждом круге — `seq` двигается только при
    # изменении состояния или процесса, само подтверждение пишет лишь `host_seen_ts`.
    sets = state_sets(rec, state, now_ms=now_ms)
    if (rec.proc_pid, rec.proc_start) != (pid, start):
        sets.update(proc_pid=pid, proc_start=start)
    sets.update(signal_sets(rec, state, now_ms))
    sets.update(_key_sets(rec, state, launch_key_hash))
    sets.update(bg_sets(rec, state, bg))  # Ф48-1: фоновые задачи — `seq` только при изменении
    conn.execute("UPDATE main.hub_session SET host_seen_ts = ? WHERE id = ?", (now_ms, hub_session_id))
    if sets:
        touch(conn, hub_session_id, now_ms, **sets)
    updated = held_mod.get(conn, hub_session_id)
    assert updated is not None  # noqa: S101 — строка этой транзакции
    return updated


def _key_sets(rec: held_mod.HubSession, state: str, launch_key_hash: str | None) -> dict:
    """Ф41-5 П1: хост прислал SHA-256 ключа запуска идущего процесса — запомнить (у `asleep` процесса нет:
    хеш не пишется, прежний стирает `state_sets`)."""
    if launch_key_hash is None or state == "asleep" or rec.launch_key_hash == launch_key_hash:
        return {}
    from . import autopilot as autopilot_mod  # импорт внутри: модуль читает записи сессий

    if not autopilot_mod.valid_key_hash(launch_key_hash):
        raise SessionError(422, "launch_key_hash: sha256 в шестнадцатеричной записи")
    return {"launch_key_hash": launch_key_hash}


def stale_to_asleep(conn, *, now_ms: int) -> list[int]:
    """2.3: хост упал или остановлен — незакрытые сессии в `running`/`idle`, чьё состояние он не подтверждал
    дольше `HOST_STALE_MS`, ядро само переводит в `asleep` (фоновая задача рядом с `subs.cleanup`). Подписки
    при этом не трогаются. Возвращает id переведённых."""
    rows = conn.execute(
        "SELECT id FROM main.hub_session WHERE closed_ts IS NULL AND state != 'asleep' "
        "AND (host_seen_ts IS NULL OR host_seen_ts < ?) ORDER BY id",
        (now_ms - held_mod.HOST_STALE_MS,),
    ).fetchall()
    ids = [row[0] for row in rows]
    if not ids:
        return ids
    # Ф41-2б П1, П8: `seq` (интерфейс и хост видят смену состояния) и снятие запросов — висящих и решённых, но
    # не доставленных: карточка не остаётся «разрешил» при невыполненном действии.
    from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

    for hub_session_id in ids:
        rec = held_mod.get(conn, hub_session_id)
        assert rec is not None  # noqa: S101 — строка этой транзакции
        held_work.force_asleep(conn, rec, now_ms=now_ms, note=held_work.NOTE_STALE)
    return ids


# --- выдача -------------------------------------------------------------------------------------------------


def _live_sub(conn, hub_session_id: int):
    """Живая подписка на проект условной тройки сессии (самая свежая) или `None`."""
    pid, proc_start, session_id = held_mod.triple(hub_session_id)
    row = conn.execute(
        "SELECT id FROM main.session_sub WHERE session_pid = ? AND session_proc_start = ? AND session_id = ? "
        "AND ended_ts IS NULL ORDER BY id DESC LIMIT 1",
        (pid, proc_start, session_id),
    ).fetchone()
    return subs_mod.get_sub(conn, row[0]) if row is not None else None


def sub_of_other_token(conn, rec: held_mod.HubSession, token_id: int | None) -> bool:
    """Ф33: живая подписка сессии хаба есть, но оформлена другим токеном (профиль MCP `all` у каждой площадки
    свой — после переключения сессия зовёт ядро токеном новой машины)."""
    if token_id is None or rec.closed_ts is not None:
        return False
    sub = _live_sub(conn, rec.id)
    return sub is not None and sub.api_token_id != token_id


def _sub_stale(want: dict | None, sub) -> bool:
    """П9: желаемый фильтр есть, а живой подписки нет или её настройки с ним расходятся."""
    return want is not None and (sub is None or subs_mod.want_differs(want, sub))


def _sub_json(conn, rec: held_mod.HubSession, sub, *, now_ms: int, presence) -> dict | None:
    """П16: живая подписка сессии на проект для окна — `{id, mode, msg_filter, keywords, exclude, batch_min,
    accounts, pending}`; `pending` — ждущие сигналы всех видов (и подписок сессии на беседы)."""
    if sub is None:
        return None
    from . import held_signals  # импорт внутри: `held_signals` опирается на этот модуль
    from . import keywords as keywords_mod

    spec = keywords_mod.public_spec(sub.keywords_json)
    # Ф67-1: действующий интервал пачки — у подписки с ключами (и 0) и у подписки без ключей с пачкой;
    # без ключей и без пачки — `None`, как раньше.
    batch = keywords_mod.batch_minutes(sub.keywords_json)
    return {
        "id": sub.id,
        "mode": subs_mod.mode_of(sub),
        "msg_filter": sub.msg_filter,
        "keywords": spec["keys"] if spec else [],
        "exclude": spec["exclude"] if spec else [],
        "batch_min": batch if not sub.direct and (spec is not None or batch) else None,
        "accounts": subs_mod.accounts_of(sub),
        "pending": held_signals.pending_total(conn, rec, now_ms=now_ms, presence=presence or []),
    }


def _cache(rec: held_mod.HubSession) -> dict | None:
    """Ф41-1: срок кэша и контекст разговора Claude Code; у OpenCode, без разговора и у закрытой — `None`."""
    if rec.engine != "claude" or not rec.engine_session_id or rec.closed_ts is not None:
        return None
    return transcript_mod.state_for(rec.engine_session_id)


def work_policy(policy_json: str | None) -> dict:
    """Ключ `work` политики проекта — `{stage, ask: [добавки], self: ["сама"]}` (добавки проекта к кнопкам
    сессии хаба). Битый JSON или негодный ключ — пустая политика: сессии остаются кнопки по умолчанию."""
    empty = {"stage": 1, "ask": [], "self": []}
    try:
        value = json.loads(policy_json) if policy_json else {}
    except ValueError, TypeError:
        value = {}
    work = value.get("work") if isinstance(value, dict) else None
    if not isinstance(work, dict):
        return empty
    stage = work.get("stage", 1)
    if stage != 1 or isinstance(stage, bool):
        return empty
    result: dict = {"stage": 1}
    for key in ("ask", "self"):
        items = work.get(key) or []
        if not isinstance(items, list) or not all(isinstance(x, str) and x for x in items):
            return empty
        result[key] = list(items)
    return result


def _work_policy(conn, project_id: int) -> dict:
    """Ключ `work` политики проекта для хоста (`{stage, ask, self}`)."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return work_policy(row[0] if row is not None else None)


def usage_of(rec: held_mod.HubSession) -> dict | None:
    """Ф41-3 (2.3): расход сессии `{"context_tokens", "context_limit", "cost_usd", "updated_ts",
    "base_tokens"}` или `None` (хост ещё не присылал; нечитаемое значение столбца — тоже `None`). Ф41-4:
    `context_tokens` — `null` после сжатия; `base_tokens` (П17) — ключ есть, когда база измерена."""
    if not rec.usage_json:
        return None
    try:
        value = json.loads(rec.usage_json)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    result = {key: value.get(key) for key in ("context_tokens", "context_limit", "cost_usd", "updated_ts")}
    if value.get("base_tokens") is not None:
        result["base_tokens"] = value["base_tokens"]  # ключ есть, только когда база измерена
    return result


def session_json(
    conn,
    rec: held_mod.HubSession,
    *,
    now_ms: int = 0,
    waiters=None,
    ui: bool = False,
    host: bool = False,
    sync: bool = False,
    presence: list[dict] | None = None,
) -> dict:
    """Ф41-4 (2.1, 2.4; П9, П11, П14–П16): публичная форма и элемент `/sync` несут `want_sub` (желаемый
    фильтр), `sub` (живая подписка на проект с числом ждущих сигналов `pending`), `sub_stale` (подписки нет
    или она расходится с `want_sub` — считает ядро) и `autosend_channels` (бесед «без подтверждения» у роли);
    `listening` подписки сессии хаба — «хост ждал не позже 120 с назад» (`held.host_listening`), а не
    ожидание токеном сессии. Форма «для хоста» — `want_sub`, `sub_id`, `sub_stale`, просьбы «поднять» и
    «сжать», `signal_lost`, срок кэша и размер контекста для правила подъёма. `presence` — присутствие владельца
    (годность карточек в `pending`); в `/sync` его нет.

    Строка сессии хаба. Публичная (`GET /sessions`, `GET /sessions/{id}`): без настоящего процесса;
    `sub_id` — живая подписка на проект этой тройки, `listening` — идёт ли её ожидание (до хоста 41-4 — честно
    `false`); `engine_session_id` — только куке UI (Ф41-2б П12), `workdir` и `cache` — ей же, только в ответах
    ручек. `sync` — элемент `hub_sessions` ленты `/sync`: без `workdir`, `cache` и `listening` (у ленты нет ни
    часов, ни учёта идущих ожиданий — эти поля клиент берёт из `GET /sessions`). `host`
    (`GET /work/host/sessions`, раздел `sessions` ожидания хоста): запись целиком — с рабочей папкой,
    настоящим процессом и просьбами владельца."""
    result = {
        "id": rec.id,
        "project_id": rec.project_id,
        "role": rec.role,
        "role_no": rec.role_no,
        "engine": rec.engine,
        "model": rec.model,
        "effort": rec.effort,
        "mode": rec.mode,
        "title": rec.title,
        "state": rec.state,
        "host_seen_ts": rec.host_seen_ts,
        "created_ts": rec.created_ts,
        "updated_ts": rec.updated_ts,
        "closed_ts": rec.closed_ts,
        "close_reason": rec.close_reason,
        "created_by": rec.created_by,
        # Ф41-2б (2.2, П13).
        "seq": rec.seq,
        "last_event_id": rec.last_event_id,
        "last_turn_ts": rec.last_turn_ts,
        "failure": rec.failure,
        "wait_reason": rec.wait_reason,
        # Ф41-3 (2.3): расход — во всех трёх формах записи.
        "usage": usage_of(rec),
        # Ф48-1: фоновые задачи процесса (подагенты, фоновые команды) — во всех трёх формах записи.
        "bg": bg_of(rec),
        # Ф48-2 (раздел D): свой порог авто-сжатия (`null` — умолчание хоста), выключатель и действующий
        # порог, как его посчитал хост (`null` — ещё не сообщал), — во всех трёх формах записи.
        "compact_tokens": rec.compact_tokens,
        "compact_auto": bool(rec.compact_auto),
        "compact_at": rec.compact_at,
        # Ф48-3: файл роли (путь относительно рабочей папки проекта; `null` — не задан) — во всех трёх
        # формах записи; хост читает его при каждом запуске процесса.
        "role_file": rec.role_file,
        # Ф50-7: подпапка процесса относительно рабочей папки проекта (`null` — корень) — во всех трёх
        # формах записи; сами пути (`workdir`, `cwd`) — только там же, где `workdir`.
        "subdir": rec.subdir,
        # Ф50-4 (П9): начало и источник идущего хода (`null` — хода нет; источник — `say`, `signal`, `self`,
        # `compact` или `null`, если хост его не назвал); сигнал подан посреди хода и ещё стоит в очереди
        # движка — во всех трёх формах записи.
        "turn_ts": rec.turn_ts,
        "turn_origin": rec.turn_origin,
        "mid_queued_ts": rec.mid_queued_ts,
        # Ф56-1б (3.1): законченных ходов — во всех трёх формах записи (ключ карточки `turn`).
        "turns": rec.turns,
        # Ф73-1: «Перечитать MCP» ждёт исполнения (мс нажатия, `null` — нет) — во всех трёх формах записи:
        # интерфейсу — строка «перечитаю MCP…», хосту — что и с каким значением исполнить.
        "mcp_reload_ts": rec.mcp_reload_ts,
        # Ф77-1 (A5): машина сессии хаба — всегда машина ядра (после переключения главной — новая), во всех
        # трёх формах записи.
        "machine": machines_mod.label(conn, None, now_ms=now_ms or None),
    }
    # Ф83-1: запасной движок — своя настройка (`fallback`: `null` — как у проекта, `{"engine": null}` — нет),
    # куда перейдёт (`fallback_target`), пометка «на запасном» и её строка — во всех трёх формах записи.
    from . import engine_fallback  # noqa: PLC0415 — модуль опирается на этот (SessionError)

    result.update(engine_fallback.session_fields(conn, rec))
    if host and rec.sys_pin is not None:
        # Ф48-5: снимок закреплённого системного текста — только хосту и только когда он есть.
        result["sys_pin"] = sys_pin_of(rec)
    # Ф41-5 (3.3): снятые для сессии выключатели автопилота — во всех трёх формах (у `manual` — пусто).
    from . import autopilot as autopilot_mod  # импорт внутри: модуль читает записи сессий

    free = autopilot_mod.auto_free(conn, rec)
    result["auto_free"] = free
    # Ф99-7.2 (П11): «Всегда в этой сессии» — во всех трёх формах (`[{"key", "ts"}]`; в любом режиме).
    from . import session_always  # noqa: PLC0415 — модуль опирается на этот (SessionError, touch)

    result["always"] = session_always.items_of(rec.always_json)
    if host:
        result["engine_session_id"] = rec.engine_session_id
        result["workdir"] = rec.workdir
        result["cwd"] = session_cwd(rec)  # Ф50-7: папка процесса (хост перед запуском проверяет её сам)
        result["proc_pid"] = rec.proc_pid
        result["proc_start"] = rec.proc_start
        result["interrupt_requested_ts"] = rec.interrupt_requested_ts
        result["sleep_requested_ts"] = rec.sleep_requested_ts
        result["launch_fails"] = rec.launch_fails
        # Ревью 2б: добавки проекта к кнопкам (`work.ask` / `work.self`); хост читает их на каждый запуск
        # процесса.
        result["policy"] = _work_policy(conn, rec.project_id)
        # Ф41-4: подписка и правило подъёма.
        live = _live_sub(conn, rec.id) if rec.closed_ts is None else None
        want = want_of(rec)
        result["want_sub"] = want
        result["sub_id"] = live.id if live is not None else None
        result["sub_stale"] = _sub_stale(want, live)
        result["wake_requested_ts"] = rec.wake_requested_ts
        result["compact_requested_ts"] = rec.compact_requested_ts
        result["signal_lost"] = rec.signal_lost_ts is not None
        cache = _cache(rec)
        result["cache_expires_ts"] = cache.get("expires_ts") if cache else None
        # П14: размер контекста для правила подъёма — из расхода хода, а пока его нет — из стенограммы.
        usage = result["usage"] or {}
        tokens = usage.get("context_tokens")
        if tokens is None and result["usage"] is None and cache:
            tokens = cache.get("context_tokens")
        result["context_tokens"] = tokens
        result["base_tokens"] = usage.get("base_tokens")  # П17
        # Ф64-1 (раздел 2): день проекта закрыт — мс закрытия (замок на подъём), иначе `null`.
        result["day_closed_ts"] = workday_mod.project_closed_ts(conn, rec.project_id)
        return result
    from . import held_work  # импорт внутри: `held_work` сам опирается на этот модуль

    if rec.close_reason == "handoff":
        # Ф41-7б: закрытая передачей дел — кому переданы (строка «дела переданы сессии #N»).
        from . import handoff as handoff_mod

        result["handoff_to"] = handoff_mod.successor(conn, rec)
    # Ф54-1 (П6): счёт — по ключам правил сессии (ключи области `project` — не её правила).
    result["auto_held"] = len(autopilot_mod.catalog.SESSION_KEYS) - len(free)
    result["autosend_ok"] = bool(rec.autosend_ok)
    pending, queued, interrupted = held_work.counts(conn, rec)
    result["pending_requests"] = pending
    result["queued_says"] = queued
    # Ф48-2 (раздел A): из них прерванных — ждут решения владельца («Отправить снова» / «Убрать»).
    result["interrupted_says"] = interrupted
    result["interrupt_requested"] = rec.interrupt_requested_ts is not None
    result["sleep_requested"] = rec.sleep_requested_ts is not None
    sub = _live_sub(conn, rec.id) if rec.closed_ts is None else None
    result["sub_id"] = sub.id if sub is not None else None
    want = want_of(rec) if rec.closed_ts is None else None
    result["want_sub"] = want
    result["sub"] = _sub_json(conn, rec, sub, now_ms=now_ms, presence=presence)
    result["sub_stale"] = _sub_stale(want, sub)
    result["autosend_channels"] = (
        autosend_count(conn, rec.project_id, rec.role_key) if rec.closed_ts is None else 0
    )
    if not sync:
        # П11: слушает ли подписку хост (и у спящей — её поднимет сигнал); `waiters` здесь больше не нужен.
        result["listening"] = sub is not None and held_mod.host_listening(now_ms)
    if ui:
        # id разговора движка — только владельцу (кука UI); bearer `read` его не получает.
        result["engine_session_id"] = rec.engine_session_id
        if not sync:
            result["workdir"] = rec.workdir
            result["cwd"] = session_cwd(rec)  # Ф50-7
            result["cache"] = _cache(rec)
            if rec.closed_ts is None:
                # Ф50-4: состояние роли — та же функция, что отдаёт его `hub_agents` и `hub_agent_state`
                # (у кого карточки, сигналы и срочные, шаг, последняя ошибка; `lines` — словами).
                from . import agent_state as agent_state_mod

                result["agent_state"] = agent_state_mod.of_hub(conn, rec, now_ms=now_ms, presence=presence)
    return result
