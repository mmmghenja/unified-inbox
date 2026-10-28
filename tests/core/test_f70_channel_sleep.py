"""Ф70-1 (docs/architecture.md, B, E): кто спит (`conn_sleep.sole_project`/`asleep`) и
выдача outbox при сне — только ручные строки владельца без графика и срока, ждущие не держат беседу, после «Начать
день» уходит остальное (`lease` = `has_leasable`)."""

import datetime as dt
import json
import random

import pytest

from hub.core import conn_sleep, mapping, outbox_ops, outbox_queries, workday
from hub.core.outbox import enqueue, lease
from hub.core.security import Principal

OWNER = "connector:1"
NOW = 1_760_000_000_000
AUDIT = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r"}


def _account(db, *, source="slack", schema="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, title, db, config_json) VALUES (?, 'a', ?, ?)",
                (source, schema, json.dumps({})),
            ).lastrowid
        )
    )


def _project(db, name) -> int:
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES (?)", (name,)).lastrowid
    )


def _token(db, kind="ui") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', ?, 'x')", (kind,)
            ).lastrowid
        )
    )


def _rule(db, account_id, project_id, match) -> int:
    return db.write_sync(
        lambda conn: mapping.add_rule(
            conn, account_id=account_id, project_id=project_id, match=match, priority=None, now_ms=NOW
        )[0]
    )


def _channel(db, account_id, ext_id, *, schema="main", kind="dm") -> int:
    """Новая беседа — авто-раскладка по правилам аккаунта (как приём)."""

    def _fn(conn):
        cid = conn.execute(
            f"INSERT INTO {schema}.channel "  # noqa: S608
            "(account_id, ext_id, kind, my_msg_count, first_seen_ts, map_source) "
            "VALUES (?, ?, ?, 1, 0, 'auto')",
            (account_id, ext_id, kind),
        ).lastrowid
        mapping.recompute_channel(conn, schema, cid, NOW)
        return cid

    return db.write_sync(_fn)


def _set(db, channel_id, sql, params=()) -> None:
    db.write_sync(
        lambda conn: conn.execute(f"UPDATE main.channel SET {sql} WHERE id = ?", (*params, channel_id))
    )  # noqa: S608


def _workday(db, project_id, *, open_: bool) -> None:
    def _fn(conn):
        workday.set_workday(conn, project_id, True, now_ms=NOW)
        workday.set_day(conn, project_id, open_, now_ms=NOW, audit=AUDIT)

    db.write_sync(_fn)


def _sole(db, account_id):
    with db.reader() as conn:
        return conn_sleep.sole_project(conn, account_id)


def _asleep(db, account_id):
    with db.reader() as conn:
        return conn_sleep.asleep(conn, account_id, NOW)


@pytest.fixture
def stand(db):
    account_id = _account(db)
    p1 = _project(db, "Globex")
    p2 = _project(db, "Личное")
    _rule(db, account_id, p1, {"all": True})
    c1 = _channel(db, account_id, "C1")
    c2 = _channel(db, account_id, "C2")
    return {"account": account_id, "p1": p1, "p2": p2, "c1": c1, "c2": c2, "token": _token(db)}


# --- A. Кто спит ------------------------------------------------------------------------------------------


def test_sole_project_whole_account(db, stand):
    assert _sole(db, stand["account"]) == stand["p1"]


def test_sole_project_rule_into_other_project_is_not_whole(db, stand):
    _rule(db, stand["account"], stand["p2"], {"channel_ext_id": "C9"})
    assert _sole(db, stand["account"]) is None


def test_sole_project_more_rules_into_same_project_still_whole(db, stand):
    _rule(db, stand["account"], stand["p1"], {"channel_ext_id": "C9"})
    assert _sole(db, stand["account"]) == stand["p1"]


def test_sole_project_manual_channel_elsewhere_is_not_whole(db, stand):
    _set(db, stand["c2"], "project_id = ?, map_source = 'manual'", (stand["p2"],))
    assert _sole(db, stand["account"]) is None


def test_sole_project_channel_without_project_is_not_whole(db, stand):
    _set(db, stand["c2"], "project_id = NULL, map_source = 'manual'")
    assert _sole(db, stand["account"]) is None


def test_sole_project_ignores_archived_and_draft_channels(db, stand):
    _set(db, stand["c2"], "project_id = ?, map_source = 'manual', archived = 1", (stand["p2"],))
    temp = _channel(db, stand["account"], "new:abc")
    _set(db, temp, "project_id = NULL, map_source = 'manual'")
    assert _sole(db, stand["account"]) == stand["p1"]


def test_sole_project_needs_all_rule(db):
    account_id = _account(db)
    p1 = _project(db, "P")
    _rule(db, account_id, p1, {"channel_ext_id": "C1"})
    _channel(db, account_id, "C1")
    assert _sole(db, account_id) is None


def test_sole_project_work_schema(db):
    account_id = _account(db, schema="work")
    p1 = _project(db, "P")
    p2 = _project(db, "Q")
    _rule(db, account_id, p1, {"all": True})
    _channel(db, account_id, "W1", schema="work")
    assert _sole(db, account_id) == p1
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE work.channel SET project_id = ?, map_source = 'manual' WHERE ext_id = 'W1'", (p2,)
        )
    )
    assert _sole(db, account_id) is None


def test_asleep_follows_the_day(db, stand):
    assert _asleep(db, stand["account"]) is None  # проект не «по времени»
    _workday(db, stand["p1"], open_=True)
    assert _asleep(db, stand["account"]) is None
    _workday(db, stand["p1"], open_=False)
    assert _asleep(db, stand["account"]) == {"project_id": stand["p1"], "since_ts": NOW}
    # Закрыт день ДРУГОГО проекта — не наш.
    _workday(db, stand["p1"], open_=True)
    _workday(db, stand["p2"], open_=False)
    assert _asleep(db, stand["account"]) is None


def test_asleep_not_for_partial_account_or_feed(db, stand):
    _workday(db, stand["p1"], open_=False)
    _set(db, stand["c2"], "project_id = ?, map_source = 'manual'", (stand["p2"],))
    assert _asleep(db, stand["account"]) is None
    feed = _account(db, source="feed")
    _rule(db, feed, stand["p1"], {"all": True})
    assert _asleep(db, feed) is None


@pytest.mark.parametrize("source", ["telegram", "whatsapp"])
def test_source_without_sleep_never_asleep_and_sends(db, source):
    """Ревью Ф70-1 п.1 (`.runs/critic-f701/test_f701_review.py`, обратное утверждение): коннектор Telegram и
    WhatsApp сон не поддерживает — ядро не помечает аккаунт спящим, `/status` и `lease` его не трогают."""
    account_id = _account(db, source=source)
    project_id = _project(db, "P")
    _rule(db, account_id, project_id, {"all": True})
    channel_id = _channel(db, account_id, "100")
    tok = _token(db)
    _workday(db, project_id, open_=False)
    row = _draft(db, channel_id, tok, "агент")
    _approve(db, row, tok, "agent")
    assert _sole(db, account_id) == project_id  # целиком в проекте, день закрыт
    assert _asleep(db, account_id) is None  # а спящим не помечен
    assert _has(db, account_id) is True
    assert _leased(db, account_id) == [row]


# --- B. Отправка при сне ------------------------------------------------------------------------------------


def _ui_send(db, channel_id, token_id, text) -> int:
    return db.write_sync(
        lambda conn: enqueue(
            conn, channel_id=channel_id, text=text, origin="ui", api_token_id=token_id, idempotency_key=None,
            now_ms=NOW,
        )
    ).outbox_id  # fmt: skip


def _draft(db, channel_id, token_id, text, *, send_at=None) -> int:
    return db.write_sync(
        lambda conn: enqueue(
            conn, channel_id=channel_id, text=text, origin="api", api_token_id=token_id, idempotency_key=None,
            now_ms=NOW, send_at=send_at,
        )
    ).outbox_id  # fmt: skip


def _approve(db, outbox_id, token_id, source) -> None:
    principal = Principal(token_id=token_id, kind="ui", scopes=frozenset(), project_ids=None)
    audit = {**AUDIT, "api_token_id": token_id}
    db.write_sync(
        lambda conn: outbox_ops.approve_draft(
            conn, outbox_id, principal=principal, text=None, send_now=False, now_ms=NOW, source=source,
            audit=audit,
        )
    )  # fmt: skip


def _leased(db, account_id, now_ms=NOW) -> list[int]:
    items = db.write_sync(
        lambda conn: lease(conn, account_id=account_id, owner=OWNER, now_ms=now_ms, rng=random.Random(1))
    )
    return [item.outbox_id for item in items]


def _has(db, account_id, now_ms=NOW) -> bool:
    with db.reader() as conn:
        return outbox_queries.has_leasable(conn, account_id=account_id, now_ms=now_ms)


def _by_hand(db, outbox_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT by_hand FROM main.outbox WHERE id = ?", (outbox_id,)).fetchone()[0]


def test_by_hand_marks(db, stand):
    tok = stand["token"]
    ui_row = _ui_send(db, stand["c1"], tok, "сам")
    by_ui = _draft(db, stand["c1"], tok, "подтвердил сам")
    by_agent = _draft(db, stand["c1"], tok, "подтвердил агент")
    by_auto = _draft(db, stand["c1"], tok, "сразу да")
    _approve(db, by_ui, tok, "ui")
    _approve(db, by_agent, tok, "agent")
    _approve(db, by_auto, tok, "auto")
    assert [_by_hand(db, i) for i in (ui_row, by_ui, by_agent, by_auto)] == [1, 1, 0, 0]


def test_lease_while_asleep_only_by_hand_then_rest_after_day_opens(db, stand):
    tok = stand["token"]
    # Порядок id: ждущие строки ВПЕРЕДИ ручных в той же беседе — FIFO при сне их пропускает.
    by_agent = _draft(db, stand["c1"], tok, "подтвердил агент")
    _approve(db, by_agent, tok, "agent")
    by_auto = _draft(db, stand["c1"], tok, "сразу да")
    _approve(db, by_auto, tok, "auto")
    api_queued = _draft(db, stand["c2"], tok, "черновик в очереди")  # остаётся pending_approval
    ui_row = _ui_send(db, stand["c1"], tok, "ручная")
    by_ui = _draft(db, stand["c2"], tok, "подтвердил сам")
    _approve(db, by_ui, tok, "ui")

    _workday(db, stand["p1"], open_=False)
    assert _has(db, stand["account"]) is True
    assert _leased(db, stand["account"]) == [ui_row, by_ui]
    assert _has(db, stand["account"]) is False  # ждущие при сне не будят коннектор
    assert _leased(db, stand["account"]) == []

    _workday(db, stand["p1"], open_=True)
    assert _has(db, stand["account"]) is True
    # Строки в пути (`sending`) держат беседу — сперва отчитаемся за них.
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET status = 'sent' WHERE id IN (?, ?)", (ui_row, by_ui)
        )
    )
    assert _leased(db, stand["account"]) == [by_agent]  # c1: FIFO снова по порядку id
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.outbox SET status = 'sent' WHERE id = ?", (by_agent,))
    )
    assert _leased(db, stand["account"]) == [by_auto]
    assert api_queued not in _leased(db, stand["account"])


def test_sending_row_still_holds_channel_while_asleep(db, stand):
    tok = stand["token"]
    first = _ui_send(db, stand["c1"], tok, "первая")
    assert _leased(db, stand["account"]) == [first]  # в пути
    _workday(db, stand["p1"], open_=False)
    second = _ui_send(db, stand["c1"], tok, "вторая")
    assert _leased(db, stand["account"]) == []  # первая ещё `sending` — порядок беседы держится
    db.write_sync(lambda conn: conn.execute("UPDATE main.outbox SET status = 'sent' WHERE id = ?", (first,)))
    assert _leased(db, stand["account"]) == [second]


def test_partial_account_not_affected_by_closed_day(db, stand):
    tok = stand["token"]
    by_agent = _draft(db, stand["c1"], tok, "агент")
    _approve(db, by_agent, tok, "agent")
    _set(db, stand["c2"], "project_id = ?, map_source = 'manual'", (stand["p2"],))  # аккаунт не целиком
    _workday(db, stand["p1"], open_=False)
    assert _leased(db, stand["account"]) == [by_agent]


def test_send_at_waits_while_asleep_and_is_not_late(db, stand):
    tok = stand["token"]
    due_ms = NOW + 60 * 60 * 1000
    iso = dt.datetime.fromtimestamp(due_ms / 1000, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    at_time = _draft(db, stand["c2"], tok, "к сроку", send_at=iso)
    _approve(db, at_time, tok, "ui")  # подтвердил сам, но со сроком — при сне ждёт начала дня
    _workday(db, stand["p1"], open_=False)
    late = due_ms + 3 * 60 * 60 * 1000  # больше двух часов опоздания
    assert _has(db, stand["account"], now_ms=late) is False
    assert _leased(db, stand["account"], now_ms=late) == []
    with db.reader() as conn:
        status = conn.execute("SELECT status FROM main.outbox WHERE id = ?", (at_time,)).fetchone()[0]
    assert status == "queued"  # не «опоздал» во сне — правило опоздания решает после начала дня
    _workday(db, stand["p1"], open_=True)
    assert _leased(db, stand["account"], now_ms=due_ms) == [at_time]


# --- Ф70-1, ответ головной: срок `send_at`, пришедшийся на сон, опаздывает от пробуждения ----------------

H = 60 * 60 * 1000


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _day(db, project_id, *, open_: bool, now_ms: int) -> None:
    def _fn(conn):
        workday.set_workday(conn, project_id, True, now_ms=now_ms)
        workday.set_day(conn, project_id, open_, now_ms=now_ms, audit=AUDIT)

    db.write_sync(_fn)


def _status(db, outbox_id) -> tuple[str, str | None]:
    with db.reader() as conn:
        return conn.execute("SELECT status, error FROM main.outbox WHERE id = ?", (outbox_id,)).fetchone()


def _night_row(db, stand, due_ms) -> int:
    row = _draft(db, stand["c2"], stand["token"], "к сроку ночью", send_at=_iso(due_ms))
    _approve(db, row, stand["token"], "agent")
    return row


def test_send_at_due_in_sleep_not_late_after_day_opens(db, stand):
    due = NOW + 3 * H
    row = _night_row(db, stand, due)
    _day(db, stand["p1"], open_=False, now_ms=NOW + H)
    wake = NOW + 12 * H  # срок глубоко в ночи: к пробуждению опоздание 9 часов
    _day(db, stand["p1"], open_=True, now_ms=wake)
    assert _has(db, stand["account"], now_ms=wake + 30 * 60 * 1000) is True
    assert _leased(db, stand["account"], now_ms=wake + 30 * 60 * 1000) == [row]
    assert _status(db, row)[0] == "sending"


def test_send_at_due_in_sleep_window_counts_from_wake(db, stand):
    row = _night_row(db, stand, NOW + 3 * H)
    _day(db, stand["p1"], open_=False, now_ms=NOW + H)
    wake = NOW + 12 * H
    _day(db, stand["p1"], open_=True, now_ms=wake)
    assert _leased(db, stand["account"], now_ms=wake + 3 * H) == []  # от пробуждения прошло больше 2 ч
    assert _status(db, row) == ("failed", "send_at_late")


def test_send_at_due_in_sleep_not_late_after_workday_off(db, stand):
    row = _night_row(db, stand, NOW + 3 * H)
    _day(db, stand["p1"], open_=False, now_ms=NOW + H)
    wake = NOW + 12 * H
    db.write_sync(lambda conn: workday.set_workday(conn, stand["p1"], False, now_ms=wake))
    assert _leased(db, stand["account"], now_ms=wake + 60 * 1000) == [row]


def test_send_at_late_without_sleep_old_rule(db, stand):
    row = _night_row(db, stand, NOW + 3 * H)
    assert _has(db, stand["account"], now_ms=NOW + 6 * H) is True  # `lease` закроет опоздавшую
    assert _leased(db, stand["account"], now_ms=NOW + 6 * H) == []
    assert _status(db, row) == ("failed", "send_at_late")


def test_send_at_due_after_wake_or_late_before_sleep_old_rule(db, stand):
    tok = stand["token"]
    before = _draft(db, stand["c1"], tok, "опоздал ещё до сна", send_at=_iso(NOW + H))
    _approve(db, before, tok, "agent")
    after = _draft(db, stand["c2"], tok, "срок после пробуждения", send_at=_iso(NOW + 13 * H))
    _approve(db, after, tok, "agent")
    _day(db, stand["p1"], open_=False, now_ms=NOW + 4 * H)  # к засыпанию первая уже опоздала на 3 ч
    _day(db, stand["p1"], open_=True, now_ms=NOW + 12 * H)
    assert _leased(db, stand["account"], now_ms=NOW + 16 * H) == []
    assert _status(db, before) == ("failed", "send_at_late")
    assert _status(db, after) == ("failed", "send_at_late")
