"""Операции над группами WhatsApp (Ф62-6, docs/architecture.md и поправки П1–П16 — они
главнее текста разделов): создать группу, добавить или убрать участников, получить ссылку-приглашение
(`link`, чтение — без карточки, П5), сбросить ссылку (`link_reset`).

Владелец из интерфейса (`create_ui`) — заявка сразу `approved`. Сессия (`create_session`, bearer `draft`, запреты
`check_attach`) — `pending` (карточка владельцу и в канал владельца), либо сразу `approved` с `auto = 1`, когда сессия
хаба в авто и выключатель автопилота `wa_group` снят (`core/autopilot.session_free`, решает маршрут). `link`
— всегда сразу (чтение). Решает владелец кликом (`decide`, ручки `ui_only`) или его агент из канала владельца
(`owner_queue`, ответ агента = клик владельца).

Очередь «ядро -> коннектор» — по форме выхода (`lease`/`wait_state`/`report`): одна операция аккаунта за раз,
темп между пишущими операциями `GAP_MS` (у `link` — `LINK_GAP_MS`), суточные потолки аккаунта (создать 5,
участников добавить 50, сбросить ссылку 10) — сдвигом срока выдачи; FloodWait/бан — пауза всем открытым
заявкам аккаунта (`retry_after_ms`), предел поиска номеров коннектора — отсрочка одной строки
(`postpone_ms`, попыткой не считается).

Номера участников (П9) превращает в jid только коннектор внутри задания; сырой номер (`input`) хранится до
закрытия заявки и стирается при `done`/`failed`/`declined` — остаются маска и jid. В аудите и журнале —
маска. После `create` беседу группы создаёт сам отчёт `done` (`_ensure_group_channel` ->
`ingest.ensure_group_channel`: WhatsApp в новую группу ничего не шлёт); пришло сообщение раньше отчёта —
отчёт привязывает уже существующую, после отчёта — `on_channel_created` из `ingest`. Беседа одна, в проекте
заявки, НЕ глушится (П12).

Исход заявки сессии — сигналом её автору (`take_signals`, вид `group_op`, срочный). Каждая запись в строку
двигает глобальный `seq`: `/sync` (`group_ops`) и кадр SSE `group_op`. Пишет только внутри `Db.write()`,
время — параметром `now_ms`.
"""

import json
import re
import sqlite3
from types import SimpleNamespace
from typing import NamedTuple

from . import held as held_mod
from . import subs as subs_mod
from .audit import write_audit
from .db import db_of, next_seq
from .security import check_attach

# fake — для тестов и стенда, как у вступления и выхода.
SOURCES = frozenset({"whatsapp", "fake"})
KINDS = ("create", "add", "remove", "link", "link_reset")
# Действия, которые видят люди: сессии — карточкой (кроме авто при снятом `wa_group`).
CARD_KINDS = frozenset({"create", "add", "remove", "link_reset"})
# Чтение без карточки и без потолков (темп `LINK_GAP_MS`): ссылка и состав группы. `members` заводит только
# `request_members` (ручка `GET /channels/{id}/group-members?refresh=1`), не `POST /group-ops`.
READ_KINDS = frozenset({"link", "members"})
MEMBERS_ROWS_MAX = 1100  # WhatsApp: до 1024 участников
SWITCH = "wa_group"

OPEN_STATUSES = ("pending", "approved")
FINAL_STATUSES = ("done", "failed", "declined")
STATUSES = (*OPEN_STATUSES, *FINAL_STATUSES)
REPORT_OUTCOMES = frozenset({"done", "retry", "drop"})
# Коды участника в итоге (мост, раздел C; `bad` — кривой адрес, `not_found` — номер не в WhatsApp).
PARTICIPANT_CODES = frozenset(
    {
        "ok",
        "privacy",
        "already",
        "not_member",
        "no_whatsapp",
        "no_pn",
        "self",
        "left_recently",
        "error",
        "bad",
    }
)

TITLE_MAX = 25  # П4: whatsmeow — длиннее 25 символов сервер отвечает 406
MEMBERS_MAX = 30
REASON_MAX = 500
ERROR_MAX = 200
NAME_MAX = 200
EXT_ID_MAX = 64
LINK_RE = re.compile(r"^https://chat\.whatsapp\.com/[A-Za-z0-9_-]{8,64}$")
_JID_RE = re.compile(r"^\d{5,20}@(s\.whatsapp\.net|lid)$")
_GROUP_JID_RE = re.compile(r"^[0-9-]{5,40}@g\.us$")
_CHANNEL_REF_RE = re.compile(r"^#?(\d{1,16})$")

_MIN_MS = 60_000
DAY_MS = 24 * 60 * _MIN_MS
# Лимиты просьб сессий (от спама карточками); у владельца лимитов нет.
OPEN_PER_PROJECT = 20
NEW_PER_PROJECT_DAY = 50
# Темп и потолки аккаунта (номер личный — бан дороже): между пишущими операциями — минута; `link` — чтение.
GAP_MS = _MIN_MS
LINK_GAP_MS = 10_000
CREATE_DAILY = 5
ADDED_DAILY = 50
RESET_DAILY = 10
LEASE_MS = 120_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_MS = 30_000
MAX_LEASE_LIMIT = 10
MAX_POSTPONE_MS = DAY_MS
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})
# Исход — автору сигналом, пока свежий.
OUTCOME_TTL_MS = DAY_MS

LIST_RECENT_MS = 7 * DAY_MS
LIST_MAX = 200

BIND_REASON = "группа создана по заявке #{id}"

_NOT_WA = "аккаунт не WhatsApp — группы создаются только в WhatsApp"
_NOT_GROUP = "это не группа WhatsApp"


class GroupOpError(Exception):
    """Базовая ошибка; текст — для человека/сессии, без номеров и названий."""


class Invalid(GroupOpError):
    """422: кривые поля, не тот аккаунт или беседа, участников нет."""


class Denied(GroupOpError):
    """403: этому токену просить нельзя."""


class NotFound(GroupOpError):
    """404: заявки, беседы или проекта нет или не видны принципалу; чужой аккаунт в отчёте."""


class Conflict(GroupOpError):
    """409: решение не к этому статусу, шаг уже выдавался."""


class TooMany(GroupOpError):
    """429: лимиты просьб проекта."""


class StaleLease(GroupOpError):
    """409: `lease_gen` не совпал или шаг уже закрыт."""


# --- строки -------------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "account_id",
    "project_id",
    "channel_id",
    "ext_id",
    "kind",
    "title",
    "participants_json",
    "reason",
    "origin",
    "status",
    "auto",
    "result_json",
    "error",
    "token_id",
    "client_name",
    "session_json",
    "hub_session_id",
    "author_sub_id",
    "author_project_id",
    "author_role_key",
    "author_role_no",
    "signaled_ts",
    "created_ts",
    "decided_ts",
    "done_ts",
    "updated_ts",
    "not_before_ms",
    "pause_until_ms",
    "attempts",
    "lease_until_ms",
    "lease_gen",
    "first_leased_ts",
    "last_leased_ts",
    "seq",
)
_SELECT = ", ".join(f"g.{c}" for c in _COLUMNS)


class Op(NamedTuple):
    id: int
    account_id: int
    project_id: int | None
    channel_id: int | None
    ext_id: str | None
    kind: str
    title: str | None
    participants_json: str | None
    reason: str | None
    origin: str
    status: str
    auto: int
    result_json: str | None
    error: str | None
    token_id: int | None
    client_name: str | None
    session_json: str | None
    hub_session_id: int | None
    author_sub_id: int | None
    author_project_id: int | None
    author_role_key: str | None
    author_role_no: int | None
    signaled_ts: int | None
    created_ts: int
    decided_ts: int | None
    done_ts: int | None
    updated_ts: int
    not_before_ms: int
    pause_until_ms: int
    attempts: int
    lease_until_ms: int | None
    lease_gen: int
    first_leased_ts: int | None
    last_leased_ts: int | None
    seq: int


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Op]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.group_op g WHERE {where}",  # noqa: S608 — where — из кода
        tuple(params),
    ).fetchall()
    return [Op(*r) for r in rows]


def get(conn: sqlite3.Connection, op_id: int) -> Op | None:
    rows = _select(conn, "g.id = ?", (op_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, op_id: int, now_ms: int, **sets) -> None:
    """Любая запись в заявку двигает `seq` и `updated_ts`."""
    sets["updated_ts"] = now_ms
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.group_op SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), op_id],
    )


def _loads(raw: str | None, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return default
    return value if isinstance(value, type(default)) else default


def participants_of(op: Op) -> list[dict]:
    return _loads(op.participants_json, [])


def result_of(op: Op) -> dict:
    return _loads(op.result_json, {})


def _role(op: Op) -> str | None:
    session = _loads(op.session_json, {})
    role = session.get("role")
    return role if isinstance(role, str) else None


def op_dict(op: Op) -> dict:
    """Выдача API (`/group-ops`, `/sync`, MCP): без токена, номеров и jid участников — только маска, имя
    беседы-лички и код итога. `title` и `reason` пишут люди/сессии — UI показывает только текстом, MCP —
    внутри блока данных. `link` — ссылка-приглашение из итога (`link`/`link_reset`)."""
    codes = {p.get("i"): p for p in result_of(op).get("participants", []) if isinstance(p, dict)}
    people = []
    for n, p in enumerate(participants_of(op)):
        row = {
            "i": n,
            "kind": p.get("kind"),
            "mask": p.get("mask"),
            "name": p.get("name"),
            "channel_id": p.get("channel_id"),
        }
        got = codes.get(n)
        if got is not None:
            row["code"] = got.get("code")
            if got.get("invite"):
                row["invite"] = True
        people.append(row)
    result = result_of(op)
    return {
        "id": op.id,
        "account_id": op.account_id,
        "project_id": op.project_id,
        "channel_id": op.channel_id,
        "kind": op.kind,
        "title": op.title,
        "participants": people,
        "reason": op.reason,
        "origin": op.origin,
        "status": op.status,
        "error": op.error,
        "link": result.get("link") if isinstance(result.get("link"), str) else None,
        "client_name": op.client_name,
        "role": _role(op),
        "created_ts": op.created_ts,
        "decided_ts": op.decided_ts,
        "done_ts": op.done_ts,
        "updated_ts": op.updated_ts,
        # «Отказать» — только пока шаг ни разу не выдавался.
        # Как в `decide`: ждущую решения — всегда (в том числе удержанную после восстановления, Ф33-3).
        "can_decline": op.status == "pending" or (op.status == "approved" and op.first_leased_ts is None),
        "seq": op.seq,
        **({"auto": True} if op.auto else {}),
    }


def _audit(
    conn: sqlite3.Connection,
    op: Op,
    *,
    action: str,
    result: str,
    now_ms: int,
    actor: str = "core",
    request_id: str = "",
    api_token_id: int | None = None,
    client_name: str | None = None,
    detail: dict | None = None,
) -> None:
    """Аудит без текстов и номеров: id заявки, вид, число участников, беседа, проект, аккаунт."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or f"group-op-{op.id}",
        action=action,
        result=result,
        channel_id=op.channel_id,
        detail={
            "group_op_id": op.id,
            "kind": op.kind,
            "project_id": op.project_id,
            "account_id": op.account_id,
            "participants": len(participants_of(op)),
            **(detail or {}),
        },
    )


# --- проверки на входе --------------------------------------------------------------------------------------


class _Chan(NamedTuple):
    id: int
    account_id: int
    ext_id: str
    kind: str
    title: str | None
    project_id: int | None
    archived: int


def _chan(conn: sqlite3.Connection, channel_id: int) -> _Chan | None:
    if channel_id <= 0 or channel_id > 2**53:
        return None
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT id, account_id, ext_id, kind, title, project_id, archived FROM {schema}.channel "  # noqa: S608
        "WHERE id = ?",
        (channel_id,),
    ).fetchone()
    return _Chan(*row) if row is not None else None


def _account(conn: sqlite3.Connection, account_id: int) -> tuple[str, str, str] | None:
    """`(схема, статус, источник)` аккаунта или `None`."""
    row = conn.execute("SELECT db, status, source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1], row[2]) if row is not None else None


def pick_account(conn: sqlite3.Connection, account_id: int | None) -> int:
    """П14: аккаунт WhatsApp. Без `account_id` — единственный аккаунт WhatsApp в хабе, иначе отказ с
    перечнем."""
    if account_id is not None:
        info = _account(conn, account_id)
        if info is None:
            raise Invalid("account_id: нет такого аккаунта")
        if info[2] not in SOURCES:
            raise Invalid(_NOT_WA)
        return account_id
    rows = conn.execute("SELECT id FROM main.account WHERE source = 'whatsapp' ORDER BY id").fetchall()
    if not rows:
        raise Invalid("account_id: в хабе нет аккаунта WhatsApp")
    if len(rows) > 1:
        ids = ", ".join(str(r[0]) for r in rows)
        raise Invalid(f"account_id: аккаунтов WhatsApp несколько ({ids}) — укажи account_id")
    return rows[0][0]


def _group_chan(conn: sqlite3.Connection, channel_id: int) -> _Chan:
    """Беседа-группа WhatsApp (или fake) — для `add`/`remove`/`link`/`link_reset`."""
    from . import draft_channels

    chan = _chan(conn, channel_id)
    if chan is None:
        raise NotFound(f"беседа #{channel_id} не найдена")
    info = _account(conn, chan.account_id)
    if info is None or info[2] not in SOURCES or chan.kind != "group":
        raise Invalid(_NOT_GROUP)
    if draft_channels.is_temp_ext_id(chan.ext_id):
        raise Invalid("это заготовка нового адресата, а не группа")
    return chan


def _known_dm(conn: sqlite3.Connection, account_id: int, jid: str) -> bool:
    """Есть ли у аккаунта беседа с этим jid (личка, которую хаб уже видел)."""
    info = _account(conn, account_id)
    if info is None:
        return False
    row = conn.execute(
        f"SELECT 1 FROM {info[0]}.channel WHERE account_id = ? AND ext_id = ? AND kind = 'dm'",  # noqa: S608
        (account_id, jid),
    ).fetchone()
    return row is not None


def parse_participants(
    conn: sqlite3.Connection,
    account_id: int,
    raw: object,
    *,
    required: bool = True,
    project_ids: frozenset[int] | None = None,
    session: bool = False,
) -> list[dict]:
    """Участники: `#123` / число — беседа-личка WhatsApp того же аккаунта (её jid; у заявки сессии — только
    видимая токену и с проектом, иначе «не найдена», ревью Ф62-6 п. 3); `79…@s.whatsapp.net` или `…@lid` —
    jid как есть, если такая личка у аккаунта уже есть; номерной jid без лички — как номер (предел
    `wa_phone_daily` и маска, ревью п. 1), `@lid` без лички — отказ; иначе номер (`+7…`, `8…`, 8–15 цифр, как
    Ф34-3). До `MEMBERS_MAX`, дубли схлопнуты. Запись: `{kind, mask, jid, channel_id, name, input}` (`input` —
    сырой номер, только у номеров; стирается при закрытии заявки, П9)."""
    from . import new_recipient

    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise Invalid("participants: нужен список")
    if len(raw) > MEMBERS_MAX:
        raise Invalid(f"participants: не больше {MEMBERS_MAX} за операцию")
    out: list[dict] = []
    seen: set[str] = set()
    for n, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise Invalid(f"participants[{n}]: нужен номер, #id лички или jid")
        text = str(item).strip()
        ref = _CHANNEL_REF_RE.match(text) if (isinstance(item, int) or text.startswith("#")) else None
        if ref is not None:
            chan = _chan(conn, int(ref.group(1)))
            if chan is None or (
                session and (chan.project_id is None or not _visible(project_ids, chan.project_id))
            ):
                raise Invalid(f"participants[{n}]: беседа #{ref.group(1)} не найдена")
            if chan.account_id != account_id or chan.kind != "dm":
                raise Invalid(f"participants[{n}]: нужна личка WhatsApp этого же аккаунта")
            from . import draft_channels

            if draft_channels.is_temp_ext_id(chan.ext_id):
                raise Invalid(f"participants[{n}]: личка ещё не начата — укажи номер")
            entry = {
                "kind": "chat",
                "jid": chan.ext_id,
                "channel_id": chan.id,
                "name": (chan.title or "")[:NAME_MAX] or None,
                "mask": None,
                "input": None,
            }
            key = chan.ext_id
        elif _JID_RE.match(text) and _known_dm(conn, account_id, text):
            user, server = text.split("@", 1)
            mask = new_recipient.mask_phone("+" + user) if server == "s.whatsapp.net" else f"…{user[-4:]}@lid"
            entry = {
                "kind": "jid",
                "jid": text,
                "channel_id": None,
                "name": None,
                "mask": mask,
                "input": None,
            }
            key = text
        else:
            if _JID_RE.match(text):  # jid без лички в хабе (ревью Ф62-6 п. 1)
                if text.endswith("@lid"):
                    raise Invalid(
                        f"participants[{n}]: такого @lid нет среди личек хаба — укажи номер или #id лички"
                    )
                text = "+" + text.split("@", 1)[0]  # номерной jid — это номер: предел и маска как у номера
            try:
                phone = new_recipient.parse_tg_phone(text)
            except new_recipient.Invalid as exc:
                raise Invalid(f"participants[{n}]: нужен номер (+7…), #id лички или jid") from exc
            entry = {
                "kind": "phone",
                "jid": None,
                "channel_id": None,
                "name": None,
                "mask": new_recipient.mask_phone(phone),
                "input": phone,
            }
            key = phone[1:] + "@s.whatsapp.net"
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    if required and not out:
        raise Invalid("participants: нужен хотя бы один участник")
    return out


def _check_title(title: object) -> str:
    if not isinstance(title, str) or not title.strip():
        raise Invalid("title: нужно название группы")
    value = title.strip()
    if len(value) > TITLE_MAX:
        raise Invalid(f"title: не длиннее {TITLE_MAX} символов (предел WhatsApp)")
    return value


def _visible(project_ids: frozenset[int] | None, project_id: int | None) -> bool:
    """Заявки и беседы без проекта видит только принципал без списка проектов (UI-сессия, профиль с
    «Неразобранным»)."""
    return project_ids is None or (project_id is not None and project_id in project_ids)


class _Target(NamedTuple):
    account_id: int
    project_id: int | None
    channel_id: int | None
    ext_id: str | None
    title: str | None
    participants: list[dict]


def _target(
    conn: sqlite3.Connection,
    *,
    kind: str,
    project_ids: frozenset[int] | None,
    project_id: int | None,
    account_id: int | None,
    channel_id: int | None,
    title: object,
    participants: object,
    session: bool,
) -> _Target:
    """Проверки на входе по виду операции; `session` — просит сессия (беседа и проект — в её скоупе, без
    «Неразобранного»)."""
    if kind not in KINDS:
        raise Invalid(f"kind: {', '.join(KINDS)}")
    if kind == "create":
        if session:
            if project_id is None or project_id <= 0:
                raise Invalid("project_id: нужен настоящий проект (не «Неразобранное»)")
        if project_id is not None:
            if (
                not _visible(project_ids, project_id)
                or conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is None
            ):
                raise NotFound("project_id: нет такого проекта или он не виден профилю")
        account = pick_account(conn, account_id)
        info = _account(conn, account)
        assert info is not None  # noqa: S101 — проверено в pick_account
        if info[1] in _BLOCKED_STATUSES:
            raise Invalid("аккаунт WhatsApp сейчас не работает (нужен вход или ограничен)")
        people = parse_participants(conn, account, participants, project_ids=project_ids, session=session)
        value = _check_title(title)
        twin = conn.execute(
            "SELECT id FROM main.group_op WHERE account_id = ? AND kind = 'create' AND title = ? "
            "AND status IN ('pending', 'approved')",  # OPEN_STATUSES
            (account, value),
        ).fetchone()
        if twin is not None:  # ревью Ф62-6 п. 2: сверка П1 различает группы только по названию
            raise Conflict(
                f"группа «{value}» уже ждёт создания (заявка #{twin[0]}) — дождись её или назови иначе"
            )
        return _Target(account, project_id, None, None, value, people)
    if channel_id is None:
        raise Invalid("channel_id: нужна беседа группы WhatsApp")
    chan = _group_chan(conn, channel_id)
    if not _visible(project_ids, chan.project_id):
        raise NotFound(f"беседа #{channel_id} не найдена или не видна профилю")
    if session and chan.project_id is None:
        raise Invalid("беседа без проекта — это может только владелец из шапки")
    if account_id is not None and account_id != chan.account_id:
        raise Invalid("account_id: беседа другого аккаунта")
    info = _account(conn, chan.account_id)
    if info is not None and info[1] in _BLOCKED_STATUSES:
        raise Invalid("аккаунт WhatsApp сейчас не работает (нужен вход или ограничен)")
    people: list[dict] = []
    if kind in ("add", "remove"):
        people = parse_participants(
            conn, chan.account_id, participants, project_ids=project_ids, session=session
        )
    elif participants:
        raise Invalid("participants: только у create, add и remove")
    return _Target(chan.account_id, chan.project_id, chan.id, chan.ext_id, chan.title, people)


def _account_pause(conn: sqlite3.Connection, account_id: int) -> int:
    """Пауза аккаунта (FloodWait/бан) — на открытых строках; новая заявка её не обходит."""
    row = conn.execute(
        "SELECT MAX(pause_until_ms) FROM main.group_op WHERE account_id = ? "
        "AND status IN ('pending', 'approved')",
        (account_id,),
    ).fetchone()
    return int(row[0] or 0)


def _session_obj(session: dict | None):
    if not isinstance(session, dict):
        return None
    pid, proc_start, session_id = session.get("pid"), session.get("proc_start"), session.get("session_id")
    if not isinstance(pid, int) or not isinstance(proc_start, int) or not isinstance(session_id, str):
        return None
    return SimpleNamespace(pid=pid, proc_start=proc_start, session_id=session_id)


def _insert(
    conn: sqlite3.Connection,
    target: _Target,
    *,
    kind: str,
    reason: str | None,
    origin: str,
    status: str,
    token_id: int | None,
    client_name: str | None,
    now_ms: int,
    session: dict | None = None,
    author=None,
) -> Op:
    session_json = None
    if origin == "session":
        session_json = json.dumps(
            {
                **{k: session[k] for k in ("pid", "proc_start", "session_id") if session and k in session},
                "sub_id": author.id if author is not None else None,
                "role": subs_mod.author_role(author) if author is not None else None,
            },
            ensure_ascii=False,
        )
    cur = conn.execute(
        "INSERT INTO main.group_op (account_id, project_id, channel_id, ext_id, kind, title, "
        "participants_json, "
        "reason, origin, status, token_id, client_name, session_json, hub_session_id, author_sub_id, "
        "author_project_id, author_role_key, author_role_no, created_ts, decided_ts, updated_ts, "
        "not_before_ms, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target.account_id,
            target.project_id,
            target.channel_id,
            target.ext_id,
            kind,
            target.title,
            json.dumps(target.participants, ensure_ascii=False) if target.participants else None,
            reason or None,
            origin,
            status,
            token_id,
            client_name,
            session_json,
            # П6: сессия хаба просителя (канал владельца: «чья карточка»), прав не даёт.
            held_mod.open_id_of(conn, session) if origin == "session" else None,
            author.id if author is not None else None,
            author.project_id if author is not None else None,
            author.role_key if author is not None else None,
            author.role_no if author is not None else None,
            now_ms,
            now_ms if status == "approved" else None,
            now_ms,
            max(now_ms, _account_pause(conn, target.account_id)),
            next_seq(conn),
        ),
    )
    op = get(conn, cur.lastrowid)
    assert op is not None  # noqa: S101
    return op


# --- создать заявку -----------------------------------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    *,
    principal,
    kind: str,
    now_ms: int,
    project_id: int | None = None,
    account_id: int | None = None,
    channel_id: int | None = None,
    title: object = None,
    participants: object = None,
    reason: str | None = None,
    client_name: str | None = None,
    request_id: str = "",
    session: dict | None = None,
) -> Op:
    """Сессия: новая заявка — `pending` (карточка владельцу), у `link` — сразу `approved` (чтение, без карточки,
    П5). Запреты `check_attach` — 403; проект/беседа не видны — 404; кривые поля, беседа без проекта, не
    WhatsApp — 422; лимиты проекта — 429. Автопилот (`wa_group`) решает маршрут — `decide(auto_session=…)`."""
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = row[0] if row and isinstance(row[0], str) else None
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise Denied("группы — только из интерактивной сессии (не дежурная и не оркестратор)") from exc
    if reason is not None and len(reason) > REASON_MAX:
        raise Invalid(f"reason: не длиннее {REASON_MAX} символов")
    target = _target(
        conn,
        kind=kind,
        project_ids=principal.project_ids,
        project_id=project_id,
        account_id=account_id,
        channel_id=channel_id,
        title=title,
        participants=participants,
        session=True,
    )
    if kind in CARD_KINDS:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM main.group_op WHERE project_id = ? AND origin = 'session' "
            "AND status IN ('pending', 'approved') AND kind <> 'link'",
            (target.project_id,),
        ).fetchone()[0]
        if open_count >= OPEN_PER_PROJECT:
            raise TooMany(
                f"у проекта уже {OPEN_PER_PROJECT} открытых просьб по группам — дождись решения владельца"
            )
    day_count = conn.execute(
        "SELECT COUNT(*) FROM main.group_op WHERE project_id = ? AND origin = 'session' AND created_ts > ?",
        (target.project_id, now_ms - DAY_MS),
    ).fetchone()[0]
    if day_count >= NEW_PER_PROJECT_DAY:
        raise TooMany(f"не больше {NEW_PER_PROJECT_DAY} просьб по группам от проекта за сутки")
    who = _session_obj(session)
    author = (
        subs_mod.author_sub(conn, token_id=principal.token_id, session=who, project_id=target.project_id)
        if who is not None
        else None
    )
    status = "approved" if kind not in CARD_KINDS else "pending"
    op = _insert(
        conn,
        target,
        kind=kind,
        reason=reason,
        origin="session",
        status=status,
        token_id=principal.token_id,
        client_name=client_name,
        now_ms=now_ms,
        session=session,
        author=author,
    )
    _audit(
        conn,
        op,
        action="group_op_created",
        result=status,
        now_ms=now_ms,
        actor=principal.kind,
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return op


def create_ui(
    conn: sqlite3.Connection,
    *,
    principal,
    kind: str,
    now_ms: int,
    project_id: int | None = None,
    account_id: int | None = None,
    channel_id: int | None = None,
    title: object = None,
    participants: object = None,
    client_name: str | None = None,
    request_id: str = "",
) -> Op:
    """Владелец (интерфейс): заявка сразу `approved`. Проверки — как у сессии, кроме проекта (любая беседа, и
    без проекта; `create` без проекта — новая беседа ляжет в «Неразобранное»)."""
    target = _target(
        conn,
        kind=kind,
        project_ids=None,
        project_id=project_id,
        account_id=account_id,
        channel_id=channel_id,
        title=title,
        participants=participants,
        session=False,
    )
    op = _insert(
        conn,
        target,
        kind=kind,
        reason=None,
        origin="ui",
        status="approved",
        token_id=principal.token_id,
        client_name=client_name,
        now_ms=now_ms,
    )
    _audit(
        conn,
        op,
        action="group_op_created",
        result="approved",
        now_ms=now_ms,
        actor="ui",
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return op


# --- состав группы (чтение) -------------------------------------------------------------------------------


def _members_chan(conn: sqlite3.Connection, principal, channel_id: int) -> _Chan:
    """Беседа-группа WhatsApp, видимая принципалу: нет или не видна — 404, не группа WhatsApp — 422."""
    chan = _chan(conn, channel_id)
    if chan is None or not _visible(principal.project_ids, chan.project_id):
        raise NotFound(f"беседа #{channel_id} не найдена или не видна профилю")
    return _group_chan(conn, channel_id)


def request_members(
    conn: sqlite3.Connection,
    *,
    principal,
    channel_id: int,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
) -> Op:
    """Обновить состав группы (только интерфейс): задание `members` коннектору — сразу `approved`, без
    карточки, по образцу `link`. Открытое задание той же беседы — оно и возвращается (одно на беседу)."""
    chan = _members_chan(conn, principal, channel_id)
    info = _account(conn, chan.account_id)
    if info is not None and info[1] in _BLOCKED_STATUSES:
        raise Invalid("аккаунт WhatsApp сейчас не работает (нужен вход или ограничен)")
    open_ops = _select(
        conn,
        "g.channel_id = ? AND g.kind = 'members' AND g.status IN ('pending', 'approved') ORDER BY g.id",
        (chan.id,),
    )
    if open_ops:
        return open_ops[0]
    target = _Target(chan.account_id, chan.project_id, chan.id, chan.ext_id, chan.title, [])
    op = _insert(
        conn,
        target,
        kind="members",
        reason=None,
        origin="ui",
        status="approved",
        token_id=principal.token_id,
        client_name=client_name,
        now_ms=now_ms,
    )
    _audit(
        conn,
        op,
        action="group_op_created",
        result="approved",
        now_ms=now_ms,
        actor="ui",
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return op


def _member_names(conn: sqlite3.Connection, account_id: int, jids: list[str]) -> dict[str, str]:
    """Имена из хаба: контакт с этим jid, иначе название лички."""
    info = _account(conn, account_id)
    if info is None or not jids:
        return {}
    marks = ", ".join("?" * len(jids))
    out: dict[str, str] = {}
    for ext_id, name in conn.execute(
        f"SELECT ext_id, title FROM {info[0]}.channel WHERE account_id = ? AND kind = 'dm' "  # noqa: S608
        f"AND ext_id IN ({marks}) AND title IS NOT NULL",
        (account_id, *jids),
    ):
        out[ext_id] = name
    for ext_id, name in conn.execute(
        f"SELECT ext_id, name FROM {info[0]}.contact WHERE account_id = ? AND ext_id IN ({marks}) "  # noqa: S608
        "AND name IS NOT NULL",
        (account_id, *jids),
    ):
        out[ext_id] = name
    return out


def members_view(
    conn: sqlite3.Connection, principal, channel_id: int, *, refresh_op: Op | None = None
) -> dict:
    """`GET /channels/{id}/group-members`: последний снимок состава (итог `members`) — `{members: [{jid, name,
    phone_masked, is_admin, is_me} (+ `phone` — только интерфейсу)], updated_ts, refresh}`. Номер целиком
    (`phone`, номерной `jid`) — только кука интерфейса; bearer — маска, у номерного участника `jid` = `None`.
    `refresh` — открытое задание обновления `{op_id, status}` или `None`; снимка ещё нет — `members` пуст,
    `updated_ts` = `None`."""
    from . import new_recipient

    chan = _members_chan(conn, principal, channel_id)
    full = bool(principal.is_ui)
    last = _select(
        conn,
        "g.channel_id = ? AND g.kind = 'members' AND g.status = 'done' "
        "ORDER BY g.done_ts DESC, g.id DESC LIMIT 1",
        (chan.id,),
    )
    rows = result_of(last[0]).get("members", []) if last else []
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("jid"), str)]
    names = _member_names(conn, chan.account_id, [r["jid"] for r in rows])
    members = []
    for r in rows:
        jid = r["jid"]
        pn = r.get("pn") or (jid.split("@", 1)[0] if jid.endswith("@s.whatsapp.net") else None)
        item = {
            "jid": jid if full or jid.endswith("@lid") else None,
            "name": names.get(jid) or r.get("name"),
            "phone_masked": new_recipient.mask_phone("+" + pn) if pn else None,
            "is_admin": bool(r.get("admin")),
            "is_me": bool(r.get("me")),
        }
        if full:
            item["phone"] = ("+" + pn) if pn else None
        members.append(item)
    if refresh_op is None:
        open_ops = _select(
            conn,
            "g.channel_id = ? AND g.kind = 'members' AND g.status IN ('pending', 'approved') ORDER BY g.id",
            (chan.id,),
        )
        refresh_op = open_ops[0] if open_ops else None
    return {
        "channel_id": chan.id,
        "members": members,
        "updated_ts": last[0].done_ts if last else None,
        "refresh": {"op_id": refresh_op.id, "status": refresh_op.status} if refresh_op is not None else None,
    }


# --- решение владельца -------------------------------------------------------------------------------------------


def _erase_inputs(conn: sqlite3.Connection, op: Op, now_ms: int, **sets) -> None:
    """П9: сырой номер участника (`input`) стирается при закрытии заявки — остаются маска и jid."""
    people = participants_of(op)
    if any(p.get("input") for p in people):
        sets["participants_json"] = json.dumps([{**p, "input": None} for p in people], ensure_ascii=False)
    _update(conn, op.id, now_ms, **sets)


RESTORE_HOLD_ERROR = "восстановлено из копии — прежняя машина могла уже выполнить; проверь в WhatsApp"


def hold_after_restore(conn: sqlite3.Connection, *, now_ms: int) -> int:
    """Ф33-3 (К12): копия восстановлена — одобренные заявки, видимые людям (`CARD_KINDS`), прежняя машина
    могла выполнить уже после снимка; повтор вслепую — вторая группа или повторный сброс ссылки. Заявка снова
    ждёт решения владельца (карточка, причина — в `error`). У `create` граница сверки П1 не теряется: шаг считается
    повтором (`lease_gen` ≥ 1) с `first_leased_ts` не позже решения — после нового «Да» коннектор сначала ищет
    уже созданную группу. Чтения (`link`, `members`) повторяются как есть. Зовёт `core.restore` до первого
    старта ядра в восстановленном каталоге."""
    held = 0
    kinds = sorted(CARD_KINDS)
    marks = ", ".join("?" for _ in kinds)
    for op in _select(conn, f"g.status = 'approved' AND g.kind IN ({marks})", kinds):
        sets: dict = {
            "status": "pending",
            "decided_ts": None,
            "auto": 0,
            "lease_until_ms": None,
            "error": RESTORE_HOLD_ERROR,
        }
        if op.kind == "create":
            sets["lease_gen"] = max(op.lease_gen, 1)
            if op.first_leased_ts is None:
                sets["first_leased_ts"] = op.decided_ts if op.decided_ts is not None else op.created_ts
        _update(conn, op.id, now_ms, **sets)
        _audit(conn, op, action="group_op_restore_hold", result="ok", now_ms=now_ms)
        held += 1
    return held


def decide(
    conn: sqlite3.Connection,
    op_id: int,
    action: str,
    *,
    now_ms: int,
    api_token_id: int | None = None,
    client_name: str | None = None,
    audit_request_id: str = "",
    auto_session: int | None = None,
) -> Op:
    """`approve` — только из `pending`; `decline` — из `pending`/`approved`, пока шаг ни разу не выдавался
    (иначе 409 «уже выполняю»). `auto_session` — принял выключатель автопилота `wa_group` этой сессии хаба:
    тот же код, отметка `auto`, своё действие аудита `group_op_auto`."""
    op = get(conn, op_id)
    if op is None:
        raise NotFound(f"заявка {op_id} не найдена")
    if action == "approve":
        if op.status != "pending":
            raise Conflict(f"одобрить можно только ждущую решения заявку (сейчас {op.status})")
        _update(
            conn,
            op_id,
            now_ms,
            status="approved",
            decided_ts=now_ms,
            attempts=0,
            not_before_ms=max(now_ms, op.pause_until_ms, _account_pause(conn, op.account_id)),
            **({"auto": 1} if auto_session is not None else {}),
            # Ф33-3: пометка «восстановлено из копии» — до нового решения, дальше не мешает итогу.
            **({"error": None} if op.error == RESTORE_HOLD_ERROR else {}),
        )
        audit_action = "group_op_auto" if auto_session is not None else "group_op_approved"
    elif action == "decline":
        if op.status == "approved" and op.first_leased_ts is not None:
            raise Conflict("уже выполняю — отказать нельзя")
        if op.status not in OPEN_STATUSES:
            raise Conflict(f"заявка уже закрыта ({op.status})")
        _erase_inputs(
            conn, op, now_ms, status="declined", decided_ts=now_ms, done_ts=now_ms, lease_until_ms=None
        )
        audit_action = "group_op_declined"
    else:
        raise Invalid(f"action: approve или decline, получено {action!r}")
    op = get(conn, op_id)
    assert op is not None  # noqa: S101
    _audit(
        conn,
        op,
        action=audit_action,
        result=op.status,
        now_ms=now_ms,
        actor="api" if auto_session is not None else "ui",
        request_id=audit_request_id,
        api_token_id=api_token_id,
        client_name=client_name,
        **({"detail": {"hub_session_id": auto_session}} if auto_session is not None else {}),
    )
    return op


# --- выдача -------------------------------------------------------------------------------------------------


def _scope(principal, where: list[str], params: list) -> bool:
    if principal.project_ids is None:
        return True
    ids = sorted(principal.project_ids)
    if not ids:
        return False
    where.append(f"g.project_id IN ({','.join('?' for _ in ids)})")
    params += ids
    return True


def _own_triple(op: Op, session: dict | None) -> bool:
    if not session:
        return False
    mine = _loads(op.session_json, {})
    return all(mine.get(k) == session.get(k) for k in ("pid", "proc_start", "session_id"))


def list_ops(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    project_id: int | None = None,
    channel_id: int | None = None,
    status: str | None = None,
    mine: dict | None = None,
    limit: int = 50,
) -> list[dict]:
    """`GET /group-ops`: видимые проекты принципала; по умолчанию — открытые и закрытые за 7 дней, новые
    первыми. `status` — один статус (`open` — все открытые); `mine` — тройка сессии: только её заявки."""
    where = ["1 = 1"]
    params: list = []
    if not _scope(principal, where, params):
        return []
    if project_id is not None:
        where.append("g.project_id = ?")
        params.append(project_id)
    if channel_id is not None:
        where.append("g.channel_id = ?")
        params.append(channel_id)
    if status == "open":
        where.append("g.status IN ('pending', 'approved')")
    elif status is not None:
        where.append("g.status = ?")
        params.append(status)
    else:
        where.append("(g.status IN ('pending', 'approved') OR g.updated_ts > ?)")
        params.append(now_ms - LIST_RECENT_MS)
    limit = max(1, min(int(limit), LIST_MAX))
    if mine is not None:
        where.append("g.origin = 'session' AND g.token_id = ?")
        params.append(principal.token_id)
    rows = _select(conn, " AND ".join(where) + " ORDER BY g.id DESC LIMIT ?", [*params, LIST_MAX])
    if mine is not None:
        rows = [r for r in rows if _own_triple(r, mine)]
    return [op_dict(r) for r in rows[:limit]]


def visible_op(conn: sqlite3.Connection, principal, op_id: int) -> Op:
    op = get(conn, op_id)
    if op is None or not _visible(principal.project_ids, op.project_id):
        raise NotFound(f"заявка {op_id} не найдена")
    return op


def changes(conn: sqlite3.Connection, principal, *, since: int, watermark: int, limit: int) -> list[dict]:
    """Заявки с `since < seq <= watermark` в скоупе принципала, по возрастанию `seq` (`/sync`, SSE)."""
    where = ["g.seq > ? AND g.seq <= ?"]
    params: list = [since, watermark]
    if not _scope(principal, where, params):
        return []
    rows = _select(conn, " AND ".join(where) + " ORDER BY g.seq LIMIT ?", [*params, limit])
    return [op_dict(r) for r in rows]


# --- очередь коннектору -------------------------------------------------------------------------------------


def _blocked(conn: sqlite3.Connection, account_id: int) -> bool:
    info = _account(conn, account_id)
    return info is None or info[1] in _BLOCKED_STATUSES or info[2] not in SOURCES


def _leased_rows(conn: sqlite3.Connection, account_id: int, since_ms: int) -> list[Op]:
    return _select(
        conn,
        "g.account_id = ? AND g.first_leased_ts IS NOT NULL AND g.first_leased_ts > ? "
        "ORDER BY g.first_leased_ts",
        (account_id, since_ms),
    )


def _cap_due(times: list[tuple[int, int]], need: int, cap: int) -> int:
    """Срок, когда в скользящих сутках освободится `need` единиц из `cap`: `times` — `(время, единиц)` по
    возрастанию. Уже свободно — 0."""
    used = sum(n for _ts, n in times)
    if used + need <= cap:
        return 0
    for ts, n in times:
        used -= n
        if used + need <= cap:
            return ts + DAY_MS
    return times[-1][0] + DAY_MS if times else 0


def _pace_due(conn: sqlite3.Connection, op: Op, now_ms: int) -> int:
    """Темп и суточные потолки аккаунта для заявки `op` (повтор уже выданной — без потолков: она уже
    засчитана)."""
    last_write, last_any = conn.execute(
        "SELECT MAX(CASE WHEN kind NOT IN ('link', 'members') THEN last_leased_ts END), MAX(last_leased_ts) "
        "FROM main.group_op "
        "WHERE account_id = ? AND id <> ?",
        (op.account_id, op.id),
    ).fetchone()
    if op.kind in READ_KINDS:
        due = (last_any + LINK_GAP_MS) if last_any is not None else 0
    else:
        due = (last_write + GAP_MS) if last_write is not None else 0
        if last_any is not None:
            due = max(due, last_any + LINK_GAP_MS)
    if op.first_leased_ts is not None:
        return due
    rows = [r for r in _leased_rows(conn, op.account_id, now_ms - DAY_MS) if r.id != op.id]
    if op.kind == "create":
        times = [(r.first_leased_ts, 1) for r in rows if r.kind == "create"]
        due = max(due, _cap_due(times, 1, CREATE_DAILY))
    if op.kind == "link_reset":
        times = [(r.first_leased_ts, 1) for r in rows if r.kind == "link_reset"]
        due = max(due, _cap_due(times, 1, RESET_DAILY))
    if op.kind in ("create", "add"):
        times = [(r.first_leased_ts, len(participants_of(r))) for r in rows if r.kind in ("create", "add")]
        due = max(due, _cap_due(times, len(participants_of(op)), ADDED_DAILY))
    return due


def _busy_until(conn: sqlite3.Connection, account_id: int, now_ms: int) -> int:
    """Операции аккаунта — по одной: пока выданная не отчиталась и лиз не истёк, следующая не выдаётся."""
    row = conn.execute(
        "SELECT MAX(lease_until_ms) FROM main.group_op WHERE account_id = ? AND status = 'approved' "
        "AND lease_until_ms > ?",
        (account_id, now_ms),
    ).fetchone()
    return int(row[0] or 0)


def _candidates(conn: sqlite3.Connection, account_id: int, now_ms: int) -> list[tuple[int, Op]]:
    """`(срок, заявка)` одобренных заявок аккаунта (старший `decided_ts` первым)."""
    busy = _busy_until(conn, account_id, now_ms)
    rows = _select(
        conn, "g.account_id = ? AND g.status = 'approved' ORDER BY g.decided_ts, g.id", (account_id,)
    )
    out = []
    for op in rows:
        own_lease = op.lease_until_ms or 0
        due = max(op.not_before_ms, op.pause_until_ms, own_lease, _pace_due(conn, op, now_ms))
        if own_lease <= now_ms:
            due = max(due, busy)
        out.append((due, op))
    return out


def _reconcile_exclude(conn: sqlite3.Connection, op: Op, first_leased_ts: int) -> list[str]:
    """Ревью Ф62-6 п. 2: группы, которые сверка П1 этой заявки `create` брать не должна, — созданные прочими
    заявками `create` аккаунта за 7 дней и беседы-группы с тем же названием, что уже были в хабе до первой
    выдачи или разложены вручную (не наша новая группа)."""
    rows = conn.execute(
        "SELECT ext_id FROM main.group_op WHERE account_id = ? AND kind = 'create' AND id != ? "
        "AND ext_id IS NOT NULL AND created_ts >= ?",
        (op.account_id, op.id, first_leased_ts - DAY_MS * 7),
    ).fetchall()
    out = {r[0] for r in rows}
    info = _account(conn, op.account_id)
    if info is not None and op.title:
        rows = conn.execute(
            f"SELECT ext_id FROM {info[0]}.channel WHERE account_id = ? AND kind = 'group' AND title = ? "  # noqa: S608
            "AND (map_source = 'manual' OR COALESCE(first_seen_ts, 0) < ?)",
            (op.account_id, op.title, first_leased_ts),
        ).fetchall()
        out.update(r[0] for r in rows)
    return sorted(out)


def _job(conn: sqlite3.Connection, op: Op, lease_gen: int, first_leased_ts: int) -> dict:
    people = [
        {
            "i": n,
            "jid": p.get("jid"),
            **({"phone": p["input"]} if p.get("input") and not p.get("jid") else {}),
        }
        for n, p in enumerate(participants_of(op))
    ]
    return {
        "id": op.id,
        "lease_gen": lease_gen,
        "kind": op.kind,
        "ext_id": op.ext_id,
        "title": op.title if op.kind == "create" else None,
        "participants": people,
        "project_id": op.project_id,
        # П1: граница сверки исхода `create` (группа создана не раньше первой выдачи − 120 с).
        "first_leased_ts": first_leased_ts,
        "attempts": op.attempts,
        **({"exclude": _reconcile_exclude(conn, op, first_leased_ts)} if op.kind == "create" else {}),
    }


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """Задания своего аккаунта, по одному (операции аккаунта последовательны), лиз `LEASE_MS`;
    `limited`/`auth_required` — пусто, строки ждут."""
    if _blocked(conn, account_id):
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    items: list[dict] = []
    while len(items) < limit:
        ready = [(due, op) for due, op in _candidates(conn, account_id, now_ms) if due <= now_ms]
        if not ready:
            break
        _due, op = ready[0]
        first = op.first_leased_ts if op.first_leased_ts is not None else now_ms
        sets: dict = {
            "lease_until_ms": now_ms + LEASE_MS,
            "lease_gen": op.lease_gen + 1,
            "last_leased_ts": now_ms,
        }
        if op.first_leased_ts is None:
            sets["first_leased_ts"] = now_ms  # граница «Отказать», потолков и сверки create
        _update(conn, op.id, now_ms, **sets)
        items.append(_job(conn, op, op.lease_gen + 1, first))
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait`: `(готово, ближайший срок в мс)`."""
    if _blocked(conn, account_id):
        return False, None
    dues = [due for due, _op in _candidates(conn, account_id, now_ms)]
    if not dues:
        return False, None
    due = min(dues)
    if due <= now_ms:
        return True, None
    return False, due


def backoff_ms(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки (с 1): 30 с, 60 с, 120 с, …"""
    return BACKOFF_BASE_MS * 2 ** (max(attempt, 1) - 1)


def _pause_account(conn: sqlite3.Connection, account_id: int, until_ms: int, now_ms: int) -> None:
    """FloodWait/бан — пауза ВСЕМ открытым заявкам аккаунта (в строках — переживает рестарт)."""
    for op in _select(
        conn,
        "g.account_id = ? AND g.status IN ('pending', 'approved') AND "
        "(g.pause_until_ms < ? OR g.not_before_ms < ?)",
        (account_id, until_ms, until_ms),
    ):
        _update(
            conn,
            op.id,
            now_ms,
            pause_until_ms=max(op.pause_until_ms, until_ms),
            not_before_ms=max(op.not_before_ms, until_ms),
        )


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def clean_result(op: Op, result: object) -> dict:
    """Итог коннектора — только известные поля: `chat` (jid группы, у create), `link` (по образцу ссылки),
    `participants` — `{i, code, invite, jid}` по индексам задания."""
    if not isinstance(result, dict):
        result = {}
    out: dict = {}
    if op.kind == "create":
        chat = result.get("chat")
        if not isinstance(chat, str) or not _GROUP_JID_RE.match(chat):
            raise Invalid("result.chat: нужен jid новой группы")
        out["chat"] = chat
        # Название группы из ответа моста — для беседы, которую создаёт отчёт (нет — название заявки).
        title = _clip(result.get("title"), TITLE_MAX * 8)
        if title is not None:
            out["title"] = title
    if op.kind in ("link", "link_reset"):
        link = result.get("link")
        if not isinstance(link, str) or not LINK_RE.match(link):
            raise Invalid("result.link: нужна ссылка https://chat.whatsapp.com/…")
        out["link"] = link
    if op.kind == "members":
        out["members"] = _clean_members(result.get("members"))
    if op.kind in ("create", "add", "remove"):
        count = len(participants_of(op))
        rows = []
        seen: set[int] = set()
        raw = result.get("participants")
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            i = item.get("i")
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count or i in seen:
                continue
            code = item.get("code")
            row = {"i": i, "code": code if code in PARTICIPANT_CODES else "error"}
            if item.get("invite") is True:
                row["invite"] = True
            jid = item.get("jid")
            if isinstance(jid, str) and _JID_RE.match(jid):
                row["jid"] = jid
            seen.add(i)
            rows.append(row)
        out["participants"] = sorted(rows, key=lambda r: r["i"])
    return out


def _clean_members(raw: object) -> list[dict]:
    """Состав группы из `group_info` моста: `{jid, pn?, name?, admin, me}`; кривые строки — мимо."""
    rows: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if len(rows) >= MEMBERS_ROWS_MAX:
            break
        if not isinstance(item, dict):
            continue
        jid = item.get("jid")
        if not isinstance(jid, str) or not _JID_RE.match(jid) or jid in seen:
            continue
        seen.add(jid)
        row: dict = {"jid": jid, "admin": item.get("admin") is True, "me": item.get("me") is True}
        pn = item.get("pn")
        if isinstance(pn, str) and pn.isdigit() and 5 <= len(pn) <= 20:
            row["pn"] = pn
        name = _clip(item.get("name"), NAME_MAX)
        if name:
            row["name"] = name
        rows.append(row)
    return rows


def report(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    op_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    retry_after_ms: int | None = None,
    postpone_ms: int | None = None,
    reason: str | None = None,
    result: dict | None = None,
) -> str:
    """`retry`: `retry_after_ms` — пауза всему аккаунту (бан/FloodWait), попыткой не считается;
    `postpone_ms` — отсрочка только этой строки (предел поиска номеров, П9), попыткой не считается; без
    них — бэкофф, 5-я неудача -> `failed`/`gave_up`. `drop` -> `failed` с причиной. `done` — итог
    (`clean_result`); у create — jid группы и привязка беседы. Итог для журнала — новый статус (или
    `paused`)."""
    if outcome not in REPORT_OUTCOMES:
        raise Invalid(f"outcome: {outcome!r}")
    op = get(conn, op_id)
    if op is None or op.account_id != account_id:
        raise NotFound(f"group_op {op_id}")
    if op.lease_gen != lease_gen or op.status != "approved":
        raise StaleLease(f"group_op {op_id}: lease_gen {lease_gen} != {op.lease_gen} или шаг закрыт")

    if outcome == "retry":
        if retry_after_ms:
            _update(conn, op_id, now_ms, lease_until_ms=None)
            _pause_account(conn, account_id, now_ms + retry_after_ms, now_ms)
            return "paused"
        if postpone_ms:
            postpone = min(int(postpone_ms), MAX_POSTPONE_MS)
            _update(
                conn,
                op_id,
                now_ms,
                lease_until_ms=None,
                not_before_ms=max(op.not_before_ms, now_ms + postpone),
            )
            return "paused"
        attempt = op.attempts + 1
        if attempt >= MAX_ATTEMPTS:
            _fail(conn, op, "gave_up", now_ms)
            return "failed"
        _update(
            conn,
            op_id,
            now_ms,
            attempts=attempt,
            lease_until_ms=None,
            not_before_ms=max(op.not_before_ms, now_ms + backoff_ms(attempt)),
        )
        return "paused"

    if outcome == "drop":
        _fail(conn, op, _clip(reason, ERROR_MAX) or "other", now_ms)
        return "failed"

    clean = clean_result(op, result)
    people = participants_of(op)
    for row in clean.get("participants", []):
        jid = row.pop("jid", None)
        if jid and not people[row["i"]].get("jid"):
            people[row["i"]] = {**people[row["i"]], "jid": jid}
    sets: dict = {
        "status": "done",
        "result_json": json.dumps(clean, ensure_ascii=False),
        "done_ts": now_ms,
        "lease_until_ms": None,
        "error": None,
    }
    if people:
        sets["participants_json"] = json.dumps([{**p, "input": None} for p in people], ensure_ascii=False)
    if op.kind == "create":
        sets["ext_id"] = clean["chat"]
    _update(conn, op_id, now_ms, **sets)
    if op.kind == "create":
        _ensure_group_channel(conn, op_id, clean.get("title"), now_ms)
    done = get(conn, op_id)
    assert done is not None  # noqa: S101
    codes = [r["code"] for r in clean.get("participants", [])]
    _audit(
        conn,
        done,
        action="group_op_done",
        result="done",
        now_ms=now_ms,
        detail={"codes": {c: codes.count(c) for c in sorted(set(codes))}} if codes else None,
    )
    return "done"


def _fail(conn: sqlite3.Connection, op: Op, error: str, now_ms: int) -> None:
    _erase_inputs(conn, op, now_ms, status="failed", error=error, done_ts=now_ms, lease_until_ms=None)
    failed = get(conn, op.id)
    assert failed is not None  # noqa: S101
    _audit(conn, failed, action="group_op_failed", result="failed", now_ms=now_ms, detail={"error": error})


# --- после create: беседа группы в проект заявки ------------------------------------------------------------


def _bind(conn: sqlite3.Connection, op: Op, schema: str, channel_id: int, now_ms: int) -> bool:
    """Беседа новой группы — в проект заявки (`manual`, причина «группа создана по заявке #N»), НЕ глушится
    (П12); у заявки без проекта беседа остаётся, где её разложили правила. В любом случае — `channel_id` в
    заявку. `True` — беседа перенесена."""
    from . import mapping

    row = conn.execute(
        f"SELECT map_source, project_id FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if row is None:
        return False
    moved = False
    if op.project_id is not None and row[0] != "manual":
        conn.execute(
            f"UPDATE {schema}.channel SET project_id = ?, map_source = 'manual', map_reason = ?, "  # noqa: S608
            "seq = ? WHERE id = ?",
            (op.project_id, BIND_REASON.format(id=op.id), next_seq(conn), channel_id),
        )
        mapping.recompute_topics(conn, schema, channel_id, now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="core",
            api_token_id=None,
            client_name=None,
            request_id=f"group-op-{op.id}",
            action="channel_update",
            result="ok",
            channel_id=channel_id,
            detail={"project_id": op.project_id, "group_op_id": op.id},
        )
        moved = True
    if op.channel_id is None:
        _update(conn, op.id, now_ms, channel_id=channel_id)
    return moved


def _ensure_group_channel(conn: sqlite3.Connection, op_id: int, title: str | None, now_ms: int) -> None:
    """Из отчёта `create` -> `done`: беседа группы есть сразу, до первого сообщения (WhatsApp в новую группу
    ничего не шлёт). Беседа уже есть (сообщение пришло раньше отчёта) — привязать её; нет — создать тем же
    кодом, что у приёма (`ingest.ensure_group_channel`): хук `on_channel_created` привяжет её к заявке.
    Название — из ответа моста, иначе из заявки."""
    from . import ingest

    op = get(conn, op_id)
    if op is None or op.ext_id is None:
        return
    info = _account(conn, op.account_id)
    if info is None:
        return
    channel_id = ingest.ensure_group_channel(conn, op.account_id, op.ext_id, title or op.title, now_ms)
    op = get(conn, op_id)
    if channel_id is not None and op is not None and op.channel_id is None:
        _bind(conn, op, info[0], channel_id, now_ms)


def on_channel_created(
    conn: sqlite3.Connection, schema: str, account_id: int, channel_id: int, now_ms: int
) -> None:
    """Хук `ingest._create_channel` (после вставки и раскладки): беседа новой группы, о которой отчёт
    `create` уже пришёл (`done`, тот же jid, `channel_id` ещё пуст), — в проект заявки."""
    row = conn.execute(
        f"SELECT ext_id, kind FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if row is None or row[1] != "group":
        return
    for op in _select(
        conn,
        "g.account_id = ? AND g.ext_id = ? AND g.kind = 'create' AND g.status = 'done' "
        "AND g.channel_id IS NULL "
        "ORDER BY g.id DESC LIMIT 1",
        (account_id, row[0]),
    ):
        _bind(conn, op, schema, channel_id, now_ms)


# --- исход — автору сигналом --------------------------------------------------------------------------------


def _signal_pred(sub, now_ms: int) -> tuple[str, list]:
    """ЕДИНСТВЕННОЕ условие «исход заявки пора отдать подписке `sub`» — и для счёта, и для выдачи (образец —
    `presses._signal_pred`): заявка сессии в конечном статусе, не отдана, не старше `OUTCOME_TTL_MS`,
    адресована ей — сама подписка автора ИЛИ её линия (проект, роль, номер), когда подписки автора уже нет.
    В отличие от нажатий — и заявки, принятые выключателем, и `link`: в исходе — ссылка и коды участников."""
    where = (
        "g.origin = 'session' AND g.signaled_ts IS NULL AND g.done_ts > ? "
        "AND g.status IN ('done', 'failed', 'declined') AND (g.author_sub_id = ?"
    )
    params: list = [now_ms - OUTCOME_TTL_MS, sub.id]
    if sub.role_key is not None:
        where += (
            " OR (g.author_project_id = ? AND g.author_role_key = ? AND g.author_role_no = ? AND NOT EXISTS "
            "(SELECT 1 FROM main.session_sub live WHERE live.id = g.author_sub_id AND live.ended_ts IS NULL)"
        )
        params += [sub.project_id, sub.role_key, sub.role_no]
        if sub.project_id == subs_mod.NO_PROJECT_ID:
            where += (
                " AND EXISTS (SELECT 1 FROM main.session_sub a WHERE a.id = g.author_sub_id "
                "AND a.session_cwd = ?)"
            )
            params.append(sub.session_cwd)
        where += ")"
    return where + ")", params


def signal_count(conn: sqlite3.Connection, sub, *, now_ms: int) -> int:
    """Читатель, для ожидания: исходы заявок, которые пора отдать подписке (все — срочные)."""
    where, params = _signal_pred(sub, now_ms)
    return conn.execute(
        f"SELECT COUNT(*) FROM main.group_op g WHERE {where}",  # noqa: S608 — фрагмент задан кодом
        params,
    ).fetchone()[0]


def signal_of(op: Op) -> dict:
    """Сигнал исхода: только числа, слова из перечня и ссылка по строгому образцу (её дал WhatsApp, не
    люди)."""
    out: dict = {"kind": "group_op", "op_id": op.id, "op": op.kind, "outcome": op.status}
    if op.channel_id is not None:
        out["channel_id"] = op.channel_id
    link = result_of(op).get("link")
    if isinstance(link, str) and LINK_RE.match(link):
        out["link"] = link
    codes = [p.get("code") for p in result_of(op).get("participants", []) if isinstance(p, dict)]
    failed = sum(1 for c in codes if c not in ("ok", "already"))
    if codes:
        out["ok"] = len(codes) - failed
        out["failed"] = failed
    return out


def take_signals(
    conn: sqlite3.Connection,
    sub,
    *,
    now_ms: int,
    limit: int,
    api_token_id: int,
    client_name: str | None,
    request_id: str,
) -> list[dict]:
    """Отдать подписке исходы её заявок (`signal_of`). Сигнал отдаётся один раз (`signaled_ts`) и не двигает
    `seq` строки. Не влезшие в `limit` ждут следующей выдачи."""
    where, params = _signal_pred(sub, now_ms)
    rows = _select(conn, where + " ORDER BY g.id LIMIT ?", [*params, max(0, limit)])
    signals: list[dict] = []
    for op in rows:
        conn.execute(
            "UPDATE main.group_op SET signal_sub_id = ?, signaled_ts = ? WHERE id = ?",
            (sub.id, now_ms, op.id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            action="sub_signal",
            result="delivered",
            channel_id=op.channel_id,
            detail={"sub_id": sub.id, "kind": "group_op", "group_op_id": op.id, "outcome": op.status},
        )
        signals.append(signal_of(op))
    return signals
