"""Сообщения агентов хаба и диалог владельца с агентом (Ф35-2, docs/architecture.md,
9, 15, 16; поправки П3–П5, П7, Н4, Н6–Н8 — главнее текста выше них).

Таблица `main.peer_msg` (`0049_main.sql`). Статусы: `pending` -> `read`; `pending` -> `lost` (подписка
адресата кончилась), `expired` (срок). Старые строки `blocked` (пределы сняты, Ф42-2) остаются в базе, но
выдачи их не показывают и не считают (`_select`).

- Агент = живая подписка сессии на проект. Отправитель — линия подписки, её берёт ядро, не запрос.
- Адресат — линия. С ролью в настоящем проекте — `(проект, роль, номер)`: можно не в сети, строка ждёт
  подписки линии. Безрольный и агент вне проектов (проект 0) — только живой, строка привязана к его
  подписке (`to_sub_id`, Н4); подписка кончилась — `lost`.
- Владелец пишет из UI (`from_user`), агент отвечает ему только ответом на его сообщение и в течение суток
  (`to_user`, Н8). Строки `to_user` не истекают и в пределы не входят (Н7).
- Сигнал подписке — сама строка: один предикат на счёт и выдачу (`_signal_pred`, П5) — только поля строки
  и время; выключатель проекта и видимость проверяются при ОТПРАВКЕ.
- Читает линия с ролью в настоящем проекте — историю линии; безрольные и проект 0 — по своей подписке (Н6).
- Текст — данные: в сигнал и в аудит не попадает.
- Ф48-4 (docs/architecture.md): у подписки с интервалом пачки (`subs.agent_batch_of` > 0)
  несрочные сообщения агентов до срока `session_sub.agent_batch_ts` не сигналят; сообщение владельца, срочное
  (`urgent`) и любая выдача подписки срок не ждут и забирают накопленное. Интервал 0 — всё как прежде.

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`.
"""

import json
import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from . import engine as engine_mod
from . import held as held_mod
from . import machines as machines_mod
from . import subs as subs_mod
from .audit import write_audit
from .db import next_seq
from .security import HUMAN_SCOPES

TEXT_MAX = 4000
ASK_ABOUT_MAX = 128  # Ф56-1б (А4): роль, о которой вопрос владельцу
NEED_ROLE = "вопрос владельцу задаёт только сессия с ролью: hub_subscribe(role=…)"
_MIN_MS = 60 * 1000
HOUR_MS = 60 * _MIN_MS
DAY_MS = 24 * HOUR_MS
IMPLICIT_REPLY_MS = 30 * _MIN_MS  # П7
RESIGNAL_MS = 10 * _MIN_MS  # П4: повторный сигнал
MAX_SIGNALS = 2
AGENT_TTL_MS = 7 * DAY_MS
USER_TTL_MS = 2 * DAY_MS  # Н4: сообщение владельца линии с ролью
LINE_LIVED_MS = 7 * DAY_MS  # ждать может только линия, жившая в последние 7 дней
USER_REPLY_MS = DAY_MS  # Н8: ответ владельцу — в течение суток
LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200
LIST_DEFAULT = 50
MARK_MAX = 200
POLICY_KEY = "peer_msgs"

NO_SUB = "сначала hub_subscribe() — сообщения агентов пишет и читает сессия с подпиской"


class PeerError(Exception):
    """Отказ ручки сообщений агентов: `status` — HTTP-код, `detail` — текст ответа (без текстов сообщений)."""

    def __init__(self, status: int, detail) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- строки -----------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "from_user",
    "from_sub_id",
    "from_project_id",
    "from_role",
    "from_role_key",
    "from_no",
    "to_user",
    "to_project_id",
    "to_role",
    "to_role_key",
    "to_no",
    "to_sub_id",
    "text",
    "reply_to",
    "chain_id",
    "hop",
    "status",
    "reason",
    "api_token_id",
    "created_ts",
    "updated_ts",
    "signal_sub_id",
    "signaled_ts",
    "read_sub_id",
    "read_ts",
    "closed_ts",
    "signals",
    "notified_ts",
    "expires_ts",
    "seq",
    "urgent",
    "ask",
    "ask_about",
    "money",
)
_SELECT = ", ".join(f"m.{c}" for c in _COLUMNS)


class Rec(NamedTuple):
    id: int
    from_user: int
    from_sub_id: int | None
    from_project_id: int | None
    from_role: str | None
    from_role_key: str | None
    from_no: int | None
    to_user: int
    to_project_id: int | None
    to_role: str | None
    to_role_key: str | None
    to_no: int | None
    to_sub_id: int | None
    text: str
    reply_to: int | None
    chain_id: int | None
    hop: int
    status: str
    reason: str | None
    api_token_id: int | None
    created_ts: int
    updated_ts: int
    signal_sub_id: int | None
    signaled_ts: int | None
    read_sub_id: int | None
    read_ts: int | None
    closed_ts: int | None
    signals: int
    notified_ts: int | None
    expires_ts: int | None
    seq: int
    urgent: int = 0  # Ф48-4: срочное — мимо пачки
    # Ф56-1б (А4): вопрос владельцу `hub_ask_owner` (мимо правила суток) и роль, о которой он (`None` — о себе).
    ask: int = 0
    ask_about: str | None = None
    # Ф93-1 (A.4): вопрос о тратах — карточка идёт по строке «Деньги» таблицы маршрута (по умолчанию владельцу).
    money: int = 0


class Target(NamedTuple):
    """Адресат-агент: линия; `sub_id` — привязка к подписке (безрольный, проект 0), `live_sub_id` — живая
    подписка линии сейчас (`None` — не в сети)."""

    project_id: int
    role: str | None
    role_key: str | None
    role_no: int
    sub_id: int | None
    live_sub_id: int | None


class Sent(NamedTuple):
    """Итог `send`: строка сообщения. `live_sub_id` — живая подписка адресата. Ф48-4: `batch_ts` — срок
    пачки, до которого адресат сигнала о сообщении не получит (`None` — сигнал сразу)."""

    rec: Rec | None
    live_sub_id: int | None = None
    batch_ts: int | None = None


def _select(conn: sqlite3.Connection, where: str, params: tuple | list = ()) -> list[Rec]:
    rows = conn.execute(
        # Старые строки `blocked` (пределы сняты, Ф42-2) не показываем и не считаем нигде — отсев здесь.
        f"SELECT {_SELECT} FROM (SELECT * FROM main.peer_msg WHERE status <> 'blocked') m "  # noqa: S608
        f"WHERE {where}",
        params,
    ).fetchall()
    return [Rec(*row) for row in rows]


def get(conn: sqlite3.Connection, msg_id: int) -> Rec | None:
    rows = _select(conn, "m.id = ?", (msg_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, msg_id: int, now_ms: int, **sets) -> None:
    """Правка строки: `updated_ts` и новый `seq` (UI узнаёт через `/sync`) — всегда."""
    sets = {**sets, "updated_ts": now_ms, "seq": next_seq(conn)}
    assign = ", ".join(f"{column} = ?" for column in sets)
    conn.execute(
        f"UPDATE main.peer_msg SET {assign} WHERE id = ?",  # noqa: S608 — имена колонок заданы кодом
        (*sets.values(), msg_id),
    )


def _engine_of_sub(conn: sqlite3.Connection, sub_id: int | None) -> str | None:
    if sub_id is None:
        return None
    row = conn.execute(
        "SELECT session_id, session_pid FROM main.session_sub WHERE id = ?", (sub_id,)
    ).fetchone()
    if row is None:
        return None
    # Ф41-2а П1: у сессии хаба движок — из её записи (условная метка `hub:<id>` движка не называет).
    hub = held_mod.get(conn, held_mod.held_id(row[1])) if held_mod.is_held(row[1]) else None
    return hub.engine if hub is not None else engine_mod.engine_of_session(None, row[0])


def msg_dict(conn: sqlite3.Connection, rec: Rec) -> dict:
    """Строка API (`/peer-msgs`, `/sync`, `alerts`). Кому её отдавать, решает вызывающий."""
    return {
        "id": rec.id,
        "from_user": bool(rec.from_user),
        "to_user": bool(rec.to_user),
        "from": (
            None
            if rec.from_user
            else {
                "project_id": rec.from_project_id,
                "role": rec.from_role,
                "role_no": rec.from_no,
                "sub_id": rec.from_sub_id,
                "engine": _engine_of_sub(conn, rec.from_sub_id),
                # Ф77-1 (C4): машина отправителя (подписки нет — `None`).
                "machine": machines_mod.of_sub_id(conn, rec.from_sub_id),
            }
        ),
        "to": (
            None
            if rec.to_user
            else {
                "project_id": rec.to_project_id,
                "role": rec.to_role,
                "role_no": rec.to_no,
                "sub_id": rec.to_sub_id,
            }
        ),
        "text": rec.text,
        "reply_to": rec.reply_to,
        "chain_id": rec.chain_id,
        "hop": rec.hop,
        "status": rec.status,
        "reason": rec.reason,
        "created_ts": rec.created_ts,
        "signaled_ts": rec.signaled_ts,
        "signals": rec.signals,
        "read_ts": rec.read_ts,
        "closed_ts": rec.closed_ts,
        "expires_ts": rec.expires_ts,
        "seq": rec.seq,
        "urgent": bool(rec.urgent),  # Ф48-4
        "batch_ts": _batch_ts_of(conn, rec),
    }


def _batch_ts_of(conn: sqlite3.Connection, rec: Rec) -> int | None:
    """Ф48-4 п.5: срок пачки, которого ждёт сигнал об этом сообщении, — у ждущего, ещё не сигналившего,
    несрочного сообщения агента агенту, чья живая подписка адресата держит срок (сам срок может уже пройти —
    сравнивает читающий). Иначе `None`."""
    if rec.status != "pending" or rec.signals or rec.from_user or rec.to_user or rec.urgent:
        return None
    if rec.to_sub_id is not None:
        row = conn.execute(
            f"SELECT {subs_mod._SUB_COLUMNS} FROM main.session_sub WHERE id = ? AND ended_ts IS NULL",  # noqa: S608
            (rec.to_sub_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {subs_mod._SUB_COLUMNS} FROM main.session_sub WHERE project_id = ? AND role_key IS ? "  # noqa: S608
            "AND role_no = ? AND ended_ts IS NULL ORDER BY id DESC LIMIT 1",
            (rec.to_project_id, rec.to_role_key, rec.to_no),
        ).fetchone()
    if row is None:
        return None
    sub = subs_mod.SubRec(*row)
    ts = subs_mod.agent_batch_row(conn, sub.id)[1]
    if ts is None or subs_mod.agent_batch_of(conn, sub) <= 0:
        return None
    return ts


# --- выключатель проекта (решение 9) -----------------------------------------------------------------------


def enabled(policy_json: str | None) -> bool:
    """Обмен сообщениями агентов в проекте: ключ `peer_msgs` политики; нет ключа — включено."""
    return subs_mod._policy_dict(policy_json).get(POLICY_KEY) is not False


def project_enabled(conn: sqlite3.Connection, project_id: int | None) -> bool:
    """У агентов вне проектов (проект 0) выключателя нет."""
    if project_id is None or project_id == subs_mod.NO_PROJECT_ID:
        return True
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return row is not None and enabled(row[0])


def set_project_enabled(conn: sqlite3.Connection, project_id: int, on: bool) -> bool | None:
    """`hub project set P --agent-msgs on|off` — ключ `peer_msgs` политики проекта (прочие ключи не
    трогаются). Нет проекта — `None`."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = subs_mod._policy_dict(row[0])
    policy[POLICY_KEY] = bool(on)
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    return bool(on)


# --- подписка сессии и линии -------------------------------------------------------------------------------


def session_sub(conn: sqlite3.Connection, principal, session: dict | None) -> subs_mod.SubRec:
    """Живая подписка ЭТОЙ сессии (токен, pid + `starttime`, разговор) — на проект или вне проектов. Сессии
    нет или она мертва — 409; подписки нет — 409 «сначала hub_subscribe()»."""
    if session is None:
        raise PeerError(422, "session — обязательна bearer-токену сессии")
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    if not subs_mod.is_alive(pid, proc_start, session.get("host")):
        raise PeerError(409, "сессия не найдена")
    row = conn.execute(
        "SELECT id FROM main.session_sub WHERE api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL ORDER BY id DESC LIMIT 1",
        (principal.token_id, pid, proc_start, session_id),
    ).fetchone()
    sub = subs_mod.get_sub(conn, row[0]) if row is not None else None
    if sub is None:
        raise PeerError(409, NO_SUB)
    return sub


def _bound(project_id: int | None, role_key: str | None) -> bool:
    """Н4: адресат «только живому» — безрольный или агент вне проектов."""
    return role_key is None or project_id == subs_mod.NO_PROJECT_ID


def _sub_live(rec, now_ms: int, waiting: Callable[[int], bool] | None) -> bool:
    if rec is None or rec.ended_ts is not None:
        return False
    if not subs_mod.rec_alive(rec):
        return False
    return not subs_mod.oc_stale(rec, now_ms, waiting=bool(waiting and waiting(rec.id)))


def _live_of_role(conn, project_id: int, role_key: str | None, now_ms: int, waiting) -> list:
    """Живые подписки проекта с этой ролью (без роли — безрольные), по номеру."""
    rows = conn.execute(
        f"SELECT {subs_mod._SUB_COLUMNS} FROM main.session_sub "  # noqa: S608 — список колонок константа
        "WHERE project_id = ? AND role_key IS ? AND ended_ts IS NULL ORDER BY role_no, id",
        (project_id, role_key),
    ).fetchall()
    return [rec for rec in (subs_mod.SubRec(*row) for row in rows) if _sub_live(rec, now_ms, waiting)]


def _lived_role(conn, project_id: int, role_key: str, role_no: int, now_ms: int) -> tuple[str] | None:
    """Решение 5: линия жила в последние 7 дней — `(роль, как её писали)`; иначе `None`."""
    return conn.execute(
        "SELECT role FROM main.session_sub WHERE project_id = ? AND role_key = ? AND role_no = ? "
        "AND (ended_ts IS NULL OR ended_ts > ?) ORDER BY id DESC LIMIT 1",
        (project_id, role_key, role_no, now_ms - LINE_LIVED_MS),
    ).fetchone()


def _project_seen(conn, principal, project_id: int) -> bool:
    """Проект адресата виден принципалу; проект 0 — только принципалу без списка проектов (решение 14)."""
    if project_id == subs_mod.NO_PROJECT_ID:
        return principal.project_ids is None
    if principal.project_ids is not None and project_id not in principal.project_ids:
        return False
    return conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is not None


NOT_ONLINE = "агент не в сети — безрольным и агентам вне проектов сообщения доходят только живым"
NO_AGENT = "такого агента нет — проверь проект, роль и номер (hub_agents)"
SUB_GONE = "эта сессия уже не в сети — по её адресу теперь другой агент; выбери агента в списке заново"


def _target_of_line(
    conn, project_id: int, role: str | None, role_key: str | None, role_no: int, now_ms: int, waiting
) -> Target:
    """Адресат по точной линии. Привязанному (безрольный, проект 0) нужна живая подписка — иначе 409 (линия
    с ролью, которой не было 7 дней, — 404); линия с ролью в настоящем проекте может быть не в сети, если
    жила в последние 7 дней (иначе 404)."""
    live = [r for r in _live_of_role(conn, project_id, role_key, now_ms, waiting) if r.role_no == role_no]
    sub = live[0] if live else None
    if _bound(project_id, role_key):
        if sub is None:
            if role_key is not None and _lived_role(conn, project_id, role_key, role_no, now_ms) is None:
                raise PeerError(404, NO_AGENT)
            raise PeerError(409, NOT_ONLINE)
        return Target(project_id, sub.role, role_key, role_no, sub.id, sub.id)
    if sub is not None:
        return Target(project_id, sub.role, role_key, role_no, None, sub.id)
    lived = _lived_role(conn, project_id, role_key, role_no, now_ms)  # type: ignore[arg-type]
    if lived is None:
        raise PeerError(404, NO_AGENT)
    return Target(project_id, lived[0] or role, role_key, role_no, None, None)


def _target_of_request(conn, principal, to: dict, now_ms: int, waiting) -> Target:
    """Адрес из запроса `{project_id, role?, number?}`. Роль без номера — единственная живая подписка роли;
    несколько — 409 со списком номеров; ни одной — №1. `to.sub_id` (только UI) сверяет `_send_user`."""
    project_id = to["project_id"]
    if not _project_seen(conn, principal, project_id):
        raise PeerError(404, "проект адресата не найден")
    role, role_key = None, None
    if to.get("role") is not None:
        try:
            role, role_key = subs_mod.role_form(to["role"])
        except subs_mod.SubError as exc:
            raise PeerError(exc.status, exc.detail) from exc
    number = to.get("number")
    if number is None:
        live = _live_of_role(conn, project_id, role_key, now_ms, waiting)
        if len(live) > 1:
            numbers = ", ".join(str(r.role_no) for r in live)
            raise PeerError(409, f"по этому адресу несколько агентов — задай number: {numbers}")
        number = live[0].role_no if live else 1
    return _target_of_line(conn, project_id, role, role_key, number, now_ms, waiting)


def _target_of_sub(conn, principal, to: dict, now_ms: int, waiting) -> Target | None:
    """Ф36-2 A1: диалог владельца привязан к подписке (`to.sub_id`), а адрес запроса устарел: роль сменили на месте
    (`hub_subscribe(role=…)`), страница владельца ещё держит прежнее «без роли N». Подписка жива и в проекте адреса
    — адресат она сама, привязкой к подписке (как продолжение диалога в `_target_of_recipient`). Иначе `None`
    — остаётся прежний отказ: преемнику номера реплика владельца по-прежнему не уходит."""
    sub = subs_mod.get_sub(conn, to["sub_id"])
    if sub is None or sub.project_id != to["project_id"]:
        return None
    if not _project_seen(conn, principal, sub.project_id) or not _sub_live(sub, now_ms, waiting):
        return None
    return Target(sub.project_id, sub.role, sub.role_key, sub.role_no, sub.id, sub.id)


def _target_of_author(conn, rec: Rec, now_ms: int, waiting) -> Target:
    """Адресат ответа — автор сообщения `rec` (агент). Привязанный автор — та же подписка и только живая."""
    if _bound(rec.from_project_id, rec.from_role_key):
        sub = subs_mod.get_sub(conn, rec.from_sub_id) if rec.from_sub_id is not None else None
        if not _sub_live(sub, now_ms, waiting):
            raise PeerError(409, NOT_ONLINE)
        return Target(sub.project_id, sub.role, sub.role_key, sub.role_no, sub.id, sub.id)
    return _target_of_line(
        conn, rec.from_project_id, rec.from_role, rec.from_role_key, rec.from_no, now_ms, waiting
    )


def _target_of_recipient(conn, rec: Rec, now_ms: int, waiting) -> Target:
    """Адресат продолжения диалога владельца — тот же агент, которому ушло `rec`."""
    if rec.to_sub_id is not None:
        sub = subs_mod.get_sub(conn, rec.to_sub_id)
        if not _sub_live(sub, now_ms, waiting):
            raise PeerError(409, NOT_ONLINE)
        return Target(sub.project_id, sub.role, sub.role_key, sub.role_no, sub.id, sub.id)
    return _target_of_line(conn, rec.to_project_id, rec.to_role, rec.to_role_key, rec.to_no, now_ms, waiting)


# --- чьё (Н6) ----------------------------------------------------------------------------------------------


def _is_line(sub) -> bool:
    return sub.role_key is not None and sub.project_id != subs_mod.NO_PROJECT_ID


def _inbox_scope(sub) -> tuple[str, list]:
    """Входящие подписки `sub` (Н6): привязанные к ней самой; у линии с ролью в настоящем проекте — ещё и
    адресованные линии, кроме написанных этой же подпиской (сменила роль на месте и встала на линию адресата:
    своё сообщение — не входящее). Строки `to_user` не входят никогда."""
    if _is_line(sub):
        return (
            "m.to_user = 0 AND (m.to_sub_id = ? OR (m.to_sub_id IS NULL AND m.to_project_id = ? "
            "AND m.to_role_key = ? AND m.to_no = ? AND (m.from_sub_id IS NULL OR m.from_sub_id <> ?)))",
            [sub.id, sub.project_id, sub.role_key, sub.role_no, sub.id],
        )
    return "m.to_user = 0 AND m.to_sub_id = ?", [sub.id]


def _sent_scope(sub) -> tuple[str, list]:
    """Отправленные: своей подпиской; у линии с ролью в настоящем проекте — вся линия."""
    if _is_line(sub):
        return (
            "m.from_user = 0 AND (m.from_sub_id = ? OR (m.from_project_id = ? AND m.from_role_key = ? "
            "AND m.from_no = ?))",
            [sub.id, sub.project_id, sub.role_key, sub.role_no],
        )
    return "m.from_user = 0 AND m.from_sub_id = ?", [sub.id]


def _addressed(rec: Rec, sub) -> bool:
    """То же, что `_inbox_scope`, для одной строки."""
    if rec.to_user:
        return False
    if rec.to_sub_id is not None:
        return rec.to_sub_id == sub.id
    if rec.from_sub_id == sub.id:
        return False
    return _is_line(sub) and (rec.to_project_id, rec.to_role_key, rec.to_no) == (
        sub.project_id,
        sub.role_key,
        sub.role_no,
    )


# --- линии ------------------------------------------------------------------------------------------------

_LINE_PAIR = (
    "(m.from_project_id = ? AND m.from_role_key IS ? AND m.from_no = ? "
    "AND m.to_project_id = ? AND m.to_role_key IS ? AND m.to_no = ?)"
)


def _line(sub) -> tuple:
    return (sub.project_id, sub.role_key, sub.role_no)


def _implicit_parent(conn, sub, target: Target, now_ms: int) -> Rec | None:
    """П7: последнее сообщение линии адресата линии отправителя за 30 минут (строки владельца не учитываются,
    Н7) — новое сообщение без `reply_to` считается ответом на него."""
    rows = _select(
        conn,
        f"m.from_user = 0 AND m.to_user = 0 AND m.created_ts > ? AND {_LINE_PAIR} "
        "ORDER BY m.id DESC LIMIT 1",
        (now_ms - IMPLICIT_REPLY_MS, target.project_id, target.role_key, target.role_no, *_line(sub)),
    )
    return rows[0] if rows else None


# --- отправка ----------------------------------------------------------------------------------------------


def _insert(
    conn,
    *,
    sub,
    target: Target | None,
    to_user: bool,
    text: str,
    reply_to: int | None,
    chain_id: int | None,
    hop: int,
    status: str,
    reason: str | None,
    token_id: int | None,
    expires_ts: int | None,
    now_ms: int,
    urgent: bool = False,
    ask: bool = False,
    ask_about: str | None = None,
    money: bool = False,
) -> Rec:
    """`sub is None` — пишет владелец (`from_user`); `target is None` — адресат владелец (`to_user`). Ф56-1б: `ask` —
    вопрос владельцу (`hub_ask_owner`), `ask_about` — о какой роли. Ф93-1 (A.4): у сообщения владельцу (`to_user`)
    `money` — явный флаг ИЛИ слова трат в тексте (`card_route.money_text`); у прочих — всегда 0."""
    from . import card_route  # noqa: PLC0415 — маршрут карточек тянет модули очереди

    money = to_user and (bool(money) or card_route.money_text(text))
    sender = (
        (0, sub.id, sub.project_id, sub.role, sub.role_key, sub.role_no)
        if sub is not None
        else (1, None, None, None, None, None)
    )
    recipient = (
        (target.project_id, target.role, target.role_key, target.role_no, target.sub_id)
        if target is not None
        else (None, None, None, None, None)
    )
    cur = conn.execute(
        "INSERT INTO main.peer_msg (from_user, from_sub_id, from_project_id, from_role, from_role_key, "
        "from_no, to_user, to_project_id, to_role, to_role_key, to_no, to_sub_id, text, reply_to, chain_id, "
        "hop, status, reason, api_token_id, created_ts, updated_ts, closed_ts, expires_ts, seq, urgent, ask, "
        "ask_about, money) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        (
            *sender,
            int(to_user),
            *recipient,
            text,
            reply_to,
            chain_id,
            hop,
            status,
            reason,
            token_id,
            now_ms,
            now_ms,
            None,
            expires_ts,
            next_seq(conn),
            int(bool(urgent)),
            int(bool(ask)),
            ask_about,
            int(money),
        ),
    )
    msg_id = cur.lastrowid
    if chain_id is None:
        conn.execute("UPDATE main.peer_msg SET chain_id = ? WHERE id = ?", (msg_id, msg_id))
    rec = get(conn, msg_id)
    assert rec is not None  # noqa: S101 — только что вставлена в этой транзакции
    return rec


def _audit(conn, *, principal, action: str, rec: Rec, now_ms: int, client_name, request_id: str) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=client_name,
        request_id=request_id,
        action=action,
        result="ok",
        detail={  # только числа: текста и ролей в аудите нет (решение 6)
            "msg_id": rec.id,
            "from_user": rec.from_user,
            "from_sub": rec.from_sub_id,
            "from_project": rec.from_project_id,
            "to_user": rec.to_user,
            "to_project": rec.to_project_id,
            "to_sub": rec.to_sub_id,
            "hop": rec.hop,
            "chars": len(rec.text),
        },
    )


def is_owner_agent(principal) -> bool:
    """Ф88-1 (П4): агент владельца — bearer ровно со скоупом `human` (как `owner_queue.is_agent`; здесь — без
    импорта очереди: она сама опирается на этот модуль)."""
    return not principal.is_ui and principal.scopes == HUMAN_SCOPES


def send(
    conn: sqlite3.Connection,
    *,
    principal,
    session: dict | None,
    text: str,
    to: dict | None,
    reply_to: int | None,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    waiting: Callable[[int], bool] | None = None,
    urgent: bool = False,
) -> Sent:
    """`POST /peer-msgs`. Ровно одно из `to`/`reply_to`. Ф48-4: `urgent` — срочное сообщение агента (мимо
    пачки адресата и забирает накопленное); у сообщения владельца и ответа владельцу не хранится.

    Bearer — подписанная сессия (`session`), UI-сессия
    владельца — без неё (`from_user`). Отказы — `PeerError`: 404 — проекта, агента или сообщения нет (линия не
    жила 7 дней); 409 — нет подписки, адресат «только живому» не в сети, по адресу несколько агентов; 403 —
    обмен выключен у проекта; 422 — текст, себе, ответ владельцу позже суток."""
    if not isinstance(text, str) or not text.strip():
        raise PeerError(422, "text: пустое сообщение")
    if len(text) > TEXT_MAX:
        raise PeerError(422, f"text: не длиннее {TEXT_MAX} символов, получено {len(text)}")
    if (to is None) == (reply_to is None):
        raise PeerError(422, "нужно ровно одно: to или reply_to")
    audit = {"principal": principal, "now_ms": now_ms, "client_name": client_name, "request_id": request_id}
    if principal.is_ui or is_owner_agent(principal):  # Ф88-1 (П4): агент владельца пишет как кука
        if session is not None:
            raise PeerError(422, "session — только у bearer-токена сессии")
        return _send_user(conn, text=text, to=to, reply_to=reply_to, waiting=waiting, audit=audit)

    if to is not None and to.get("sub_id") is not None:
        raise PeerError(422, "to.sub_id — только из интерфейса хаба; агенту адрес — проект, роль и номер")
    sub = session_sub(conn, principal, session)
    parent: Rec | None = None
    target: Target | None = None
    if reply_to is not None:
        parent = get(conn, reply_to)
        if parent is None or not _addressed(parent, sub):
            raise PeerError(404, "сообщение не найдено среди адресованных этой сессии")
        if parent.from_user:
            # Решение 16, Н8: ответ владельцу — только на его сообщение и в течение суток.
            if now_ms - parent.created_ts > USER_REPLY_MS:
                raise PeerError(422, "ответ владельцу — в течение суток от его сообщения; напиши в своей сессии")
        else:
            target = _target_of_author(conn, parent, now_ms, waiting)
            if not _project_seen(conn, principal, target.project_id):
                raise PeerError(404, "проект адресата не найден")
    else:
        target = _target_of_request(conn, principal, to, now_ms, waiting)  # type: ignore[arg-type]

    if target is not None:
        target_line = (target.project_id, target.role_key, target.role_no)
        if target.sub_id == sub.id or (target.sub_id is None and _is_line(sub) and _line(sub) == target_line):
            raise PeerError(422, "себе писать нельзя")
        if not project_enabled(conn, sub.project_id) or not project_enabled(conn, target.project_id):
            raise PeerError(
                403, "обмен сообщениями агентов выключен у проекта (hub project set --agent-msgs)"
            )

    chain_id: int | None = None
    # Цепочка ответов (`chain_id`) — от явного `reply_to` или последнего сообщения собеседника за 30 минут;
    # глубину (`hop`) больше не считаем — пределов нет (Ф42-2), пишем как у первого сообщения.
    implicit = _implicit_parent(conn, sub, target, now_ms) if target is not None else None
    if parent is not None:
        chain_id = parent.chain_id
    elif implicit is not None:
        chain_id = implicit.chain_id

    row = {
        "sub": sub,
        "target": target,
        "to_user": target is None,
        "text": text,
        "reply_to": reply_to,
        "chain_id": chain_id,
        "hop": 0,
        "token_id": principal.token_id,
        "now_ms": now_ms,
    }
    live_sub_id = target.live_sub_id if target is not None else None
    expires_ts = None if target is None else now_ms + AGENT_TTL_MS  # Н7: ответ владельцу не истекает
    urgent = bool(urgent) and target is not None
    # Ф48-4: срок пачки адресата — ДО вставки (первое ждущее сообщение ставит срок).
    batch_ts = _batch_arrive(conn, live_sub_id, now_ms) if target is not None and not urgent else None
    rec = _insert(conn, status="pending", reason=None, expires_ts=expires_ts, urgent=urgent, **row)
    _audit(conn, action="peer_msg_send", rec=rec, **audit)
    return Sent(rec, live_sub_id, batch_ts)


def _send_user(conn, *, text: str, to: dict | None, reply_to: int | None, waiting, audit: dict) -> Sent:
    """Решение 15: сообщение владельца из UI. Выключатель проекта к нему не применяется (он про обмен
    агентов). `reply_to` — ответ на сообщение агента владельцу или продолжение диалога (своё прежнее сообщение)."""
    principal, now_ms = audit["principal"], audit["now_ms"]
    chain_id: int | None = None
    hop = 0
    if reply_to is not None:
        parent = get(conn, reply_to)
        if parent is None:
            raise PeerError(404, "сообщение не найдено")
        if parent.to_user:
            target = _target_of_author(conn, parent, now_ms, waiting)
        elif parent.from_user:
            target = _target_of_recipient(conn, parent, now_ms, waiting)
        else:
            raise PeerError(422, "reply_to: это не сообщение из диалога владельца с агентом")
        chain_id, hop = parent.chain_id, parent.hop + 1
    else:
        # Диалог владельца с безрольным и с агентом вне проектов привязан к ПОДПИСКЕ, а адрес — номер: подписка
        # кончилась, номер заняла другая сессия — реплика владельца (для агента это команда) ей не уходит.
        want = to.get("sub_id")  # type: ignore[union-attr]
        try:
            target = _target_of_request(conn, principal, to, now_ms, waiting)  # type: ignore[arg-type]
            if want is None and target.sub_id is not None:
                raise PeerError(422, "to.sub_id: безрольному и агенту вне проектов владелец пишет его подписке")
            if want is not None and target.live_sub_id != want:
                raise PeerError(409, SUB_GONE)
        except PeerError:
            # Ф36-2 A1: адрес устарел, а сама подписка диалога жива — пишем ей (роль сменили на месте).
            bound = _target_of_sub(conn, principal, to, now_ms, waiting) if want is not None else None  # type: ignore[arg-type]
            if bound is None:
                raise
            target = bound
    # Н4: линии с ролью в настоящем проекте сообщение владельца ждёт двое суток; привязанное живёт, пока жива
    # подписка (кончилась — `lost`), но не дольше общих 7 дней.
    expires_ts = now_ms + (AGENT_TTL_MS if target.sub_id is not None else USER_TTL_MS)
    rec = _insert(
        conn,
        sub=None,
        target=target,
        to_user=False,
        text=text,
        reply_to=reply_to,
        chain_id=chain_id,
        hop=hop,
        status="pending",
        reason=None,
        token_id=None,
        expires_ts=expires_ts,
        now_ms=now_ms,
    )
    _audit(conn, action="peer_msg_send", rec=rec, **audit)
    return Sent(rec, target.live_sub_id)


def reply_user(
    conn: sqlite3.Connection,
    *,
    text: str,
    reply_to: int,
    principal,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    waiting: Callable[[int], bool] | None = None,
) -> Sent:
    """Ф56-1б (4.2): ответ владельца по карточке `agent_msg` — тот же путь, что его ответ из интерфейса
    (`_send_user` -> `_target_of_author`): сообщение «ОТ ВЛАДЕЛЬЦА» автору с `reply_to`. Зовёт очередь канала
    владельца и для куки, и для токена его агента (`principal` — только для аудита: проверки `is_ui` здесь нет)."""
    if not isinstance(text, str) or not text.strip():
        raise PeerError(422, "text: пустое сообщение")
    if len(text) > TEXT_MAX:
        raise PeerError(422, f"text: не длиннее {TEXT_MAX} символов, получено {len(text)}")
    audit = {"principal": principal, "now_ms": now_ms, "client_name": client_name, "request_id": request_id}
    return _send_user(conn, text=text, to=None, reply_to=reply_to, waiting=waiting, audit=audit)


def ask_user(
    conn: sqlite3.Connection,
    *,
    principal,
    session: dict | None,
    text: str,
    about_role: str | None,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    money: bool = False,
) -> Sent:
    """Ф56-1б (4.3, А4б): `hub_ask_owner` — вопрос владельцу от сессии с живой подпиской-ролью: строка `to_user`
    без `reply_to`, без срока (Н7), `ask=1`. Правило суток (`USER_REPLY_MS`) не проверяется — это не ответ;
    выключатель обмена агентов проекта не применяется — это обращение к владельцу. UI — 422; подписки нет — 409;
    подписка без роли — 409; текст и `about_role` — 422. Аудит `peer_msg_ask` (без текста). Ф93-1 (A.4):
    `money` — вопрос о тратах (флаг сессии; слова трат в тексте ставят его и без флага)."""
    if principal.is_ui:
        raise PeerError(422, "вопрос владельцу задаёт сессия с подпиской, а не интерфейс")
    if not isinstance(text, str) or not text.strip():
        raise PeerError(422, "text: пустое сообщение")
    if len(text) > TEXT_MAX:
        raise PeerError(422, f"text: не длиннее {TEXT_MAX} символов, получено {len(text)}")
    if about_role is not None:
        if not isinstance(about_role, str) or not about_role.strip():
            raise PeerError(422, "about_role: роль, о которой вопрос, — не пустая строка")
        about_role = " ".join(about_role.split())
        if len(about_role) > ASK_ABOUT_MAX:
            raise PeerError(422, f"about_role: не длиннее {ASK_ABOUT_MAX} символов")
    sub = session_sub(conn, principal, session)
    if sub.role_key is None:
        raise PeerError(409, NEED_ROLE)
    rec = _insert(
        conn,
        sub=sub,
        target=None,
        to_user=True,
        text=text,
        reply_to=None,
        chain_id=None,
        hop=0,
        status="pending",
        reason=None,
        token_id=principal.token_id,
        expires_ts=None,
        now_ms=now_ms,
        ask=True,
        ask_about=about_role,
        money=money,
    )
    _audit(
        conn,
        principal=principal,
        action="peer_msg_ask",
        rec=rec,
        now_ms=now_ms,
        client_name=client_name,
        request_id=request_id,
    )
    return Sent(rec, None, None)


# --- сигнал подписке (решение 6, П4, П5, Н1, Н7) -----------------------------------------------------------


def batch_left_ms(conn: sqlite3.Connection, sub, now_ms: int) -> int:
    """Ф48-4: сколько ещё держится пачка сообщений агентов подписки `sub`, мс (0 — не держится: интервала
    нет, срок не стоит или прошёл). Интервал — действующий СЕЙЧАС (`subs.agent_batch_of`): снятый интервал
    старый срок не держит."""
    ts = subs_mod.agent_batch_row(conn, sub.id)[1]
    if ts is None or ts <= now_ms or subs_mod.agent_batch_of(conn, sub) <= 0:
        return 0
    return ts - now_ms


_BATCH_FREE = "(m.from_user = 1 OR m.urgent = 1)"  # Ф48-4: мимо пачки — владелец и срочное


def _batch_arrive(conn: sqlite3.Connection, live_sub_id: int | None, now_ms: int) -> int | None:
    """Ф48-4 п.1: несрочное сообщение агента ложится живой подписке адресата. Интервала нет или с прошлого
    сигнала о сообщениях агентов прошло не меньше интервала (срок не стоит или прошёл) — `None`: сигнал
    сразу. Срок ещё стоит: первое ждущее сообщение пачки переставляет его на «сейчас + интервал», остальные
    копятся к тому же сроку. Возвращает срок, которого будет ждать сообщение. Зовётся ДО вставки строки."""
    sub = subs_mod.get_sub(conn, live_sub_id) if live_sub_id is not None else None
    if sub is None or sub.ended_ts is not None:
        return None
    minutes = subs_mod.agent_batch_of(conn, sub)
    gate_ts = subs_mod.agent_batch_row(conn, sub.id)[1]
    if minutes <= 0 or gate_ts is None or gate_ts <= now_ms:
        return None
    where, params = _inbox_scope(sub)
    waiting = conn.execute(
        f"SELECT COUNT(*) FROM main.peer_msg m WHERE m.status = 'pending' AND m.signals = 0 "  # noqa: S608
        f"AND NOT {_BATCH_FREE} AND {where}",
        params,
    ).fetchone()[0]
    if waiting:
        return gate_ts
    ready_ts = now_ms + minutes * _MIN_MS
    conn.execute("UPDATE main.session_sub SET agent_batch_ts = ? WHERE id = ?", (ready_ts, sub.id))
    return ready_ts


def _signal_pred(sub, now_ms: int, *, held: bool = False) -> tuple[str, list]:
    """П5: ЕДИНСТВЕННОЕ условие «сообщению пора сигналить подписке `sub`» — и для счёта, и для выдачи; только
    поля строки и время. Первый сигнал — сразу, второй — через `RESIGNAL_MS` после первого, если не прочитано
    (П4); больше двух не бывает. `to_user = 0` — в `_inbox_scope` (Н7). Ф48-4: `held` — пачка подписки
    держится (`batch_left_ms` > 0): готовы только сообщения владельца и срочные."""
    where, params = _inbox_scope(sub)
    gate = f"AND {_BATCH_FREE} " if held else ""
    return (
        f"m.status = 'pending' AND {where} {gate}"
        "AND (m.signals = 0 OR (m.signals < ? AND m.signaled_ts IS NOT NULL AND m.signaled_ts <= ?))",
        [*params, MAX_SIGNALS, now_ms - RESIGNAL_MS],
    )


def _pred_count(conn: sqlite3.Connection, sub, now_ms: int, *, held: bool) -> int:
    where, params = _signal_pred(sub, now_ms, held=held)
    return conn.execute(
        f"SELECT COUNT(*) FROM main.peer_msg m WHERE {where}",  # noqa: S608 — фрагмент задан кодом
        params,
    ).fetchone()[0]


def signal_count(conn: sqlite3.Connection, sub, *, now_ms: int) -> int:
    """Читатель, для ожидания: сообщения, которым пора сигналить подписке СЕЙЧАС (Ф48-4: ждущие срока пачки
    не считаются — их отдаёт `batch_state`)."""
    return _pred_count(conn, sub, now_ms, held=batch_left_ms(conn, sub, now_ms) > 0)


def urgent_count(conn: sqlite3.Connection, sub, *, now_ms: int) -> int:
    """Ф50-1, читатель: сообщения, которым пора сигналить СЕЙЧАС и которые идут мимо пачки, — от владельца и
    срочные. Тот же предикат, что у `signal_count` при держащейся пачке."""
    return _pred_count(conn, sub, now_ms, held=True)


def batch_state(conn: sqlite3.Connection, sub, *, now_ms: int) -> tuple[int, int]:
    """Ф48-4, читатель: `(сообщений, ждущих срока пачки, мс до срока)`; пачка не держится или ждать нечему —
    `(0, 0)`."""
    left_ms = batch_left_ms(conn, sub, now_ms)
    if left_ms <= 0:
        return 0, 0
    later = _pred_count(conn, sub, now_ms, held=False) - _pred_count(conn, sub, now_ms, held=True)
    return (later, left_ms) if later > 0 else (0, 0)


def take_signals(
    conn: sqlite3.Connection,
    sub,
    *,
    now_ms: int,
    limit: int,
    api_token_id: int,
    client_name: str | None,
    request_id: str,
    woken: bool = True,
) -> list[dict]:
    """Отдать подписке сигналы сообщений — только числа: `{"kind": "peer", "msg_id", "from_sub",
    "from_project"}`, от владельца — `{"kind": "peer", "msg_id", "from_user": 1}` (Н1). Каждой строке — счётчик
    `signals`, `signaled_ts`. Не влезшие в `limit` остаются до следующей выдачи.

    Ф48-4: выдача срок пачки НЕ смотрит — раз сессия всё равно просыпается (зовут её, только когда у подписки
    есть готовое), накопленное уходит тем же сигналом; пока пачка держится, сообщения владельца и срочные —
    первыми. После выдачи подписке с интервалом ставится новый срок «сейчас + интервал» (не чаще раза в
    интервал); не влезло в `limit` — срок не трогается. `woken=False` — этой выдачей сессии больше ничего не
    ушло (карточки отсеяны при выдаче): пока пачка держится и нет ни сообщения владельца, ни срочного, накопленное
    остаётся ждать срока."""
    if limit <= 0:
        return []
    minutes = subs_mod.agent_batch_of(conn, sub)
    held = batch_left_ms(conn, sub, now_ms) > 0
    if held and not woken and _pred_count(conn, sub, now_ms, held=True) == 0:
        return []
    where, params = _signal_pred(sub, now_ms)
    order = f"{_BATCH_FREE} DESC, m.id" if held else "m.id"
    signals: list[dict] = []
    for rec in _select(conn, f"{where} ORDER BY {order} LIMIT ?", [*params, limit]):
        _update(conn, rec.id, now_ms, signal_sub_id=sub.id, signaled_ts=now_ms, signals=rec.signals + 1)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            action="sub_signal",
            result="delivered",
            detail={"sub_id": sub.id, "kind": "peer", "msg_id": rec.id},
        )
        if rec.from_user:
            signals.append({"kind": "peer", "msg_id": rec.id, "from_user": 1})
        else:
            signals.append(
                {
                    "kind": "peer",
                    "msg_id": rec.id,
                    "from_sub": rec.from_sub_id,
                    "from_project": rec.from_project_id,
                }
            )
    if signals and minutes > 0 and _pred_count(conn, sub, now_ms, held=False) == 0:
        conn.execute(
            "UPDATE main.session_sub SET agent_batch_ts = ? WHERE id = ?",
            (now_ms + minutes * _MIN_MS, sub.id),
        )
    return signals


def unread_count(conn: sqlite3.Connection, sub) -> int:
    """Решение 7: непрочитанные входящие подписки (в том числе уже отсигналенные) — `peer_unread` в ответе
    `POST /subs`."""
    where, params = _inbox_scope(sub)
    return conn.execute(
        f"SELECT COUNT(*) FROM main.peer_msg m WHERE m.status = 'pending' AND {where}",  # noqa: S608
        params,
    ).fetchone()[0]


# --- чтение ------------------------------------------------------------------------------------------------


def inbox(conn: sqlite3.Connection, sub, *, include_all: bool, now_ms: int) -> list[dict]:
    """`GET /peer-msgs` сессии: непрочитанные входящие; `include_all` — ещё прочитанные входящие и свои
    отправленные за 7 дней (со статусом). По возрастанию номера."""
    where, params = _inbox_scope(sub)
    clause = f"(m.status = 'pending' AND {where})"
    if include_all:
        sent, sent_params = _sent_scope(sub)
        clause += f" OR (m.created_ts > ? AND ((m.status = 'read' AND {where}) OR ({sent})))"
        params = [*params, now_ms - LIST_RECENT_MS, *params, *sent_params]
    rows = _select(conn, f"({clause}) ORDER BY m.id DESC LIMIT ?", [*params, LIST_MAX])
    return [msg_dict(conn, rec) for rec in reversed(rows)]


def mark_read(
    conn: sqlite3.Connection,
    ids: list[int],
    *,
    principal,
    sub,
    now_ms: int,
    client_name: str | None,
    request_id: str,
) -> int:
    """`POST /peer-msgs/read`: сессия (`sub`) — свои непрочитанные входящие; владелец (`sub is None`, UI) —
    только строки `to_user`. Чужие и уже прочитанные — мимо. Возвращает число отмеченных."""
    ids = sorted(set(ids))[:MARK_MAX]
    if not ids:
        return 0
    if sub is not None:
        where, params = _inbox_scope(sub)
    else:
        where, params = "m.to_user = 1", []
    marks = ",".join("?" for _ in ids)
    rows = _select(
        conn, f"m.id IN ({marks}) AND m.status = 'pending' AND {where} ORDER BY m.id", [*ids, *params]
    )
    for rec in rows:
        _update(
            conn,
            rec.id,
            now_ms,
            status="read",
            read_ts=now_ms,
            read_sub_id=sub.id if sub is not None else None,
        )
    if rows:
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="peer_msg_read",
            result="ok",
            detail={"sub_id": sub.id if sub is not None else None, "ids": [rec.id for rec in rows]},
        )
    return len(rows)


def _scope(principal, where: list[str], params: list) -> bool:
    """Скоуп проектов принципала: строка его, если ВСЕ проекты строки (отправителя и адресата) — из его
    списка (проект 0 в списке не бывает). Без списка — всё. `False` — ему не видно ничего."""
    if principal.project_ids is None:
        return True
    ids = sorted(principal.project_ids)
    if not ids:
        return False
    marks = ",".join("?" for _ in ids)
    where.append(
        f"(m.from_project_id IS NULL OR m.from_project_id IN ({marks})) "
        f"AND (m.to_project_id IS NULL OR m.to_project_id IN ({marks}))"
    )
    params += [*ids, *ids]
    return True


def list_for_ui(conn: sqlite3.Connection, principal, *, limit: int, before: int | None) -> list[dict]:
    """`GET /peer-msgs` UI-сессии: вся переписка в скоупе проектов, новые первыми; `before` — номер, раньше
    которого листать."""
    where = ["1 = 1"]
    params: list = []
    if not _scope(principal, where, params):
        return []
    if before is not None:
        where.append("m.id < ?")
        params.append(before)
    limit = max(1, min(int(limit), LIST_MAX))
    rows = _select(conn, " AND ".join(where) + " ORDER BY m.id DESC LIMIT ?", [*params, limit])
    return [msg_dict(conn, rec) for rec in rows]


def changes(conn: sqlite3.Connection, *, since: int, watermark: int, limit: int) -> list[dict]:
    """Строки с `since < seq <= watermark` по возрастанию `seq` (`/sync`, SSE). Отдавать ТОЛЬКО UI-сессии
    (решение 6) — иначе любая сессия прочла бы всю переписку."""
    rows = _select(conn, "m.seq > ? AND m.seq <= ? ORDER BY m.seq LIMIT ?", (since, watermark, limit))
    return [msg_dict(conn, rec) for rec in rows]


# --- срок и конец подписки ---------------------------------------------------------------------------------


def has_expired(conn: sqlite3.Connection, now_ms: int) -> bool:
    """Читатель минутного цикла: есть ли ждущие с прошедшим сроком."""
    row = conn.execute(
        "SELECT 1 FROM main.peer_msg WHERE status = 'pending' AND expires_ts IS NOT NULL AND expires_ts <= ? "
        "LIMIT 1",
        (now_ms,),
    ).fetchone()
    return row is not None


def expire(conn: sqlite3.Connection, now_ms: int) -> int:
    """Ждущие с прошедшим сроком -> `expired`. Строки без срока (`to_user`, Н7) не трогаются."""
    rows = _select(
        conn,
        "m.status = 'pending' AND m.expires_ts IS NOT NULL AND m.expires_ts <= ? ORDER BY m.id",
        (now_ms,),
    )
    for rec in rows:
        _update(conn, rec.id, now_ms, status="expired", closed_ts=now_ms, reason="expired")
    return len(rows)


def on_sub_end(conn: sqlite3.Connection, sub_id: int, *, now_ms: int) -> int:
    """Подписка кончилась: ждущие, привязанные к ней (`to_sub_id`), -> `lost` (решение 5, Н4)."""
    rows = _select(conn, "m.status = 'pending' AND m.to_sub_id = ? ORDER BY m.id", (sub_id,))
    for rec in rows:
        _update(conn, rec.id, now_ms, status="lost", closed_ts=now_ms, reason="sub_ended")
    return len(rows)


def rebind_lost(conn: sqlite3.Connection, old_sub, new_sub_id: int, *, now_ms: int) -> int:
    """Ф41-2а П7 (ревью Н1): подписка `old_sub` сессии хаба снята сменой токена, та же сессия подписалась
    снова (`new_sub_id`) — сообщения, ушедшие в `lost` ИМЕННО этим завершением (`reason = sub_ended`,
    `closed_ts` = момент конца подписки), снова `pending` и привязаны к новой подписке. Срок (`expires_ts`)
    не продлевается: истёкшие к этому моменту остаются `lost`; прочитанные и истёкшие по сроку сюда не
    попадают. Число возвращённых."""
    if old_sub.ended_ts is None:
        return 0
    rows = _select(
        conn,
        "m.status = 'lost' AND m.reason = 'sub_ended' AND m.to_sub_id = ? AND m.closed_ts = ? "
        "AND (m.expires_ts IS NULL OR m.expires_ts > ?) ORDER BY m.id",
        (old_sub.id, old_sub.ended_ts, now_ms),
    )
    for rec in rows:
        _update(conn, rec.id, now_ms, status="pending", closed_ts=None, reason=None, to_sub_id=new_sub_id)
    return len(rows)


# --- уведомление владельцу (П7, Н8) -----------------------------------------------------------------------------

_ALERT = "m.notified_ts IS NULL AND m.to_user = 1 AND m.status = 'pending'"


def alerts(conn: sqlite3.Connection, principal) -> list[dict]:
    """`GET /agent/peer-msgs/alerts`: непрочитанные ответы владельцу, уведомление по которым
    ещё не уходило; принципалу со списком проектов — только строки его проектов (Н8)."""
    where = [_ALERT]
    params: list = []
    if not _scope(principal, where, params):
        return []
    rows = _select(conn, " AND ".join(where) + " ORDER BY m.id LIMIT ?", [*params, LIST_MAX])
    return [msg_dict(conn, rec) for rec in rows]


def mark_notified(conn: sqlite3.Connection, ids: list[int], principal, *, now_ms: int) -> int:
    """Отметка `notified_ts` (уведомление ушло — один раз). Уже отмеченные и вне
    скоупа — мимо."""
    ids = sorted(set(ids))[:MARK_MAX]
    where = [f"m.id IN ({','.join('?' for _ in ids)}) AND m.notified_ts IS NULL"]
    params: list = list(ids)
    if not ids or not _scope(principal, where, params):
        return 0
    cur = conn.execute(
        f"UPDATE main.peer_msg AS m SET notified_ts = ? WHERE {' AND '.join(where)}",  # noqa: S608
        (now_ms, *params),
    )
    return cur.rowcount


# --- Ф41-5б: сообщения по заявкам агентов (docs/architecture.md) ---------------------


def note_from_user(conn, *, sub, text: str, now_ms: int, waiting=None) -> Sent | None:
    """П9: служебное сообщение «ОТ ВЛАДЕЛЬЦА» линии роли подписки `sub` — исход заявки агента (текст собирает
    ядро: только номера и постоянные слова плюс слова владельца). Линия без роли, вне проектов или не жившая 7
    дней — `None` (исход остаётся только статусом заявки). Аудит `peer_msg_send` (actor `ui`), без текста."""
    if sub is None or not _is_line(sub):
        return None
    try:
        target = _target_of_line(conn, sub.project_id, sub.role, sub.role_key, sub.role_no, now_ms, waiting)
    except PeerError:
        return None
    rec = _insert(
        conn,
        sub=None,
        target=target,
        to_user=False,
        text=text[:TEXT_MAX],
        reply_to=None,
        chain_id=None,
        hop=0,
        status="pending",
        reason=None,
        token_id=None,
        expires_ts=now_ms + USER_TTL_MS,
        now_ms=now_ms,
    )
    _system_audit(conn, rec, now_ms=now_ms, actor="ui")
    return Sent(rec, target.live_sub_id)


def task_to_line(
    conn, *, sub, project_id: int, role: str, role_key: str, role_no: int, text: str, now_ms: int
) -> Rec:
    """5.3: первая задача напарнику — сообщение агента от линии просителя (`sub` — его подписка на момент
    заявки) линии только что заведённой сессии хаба. Линия ещё не подписана (подписку оформит первый запуск
    по `want_sub`) — строка адресуется линии и ждёт её 7 дней, как любое сообщение агента."""
    target = Target(project_id, role, role_key, role_no, None, None)
    rec = _insert(
        conn,
        sub=sub,
        target=target,
        to_user=False,
        text=text[:TEXT_MAX],
        reply_to=None,
        chain_id=None,
        hop=0,
        status="pending",
        reason=None,
        token_id=sub.api_token_id,
        expires_ts=now_ms + AGENT_TTL_MS,
        now_ms=now_ms,
    )
    _system_audit(conn, rec, now_ms=now_ms, actor="api", token_id=sub.api_token_id)
    return rec


def _system_audit(conn, rec: Rec, *, now_ms: int, actor: str, token_id: int | None = None) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=token_id,
        client_name=None,
        request_id=f"agent-request-msg-{rec.id}",
        action="peer_msg_send",
        result="ok",
        detail={
            "msg_id": rec.id,
            "from_user": rec.from_user,
            "from_sub": rec.from_sub_id,
            "from_project": rec.from_project_id,
            "to_user": rec.to_user,
            "to_project": rec.to_project_id,
            "to_sub": rec.to_sub_id,
            "hop": rec.hop,
            "chars": len(rec.text),
            "agent_request": True,
        },
    )
