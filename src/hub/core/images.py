"""Суточные пределы и аудит картинок `POST /images` (Ф91 Р4; docs/architecture.md).

Счёт — по `main.audit` (без миграции, индекс `audit_action_ts`), действие `image`, окно — скользящие 24 ч:
- `reserved` — перед вызовом Alibaba, в транзакции писателя вместе с проверкой пределов (параллельные
  запросы не пробьют предел);
- `ok` — Alibaba сделал картинки (деньги списаны): `{project_id, role, n, model, cost_usd, missed?}`;
- `failed` — не сделано (вызов не прошёл или пришло меньше заказанного): возвращает зарезервированное.

Занято = сумма `n` у `reserved` в окне минус сумма `n` у `failed` тех же запросов (окно — по времени
резерва). После 2xx Alibaba со ссылками картинки оплачены: `ok` пишется на число ссылок, даже если скачать
не вышло. Резерв, после которого ядро упало, остаётся занятым до конца окна — предел считает его
потраченным (с запасом в безопасную сторону).

Аудит только дописывается (триггер `audit_no_update`); текста промпта в нём нет — только его длина.
Проект `0` — запрос без проекта (кука интерфейса, токен на все проекты без `project_id`): у него свой
счёт с тем же пределом проекта. Функции с `conn` зовутся внутри `Db.write()`, кроме помеченных «читатель».
"""

import sqlite3

from .audit import write_audit

ACTION = "image"
WINDOW_MS = 24 * 60 * 60 * 1000
NO_PROJECT = 0


class LimitExceeded(Exception):
    """Предел исчерпан — текст для сессии (429)."""


def usage(conn: sqlite3.Connection, *, project_id: int, now_ms: int) -> tuple[int, int]:
    """Читатель или писатель: `(занято проектом, занято всего)` за последние 24 ч.

    Окно считается по времени резерва: `failed` пишется позже своего `reserved` (после вызова до ~90 с) и
    вычитается, только пока его резерв в окне (пара по `request_id`). Иначе на краю окна отмена вышедшего
    резерва вычиталась бы из чужих — удачных картинок за сутки выходило бы больше предела."""
    since = now_ms - WINDOW_MS
    rows = conn.execute(
        "SELECT CAST(json_extract(detail_json, '$.project_id') AS INTEGER), "
        "COALESCE(SUM(CAST(json_extract(detail_json, '$.n') AS INTEGER)), 0) FROM main.audit "
        "WHERE action = ? AND result = 'reserved' AND ts > ? GROUP BY 1",
        (ACTION, since),
    ).fetchall()
    back = conn.execute(
        "SELECT CAST(json_extract(f.detail_json, '$.project_id') AS INTEGER), "
        "COALESCE(SUM(CAST(json_extract(f.detail_json, '$.n') AS INTEGER)), 0) FROM main.audit f "
        "WHERE f.action = ? AND f.result = 'failed' AND f.ts > ? AND f.request_id IN ("
        "SELECT r.request_id FROM main.audit r WHERE r.action = ? AND r.result = 'reserved' AND r.ts > ?) "
        "GROUP BY 1",
        (ACTION, since, ACTION, since),
    ).fetchall()
    project = total = 0
    for sign, part in ((1, rows), (-1, back)):
        for pid, n in part:
            total += sign * int(n)
            if int(pid or 0) == project_id:
                project += sign * int(n)
    return max(project, 0), max(total, 0)


def reserve(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    n: int,
    daily_project: int,
    daily_total: int,
    now_ms: int,
    actor: str,
    api_token_id: int | None,
    client_name: str | None,
    request_id: str,
    detail: dict,
) -> tuple[int, int]:
    """Проверить пределы и записать резерв. Вернёт остаток `(проекту, всего)` после резерва; не хватает —
    `LimitExceeded` с текстом."""
    used_project, used_total = usage(conn, project_id=project_id, now_ms=now_ms)
    if used_project + n > daily_project:
        left = max(daily_project - used_project, 0)
        raise LimitExceeded(
            f"предел картинок проекта: {daily_project} в сутки, осталось {left} — повтори позже или скажи "
            "владельцу"
        )
    if used_total + n > daily_total:
        left = max(daily_total - used_total, 0)
        raise LimitExceeded(
            f"общий предел картинок хаба: {daily_total} в сутки, осталось {left} — повтори позже или скажи "
            "владельцу"
        )
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id,
        action=ACTION,
        result="reserved",
        detail={**detail, "project_id": project_id, "n": n},
    )
    return daily_project - used_project - n, daily_total - used_total - n


def finish(
    conn: sqlite3.Connection,
    *,
    ok: bool,
    project_id: int,
    n: int,
    now_ms: int,
    actor: str,
    api_token_id: int | None,
    client_name: str | None,
    request_id: str,
    detail: dict,
) -> None:
    """Итог вызова: `ok` (строка расхода) или `failed` (резерв возвращается)."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id,
        action=ACTION,
        result="ok" if ok else "failed",
        detail={**detail, "project_id": project_id, "n": n},
    )
