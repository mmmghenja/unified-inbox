"""Правила проекта для входящих (Ф63-1, docs/architecture.md П1–П7 —
приоритетнее разделов 1–6).

Правило = условие -> действия, оба — данные, не код. Условие (всё перечисленное — И, пустое поле не
проверяется, хотя бы одно из `from`/`subject`/`text` обязательно): `accounts` — номера аккаунтов; `from` —
домены (с поддоменами) или адреса отправителя, только у почты; `subject` — ключи по теме письма (у не-почты —
по названию беседы); `text` — ключи по тексту; `exclude` — минус-слова по теме и тексту; `kinds` — виды бесед
(по умолчанию все, кроме бесед трекеров — для них правило не срабатывает никогда). Ключи — формат подписки
(`keywords`: по началу слова, фразы до 6 слов, регулярок нет). Действия (хотя бы одно): `wake` — роли, чьи
подписки проекта в режиме «сообщения» получают сигнал сразу (мимо пачки, фильтра «лично мне», заглушки
беседы),
`attention` — `react` (поднимает рассылку до карточки) или `info` (гасит), `archive` — беседа сразу в
архив (владелец 22.10: «фильтр, по которому всё это убирается сразу в архив»; ставит и правит такое правило
только владелец — интерфейс или админский сокет; открытые карточки беседы закрываются, разборщик и подписки
архив не будят).

П2: правила оцениваются ОДИН раз — в ингесте при вставке нового чужого входящего (`evaluate`). Совпавшие
правила пишутся на сообщение (`message.rule_hits`, `message.rule_attention`), счётчик правила — в той же
транзакции. Правка сообщения правила не пересчитывает; правило, заведённое позже, на старые сообщения не
действует. Правило проекта P видит только беседы, лежащие в P после раскладки; беседы «Неразобранного»
аккаунтов, которые P слышит (Ф52-2), — только `wake`. Хаб по правилу ничего не отправляет.
"""

import functools
import json
import re
from typing import NamedTuple

from . import keywords as keywords_mod
from . import subs as subs_mod
from . import unsorted as unsorted_mod
from .attention import TASK_KINDS
from .audit import write_audit

RULES_MAX = 50  # правил на проект (включая выключенные)
LIST_MAX = 20  # элементов в поле-списке
TITLE_MAX = 80
KEYS_MAX = LIST_MAX
HITS_MAX = 5  # номеров правил на сообщении (`message.rule_hits`) и в сигнале (`rules_json`)
ADDR_MAX = 254
KINDS = ("dm", "group", "channel", "topic", "mail_thread")
ATTENTIONS = ("react", "info")
MATCH_FIELDS = ("accounts", "from", "subject", "text", "exclude", "kinds")
ACTION_FIELDS = ("wake", "attention", "archive")
ARCHIVE_ACTORS = ("ui", "admin")  # кто ставит и правит правило «в архив»
FIELDS = ("title", *MATCH_FIELDS, *ACTION_FIELDS, "enabled")

_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,61}[a-z0-9]")
_LOCAL_RE = re.compile(r"[^\s@\"(),:;<>\[\]\\]{1,64}")
_BAD_TITLE_RE = re.compile(r"[\x00-\x1f\x7f]")


class RuleError(Exception):
    """Отказ: `status` — HTTP-код, `detail` — текст (поле и причина)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- проверка полей --------------------------------------------------------------------------------------


def _int_list(value, field: str) -> list[int]:
    if not isinstance(value, list):
        raise RuleError(422, f"{field}: нужен список")
    if len(value) > LIST_MAX:
        raise RuleError(422, f"{field}: больше {LIST_MAX} элементов")
    out: list[int] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, int) or isinstance(item, bool) or not 1 <= item <= 2**53:
            raise RuleError(422, f"{field} {index}: номер аккаунта — целое от 1")
        if item not in out:
            out.append(item)
    return out


def _from_list(value) -> list[str]:
    if not isinstance(value, list):
        raise RuleError(422, "from: нужен список доменов или адресов")
    if len(value) > LIST_MAX:
        raise RuleError(422, f"from: больше {LIST_MAX} элементов")
    out: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            raise RuleError(422, f"from {index}: не строка")
        text = item.strip().lower()
        if text.startswith("@"):
            text = text[1:]  # «@example.com» — домен
        if not text or len(text) > ADDR_MAX:
            raise RuleError(422, f"from {index}: пусто или длиннее {ADDR_MAX}")
        if "@" in text:
            local, _, domain = text.rpartition("@")
            if not _LOCAL_RE.fullmatch(local) or not _DOMAIN_RE.fullmatch(domain):
                raise RuleError(422, f"from {index}: не адрес почты (имя@домен)")
        elif not _DOMAIN_RE.fullmatch(text):
            raise RuleError(422, f"from {index}: не домен (пример: example.com) и не адрес")
        if text not in out:
            out.append(text)
    return out


def _keys(value, field: str) -> list[str]:
    try:
        return keywords_mod.normalize(value, KEYS_MAX, what=field, words_max=keywords_mod.RULE_WORDS_MAX)
    except keywords_mod.Invalid as exc:
        raise RuleError(422, str(exc)) from exc


def _kinds(value) -> list[str]:
    if not isinstance(value, list):
        raise RuleError(422, "kinds: нужен список")
    out: list[str] = []
    for index, item in enumerate(value, start=1):
        if item not in KINDS:
            raise RuleError(422, f"kinds {index}: вид беседы — один из {', '.join(KINDS)}")
        if item not in out:
            out.append(item)
    return out


def _wake(value) -> list[str]:
    if not isinstance(value, list):
        raise RuleError(422, "wake: нужен список ролей")
    if len(value) > LIST_MAX:
        raise RuleError(422, f"wake: больше {LIST_MAX} ролей")
    out: list[str] = []
    keys: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str):
            raise RuleError(422, f"wake {index}: не строка")
        try:
            role, key = subs_mod.role_form(item)
        except subs_mod.SubError as exc:
            raise RuleError(422, f"wake {index}: {exc.detail}") from exc
        if role is None or key is None:
            raise RuleError(422, f"wake {index}: пустая роль")
        if key not in keys:
            keys.add(key)
            out.append(role)
    return out


def _title(value) -> str:
    if not isinstance(value, str):
        raise RuleError(422, "title: нужна строка")
    text = " ".join(_BAD_TITLE_RE.sub(" ", value).split())
    if not text:
        raise RuleError(422, "title: пустое название")
    if len(text) > TITLE_MAX:
        raise RuleError(422, f"title: длиннее {TITLE_MAX} символов")
    return text


def normalize(fields: dict, *, base: dict | None = None) -> dict:
    """Поля запроса (`add` — все, `update` — поверх `base`, прежнего правила в той же форме) -> правило в
    нормальной форме `{title, enabled, match: {...}, action: {...}}`. `None` у поля-списка и у `attention` —
    очистить. Отказ — `RuleError(422)` с полем и причиной."""
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise RuleError(422, f"неизвестное поле: {unknown[0]}")
    result = {
        "title": (base or {}).get("title"),
        "enabled": (base or {}).get("enabled", True),
        "match": dict((base or {}).get("match") or {}),
        "action": dict((base or {}).get("action") or {}),
    }
    if "title" in fields:
        result["title"] = _title(fields["title"])
    if "enabled" in fields:
        if not isinstance(fields["enabled"], bool):
            raise RuleError(422, "enabled: true или false")
        result["enabled"] = fields["enabled"]
    parsers = {
        "accounts": lambda v: _int_list(v, "accounts"),
        "from": _from_list,
        "subject": lambda v: _keys(v, "subject"),
        "text": lambda v: _keys(v, "text"),
        "exclude": lambda v: _keys(v, "exclude"),
        "kinds": _kinds,
    }
    for field, parse in parsers.items():
        if field not in fields:
            continue
        value = fields[field]
        parsed = parse(value) if value is not None else []
        if parsed:
            result["match"][field] = parsed
        else:
            result["match"].pop(field, None)
    if "wake" in fields:
        wake = _wake(fields["wake"]) if fields["wake"] is not None else []
        if wake:
            result["action"]["wake"] = wake
        else:
            result["action"].pop("wake", None)
    if "attention" in fields:
        attention = fields["attention"]
        if attention is None:
            result["action"].pop("attention", None)
        elif attention in ATTENTIONS:
            result["action"]["attention"] = attention
        else:
            raise RuleError(422, "attention: react, info или null")
    if "archive" in fields:
        archive = fields["archive"]
        if archive is True:
            result["action"]["archive"] = True
        elif archive is None or archive is False:
            result["action"].pop("archive", None)
        else:
            raise RuleError(422, "archive: true, false или null")
    if result["title"] is None:
        raise RuleError(422, "title: нужно название правила")
    match, action = result["match"], result["action"]
    if not any(match.get(f) for f in ("from", "subject", "text")):
        raise RuleError(422, "условие: нужно хотя бы одно из from, subject, text")
    if not action.get("wake") and not action.get("attention") and not action.get("archive"):
        raise RuleError(422, "действие: нужно хотя бы одно из wake, attention, archive")
    return result


# --- хранение --------------------------------------------------------------------------------------------


_COLUMNS = (
    "id, project_id, title, match_json, action_json, enabled, created_by_role, created_by_session, "
    "created_ts, updated_ts, hits, last_hit_ts"
)


class Rec(NamedTuple):
    id: int
    project_id: int
    title: str
    match_json: str
    action_json: str
    enabled: int
    created_by_role: str | None
    created_by_session: int | None
    created_ts: int
    updated_ts: int
    hits: int
    last_hit_ts: int | None


def _loads(raw: str | None) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def _dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def get(conn, rule_id: int) -> Rec | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM main.inbound_rule WHERE id = ?", (rule_id,)).fetchone()  # noqa: S608
    return Rec(*row) if row is not None else None


def of_project(conn, project_id: int) -> list[Rec]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.inbound_rule WHERE project_id = ? ORDER BY id",  # noqa: S608
        (project_id,),
    ).fetchall()
    return [Rec(*row) for row in rows]


def _form(rec: Rec) -> dict:
    return {
        "title": rec.title,
        "enabled": bool(rec.enabled),
        "match": _loads(rec.match_json),
        "action": _loads(rec.action_json),
    }


def project_exists(conn, project_id: int) -> bool:
    if not isinstance(project_id, int) or project_id < 1:
        return False
    return conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is not None


def _check_accounts(conn, accounts: list[int]) -> None:
    if not accounts:
        return
    known = {row[0] for row in conn.execute("SELECT id FROM main.account")}
    for index, account in enumerate(accounts, start=1):
        if account not in known:
            raise RuleError(422, f"accounts {index}: аккаунта {account} нет")


def _audit(
    conn, *, action: str, rec_id: int, project_id: int, actor: dict, now_ms: int, detail: dict
) -> None:
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor["kind"],
        api_token_id=actor.get("token_id"),
        client_name=actor.get("client_name"),
        request_id=actor.get("request_id") or "",
        action=action,
        result="ok",
        detail={"rule_id": rec_id, "project_id": project_id, "by": actor.get("role") or "ui", **detail},
    )


def _shape(form: dict) -> dict:
    """Для аудита — без текстов ключей: какие поля заданы и сколько в них."""
    match, action = form["match"], form["action"]
    return {
        "match": {k: len(v) for k, v in match.items()},
        "wake": len(action.get("wake") or []),
        "attention": action.get("attention"),
        "archive": bool(action.get("archive")),
        "enabled": form["enabled"],
    }


def _check_archive(actor: dict, *forms: dict) -> None:
    """Правило «в архив» прячет сообщения от владельца — ставит, правит и снимает его только он сам."""
    if actor.get("kind") in ARCHIVE_ACTORS:
        return
    if any(form["action"].get("archive") for form in forms):
        raise RuleError(403, "правило «в архив» ставит и правит только владелец в интерфейсе")


def add(conn, project_id: int, fields: dict, *, actor: dict, now_ms: int) -> Rec:
    """Новое правило проекта. `actor` — `{kind, token_id, client_name, request_id, role, sub_id}`
    (`role`/`sub_id` — у сессии). 404 — проекта нет; 422 — поле; 429 — 50 правил в проекте."""
    if not project_exists(conn, project_id):
        raise RuleError(404, "проекта нет")
    form = normalize(fields)
    _check_archive(actor, form)
    _check_accounts(conn, form["match"].get("accounts") or [])
    count = conn.execute(
        "SELECT COUNT(*) FROM main.inbound_rule WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if count >= RULES_MAX:
        raise RuleError(429, f"в проекте уже {RULES_MAX} правил входящих — удали ненужные")
    cur = conn.execute(
        "INSERT INTO main.inbound_rule (project_id, title, match_json, action_json, enabled, "
        "created_by_role, created_by_session, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            form["title"],
            _dumps(form["match"]),
            _dumps(form["action"]),
            int(form["enabled"]),
            actor.get("role"),
            actor.get("sub_id"),
            now_ms,
            now_ms,
        ),
    )
    rec = get(conn, cur.lastrowid)
    assert rec is not None
    _audit(
        conn,
        action="inbound_rule_add",
        rec_id=rec.id,
        project_id=project_id,
        actor=actor,
        now_ms=now_ms,
        detail=_shape(form),
    )
    return rec


def _own(conn, project_id: int, rule_id: int) -> Rec:
    rec = get(conn, rule_id)
    if rec is None or rec.project_id != project_id:
        raise RuleError(404, "правила нет в этом проекте")
    return rec


def update(conn, project_id: int, rule_id: int, fields: dict, *, actor: dict, now_ms: int) -> Rec:
    """Правка (поля поверх прежних; `enabled` — выключить/включить). 404 — правила нет в проекте."""
    rec = _own(conn, project_id, rule_id)
    if not fields:
        raise RuleError(422, "что поменять: нет ни одного поля")
    base = _form(rec)
    form = normalize(fields, base=base)
    _check_archive(actor, base, form)
    _check_accounts(conn, form["match"].get("accounts") or [])
    conn.execute(
        "UPDATE main.inbound_rule SET title = ?, match_json = ?, action_json = ?, enabled = ?, "
        "updated_ts = ? WHERE id = ?",
        (form["title"], _dumps(form["match"]), _dumps(form["action"]), int(form["enabled"]), now_ms, rule_id),
    )
    new = get(conn, rule_id)
    assert new is not None
    _audit(
        conn,
        action="inbound_rule_update",
        rec_id=rule_id,
        project_id=project_id,
        actor=actor,
        now_ms=now_ms,
        detail={"fields": sorted(fields), **_shape(form)},
    )
    return new


def remove(conn, project_id: int, rule_id: int, *, actor: dict, now_ms: int) -> Rec:
    rec = _own(conn, project_id, rule_id)
    _check_archive(actor, _form(rec))
    conn.execute("DELETE FROM main.inbound_rule WHERE id = ?", (rule_id,))
    _audit(
        conn,
        action="inbound_rule_remove",
        rec_id=rule_id,
        project_id=project_id,
        actor=actor,
        now_ms=now_ms,
        detail={"hits": rec.hits},
    )
    return rec


# --- выдача ----------------------------------------------------------------------------------------------


def wake_status(conn, project_id: int, roles: list[str]) -> list[dict]:
    """П4: кто из ролей `wake` слушает. `messages` — живая подписка проекта в режиме «сообщения» (сигнал
    дойдёт), `cards` — только «карточки» (правилу нужно `attention: react`), `none` — роль не слушает
    (сигнал не копится)."""
    rows = conn.execute(
        "SELECT role_key, mode, direct FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL "
        "AND role_key IS NOT NULL",
        (project_id,),
    ).fetchall()
    modes: dict[str, set[str]] = {}
    for key, mode, direct in rows:
        modes.setdefault(key, set()).add("direct" if direct else mode)
    out = []
    for role in roles:
        key = role_key(role)
        have = modes.get(key or "", set())
        status = "messages" if "messages" in have else "cards" if "cards" in have else "none"
        out.append({"role": role, "listening": status})
    return out


def describe(form: dict) -> str:
    """Правило словами — для интерфейса и MCP (тексты ключей — данные, их пишет сессия или владелец)."""
    match, action = form["match"], form["action"]
    parts: list[str] = []
    if match.get("from"):
        parts.append("письма с " + ", ".join(match["from"]))
    if match.get("subject"):
        parts.append("тема: " + ", ".join(f"«{k}»" for k in match["subject"]))
    if match.get("text"):
        parts.append("текст: " + ", ".join(f"«{k}»" for k in match["text"]))
    if match.get("exclude"):
        parts.append("кроме: " + ", ".join(f"«{k}»" for k in match["exclude"]))
    if match.get("accounts"):
        parts.append("аккаунты " + ", ".join(str(a) for a in match["accounts"]))
    if match.get("kinds"):
        parts.append("виды: " + ", ".join(match["kinds"]))
    acts: list[str] = []
    if action.get("wake"):
        acts.append("будит " + ", ".join(f"«{r}»" for r in action["wake"]))
    if action.get("attention") == "react":
        acts.append("внимание: реагирую (карточка)")
    elif action.get("attention") == "info":
        acts.append("внимание: к сведению")
    if action.get("archive"):
        acts.append("беседа сразу в архив")
    return " · ".join(parts) + " → " + "; ".join(acts)


def rule_dict(conn, rec: Rec) -> dict:
    form = _form(rec)
    return {
        "id": rec.id,
        "project_id": rec.project_id,
        "title": rec.title,
        "enabled": bool(rec.enabled),
        "match": {field: form["match"].get(field, []) for field in MATCH_FIELDS},
        "action": {
            "wake": form["action"].get("wake", []),
            "attention": form["action"].get("attention"),
            "archive": bool(form["action"].get("archive")),
        },
        "description": describe(form),
        "created_by_role": rec.created_by_role,
        "created_by_session": rec.created_by_session,
        "created_ts": rec.created_ts,
        "updated_ts": rec.updated_ts,
        "hits": rec.hits,
        "last_hit_ts": rec.last_hit_ts,
        "wake_status": wake_status(conn, rec.project_id, form["action"].get("wake", [])),
    }


def list_rules(conn, project_id: int) -> list[dict]:
    return [rule_dict(conn, rec) for rec in of_project(conn, project_id)]


# --- оценка в ингесте (П2) -------------------------------------------------------------------------------


def role_key(role: str) -> str | None:
    """Ключ сравнения роли — тот же, что у подписки (`subs.role_form`)."""
    try:
        return subs_mod.role_form(role)[1]
    except subs_mod.SubError:
        return None


class Compiled(NamedTuple):
    id: int
    project_id: int
    accounts: frozenset[int]
    senders: tuple[str, ...]
    subject: keywords_mod.Compiled | None
    text: keywords_mod.Compiled | None
    exclude: keywords_mod.Compiled | None
    kinds: frozenset[str]
    wake: frozenset[str]  # ключи ролей
    attention: str | None
    archive: bool


@functools.lru_cache(maxsize=512)
def _compile(rule_id: int, project_id: int, match_json: str, action_json: str) -> Compiled:
    """Раскладка правила для `match` — кэш по строке правила (правка меняет JSON — новая раскладка)."""
    match, action = _loads(match_json), _loads(action_json)

    def keys(field: str) -> keywords_mod.Compiled | None:
        values = [k for k in match.get(field) or [] if isinstance(k, str) and k]
        return keywords_mod.compile({"keys": values, "exclude": []}) if values else None

    wake = {role_key(r) for r in action.get("wake") or [] if isinstance(r, str)}
    attention = action.get("attention")
    return Compiled(
        id=rule_id,
        project_id=project_id,
        accounts=frozenset(a for a in match.get("accounts") or [] if isinstance(a, int)),
        senders=tuple(s for s in match.get("from") or [] if isinstance(s, str)),
        subject=keys("subject"),
        text=keys("text"),
        exclude=keys("exclude"),
        kinds=frozenset(k for k in match.get("kinds") or [] if k in KINDS),
        wake=frozenset(k for k in wake if k),
        attention=attention if attention in ATTENTIONS else None,
        archive=action.get("archive") is True,
    )


class MsgCtx(NamedTuple):
    account_id: int
    kind: str
    sender: str | None  # адрес отправителя письма (нижний регистр); не почта — `None`
    subject: str | None  # тема письма; не почта (или тема не пришла) — название беседы
    text: str | None


def _sender_hit(senders: tuple[str, ...], address: str | None) -> bool:
    if not address or "@" not in address:
        return False
    domain = address.rpartition("@")[2]
    for pattern in senders:
        if "@" in pattern:
            if address == pattern:
                return True
        elif domain == pattern or domain.endswith("." + pattern):
            return True
    return False


def match(rule: Compiled, ctx: MsgCtx) -> bool:
    """Чистая проверка условия правила на сообщении (без SQL). Беседы трекеров — никогда."""
    if ctx.kind in TASK_KINDS or ctx.kind not in KINDS:
        return False
    if rule.kinds and ctx.kind not in rule.kinds:
        return False
    if rule.accounts and ctx.account_id not in rule.accounts:
        return False
    if not rule.senders and rule.subject is None and rule.text is None:
        return False  # страховка: условие без from/subject/text не срабатывает
    if rule.senders and (ctx.kind != "mail_thread" or not _sender_hit(rule.senders, ctx.sender)):
        return False
    if rule.subject is not None and keywords_mod.check(rule.subject, ctx.subject) is None:
        return False
    if rule.text is not None and keywords_mod.check(rule.text, ctx.text) is None:
        return False
    if rule.exclude is not None and (
        keywords_mod.check(rule.exclude, ctx.subject) is not None
        or keywords_mod.check(rule.exclude, ctx.text) is not None
    ):
        return False
    return True


def _enabled(conn, project_ids: list[int]) -> list[Compiled]:
    if not project_ids:
        return []
    marks = ",".join("?" for _ in project_ids)
    rows = conn.execute(
        "SELECT id, project_id, match_json, action_json FROM main.inbound_rule "  # noqa: S608
        f"WHERE enabled = 1 AND project_id IN ({marks}) ORDER BY id",
        project_ids,
    ).fetchall()
    return [_compile(*row) for row in rows]


class WakeRule(NamedTuple):
    rule_id: int
    project_id: int
    roles: frozenset[str]  # ключи ролей


class Outcome(NamedTuple):
    hits: list[int]  # номера совпавших правил для `message.rule_hits` (не больше `HITS_MAX`)
    attention: str | None
    wake: tuple[WakeRule, ...]
    archive: bool = False  # беседу — в архив (только правила проекта беседы)


NO_OUTCOME = Outcome([], None, ())


def evaluate(conn, *, project_id: int | None, ctx: MsgCtx, now_ms: int) -> Outcome:
    """П2: правила для нового чужого входящего. Беседа в проекте — правила проекта (все действия); беседа без
    проекта — правила проектов, слышащих «Неразобранное» её аккаунта (Ф52-2), только `wake`. Счётчик
    сработавших правил — здесь же. Конфликт `attention` нескольких правил — `react` (не гасить, если хоть одно
    правило просит реакцию). `archive` — хоть одно правило проекта беседы просит архив (у «Неразобранного»
    нет)."""
    if ctx.kind in TASK_KINDS:
        return NO_OUTCOME
    if project_id is not None:
        projects, full = [project_id], True
    else:
        projects, full = [pid for pid, _muted in unsorted_mod.listening_projects(conn, ctx.account_id)], False
    hits: list[int] = []
    attentions: set[str] = set()
    wake: list[WakeRule] = []
    archive = False
    for rule in _enabled(conn, projects):
        if not match(rule, ctx):
            continue
        hits.append(rule.id)
        if full and rule.attention is not None:
            attentions.add(rule.attention)
        if full and rule.archive:
            archive = True
        if rule.wake:
            wake.append(WakeRule(rule.id, rule.project_id, rule.wake))
    if not hits:
        return NO_OUTCOME
    marks = ",".join("?" for _ in hits)
    conn.execute(
        f"UPDATE main.inbound_rule SET hits = hits + 1, last_hit_ts = ? WHERE id IN ({marks})",  # noqa: S608
        (now_ms, *hits),
    )
    attention = "react" if "react" in attentions else ("info" if attentions else None)
    return Outcome(hits[:HITS_MAX], attention, tuple(wake), archive)


def dump_hits(hits: list[int]) -> str | None:
    return json.dumps(hits[:HITS_MAX]) if hits else None


def parse_hits(raw) -> list[int]:
    """`message.rule_hits` / `session_chan_signal.rules_json` -> номера правил (строгая форма, не больше
    5)."""
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError, TypeError:
        return []
    if not isinstance(value, list):
        return []
    out = [v for v in value if isinstance(v, int) and not isinstance(v, bool) and v >= 1]
    return out[:HITS_MAX]


def annotate(conn, messages: list[dict], visible_projects: frozenset[int] | None) -> None:
    """Поле `rules` сообщений выдачи (`[{id, title}]`, по `rule_hits`): название — у правил видимых проектов
    (`visible_projects` — `None` у интерфейса и токена на все проекты); удалённое правило — `title: null`.
    Одна выборка на страницу."""
    ids = sorted({rid for m in messages if isinstance(m, dict) for rid in m.get("rule_hits") or []})
    titles: dict[int, str | None] = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        for rid, project_id, title in conn.execute(
            f"SELECT id, project_id, title FROM main.inbound_rule WHERE id IN ({marks})",  # noqa: S608
            ids,
        ):
            if visible_projects is None or project_id in visible_projects:
                titles[rid] = title
    for message in messages:
        if not isinstance(message, dict):
            continue
        hits = message.get("rule_hits") or []
        message["rules"] = [{"id": rid, "title": titles.get(rid)} for rid in hits]
