"""Сон каналов вместе с днём проекта (Ф70-1, docs/architecture.md).

Аккаунт, который целиком идёт в один проект «по времени», засыпает, пока день этого проекта закрыт: коннектор
перестаёт получать (сам, по признаку `sleep` от ядра), отправка идёт только ручная (`outbox.lease`, раздел B).
Признак считается на лету из правил раскладки, бесед аккаунта и дня проекта — хранить нечего, миграций нет.

«Целиком» (`sole_project`): у аккаунта есть правило «весь аккаунт» (`{"all": true}`) в проект P, все прочие
его правила тоже ведут в P, и ни одна НЕ архивная беседа аккаунта (кроме бесед-заготовок `new:…`) не лежит вне
P — ни без проекта, ни ручной раскладкой в другой проект. Беседы, «удалённые у себя» (`hidden_before_ts`),
считаются: новое сообщение вернёт такую беседу в её проект — значит, аккаунт ещё нужен тому проекту.

Только чтение; время — параметром."""

import json

from . import draft_channels
from . import workday as workday_mod

# Источники, чей коннектор умеет спать (`sleep_enabled = True`: Slack, Mattermost, почта Gmail и обычный
# IMAP — оба `source='mail'`, поддельный). Остальные (Telegram, WhatsApp, лента `feed`) не спят: ядро не
# помечает их спящими — `/status`, интерфейс и `outbox.lease` их не трогают. Сверку с классами держит тест.
SLEEP_SOURCES = frozenset({"slack", "mattermost", "mail", "fake"})


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в mapping.py/outbox.py: схема и доверенные фрагменты подставляются здесь."""
    return template.format(schema=schema, **parts)


def _rule_project(conn, account_id: int) -> int | None:
    """Проект, куда ведут ВСЕ правила аккаунта, если среди них есть «весь аккаунт»; иначе `None`."""
    rows = conn.execute(
        "SELECT match_json, project_id FROM main.map_rule WHERE account_id = ?", (account_id,)
    ).fetchall()
    projects = {project_id for _match, project_id in rows}
    if len(projects) != 1:
        return None
    has_all = False
    for match_json, _project_id in rows:
        try:
            match = json.loads(match_json)
        except ValueError, TypeError:
            return None
        if isinstance(match, dict) and match.get("all") is True:
            has_all = True
    return next(iter(projects)) if has_all else None


def _channels_only_in(conn, schema: str, account_id: int, project_id: int) -> bool:
    """Нет ни одной не архивной беседы аккаунта (кроме заготовок) вне `project_id`."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.channel c WHERE c.account_id = ? AND c.archived = 0 AND {not_temp} "
            "AND (c.project_id IS NULL OR c.project_id <> ?) LIMIT 1",
            not_temp=draft_channels.not_temp_sql("c"),
        ),
        (account_id, project_id),
    ).fetchone()
    return row is None


def _account(conn, account_id: int) -> tuple[str, str] | None:
    row = conn.execute("SELECT db, source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1]) if row is not None else None


def sole_project(conn, account_id: int) -> int | None:
    """Проект, в который аккаунт идёт ЦЕЛИКОМ, иначе `None` (определение — докстринг модуля)."""
    account = _account(conn, account_id)
    if account is None:
        return None
    schema, _source = account
    project_id = _rule_project(conn, account_id)
    if project_id is None:
        return None
    if not _channels_only_in(conn, schema, account_id, project_id):
        return None
    return project_id


def asleep(conn, account_id: int, now_ms: int) -> dict | None:
    """`{project_id, since_ts}` — аккаунт спит: он целиком в проекте P, у P включён рабочий день и день
    закрыт (`since_ts` — время закрытия). Иначе `None`. Источник не из `SLEEP_SOURCES` не спит.

    Порядок проверок — от дешёвой к дорогой: правила, день проекта и только потом беседы аккаунта (ручка
    ожидания коннектора зовёт это на каждую запись в базу)."""
    del now_ms  # день закрыт или открыт не по часам, а кнопкой; параметр — ради единообразия
    account = _account(conn, account_id)
    if account is None:
        return None
    schema, source = account
    if source not in SLEEP_SOURCES:
        return None
    project_id = _rule_project(conn, account_id)
    if project_id is None:
        return None
    since_ts = workday_mod.project_closed_ts(conn, project_id)
    if since_ts is None:
        return None
    if not _channels_only_in(conn, schema, account_id, project_id):
        return None
    return {"project_id": project_id, "since_ts": since_ts}


def last_sleep(conn, account_id: int) -> tuple[int, int] | None:
    """Ф70-1: последний закончившийся сон аккаунта `(закрытие, открытие)` — день проекта, в который аккаунт
    идёт целиком сейчас, был закрыт и открыт (кнопкой или выключением «по времени»); иначе `None`. Зовёт
    `outbox.lease` только для строки, опоздавшей по старому правилу. Источник не из `SLEEP_SOURCES`
    не спит."""
    account = _account(conn, account_id)
    if account is None:
        return None
    schema, source = account
    if source not in SLEEP_SOURCES:
        return None
    project_id = _rule_project(conn, account_id)
    if project_id is None:
        return None
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    slept = workday_mod.slept_of(row[0]) if row is not None else None
    if slept is None or not _channels_only_in(conn, schema, account_id, project_id):
        return None
    return slept
