"""Ф27-1 B (`docs/architecture.md`): упоминания людей Slack для интерфейса.

- `attach_messages` / `attach_outbox` — у строк Slack (черновик и отправленное до эха — строки ленты с
  `outbox_id`; строки очереди `/outbox`) поле `mention_names: {id: имя}` для `<@U…>` текста, найденных в
  справочнике аккаунта `slack_people`; ни одного — поля нет. Справочник читается один раз на аккаунт за вызов,
  а вызов — один на ответ.
- `mention_people` — `GET /accounts/{id}/mention_people?q=&channel_id=`: до `MENTION_PEOPLE_MAX` людей для
  подсказки `@` — сначала участники беседы (по последнему сообщению), потом справочник; совпадение по началу
  слов имени и ника, без регистра и `ё`/`е`. Только люди справочника: прочих коннектор настоящим упоминанием
  не отправит (`slack/convert.escape_outgoing`).

Имена — данные людей: в журнал и аудит не пишутся.
"""

import re
from collections.abc import Iterable

from . import new_recipient as nr
from .db import db_of
from .security import Principal

# Тот же вид упоминания, что пропускает коннектор (`slack/convert.OUTGOING_MENTION_RE`).
MENTION_RE = re.compile(r"(?<!<)<@([UW][A-Z0-9]{2,20})>(?!>)")
MENTION_PEOPLE_MAX = 8
QUERY_MAX = 100
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,20}$")
_WORD_SPLIT_RE = re.compile(r"[\s.,;:_\-()\[\]/\\|'\"«»]+")
_PARTICIPANTS_MAX = 200


class NotFound(Exception):
    pass


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


def mention_ids(text: object) -> list[str]:
    """Id из `<@ID>` текста в порядке первого появления, без повторов."""
    if not isinstance(text, str) or "<@" not in text:
        return []
    return list(dict.fromkeys(m.group(1) for m in MENTION_RE.finditer(text)))


def slack_people(conn, account_id: int) -> dict[str, dict]:
    """Справочник `slack_people` аккаунта: id -> запись (только валидные id)."""
    from . import queries  # queries импортирует этот модуль

    out: dict[str, dict] = {}
    for item in queries.account_meta_list(conn, account_id, "slack_people"):
        user_id = item.get("id")
        if isinstance(user_id, str) and _SLACK_ID_RE.match(user_id):
            out[user_id] = item
    return out


def person_name(user_id: str, item: dict) -> str:
    """Имя для показа: `display_name`, иначе `real_name`, иначе сам id."""
    return nr.slack_person_name(item) or user_id


def _slack_accounts(conn, channel_ids: Iterable[int]) -> dict[int, int]:
    """`channel_id -> account_id` только для бесед аккаунтов Slack; один запрос на схему."""
    by_schema: dict[str, set[int]] = {}
    for cid in channel_ids:
        by_schema.setdefault(db_of(cid), set()).add(cid)
    out: dict[int, int] = {}
    for schema, ids in by_schema.items():
        ordered = sorted(ids)
        for start in range(0, len(ordered), 500):
            chunk = ordered[start : start + 500]
            rows = conn.execute(
                _sql(
                    schema,
                    "SELECT c.id, c.account_id FROM {schema}.channel c JOIN main.account a "
                    "ON a.id = c.account_id WHERE a.source = 'slack' AND c.id IN ({ph})",
                    ph=",".join("?" for _ in chunk),
                ),
                chunk,
            ).fetchall()
            out.update({cid: account_id for cid, account_id in rows})
    return out


def _attach(conn, rows: list[dict]) -> None:
    candidates = [(row, ids) for row in rows if (ids := mention_ids(row.get("text")))]
    if not candidates:
        return
    accounts = _slack_accounts(conn, {row["channel_id"] for row, _ids in candidates})
    people: dict[int, dict[str, dict]] = {}
    for row, ids in candidates:
        account_id = accounts.get(row["channel_id"])
        if account_id is None:
            continue
        if account_id not in people:
            people[account_id] = slack_people(conn, account_id)
        directory = people[account_id]
        names = {uid: person_name(uid, directory[uid]) for uid in ids if uid in directory}
        if names:
            row["mention_names"] = names


def attach_messages(conn, messages: list[dict]) -> None:
    """Строки ленты: только свои с `outbox_id` (черновик, очередь, отправленное до эха). Эхо приходит уже
    разобранным `convert` (`@Имя`), входящее — тоже; их не трогаем."""
    _attach(conn, [m for m in messages if m.get("outbox_id") is not None and m.get("is_me")])


def attach_outbox(conn, rows: list[dict]) -> None:
    """Строки outbox (`GET /outbox`, `GET /outbox/{id}`)."""
    _attach(conn, rows)


def _fold(value: object) -> str:
    return value.casefold().replace("ё", "е") if isinstance(value, str) else ""


def _matches(needle: str, fields: Iterable[object]) -> bool:
    for field in fields:
        folded = _fold(field).lstrip("@")
        if not folded:
            continue
        if folded.startswith(needle):
            return True
        if any(word.startswith(needle) for word in _WORD_SPLIT_RE.split(folded) if word):
            return True
    return False


def _mm_mention_people(conn, account_id: int, participants: list[str], q: str) -> list[dict]:
    """Ф40-1b E.4: подсказка `@` для Mattermost: участники беседы первыми, потом справочник по имени."""
    directory = nr.mm_addressable(conn, account_id)
    first = [uid for uid in participants if uid in directory]
    first_set = set(first)
    rest = sorted(
        (uid for uid in directory if uid not in first_set),
        key=lambda uid: (_fold(nr.mm_person_name(directory[uid]) or uid), uid),
    )
    needle = _fold(q.strip()).lstrip("@")
    out: list[dict] = []
    for uid in first + rest:
        item = directory[uid]
        username = nr.stored_name(item.get("username"))
        if needle and not _matches(needle, (item.get("name"), username)):
            continue
        out.append({"id": uid, "name": nr.mm_person_name(item) or uid, "handle": username})
        if len(out) >= MENTION_PEOPLE_MAX:
            break
    return out


def mention_people(
    conn, principal: Principal, account_id: int, *, q: str = "", channel_id: int | None = None
) -> list[dict]:
    """`GET /accounts/{id}/mention_people`: `[{id, name, handle}]`, не больше `MENTION_PEOPLE_MAX`.

    Ф40-1b E.4: у аккаунта Mattermost — люди справочника `mm_people` (не боты, не удалённые, не я),
    `handle` — `username`; порядок тот же: участники беседы, потом справочник по имени.

    Аккаунт не Slack и не Mattermost или не существует — `NotFound`. Bearer видит аккаунт, только если у
    аккаунта есть беседа
    в проектах токена; `channel_id` — видимая принципалу беседа ЭТОГО аккаунта, иначе `NotFound`."""
    from . import queries

    row = conn.execute("SELECT source, db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None or row[0] not in ("slack", "mattermost"):
        raise NotFound(f"аккаунт {account_id} не Slack")
    source, schema = row
    if principal.project_ids is not None:
        pred, pred_params = queries._scope_predicate("project_id", principal)  # noqa: SLF001
        seen = conn.execute(
            _sql(
                schema,
                "SELECT 1 FROM {schema}.channel WHERE account_id = ? AND ({pred}) LIMIT 1",
                pred=pred,
            ),
            [account_id, *pred_params],
        ).fetchone()
        if seen is None:
            raise NotFound(f"аккаунт {account_id} не виден")
    participants: list[str] = []
    if channel_id is not None:
        try:
            queries.channel_visible(conn, principal, channel_id)
        except queries.NotFound as exc:
            raise NotFound(f"канал {channel_id} не найден") from exc
        ch_schema = db_of(channel_id)
        owner = conn.execute(
            _sql(ch_schema, "SELECT account_id FROM {schema}.channel WHERE id = ?"), (channel_id,)
        ).fetchone()
        if owner is None or owner[0] != account_id:
            raise NotFound(f"канал {channel_id} не из аккаунта {account_id}")
        participants = [
            ext_id
            for (ext_id,) in conn.execute(
                _sql(
                    ch_schema,
                    "SELECT ct.ext_id FROM {schema}.message m JOIN {schema}.contact ct "
                    "ON ct.id = m.sender_contact_id WHERE m.channel_id = ? AND m.is_me = 0 AND ct.is_me = 0 "
                    "GROUP BY ct.ext_id ORDER BY MAX(m.ts) DESC LIMIT ?",
                ),
                (channel_id, _PARTICIPANTS_MAX),
            ).fetchall()
        ]

    if source == "mattermost":
        return _mm_mention_people(conn, account_id, participants, q)
    directory = slack_people(conn, account_id)
    if not directory:
        return []
    handles = {
        ext_id: handle
        for ext_id, handle in conn.execute(
            _sql(
                schema,
                "SELECT ext_id, handle FROM {schema}.contact WHERE account_id = ? AND is_me = 0 "
                "AND handle IS NOT NULL",
            ),
            (account_id,),
        ).fetchall()
        if isinstance(handle, str) and handle
    }
    first = [uid for uid in participants if uid in directory]
    first_set = set(first)
    rest = sorted(
        (uid for uid in directory if uid not in first_set),
        # по имени; люди без имени (показ — сам id) — в конце
        key=lambda uid: (
            nr.slack_person_name(directory[uid]) is None,
            _fold(person_name(uid, directory[uid])),
            uid,
        ),
    )
    order = first + rest
    needle = _fold(q.strip()).lstrip("@")
    out: list[dict] = []
    for uid in order:
        item = directory[uid]
        handle = handles.get(uid)
        if needle and not _matches(needle, (item.get("display_name"), item.get("real_name"), handle)):
            continue
        out.append({"id": uid, "name": person_name(uid, item), "handle": handle})
        if len(out) >= MENTION_PEOPLE_MAX:
            break
    return out
