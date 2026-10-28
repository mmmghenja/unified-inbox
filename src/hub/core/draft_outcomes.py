"""Автор черновика сессии и исход черновика — автору (Ф39, docs/architecture.md главнее текста выше них).

- Автор (B2): у строки `origin='api'`, созданной запросом с `session`, пишутся `outbox.author_sub_id` —
  живая подписка сессии на проект (`subs.author_sub`) — и `outbox.author_role` (роль словами). Роль сессия
  назначает себе сама: подпись — сведение, прав не даёт; на авто-отправку (`autosend.try_send`) запись не
  влияет.
- «Слышит ли сессия беседу» (B4 + П7): `heard` — чем именно слышит, одно из `HEARD`.
- Исход (B5): таблица `main.draft_outcome` (`0055_main.sql`). Строку пишет ТОЛЬКО ручка владельца (UI) в своей
  транзакции — подтвердил, поправил, отклонил, снял после подтверждения. Сигнал подписке — сама строка
  (образец — `reminders.py`): один предикат на счёт и выдачу (`_signal_pred`, П4). Адрес — подписка автора
  или её линия (П3, как входящие агента в `peer_msgs._inbox_scope`).
- В сигнале и в аудите — только числа и постоянные слова; текстов и ролей там нет. Беседа берётся из строки
  outbox при выдаче (П5).

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`.
"""

import sqlite3

from . import chan_subs, draft_channels
from . import subs as subs_mod
from .audit import write_audit
from .db import db_of

OUTCOMES = ("approved", "edited", "declined", "withdrawn")
# Исход старше суток не выдаётся никогда: сессии он уже ни к чему.
OUTCOME_TTL_MS = 24 * 60 * 60 * 1000
# П7: чем сессия слышит беседу — по порядку предпочтения; `no` — ничем.
HEARD = ("channel", "project", "personal", "cards", "no")


# --- автор (B2) --------------------------------------------------------------------------------------------


def set_author(
    conn: sqlite3.Connection, outbox_id: int, *, token_id: int, session, project_id: int | None
) -> subs_mod.SubRec | None:
    """B2: записать автора строке outbox, только что созданной ЭТИМ запросом (зовущий проверил: строка
    `origin='api'`, не повтор по ключу идемпотентности). `project_id` — проект беседы или `None`. У сессии
    нет живой подписки на проект (или процесс мёртв) — ничего не пишется, `None`."""
    sub = subs_mod.author_sub(conn, token_id=token_id, session=session, project_id=project_id)
    if sub is None:
        return None
    conn.execute(
        f"UPDATE {db_of(outbox_id)}.outbox SET author_role = ?, author_sub_id = ? WHERE id = ?",  # noqa: S608
        (subs_mod.author_role(sub), sub.id, outbox_id),
    )
    return sub


# --- слышит ли сессия беседу (B4 + П7) ---------------------------------------------------------------------


def heard(conn: sqlite3.Connection, *, token_id: int, session, channel_id: int) -> str | None:
    """B4 + П7: чем сессия `session` слышит беседу — первое подходящее из `HEARD`; у беседы-заготовки `new:…`
    — `None` (подписаться на неё нельзя, подсказка «подпишись» не к месту: поля в ответе нет).

    - `channel` — у неё живая подписка на эту беседу;
    - `project` / `personal` / `cards` — живая подписка на проект беседы: режим `messages` с фильтром `all`,
      он же с фильтром `personal`, режим карточек. Подписка не `direct`, её фильтр аккаунтов слышит аккаунт
      беседы, беседа и проект не заглушены;
    - `no` — ничего из этого (и когда процесса сессии уже нет)."""
    row = conn.execute(
        f"SELECT c.project_id, c.account_id, c.muted, COALESCE(p.muted, 0), c.ext_id "  # noqa: S608
        f"FROM {db_of(channel_id)}.channel c LEFT JOIN main.project p ON p.id = c.project_id WHERE c.id = ?",
        (channel_id,),
    ).fetchone()
    if row is None or draft_channels.is_temp_ext_id(row[4]):
        return None
    if not subs_mod.is_alive(session.pid, session.proc_start, getattr(session, "host", None)):
        return "no"
    key = chan_subs.session_key(token_id, session.pid, session.proc_start, session.session_id)
    if chan_subs.live_of_session(conn, channel_id, key) is not None:
        return "channel"
    project_id, account_id, channel_muted, project_muted = row[:4]
    if project_id is None or channel_muted or project_muted:
        return "no"
    rows = conn.execute(
        f"SELECT {subs_mod._SUB_COLUMNS} FROM main.session_sub WHERE ended_ts IS NULL AND project_id = ? "  # noqa: S608
        "AND direct = 0 AND api_token_id = ? AND session_pid = ? AND session_proc_start = ? "
        "AND session_id = ? ORDER BY id",
        (project_id, *key),
    ).fetchall()
    found: set[str] = set()
    for rec in (subs_mod.SubRec(*r) for r in rows):
        if not subs_mod.listens_account(rec, account_id):
            continue
        if rec.mode == "messages":
            found.add("personal" if rec.msg_filter == "personal" else "project")
        else:
            found.add("cards")
    return next((kind for kind in HEARD if kind in found), "no")


# --- исход (B5) --------------------------------------------------------------------------------------------


def record(conn: sqlite3.Connection, outbox_id: int, outcome: str, *, now_ms: int) -> bool:
    """B5: записать исход черновика его автору. Строка outbox — `origin='api'`, с `author_sub_id` и без
    `auto_role` (ушедшее без подтверждения владельца исхода не имеет); линия — из подписки автора
    (`main.session_sub`: проект, `role_key`, `role_no`). Иначе ничего. Зовут только ручки владельца (UI), в той же
    транзакции, что и сам переход строки."""
    if outcome not in OUTCOMES:
        raise ValueError(f"draft_outcomes.record: исход {outcome!r}")
    row = conn.execute(
        f"SELECT origin, author_sub_id, auto_role FROM {db_of(outbox_id)}.outbox WHERE id = ?",  # noqa: S608
        (outbox_id,),
    ).fetchone()
    if row is None or row[0] != "api" or row[1] is None or row[2] is not None:
        return False
    line = conn.execute(
        "SELECT project_id, role_key, role_no FROM main.session_sub WHERE id = ?", (row[1],)
    ).fetchone()
    if line is None:
        return False
    conn.execute(
        "INSERT INTO main.draft_outcome (outbox_id, author_sub_id, project_id, role_key, role_no, outcome, "
        "created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (outbox_id, row[1], line[0], line[1], line[2], outcome, now_ms),
    )
    return True


def _signal_pred(sub, now_ms: int) -> tuple[str, list]:
    """П4: ЕДИНСТВЕННОЕ условие «исход пора отдать подписке `sub`» — и для счёта, и для выдачи: не отдан, не
    старше `OUTCOME_TTL_MS`, адресован ей. Адрес (П3): сама подписка автора (`author_sub_id` — закрывает и
    смену роли на месте) ИЛИ её линия:

    - подписка с ролью в настоящем проекте — `(project_id, role_key, role_no)` автора;
    - подписка с ролью в проекте 0 — та же тройка И та же папка сессии, что у подписки автора (вне проектов
      линии нет: `peer_msgs._is_line`, `subs._role_source` с `cwd`);
    - подписка без роли — только `author_sub_id`.

    Ревью: по линии исход уходит, только когда подписки автора уже нет (`ended_ts`): пока автор жив, исход
    ждёт его самого — иначе автор, сменивший роль на месте, уступил бы исход сессии, занявшей его прежнюю
    линию."""
    where = "d.signaled_ts IS NULL AND d.created_ts > ? AND (d.author_sub_id = ?"
    params: list = [now_ms - OUTCOME_TTL_MS, sub.id]
    if sub.role_key is not None:
        where += (
            " OR (d.project_id = ? AND d.role_key = ? AND d.role_no = ? AND NOT EXISTS (SELECT 1 "
            "FROM main.session_sub live WHERE live.id = d.author_sub_id AND live.ended_ts IS NULL)"
        )
        params += [sub.project_id, sub.role_key, sub.role_no]
        if sub.project_id == subs_mod.NO_PROJECT_ID:
            where += (
                " AND EXISTS (SELECT 1 FROM main.session_sub a WHERE a.id = d.author_sub_id "
                "AND a.session_cwd = ?)"
            )
            params.append(sub.session_cwd)
        where += ")"
    return where + ")", params


def signal_count(conn: sqlite3.Connection, sub, *, now_ms: int) -> int:
    """Читатель, для ожидания: исходы черновиков, которые пора отдать подписке."""
    where, params = _signal_pred(sub, now_ms)
    return conn.execute(
        f"SELECT COUNT(*) FROM main.draft_outcome d WHERE {where}",  # noqa: S608 — фрагмент задан кодом
        params,
    ).fetchone()[0]


def take_signals(
    conn: sqlite3.Connection,
    sub,
    *,
    now_ms: int,
    limit: int,
    api_token_id: int,
    client_name: str | None,
    request_id: str,
) -> list[dict]:
    """Отдать подписке исходы её черновиков — `{"kind": "draft", "outbox_id", "channel_id", "outcome"}`,
    каждому `signaled_ts` (сигнал отдаётся один раз). П5: беседа — из строки outbox; строки outbox уже нет —
    исход помечается отданным и пропускается. Не влезшие в `limit` остаются до следующей выдачи."""
    where, params = _signal_pred(sub, now_ms)
    rows = conn.execute(
        f"SELECT d.id, d.outbox_id, d.outcome FROM main.draft_outcome d WHERE {where} ORDER BY d.id",  # noqa: S608
        params,
    ).fetchall()
    signals: list[dict] = []
    for outcome_id, outbox_id, outcome in rows:
        chan = conn.execute(
            f"SELECT channel_id FROM {db_of(outbox_id)}.outbox WHERE id = ?",  # noqa: S608 — схема из `db_of`
            (outbox_id,),
        ).fetchone()
        if chan is not None and len(signals) >= limit:
            continue  # остаются ждать — следующей выдачей
        conn.execute(
            "UPDATE main.draft_outcome SET signal_sub_id = ?, signaled_ts = ? WHERE id = ?",
            (sub.id, now_ms, outcome_id),
        )
        if chan is None:
            continue
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            action="sub_signal",
            result="delivered",
            channel_id=chan[0],
            outbox_id=outbox_id,
            detail={"sub_id": sub.id, "kind": "draft", "outcome": outcome},
        )
        signals.append({"kind": "draft", "outbox_id": outbox_id, "channel_id": chan[0], "outcome": outcome})
    return signals
