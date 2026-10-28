"""Фильтр подписки на проект по аккаунтам (Ф38-1, docs/architecture.md): форма и
правило «слышит ли подписка аккаунт». Чистые функции без БД — одно определение для ядра (`subs`,
`chan_subs`), дежурной (`wake/daemon.py`) и текстов MCP.

Форма — `{"only": [..]}` («только эти аккаунты») или `{"except": [..]}` («все, кроме этих»): целые ≥ 1,
уникальные, по возрастанию. Нет фильтра — `None`. Кривое значение (не объект, незнакомый ключ, пустой список,
не числа) — тоже «фильтра нет»: подписка слышит всё.
"""

import json

ONLY = "only"
EXCEPT = "except"
KINDS = (ONLY, EXCEPT)
ACCOUNTS_MAX = 20


def is_account_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def clean(value) -> dict | None:
    """Значение поля `accounts` (из строки подписки, из ответа ядра) -> фильтр строгой формы или `None`."""
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((kind, ids),) = value.items()
    if kind not in KINDS or not isinstance(ids, list) or not ids:
        return None
    if not all(is_account_id(x) for x in ids):
        return None
    return {kind: sorted(set(ids))}


def parse(raw: str | None) -> dict | None:
    """`session_sub.accounts_json` -> фильтр или `None` (и на кривом JSON — без исключения)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    return clean(value)


def dump(kind: str, ids) -> str:
    """Фильтр -> `accounts_json` (числа уникальные, по возрастанию)."""
    return json.dumps({kind: sorted(set(ids))})


def listens(spec, account_id) -> bool:
    """Единственное место правила: слышит ли подписка с фильтром `spec` (значение поля `accounts`) беседу
    аккаунта `account_id`. Фильтра нет, он кривой или аккаунт беседы неизвестен — слышит."""
    spec = clean(spec)
    if spec is None or not is_account_id(account_id):
        return True
    if ONLY in spec:
        return account_id in spec[ONLY]
    return account_id not in spec[EXCEPT]
