"""Ф14-2 A5 (`docs/architecture.md`): поиск «Кому» формы «Новое сообщение» —
`GET /api/v1/recipients?q=` (только UI-сессия, маршрут — `routes/drafts.py`).

Ответ — не больше `LIMIT` строк: сначала беседы (не больше `CHANNELS_MAX`), затем люди.
- Беседа — неархивная, не заготовка, не удалённая в хабе, в которую можно писать (`can_reply` не `false`:
  трекерные уведомления почты — нет); совпадение — по названию; порядок — `last_msg_ts DESC`.
- Человек — из `contact` аккаунта (не `is_me`) и справочников аккаунта (`tg_contacts`, `slack_people` —
  `queries.account_meta_list`): Telegram — числовой id пользователя, Slack — `U…`/`W…`, почта — адрес
  (ящик «только на приём» не предлагается). Совпадение — по имени, `handle`, адресу; повтор (тот же аккаунт и
  id из таблицы и справочника) — одной строкой. Человек Telegram, у которого с аккаунтом есть настоящая
  личка, несёт её `channel_id` и не выводится, если эта беседа уже есть среди бесед ответа. Порядок —
  начинается с `q` выше, затем по имени.

Подстрока сравнивается без учёта регистра в Python (`casefold`): `lower()` SQLite складывает только ASCII.
Строки ответа — данные людей: в журнал и аудит не пишутся.
"""

import re

from . import draft_channels, queries
from . import new_recipient as nr
from . import outbox as outbox_mod
from . import threads as threads_mod

QUERY_MIN = 2
QUERY_MAX = 100
LIMIT = 20
CHANNELS_MAX = 12

_SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]{2,20}$")


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


def _fold(value: object) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _channels(conn, needle: str) -> list[dict]:
    found: list[dict] = []
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT c.id, c.title, c.account_id, a.source, c.kind, c.project_id, c.last_msg_ts, "
                "c.meta_json FROM {schema}.channel c JOIN main.account a ON a.id = c.account_id "
                "WHERE c.archived = 0 AND c.title IS NOT NULL AND {not_temp} AND {visible}",
                not_temp=draft_channels.not_temp_sql("c"),
                visible=threads_mod.channel_visible_sql(schema, "c"),
            )
        ).fetchall()
        for cid, title, account_id, source, kind, project_id, last_msg_ts, meta_json in rows:
            if needle not in title.casefold() or outbox_mod.channel_no_reply(meta_json):
                continue
            found.append(
                {
                    "type": "channel",
                    "channel_id": cid,
                    "title": title,
                    "account_id": account_id,
                    "source": source,
                    "kind": kind,
                    "project_id": project_id,
                    "last_msg_ts": last_msg_ts,
                }
            )
    found.sort(key=lambda c: (c["last_msg_ts"] is None, -(c["last_msg_ts"] or 0), c["channel_id"]))
    return found[:CHANNELS_MAX]


def _contacts(conn, schema: str, account_id: int) -> list[tuple[str, str | None, str | None]]:
    return conn.execute(
        _sql(
            schema,
            "SELECT ext_id, name, handle FROM {schema}.contact WHERE account_id = ? AND is_me = 0 "
            "ORDER BY id",
        ),
        (account_id,),
    ).fetchall()


def _tg_people(conn, schema: str, account_id: int) -> list[dict]:
    people: dict[int, dict] = {}
    for ext_id, name, handle in _contacts(conn, schema, account_id):
        if not isinstance(ext_id, str) or not ext_id.isascii() or not ext_id.isdigit():
            continue
        user_id = int(ext_id)
        if not 1 <= user_id <= nr.TG_USER_ID_MAX:
            continue
        people[user_id] = {"name": nr.stored_name(name), "username": nr.username_or_none(handle)}
    for item in queries.account_meta_list(conn, account_id, "tg_contacts"):
        user_id = item.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or not 1 <= user_id <= nr.TG_USER_ID_MAX:
            continue
        person = people.setdefault(user_id, {"name": None, "username": None})
        person["name"] = person["name"] or nr.tg_contact_name(item)
        person["username"] = person["username"] or nr.username_or_none(item.get("username"))
    dms = dict(
        conn.execute(
            _sql(
                schema,
                "SELECT c.ext_id, c.id FROM {schema}.channel c WHERE c.account_id = ? AND c.archived = 0 "
                "AND {not_temp}",
                not_temp=draft_channels.not_temp_sql("c"),
            ),
            (account_id,),
        ).fetchall()
    )
    out = []
    for user_id, person in people.items():
        name, username = person["name"], person["username"]
        handle = f"@{username}" if username else None
        out.append(
            {
                "name": name,
                "handle": handle,
                "to": {"kind": "tg_user", "user_id": user_id, "name": name, "username": username},
                "channel_id": dms.get(str(user_id)),
                "_match": (name, handle, username),
            }
        )
    return out


def _slack_people(conn, schema: str, account_id: int) -> list[dict]:
    people: dict[str, dict] = {}
    for ext_id, name, handle in _contacts(conn, schema, account_id):
        if isinstance(ext_id, str) and _SLACK_USER_RE.match(ext_id):
            people[ext_id] = {
                "name": nr.stored_name(name),
                "handle": handle if isinstance(handle, str) and handle else None,
                "extra": (),
            }
    for item in queries.account_meta_list(conn, account_id, "slack_people"):
        user_id = item.get("id")
        if not isinstance(user_id, str) or not _SLACK_USER_RE.match(user_id):
            continue
        person = people.setdefault(user_id, {"name": None, "handle": None, "extra": ()})
        person["name"] = person["name"] or nr.slack_person_name(item)
        person["extra"] = (item.get("real_name"), item.get("display_name"))
    return [
        {
            "name": person["name"],
            "handle": person["handle"],
            "to": {"kind": "slack_users", "users": [user_id]},
            "channel_id": None,
            "_match": (person["name"], person["handle"], *person["extra"]),
        }
        for user_id, person in people.items()
    ]


def _mm_people(conn, schema: str, account_id: int) -> list[dict]:
    """Ф40-1b E.3: люди Mattermost — только из справочника `mm_people` (не боты, не удалённые, не я); ник —
    `username`. Почта в поиске не участвует и в ответ не попадает."""
    out = []
    for user_id, item in nr.mm_addressable(conn, account_id).items():
        name = nr.mm_person_name(item)
        username = nr.stored_name(item.get("username"))
        handle = f"@{username}" if username else None
        out.append(
            {
                "name": name,
                "handle": handle,
                "to": {"kind": "mm_users", "users": [user_id]},
                "channel_id": None,
                "_match": (name, handle, username),
            }
        )
    return out


def _mail_people(conn, schema: str, account_id: int) -> list[dict]:
    out = []
    for ext_id, name, handle in _contacts(conn, schema, account_id):
        if not isinstance(ext_id, str) or "@" not in ext_id:
            continue
        try:
            address = nr.parse_email(ext_id)
        except nr.Invalid:
            continue
        name = nr.stored_name(name)
        out.append(
            {
                "name": name,
                "handle": handle if isinstance(handle, str) and handle else address,
                "to": address,
                "channel_id": None,
                "_match": (name, handle, address),
            }
        )
    return out


def _people(conn, needle: str) -> list[dict]:
    accounts = conn.execute("SELECT id, source, db, config_json FROM main.account ORDER BY id").fetchall()
    found: list[dict] = []
    for account_id, source, schema, config_json in accounts:
        if source == "telegram":
            people = _tg_people(conn, schema, account_id)
        elif source == "slack":
            people = _slack_people(conn, schema, account_id)
        elif source == "mattermost":
            people = _mm_people(conn, schema, account_id)
        elif source == "mail" and nr.mail_send_enabled(config_json):
            people = _mail_people(conn, schema, account_id)
        else:
            continue
        for person in people:
            fields = [_fold(v) for v in person.pop("_match")]
            if not any(needle in f for f in fields if f):
                continue
            prefix = any(f.startswith(needle) or f.lstrip("@").startswith(needle) for f in fields if f)
            found.append(
                {
                    "type": "person",
                    "account_id": account_id,
                    "source": source,
                    **person,
                    "_prefix": prefix,
                }
            )
    found.sort(key=lambda p: (not p["_prefix"], _fold(p["name"] or p["handle"]), p["account_id"]))
    for person in found:
        del person["_prefix"]
    return found


def search(conn, query: str) -> list[dict]:
    """A5: строки ответа `GET /recipients` (см. докстринг модуля); `query` — уже проверенный `strip()`."""
    needle = query.casefold()
    channels = _channels(conn, needle)
    shown = {c["channel_id"] for c in channels}
    people = [p for p in _people(conn, needle) if p["channel_id"] is None or p["channel_id"] not in shown]
    return channels + people[: LIMIT - len(channels)]
