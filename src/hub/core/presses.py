"""Нажатия кнопок под сообщениями Telegram-ботов (Ф44-1c, docs/architecture.md–4.4 и поправки П1–П4, П6–П8 — они главнее
текста выше них; фактический контракт — раздел 11 спеки).

Нажатие — такое же действие от имени владельца, как отправка сообщения: бот по нему выдаёт ключ, списывает,
отписывает. Поэтому путь тот же, что у вступлений (`join.py`): строка `main.button_press` -> (карточка владельцу)
-> очередь коннектору (`wait_state` / `lease` / `report`) -> исход.

- Владелец из интерфейса (`by='ui'`) — строка сразу `approved`.
- Сессия (`by='session'`) — `pending`, карточка. БЕЗ карточки — ровно в двух случаях (`_auto_way`), оба —
  существующие исключения правила «хаб не отправляет сам», применённые к нажатию (владелец 09.10.2026):
  беседа из списка «без подтверждения» для роли сессии (та же проверка, что у черновика:
  `autosend.list_sub`) и сессия хаба в авто при снятом выключателе `hub_press`
  (`autopilot.session_free`). Условия не ослаблять без прямого слова владельца.
  Ф63-2: в беседе с Telegram-ботом второй выключатель — `bot_chat` (та же `session_free`, `auto_by =
  'autopilot'`), и любой путь «сразу» в ней проходит страховку `bot_guard.risky` по тексту кнопки и
  сообщения с ней (П4) — при создании и повторно при выдаче (`_auto_holds`); сработала — карточка с
  `reason = "bot_guard:<категория>"`.
- Нажатие НЕ идемпотентно (второй ключ, вторая оплата): одна живая строка на кнопку (П2), решение годно
  `APPROVED_TTL_MS` (П3), оборванный шаг и истёкший лиз — `unknown`, в очередь строка не возвращается.

Сырой callback (`data_b64`) наружу не отдаётся никогда: его получает только коннектор в `lease`. Ответ бота
(`result_json`) — чужой ввод: только владельцу и автору нажатия (П8); в аудит, сигнал и карточку он не кладётся.

Каждая запись в строку двигает глобальный `seq`: `/sync` (раздел `presses`) и кадр SSE `press`. Пишет только
внутри `Db.write()`, время — параметром `now_ms`. Импорт `queries` здесь не нужен: видимость беседы
проверяет ручка (`queries.channel_visible`) и передаёт проект.
"""

import json
import sqlite3
import unicodedata
from types import SimpleNamespace
from typing import NamedTuple

from . import autopilot as autopilot_mod
from . import autosend, bot_guard, buttons
from . import card_route as card_route_mod
from . import held as held_mod
from . import subs as subs_mod
from .audit import write_audit
from .db import db_of, next_seq
from .security import check_attach

# fake — для тестов и стенда, как у вступлений.
SOURCES = frozenset({"telegram", "fake"})
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})

LIVE_STATUSES = ("pending", "approved", "leased")
FINAL_STATUSES = ("done", "failed", "unknown", "declined", "expired")
STATUSES = (*LIVE_STATUSES, *FINAL_STATUSES)
REPORT_OUTCOMES = frozenset({"done", "failed", "unknown", "retry"})

_MIN_MS = 60_000
_HOUR_MS = 60 * _MIN_MS
DAY_MS = 24 * _HOUR_MS
# П3: решение годно минуту (у клика владельца и у «сразу» — от создания): позже нажимать уже не то, что просили.
APPROVED_TTL_MS = 60_000
# Шаг коннектора ограничен 25 с; лиз с запасом. Истёк — `unknown`, не возврат в очередь.
LEASE_MS = 40_000
RETRY_MS = 2_000
PENDING_TTL_MS = DAY_MS
KEEP_MS = 30 * DAY_MS
# Исход карточки автору сигналом — не старше суток (как исход черновика, Ф39).
OUTCOME_TTL_MS = DAY_MS
# 4.3: пределы «сразу» — по `button_press.auto`, отдельно от сообщений.
PRESS_PER_HOUR = 40  # на беседу (обе ветки); Ф66-1: слово владельца — было 20
PRESS_TOKEN_PER_HOUR = 60  # ветка списка — на токен
PRESS_SESSION_PER_HOUR = autosend.AUTOPILOT_SESSION_PER_HOUR  # ветка выключателя — на сессию хаба
PRESS_SESSION_PER_DAY = autosend.AUTOPILOT_SESSION_PER_DAY

TEXT_MAX = buttons.MAX_TEXT
RESULT_MESSAGE_MAX = 500
RESULT_URL_MAX = 1024
REASON_MAX = 64
QUOTE_MAX = 300
LIST_MAX = 200

SWITCH = "hub_press"
BOT_SWITCH = autosend.BOT_CHAT  # Ф63-2: беседы с ботом
STALE = "stale_button"


_LOOSE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _loose(text: object) -> str:
    """Текст кнопки, как его видит сессия в выдаче (`mcp_server.format.one_line`): управляющие и невидимые
    символы (ZWJ в эмодзи, перевод строки, RLM) там заменены пробелом — сверяем после той же замены с обеих
    сторон. От подмены кнопки защищает не текст, а точное совпадение данных при выдаче (`_prepare`)."""
    return "".join(" " if unicodedata.category(ch) in _LOOSE_CATEGORIES else ch for ch in str(text or ""))


class PressError(Exception):
    """Отказ операции; маршрут переводит подкласс в HTTP-код."""


class Invalid(PressError):
    """422."""


class Denied(PressError):
    """403."""


class NotFound(PressError):
    """404."""


class Conflict(PressError):
    """409."""


class StaleLease(PressError):
    """409: отчёт по устаревшему лизу."""


# --- строка -----------------------------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "account_id",
    "channel_id",
    "project_id",
    "message_id",
    "ext_id",
    "row",
    "col",
    "text",
    "data_b64",
    "by",
    "token_id",
    "client_name",
    "session_json",
    "autopilot_session",
    "auto",
    "auto_by",
    "status",
    "reason",
    "result_json",
    "created_ts",
    "decided_ts",
    "done_ts",
    "updated_ts",
    "not_before",
    "lease_gen",
    "lease_until",
    "attempts",
    "author_sub_id",
    "author_project_id",
    "author_role_key",
    "author_role_no",
    "signaled_ts",
    "signal_sub_id",
    "seq",
)
_SELECT = ", ".join(f"p.{c}" for c in _COLUMNS)


class Press(NamedTuple):
    id: int
    account_id: int
    channel_id: int
    project_id: int | None
    message_id: int
    ext_id: str
    row: int
    col: int
    text: str | None
    data_b64: str | None
    by: str
    token_id: int | None
    client_name: str | None
    session_json: str | None
    autopilot_session: int | None
    auto: int
    auto_by: str | None
    status: str
    reason: str | None
    result_json: str | None
    created_ts: int
    decided_ts: int | None
    done_ts: int | None
    updated_ts: int
    not_before: int
    lease_gen: int
    lease_until: int | None
    attempts: int
    author_sub_id: int | None
    author_project_id: int | None
    author_role_key: str | None
    author_role_no: int | None
    signaled_ts: int | None
    signal_sub_id: int | None
    seq: int


def _select(conn: sqlite3.Connection, where: str, params: tuple | list) -> list[Press]:
    rows = conn.execute(
        f"SELECT {_SELECT} FROM main.button_press p WHERE {where}",  # noqa: S608 — where — из кода
        tuple(params),
    ).fetchall()
    return [Press(*r) for r in rows]


def get(conn: sqlite3.Connection, press_id: int) -> Press | None:
    rows = _select(conn, "p.id = ?", (press_id,))
    return rows[0] if rows else None


def _update(conn: sqlite3.Connection, press_id: int, now_ms: int, **sets) -> None:
    """Любая запись в строку двигает `seq` и `updated_ts` (П3)."""
    sets["updated_ts"] = now_ms
    sets["seq"] = next_seq(conn)
    names = list(sets)
    conn.execute(
        f"UPDATE main.button_press SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
        [*(sets[n] for n in names), press_id],
    )


def _session(press: Press) -> dict:
    try:
        value = json.loads(press.session_json) if press.session_json else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _result(press: Press) -> dict | None:
    if press.result_json is None:
        return None
    try:
        value = json.loads(press.result_json)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _quote(conn: sqlite3.Connection, press: Press) -> tuple[str | None, str | None]:
    """`(название беседы, начало текста сообщения бота)` — для карточки; скрытое и очищенное не цитируется."""
    schema = db_of(press.channel_id)
    chan = conn.execute(
        f"SELECT title FROM {schema}.channel WHERE id = ?",  # noqa: S608 — схема из `db_of`
        (press.channel_id,),
    ).fetchone()
    msg = conn.execute(
        f"SELECT text FROM {schema}.message WHERE id = ? AND hidden = 0 AND purged = 0",  # noqa: S608
        (press.message_id,),
    ).fetchone()
    text = msg[0] if msg is not None and isinstance(msg[0], str) and msg[0] else None
    return (chan[0] if chan is not None else None), (text[:QUOTE_MAX] if text else None)


def press_dict(conn: sqlite3.Connection, press: Press, *, result: bool) -> dict:
    """Словарь нажатия для выдачи. БЕЗ `data_b64` и без тройки сессии — список ключей разрешительный.
    `result` (ответ бота) — только когда зовущий решил, что читателю можно (П8: владелец и автор). У ждущей
    карточки — `channel_title` и `quote` (начало сообщения бота)."""
    out: dict = {
        "id": press.id,
        "account_id": press.account_id,
        "channel_id": press.channel_id,
        "project_id": press.project_id,
        "message_id": press.message_id,
        "row": press.row,
        "col": press.col,
        "text": press.text,
        "by": press.by,
        "role": _session(press).get("role"),
        "auto": bool(press.auto),
        "auto_by": press.auto_by,
        "status": press.status,
        "reason": press.reason,
        "created_ts": press.created_ts,
        "decided_ts": press.decided_ts,
        "done_ts": press.done_ts,
        "updated_ts": press.updated_ts,
        "seq": press.seq,
    }
    if press.status == "pending":
        out["channel_title"], out["quote"] = _quote(conn, press)
    if result:
        out["result"] = _result(press)
    return out


def _audit(
    conn: sqlite3.Connection,
    press: Press,
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
    """Аудит без чужого ввода: номера и постоянные слова; ни текста кнопки, ни callback, ни ответа бота."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or f"press-{press.id}",
        action=action,
        result=result,
        channel_id=press.channel_id,
        detail={
            "press_id": press.id,
            "project_id": press.project_id,
            "account_id": press.account_id,
            "message_id": press.message_id,
            "row": press.row,
            "col": press.col,
            "by": press.by,
            **(detail or {}),
        },
    )


# --- сроки (П3) -------------------------------------------------------------------------------------------


def _deadline(press: Press) -> int:
    """До какого момента `approved` можно выдать коннектору."""
    return (press.decided_ts if press.decided_ts is not None else press.created_ts) + APPROVED_TTL_MS


def overdue(press: Press, now_ms: int) -> bool:
    """Строку пора закрыть по сроку (`sweep`): истёкший лиз или просроченное решение."""
    if press.status == "leased":
        return press.lease_until is None or press.lease_until <= now_ms
    if press.status == "approved":
        return _deadline(press) <= now_ms or press.not_before > _deadline(press)
    return False


def sweep(conn: sqlite3.Connection, now_ms: int) -> int:
    """П3: истёкший лиз -> `unknown` (нажатие могло уйти — в очередь не возвращается); `approved`, не
    выданная за срок, -> `expired`; `approved`, которой пауза FloodWait не даёт успеть, -> `failed`,
    `flood`. Зовут `create`, `decide`, `lease`, часовая чистка и чтение одной строки."""
    closed = 0
    for press in _select(conn, "p.status IN ('approved', 'leased')", ()):
        if not overdue(press, now_ms):
            continue
        if press.status == "leased":
            sets = {"status": "unknown", "reason": "lease_expired"}
        elif _deadline(press) <= now_ms:
            sets = {"status": "expired", "reason": "not_sent_in_time"}
        else:
            sets = {"status": "failed", "reason": "flood"}
        _update(conn, press.id, now_ms, done_ts=now_ms, **sets)
        closed += 1
    return closed


RESTORE_HOLD_REASON = "restored"


def hold_after_restore(conn: sqlite3.Connection, *, now_ms: int) -> int:
    """Ф33-3 (К12): копия восстановлена — одобренные и выданные нажатия прежняя машина могла уже нажать после
    снимка. Как у `unknown` по истёкшему лизу: исход неизвестен, в очередь не возвращается, автору — сигнал
    исхода. Зовёт `core.restore` до первого старта ядра в восстановленном каталоге."""
    held = 0
    for press in _select(conn, "p.status IN ('approved', 'leased')", ()):
        _update(
            conn,
            press.id,
            now_ms,
            status="unknown",
            reason=RESTORE_HOLD_REASON,
            done_ts=now_ms,
            lease_until=None,
        )
        detail = {"from": press.status}
        _audit(conn, press, action="press_restore_hold", result="ok", now_ms=now_ms, detail=detail)
        held += 1
    return held


def _account_pause(conn: sqlite3.Connection, account_id: int, now_ms: int) -> int:
    """Пауза FloodWait аккаунта — на его строках (`not_before` в будущем); новая строка её не обходит."""
    row = conn.execute(
        "SELECT MAX(not_before) FROM main.button_press WHERE account_id = ? AND not_before > ?",
        (account_id, now_ms),
    ).fetchone()
    return row[0] if row is not None and row[0] is not None else 0


# --- 4.2, 4.3: создание -----------------------------------------------------------------------------------


def message_channel(conn: sqlite3.Connection, message_id: int) -> int:
    """Беседа сообщения; нет сообщения, оно скрыто или очищено — `NotFound` (404 без уточнений)."""
    row = conn.execute(
        f"SELECT channel_id FROM {db_of(message_id)}.message WHERE id = ? AND hidden = 0 AND purged = 0",  # noqa: S608
        (message_id,),
    ).fetchone()
    if row is None:
        raise NotFound("сообщение не найдено")
    return row[0]


# Ф66-1, ревью Р5: столбец строки нажатия -> поле записи «сразу да» (`card_route.route_count`).
_ROUTE_KEY = {"channel_id": "channel_id", "token_id": "token_id", "autopilot_session": "session_id"}


def _auto_count(conn: sqlite3.Connection, since: int, column: str, value: int) -> int:
    """Нажатий без карточки после `since` по беседе / токену / сессии хаба — вместе с «сразу да» таблицы
    маршрута (Ф66-1, ревью Р5: пределы общие)."""
    own = conn.execute(
        f"SELECT COUNT(*) FROM main.button_press WHERE auto = 1 AND created_ts > ? AND {column} = ?",  # noqa: S608
        (since, value),
    ).fetchone()[0]
    return own + card_route_mod.route_count(conn, "press", since, **{_ROUTE_KEY[column]: value})


class _Way(NamedTuple):
    """Итог `_auto_way`: `by` — каким исключением нажатие уходит без карточки (`None` — карточка);
    `denied` / `limit_of` — условие выполнено, но исчерпан предел (карточка с пометкой)."""

    by: str | None = None
    autopilot_session: int | None = None
    denied: str | None = None
    limit_of: str | None = None
    # Ф63-2: каким выключателем (`list` | `autopilot` — `hub_press` | `bot_chat`) — для аудита `press_auto`;
    # `guard` — категория страховки (`denied == "guard"`).
    via: str | None = None
    guard: str | None = None


def _press_guard(conn: sqlite3.Connection, channel_id: int, message_id: int, text: str | None) -> str | None:
    """Категория страховки нажатия — всегда `None`: владелец 24.10 снял страховку с нажатий кнопок (кнопки
    называются как угодно, «Оплатить» бывает нужно нажать, чтобы увидеть цену). Нажатие держат выключатели
    `hub_press`/`bot_chat`, таблица карточек и пределы в час. Страховка текстов боту (`bot_guard.risky` в
    `autosend`/`outbox`) остаётся. Ф63-2 П4 (по тексту кнопки и сообщения с ней) — отменено."""
    return None


def _auto_way(
    conn: sqlite3.Connection,
    *,
    principal,
    token_name: str | None,
    session,
    session_key: str | None,
    channel_id: int,
    project_id: int | None,
    now_ms: int,
    waiting,
    message_id: int | None = None,
    text: str | None = None,
) -> tuple[_Way, subs_mod.SubRec | None]:
    """4.3 + П7 — дословно как у черновика (`autosend.try_send`): сначала «запрос от подписанной сессии
    проекта беседы с ролью» (`autosend.list_sub`; нет — карточка); дальше РОВНО два случая:

    1. пара (беседа, роль) в списке владельца и список не закрыт для сессии — пределы `PRESS_PER_HOUR` на беседу и
       `PRESS_TOKEN_PER_HOUR` на токен;
    2. иначе — сессия хаба в авто при снятом выключателе `hub_press` (`autopilot.session_free`: ключ запуска,
       состояние, режим, проект сессии = проект беседы) — пределы как у `autosend._try_autopilot`.

    Беседа без проекта, сессия без подписки с ролью, внешняя сессия, ручной режим, недошедший ключ — карточка.
    Возвращает ещё подписку сессии (для записи роли).

    Ф63-2: в беседе с Telegram-ботом между 1 и 2 — выключатель `bot_chat` (та же `session_free`, те же
    пределы, `by = "autopilot"`); на каждом пути «сразу» — страховка по тексту кнопки `text` и сообщению
    `message_id` (сработала — карточка, `denied = "guard"`)."""
    try:
        sub, by_list = autosend.list_sub(
            conn,
            principal=principal,
            token_name=token_name,
            session=session,
            channel_id=channel_id,
            project_id=project_id,
            now_ms=now_ms,
            waiting=waiting,
        )
    except autosend.NoRoleSub:
        return _Way(), None
    hour = now_ms - _HOUR_MS
    bot = bot_guard.is_bot_chat(conn, channel_id)
    if by_list:
        if bot and message_id is not None:
            hit = _press_guard(conn, channel_id, message_id, text)
            if hit is not None:
                return _Way(denied="guard", via="list", guard=hit), sub
        if _auto_count(conn, hour, "channel_id", channel_id) >= PRESS_PER_HOUR:
            return _Way(denied="limit", limit_of="channel"), sub
        if _auto_count(conn, hour, "token_id", principal.token_id) >= PRESS_TOKEN_PER_HOUR:
            return _Way(denied="limit", limit_of="token"), sub
        return _Way(by="list", via="list"), sub
    ident = autopilot_mod.ident(session, session_key)
    free, via = None, "autopilot"
    if bot:
        free, via = autopilot_mod.session_free(conn, ident, BOT_SWITCH, project_id), BOT_SWITCH
    if free is None:
        free, via = autopilot_mod.session_free(conn, ident, SWITCH, project_id), "autopilot"
    if free is None:
        return _Way(), sub
    if bot and message_id is not None:
        hit = _press_guard(conn, channel_id, message_id, text)
        if hit is not None:
            return _Way(denied="guard", via=via, guard=hit), sub
    if _auto_count(conn, hour, "channel_id", channel_id) >= PRESS_PER_HOUR:
        return _Way(denied="limit", limit_of="channel"), sub
    if _auto_count(conn, hour, "autopilot_session", free.id) >= PRESS_SESSION_PER_HOUR:
        return _Way(denied="limit", limit_of="session"), sub
    if _auto_count(conn, now_ms - DAY_MS, "autopilot_session", free.id) >= PRESS_SESSION_PER_DAY:
        return _Way(denied="limit", limit_of="session_day"), sub
    return _Way(by="autopilot", autopilot_session=free.id, via=via), sub


class Created(NamedTuple):
    press: Press
    created: bool
    denied: str | None = None
    limit_of: str | None = None
    guard: str | None = None  # Ф63-2: категория страховки (`denied == "guard"`)


def create(
    conn: sqlite3.Connection,
    *,
    principal,
    message_id: int,
    channel_id: int,
    project_id: int | None,
    row: int,
    col: int,
    text: str,
    session,
    session_key: str | None,
    now_ms: int,
    client_name: str | None = None,
    request_id: str = "",
    waiting=None,
) -> Created:
    """4.2: нажатие кнопки `(message_id, row, col)` (ряд и место с 1). `channel_id` и `project_id` — беседа
    сообщения, видимость которой принципалу ручка уже проверила. `session` — модель с `pid`, `proc_start`,
    `session_id` (не словарь: `autosend._list_closed` читает `session.pid`).

    Отказы: 403 — bearer без скоупа `draft` или токен дежурной / оркестратора / рабочей сессии
    (`check_attach`); 404 — сообщения нет; 409 `stale_button` — кнопки на этом месте уже нет или её текст
    другой (П4); 422 — не Telegram, аккаунт не подключён, кнопка не callback или с паролем, bearer без
    `session`. Живая строка на эту кнопку (П2) — она же, `created=False`."""
    is_ui = principal.is_ui
    token_name = None
    if not is_ui:
        if not principal.has_scope("draft"):
            raise Denied("forbidden")
        found = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
        token_name = found[0] if found and isinstance(found[0], str) else None
        try:
            check_attach(name=token_name, kind=principal.kind, scopes=principal.scopes)
        except ValueError as exc:
            raise Denied("нажатие — только из интерактивной сессии (не дежурная и не оркестратор)") from exc
        if session is None:
            raise Invalid("session: сессия не определена")
    schema = db_of(channel_id)
    msg = conn.execute(
        f"SELECT m.ext_id, c.account_id, a.source, a.status FROM {schema}.message m "  # noqa: S608
        f"JOIN {schema}.channel c ON c.id = m.channel_id JOIN main.account a ON a.id = c.account_id "
        "WHERE m.id = ? AND m.channel_id = ? AND m.hidden = 0 AND m.purged = 0",
        (message_id, channel_id),
    ).fetchone()
    if msg is None:
        raise NotFound("сообщение не найдено")
    ext_id, account_id, source, account_status = msg
    if source not in SOURCES:
        raise Invalid("нажимать можно только кнопки под сообщениями Telegram")
    if account_status == "auth_required":
        raise Invalid("аккаунт Telegram не подключён — нажать нельзя")
    button = buttons.find(conn, message_id, row, col)
    if button is None or _loose(button.get("t")) != _loose(text):
        raise Conflict(f"{STALE}: клавиатура изменилась — перечитай сообщение")
    if button.get("k") != "callback":
        raise Invalid("эта кнопка не нажимается из хаба (ссылка, копирование или другой вид)")
    if button.get("pw") is True:
        raise Invalid("кнопка просит пароль — такие из хаба не нажимаются")
    if buttons.callback_data(button) is None:
        raise Invalid("у кнопки нет данных для нажатия")

    sweep(conn, now_ms)
    live = _select(
        conn,
        "p.message_id = ? AND p.row = ? AND p.col = ? AND p.status IN ('pending', 'approved', 'leased')",
        (message_id, row, col),
    )
    if live:
        return Created(live[0], False)

    way, sub = _Way(), None
    author = None
    if not is_ui:
        way, sub = _auto_way(
            conn,
            principal=principal,
            token_name=token_name,
            session=session,
            session_key=session_key,
            channel_id=channel_id,
            project_id=project_id,
            now_ms=now_ms,
            waiting=waiting,
            message_id=message_id,
            text=button["t"],
        )
        # Автор — для сигнала исхода карточки (как у черновика, Ф39): живая подписка сессии на проект.
        author = sub or subs_mod.author_sub(
            conn, token_id=principal.token_id, session=session, project_id=project_id
        )
    sent = is_ui or way.by is not None
    session_json = None
    if not is_ui:
        session_json = json.dumps(
            {
                "pid": session.pid,
                "proc_start": session.proc_start,
                "session_id": session.session_id,
                "sub_id": author.id if author is not None else None,
                "role": subs_mod.author_role(author) if author is not None else None,
            },
            ensure_ascii=False,
        )
    cur = conn.execute(
        "INSERT INTO main.button_press (account_id, channel_id, project_id, message_id, ext_id, row, col, "
        "text, data_b64, by, token_id, client_name, session_json, autopilot_session, auto, auto_by, status, "
        "created_ts, decided_ts, updated_ts, not_before, author_sub_id, author_project_id, author_role_key, "
        "author_role_no, seq, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?)",
        (
            account_id,
            channel_id,
            project_id,
            message_id,
            ext_id,
            row,
            col,
            button["t"],
            button["d"],
            "ui" if is_ui else "session",
            principal.token_id,
            client_name,
            session_json,
            way.autopilot_session,
            1 if way.by is not None else 0,
            way.by,
            "approved" if sent else "pending",
            now_ms,
            now_ms if sent else None,
            now_ms,
            _account_pause(conn, account_id, now_ms) if sent else 0,
            author.id if author is not None else None,
            author.project_id if author is not None else None,
            author.role_key if author is not None else None,
            author.role_no if author is not None else None,
            next_seq(conn),
            bot_guard.press_reason(way.guard) if way.guard is not None else None,
        ),
    )
    press = get(conn, cur.lastrowid)
    assert press is not None
    who = {
        "actor": "ui" if is_ui else "api",
        "request_id": request_id,
        "api_token_id": principal.token_id,
        "client_name": client_name,
    }
    detail = None
    if way.guard is not None:
        detail = {"guard": way.guard}
        bot_guard.audit_hit(
            conn,
            category=way.guard,
            channel_id=channel_id,
            path=way.via or "",
            now_ms=now_ms,
            press_id=press.id,
        )
    elif way.denied:
        detail = {"limit": way.limit_of}
    _audit(conn, press, action="press_create", result=press.status, now_ms=now_ms, detail=detail, **who)
    if way.by is not None:
        # Ф63-2 (П9): путь — `list` | `autopilot` (`hub_press`) | `bot_chat`; в строке `auto_by` у
        # `bot_chat` — `autopilot` (П8).
        _audit(
            conn,
            press,
            action="press_auto",
            result=way.via or way.by,
            now_ms=now_ms,
            detail={
                "sub_id": sub.id if sub is not None else None,
                "role": sub.role if sub is not None else None,
                "session_id": session.session_id,
                "hub_session_id": way.autopilot_session,
            },
            **who,
        )
    return Created(press, True, way.denied, way.limit_of, way.guard)


# --- П8: чтение -------------------------------------------------------------------------------------------


def _visible(principal, project_id: int | None) -> bool:
    if principal.project_ids is None:
        return True
    return project_id is not None and project_id in principal.project_ids


def _own(press: Press, triple: tuple | None) -> bool:
    """Строка — этой сессии: совпала тройка (pid, `starttime`, разговор) в `session_json`."""
    if triple is None or press.by != "session":
        return False
    sess = _session(press)
    return (sess.get("pid"), sess.get("proc_start"), sess.get("session_id")) == tuple(triple)


def readable(press: Press, principal, triple: tuple | None) -> bool:
    """П8: владельцу (кука) — всё; bearer — только нажатия своей тройки сессии в видимых проектах, сделанные этим
    же токеном (тройка не секрет; токены дежурной и оркестратора нажатий не создают — и не читают)."""
    if principal.is_ui:
        return True
    return (
        press.token_id is not None
        and press.token_id == principal.token_id
        and _visible(principal, press.project_id)
        and _own(press, triple)
    )


def list_presses(
    conn: sqlite3.Connection,
    principal,
    *,
    now_ms: int,
    triple: tuple | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """`GET /button-presses`: владельцу — все, bearer — свои (П8); по умолчанию — живые и изменившиеся за сутки,
    новые первыми; `status` — один статус. Ответ бота — в каждой выданной строке (читателю его можно)."""
    where = "p.status = ?" if status is not None else "(p.status IN ('pending', 'approved', 'leased') "
    params: list = [status] if status is not None else []
    if status is None:
        where += "OR p.updated_ts > ?)"
        params.append(now_ms - DAY_MS)
    rows = _select(conn, where + " ORDER BY p.id DESC", params)
    limit = max(1, min(int(limit), LIST_MAX))
    out = [press_dict(conn, p, result=True) for p in rows if readable(p, principal, triple)]
    return out[:limit]


def changes(conn: sqlite3.Connection, *, since: int, watermark: int, limit: int) -> list[dict]:
    """`/sync` и SSE — ТОЛЬКО UI-сессии (зовущий проверил): нажатия с `since < seq <= watermark` по
    возрастанию `seq`, с ответом бота."""
    rows = _select(conn, "p.seq > ? AND p.seq <= ? ORDER BY p.seq LIMIT ?", (since, watermark, limit))
    return [press_dict(conn, p, result=True) for p in rows]


# --- П6: решение владельца -------------------------------------------------------------------------------------


def decide(
    conn: sqlite3.Connection,
    press_id: int,
    action: str,
    *,
    now_ms: int,
    api_token_id: int | None = None,
    client_name: str | None = None,
    audit_request_id: str = "",
) -> Press:
    """«Нажать» / «Нет» по карточке — только из `pending`, иначе 409. `approve` — срок годности решения
    пошёл (`decided_ts`). Аудит `press_decide` (actor `ui`)."""
    sweep(conn, now_ms)
    press = get(conn, press_id)
    if press is None:
        raise NotFound(f"нажатие {press_id} не найдено")
    if action not in ("approve", "decline"):
        raise Invalid(f"action: approve или decline, получено {action!r}")
    if press.status != "pending":
        raise Conflict(f"решить можно только ждущее нажатие (сейчас {press.status})")
    if action == "approve":
        _update(
            conn,
            press_id,
            now_ms,
            status="approved",
            decided_ts=now_ms,
            attempts=0,
            not_before=_account_pause(conn, press.account_id, now_ms),
        )
    else:
        _update(conn, press_id, now_ms, status="declined", decided_ts=now_ms, done_ts=now_ms)
    press = get(conn, press_id)
    assert press is not None
    _audit(
        conn,
        press,
        action="press_decide",
        result=press.status,
        now_ms=now_ms,
        actor="ui",
        request_id=audit_request_id,
        api_token_id=api_token_id,
        client_name=client_name,
    )
    return press


# --- 4.4 + П3: очередь коннектору -------------------------------------------------------------------------


def _account_ok(conn: sqlite3.Connection, account_id: int) -> bool:
    row = conn.execute("SELECT status, source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row is not None and row[0] not in _BLOCKED_STATUSES and row[1] in SOURCES


def _auto_holds(conn: sqlite3.Connection, press: Press, project_id: int | None) -> bool:
    """П3: условие «сразу» авто-строки выполняется СЕЙЧАС (к выдаче коннектору). `project_id` — проект
    беседы сейчас.

    - `list`: подписка сессии та же и жива (не завершена, процесс жив), у неё роль, её проект — проект
      беседы, пара (беседа, роль) по-прежнему в списке владельца и список не закрыт для сессии
      (`autosend.list_allows` — та же функция, что у черновика);
    - `autopilot`: сессия хаба не закрыта, в авто, её проект — проект беседы, выключатель `hub_press` снят
      (как `join._autopilot_allowed`) ИЛИ (Ф63-2 П8) снят `bot_chat` и беседа — с ботом.

    Ф63-2 (П4, П8): в беседе с ботом — ещё и страховка повторно (текст кнопки и сообщение с ней сейчас)."""
    if project_id is None:
        return False
    bot = bot_guard.is_bot_chat(conn, press.channel_id)
    if bot and _press_guard(conn, press.channel_id, press.message_id, press.text) is not None:
        return False
    if press.auto_by == "autopilot":
        rec = held_mod.get_open(conn, press.autopilot_session)
        if rec is None or rec.mode != "auto" or rec.project_id != project_id:
            return False
        if autopilot_mod.switch_free(conn, SWITCH, project_id):
            return True
        return bot and autopilot_mod.switch_free(conn, BOT_SWITCH, project_id)
    if press.auto_by == "list":
        sess = _session(press)
        pid, proc_start, sub_id = sess.get("pid"), sess.get("proc_start"), sess.get("sub_id")
        if not isinstance(pid, int) or not isinstance(proc_start, int) or not isinstance(sub_id, int):
            return False
        sub = subs_mod.get_sub(conn, sub_id)
        if sub is None or sub.ended_ts is not None or not sub.role_key or sub.project_id != project_id:
            return False
        if (sub.session_pid, sub.session_proc_start) != (pid, proc_start):
            return False
        if not subs_mod.rec_alive(sub):
            return False
        return autosend.list_allows(
            conn, channel_id=press.channel_id, role_key=sub.role_key, session=SimpleNamespace(pid=pid)
        )
    return False


def _prepare(conn: sqlite3.Connection, press: Press, now_ms: int) -> dict | None:
    """Последняя сверка перед выдачей (4.4, П3). Возвращает задание коннектору или `None` — строка закрыта
    (`failed`, `stale_button`) либо стала карточкой (`pending`)."""
    chan = conn.execute(
        f"SELECT ext_id, project_id FROM {db_of(press.channel_id)}.channel WHERE id = ?",  # noqa: S608
        (press.channel_id,),
    ).fetchone()
    button = buttons.find(conn, press.message_id, press.row, press.col)
    same = (
        chan is not None
        and button is not None
        and press.data_b64 is not None
        and button.get("t") == press.text
        and button.get("d") == press.data_b64
        and buttons.callback_data(button) is not None
    )
    if not same:
        _update(conn, press.id, now_ms, status="failed", reason=STALE, done_ts=now_ms)
        return None
    if press.auto and not _auto_holds(conn, press, chan[1]):
        # Условие «сразу» больше не выполняется (владелец убрал беседу из списка, поставил выключатель, сессия
        # закрыта; Ф63-2 — сработала страховка) — решает владелец: карточка.
        hit = _press_guard(conn, press.channel_id, press.message_id, press.text)
        sets: dict = {"reason": bot_guard.press_reason(hit)} if hit is not None else {}
        _update(conn, press.id, now_ms, status="pending", auto=0, decided_ts=None, not_before=0, **sets)
        demoted = get(conn, press.id)
        assert demoted is not None
        _audit(conn, demoted, action="press_auto", result="revoked", now_ms=now_ms)
        if hit is not None:
            bot_guard.audit_hit(
                conn,
                category=hit,
                channel_id=press.channel_id,
                path="lease",
                now_ms=now_ms,
                press_id=press.id,
            )
        return None
    gen = press.lease_gen + 1
    _update(
        conn,
        press.id,
        now_ms,
        status="leased",
        lease_gen=gen,
        lease_until=now_ms + LEASE_MS,
        attempts=press.attempts + 1,
    )
    return {
        "id": press.id,
        "lease_gen": gen,
        "channel_ext_id": chan[0],
        "message_ext_id": press.ext_id,
        "data_b64": press.data_b64,
    }


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """Задания своего аккаунта `{id, lease_gen, channel_ext_id, message_ext_id, data_b64}` — старшие первыми;
    лиз `LEASE_MS`. Перед выдачей — `sweep` (сроки) и `_prepare` (кнопка та же, условие «сразу» в силе).
    Аккаунт `limited` / `auth_required` — пусто (решения истекут по сроку)."""
    sweep(conn, now_ms)
    if not _account_ok(conn, account_id):
        return []
    limit = max(1, min(int(limit), 10))
    items: list[dict] = []
    for press in _select(
        conn,
        "p.account_id = ? AND p.status = 'approved' AND p.not_before <= ? ORDER BY p.id",
        (account_id, now_ms),
    ):
        if len(items) >= limit:
            break
        item = _prepare(conn, press, now_ms)
        if item is not None:
            items.append(item)
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait`: `(готово, ближайший срок в мс)`. «Готово» — `lease` что-то сделает: выдаст
    строку или закроет просроченную (П3: срок переводит шаг `lease` / `wait`). Срок — ближайшее из: конец
    паузы, конец годности решения, конец лиза."""
    if not _account_ok(conn, account_id):
        return False, None
    dues: list[int] = []
    for press in _select(conn, "p.account_id = ? AND p.status IN ('approved', 'leased')", (account_id,)):
        if overdue(press, now_ms):
            return True, None
        if press.status == "leased":
            dues.append(press.lease_until or now_ms)
        elif press.not_before <= now_ms:
            return True, None
        else:
            dues.append(min(press.not_before, _deadline(press)))
    return False, (min(dues) if dues else None)


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def clean_result(result: object) -> dict:
    """Ответ бота из отчёта коннектора -> `result_json`: `{"message"?: <= 500, "alert"?: true,
    "url"?: <= 1024}` или `{"timeout": true}` (нажатие доставлено, бот на запрос не ответил). Пустые поля
    опускаются; адрес длиннее предела не режется, а выбрасывается (обрезанная ссылка — другая ссылка)."""
    if not isinstance(result, dict):
        return {}
    if result.get("timeout") is True:
        return {"timeout": True}
    out: dict = {}
    message = _clip(result.get("message"), RESULT_MESSAGE_MAX)
    if message:
        out["message"] = message
    if result.get("alert") is True:
        out["alert"] = True
    url = result.get("url")
    if isinstance(url, str) and 0 < len(url) <= RESULT_URL_MAX:
        out["url"] = url
    return out


def report(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    press_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    result: object = None,
    reason: str | None = None,
    retry_after_ms: int | None = None,
) -> str:
    """Отчёт коннектора; возвращает новый статус строки.

    - `done` — запрос дошёл: `result` — ответ бота (`clean_result`);
    - `failed` — запрос отклонён (`reason`: `stale_button`, класс ошибки);
    - `unknown` — запрос мог уйти, исход неизвестен: без повтора;
    - `retry` — запрос НЕ ушёл (FloodWait — `reason='flood'`, нет связи): строка возвращается в `approved` с
      паузой, если успевает до конца срока годности решения, иначе `failed` (П3). FloodWait ставит паузу
      всем ждущим строкам аккаунта.

    Чужой аккаунт или нет строки — `NotFound`; не тот `lease_gen` — `StaleLease`; строка не `leased` —
    `Conflict`, кроме `unknown` по истёкшему лизу: поздний отчёт с верным `lease_gen` принимается как
    уточнение (П3)."""
    if outcome not in REPORT_OUTCOMES:
        raise Invalid(f"outcome: {sorted(REPORT_OUTCOMES)}")
    press = get(conn, press_id)
    if press is None or press.account_id != account_id:
        raise NotFound(f"нажатие {press_id} не найдено")
    if press.lease_gen != lease_gen:
        raise StaleLease("lease_gen устарел")
    late = press.status == "unknown" and press.reason == "lease_expired"
    if press.status != "leased" and not late:
        raise Conflict(f"нажатие не выдано (сейчас {press.status})")
    why = _clip(reason, REASON_MAX)
    if outcome == "done":
        sets: dict = {
            "status": "done",
            "reason": None,
            "result_json": json.dumps(clean_result(result), ensure_ascii=False, sort_keys=True),
        }
    elif outcome == "failed":
        sets = {"status": "failed", "reason": why or "error"}
    elif outcome == "unknown":
        sets = {"status": "unknown", "reason": why or "unknown"}
    else:
        flood = why == "flood"
        until = now_ms + (retry_after_ms if retry_after_ms is not None else RETRY_MS)
        if flood:
            # Пауза аккаунта: ждущие решения дальше этого срока не выдаются (и закроются `sweep`, если не
            # успевают); новая строка узнаёт паузу по `_account_pause`.
            conn.execute(
                "UPDATE main.button_press SET not_before = ? WHERE account_id = ? AND status = 'approved' "
                "AND not_before < ?",
                (until, account_id, until),
            )
        if late or until >= _deadline(press):
            sets = {"status": "failed", "reason": "flood" if flood else "not_sent", "not_before": until}
        else:
            _update(conn, press_id, now_ms, status="approved", lease_until=None, not_before=until)
            return "approved"
    _update(conn, press_id, now_ms, done_ts=now_ms, lease_until=None, **sets)
    return sets["status"]


# --- чистка (4.1) -----------------------------------------------------------------------------------------


def cleanup(conn: sqlite3.Connection, *, now_ms: int) -> dict:
    """Часовой проход: сроки (`sweep`); карточка старше суток -> `expired`; строки старше 30 дней — долой;
    у строк, чьё сообщение стёрто, скрыто или пропало, обнуляются `text`, `data_b64`, `result_json` (живая
    такая строка закрывается `failed`, `stale_button`)."""
    swept = sweep(conn, now_ms)
    expired = 0
    for press in _select(conn, "p.status = 'pending' AND p.created_ts <= ?", (now_ms - PENDING_TTL_MS,)):
        _update(conn, press.id, now_ms, status="expired", reason="not_decided", done_ts=now_ms)
        expired += 1
    removed = conn.execute(
        "DELETE FROM main.button_press WHERE created_ts <= ? AND status NOT IN ('pending', 'approved', "
        "'leased')",
        (now_ms - KEEP_MS,),
    ).rowcount
    scrubbed = 0
    for press in _select(
        conn, "p.text IS NOT NULL OR p.data_b64 IS NOT NULL OR p.result_json IS NOT NULL", ()
    ):
        alive = conn.execute(
            f"SELECT 1 FROM {db_of(press.message_id)}.message WHERE id = ? AND hidden = 0 AND purged = 0",  # noqa: S608
            (press.message_id,),
        ).fetchone()
        if alive is not None:
            continue
        sets = {"text": None, "data_b64": None, "result_json": None}
        if press.status in ("pending", "approved"):
            sets |= {"status": "failed", "reason": STALE, "done_ts": now_ms}
        _update(conn, press.id, now_ms, **sets)
        scrubbed += 1
    return {"swept": swept, "expired": expired, "removed": removed, "scrubbed": scrubbed}


# --- исход карточки — автору сигналом (4.5, П8) -----------------------------------------------------------


def _signal_pred(sub, now_ms: int) -> tuple[str, list]:
    """ЕДИНСТВЕННОЕ условие «исход нажатия пора отдать подписке `sub`» — и для счёта, и для выдачи (образец —
    `draft_outcomes._signal_pred`, Ф39 П3–П4): нажатие сессии, шедшее карточкой (`auto = 0`), в конечном
    статусе, не отдано, не старше `OUTCOME_TTL_MS`, адресовано ей — сама подписка автора ИЛИ её линия
    (проект, роль, номер), когда подписки автора уже нет; вне проектов — ещё и та же папка сессии."""
    where = (
        "p.by = 'session' AND p.auto = 0 AND p.signaled_ts IS NULL AND p.done_ts > ? "
        "AND p.status IN ('done', 'failed', 'unknown', 'declined', 'expired') AND (p.author_sub_id = ?"
    )
    params: list = [now_ms - OUTCOME_TTL_MS, sub.id]
    if sub.role_key is not None:
        where += (
            " OR (p.author_project_id = ? AND p.author_role_key = ? AND p.author_role_no = ? AND NOT EXISTS "
            "(SELECT 1 FROM main.session_sub live WHERE live.id = p.author_sub_id AND live.ended_ts IS NULL)"
        )
        params += [sub.project_id, sub.role_key, sub.role_no]
        if sub.project_id == subs_mod.NO_PROJECT_ID:
            where += (
                " AND EXISTS (SELECT 1 FROM main.session_sub a WHERE a.id = p.author_sub_id "
                "AND a.session_cwd = ?)"
            )
            params.append(sub.session_cwd)
        where += ")"
    return where + ")", params


def signal_count(conn: sqlite3.Connection, sub, *, now_ms: int) -> int:
    """Читатель, для ожидания: исходы нажатий, которые пора отдать подписке."""
    where, params = _signal_pred(sub, now_ms)
    return conn.execute(
        f"SELECT COUNT(*) FROM main.button_press p WHERE {where}",  # noqa: S608 — фрагмент задан кодом
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
    """Отдать подписке исходы её нажатий — `{"kind": "press", "press_id", "channel_id", "outcome"}`: только
    числа и постоянное слово (статус); ответа бота и текста кнопки в сигнале нет (П8). Сигнал отдаётся один
    раз (`signaled_ts`) и не двигает `seq` строки. Не влезшие в `limit` ждут следующей выдачи."""
    where, params = _signal_pred(sub, now_ms)
    rows = conn.execute(
        f"SELECT p.id, p.channel_id, p.status FROM main.button_press p WHERE {where} ORDER BY p.id LIMIT ?",  # noqa: S608
        [*params, max(0, limit)],
    ).fetchall()
    signals: list[dict] = []
    for press_id, channel_id, status in rows:
        conn.execute(
            "UPDATE main.button_press SET signal_sub_id = ?, signaled_ts = ? WHERE id = ?",
            (sub.id, now_ms, press_id),
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
            channel_id=channel_id,
            detail={"sub_id": sub.id, "kind": "press", "press_id": press_id, "outcome": status},
        )
        signals.append({"kind": "press", "press_id": press_id, "channel_id": channel_id, "outcome": status})
    return signals
