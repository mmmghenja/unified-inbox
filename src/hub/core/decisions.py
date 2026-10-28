"""Журнал решений по карточкам и образцы решений владельца (Ф76-1, docs/architecture.md П1–П16 главнее разделов 2–3а).

- Журнал (`decision_log`, 0106_shared.sql): строка на закрытие карточки-решения (`DECISION_KINDS`) — одна
  функция записи `record(conn, ref, by, action, text)` на всех путях (П5): маршруты интерфейса, ответ из
  канала владельца (`owner_queue.answer`), ход сессии (`owner_queue.close_turns`), «сразу да»
  (`card_route.record`),
  снятая галочка (`held_work.auto_request`). Пишется в файл карточки (запрос, черновик — по `db_of(id)`;
  прочее — `agent_runs.schema_for_project`). Не решения (П6) — `later`, `superseded`, `closed`, `expired`;
  состояния сессии, напоминания, сообщения агентов — не карточки-решения, строк не дают.
- Кто решил (`decided_by`): учебные — `LEARN_BY` (П1); прочие видны, но образцом не служат.
- Отпечаток и класс — кодом (`decision_print`, П3, П10). Тексты — маскированные ядром (П13).
- Образцов не храним (П9): `patterns_for` считает их запросом в момент карточки (одинаковый отпечаток, одно
  решение владельца не меньше `PATTERN_MIN` раз за `WINDOW_MS`, противоположных нет). Хранятся только правки владельца
  (`edits_of` / `set_edits`, П14): «оставить» / «убрать» / «всегда меня» и правила текстом — по проекту
  (`project.agent_policy_json["patterns"]`) и общие (`main.meta`, ключ `GENERAL_META`).
- «Всегда меня» (П7) — кодом: `always_me_card` / `always_me_print`; маршрут такой карточки — `me`.
- Первая загрузка из истории (П4) — `backfill`, один раз на старте ядра (флаг `BACKFILL_KEY`), идемпотентно
  (ключ `source_key` = `ref`).
- Внешняя история (П11–П13) — `ext_prepare` (вне потока-писателя: гард может звать git) и `ext_store`.
- Решения агента для раздела «Мой агент» (Ф76-4а) — `agent_decisions`; «неверно» владельца — `mark_wrong`: строка
  выходит из счёта образцов и «похожих», ключ образца из основания уходит в «убрать».

Запись — только внутри `Db.write()`, время — параметром `now_ms`."""

# ruff: noqa: S608 — схема в SQL только из `STORES` / `db_of` / `schema_for_project`, не из ввода

import contextlib
import contextvars
import dataclasses
import json
import logging
import os
import re
import time

from . import decision_print as dp
from .audit import write_audit
from .db import db_of, next_seq

_log = logging.getLogger("hub.decisions")

BY_UI, BY_BOT, BY_WORD, BY_EXT = "owner_ui", "owner_bot", "agent_on_word", "owner_ext"
BY_AGENT, BY_AUTO, BY_AUTOPILOT, BY_DELEGATE = "agent", "auto", "autopilot", "delegate"
BY = (BY_UI, BY_BOT, BY_WORD, BY_EXT, BY_AGENT, BY_AUTO, BY_AUTOPILOT, BY_DELEGATE)
LEARN_BY = (BY_UI, BY_BOT, BY_WORD, BY_EXT)  # П1: учебные строки
_LEARN_SQL = "(" + ", ".join(f"'{b}'" for b in LEARN_BY) + ")"

# Карточки-решения (остальные виды очереди — состояния и сведения: строк журнала нет).
DECISION_KINDS = frozenset(
    {"permission", "question", "turn", "human", "draft", "press", "join", "leave", "group_op", "rule",
     "agent_request"}
)  # fmt: skip
NOT_DECISIONS = frozenset({"later", "superseded", "closed", "expired"})  # П6
_YES_ACTIONS = frozenset({"allow", "approve"})
_NO_ACTIONS = frozenset({"deny", "cancel", "decline"})

WINDOW_MS = 30 * 24 * 60 * 60 * 1000
PATTERN_MIN = 3
SIMILAR_MAX = 5
ASK_MAX = 2000
ANSWER_MAX = 2000
RULES_MAX = 8000
KEYS_MAX = 200
KEY_MAX = 300
# Ф76-3: основание ответа агента владельца — `rule:<…>`, `pattern:<отпечаток/класс>` (образец),
# `similar:<дата>` (похожее решение), `card` (ответ очевиден из карточки), `none` (основания нет).
BASIS_MAX = 200
_BASIS_RE = re.compile(r"^(?:(?:rule|pattern|similar):\S.*|card|none)$", re.S)
STORES = ("main", "work")

EDITS_KEY = "patterns"  # в `project.agent_policy_json`
GENERAL_META = "patterns_general"  # общие правки владельца («все проекты»), `main.meta`
GENERAL_ID = 0  # номер «все проекты» в ручках
EDIT_LISTS = ("keep", "drop", "always_me")
BACKFILL_KEY = "decisions_backfill_v1"

EXT_ROWS_MAX = 200
EXT_KINDS = ("permission", "turn")
_EXT_SKIP_PARTS = (f"{os.sep}.runs{os.sep}",)
_EXT_SKIP_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")  # noqa: S108 — сверка путей, файлов здесь нет


class DecisionError(Exception):
    """Отказ ручек правок и приёма: `status` — HTTP-код, `detail` — готовый текст."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _mask(text) -> str:
    from ..work import session as work_session  # код хоста, только stdlib (как `owner_queue._mask`)

    return work_session.mask_secrets(str(text))


def _clip(text, limit: int) -> str | None:
    if text is None:
        return None
    text = _mask(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def check_basis(value) -> str | None:
    """Ф76-3: основание ответа агента (одна строка до `BASIS_MAX`, маскируется) или `None`; иначе 422."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecisionError(422, "basis: строка")
    value = " ".join(value.split())
    if not value or len(value) > BASIS_MAX or not _BASIS_RE.match(value):
        raise DecisionError(
            422, f"basis: rule:<…> | pattern:<…> | similar:<дата> | card | none, до {BASIS_MAX} знаков"
        )
    return _clip(value, BASIS_MAX)


def basis_checked(conn, ref: str, basis: str | None, project_id: int | None, *, now_ms: int) -> str | None:
    """Ревью Ф76: основание агента по существу (форма уже проверена `check_basis`) — до записи в журнал.
    `pattern:<ключ>` — только если ключ среди образцов карточки (`patterns_for`) или в «оставить» (кроме
    ключа без класса); `rule:` — только если у проекта или в общих правилах есть текст; `similar:` — только
    если у карточки есть похожие решения (`similar`). Не прошло — `none` (ответ агента принимается)."""
    if basis is None or not basis.startswith(("pattern:", "rule:", "similar:")):
        return basis
    if basis.startswith("rule:"):
        texts = [(edits_of(conn, GENERAL_ID) or _empty_edits())["rules"]]
        if project_id:
            texts.append((edits_of(conn, project_id) or _empty_edits())["rules"])
        return basis if any(isinstance(t, str) and t.strip() for t in texts) else "none"
    card = describe(conn, ref, now_ms=now_ms)
    if card is None:
        return "none"
    if basis.startswith("similar:"):
        return basis if similar(conn, card) else "none"
    key = pattern_key_of(basis)
    if key is None:
        return "none"
    if any(p["key"] == key for p in patterns_for(conn, card, now_ms=now_ms)):
        return basis
    kept = card.project_id and key in _merged_edits(conn, card.project_id)["keep"]
    return basis if kept and not dp.classless(key) else "none"


def by_of_source(source: str) -> str:
    """Источник ответа очереди (`ui` / `agent` / `system` / `auto`) -> `decided_by`."""
    return {"ui": BY_UI, "agent": BY_AGENT}.get(source, BY_AUTO)


# --- карточка: что решали -----------------------------------------------------------------------------------


@dataclasses.dataclass
class Card:
    ref: str
    kind: str
    project_id: int | None
    session_id: int | None
    role: str | None
    engine: str | None
    fingerprint: str
    ask_class: str | None
    rule: str | None
    ask: str | None
    card_ts: int | None
    options: tuple[str, ...]
    schema: str


def _schema_for_project(conn, project_id) -> str:
    from . import agent_runs

    return agent_runs.schema_for_project(conn, project_id) if project_id else "main"


def _ask_of_input(tool, tool_input) -> str:
    if not isinstance(tool_input, dict):
        return tool or ""
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(tool_input.get(key), str):
            return f"{tool}: {tool_input[key]}"
    return f"{tool}: {json.dumps(tool_input, ensure_ascii=False)[:ASK_MAX]}"


def _questions(tool_input) -> tuple[str, tuple[str, ...]]:
    """(текст вопросов, варианты единственного вопроса)."""
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    items = (
        [q for q in raw if isinstance(q, dict) and isinstance(q.get("question"), str)]
        if isinstance(raw, list)
        else []
    )
    text = "; ".join(q["question"] for q in items)
    options: tuple[str, ...] = ()
    if len(items) == 1 and isinstance(items[0].get("options"), list):
        options = tuple(
            o["label"] for o in items[0]["options"] if isinstance(o, dict) and isinstance(o.get("label"), str)
        )
    return text, options


def _tool_print(kind: str, tool, tool_input, rule) -> tuple[str, str | None, str, tuple[str, ...]]:
    """(отпечаток, класс, текст вопроса, варианты) разрешения или вопроса сессии."""
    if kind == "question":
        text, options = _questions(tool_input)
        # Ревью Ф76: слова вопроса в отпечатке — под маской секретов.
        fingerprint, cls = dp.question_print(_mask(text))
        return fingerprint, cls, text, options
    fingerprint, _cmd = dp.permission_print(tool, tool_input, rule)
    return fingerprint, None, _ask_of_input(tool, tool_input), ()


def _draft_kind(conn, schema: str, channel_id: int, send_at, cancel_if_reply) -> str:
    """Вид письма для отпечатка черновика: догон (`send_at` / «отменить, если ответят»), первое (беседа-
    заготовка или ни одного входящего), ответ."""
    if send_at is not None or cancel_if_reply:
        return "followup"
    ext = conn.execute(f"SELECT ext_id FROM {schema}.channel WHERE id = ?", (channel_id,)).fetchone()
    if ext is not None and isinstance(ext[0], str) and ext[0].startswith("new:"):
        return "first"
    inbound = conn.execute(
        f"SELECT 1 FROM {schema}.message WHERE channel_id = ? AND is_me = 0 LIMIT 1",
        (channel_id,),
    ).fetchone()
    return "reply" if inbound is not None else "first"


def describe(conn, ref: str, *, now_ms: int = 0) -> Card | None:
    """Карточка-решение по `ref` (и решённая) — для журнала, образцов и «всегда меня»; нет такой или вид не
    решение — `None`."""
    from . import owner_queue as zq  # noqa: PLC0415 — очередь опирается на этот модуль

    try:
        prefix, number = zq.parse_ref(ref)
    except zq.OwnerError:
        return None
    loc = zq._locate(conn, prefix, number, now_ms, ref)  # noqa: SLF001 — тот же поиск, что у ответа
    if loc is None or loc.kind not in DECISION_KINDS:
        return None
    kind = loc.kind
    role = engine = rule = ask = None
    cls = None
    options: tuple[str, ...] = ()
    card_ts = None
    schema = None
    store = None
    if loc.hub_id is not None:
        row = conn.execute(
            "SELECT role, engine, store FROM main.hub_session WHERE id = ?", (loc.hub_id,)
        ).fetchone()
        if row is not None:
            role, engine, store = row
    if prefix == "req":
        schema = db_of(number)
        row = conn.execute(
            f"SELECT kind, tool, input_json, created_ts, rule FROM {schema}.held_request WHERE id = ?",
            (number,),
        ).fetchone()
        if row is None:
            return None
        sub_kind, tool, raw, card_ts, rule = row
        fingerprint, cls, ask, options = _tool_print(sub_kind, tool, _json(raw) or {}, rule)
    elif kind == "turn":
        row = conn.execute(
            "SELECT payload_json, created_ts FROM main.owner_item WHERE id = ?", (loc.number,)
        ).fetchone()
        if row is None:
            return None
        payload, card_ts = _json(row[0]), row[1]
        brief, full = payload.get("brief"), payload.get("full")
        ask = brief if payload.get("brief_kind") == "owner_channel" and isinstance(brief, str) else None
        if ask is None and isinstance(full, str):
            ask = full[-500:]
        fingerprint, cls = dp.turn_print(ask)
        schema = store or _schema_for_project(conn, loc.project_id)
    elif kind == "draft":
        schema = db_of(number)
        row = conn.execute(
            f"SELECT channel_id, COALESCE(draft_text, text), send_at_ts, cancel_if_reply, created_ts, "
            f"author_role FROM {schema}.outbox WHERE id = ?",
            (number,),
        ).fetchone()
        if row is None:
            return None
        channel_id, ask, send_at, cancel_if_reply, card_ts, author_role = row
        role = role or author_role
        fingerprint = f"draft|{channel_id}|{_draft_kind(conn, schema, channel_id, send_at, cancel_if_reply)}"
    else:
        sql = {
            "press": "SELECT channel_id, text, created_ts FROM main.button_press WHERE id = ?",
            "join": "SELECT target, reason, created_ts FROM main.join_request WHERE id = ?",
            "leave": "SELECT channel_id, title, created_ts FROM main.leave_request WHERE id = ?",
            "group_op": "SELECT kind, title, created_ts FROM main.group_op WHERE id = ?",
            "rule": "SELECT kind, reason, created_ts FROM main.rule_request WHERE id = ?",
            "agent_request": "SELECT kind, reason, created_ts FROM main.agent_request WHERE id = ?",
            "human": "SELECT kind, need, created_ts FROM main.human_request WHERE id = ?",
        }[kind]
        row = conn.execute(sql, (number,)).fetchone()
        if row is None:
            return None
        target, ask, card_ts = row
        if kind == "press":
            target = f"{target}|{dp.ask_words(_mask(ask) if ask is not None else ask)}"
        fingerprint = f"{kind}|{target}"
    if schema is None:
        schema = _schema_for_project(conn, loc.project_id)
    return Card(
        ref=ref,
        kind=kind,
        project_id=loc.project_id,
        session_id=loc.hub_id,
        role=role,
        engine=engine,
        fingerprint=fingerprint,
        ask_class=cls,
        rule=rule,
        ask=_clip(ask, ASK_MAX),
        card_ts=card_ts,
        options=options,
        schema=schema if schema in STORES else "main",
    )


# --- запись -------------------------------------------------------------------------------------------------


def verdict_of(card: Card, action: str, text, answers) -> str | None:
    """П3: дискретное решение. Разрешения, заявки, черновики — по действию; вопрос с вариантами — выбранный
    вариант; ход сессии — короткий ответ по словарю; `ok` хода — нейтрально (`None`); «нужен человек» —
    исход, а не решение (`None`)."""
    if card.kind == "human":
        return None
    if card.kind == "turn":
        return dp.verdict_of_text(text) if action == "reply" else None
    if action in _NO_ACTIONS:
        return dp.NO
    if action == "answer":
        if not card.options or not isinstance(answers, dict) or len(answers) != 1:
            return None
        chosen = next(iter(answers.values()))
        known = {o.casefold(): o for o in card.options}
        return known.get(chosen.casefold().strip()) if isinstance(chosen, str) else None
    if action in _YES_ACTIONS:
        return dp.YES
    return None


@dataclasses.dataclass
class _Acting:
    by: str
    text: str | None
    word: dict | None
    refs: set
    basis: str | None = None


_ACTING: contextvars.ContextVar[_Acting | None] = contextvars.ContextVar("decisions_acting", default=None)


@contextlib.contextmanager
def acting(by: str, text: str | None = None, word: dict | None = None, basis: str | None = None):
    """Ответ из канала владельца: строки, которые запишет вложенный путь (`close_turns` при реплике сессии), — от
    имени этого ответа (`by`, слово владельца, основание агента — Ф76-3); `refs` — записанные под ним карточки."""
    act = _Acting(by, text, word, set(), basis)
    token = _ACTING.set(act)
    try:
        yield act
    finally:
        _ACTING.reset(token)


def _answer_text(text, answers) -> str | None:
    if isinstance(answers, dict) and answers:
        joined = "; ".join(str(v) for v in answers.values())
        text = f"{text}\n{joined}" if text else joined
    return _clip(text, ANSWER_MAX) if text else None


def _insert(
    conn,
    card: Card,
    *,
    by: str,
    action: str,
    text,
    answers,
    word: dict | None,
    ts: int,
    source_key: str | None,
    basis: str | None = None,
) -> int | None:
    verdict = verdict_of(card, action, text, answers)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {card.schema}.decision_log (ts, ref, source_key, project_id, "
        "session_id, role, engine, kind, ask_class, rule, fingerprint, action, verdict, ask_text, "
        "answer_text, decided_by, word_json, card_ts, basis) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            card.ref,
            source_key,
            card.project_id,
            card.session_id,
            card.role,
            card.engine,
            card.kind,
            card.ask_class,
            card.rule,
            card.fingerprint,
            action,
            verdict,
            card.ask,
            _answer_text(text, answers),
            by,
            json.dumps(word, ensure_ascii=False) if word else None,
            card.card_ts,
            basis,
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def record(
    conn,
    ref: str,
    by: str,
    action: str,
    text: str | None = None,
    *,
    now_ms: int,
    answers: dict | None = None,
    word: dict | None = None,
    nested: bool = False,
    basis: str | None = None,
) -> int | None:
    """П5: строка журнала о закрытии карточки `ref` — в той же транзакции, что решение (зовущий решил её
    перед этим). `by` — `BY`; `action` — действие ответа (`allow`, `deny`, `answer`, `approve`, `cancel`,
    `decline`, `go`, `reply`, `ok`, `done`, `failed`); `text` — текст владельца (правка черновика, отказ,
    реплика; у `agent_on_word` — его слово). Не решение (П6) или вид не решение — ничего; повтор по той же
    карточке — ничего (`source_key`). `nested` — запись вложенного пути (`close_turns`): внутри `acting`
    берёт `by`, текст, слово и основание ответа канала. `basis` (Ф76-3) — основание ответа агента
    (`check_basis`). Возвращает id строки или `None`."""
    act = _ACTING.get()
    if nested and act is not None:
        by, word, basis = act.by, act.word, act.basis
        text = act.text if act.text is not None else text
    if by not in BY:
        raise ValueError(f"decisions.record: by {by!r}")
    if action in NOT_DECISIONS:
        return None
    card = describe(conn, ref, now_ms=now_ms)
    if card is None:
        return None
    if nested and act is not None:
        act.refs.add(ref)
    return _insert(
        conn,
        card,
        by=by,
        action=action,
        text=text,
        answers=answers,
        word=word,
        ts=now_ms,
        source_key=ref,
        basis=basis,
    )


# --- первая загрузка из истории (П4) ------------------------------------------------------------------------


def _backfill_requests(conn, now_ms: int) -> int:
    done = 0
    for schema in STORES:
        rows = conn.execute(
            f"SELECT id, status, answer_json, decided_ts FROM {schema}.held_request "
            "WHERE status IN ('allowed', 'denied', 'answered') ORDER BY id"
        ).fetchall()
        for request_id, status, raw, decided_ts in rows:
            ref = f"req:{request_id}"
            answer = _json(raw)
            closer = conn.execute(
                "SELECT closed_by FROM main.owner_item WHERE ref = ? AND closed_by IN ('agent', 'system') "
                "ORDER BY id DESC LIMIT 1",
                (ref,),
            ).fetchone()
            who = answer.get("by")
            if isinstance(who, dict) and who.get("auto") is True:
                by = BY_AUTOPILOT
            elif who is not None:
                by = BY_DELEGATE  # Ф49 (до Ф66-1): `{role, role_no, sub}`
            elif closer is not None:
                by = BY_AGENT if closer[0] == "agent" else BY_AUTO
            else:
                by = BY_UI
            action = {"allowed": "allow", "denied": "deny", "answered": "answer"}[status]
            card = _backfill_describe(conn, ref, now_ms)
            if card is None:
                continue
            answers = answer.get("answers") if isinstance(answer.get("answers"), dict) else None
            done += bool(
                _insert(
                    conn,
                    card,
                    by=by,
                    action=action,
                    text=answer.get("message") if isinstance(answer.get("message"), str) else None,
                    answers=answers,
                    word=None,
                    ts=decided_ts or card.card_ts or now_ms,
                    source_key=ref,
                )
            )
    return done


def say_text(conn, say_id) -> str | None:
    if not isinstance(say_id, int) or isinstance(say_id, bool):
        return None
    row = conn.execute(
        f"SELECT text FROM {db_of(say_id)}.held_say WHERE id = ?",
        (say_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _backfill_turns(conn, now_ms: int) -> int:
    done = 0
    rows = conn.execute(
        "SELECT ref, closed_ts, closed_by, payload_json FROM main.owner_item WHERE kind = 'turn' "
        "AND closed_by IN ('ui', 'agent') ORDER BY id"
    ).fetchall()
    for ref, closed_ts, closed_by, raw in rows:
        payload = _json(raw)
        action = payload.get("action")
        if action not in ("reply", "ok"):
            continue
        card = _backfill_describe(conn, ref, now_ms)
        if card is None:
            continue
        done += bool(
            _insert(
                conn,
                card,
                by=by_of_source(closed_by),
                action=action,
                text=say_text(conn, payload.get("say_id")),
                answers=None,
                word=None,
                ts=closed_ts or now_ms,
                source_key=ref,
            )
        )
    return done


def _backfill_drafts(conn, now_ms: int) -> int:
    """Черновики: подтверждение — аудит `outbox_approve` (`detail.source` есть только у не-ui), отклонение —
    исход `declined` (пишет только ответ владельца или агента) и аудит `outbox_cancel` (кто отменил)."""
    done = 0
    rows = conn.execute(
        "SELECT outbox_id, ts, actor, detail_json FROM main.audit WHERE action = 'outbox_approve' "
        "AND actor != 'outbox' AND outbox_id IS NOT NULL ORDER BY id"
    ).fetchall()
    for outbox_id, ts, _actor, raw in rows:
        detail = _json(raw)
        by = {"agent": BY_AGENT, "auto": BY_AUTO}.get(detail.get("source"), BY_UI)
        origin = conn.execute(
            f"SELECT origin FROM {db_of(outbox_id)}.outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        if origin is None or origin[0] == "ui":
            continue  # своё сообщение владельца, не карточка
        ref = f"draft:{outbox_id}"
        card = _backfill_describe(conn, ref, now_ms)
        if card is None:
            continue
        text = None
        if detail.get("edited") is True:
            row = conn.execute(
                f"SELECT text FROM {db_of(outbox_id)}.outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
            text = row[0] if row is not None else None
        done += bool(
            _insert(
                conn, card, by=by, action="approve", text=text, answers=None, word=None, ts=ts, source_key=ref
            )
        )
    # Отклонение владельца: отмена кукой (`actor='ui'`) черновика сессии (`origin='api'`), который до того не
    # подтверждали (иначе это снятие уже подтверждённого, не решение по карточке). Агента — его строка
    # `owner_item` с действием `cancel`. Отмена самой сессией — ни то, ни другое (П6).
    rows = conn.execute(
        "SELECT a.outbox_id, a.ts FROM main.audit a WHERE a.action = 'outbox_cancel' AND a.actor = 'ui' "
        "AND a.result = 'cancelled' AND a.outbox_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM main.audit p "
        "WHERE p.action = 'outbox_approve' AND p.outbox_id = a.outbox_id AND p.id < a.id) ORDER BY a.id"
    ).fetchall()
    cancels = [(outbox_id, ts, BY_UI) for outbox_id, ts in rows]
    cancels += [
        (int(ref.split(":", 1)[1]), ts, BY_AGENT)
        for ref, ts in conn.execute(
            "SELECT ref, closed_ts FROM main.owner_item WHERE kind = 'draft' AND closed_by = 'agent' "
            "AND json_extract(payload_json, '$.action') = 'cancel' ORDER BY id"
        ).fetchall()
    ]
    for outbox_id, ts, by in cancels:
        origin = conn.execute(
            f"SELECT origin FROM {db_of(outbox_id)}.outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        if origin is None or origin[0] == "ui":
            continue
        ref = f"draft:{outbox_id}"
        card = _backfill_describe(conn, ref, now_ms)
        if card is None:
            continue
        done += bool(
            _insert(
                conn, card, by=by, action="cancel", text=None, answers=None, word=None, ts=ts, source_key=ref
            )
        )
    return done


def _backfill_describe(conn, ref: str, now_ms: int) -> Card | None:
    """Загрузка истории не роняет ядро: карточку, которую не описать, пропускаем с записью в журнал."""
    try:
        return describe(conn, ref, now_ms=now_ms)
    except Exception:  # noqa: BLE001 — история разная, ядро должно подняться
        _log.warning("журнал решений: карточка %s из истории пропущена", ref, exc_info=True)
        return None


def backfill(conn, *, now_ms: int) -> int:
    """П4: первая загрузка журнала из истории — один раз (флаг `BACKFILL_KEY` в `main.meta`), идемпотентно
    (`source_key` = `ref`, повтор — `INSERT OR IGNORE`). Источники: решённые `held_request` (решение владельца —
    без `by` в ответе и без строки `owner_item` агента или «сразу да»), закрытые ответом карточки `turn`
    (текст — реплика `held_say`), черновики по аудиту. Возвращает число записанных строк."""
    if conn.execute("SELECT 1 FROM main.meta WHERE key = ?", (BACKFILL_KEY,)).fetchone() is not None:
        return 0
    done = _backfill_requests(conn, now_ms) + _backfill_turns(conn, now_ms) + _backfill_drafts(conn, now_ms)
    conn.execute("INSERT OR REPLACE INTO main.meta (key, value) VALUES (?, ?)", (BACKFILL_KEY, str(now_ms)))
    return done


# --- правки владельца (П14) --------------------------------------------------------------------------------------


def _empty_edits() -> dict:
    return {"rules": None, "keep": [], "drop": [], "always_me": []}


def _edits_value(raw) -> dict:
    out = _empty_edits()
    value = raw if isinstance(raw, dict) else {}
    if isinstance(value.get("rules"), str):
        out["rules"] = value["rules"]
    for name in EDIT_LISTS:
        items = value.get(name)
        if isinstance(items, list):
            out[name] = [k for k in items if isinstance(k, str)]
    return out


def edits_of(conn, project_id: int) -> dict | None:
    """Правки владельца проекта (`GENERAL_ID` — «все проекты»): `{rules, keep, drop, always_me}`; нет проекта —
    `None`."""
    if project_id == GENERAL_ID:
        row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (GENERAL_META,)).fetchone()
        return _edits_value(_json(row[0]) if row is not None else {})
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    return _edits_value(_json(row[0]).get(EDITS_KEY))


def _check_edits(changes) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise DecisionError(422, "правки: {rules?, keep?, drop?, always_me?}")
    unknown = sorted(set(changes) - {"rules", *EDIT_LISTS})
    if unknown:
        raise DecisionError(422, f"правки: неизвестное поле {', '.join(unknown)}")
    out: dict = {}
    if "rules" in changes:
        rules = changes["rules"]
        if rules is not None and (not isinstance(rules, str) or len(rules) > RULES_MAX):
            raise DecisionError(422, f"rules: текст не длиннее {RULES_MAX}")
        out["rules"] = rules if rules and rules.strip() else None
    for name in EDIT_LISTS:
        if name not in changes:
            continue
        items = changes[name]
        if not isinstance(items, list) or len(items) > KEYS_MAX:
            raise DecisionError(422, f"{name}: список ключей образцов, не больше {KEYS_MAX}")
        seen: list[str] = []
        for key in items:
            if not isinstance(key, str) or not key.strip() or len(key) > KEY_MAX:
                raise DecisionError(422, f"{name}: ключ — строка до {KEY_MAX} символов")
            if key not in seen:
                seen.append(key)
        out[name] = seen
    return out


def set_edits(conn, project_id: int, changes, *, now_ms: int, audit: dict) -> dict | None:
    """Записать правки владельца (поля из `changes` заменяют прежние; прочие ключи политики проекта не трогаются).
    Только кука интерфейса (проверяет маршрут). Нет проекта — `None`; кривое — 422. Аудит `patterns_edit`
    без текстов; `seq` двигается."""
    checked = _check_edits(changes)
    current = edits_of(conn, project_id)
    if current is None:
        return None
    new = {**current, **checked}
    if project_id == GENERAL_ID:
        conn.execute(
            "INSERT OR REPLACE INTO main.meta (key, value) VALUES (?, ?)",
            (GENERAL_META, json.dumps(new, ensure_ascii=False, sort_keys=True)),
        )
    else:
        row = conn.execute(
            "SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)
        ).fetchone()
        policy = _json(row[0] if row is not None else None)
        policy[EDITS_KEY] = new
        conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
            (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
        )
    next_seq(conn)
    write_audit(
        conn,
        now_ms=now_ms,
        action="patterns_edit",
        result="ok",
        detail={
            "project_id": project_id,
            "rules_len": len(new["rules"] or ""),
            **{name: len(new[name]) for name in EDIT_LISTS},
        },
        **audit,
    )
    return new


# --- «всегда меня» (П7) -------------------------------------------------------------------------------------


def _matches(keys, fingerprint: str, cls: str | None, rule: str | None) -> bool:
    """Ключ правки — отпечаток целиком, `class:<класс>` или `rule:<правило гарда>`."""
    return (
        fingerprint in keys
        or (cls is not None and f"class:{cls}" in keys)
        or (rule is not None and f"rule:{rule}" in keys)
    )


def always_me_keys(conn, project_id: int | None) -> frozenset[str]:
    """Ключи «всегда меня» проекта и общие; проекта нет (и проект 0) — только общие."""
    keys = set((edits_of(conn, GENERAL_ID) or _empty_edits())["always_me"])
    if project_id:
        keys |= set((edits_of(conn, project_id) or _empty_edits())["always_me"])
    return frozenset(keys)


def always_me_print(
    conn, project_id, fingerprint: str, cls: str | None = None, rule: str | None = None
) -> bool:
    keys = always_me_keys(conn, project_id)
    return bool(keys) and _matches(keys, fingerprint, cls, rule)


def always_me_card(conn, project_id, ref: str, *, keys: frozenset[str] | None = None) -> bool:
    """П7: карточка `ref` попала под «всегда меня» проекта — её маршрут `me`, агенту её не дают."""
    keys = always_me_keys(conn, project_id) if keys is None else keys
    if not keys:
        return False
    card = describe(conn, ref)
    return card is not None and _matches(keys, card.fingerprint, card.ask_class, card.rule)


# --- образцы и похожие (П8, П9) -----------------------------------------------------------------------------


def _times(n: int) -> str:
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} раза"
    return f"{n} раз"


def _verdict_ru(verdict: str) -> str:
    return {dp.YES: "да", dp.NO: "нет"}.get(verdict, f"«{verdict[:60]}»")


def line(kind: str, fingerprint: str, cls, rule, verdict: str, count: int, last_ts: int) -> str:
    """П8: строка образца по шаблону «<класс>/<правило гарда> → <решение>, N раз, последний ДД.ММ»."""
    first = cls or kind
    if fingerprint.startswith("permission|"):
        parts = fingerprint.split("|", 3)
        first = (parts[3] if len(parts) > 3 else "") or parts[1]
    second = rule or kind
    day = time.strftime("%d.%m", time.localtime(last_ts / 1000))
    return f"{first}/{second} → {_verdict_ru(verdict)}, {_times(count)}, последний {day}"


def _rows(conn, where: str, params: list, order: str = "ts DESC", limit: int | None = None) -> list[dict]:
    cols = (
        "ts, ref, project_id, session_id, role, engine, kind, ask_class, rule, fingerprint, action, verdict, "
        "ask_text, answer_text, decided_by, word_json"
    )
    names = [c.strip() for c in cols.split(",")]
    found: list[dict] = []
    for schema in STORES:
        # Ф76-4а: решение, помеченное владельцем «неверно», не учит и в «похожие» не идёт.
        sql = (
            f"SELECT {cols} FROM {schema}.decision_log WHERE wrong_ts IS NULL AND ({where}) ORDER BY {order}"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        found += [dict(zip(names, r, strict=True)) for r in conn.execute(sql, params).fetchall()]
    found.sort(key=lambda r: r["ts"], reverse=True)
    return found if limit is None else found[:limit]


def _pattern(rows: list[dict], key: str, edits: dict, kind: str) -> dict | None:
    """Образец по учебным строкам одного отпечатка: одно решение (противоположных нет), не меньше
    `PATTERN_MIN` раз (у «оставить» — хоть раз); «убрать» — нет образца; у разрешения — только «нет» (П2);
    отпечаток без класса (`turn|-`, `decision_print.classless`) — нет образца, только «похожие» (ревью
    Ф76)."""
    verdicts = {r["verdict"] for r in rows if r["verdict"] is not None}
    if dp.classless(key) or len(verdicts) != 1 or key in edits["drop"]:
        return None
    verdict = next(iter(verdicts))
    hits = [r for r in rows if r["verdict"] == verdict]
    kept = key in edits["keep"]
    if len(hits) < PATTERN_MIN and not kept:
        return None
    # `work_request` — старые строки журнала (задачи по «Го» убраны в Ф78-1).
    if kind in ("permission", "work_request") and key.startswith("permission|") and verdict != dp.NO:
        return None
    last = hits[0]
    return {
        "key": key,
        "line": line(last["kind"], key, last["ask_class"], last["rule"], verdict, len(hits), last["ts"]),
        "verdict": verdict,
        "count": len(hits),
        "last_ts": last["ts"],
        "kept": kept,
    }


def _merged_edits(conn, project_id: int) -> dict:
    general = edits_of(conn, GENERAL_ID) or _empty_edits()
    own = edits_of(conn, project_id) or _empty_edits()
    return {name: list({*general[name], *own[name]}) for name in EDIT_LISTS}


def patterns_for(conn, card: Card, *, now_ms: int) -> list[dict]:
    """П9: образцы карточки — запросом: учебные строки проекта карточки с тем же отпечатком за `WINDOW_MS`,
    той же роли или без роли (внешние окна). Карточка вне проекта — ничего."""
    if not card.project_id:
        return []
    rows = _rows(
        conn,
        f"project_id = ? AND fingerprint = ? AND decided_by IN {_LEARN_SQL} AND ts >= ? "
        "AND (role IS NULL OR role IS ?)",
        [card.project_id, card.fingerprint, now_ms - WINDOW_MS, card.role],
    )
    found = _pattern(rows, card.fingerprint, _merged_edits(conn, card.project_id), card.kind)
    return [found] if found is not None else []


def project_patterns(conn, project_id: int, *, now_ms: int) -> list[dict]:
    """Все образцы проекта (для раздела «Мой агент»): по отпечаткам учебных строк за окно, с пометкой
    `status` — `active` (действует), `young` (меньше `PATTERN_MIN`), `faded` (есть противоположное),
    `dropped` («убрать»)."""
    if not project_id:
        return []
    edits = _merged_edits(conn, project_id)
    rows = _rows(
        conn,
        f"project_id = ? AND decided_by IN {_LEARN_SQL} AND ts >= ? AND verdict IS NOT NULL",
        [project_id, now_ms - WINDOW_MS],
    )
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if dp.classless(row["fingerprint"]):
            continue  # ревью Ф76: без класса образцом не бывает — в списке образцов его нет
        groups.setdefault(row["fingerprint"], []).append(row)
    out: list[dict] = []
    for key, group in groups.items():
        last = group[0]
        found = _pattern(group, key, edits, last["kind"])
        verdicts = {r["verdict"] for r in group}
        if found is None:
            status = "dropped" if key in edits["drop"] else "faded" if len(verdicts) > 1 else "young"
            verdict = last["verdict"]
            count = sum(1 for r in group if r["verdict"] == verdict)
            found = {
                "key": key,
                "line": line(last["kind"], key, last["ask_class"], last["rule"], verdict, count, last["ts"]),
                "verdict": verdict,
                "count": count,
                "last_ts": last["ts"],
                "kept": key in edits["keep"],
            }
        else:
            status = "active"
        found.update(
            {
                "status": status,
                "kind": last["kind"],
                "class": last["ask_class"],
                "rule": last["rule"],
                "always_me": _matches(set(edits["always_me"]), key, last["ask_class"], last["rule"]),
                "examples": [_example(r) for r in group[:3]],
            }
        )
        out.append(found)
    out.sort(key=lambda p: p["last_ts"], reverse=True)
    return out


def _example(row: dict) -> dict:
    return {
        "ts": row["ts"],
        "by": row["decided_by"],
        "kind": row["kind"],
        "role": row["role"],
        "action": row["action"],
        "verdict": row["verdict"],
        "ask": row["ask_text"],
        "answer": row["answer_text"],
    }


def similar(conn, card: Card, *, limit: int = SIMILAR_MAX) -> list[dict]:
    """До `limit` последних похожих решений владельца (учебные строки) в проекте карточки: тот же отпечаток, затем
    тот же вид и класс; та же роль или без роли. Сама карточка — нет."""
    if not card.project_id:
        return []
    base = f"project_id = ? AND decided_by IN {_LEARN_SQL} AND (role IS NULL OR role IS ?) AND ref IS NOT ? "
    same = _rows(conn, base + "AND fingerprint = ?", [card.project_id, card.role, card.ref, card.fingerprint],
                 limit=limit)  # fmt: skip
    if len(same) < limit and card.ask_class is not None:
        more = _rows(
            conn,
            base + "AND kind = ? AND ask_class = ? AND fingerprint != ?",
            [card.project_id, card.role, card.ref, card.kind, card.ask_class, card.fingerprint],
            limit=limit - len(same),
        )
        same += more
    return [_example(r) for r in same[:limit]]


HUB_MD_MAX = 8000  # как у вклейки правил сессий (`session_rules.RULES_MAX_CHARS`)


def _workdir_of(conn, project_id) -> str | None:
    if not project_id:
        return None
    row = conn.execute("SELECT workdir FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return row[0] if row is not None and row[0] else None


def hub_md_of(workdir: str | None) -> dict | None:
    """Ф76-3: файл проекта `<workdir>/.claude/hub.md` для агента — `{path, text, truncated}` (текст
    маскирован, до `HUB_MD_MAX`); нет папки, файла или он пуст — `None`."""
    from .. import session_rules  # только stdlib

    if not workdir:
        return None
    path = session_rules.project_rules_path(workdir)
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return None
    if not raw:
        return None
    text = _mask(raw)
    return {
        "path": str(path),
        "text": text if len(text) <= HUB_MD_MAX else text[: HUB_MD_MAX - 1] + "…",
        "truncated": len(text) > HUB_MD_MAX,
    }


# --- внешняя история (П11–П13) ------------------------------------------------------------------------------


def project_of_cwd(cwd, projects: list[tuple[int, str]]) -> int | None:
    """П12: проект по папке самым длинным префиксом `project.workdir`; `.runs/`, временные каталоги и папки
    вне проектов — `None` («вне проектов»)."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    path = os.path.normpath(cwd)
    if any(path == root or path.startswith(root + os.sep) for root in _EXT_SKIP_ROOTS):
        return None
    if any(part in path + os.sep for part in _EXT_SKIP_PARTS):
        return None
    best: tuple[int, int] | None = None
    for project_id, workdir in projects:
        if not workdir:
            continue
        root = os.path.normpath(workdir)
        if (path == root or path.startswith(root + os.sep)) and (best is None or len(root) > best[1]):
            best = (project_id, len(root))
    return best[0] if best is not None else None


def ext_prepare(rows: list[dict], *, projects: list[tuple[int, str]], home: str) -> list[dict]:
    """Разбор строк сборщика ВНЕ потока-писателя (гард может звать git): проект по `cwd`, правило гарда,
    отпечаток, класс, решение, маска текстов. Сборщику не верим — всё считает ядро. Кривая строка — 422."""
    if len(rows) > EXT_ROWS_MAX:
        raise DecisionError(422, f"rows: не больше {EXT_ROWS_MAX}")
    workdirs = dict(projects)
    out: list[dict] = []
    for n, row in enumerate(rows):
        kind = row.get("kind")
        if kind not in EXT_KINDS:
            raise DecisionError(422, f"rows[{n}].kind: {' или '.join(EXT_KINDS)}")
        project_id = project_of_cwd(row.get("cwd"), projects)
        tool, tool_input = row.get("tool"), row.get("input")
        if kind == "permission":
            decision = row.get("decision")
            if decision not in ("allow", "deny") or not isinstance(tool, str) or not tool:
                raise DecisionError(422, f"rows[{n}]: у разрешения нужны tool и decision allow|deny")
            rule = dp.rule_of(
                tool, tool_input, cwd=row.get("cwd"), workdir=workdirs.get(project_id), home=home
            )
            fingerprint, _cmd = dp.permission_print(tool, tool_input, rule)
            cls = None
            ask = _ask_of_input(tool, tool_input)
            action = decision
            verdict = dp.YES if decision == "allow" else dp.NO
            answer = row.get("answer")
        else:
            ask, answer = row.get("ask"), row.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise DecisionError(422, f"rows[{n}]: у ответа окна нужен answer")
            fingerprint, cls = dp.turn_print(ask)
            rule = None
            action = "reply"
            verdict = dp.verdict_of_text(answer)
        out.append(
            {
                "source_key": f"ext:{row['key']}",
                "ts": int(row["ts"]),
                "project_id": project_id,
                "engine": row.get("engine"),
                "kind": kind,
                "ask_class": cls,
                "rule": rule,
                "fingerprint": fingerprint,
                "action": action,
                "verdict": verdict,
                "ask_text": _clip(ask, ASK_MAX) if ask else None,
                "answer_text": _clip(answer, ANSWER_MAX) if isinstance(answer, str) and answer else None,
            }
        )
    return out


def ext_store(conn, prepared: list[dict], *, now_ms: int, audit: dict | None = None) -> dict:
    """Строки внешней истории — в журнал (`owner_ext`), в файл проекта; повтор ключа не задваивает. Аудит
    `patterns_ext` — только числа."""
    accepted = 0
    for row in prepared:
        if ext_key_taken(conn, row["source_key"]):
            continue  # повторный проход сборщика (и строка, ушедшая в другой файл после смены проекта)
        schema = _schema_for_project(conn, row["project_id"])
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {schema}.decision_log (ts, ref, source_key, project_id, "
            "session_id, role, engine, kind, ask_class, rule, fingerprint, action, verdict, ask_text, "
            "answer_text, decided_by) "
            "VALUES (?, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["ts"],
                row["source_key"],
                row["project_id"],
                row["engine"],
                row["kind"],
                row["ask_class"],
                row["rule"],
                row["fingerprint"],
                row["action"],
                row["verdict"],
                row["ask_text"],
                row["answer_text"],
                BY_EXT,
            ),
        )
        accepted += cur.rowcount
    result = {"accepted": accepted, "duplicates": len(prepared) - accepted}
    if audit is not None:
        write_audit(conn, now_ms=now_ms, action="patterns_ext", result="ok", detail=result, **audit)
    return result


def ext_key_taken(conn, source_key: str) -> bool:
    return any(
        conn.execute(
            f"SELECT 1 FROM {schema}.decision_log WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        for schema in STORES
    )


# --- решения агента и «неверно» владельца (Ф76-4а) ---------------------------------------------------------------

AGENT_BY = (BY_AGENT, BY_WORD)
_AGENT_SQL = "(" + ", ".join(f"'{b}'" for b in AGENT_BY) + ")"
AGENT_DECISIONS_MAX = 100
AGENT_TEXT_MAX = 300
WRONG_NOTE_MAX = 500


def pattern_key_of(basis) -> str | None:
    """Ключ образца из основания агента `pattern:<ключ>`; другое основание или пусто — `None`."""
    if not isinstance(basis, str) or not basis.startswith("pattern:"):
        return None
    key = basis[len("pattern:") :].strip()
    return key or None


def _short(text) -> str | None:
    """Текст журнала уже маскирован при записи (П13); маска ещё раз — на случай строки, записанной мимо."""
    return _clip(text, AGENT_TEXT_MAX) if text else None
