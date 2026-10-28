"""Люди: один человек во всех источниках (Ф21-1, docs/architecture.md, E и поправки
П3, П5, П6, П8–П10).

Человек — группа контактов: `contact.person_id` в файле контакта указывает на строку `main.person` (без ПДн,
П5: имя, заданное владельцем, — `contact.person_name` у всех контактов человека). Связи через границу файлов нет;
удаление рабочего файла стирает рабочие контакты, их связи и цифры, пустых людей убирает уборка (B4).

- `link_all` — проход связывания раз в 10 минут (одна транзакция писателя): B1 кто не человек (каждый проход
  снимает человека у не-людей, П3, П8), B2 почта контакта, B3 склейка по почте, B4 уборка; висящий `person_id`
  (П9) — новый человек.
- `ensure_person` — человек для контакта по запросу карточки (E2), та же проверка B1.
- `merge`/`split`/`rename`/`mark_not_same` — ручные действия владельца (B5), только из UI.
- `card`/`list_people`/`channel_people`/`suggestions` — чтение (E2–E4, E6). Видны только контакты с не
  скрытым сообщением (П10), названия чужих проектов не отдаются (П6), беседы и образцы — в пределах проектов
  токена (E1).

Имена, ники и почты — ПДн: в журнал и аудит не пишутся (аудит — только id).
"""

import itertools
import json
import re
from collections import defaultdict
from collections.abc import Iterable

from . import new_recipient as nr
from . import people_stats as stats_mod
from . import person_facts, queries
from . import threads as threads_mod
from .db import db_of, reconcile_person_seq
from .security import Principal

SCHEMAS = ("main", "work")
EMAIL_RE = re.compile(r"^[^@\s:]+@[^@\s]+\.[^@\s]+$")
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,20}$")
NAME_MAX = 100
CHANNEL_PEOPLE_MAX = 30
SUGGESTIONS_MAX = 50
SUGGEST_GROUP_MAX = 50  # одинаковый ключ у большего числа людей — не предложение, а шум
HANDLE_MIN = 4
EXAMPLE_TEXT_MAX = 300
EXAMPLES_PER_KIND = 3
NO_NAME = "(без имени)"

NotFound = queries.NotFound


class NotPerson(Exception):
    """Контакт не человек (E2): `code` — `is_me` или `not_person`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Invalid(Exception):
    """Нарушение правил B5 — 422."""


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `queries._sql`: текст запроса — литерал в вызове, подставляются только доверенные
    фрагменты."""
    return template.format(schema=schema, **parts)


def _in(values) -> str:
    return ",".join("?" for _ in values)


def fold(value: str | None) -> str:
    """Сравнение без регистра и с `ё` = `е` (E4)."""
    return (value or "").casefold().replace("ё", "е")


# --- контакты ------------------------------------------------------------------------------------------

_CONTACT_SELECT = (
    "SELECT c.id, c.account_id, a.source, a.title, c.ext_id, c.name, c.handle, c.email, c.person_id, "
    "c.person_manual, c.person_name, c.is_me, {visible} AS visible{stats_columns} "
    "FROM {schema}.contact c LEFT JOIN main.account a ON a.id = c.account_id {stats_join}"
    "WHERE {where} ORDER BY c.id"
)
_VISIBLE = "EXISTS (SELECT 1 FROM {schema}.message vm WHERE vm.sender_contact_id = c.id AND vm.hidden = 0)"
_STATS_COLUMNS = (
    ", s.known, s.last_ts, s.seen_ts, s.in_total, s.out_total, s.in_30d, s.out_30d, s.stats_json, "
    "s.computed_ts"
)
_STATS_KEYS = (
    "known",
    "last_ts",
    "seen_ts",
    "in_total",
    "out_total",
    "in_30d",
    "out_30d",
    "stats_json",
    "computed_ts",
)
_CONTACT_KEYS = (
    "id",
    "account_id",
    "source",
    "account_title",
    "ext_id",
    "name",
    "handle",
    "email",
    "person_id",
    "person_manual",
    "person_name",
    "is_me",
    "visible",
)


def _load_contacts(
    conn,
    *,
    where: str = "1=1",
    params: tuple | list = (),
    schemas: tuple[str, ...] = SCHEMAS,
    visible_only: bool = False,
    with_stats: bool = False,
) -> list[dict]:
    """Контакты обоих файлов (сначала главный, внутри — по id). `where` — доверенный фрагмент, алиас `c`."""
    out: list[dict] = []
    for schema in schemas:
        visible = _sql(schema, _VISIBLE)
        sql = _sql(
            schema,
            _CONTACT_SELECT,
            visible=visible,
            stats_columns=_STATS_COLUMNS if with_stats else "",
            stats_join=(
                _sql(schema, "LEFT JOIN {schema}.contact_stats s ON s.contact_id = c.id ")
                if with_stats
                else ""
            ),
            where=f"({where}) AND {visible}" if visible_only else where,
        )
        keys = _CONTACT_KEYS + (_STATS_KEYS if with_stats else ())
        for row in conn.execute(sql, list(params)):
            item = dict(zip(keys, row, strict=True))
            item["schema"] = schema
            out.append(item)
    return out


def _mail_identity(config_json: str | None) -> set[str]:
    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        return set()
    if not isinstance(config, dict):
        return set()
    out = set()
    address = str(config.get("address") or "").strip().lower()
    if address:
        out.add(address)
    aliases = config.get("aliases") or []
    if isinstance(aliases, list):
        out.update(str(a).strip().lower() for a in aliases if str(a).strip())
    return out


def own_addresses(conn) -> set[str]:
    """П8: адреса контактов `is_me` почтовых аккаунтов + адрес и `aliases` из конфига почтовых аккаунтов."""
    out: set[str] = set()
    for (config_json,) in conn.execute("SELECT config_json FROM main.account WHERE source = 'mail'"):
        out |= _mail_identity(config_json)
    for schema in SCHEMAS:
        for (ext_id,) in conn.execute(
            _sql(
                schema,
                "SELECT c.ext_id FROM {schema}.contact c JOIN main.account a ON a.id = c.account_id "
                "WHERE a.source = 'mail' AND c.is_me = 1",
            )
        ):
            value = (ext_id or "").strip().lower()
            if EMAIL_RE.match(value):
                out.add(value)
    return out


def _slack_emails(conn) -> dict[int, dict[str, str]]:
    """B2/C1: почты людей Slack из справочника `slack_people` каждого аккаунта Slack."""
    out: dict[int, dict[str, str]] = {}
    for (account_id,) in conn.execute("SELECT id FROM main.account WHERE source = 'slack'"):
        emails: dict[str, str] = {}
        for item in queries.account_meta_list(conn, account_id, "slack_people"):
            user_id, email = item.get("id"), item.get("email")
            if isinstance(user_id, str) and isinstance(email, str) and email.strip():
                emails[user_id] = email.strip().lower()
        out[account_id] = emails
    return out


def _mm_people(conn, account_ids: Iterable[int] | None = None) -> dict[int, dict[str, dict]]:
    """Ф40-1b E.2: справочник `mm_people` аккаунтов Mattermost (всех или названных): id человека -> запись
    `{id, username, name, email, bot, deleted}` — как её публикует коннектор."""
    if account_ids is None:
        account_ids = [r[0] for r in conn.execute("SELECT id FROM main.account WHERE source = 'mattermost'")]
    return {account_id: nr.mm_people(conn, account_id) for account_id in account_ids}


def _mm_item(contact: dict, mm_people: dict[int, dict[str, dict]]) -> dict | None:
    return mm_people.get(contact["account_id"], {}).get(contact["ext_id"])


def _email_for(
    contact: dict,
    slack_emails: dict[int, dict[str, str]],
    mm_people: dict[int, dict[str, dict]] | None = None,
) -> str | None:
    """B2: почта контакта. Почта — `ext_id`, если это адрес (трекеры `jira:…` — нет); Slack и Mattermost —
    из справочника аккаунта (пропал или сервер скрыл почту — прежнее значение, без догадок по имени);
    прочие источники — без изменений."""
    source = contact["source"]
    if source == "mail":
        value = (contact["ext_id"] or "").strip().lower()
        return value if EMAIL_RE.match(value) else None
    if source == "slack":
        return slack_emails.get(contact["account_id"], {}).get(contact["ext_id"]) or contact["email"]
    if source == "mattermost":
        item = _mm_item(contact, mm_people or {})
        email = item.get("email") if item is not None else None
        if isinstance(email, str) and email.strip():
            return email.strip().lower()
    return contact["email"]


def _not_person(
    conn,
    contact: dict,
    email: str | None,
    own: set[str],
    mm_people: dict[int, dict[str, dict]] | None = None,
) -> str | None:
    """B1 + П3 + П8: причина «не человек» или `None`. Ф40-1b: бот Mattermost — только по справочнику
    `mm_people` (по виду id его не узнать); `mm_people=None` — справочник аккаунта читается здесь."""
    if contact["is_me"]:
        return "is_me"
    source = contact["source"]
    ext_id = contact["ext_id"] or ""
    if source == "telegram":
        if ext_id.startswith("-"):
            return "chat"
        handle = fold((contact["handle"] or "").strip().lstrip("@"))
        if ext_id == "777000" or handle.endswith("bot"):
            return "bot"
    if source == "slack" and (ext_id.startswith("B") or ext_id == "USLACKBOT"):
        return "bot"
    if source == "mattermost":
        if mm_people is None:
            mm_people = _mm_people(conn, [contact["account_id"]])
        item = _mm_item(contact, mm_people)
        if item is not None and item.get("bot") is True:
            return "bot"
    if email and email in own:
        return "own"
    row = conn.execute(
        _sql(
            contact["schema"],
            "SELECT 1 FROM {schema}.message WHERE sender_contact_id = ? AND is_bot = 1 LIMIT 1",
        ),
        (contact["id"],),
    ).fetchone()
    return "bot" if row is not None else None


def _new_person(conn, now_ms: int) -> int:
    return conn.execute("INSERT INTO main.person (created_ts) VALUES (?)", (now_ms,)).lastrowid


def _update_contact(conn, contact: dict, **fields) -> None:
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        _sql(contact["schema"], "UPDATE {schema}.contact SET {sets} WHERE id = ?", sets=sets),
        [*fields.values(), contact["id"]],
    )
    contact.update(fields)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _not_same_pairs(conn) -> set[tuple[int, int]]:
    return {(a, b) for a, b in conn.execute("SELECT a, b FROM main.person_not_same")}


def _first_name(contacts: list[dict]) -> str | None:
    """П5: имя человека, заданное владельцем, — первое непустое `person_name` (сначала главный файл)."""
    for contact in sorted(contacts, key=lambda c: c["id"]):
        if contact.get("person_name"):
            return contact["person_name"]
    return None


# --- B: проход связывания ---------------------------------------------------------------------------------


def link_all(conn, now_ms: int) -> dict:
    """B1–B4 одной транзакцией писателя. Возвращает счётчики (без ПДн) для журнала."""
    reconcile_person_seq(conn)  # П9: и на ходу — висящие ссылки не должны совпасть с новым человеком
    own = own_addresses(conn)
    slack_emails = _slack_emails(conn)
    mm_people = _mm_people(conn)
    contacts = _load_contacts(conn)
    persons = {row[0] for row in conn.execute("SELECT id FROM main.person")}
    counts = {"created": 0, "cleared": 0, "moved": 0, "removed": 0}
    dangling: dict[int, int] = {}
    for contact in contacts:
        email = _email_for(contact, slack_emails, mm_people)
        if email != contact["email"]:
            _update_contact(conn, contact, email=email)
        reason = _not_person(conn, contact, email, own, mm_people)
        contact["is_person"] = reason is None
        if reason is not None:
            if (
                contact["person_id"] is not None
                or contact["person_manual"]
                or contact["person_name"] is not None
            ):
                _update_contact(conn, contact, person_id=None, person_manual=0, person_name=None)
                counts["cleared"] += 1
            continue
        person_id = contact["person_id"]
        if person_id is not None and person_id in persons:
            continue
        if person_id is None:
            new_id = _new_person(conn, now_ms)
        else:
            # П9: строки человека нет (main откатился, work закоммитился) — новый человек на всю группу.
            new_id = dangling.get(person_id)
            if new_id is None:
                new_id = dangling[person_id] = _new_person(conn, now_ms)
                person_facts.move(conn, person_id, new_id)  # Ф24-1: факты висящего id — новому человеку
        persons.add(new_id)
        _update_contact(conn, contact, person_id=new_id)
        counts["created"] += 1
    counts["moved"] = _merge_by_email(conn, [c for c in contacts if c["is_person"]])
    counts["removed"] = cleanup(conn)
    return counts


def _merge_by_email(conn, contacts: list[dict]) -> int:
    """B3: контакты-люди с одной почтой из обоих файлов — один человек (кроме ручных и «разных»)."""
    not_same = _not_same_pairs(conn)
    by_person: dict[int, list[dict]] = defaultdict(list)
    groups: dict[str, list[dict]] = defaultdict(list)
    for contact in contacts:
        by_person[contact["person_id"]].append(contact)
        if contact["email"]:
            groups[contact["email"]].append(contact)
    moved = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        manual = {m["person_id"] for m in members if m["person_manual"]}
        if len(manual) > 1:
            continue  # владелец развёл ручные контакты по разным людям — группу не трогать
        # Ревью Ф21-1a: без ручных — старейший человек группы (наименьший `person_id`), а не человек
        # наименьшего контакта: контакты главного файла младше рабочих (id work >= 2**40), и новое письмо
        # уводило бы рабочий контакт от старого человека вместе с его именем и парами «разные».
        target = manual.pop() if manual else min(m["person_id"] for m in members)
        for member in members:
            current = member["person_id"]
            if member["person_manual"] or current == target or _pair(current, target) in not_same:
                continue
            name = _first_name(by_person[target])
            by_person[current].remove(member)
            _update_contact(conn, member, person_id=target, person_name=name)
            by_person[target].append(member)
            moved += 1
            if not person_facts.has_any_contacts(conn, current):
                # Ф24-1 П1: прежний человек опустел (его уберёт B4) — его факты переезжают к цели.
                person_facts.move(conn, current, target)
    return moved


def cleanup(conn) -> int:
    """B4: люди без контактов в обоих файлах, их пары «разные» и (Ф24-1) их факты в обоих файлах."""
    removed = conn.execute(
        "DELETE FROM main.person "
        "WHERE NOT EXISTS (SELECT 1 FROM main.contact c WHERE c.person_id = person.id) "
        "AND NOT EXISTS (SELECT 1 FROM work.contact w WHERE w.person_id = person.id)"
    ).rowcount
    conn.execute(
        "DELETE FROM main.person_not_same WHERE a NOT IN (SELECT id FROM main.person) "
        "OR b NOT IN (SELECT id FROM main.person)"
    )
    person_facts.remove_orphans(conn)
    return removed


def _person_exists(conn, person_id: int) -> bool:
    return conn.execute("SELECT 1 FROM main.person WHERE id = ?", (person_id,)).fetchone() is not None


def contact_person(conn, contact_id: int) -> int | None:
    """E2 `of-contact` на чтении: `person_id` контакта (`None` — человека ещё нет, его создаст
    `ensure_person`). Нет контакта или у него нет не скрытых сообщений (П10) — `NotFound`; `is_me`/не человек
    — `NotPerson`."""
    rows = _load_contacts(conn, where="c.id = ?", params=(contact_id,), schemas=(db_of(contact_id),))
    if not rows:
        raise NotFound(f"контакт {contact_id} не найден")
    contact = rows[0]
    email = _email_for(contact, {})
    reason = _not_person(conn, contact, email, own_addresses(conn))
    if reason == "is_me":
        raise NotPerson("is_me")
    if reason is not None:
        raise NotPerson("not_person")
    if not contact["visible"]:
        raise NotFound(f"контакт {contact_id} не найден")
    person_id = contact["person_id"]
    return person_id if person_id is not None and _person_exists(conn, person_id) else None


def ensure_person(conn, contact_id: int, now_ms: int) -> int:
    """E2: человек контакта — тот же разбор B1, что у прохода; нет человека — создаётся сразу (в писателе)."""
    person_id = contact_person(conn, contact_id)
    if person_id is not None:
        return person_id
    reconcile_person_seq(conn)
    person_id = _new_person(conn, now_ms)
    conn.execute(
        _sql(db_of(contact_id), "UPDATE {schema}.contact SET person_id = ? WHERE id = ?"),
        (person_id, contact_id),
    )
    return person_id


# --- B5: ручные действия владельца --------------------------------------------------------------------------


def _person_contact_rows(conn, person_id: int) -> list[dict]:
    return _load_contacts(conn, where="c.person_id = ?", params=(person_id,))


def _set_person_contacts(conn, person_id: int, **fields) -> None:
    sets = ", ".join(f"{name} = ?" for name in fields)
    for schema in SCHEMAS:
        conn.execute(
            _sql(schema, "UPDATE {schema}.contact SET {sets} WHERE person_id = ?", sets=sets),
            [*fields.values(), person_id],
        )


def merge(conn, into: int, from_ids: list[int]) -> None:
    """B5 «Объединить»: контакты `from` -> `into`, весь итоговый человек — ручной, имя — `into`, иначе первое
    из `from`; пары «разные» переносятся на `into`, пара самих объединяемых снимается."""
    from_ids = list(dict.fromkeys(from_ids))
    if not from_ids or into in from_ids:
        raise Invalid("from: непустой список других людей")
    for person_id in (into, *from_ids):
        if not _person_exists(conn, person_id):
            raise NotFound(f"человек {person_id} не найден")
    name = _first_name(_person_contact_rows(conn, into))
    if name is None:
        for person_id in from_ids:
            name = _first_name(_person_contact_rows(conn, person_id))
            if name is not None:
                break
    marks = _in(from_ids)
    for schema in SCHEMAS:
        conn.execute(
            _sql(
                schema, "UPDATE {schema}.contact SET person_id = ? WHERE person_id IN ({marks})", marks=marks
            ),
            [into, *from_ids],
        )
    _set_person_contacts(conn, into, person_manual=1, person_name=name)
    for person_id in from_ids:
        person_facts.move(conn, person_id, into)  # Ф24-1: факты и состояние — к `into`, затем предел 50
    conn.execute(_sql("main", "DELETE FROM main.person WHERE id IN ({marks})", marks=marks), from_ids)
    rows = conn.execute(
        _sql("main", "SELECT a, b, ts FROM main.person_not_same WHERE a IN ({m}) OR b IN ({m})", m=marks),
        [*from_ids, *from_ids],
    ).fetchall()
    conn.execute(
        _sql("main", "DELETE FROM main.person_not_same WHERE a IN ({m}) OR b IN ({m})", m=marks),
        [*from_ids, *from_ids],
    )
    gone = set(from_ids)
    for a, b, ts in rows:
        a2 = into if a in gone else a
        b2 = into if b in gone else b
        if a2 == b2:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO main.person_not_same (a, b, ts) VALUES (?, ?, ?)", (*_pair(a2, b2), ts)
        )


def split(conn, contact_ids: list[int], now_ms: int) -> int:
    """B5 «Разъединить»: контакты одного человека (не все) — в нового; ручные обе стороны, пара — «разные»."""
    contact_ids = list(dict.fromkeys(contact_ids))
    if not contact_ids:
        raise Invalid("contact_ids: пусто")
    rows = []
    for contact_id in contact_ids:
        found = _load_contacts(conn, where="c.id = ?", params=(contact_id,), schemas=(db_of(contact_id),))
        if not found:
            raise NotFound(f"контакт {contact_id} не найден")
        rows.append(found[0])
    persons = {row["person_id"] for row in rows}
    if None in persons or len(persons) != 1:
        raise Invalid("контакты — одного человека")
    old = persons.pop()
    if not _person_exists(conn, old):
        raise NotFound(f"человек {old} не найден")
    all_ids = {row["id"] for row in _person_contact_rows(conn, old)}
    if all_ids <= set(contact_ids):
        raise Invalid("нельзя отделить все контакты человека")
    new_id = _new_person(conn, now_ms)
    for row in rows:
        _update_contact(conn, row, person_id=new_id, person_manual=1, person_name=None)
    _set_person_contacts(conn, old, person_manual=1)
    conn.execute(
        "INSERT OR IGNORE INTO main.person_not_same (a, b, ts) VALUES (?, ?, ?)",
        (*_pair(old, new_id), now_ms),
    )
    return new_id


def rename(conn, person_id: int, name: str | None) -> None:
    """B5 «Имя» (П5): строка 1–100 знаков после `strip` всем контактам человека или `None` — вернуть
    автоматическое."""
    if not _person_exists(conn, person_id):
        raise NotFound(f"человек {person_id} не найден")
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= NAME_MAX:
            raise Invalid(f"name: 1–{NAME_MAX} знаков")
    # Ревью Ф21-1a: переименование — тоже ручное решение владельца: склейка по почте эти контакты не двигает.
    _set_person_contacts(conn, person_id, person_name=name, person_manual=1)


def mark_not_same(conn, a: int, b: int, now_ms: int) -> None:
    """B5 «Нет, разные»."""
    if a == b:
        raise Invalid("a и b — разные люди")
    for person_id in (a, b):
        if not _person_exists(conn, person_id):
            raise NotFound(f"человек {person_id} не найден")
    conn.execute(
        "INSERT OR IGNORE INTO main.person_not_same (a, b, ts) VALUES (?, ?, ?)", (*_pair(a, b), now_ms)
    )


# --- E: чтение --------------------------------------------------------------------------------------------


def _activity(contact: dict) -> int:
    return int(contact.get("in_total") or 0) + int(contact.get("out_total") or 0)


def display_name(contacts: list[dict]) -> str:
    """E3: имя, заданное владельцем; иначе имя контакта с наибольшим `in + out` (равенство — более свежий
    `last_ts`, затем меньший id); нет имён — ник, затем почта, затем «(без имени)»."""
    manual = _first_name(contacts)
    if manual:
        return manual
    ordered = sorted(contacts, key=lambda c: (-_activity(c), -(c.get("last_ts") or 0), c["id"]))
    for field in ("name", "handle", "email"):
        for contact in ordered:
            value = (contact.get(field) or "").strip()
            if value:
                return value
    return NO_NAME


def _token_projects(principal: Principal) -> frozenset[int] | None:
    return principal.project_ids


def _closed_channels(conn, principal: Principal) -> frozenset[int]:
    """Ф54-2 (П2): служебные лички с ботом хаба — bearer-токенам в карточке человека не показываются."""
    from . import service_bot as service_bot_mod

    return frozenset() if principal.is_ui else service_bot_mod.channel_ids(conn)


def _channel_allowed(principal: Principal, project_id: int | None) -> bool:
    """Тот же смысл, что `queries._scope_predicate`: список проектов токена — только они; без списка — всё."""
    allowed = _token_projects(principal)
    return allowed is None or (project_id is not None and project_id in allowed)


def _channels_info(conn, channel_ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    by_schema: dict[str, list[int]] = defaultdict(list)
    for channel_id in channel_ids:
        if isinstance(channel_id, int) and not isinstance(channel_id, bool):
            by_schema[db_of(channel_id)].append(channel_id)
    for schema, ids in by_schema.items():
        rows = conn.execute(
            _sql(
                schema,
                "SELECT c.id, c.title, c.kind, c.project_id FROM {schema}.channel c "
                "WHERE c.id IN ({marks}) AND {visible}",
                marks=_in(ids),
                visible=threads_mod.channel_visible_sql(schema, "c"),
            ),
            ids,
        ).fetchall()
        for cid, title, kind, project_id in rows:
            out[cid] = {"title": title, "kind": kind, "project_id": project_id}
    return out


def _projects(conn, principal: Principal, project_ids: set[int]) -> list[dict]:
    """П6: `{id, title}` — название только у проектов токена (UI — у всех)."""
    if not project_ids:
        return []
    ids = sorted(project_ids)
    names = dict(
        conn.execute(_sql("main", "SELECT id, name FROM main.project WHERE id IN ({m})", m=_in(ids)), ids)
    )
    allowed = _token_projects(principal)
    return [
        {"id": pid, "title": names.get(pid) if allowed is None or pid in allowed else None}
        for pid in ids
        if pid in names
    ]


def _merged(contacts: list[dict]) -> dict | None:
    items = [stats_mod.load(c.get("stats_json")) for c in contacts]
    items = [item for item in items if item is not None]
    return stats_mod.merge(items) if items else None


def _examples(conn, principal: Principal, merged: dict) -> list[dict]:
    """E2: образцы по ids стиля — перечитываются (скрытое, удалённое, очищенное — нет) в пределах проектов
    токена; до трёх последних каждого вида."""
    out: list[dict] = []
    closed = _closed_channels(conn, principal)
    for kind in ("chat", "mail"):
        ids = merged["style"][kind]["examples"]
        found: list[dict] = []
        by_schema: dict[str, list[int]] = defaultdict(list)
        for message_id in ids:
            by_schema[db_of(message_id)].append(message_id)
        for schema, schema_ids in by_schema.items():
            rows = conn.execute(
                _sql(
                    schema,
                    "SELECT m.id, m.channel_id, m.ts, m.text, c.project_id FROM {schema}.message m "
                    "JOIN {schema}.channel c ON c.id = m.channel_id WHERE m.id IN ({marks}) AND m.hidden = 0 "
                    "AND m.deleted = 0 AND m.purged = 0",
                    marks=_in(schema_ids),
                ),
                schema_ids,
            ).fetchall()
            for message_id, channel_id, ts, text, project_id in rows:
                if not _channel_allowed(principal, project_id) or not text or channel_id in closed:
                    continue
                found.append(
                    {
                        "message_id": message_id,
                        "channel_id": channel_id,
                        "ts": ts,
                        "kind": kind,
                        "text": text.strip()[:EXAMPLE_TEXT_MAX],
                    }
                )
        found.sort(key=lambda e: (-e["ts"], -e["message_id"]))
        out.extend(found[:EXAMPLES_PER_KIND])
    return out


def slack_id(contact: dict) -> str | None:
    """Ф27-1 B: id человека Slack (`U…`/`W…`) — сессии упоминают его `<@id>` в черновике; прочим `None`."""
    ext_id = contact.get("ext_id")
    if contact.get("source") == "slack" and isinstance(ext_id, str) and _SLACK_ID_RE.match(ext_id):
        return ext_id
    return None


def _sources(contacts: list[dict]) -> list[dict]:
    out: list[dict] = []
    for contact in contacts:
        item = {"source": contact["source"], "handle": contact["handle"], "email": contact["email"]}
        if (sid := slack_id(contact)) is not None:
            item["slack_id"] = sid  # Ф27-1 B
        if item not in out:
            out.append(item)
    return out


def _brief(conn, principal: Principal, person_id: int, contacts: list[dict]) -> dict:
    """E4 `PersonBrief`."""
    merged = _merged(contacts)
    project_ids: set[int] = set()
    reply_me = reply_them = None
    if merged is not None:
        info = _channels_info(conn, [c[0] for c in merged["channels"]])
        project_ids = {i["project_id"] for i in info.values() if i["project_id"] is not None}
        if merged["reply_me"]["samples"]:
            reply_me = stats_mod.quantile(merged["reply_me"]["samples"], 0.5)
        if merged["reply_them"]["samples"]:
            reply_them = stats_mod.quantile(merged["reply_them"]["samples"], 0.5)
    last = [c["last_ts"] for c in contacts if c.get("last_ts") is not None]
    return {
        "id": person_id,
        "name": display_name(contacts),
        "known": any(c.get("known") for c in contacts),
        "sources": _sources(contacts),
        "projects": _projects(conn, principal, project_ids),
        "last_ts": max(last) if last else None,
        "in_30d": sum(int(c.get("in_30d") or 0) for c in contacts),
        "out_30d": sum(int(c.get("out_30d") or 0) for c in contacts),
        "reply_me_median_ms": reply_me,
        "reply_them_median_ms": reply_them,
    }


def card(conn, principal: Principal, person_id: int) -> dict:
    """E2: карточка человека. Нет человека или у него нет видимых контактов (П10) — `NotFound`."""
    if not _person_exists(conn, person_id):
        raise NotFound(f"человек {person_id} не найден")
    contacts = _load_contacts(
        conn, where="c.person_id = ?", params=(person_id,), visible_only=True, with_stats=True
    )
    if not contacts:
        raise NotFound(f"человек {person_id} не найден")
    merged = _merged(contacts)
    names: list[str] = []
    for contact in contacts:
        if contact["name"] and contact["name"] not in names:
            names.append(contact["name"])
    channels: list[dict] = []
    project_ids: set[int] = set()
    examples: list[dict] = []
    stats = None
    if merged is not None:
        closed = _closed_channels(conn, principal)
        info = _channels_info(conn, [c[0] for c in merged["channels"]])
        for cid, n_in, n_out, n_seen in merged["channels"]:
            channel = info.get(cid)
            if channel is None:
                continue
            if channel["project_id"] is not None:
                project_ids.add(channel["project_id"])
            if not _channel_allowed(principal, channel["project_id"]) or cid in closed:
                continue
            channels.append(
                {
                    "channel_id": cid,
                    "title": channel["title"],
                    "kind": channel["kind"],
                    "project_id": channel["project_id"],
                    "in": n_in,
                    "out": n_out,
                    "seen": n_seen,
                }
            )
        examples = _examples(conn, principal, merged)
        stats = stats_mod.summary(merged)
    computed = [c["computed_ts"] for c in contacts if c.get("computed_ts") is not None]
    return {
        "id": person_id,
        "name": display_name(contacts),
        "name_manual": _first_name(contacts) is not None,
        "names": names,
        "known": any(c.get("known") for c in contacts),
        "contacts": [
            {
                "contact_id": c["id"],
                "account_id": c["account_id"],
                "source": c["source"],
                "account_title": c["account_title"],
                "name": c["name"],
                "handle": c["handle"],
                "email": c["email"],
                "manual": bool(c["person_manual"]),
                **({"slack_id": sid} if (sid := slack_id(c)) is not None else {}),  # Ф27-1 B
            }
            for c in contacts
        ],
        "projects": _projects(conn, principal, project_ids),
        "channels": channels,
        "stats": stats,
        "examples": examples,
        "stats_ts": max(computed) if computed else None,
        "facts": person_facts.card_facts(conn, principal, person_id),  # Ф24-1 A3
    }


def _people_contacts(conn) -> dict[int, list[dict]]:
    """Видимые (П10) контакты-люди обоих файлов со статистикой, по людям (висящие ссылки — мимо)."""
    persons = {row[0] for row in conn.execute("SELECT id FROM main.person")}
    grouped: dict[int, list[dict]] = defaultdict(list)
    for contact in _load_contacts(conn, where="c.person_id IS NOT NULL", visible_only=True, with_stats=True):
        if contact["person_id"] in persons:
            grouped[contact["person_id"]].append(contact)
    return grouped


def list_people(conn, principal: Principal, *, q: str | None, scope: str, limit: int, offset: int) -> dict:
    """E4: список и поиск. Сборка в Python; выборки `stats_json` разбираются только для отданной страницы."""
    needle = fold(q.strip()) if q and q.strip() else None
    rows = []
    for person_id, contacts in _people_contacts(conn).items():
        known = any(c.get("known") for c in contacts)
        if scope == "known" and not known:
            continue
        if needle is not None:
            fields = [c.get(f) for c in contacts for f in ("name", "handle", "email", "person_name")]
            if not any(needle in fold(value) for value in fields if value):
                continue
        last = [c["last_ts"] for c in contacts if c.get("last_ts") is not None]
        seen = [c["seen_ts"] for c in contacts if c.get("seen_ts") is not None]
        has_stats = any(c.get("computed_ts") is not None for c in contacts)
        rows.append(
            (person_id, contacts, max(last) if last else None, max([*last, *seen], default=None), has_stats)
        )
    if scope == "known":
        rows.sort(key=lambda r: (-(r[2] or 0), -r[0]))
    else:
        rows.sort(key=lambda r: (0 if r[4] else 1, -(r[3] or 0), -r[0]))
    page = rows[offset : offset + limit]
    return {"items": [_brief(conn, principal, r[0], r[1]) for r in page], "total": len(rows)}


def channel_people(conn, principal: Principal, channel_id: int, now_ms: int) -> list[dict]:
    """E4 `GET /channels/{id}/people`: люди, писавшие в беседе за окно (кроме меня и ботов), топ-30 по числу.
    Видимость беседы — как `GET /channels/{id}` (`queries.get_channel`)."""
    queries.get_channel(conn, principal, channel_id)
    schema = db_of(channel_id)
    rows = conn.execute(
        _sql(
            schema,
            "SELECT c.person_id, COUNT(*), MAX(m.ts) FROM {schema}.message m "
            "JOIN {schema}.contact c ON c.id = m.sender_contact_id "
            "WHERE m.channel_id = ? AND m.hidden = 0 AND m.deleted = 0 AND m.is_me = 0 AND m.is_bot = 0 "
            "AND m.kind <> 'event' AND m.ts >= ? AND c.person_id IS NOT NULL GROUP BY c.person_id",
        ),
        (channel_id, now_ms - stats_mod.WINDOW_MS),
    ).fetchall()
    if not rows:
        return []
    grouped = _people_contacts(conn)
    out = [
        {"person_id": person_id, "name": display_name(grouped[person_id]), "n": n, "last_ts": last_ts}
        for person_id, n, last_ts in rows
        if person_id in grouped
    ]
    out.sort(key=lambda r: (-r["n"], -(r["last_ts"] or 0), r["person_id"]))
    return out[:CHANNEL_PEOPLE_MAX]


# --- E6: предложения склейки ----------------------------------------------------------------------------

_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def name_key(name: str | None) -> str | None:
    """E6: имя без регистра, `ё` -> `е`, без знаков, транслитом в латиницу, слова отсортированы; меньше двух
    слов — `None`."""
    value = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in fold(name))
    words = value.split()
    if len(words) < 2:
        return None
    return " ".join(sorted("".join(_TRANSLIT.get(ch, ch) for ch in word) for word in words))


def _handle_key(contact: dict) -> str | None:
    """E6: ник Telegram/логин Slack/`username` Mattermost без `@`, локальная часть почты; от четырёх
    знаков, без регистра."""
    source = contact["source"]
    if source in ("telegram", "slack", "mattermost"):
        value = (contact["handle"] or "").strip().lstrip("@")
    elif source == "mail":
        address = contact["email"] or ""
        value = address.split("@", 1)[0] if "@" in address else ""
    else:
        return None
    value = value.casefold()
    return value if len(value) >= HANDLE_MIN else None


def suggestions(conn, principal: Principal) -> list[dict]:
    """E6: пары разных людей с совпавшим нормализованным именем или ником — контакты из разных аккаунтов, хотя
    бы один знакомый, не «разные»; до 50, свежие первыми."""
    grouped = _people_contacts(conn)
    not_same = _not_same_pairs(conn)
    indexes: dict[str, dict[str, set[int]]] = {"name": defaultdict(set), "handle": defaultdict(set)}
    for person_id, contacts in grouped.items():
        for contact in contacts:
            for value in (contact["name"], contact.get("person_name")):
                key = name_key(value)
                if key:
                    indexes["name"][key].add(person_id)
            key = _handle_key(contact)
            if key:
                indexes["handle"][key].add(person_id)

    def _known(person_id: int) -> bool:
        return any(c.get("known") for c in grouped[person_id])

    def _last(person_id: int) -> int:
        values = [c.get(k) for c in grouped[person_id] for k in ("last_ts", "seen_ts")]
        return max((v for v in values if v is not None), default=0)

    pairs: dict[tuple[int, int], str] = {}
    for reason in ("name", "handle"):
        for members in indexes[reason].values():
            if len(members) < 2 or len(members) > SUGGEST_GROUP_MAX:
                continue
            for a, b in itertools.combinations(sorted(members), 2):
                if (a, b) in pairs or (a, b) in not_same:
                    continue
                accounts_a = {c["account_id"] for c in grouped[a]}
                accounts_b = {c["account_id"] for c in grouped[b]}
                if accounts_a & accounts_b or not (_known(a) or _known(b)):
                    continue
                pairs[(a, b)] = reason
    ordered = sorted(pairs.items(), key=lambda item: (-max(_last(item[0][0]), _last(item[0][1])), item[0]))
    return [
        {
            "a": _brief(conn, principal, a, grouped[a]),
            "b": _brief(conn, principal, b, grouped[b]),
            "reason": reason,
        }
        for (a, b), reason in ordered[:SUGGESTIONS_MAX]
    ]
