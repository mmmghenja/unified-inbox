"""Ф52-2 (docs/architecture.md): проект слышит «Неразобранное» выбранных
аккаунтов — `main.project.unsorted_accounts` (JSON-список id аккаунтов, `NULL` — ничего).

Право задаёт только владелец (интерфейс, CLI). Оно даёт подпискам проекта (кроме `direct`) только СИГНАЛ по
беседам без проекта этих аккаунтов: запись сигнала (`subs.signal_for_item`, `chan_subs._signal`) и годность
при выдаче (`subs._skip_reason`, `chan_subs._project_skip_reason`) спрашивают это право здесь. Границу чтения
и пишущие ручки право не меняет.
"""

import json

from . import security, sub_accounts

ACCOUNTS_MAX = sub_accounts.ACCOUNTS_MAX


class UnsortedError(Exception):
    """Отказ записи права: маршрут отвечает 422 с этим текстом."""


def parse(raw: str | None) -> list[int]:
    """Значение столбца -> номера аккаунтов по возрастанию; `NULL` и кривое значение — пусто (не слышит)."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return []
    if not isinstance(value, list) or not all(sub_accounts.is_account_id(x) for x in value):
        return []
    return sorted(set(value))


def project_accounts(conn, project_id: int | None) -> list[int]:
    """Аккаунты, чьё «Неразобранное» слышит проект (нет проекта или права — пусто)."""
    if project_id is None:
        return []
    row = conn.execute("SELECT unsorted_accounts FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return parse(row[0]) if row is not None else []


def hears(conn, project_id: int | None, account_id) -> bool:
    """Слышит ли проект беседы без проекта аккаунта `account_id` — проверка и при записи, и при выдаче."""
    return sub_accounts.is_account_id(account_id) and account_id in project_accounts(conn, project_id)


def token_sees(conn, token_id: int | None) -> bool:
    """Правка по ревью: право действует только для подписок, чей токен САМ видит «Неразобранное»
    (`Principal.sees_unsorted` — вход владельца или профиль на все проекты со скоупом `organize`). Подписке
    проектного токена сигнал по беседе без проекта не пишется и не выдаётся: иначе подбором ключей она
    прощупывала бы беседы, которые читать не может. Читается строка токена на момент вызова."""
    if token_id is None:
        return False
    row = conn.execute(
        "SELECT kind, scopes, project_scope_json FROM main.api_token WHERE id = ?", (token_id,)
    ).fetchone()
    if row is None:
        return False
    kind, scopes, scope_json = row
    try:
        project_ids = frozenset(json.loads(scope_json)) if scope_json is not None else None
    except ValueError, TypeError:
        return False
    principal = security.Principal(
        token_id=token_id, kind=kind, scopes=frozenset((scopes or "").split()), project_ids=project_ids
    )
    return principal.sees_unsorted


def listening_projects(conn, account_id) -> list[tuple[int, bool]]:
    """Проекты, слышащие «Неразобранное» аккаунта: `(id проекта, заглушён ли проект)`, по id."""
    if not sub_accounts.is_account_id(account_id):
        return []
    rows = conn.execute(
        "SELECT id, unsorted_accounts, muted FROM main.project "
        "WHERE unsorted_accounts IS NOT NULL ORDER BY id"
    ).fetchall()
    return [(pid, bool(muted)) for pid, raw, muted in rows if account_id in parse(raw)]


def set_project(conn, project_id: int, accounts) -> list[int] | None:
    """Запись права: список номеров существующих аккаунтов (пустой — снять, столбец `NULL`). Нет проекта —
    `None`; кривой список или неизвестный аккаунт — `UnsortedError`. Возвращает записанный список."""
    if not isinstance(accounts, list) or len(accounts) > ACCOUNTS_MAX:
        raise UnsortedError(f"unsorted_accounts: список номеров аккаунтов, не больше {ACCOUNTS_MAX}")
    if not all(sub_accounts.is_account_id(x) for x in accounts):
        raise UnsortedError("unsorted_accounts: номера аккаунтов — целые от 1")
    if conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is None:
        return None
    known = {row[0] for row in conn.execute("SELECT id FROM main.account")}
    unknown = sorted(set(accounts) - known)
    if unknown:
        raise UnsortedError(f"unsorted_accounts: нет аккаунтов {', '.join(str(x) for x in unknown)}")
    ids = sorted(set(accounts))
    conn.execute(
        "UPDATE main.project SET unsorted_accounts = ? WHERE id = ?",
        (json.dumps(ids) if ids else None, project_id),
    )
    return ids
