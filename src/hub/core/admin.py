"""Записи для внутренних ручек, требующих прямого SQL (правило слоя — маршруты его не пишут): создание
аккаунта + секрета коннектора, проекта, отметка статуса аккаунта коннектором. Спецификация:
docs/architecture.md. Токены и логин-код — через уже
существующие функции `security.py`, вызываются прямо из `routes/internal.py`, отдельных обёрток здесь нет.

Все функции вызываются только внутри `Db.write()`, как и в `outbox.py`/`ingest.py`; время — параметром
`now_ms`.
"""

import json
import re
import sqlite3
import unicodedata
from typing import NamedTuple

from . import attention as attention_mod
from . import card_route as card_route_mod
from . import connector_auth
from .db import next_seq

# Подстроки в именах ключей config, похожие на секрет источника: их наличие запрещено (спека, раздел
# «Внутренние ручки») — секреты источников вводятся отдельно, через `hub auth <источник>` (кусок 1+),
# а не через `hub account add --config`. Подстрока, а не точное совпадение: реальные поля вида `api_hash`
# (Telegram) или `xoxp_token` (Slack) не совпадают ни с одним словом целиком, но безусловно секрет.
_SECRET_LIKE_WORDS = ("token", "secret", "password", "hash", "key")
# Ф84-1: имя ключа несёт слово «key», но значение — не секрет (правила склейки писем по ключу беседы)
_NOT_SECRET_KEYS = frozenset({"notify_keys"})
# То же перечисление, что CHECK таблицы account в 0001_main.sql.
_ACCOUNT_STATUSES = frozenset({"ok", "stale", "auth_required", "limited", "no_network"})


class ConfigHasSecrets(Exception):
    """`config` содержит ключ, в имени которого есть token|secret|password|hash|key — 422."""


def _check_config_safe(config: dict) -> None:
    for key in config:
        lowered = key.lower()
        if lowered in _NOT_SECRET_KEYS:
            continue
        if any(word in lowered for word in _SECRET_LIKE_WORDS):
            raise ConfigHasSecrets(f"config содержит запрещённый ключ {key!r}")


class CreatedAccount(NamedTuple):
    account_id: int
    connector_secret: str  # отдаётся ОДИН раз — тем же ответом, что и создание; в БД только его sha256


def create_account(
    conn, *, source: str, title: str | None, db: str, config: dict | None, now_ms: int
) -> CreatedAccount:
    if config:
        _check_config_safe(config)
    config_json = json.dumps(config, ensure_ascii=False) if config else None

    cur = conn.execute(
        "INSERT INTO main.account (source, title, config_json, db) VALUES (?, ?, ?, ?)",
        (source, title, config_json, db),
    )
    account_id = cur.lastrowid

    secret = connector_auth.generate_connector_secret()
    conn.execute(
        "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, ?)",
        (account_id, connector_auth.hash_secret(secret), now_ms),
    )
    return CreatedAccount(account_id=account_id, connector_secret=secret)


class InvalidProject(Exception):
    """Ф14-1 A4: имя или цвет проекта не годятся — 422 (текст уходит как есть)."""


class ProjectNameTaken(Exception):
    """Ф14-1 A4: имя занято другим проектом (без учёта регистра) — 409."""


_PROJECT_NAME_MAX = 40
# Имена псевдо-проектов и главной UI — настоящему проекту их давать нельзя (путаница во вкладках).
_RESERVED_PROJECT_NAMES = frozenset({"неразобранное", "все", "главная"})
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _check_project_name(conn, name, *, project_id: int | None) -> str:
    if not isinstance(name, str):
        raise InvalidProject("название проекта: нужна строка")
    name = name.strip()
    if not name or len(name) > _PROJECT_NAME_MAX:
        raise InvalidProject(f"название проекта: от 1 до {_PROJECT_NAME_MAX} символов")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise InvalidProject("название проекта: без управляющих символов")
    folded = name.casefold()
    if folded in _RESERVED_PROJECT_NAMES:
        raise InvalidProject(f"название «{name}» занято интерфейсом хаба")
    for other_id, other_name in conn.execute("SELECT id, name FROM main.project").fetchall():
        if other_id != project_id and isinstance(other_name, str) and other_name.strip().casefold() == folded:
            raise ProjectNameTaken(f"проект «{other_name}» уже есть (#{other_id})")
    return name


def _check_project_color(color) -> None:
    if color is not None and (not isinstance(color, str) or not _COLOR_RE.match(color)):
        raise InvalidProject("цвет проекта: #rrggbb")


def create_project(conn, *, name: str, color: str | None, workdir: str | None) -> int:
    """Ф14-1 A4: имя — `strip()`, 1..40 символов, без управляющих, не «Неразобранное»/«Все»/«Главная»
    (`InvalidProject` -> 422), не занято другим проектом без учёта регистра (`ProjectNameTaken` -> 409);
    цвет — `#rrggbb` или `None`. Те же проверки у UI и у `hub project add`. Ф66-1 (ревью Р4): политика
    проекта — таблица маршрута карточек «всё мне» (`card_route.initial_policy`)."""
    name = _check_project_name(conn, name, project_id=None)
    _check_project_color(color)
    # Ф66-1, ревью Р4: таблица маршрута карточек «всё мне» — сразу, перенос по прежним настройкам её не
    # пересчитает.
    # Ф99-8: новый проект — в конец очерёдности.
    last = conn.execute("SELECT COALESCE(MAX(sort), -1) FROM main.project").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO main.project (name, color, workdir, agent_policy_json, sort) VALUES (?, ?, ?, ?, ?)",
        (name, color, workdir, card_route_mod.initial_policy(), last + 1),
    )
    return cur.lastrowid


def set_project_order(conn, ids: list[int]) -> None:
    """Ф99-8: `project.sort` = место в списке. Список обязан совпадать с множеством проектов (без
    лишних, пропущенных и повторов) — иначе `InvalidProject` (422)."""
    existing = {r[0] for r in conn.execute("SELECT id FROM main.project")}
    if len(set(ids)) != len(ids):
        raise InvalidProject("ids: повтор проекта")
    if set(ids) != existing:
        raise InvalidProject("ids: список должен содержать все проекты, без лишних")
    for place, pid in enumerate(ids):
        conn.execute("UPDATE main.project SET sort = ? WHERE id = ?", (place, pid))


def update_project(conn, project_id: int, changes: dict) -> bool:
    """Ф14-1 A4 `PATCH /projects/{id}`: `name` и/или `color` (`None` — снять цвет); проверки как у
    `create_project`. Возвращает False, если проекта нет (маршрут -> 404). Имя проекта отдаётся в
    `GET /projects`, UI перечитывает список сам (`loadProjects`)."""
    row = conn.execute("SELECT id FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return False
    if "name" in changes:
        name = _check_project_name(conn, changes["name"], project_id=project_id)
        conn.execute("UPDATE main.project SET name = ? WHERE id = ?", (name, project_id))
    if "color" in changes:
        _check_project_color(changes["color"])
        conn.execute("UPDATE main.project SET color = ? WHERE id = ?", (changes["color"], project_id))
    return True


class InvalidWorkdir(Exception):
    """`workdir` не абсолютный путь — 422. Существование каталога и безопасность пути проверяет CLI
    (`hub project set`) и сам hub-wake: ядро не смотрит в файловую систему (спека 7c, W2)."""


def set_project_workdir(conn, project_id: int, workdir: str | None) -> bool:
    """`PATCH /admin/projects/{id}` (спека 7c, W2): рабочая папка проекта для сессии Claude
    (`hub project set ID --workdir PATH|--no-workdir`). `None` — снять. Возвращает False, если проекта нет
    (маршрут -> 404)."""
    if workdir is not None and (not workdir.startswith("/") or "\x00" in workdir):
        raise InvalidWorkdir("workdir: нужен абсолютный путь")
    row = conn.execute("SELECT id FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE main.project SET workdir = ? WHERE id = ?", (workdir, project_id))
    return True


_UNSET = object()


def set_project_attention(conn, project_id: int, *, changes: dict | None = None) -> dict | None:
    """Ф8-3 A6: политика внимания проекта — ключ `attention` в `project.agent_policy_json` (слияние: прочие
    ключи политики не трогаются). `changes` — `{вид: режим | None}` (`None` — удалить ключ вида).
    Режим каналов проекта мог смениться — `channel.seq`
    всех его каналов (main и work) растёт, UI и агенты видят это через `/sync` (A5). Возвращает новую политику
    `attention` или `None`, если проекта нет (маршрут -> 404). Неверный вид/режим — `ValueError`
    (422)."""
    normalized = attention_mod.normalize_policy_changes(changes or {})
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    try:
        policy = json.loads(row[0]) if row[0] else {}
    except ValueError:
        policy = {}
    if not isinstance(policy, dict):
        policy = {}
    current = policy.get("attention")
    attention = dict(current) if isinstance(current, dict) else {}
    for key, value in normalized.items():
        if value is None:
            attention.pop(key, None)
        else:
            attention[key] = value
    policy["attention"] = attention
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    for schema in ("main", "work"):
        ids = conn.execute(
            f"SELECT id FROM {schema}.channel WHERE project_id = ?",  # noqa: S608 — схема из списка выше
            (project_id,),
        ).fetchall()
        for (channel_id,) in ids:
            conn.execute(
                f"UPDATE {schema}.channel SET seq = ? WHERE id = ?",  # noqa: S608 — схема из списка выше
                (next_seq(conn), channel_id),
            )
    return attention


def set_account_status(conn, account_id: int, *, status: str, last_error: str | None, now_ms: int) -> None:
    """`account_id` приходит из `ConnectorPrincipal`, не из тела запроса — строка гарантированно существует
    (её нашла аутентификация). Невалидный `status` ловится CHECK-constraint'ом таблицы `account`
    (`sqlite3.IntegrityError` — маршрут переводит его в 422).

    Раздел C спеки куска 1a (с поправкой после критика): `limited` коннектор не снимает ничем, см. ниже.
    `last_ok_ts` обновляется на `ok` всегда — это пульс для `hub-watch` (`account:<id>:silent`).
    """
    if status not in _ACCOUNT_STATUSES:
        # Раньше это ловил CHECK таблицы; ветка `limited` ниже не пишет status — проверяем явно.
        raise sqlite3.IntegrityError(f"invalid status {status!r}")
    current = conn.execute("SELECT status FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if current is not None and current[0] == "limited":
        # `limited` (PeerFlood — спам-ограничение источника) коннектор не снимает НИКАКИМ статусом: иначе
        # цепочка limited -> no_network (сон, лежит прокси) -> ok молча возобновляла отправку под
        # ограничением (находка 2 критика 1a). Снимает только `resume_account` (`hub account resume`).
        # Причина (`last_error`) остаётся; пульс `ok` продолжает обновлять `last_ok_ts`.
        if status == "ok":
            conn.execute("UPDATE main.account SET last_ok_ts = ? WHERE id = ?", (now_ms, account_id))
        return
    if status == "ok":
        conn.execute(
            "UPDATE main.account SET status = ?, last_error = ?, last_ok_ts = ? WHERE id = ?",
            (status, last_error, now_ms, account_id),
        )
    else:
        conn.execute(
            "UPDATE main.account SET status = ?, last_error = ? WHERE id = ?",
            (status, last_error, account_id),
        )


def update_account_config(conn, account_id: int, changes: dict) -> dict | None:
    """`hub account set ID k=v …`: слияние верхнего уровня `config_json`: ключ со значением `None` удаляется,
    остальные заменяются целиком. Ключи, похожие на секрет, запрещены так же, как при создании. Коннектор
    читает конфиг при старте (`GET /account`) — изменение действует после его перезапуска. Возвращает новый
    конфиг или None, если аккаунта нет (маршрут переводит это в 404).

    Появилось 24.09 (живой запуск почты): ящик, подключённый `--receive-only`, после починки SMTP включается
    `send_enabled=true`; так же задаются `aliases` и `exclude_categories` почты."""
    _check_config_safe(changes)
    row = conn.execute("SELECT config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None
    config = json.loads(row[0]) if row[0] else {}
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    conn.execute(
        "UPDATE main.account SET config_json = ? WHERE id = ?",
        (json.dumps(config, ensure_ascii=False) if config else None, account_id),
    )
    return config


def resume_account(conn, account_id: int) -> bool:
    """`hub account resume ID` -> `status='ok'`, `flood_until=NULL` (раздел C) и снятый стоп первых сообщений
    новым людям `peer_flood_ts=NULL` (Ф69-1 D, П6). Возвращает False, если аккаунт не существует (маршрут
    переводит это в 404)."""
    row = conn.execute("SELECT peer_flood_ts FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return False
    if row[0] is not None:
        from . import peer_flood  # noqa: PLC0415 — peer_flood -> bot_guard -> channels: держим admin лёгким

        peer_flood.bump_held_rows(conn, account_id)  # лента узнаёт, что строки больше не держит стоп
    conn.execute(
        "UPDATE main.account SET status = 'ok', flood_until = NULL, peer_flood_ts = NULL WHERE id = ?",
        (account_id,),
    )
    return True


class AccountNotEmpty(Exception):
    """`hub account rm`: аккаунт уже подключался или у него есть данные — 409 (текст уходит как есть)."""


# Таблицы БД аккаунта (`main`|`work`) и только `main`, строка в которых значит «аккаунт не пустой».
_ACCOUNT_DATA_TABLES = (
    "channel",
    "message",
    "outbox",
    "contact",
    "channel_alias",
    "dead_letter",
    "media",
    "upload",
    "read_ack",
    "reaction_ack",
    "msg_op",
)
_ACCOUNT_MAIN_TABLES = (
    "map_rule",
    "join_request",
    "leave_request",
    "rule_request",
    "button_press",
    "group_op",  # Ф62-6
)
# Служебное самого аккаунта — уходит вместе с ним.
_ACCOUNT_OWN_TABLES = ("cursor", "account_meta")


def delete_account(conn, account_id: int) -> str | None:
    """`hub account rm ID` (владелец 04.10.2026): убрать ПУСТОЙ аккаунт — заведённый `hub auth …`/`hub account
    add`, но так и не подключённый (вход прервали до конца). Аккаунт, чей коннектор хоть раз дал пульс, и
    аккаунт с любыми данными (беседы, сообщения, отправки, контакты, правила раскладки, заявки) не
    удаляется — `AccountNotEmpty`. Возвращает источник удалённого или None, если аккаунта нет. `id`
    аккаунта — AUTOINCREMENT, повторно не выдаётся."""
    row = conn.execute(
        "SELECT source, db, last_ok_ts FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return None
    source, schema, last_ok_ts = row
    if last_ok_ts is not None:
        raise AccountNotEmpty("аккаунт уже подключался — такие не удаляются")
    busy = [
        f"{db}.{table}"
        for db, tables in ((schema, _ACCOUNT_DATA_TABLES), ("main", _ACCOUNT_MAIN_TABLES))
        for table in tables
        if conn.execute(
            f"SELECT 1 FROM {db}.{table} WHERE account_id = ? LIMIT 1",  # noqa: S608 — имена из констант
            (account_id,),
        ).fetchone()
    ]
    if busy:
        raise AccountNotEmpty(f"у аккаунта есть данные ({', '.join(busy)}) — такие не удаляются")
    for table in _ACCOUNT_OWN_TABLES:
        conn.execute(f"DELETE FROM {schema}.{table} WHERE account_id = ?", (account_id,))  # noqa: S608
    conn.execute("DELETE FROM main.connector_secret WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM main.account WHERE id = ?", (account_id,))
    return source
