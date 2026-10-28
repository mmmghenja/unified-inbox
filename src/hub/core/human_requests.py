"""Заявки «нужен человек» (Ф54-1, docs/architecture.md и поправки П1–П13 — они
главнее текста выше них; фактический контракт — раздел 10 спеки).

Сессия с живой подпиской на проект с ролью зовёт владельца в окно браузера: капча, площадка разлогинила, код,
оплата, галочка согласия. Строка `main.human_request` -> карточка владельцу -> его кнопка («Готово», «Не вышло»,
«Не сейчас») -> сигнал исхода линии автора (проект, роль, номер). Сама сессия препятствие не проходит.

Кто что может (П2):
- автор — bearer подписанной сессии: завести (`create`), снять (`cancel`), читать заявки своей линии;
- Владелец (кука, и с телефона): всё видит, решает;
- ответчик-агент (токен ровно со скоупом `human`, П3) — агент владельца, действует от его лица: видит и решает
  заявки ЛЮБОГО вида, когда маршрут группы `human` проекта заявки — `agent` (Ф66-1, таблица `card_route`;
  выключатель `human_agent` больше не читается) или по заявке открыто поручение «Реши» (П2 Ф66-1), и только
  своих проектов (П4, П5). Завести заявку такой токен не может — «не отвечать на свою» выполняется структурно;
- бот владельца в Telegram (токен ровно со скоупом `human_bot`, Ф54-2 П5): видит все ждущие и закрытые за сутки
  заявки всех видов и проектов и решает любую ждущую — он рука владельца, выключатели видов на него не действуют;
  запись `decided_by = "bot"`. Завести и снять заявку не может;
- прочий bearer — ничего.

Линия, а не тройка сессии (П7, П8): исход, пределы «открытых на сессию», право отмены и выдача списка — по
линии подписки автора; наследник линии после перезапуска получает исход и видит заявку целиком.

Срок (П1): `sweep` зовут минутный цикл ядра (`runner._human_loop`), `create`, `decide`, `cancel` и чтение.

Текст автора (`site`, `url`, `doing`, `need`, `profile`, `tab`) — чужой ввод для интерфейса и для ответчика:
в аудит, сигнал и кадр SSE не кладётся. Байты скриншота — `<data_dir>/human-files/<sha[:2]>/<sha>` (0600),
один файл на содержимое; появление и удаление файла — только в потоке-писателе.

Каждая запись в строку двигает глобальный `seq`: `/sync` (раздел `human_requests`, только UI) и кадр SSE
`human_request`. Пишет только внутри `Db.write()`, время — параметром `now_ms`."""

import contextlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from . import media as media_mod
from . import reg_mail_rule
from . import subs as subs_mod
from . import workday as workday_mod
from .audit import write_audit
from .db import next_seq
from .security import HUMAN_BOT_SCOPE, HUMAN_SCOPE, check_attach, human_only

_logger = logging.getLogger(__name__)

KINDS = ("captcha", "login", "code", "payment", "consent", "other")
OUTCOMES = ("done", "failed", "later")
FINAL_STATUSES = (*OUTCOMES, "expired", "cancelled")
STATUSES = ("pending", *FINAL_STATUSES)
# Исходы, о которых автору уходит сигнал (отмена — действие самого автора: сигнала нет).
SIGNAL_OUTCOMES = (*OUTCOMES, "expired")
DECIDERS = ("ui", "agent", "bot")

SITE_MAX = 60
URL_MAX = 500
DOING_MAX = 300
NEED_MAX = 200
PROFILE_MAX = 60
TAB_MAX = 80
NOTE_MAX = 1000  # Ф93-2 (A): причина исхода — что написал решивший
TTL_MIN_MAX = 1440
TTL_MIN_DEFAULT = 30

_MIN_MS = 60_000
DAY_MS = 24 * 60 * _MIN_MS
# Заявка без срока снимается через сутки (раздел 2).
NO_TTL_MS = DAY_MS
# Байты скриншота стираются через 7 суток после решения; строка живёт 30 дней.
SHOT_KEEP_MS = 7 * DAY_MS
KEEP_MS = 30 * DAY_MS
# Исход автору сигналом — не старше суток (как исход нажатия, Ф44).
OUTCOME_TTL_MS = DAY_MS
# П7: пределы — открытых на линию, открытых на проект, новых на проект в сутки.
OPEN_PER_LINE = 3
OPEN_PER_PROJECT = 10
NEW_PER_PROJECT_DAY = 60
LIST_MAX = 200

MIB = 1024 * 1024
SHOT_MAX_BYTES = 5 * MIB
SHOT_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
DIR_NAME = "human-files"
TMP_PREFIX = ".tmp-"
TMP_MAX_AGE_S = 3600
_SHA_RE = re.compile(r"[0-9a-f]{64}")

LIMIT_LINE = (
    f"открытых заявок «нужен человек» у твоей роли уже {OPEN_PER_LINE} — дождись исхода или сними лишнюю"
)
LIMIT_PROJECT = (
    f"открытых заявок «нужен человек» в проекте уже {OPEN_PER_PROJECT} — дождись, пока владелец разберёт"
)
LIMIT_DAY = f"за сутки в проекте уже {NEW_PER_PROJECT_DAY} заявок «нужен человек» — на сегодня предел"
WAITS_OWNER = "ждёт владельца: заявку этого вида решает только он"
STATUS_RU = {
    "pending": "ждёт",
    "done": "готово",
    "failed": "не вышло",
    "later": "не сейчас",
    "expired": "срок вышел",
    "cancelled": "снята автором",
}


class HumanError(Exception):
    """Отказ операции; маршрут переводит подкласс в HTTP-код."""


class Invalid(HumanError):
    """422."""


class Denied(HumanError):
    """403."""


class NotFound(HumanError):
    """404."""


class Conflict(HumanError):
    """409."""


class Limited(HumanError):
    """429."""


# --- строка -----------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "project_id",
    "kind",
    "site",
    "url",
    "doing",
    "need",
    "profile",
    "tab",
    "shot_sha256",
    "shot_size",
    "shot_mime",
    "shot_purged_ts",
    "expires_ts",
    "status",
    "decided_ts",
    "decided_by",
    "decided_token_id",
    "author_sub_id",
    "author_project_id",
    "author_role_key",
    "author_role_no",
    "session_json",
    "token_id",
    "signaled_ts",
    "signal_sub_id",
    "created_ts",
    "seq",
    "decided_note",
)
_SELECT = ", ".join(f"h.{c}" for c in _COLUMNS)


class Human(NamedTuple):
    id: int
    project_id: int
    kind: str
    site: str | None
    url: str | None
    doing: str | None
    need: str
    profile: str | None
    tab: str | None
    shot_sha256: str | None
    shot_size: int | None
    shot_mime: str | None
    shot_purged_ts: int | None
    expires_ts: int | None
    status: str
    decided_ts: int | None
    decided_by: str | None
    decided_token_id: int | None
    author_sub_id: int | None
    author_project_id: int | None
    author_role_key: str | None
    author_role_no: int | None
    session_json: str | None
    token_id: int | None
    signaled_ts: int | None
    signal_sub_id: int | None
    created_ts: int
    seq: int
    decided_note: str | None = None


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Human]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.human_request h WHERE {where}",  # noqa: S608 — where — из кода
        tuple(params),
    ).fetchall()
    return [Human(*r) for r in rows]


def get(conn: sqlite3.Connection, human_id: int) -> Human | None:
    rows = _select(conn, "h.id = ?", (human_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, human_id: int, **sets) -> None:
    """Любая запись в строку двигает `seq`."""
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.human_request SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), human_id],
    )


def _close(conn: sqlite3.Connection, human_id: int, status: str, *, now_ms: int, **sets) -> None:
    """ЕДИНСТВЕННОЕ место, где заявка получает итог (решение, срок, снятие автором). Ф95-1 (A.5): итог
    `failed`/`expired`/`cancelled` снимает правила почты, заведённые заявкой по домену регистрации; `done` и
    `later` их оставляют."""
    _update(conn, human_id, status=status, decided_ts=now_ms, **sets)
    if status in reg_mail_rule.DROP_STATUSES:
        reg_mail_rule.drop_for_request(conn, human_id, now_ms=now_ms, status=status)


def _session(req: Human) -> dict:
    try:
        value = json.loads(req.session_json) if req.session_json else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def last_window(conn: sqlite3.Connection, project_id: int | None, role: str | None) -> dict | None:
    """Ф76-5: окно браузера сессии — из её последней заявки с профилем (ключ — проект и роль автора, любая
    линия и любой статус; строки живут 30 дней после решения). `{request_id, profile, tab, url, site, ts}`
    без маскировки (как сырая заявка агенту в `GET /human-requests`); нет — `None`."""
    if not project_id or not isinstance(role, str):
        return None
    try:
        _, key = subs_mod.role_form(role)
    except subs_mod.SubError:
        return None
    if key is None:
        return None
    rows = _select(
        conn,
        "h.author_project_id = ? AND h.author_role_key = ? AND h.profile IS NOT NULL "
        "ORDER BY h.created_ts DESC, h.id DESC LIMIT 1",
        (project_id, key),
    )
    if not rows:
        return None
    req = rows[0]
    return {
        "request_id": req.id,
        "profile": req.profile,
        "tab": req.tab,
        "url": req.url,
        "site": req.site,
        "ts": req.created_ts,
    }


def has_shot(req: Human) -> bool:
    return req.shot_sha256 is not None and req.shot_purged_ts is None


def human_dict(req: Human, *, source: bool = True) -> dict:
    """Словарь заявки для выдачи — один и тот же владельцу, автору (линии) и ответчику-агенту; кому какие строки
    видны, решает зовущий. Без тройки сессии, номеров токенов и хеша скриншота — список ключей
    разрешительный. `shot` — `{size, mime}` или `null` (нет или байты стёрты); сами байты —
    `GET /human-requests/{id}/shot`."""
    # 12а: источник ответа (`decided_by`) виден только владельцу; сессии и ответчику ответ по каналу — ответ владельца.
    sess = _session(req)
    out = {
        "id": req.id,
        "project_id": req.project_id,
        "kind": req.kind,
        "site": req.site,
        "url": req.url,
        "doing": req.doing,
        "need": req.need,
        "profile": req.profile,
        "tab": req.tab,
        "shot": {"size": req.shot_size, "mime": req.shot_mime} if has_shot(req) else None,
        "expires_ts": req.expires_ts,
        "status": req.status,
        "decided_ts": req.decided_ts,
        "decided_by": req.decided_by,
        # Ф93-2 (A): причина исхода (что написал решивший) — автору, агенту, владельцу и боту; `null` — нет.
        "decided_note": req.decided_note,
        "role": sess.get("role"),
        "role_no": req.author_role_no,
        "created_ts": req.created_ts,
        "seq": req.seq,
    }
    if not source:
        del out["decided_by"]
    return out


def _audit(
    conn: sqlite3.Connection,
    req: Human,
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
    """Аудит без чужого ввода: номера и постоянные слова; текста автора и адреса страницы в нём нет."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or f"human-{req.id}",
        action=action,
        result=result,
        detail={
            "human_id": req.id,
            "project_id": req.project_id,
            "kind": req.kind,
            "sub_id": req.author_sub_id,
            **(detail or {}),
        },
    )


# --- склад скриншотов -------------------------------------------------------------------------------------


def root(data_dir: Path) -> Path:
    return Path(data_dir) / DIR_NAME


def file_path(data_dir: Path, sha256: str) -> Path:
    if _SHA_RE.fullmatch(sha256 or "") is None:
        raise ValueError("sha256")
    return root(data_dir) / sha256[:2] / sha256


def tmp_path(data_dir: Path) -> Path:
    """Временный файл приёма — в корне склада (тот же раздел диска, что и место назначения)."""
    root(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    return root(data_dir) / f"{TMP_PREFIX}{secrets.token_hex(16)}"


def _refs(conn: sqlite3.Connection, sha256: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM main.human_request WHERE shot_sha256 = ? AND shot_purged_ts IS NULL", (sha256,)
    ).fetchone()[0]


def _release_bytes(conn: sqlite3.Connection, sha256: str) -> None:
    """Правило ссылок: после изменения строк в ЭТОЙ транзакции нет нестёртых ссылок на `sha256` — файл
    удаляется."""
    if _refs(conn, sha256):
        return
    try:
        file_path(media_mod.data_dir_of(conn), sha256).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        _logger.warning("заявки «нужен человек»: файл не удалён (%s)", type(exc).__name__)


def _purge_shot(conn: sqlite3.Connection, req: Human, now_ms: int) -> None:
    """Стереть байты скриншота заявки (строка остаётся, `seq` не двигается: в выдаче `shot` станет `null` со
    следующей записью строки или чтением)."""
    if not has_shot(req):
        return
    conn.execute("UPDATE main.human_request SET shot_purged_ts = ? WHERE id = ?", (now_ms, req.id))
    _release_bytes(conn, req.shot_sha256)


def _commit_shot(conn: sqlite3.Connection, shot: dict) -> None:
    """Принятый временный файл -> склад (по sha256; такой уже лежит — временный удаляется)."""
    final = file_path(media_mod.data_dir_of(conn), shot["sha256"])
    final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = Path(shot["tmp"])
    if final.is_file() and not final.is_symlink() and final.stat().st_size == shot["size"]:
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, final)


# --- сроки (П1) -------------------------------------------------------------------------------------------

_DUE = (
    "h.status = 'pending' AND ((h.expires_ts IS NOT NULL AND h.expires_ts <= ?) "
    "OR (h.expires_ts IS NULL AND h.created_ts <= ?))"
)


def bump_project(conn: sqlite3.Connection, project_id: int) -> int:
    """Ф64-1 (П9): «Начать день» — ждущие заявки проекта получают новый `seq` (каждая свой: страница
    `bot_changes` режется по `seq`), бот видит их в ленте изменений. Возвращает число заявок."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM main.human_request WHERE project_id = ? AND status = 'pending' ORDER BY id",
            (project_id,),
        )
    ]
    for human_id in ids:
        conn.execute("UPDATE main.human_request SET seq = ? WHERE id = ?", (next_seq(conn), human_id))
    return len(ids)


def bump(conn: sqlite3.Connection, human_id: int) -> None:
    """Ф93-2 (B): поручение агенту по заявке открылось или закрылось — новый `seq` строки: бот (страница
    изменений) и интерфейс узнают, у владельца ли теперь заявка (`at_owner`)."""
    conn.execute("UPDATE main.human_request SET seq = ? WHERE id = ?", (next_seq(conn), human_id))


def hold_until(conn: sqlite3.Connection, human_id: int, until_ts: int) -> bool:
    """Ф96-1 (П1): ждущая заявка не снимается по сроку раньше `until_ts` — срок `expires_ts` сдвигается до
    `max(expires_ts, until_ts)` (руки у владельца посреди поручения агента). Заявка без срока (снимается через
    сутки от создания) получает срок, только если сутки кончаются раньше `until_ts`. Не ждущая — не трогается.
    Свой `seq` не двигает (его двигает запись поручения, `_bump_human`). -> сдвинут ли срок."""
    req = get(conn, human_id)
    if req is None or req.status != "pending":
        return False
    edge = req.expires_ts if req.expires_ts is not None else req.created_ts + NO_TTL_MS
    if edge >= until_ts:
        return False
    conn.execute("UPDATE main.human_request SET expires_ts = ? WHERE id = ?", (until_ts, human_id))
    return True


def has_due(conn: sqlite3.Connection, now_ms: int) -> bool:
    """Читатель: есть ли что снимать по сроку (минутный цикл и чтение пишут, только когда есть)."""
    row = conn.execute(
        f"SELECT 1 FROM main.human_request h WHERE {_DUE} LIMIT 1",  # noqa: S608 — фрагмент задан кодом
        (now_ms, now_ms - NO_TTL_MS),
    ).fetchone()
    return row is not None


def sweep(conn: sqlite3.Connection, now_ms: int) -> int:
    """П1: ждущая заявка с вышедшим сроком (или без срока старше суток) -> `expired`; автору — тот же сигнал
    исхода. Зовут минутный цикл ядра, `create`, `decide`, `cancel` и чтение."""
    closed = 0
    for req in _select(conn, _DUE, (now_ms, now_ms - NO_TTL_MS)):
        _close(conn, req.id, "expired", now_ms=now_ms)
        closed += 1
    return closed


# --- автор: подписки сессии и линия (П7) ------------------------------------------------------------------


def _project_subs(conn: sqlite3.Connection, token_id: int, session) -> list[subs_mod.SubRec]:
    """Живые подписки НА ПРОЕКТ (`project_id > 0`) сессии `session` под токеном `token_id`; процесс сессии
    мёртв — пусто (как `subs.author_sub`)."""
    if session is None or not subs_mod.is_alive(
        session.pid, session.proc_start, getattr(session, "host", None)
    ):
        return []
    rows = conn.execute(
        f"SELECT {subs_mod._SUB_COLUMNS} FROM main.session_sub WHERE ended_ts IS NULL "  # noqa: S608, SLF001
        "AND api_token_id = ? AND session_pid = ? AND session_proc_start = ? AND session_id = ? "
        "AND project_id > 0 ORDER BY id",
        (token_id, session.pid, session.proc_start, session.session_id),
    ).fetchall()
    return [subs_mod.SubRec(*row) for row in rows]


def _lines(recs: list[subs_mod.SubRec]) -> set[tuple]:
    return {(r.project_id, r.role_key, r.role_no) for r in recs if r.role_key is not None}


def _line_of(req: Human) -> tuple:
    return (req.author_project_id, req.author_role_key, req.author_role_no)


def _author_live(conn: sqlite3.Connection, req: Human) -> bool:
    """Подписка автора заявки ещё жива (та же строка `session_sub`, какой бы ни стала её роль)."""
    if req.author_sub_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM main.session_sub WHERE id = ? AND ended_ts IS NULL", (req.author_sub_id,)
    ).fetchone()
    return row is not None


def _owned(conn: sqlite3.Connection, req: Human, recs: list[subs_mod.SubRec]) -> bool:
    """Заявка принадлежит сессии с подписками `recs`: пока подписка автора жива — только ей (смена роли на
    месте заявку не отрывает и соседу, занявшему прежнюю линию, её не отдаёт); подписки автора уже нет —
    наследнику линии (проект, роль, номер). То же условие, что в `_signal_pred`."""
    if any(r.id == req.author_sub_id for r in recs):
        return True
    return _line_of(req) in _lines(recs) and not _author_live(conn, req)


# Ждущие заявки подписки `sub`: её собственные и заявки её линии, чей автор уже снят (наследник).
_OPEN_OF_SUB = (
    "h.status = 'pending' AND (h.author_sub_id = ? OR (h.author_project_id = ? AND h.author_role_key = ? "
    "AND h.author_role_no = ? AND NOT EXISTS (SELECT 1 FROM main.session_sub live "
    "WHERE live.id = h.author_sub_id AND live.ended_ts IS NULL))) ORDER BY h.id"
)


def _author_sub(conn: sqlite3.Connection, principal, session, project_id: int | None) -> subs_mod.SubRec:
    """П7: подписка автора. Одна подписка сессии на проект — она; несколько — нужен `project_id`; нужна роль
    (исход уходит линии — у безрольной подписки после перезапуска он потерялся бы)."""
    recs = _project_subs(conn, principal.token_id, session)
    if not recs:
        raise Conflict("подпишись на проект: звать человека может сессия с подпиской на проект с ролью")
    if project_id is None:
        if len(recs) > 1:
            listed = ", ".join(str(r.project_id) for r in recs)
            raise Conflict(f"у сессии подписки на несколько проектов ({listed}) — назови project_id")
        sub = recs[0]
    else:
        found = [r for r in recs if r.project_id == project_id]
        if not found:
            raise Conflict(f"у сессии нет подписки на проект {project_id} — подпишись на него")
        sub = found[0]
    if sub.role_key is None:
        raise Conflict("подпишись с ролью: hub_subscribe(role=…) — исход заявки уходит роли")
    return sub


def is_bot(principal) -> bool:
    """Ф54-2: бот владельца в Telegram — токен со скоупом `human_bot`."""
    return principal.has_scope(HUMAN_BOT_SCOPE)


def sees_source(principal) -> bool:
    """Кому отдаётся `decided_by`: владельцу (кука) и его боту (Ф54-2 П5 — строка «закрыто: в хабе / агентом»)."""
    return principal.is_ui or is_bot(principal)


_NO_ERRAND = {"errand": None, "errand_returned": None}


def _errand_map(conn: sqlite3.Connection) -> dict[int, dict]:
    from . import owner_queue  # импорт внутри: очередь сама опирается на этот модуль

    return owner_queue.human_errands(conn)


def out_dict(conn: sqlite3.Connection, principal, req: Human, errands: dict[int, dict] | None = None) -> dict:
    """Заявка для выдачи принципалу: `decided_by` — владельцу и его боту; боту ещё `project_name` (в сообщении
    Telegram проект называется именем, других ручек у бота нет). Ф58-3: владельцу и боту — `errand` (открытое
    поручение агенту по заявке) и `errand_returned` (`{reason, detail, ts, by}` — вернулось владельцу, Ф65-1);
    `errands` — готовая карта `owner_queue.human_errands` для списка, иначе читается здесь."""
    out = human_dict(req, source=sees_source(principal))
    if sees_source(principal):
        out.update((errands if errands is not None else _errand_map(conn)).get(req.id, _NO_ERRAND))
    if is_bot(principal):
        row = conn.execute("SELECT name FROM main.project WHERE id = ?", (req.project_id,)).fetchone()
        out["project_name"] = row[0] if row is not None else None
        out["at_owner"] = at_owner(conn, req, out.get("errand"), out.get("errand_returned"))
    return out


def at_owner(conn: sqlite3.Connection, req: Human, errand: dict | None, returned: dict | None) -> bool:
    """Ф93-2 (B): заявка у владельца — бот шлёт её с кнопками. Нет открытого поручения агенту И (маршрут карточки
    `me` — таблица «Кто отвечает на карточки», строка «Деньги» у `payment`, ручная сессия хаба — ИЛИ агент
    вернул последнее поручение владельцу: сам или ядро за зависание). Заявку, которую решает агент, бот не шлёт.
    Ф96-1 (П3): открытое поручение, у которого руки у владельца (`hands = "owner_channel"`), — заявка у владельца."""
    if errand is not None:
        return errand.get("hands") == "owner_channel"
    from . import card_route as card_route_mod  # импорт внутри: маршрут опирается на очередь
    from . import owner_queue as zq

    if isinstance(returned, dict) and returned.get("by") in zq.RETURNED_BY:
        return True
    hub_id = zq._hub_of_sub(conn, req.author_sub_id)  # noqa: SLF001
    route = card_route_mod.for_card(conn, req.project_id, "human", hub_id, ref=f"human:{req.id}")
    return route == card_route_mod.ME


def _check_author_token(conn: sqlite3.Connection, principal) -> None:
    """Допуск к `create` и `cancel` (раздел 3 + П3): не кука, не ответчик-агент, не бот, bearer со скоупом
    `draft`, не дежурная, не оркестратор и не фоновый токен (`check_attach`)."""
    if principal.is_ui or human_only(principal):
        raise Denied("forbidden")
    if not (principal.has_scope("draft") or principal.has_scope("send")):
        raise Denied("forbidden")
    found = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = found[0] if found and isinstance(found[0], str) else None
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise Denied("звать человека может только сессия проекта (не дежурная и не оркестратор)") from exc


# --- создание ---------------------------------------------------------------------------------------------


class Created(NamedTuple):
    human: Human
    created: bool
    # Ф95-1 (A.4): правила почты по домену регистрации, заведённые заявкой (`reg_mail_rule.add_for_request`)
    mail_rule: dict | None = None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def create(
    conn: sqlite3.Connection,
    *,
    principal,
    session,
    kind: str,
    need: str,
    now_ms: int,
    project_id: int | None = None,
    site: str | None = None,
    url: str | None = None,
    doing: str | None = None,
    profile: str | None = None,
    tab: str | None = None,
    ttl_min: int = TTL_MIN_DEFAULT,
    shot: dict | None = None,
    client_name: str | None = None,
    request_id: str = "",
) -> Created:
    """Завести заявку. `shot` — принятый скриншот `{tmp, sha256, size, mime}` (тип и размер проверил
    маршрут) или `None`. Отказы: 403 — кука, ответчик-агент, bearer без `draft`, дежурная, оркестратор; 409 —
    нет подписки на проект, не назван проект из нескольких, подписка без роли; 422 — форма; 429 — пределы.
    Повтор (П7): открытая заявка той же линии, того же вида и с тем же непустым `url` — она же,
    `created=False` (новый скриншот не берётся)."""
    _check_author_token(conn, principal)
    if session is None:
        raise Invalid("session: сессия не определена")
    if kind not in KINDS:
        raise Invalid(f"kind: {', '.join(KINDS)}")
    need = (need or "").strip()
    if not need:
        raise Invalid("need: одной строкой — что нужно сделать человеку")
    if not isinstance(ttl_min, int) or isinstance(ttl_min, bool) or not 0 <= ttl_min <= TTL_MIN_MAX:
        raise Invalid(f"ttl_min: от 1 до {TTL_MIN_MAX} минут, 0 — без срока")
    sub = _author_sub(conn, principal, session, project_id)
    line = (sub.project_id, sub.role_key, sub.role_no)
    url = _text(url)

    sweep(conn, now_ms)
    opened = _select(conn, _OPEN_OF_SUB, (sub.id, *line))
    if url is not None:
        same = [h for h in opened if h.kind == kind and h.url == url]
        if same:
            return Created(same[0], False)
    if len(opened) >= OPEN_PER_LINE:
        raise Limited(LIMIT_LINE)
    count = conn.execute(
        "SELECT COUNT(*) FROM main.human_request WHERE project_id = ? AND status = 'pending'",
        (sub.project_id,),
    ).fetchone()[0]
    if count >= OPEN_PER_PROJECT:
        raise Limited(LIMIT_PROJECT)
    count = conn.execute(
        "SELECT COUNT(*) FROM main.human_request WHERE project_id = ? AND created_ts > ?",
        (sub.project_id, now_ms - DAY_MS),
    ).fetchone()[0]
    if count >= NEW_PER_PROJECT_DAY:
        raise Limited(LIMIT_DAY)

    if shot is not None:
        _commit_shot(conn, shot)
    session_json = json.dumps(
        {
            "pid": session.pid,
            "proc_start": session.proc_start,
            "session_id": session.session_id,
            "sub_id": sub.id,
            "role": subs_mod.author_role(sub),
        },
        ensure_ascii=False,
    )
    cur = conn.execute(
        "INSERT INTO main.human_request (project_id, kind, site, url, doing, need, profile, tab, "
        "shot_sha256, shot_size, shot_mime, expires_ts, status, author_sub_id, author_project_id, "
        "author_role_key, author_role_no, session_json, token_id, created_ts, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sub.project_id,
            kind,
            _text(site),
            url,
            _text(doing),
            need,
            _text(profile),
            _text(tab),
            shot["sha256"] if shot is not None else None,
            shot["size"] if shot is not None else None,
            shot["mime"] if shot is not None else None,
            now_ms + ttl_min * _MIN_MS if ttl_min else None,
            sub.id,
            sub.project_id,
            sub.role_key,
            sub.role_no,
            session_json,
            principal.token_id,
            now_ms,
            next_seq(conn),
        ),
    )
    req = get(conn, cur.lastrowid)
    assert req is not None
    _audit(
        conn,
        req,
        action="human_create",
        result="pending",
        now_ms=now_ms,
        actor="api",
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
        detail={"shot": shot is not None, "ttl_min": ttl_min},
    )
    return Created(req, True, reg_mail_rule.add_for_request(conn, req, now_ms=now_ms))


# --- чтение (П2, П5, П8) ----------------------------------------------------------------------------------


def agent_sees(conn: sqlite3.Connection, principal, req: Human) -> bool:
    """П5: ответчик-агент видит заявку — её проект среди проектов токена и (Ф66-1, П1/П2) маршрут `human`
    проекта — `agent` (сессия-автор не ручная сессия хаба) ИЛИ по заявке открыто поручение «Реши». Вид
    заявки значения не имеет."""
    if principal.project_ids is not None and req.project_id not in principal.project_ids:
        return False
    from . import owner_queue as zq  # импорт внутри: очередь сама опирается на этот модуль

    hub_id = zq._hub_of_sub(conn, req.author_sub_id)  # noqa: SLF001
    return zq.agent_may(conn, f"human:{req.id}", req.project_id, hub_id, "human")


def _reader_subs(conn: sqlite3.Connection, principal, session) -> list[subs_mod.SubRec]:
    """Подписки читателя-сессии на проект с ролью; пусто — не автор: 403."""
    recs = [r for r in _project_subs(conn, principal.token_id, session) if r.role_key is not None]
    if not recs:
        raise Denied("заявки «нужен человек» видит сессия с подпиской на проект с ролью")
    return recs


def readable(
    conn: sqlite3.Connection, principal, req: Human, session=None, now_ms: int | None = None
) -> bool:
    """П2: владельцу (кука) — всё; боту владельца (Ф54-2) — ждущие и закрытые за сутки (`now_ms` не назван — без
    предела давности); ответчику-агенту — по П5; сессии — заявки своей линии (П8). Сессия без подписки с
    ролью — `Denied`."""
    if principal.is_ui:
        return True
    if req.status == "pending" and (is_bot(principal) or principal.has_scope(HUMAN_SCOPE)):
        # Ф64-1 (раздел 3, П9): ждущие заявки проекта с закрытым днём боту и агенту владельца не отдаются; при
        # «Начать день» их `seq` растёт (`bump_project`) — бот получит их утром.
        if workday_mod.project_closed_ts(conn, req.project_id) is not None:
            return False
    if is_bot(principal):
        return (
            req.status == "pending"
            or now_ms is None
            or (req.decided_ts is not None and req.decided_ts > now_ms - DAY_MS)
        )
    if principal.has_scope(HUMAN_SCOPE):
        return agent_sees(conn, principal, req)
    return _owned(conn, req, _reader_subs(conn, principal, session))


def read_one(
    conn: sqlite3.Connection, principal, human_id: int, session=None, now_ms: int | None = None
) -> Human:
    """Одна заявка; невидимая и несуществующая неотличимы — `NotFound`."""
    recs = None
    if not principal.is_ui and not human_only(principal):
        recs = _reader_subs(conn, principal, session)  # 403 — раньше, чем «есть ли такая»
    req = get(conn, human_id)
    if req is None:
        raise NotFound("not found")
    if recs is not None:
        if not _owned(conn, req, recs):
            raise NotFound("not found")
    elif not readable(conn, principal, req, now_ms=now_ms):
        raise NotFound("not found")
    return req


def list_requests(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    session=None,
    project_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """`GET /human-requests`: по умолчанию — ждущие и решённые за сутки, новые первыми; `status` — один
    статус; `project_id` — один проект. Кому что видно — `readable`."""
    where = "h.status = ?" if status is not None else "(h.status = 'pending' OR h.decided_ts > ?)"
    params: list = [status if status is not None else now_ms - DAY_MS]
    if project_id is not None:
        where += " AND h.project_id = ?"
        params.append(project_id)
    recs = None
    if not principal.is_ui and not human_only(principal):
        recs = _reader_subs(conn, principal, session)
    errands = _errand_map(conn) if sees_source(principal) else {}
    out: list[dict] = []
    for req in _select(conn, where + " ORDER BY h.id DESC", params):
        if recs is not None:
            if not _owned(conn, req, recs):
                continue
        elif not readable(conn, principal, req, now_ms=now_ms if is_bot(principal) else None):
            continue  # боту — закрытые только за сутки, и при явном `status`
        out.append(out_dict(conn, principal, req, errands))
    return out[: max(1, min(int(limit), LIST_MAX))]


CHANGES_MAX = 100
CHANGES_WAIT_MAX_S = 25


def bot_changes(
    conn: sqlite3.Connection, principal, *, since: int, now_ms: int, limit: int = CHANGES_MAX
) -> dict:
    """Ф54-2 (П6): `GET /human-requests/changes` — ТОЛЬКО боту владельца (зовущий проверил): заявки с
    `since < seq <= watermark` по возрастанию `seq`, не больше `limit`. `seq` ответа — куда клиенту поставить
    свою отметку: страница полна — `seq` её последней заявки (остальное следующим запросом), иначе текущий
    максимум глобального счётчика. `since` больше максимума (база восстановлена из копии) — пусто и текущий
    максимум: клиент берёт отметку из ответа. Закрытые раньше суток назад боту не отдаются (как в
    списке и в чтении одной), отметка мимо них всё равно идёт."""
    watermark = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    rows = _select(conn, "h.seq > ? AND h.seq <= ? ORDER BY h.seq LIMIT ?", (since, watermark, limit))
    seq = rows[-1].seq if len(rows) >= limit else watermark
    errands = _errand_map(conn)
    items = [
        out_dict(conn, principal, h, errands) for h in rows if readable(conn, principal, h, now_ms=now_ms)
    ]
    return {"items": items, "seq": seq}


def bot_has_changes(conn: sqlite3.Connection, *, since: int) -> bool:
    watermark = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    if since > watermark:
        return True  # отметка клиента из будущего — ответить сразу, он возьмёт `seq` из ответа
    row = conn.execute("SELECT 1 FROM main.human_request h WHERE h.seq > ? LIMIT 1", (since,)).fetchone()
    return row is not None


def changes(conn: sqlite3.Connection, *, since: int, watermark: int, limit: int) -> list[dict]:
    """`/sync` и SSE — ТОЛЬКО UI-сессии (зовущий проверил, П13): заявки с `since < seq <= watermark` по
    возрастанию `seq`."""
    rows = _select(conn, "h.seq > ? AND h.seq <= ? ORDER BY h.seq LIMIT ?", (since, watermark, limit))
    errands = _errand_map(conn) if rows else {}
    return [{**human_dict(h), **errands.get(h.id, _NO_ERRAND)} for h in rows]


def shot_file(
    conn: sqlite3.Connection, principal, human_id: int, now_ms: int | None = None
) -> tuple[Path, str, int]:
    """`GET /human-requests/{id}/shot`: `(путь, тип, размер)`. Только владелец, его бот (Ф54-2) и ответчик-агент
    (П2, П5); прочий bearer, в том числе автор, — `Denied`; нет заявки, скриншота или байты стёрты —
    `NotFound`."""
    if not principal.is_ui and not human_only(principal):
        raise Denied("forbidden")
    req = get(conn, human_id)
    if req is None or not readable(conn, principal, req, now_ms=now_ms) or not has_shot(req):
        raise NotFound("not found")
    mime = req.shot_mime if req.shot_mime in SHOT_TYPES.values() else "application/octet-stream"
    return file_path(media_mod.data_dir_of(conn), req.shot_sha256), mime, req.shot_size or 0


# --- решение (П2, П4) и отмена ----------------------------------------------------------------------------


def decide(
    conn: sqlite3.Connection,
    human_id: int,
    outcome: str,
    *,
    principal,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
    by_word: bool = False,
    note: str | None = None,
) -> Human:
    """«Готово» / «Не вышло» / «Не сейчас». Владелец (кука) — любую ждущую; ответчик-агент — только заявку,
    которую видит (`agent_sees`: маршрут `agent` или поручение «Реши»; иначе 409 «ждёт владельца» на ЛЮБОЙ из трёх
    исходов, П4; чужой проект — 404); бот владельца (Ф54-2, П5) — любую ждущую, без проверки выключателя,
    `decided_by = "bot"`. Прочий bearer сюда не доходит (маршрут). Решённая, отменённая, просроченная —
    409. Ф75-1: `by_word` — агент решает по слову владельца в разговоре (сверено `owner_word.check` в очереди):
    маршрут не сверяется (это клик владельца), проект токена — как обычно. Ф93-2 (A): `note` — причина исхода
    (до `NOTE_MAX`, пустая — NULL), при любом исходе."""
    if outcome not in OUTCOMES:
        raise Invalid(f"outcome: {', '.join(OUTCOMES)}")
    note = _text(note)
    if note is not None and len(note) > NOTE_MAX:
        raise Invalid(f"note: не длиннее {NOTE_MAX}")
    bot = is_bot(principal)
    agent = not principal.is_ui and not bot
    if agent and not principal.has_scope(HUMAN_SCOPE):
        raise Denied("forbidden")
    sweep(conn, now_ms)
    req = get(conn, human_id)
    if req is None:
        raise NotFound(f"заявка {human_id} не найдена")
    if agent:
        if principal.project_ids is not None and req.project_id not in principal.project_ids:
            raise NotFound(f"заявка {human_id} не найдена")
        if not by_word and not agent_sees(conn, principal, req):
            raise Conflict(WAITS_OWNER)
    if req.status != "pending":
        raise Conflict(f"заявка уже закрыта: {STATUS_RU.get(req.status, req.status)}")
    by = "bot" if bot else "agent" if agent else "ui"
    _close(
        conn,
        human_id,
        outcome,
        now_ms=now_ms,
        decided_by=by,
        decided_token_id=principal.token_id if agent or bot else None,
        decided_note=note,
    )
    req = get(conn, human_id)
    assert req is not None
    _audit(
        conn,
        req,
        action="human_decide",
        result=outcome,
        now_ms=now_ms,
        actor=by,
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return req


def cancel(
    conn: sqlite3.Connection,
    human_id: int,
    *,
    principal,
    session,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
) -> Human:
    """Автор снял заявку сам (препятствие исчезло) — право по линии (П7): сессия с живой подпиской той же
    линии. Сигнала автору нет; байты скриншота стираются сразу. Не автор — 403; заявка другой линии — 404;
    уже закрыта — 409."""
    _check_author_token(conn, principal)
    recs = [r for r in _project_subs(conn, principal.token_id, session) if r.role_key is not None]
    if not recs:
        raise Denied("снять заявку может сессия с подпиской той же роли проекта")
    sweep(conn, now_ms)
    req = get(conn, human_id)
    if req is None or not _owned(conn, req, recs):
        raise NotFound(f"заявка {human_id} не найдена")
    if req.status != "pending":
        raise Conflict(f"заявка уже закрыта: {STATUS_RU.get(req.status, req.status)}")
    _close(conn, human_id, "cancelled", now_ms=now_ms)
    _purge_shot(conn, req, now_ms)
    req = get(conn, human_id)
    assert req is not None
    _audit(
        conn,
        req,
        action="human_cancel",
        result="cancelled",
        now_ms=now_ms,
        actor="api",
        request_id=request_id,
        api_token_id=principal.token_id,
        client_name=client_name,
    )
    return req


# --- чистка (часовой проход) ------------------------------------------------------------------------------


def cleanup(conn: sqlite3.Connection, *, now_ms: int) -> dict:
    """Часовой проход (П1: сроки — НЕ здесь, а в минутном цикле; здесь только страховка `sweep`): байты
    скриншотов закрытых заявок старше 7 суток; закрытые строки старше 30 дней — долой (с байтами); брошенные
    временные файлы приёма и байты без строки (откат создания) старше часа."""
    swept = sweep(conn, now_ms)
    purged = 0
    for req in _select(
        conn,
        "h.status != 'pending' AND h.shot_sha256 IS NOT NULL AND h.shot_purged_ts IS NULL "
        "AND COALESCE(h.decided_ts, h.created_ts) <= ?",
        (now_ms - SHOT_KEEP_MS,),
    ):
        _purge_shot(conn, req, now_ms)
        purged += 1
    old = _select(conn, "h.status != 'pending' AND h.created_ts <= ?", (now_ms - KEEP_MS,))
    for req in old:
        conn.execute("DELETE FROM main.human_request WHERE id = ?", (req.id,))
        if has_shot(req):
            _release_bytes(conn, req.shot_sha256)
    data_dir = media_mod.data_dir_of(conn)
    _sweep_tmp(data_dir)
    _sweep_orphans(conn, data_dir)
    return {"swept": swept, "purged": purged, "removed": len(old)}


def _sweep_tmp(data_dir: Path) -> None:
    base = root(data_dir)
    try:
        names = os.listdir(base)
    except OSError:
        return
    limit = time.time() - TMP_MAX_AGE_S
    for name in names:
        if not name.startswith(TMP_PREFIX):
            continue
        with contextlib.suppress(OSError):
            path = base / name
            if path.lstat().st_mtime < limit:
                path.unlink()


def _sweep_orphans(conn: sqlite3.Connection, data_dir: Path) -> None:
    """Байты без строки: `_commit_shot` кладёт файл ДО вставки строки; откатилась транзакция создания — файл
    остался бы навсегда. Файл склада старше часа без нестёртых ссылок удаляется (проход — в писателе)."""
    base = root(data_dir)
    limit = time.time() - TMP_MAX_AGE_S
    try:
        shelves = [d for d in os.listdir(base) if len(d) == 2]
    except OSError:
        return
    for shelf in shelves:
        try:
            names = os.listdir(base / shelf)
        except OSError:
            continue
        for name in names:
            if _SHA_RE.fullmatch(name) is None or not name.startswith(shelf):
                continue
            path = base / shelf / name
            with contextlib.suppress(OSError):
                if path.lstat().st_mtime < limit and not _refs(conn, name):
                    path.unlink()


# --- исход — автору сигналом (раздел 3, П8, П11) ----------------------------------------------------------


def _signal_pred(sub, now_ms: int) -> tuple[str, list]:
    """ЕДИНСТВЕННОЕ условие «исход заявки пора отдать подписке `sub`» — и для счёта, и для выдачи (образец —
    `presses._signal_pred`): заявка в исходе из `SIGNAL_OUTCOMES`, не отдана, не старше `OUTCOME_TTL_MS`,
    адресована ей — сама подписка автора ИЛИ её линия (проект, роль, номер), когда подписки автора уже нет
    (наследник линии продолжает работу роли, П8)."""
    where = (
        "h.signaled_ts IS NULL AND h.decided_ts > ? AND h.status IN ('done', 'failed', 'later', 'expired') "
        "AND (h.author_sub_id = ?"
    )
    params: list = [now_ms - OUTCOME_TTL_MS, sub.id]
    if sub.role_key is not None and sub.project_id != subs_mod.NO_PROJECT_ID:
        where += (
            " OR (h.author_project_id = ? AND h.author_role_key = ? AND h.author_role_no = ? AND NOT EXISTS "
            "(SELECT 1 FROM main.session_sub live WHERE live.id = h.author_sub_id AND live.ended_ts IS NULL))"
        )
        params += [sub.project_id, sub.role_key, sub.role_no]
    return where + ")", params


def signal_count(conn: sqlite3.Connection, sub, *, now_ms: int) -> int:
    """Читатель, для ожидания: исходы заявок, которые пора отдать подписке. Все они срочные (сессию хаба
    будят и посреди хода, и «тяжёлую холодную»): владелец ждёт у открытой вкладки."""
    where, params = _signal_pred(sub, now_ms)
    return conn.execute(
        f"SELECT COUNT(*) FROM main.human_request h WHERE {where}",  # noqa: S608 — фрагмент задан кодом
        params,
    ).fetchone()[0]


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
    """Отдать подписке исходы заявок её линии — `{"kind": "human", "human_id", "outcome"}`: только число и
    слово из перечня. Сигнал отдаётся один раз (`signaled_ts`) и не двигает `seq` строки. Не влезшие в
    `limit` ждут следующей выдачи."""
    where, params = _signal_pred(sub, now_ms)
    rows = conn.execute(
        f"SELECT h.id, h.status FROM main.human_request h WHERE {where} ORDER BY h.id LIMIT ?",  # noqa: S608
        [*params, max(0, limit)],
    ).fetchall()
    signals: list[dict] = []
    for human_id, status in rows:
        conn.execute(
            "UPDATE main.human_request SET signal_sub_id = ?, signaled_ts = ? WHERE id = ?",
            (sub.id, now_ms, human_id),
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
            detail={"sub_id": sub.id, "kind": "human", "human_id": human_id, "outcome": status},
        )
        signals.append({"kind": "human", "human_id": human_id, "outcome": status})
    return signals
