"""Ф24-1a (docs/architecture.md): факты о людях в ядре — миграция 0038, `person_facts`
(созревшие, контекст, запись службы, правка владельца, карточка), ручки `routes/person_facts.py`, скоуп
`facts`, хуки людей. Обязательные тесты D.1–D.10 (тест D.3 — существующие тесты `people_stats` без правки
ожиданий, плюс сверка разбора адресата ниже) и поправки П1–П5, П8."""

import json
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import db as db_module
from hub.core import people, person_facts, security
from hub.core.db import WORK_ID_BASE, Db, db_of

from .conftest import BASE_URL, csrf_headers, insert_project, login_via_code, make_bearer_token
from .people_helpers import (
    DAY_MS,
    MIN_MS,
    NOW_MS,
    account,
    channel,
    contact,
    link,
    msg,
    person_of,
    recompute,
    slack_people,
)

API = "/api/v1"
HOUR_MS = 60 * MIN_MS
SECRET = "СЕКРЕТНЫЙ-ТЕКСТ-ФАКТА"


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
async def ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


@pytest.fixture
async def bearer_client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


def _facts_token(db) -> dict:
    token = make_bearer_token(db, kind="background", scopes=("read", "facts"), name="facts:worker")
    return {"Authorization": f"Bearer {token}"}


def _read_token(db, *, project_ids=None) -> dict:
    return {"Authorization": f"Bearer {make_bearer_token(db, scopes=('read',), project_ids=project_ids)}"}


def fact(db, person_id, text, *, schema="main", source="auto", ts=NOW_MS, projects=(), rejected=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {schema}.person_fact (person_id, text, source, project_ids_json, created_ts, "
                "updated_ts, rejected_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (person_id, text, source, json.dumps(list(projects)), ts, ts, rejected),
            ).lastrowid
        )
    )


def facts_of(db, person_id, *, schema="main", live=True) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            f"SELECT id, text, source, project_ids_json, updated_ts, rejected_ts FROM {schema}.person_fact "  # noqa: S608
            "WHERE person_id = ? ORDER BY id",
            (person_id,),
        ).fetchall()
    out = [
        {"id": r[0], "text": r[1], "source": r[2], "projects": json.loads(r[3]), "ts": r[4], "rejected": r[5]}
        for r in rows
    ]
    return [f for f in out if f["rejected"] is None] if live else out


def state_of(db, person_id, *, schema="main") -> dict | None:
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT cursor_ts, last_try_ts, version, fails, updated_ts FROM {schema}.person_fact_state "  # noqa: S608
            "WHERE person_id = ?",
            (person_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(("cursor_ts", "last_try_ts", "version", "fails", "updated_ts"), row, strict=True))


def put_state(db, person_id, *, schema="main", **values) -> None:
    base = {"cursor_ts": None, "last_try_ts": None, "version": 0, "fails": 0, "updated_ts": None}
    base.update(values)
    db.write_sync(
        lambda conn: conn.execute(
            f"INSERT OR REPLACE INTO {schema}.person_fact_state (person_id, cursor_ts, last_try_ts, version, "  # noqa: S608
            "fails, updated_ts) VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, *base.values()),
        )
    )


def ctx(db, person_id, schema="main", now_ms=NOW_MS) -> dict:
    with db.reader() as conn:
        return person_facts.context(conn, person_id, schema, now_ms)


def apply(db, person_id, body, schema="main", now_ms=NOW_MS) -> dict:
    full = {"cursor_ts": now_ms, "project_ids": []}
    full.update(body)
    return db.write_sync(lambda conn: person_facts.apply(conn, person_id, schema, full, now_ms))


def due(db, now_ms=NOW_MS, limit=20, start=NOW_MS - 10 * DAY_MS) -> list[dict]:
    with db.reader() as conn:
        return person_facts.due(conn, now_ms, limit, start)


@pytest.fixture
def world(db):
    """Пётр: Telegram (главный файл, личка и группа, проект 1) + Slack (рабочий файл, личка, проект 2) — один
    человек. Кира — в той же группе (третье лицо). Свежие обмены владельца с Петром в обоих файлах."""
    p1 = insert_project(db, name="Globex")
    p2 = insert_project(db, name="Секретный")
    tg = account(db, "telegram", title="Telegram")
    sl = account(db, "slack", schema="work", title="Slack")
    petr_tg = contact(db, tg, "100", "Пётр", "@petr")
    petr_sl = contact(db, sl, "U0PETR", "Petr", "@petr", schema="work")
    kira = contact(db, tg, "200", "Кира", "@kira")
    dm = channel(db, tg, "100", project_id=p1, title="Пётр")
    grp = channel(db, tg, "-500", kind="group", project_id=p1, title="Группа")
    sdm = channel(db, sl, "D1", schema="work", project_id=p2, title="Petr в Slack")
    msg(db, dm, ts=NOW_MS - 3 * DAY_MS, sender=petr_tg, text="Пётр пишет в личку")
    msg(db, dm, ts=NOW_MS - 3 * DAY_MS + 5 * MIN_MS, me=True, text="владелец отвечает в личку")
    msg(db, sdm, ts=NOW_MS - 2 * DAY_MS, sender=petr_sl, text="Petr in slack")
    msg(db, sdm, ts=NOW_MS - 2 * DAY_MS + MIN_MS, me=True, text="владелец в слаке")
    link(db)
    db.write_sync(lambda conn: people.merge(conn, person_of(db, petr_tg), [person_of(db, petr_sl)]))
    recompute(db)
    return {
        "p1": p1,
        "p2": p2,
        "tg": tg,
        "sl": sl,
        "petr": person_of(db, petr_tg),
        "petr_tg": petr_tg,
        "petr_sl": petr_sl,
        "kira": person_of(db, kira),
        "kira_contact": kira,
        "dm": dm,
        "grp": grp,
        "sdm": sdm,
    }


# --- D.1: миграция 0038 -------------------------------------------------------------------------------------


def test_d1_migration_both_files_work_ids_rerun(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 37}
    )
    old = Db(tmp_path)
    old.start()
    with old.reader() as conn:
        for schema in ("main", "work"):
            assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 37
            tables = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master")}  # noqa: S608
            assert "person_fact" not in tables
    old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    for _ in range(2):  # повторный запуск миграций — без ошибок
        new = Db(tmp_path)
        new.start()
        new.close()
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 38
                tables = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master")}  # noqa: S608
                assert {"person_fact", "person_fact_state", "person_fact_person"} <= tables
        main_id = fact(new, 1, "главный", schema="main")
        work_id = fact(new, 1, "рабочий", schema="work")
        assert main_id < WORK_ID_BASE < work_id
        assert db_of(work_id) == "work" and db_of(main_id) == "main"
        with pytest.raises(Exception, match="CHECK"):
            fact(new, 1, "плохой источник", source="model")
    finally:
        new.close()


# --- D.2: созревшие -----------------------------------------------------------------------------------------


async def test_d2_due_start_mark_and_route(bearer_client, db, world, clock):
    headers = _facts_token(db)
    r = await bearer_client.get(f"{API}/people/facts/due", headers=headers)
    assert r.status_code == 200 and r.json() == []  # без метки — пусто, метка записана
    with db.reader() as conn:
        assert person_facts.start_ts(conn) == NOW_MS
    r = await bearer_client.get(f"{API}/people/facts/due", headers=headers)
    assert r.json() == []  # обмены до метки не созревают
    dm = world["dm"]
    clock.advance_ms(2 * HOUR_MS)
    msg(db, dm, ts=NOW_MS + HOUR_MS, me=True, text="после метки")
    recompute(db, NOW_MS + HOUR_MS)
    r = await bearer_client.get(f"{API}/people/facts/due", headers=headers)
    assert r.json() == [{"person_id": world["petr"], "db": "main", "out_ts": NOW_MS + HOUR_MS}]
    r = await bearer_client.get(f"{API}/people/facts/due", params={"limit": 51}, headers=headers)
    assert r.status_code == 422


def test_d2_due_rules(db, world):
    petr = world["petr"]
    start = NOW_MS - 10 * DAY_MS
    # Оба файла Петра созрели; порядок — по out_ts от старых.
    assert [(d["person_id"], d["db"]) for d in due(db, start=start)] == [(petr, "main"), (petr, "work")]
    # Обмен до метки — нет.
    assert due(db, start=NOW_MS) == []
    # Курсор: обмен учтён — нет.
    put_state(db, petr, cursor_ts=NOW_MS - 3 * DAY_MS + 5 * MIN_MS)
    assert [d["db"] for d in due(db, start=start)] == ["work"]
    # Попытка моложе 60 мин — нет; старше — да; П5: со сбоями пауза растёт.
    put_state(db, petr, schema="work", last_try_ts=NOW_MS - 59 * MIN_MS)
    assert due(db, start=start) == []
    put_state(db, petr, schema="work", last_try_ts=NOW_MS - 60 * MIN_MS)
    assert [d["db"] for d in due(db, start=start)] == ["work"]
    put_state(db, petr, schema="work", last_try_ts=NOW_MS - 3 * HOUR_MS, fails=2)
    assert due(db, start=start) == []
    put_state(db, petr, schema="work", last_try_ts=NOW_MS - 4 * HOUR_MS, fails=2)
    assert [d["db"] for d in due(db, start=start)] == ["work"]
    put_state(db, petr, schema="work", last_try_ts=NOW_MS - 31 * HOUR_MS, fails=9)  # потолок 2^5
    assert due(db, start=start) == []
    put_state(db, petr, schema="work", last_try_ts=NOW_MS - 32 * HOUR_MS, fails=9)
    assert [d["db"] for d in due(db, start=start)] == ["work"]


def test_d2_due_excludes_incoming_only_bots_me_and_limits(db):
    tg = account(db, "telegram")
    me = contact(db, tg, "1", "Я", is_me=1)
    ivan = contact(db, tg, "300", "Иван")  # пишет владельцу, владелец ему — нет
    bot = contact(db, tg, "400", "Бот")
    many = [contact(db, tg, str(1000 + i), f"Человек {i}") for i in range(4)]
    ivan_dm = channel(db, tg, "300")
    msg(db, ivan_dm, ts=NOW_MS - DAY_MS, sender=ivan)
    bot_dm = channel(db, tg, "400")
    msg(db, bot_dm, ts=NOW_MS - DAY_MS, sender=bot, is_bot=1)
    msg(db, bot_dm, ts=NOW_MS - DAY_MS + 1, me=True, sender=me)
    for i, cid in enumerate(many):
        dm = channel(db, tg, str(1000 + i))
        msg(db, dm, ts=NOW_MS - DAY_MS, sender=cid)
        msg(db, dm, ts=NOW_MS - DAY_MS + (4 - i) * MIN_MS, me=True, sender=me)
    link(db)
    recompute(db)
    assert person_of(db, bot) is None and person_of(db, me) is None
    found = due(db)
    assert person_of(db, ivan) not in {d["person_id"] for d in found}
    assert [d["person_id"] for d in found] == [person_of(db, c) for c in reversed(many)]
    assert [d["person_id"] for d in due(db, limit=2)] == [person_of(db, c) for c in reversed(many)][:2]


# --- D.3: разбор адресата вынесен из статистики -------------------------------------------------------------


def test_d3_messages_same_addressing_as_stats(db, world):
    """`_File.messages` — тот же D1, что у статистики: ответ владельца в личке адресован Петру, чужое — «Владельцу»."""
    from hub.core import people_stats

    with db.reader() as conn:
        file = people_stats._File(conn, "main", NOW_MS)
        ch = conn.execute(
            "SELECT ch.id, ch.kind, ch.ext_id, ch.account_id, "
            "json_extract(ch.meta_json, '$.attrs.slack_type'), a.source FROM main.channel ch "
            "LEFT JOIN main.account a ON a.id = ch.account_id WHERE ch.id = ?",
            (world["dm"],),
        ).fetchone()
        msgs, personal, mail = file.messages(ch)
    assert personal is True and mail is False
    assert [(m["me"], m["to_me"], m["targets"]) for m in msgs] == [
        (False, True, []),
        (True, False, [world["petr_tg"]]),
    ]


# --- D.4: контекст ------------------------------------------------------------------------------------------


def test_d4_context_only_exchange(db, world):
    grp, dm, petr_tg, kira = world["grp"], world["dm"], world["petr_tg"], world["kira_contact"]
    base = NOW_MS - DAY_MS
    petr_q = msg(db, grp, ts=base, sender=petr_tg, text="Пётр владельцу", mentions_me=1)
    kira_q = msg(db, grp, ts=base + 1, sender=kira, text="Кира пишет в группу")
    msg(db, grp, ts=base + 2, sender=kira, text="Кира владельцу", mentions_me=1)
    msg(db, grp, ts=base + 3, me=True, text="владелец Петру", reply_to=_ext(db, petr_q))
    msg(db, grp, ts=base + 4, me=True, text="владелец Кире", reply_to=_ext(db, kira_q))
    msg(db, dm, ts=base + 5, me=True, stub=True, text="заготовка outbox")
    msg(db, dm, ts=base + 6, sender=petr_tg, text="скрытое", hidden=1)
    msg(db, dm, ts=base + 7, sender=petr_tg, text="удалённое", deleted=1)
    msg(db, dm, ts=base + 8, sender=petr_tg, text="очищенное", purged=1)
    msg(db, dm, ts=base + 9, sender=petr_tg, text="событие", kind="event")
    photo = msg(db, dm, ts=base + 10, sender=petr_tg, text="", kind="file")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.message SET attachments_json = ? WHERE id = ?",
            (json.dumps([{"type": "photo"}]), photo),
        )
    )
    long_id = msg(db, dm, ts=base + 11, sender=petr_tg, text="я" * 1500)
    assert long_id
    result = ctx(db, world["petr"])
    texts = [(m["dir"], m["text"]) for m in result["messages"]]
    assert texts == [
        ("in", "Пётр пишет в личку"),
        ("out", "владелец отвечает в личку"),
        ("in", "Пётр владельцу"),
        ("out", "владелец Петру"),
        ("in", "[вложение: photo]"),
        ("in", "я" * 1000),  # обрезано до 1000
    ]
    assert result["truncated"] is False
    assert result["since_ts"] == NOW_MS - person_facts.BOOTSTRAP_MS and result["until_ts"] == NOW_MS
    assert result["project_ids"] == [world["p1"]]
    first = result["messages"][2]
    assert (
        first["channel_title"] == "Группа"
        and first["channel_kind"] == "group"
        and first["project"] == "Globex"
    )
    assert result["name"] and result["db"] == "main" and result["version"] == 0
    assert {s["source"] for s in result["sources"]} == {"telegram"}
    # Рабочий файл — только своё; главный не видит рабочих бесед и наоборот.
    work = ctx(db, world["petr"], "work")
    assert [(m["dir"], m["text"]) for m in work["messages"]] == [
        ("in", "Petr in slack"),
        ("out", "владелец в слаке"),
    ]
    assert work["project_ids"] == [world["p2"]]
    assert "Petr in slack" not in json.dumps(result, ensure_ascii=False)


def _ext(db, message_id) -> str:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT ext_id FROM {db_of(message_id)}.message WHERE id = ?",  # noqa: S608
            (message_id,),
        ).fetchone()[0]


def test_d4_context_window_cursor_and_budget(db, world):
    dm, petr_tg = world["dm"], world["petr_tg"]
    msg(db, dm, ts=NOW_MS - 70 * DAY_MS, sender=petr_tg, text="за окном первого прогона")
    msg(db, dm, ts=NOW_MS - 50 * DAY_MS, sender=petr_tg, text="в окне первого прогона")
    texts = [m["text"] for m in ctx(db, world["petr"])["messages"]]
    assert "в окне первого прогона" in texts and "за окном первого прогона" not in texts
    # Курсор: окно — после него.
    put_state(db, world["petr"], cursor_ts=NOW_MS - 3 * DAY_MS)
    result = ctx(db, world["petr"])
    assert result["since_ts"] == NOW_MS - 3 * DAY_MS
    assert [m["text"] for m in result["messages"]] == ["владелец отвечает в личку"]
    # Бюджет: новейшие, пока влезают.
    put_state(db, world["petr"], cursor_ts=None)
    for i in range(20):
        msg(db, dm, ts=NOW_MS - DAY_MS + i, sender=petr_tg, text=f"{i:02d}" + "ж" * 998)
    result = ctx(db, world["petr"])
    assert result["truncated"] is True
    kept = [m["text"][:2] for m in result["messages"]]
    assert kept == [f"{i:02d}" for i in range(4, 20)]  # 16 по 1000 знаков — самые новые
    assert sum(len(m["text"]) for m in result["messages"]) <= person_facts.CONTEXT_CHARS


def test_d4_context_missing(db, world):
    with pytest.raises(person_facts.NotFound):
        ctx(db, 999_999)
    tg = world["tg"]
    lone = contact(db, tg, "777", "Одинокий")
    msg(db, channel(db, tg, "777"), ts=NOW_MS, sender=lone)
    link(db)
    with pytest.raises(person_facts.NotFound):
        ctx(db, person_of(db, lone), "work")  # нет контактов в этом файле


def test_d4_context_hides_facts_of_other_projects(db, world):
    """После ревью п.1 (П2): модель видит только факты (и отклонённые тексты), все проекты которых — среди
    проектов бесед окна; иначе факт проекта p3 пересказывался бы новым фактом с проектами окна и уходил токену
    p1. Сверка дублей в `apply` по-прежнему по всем отклонённым."""
    petr, p1 = world["petr"], world["p1"]
    p3 = insert_project(db, name="Третий")
    own = fact(db, petr, "из p1", projects=[p1])
    manual = fact(db, petr, "ручной", source="manual")
    fact(db, petr, "тайная сделка", projects=[p3])
    fact(db, petr, "из p1 и p3", projects=[p1, p3])
    fact(db, petr, "отклонён p3", projects=[p3], rejected=NOW_MS - 1)
    fact(db, petr, "отклонён p1", projects=[p1], rejected=NOW_MS - 1)
    c = ctx(db, petr)
    assert c["project_ids"] == [p1]
    assert {f["id"] for f in c["facts"]} == {own, manual}
    assert c["rejected"] == ["отклонён p1"]
    result = apply(db, petr, {"version": c["version"], "add": ["Отклонён P3"], "project_ids": [p1]})
    assert result["added"] == 0


async def test_d4_context_big_channel_in_thread(bearer_client, db, world, monkeypatch):
    """П4: беседа на 10 000 сообщений — контекст за разумное время, чтение — не в потоке цикла событий."""
    dm, petr_tg = world["dm"], world["petr_tg"]

    def _bulk(conn):
        base_seq = conn.execute(
            "UPDATE seq_counter SET value = value + 10000 WHERE id = 1 RETURNING value"
        ).fetchone()[0]
        rows = []
        for i in range(10_000):
            seq = base_seq - 10_000 + i + 1
            me = i % 2
            rows.append(
                (seq, seq, world["tg"], dm, f"b{i}", NOW_MS - DAY_MS + i, None if me else petr_tg, me)
            )
        conn.executemany(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ts, "
            "sender_contact_id, is_me, kind, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'text', 'сообщение из большой беседы')",
            rows,
        )

    db.write_sync(_bulk)
    threads: list[int] = []
    real = person_facts.context

    def _spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(person_facts, "context", _spy)
    started = time.monotonic()
    r = await bearer_client.get(
        f"{API}/people/{world['petr']}/facts/context", params={"db": "main"}, headers=_facts_token(db)
    )
    elapsed = time.monotonic() - started
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is True and len(body["messages"]) > 100
    assert elapsed < 10.0
    assert threads and threads[0] != threading.get_ident()  # в потоке, не в цикле событий


# --- D.5: запись службы -------------------------------------------------------------------------------------


def test_d5_apply_add_update_delete(db, world):
    petr = world["petr"]
    result = apply(
        db,
        petr,
        {
            "version": 0,
            "add": ["Работает в Globex", "Любит  короткие\nсообщения"],
            "project_ids": [world["p1"]],
        },
    )
    assert result == {"added": 2, "updated": 0, "deleted": 0, "evicted": 0, "skipped": 0, "version": 1}
    facts = facts_of(db, petr)
    assert [f["text"] for f in facts] == ["Работает в Globex", "Любит короткие сообщения"]
    assert all(f["projects"] == [world["p1"]] and f["source"] == "auto" for f in facts)
    state = state_of(db, petr)
    assert state == {
        "cursor_ts": NOW_MS,
        "last_try_ts": NOW_MS,
        "version": 1,
        "fails": 0,
        "updated_ts": NOW_MS,
    }
    later = NOW_MS + HOUR_MS
    result = apply(
        db,
        petr,
        {
            "version": 1,
            "update": [{"id": facts[0]["id"], "text": "Руководит разработкой в Globex"}],
            "delete": [facts[1]["id"]],
            "project_ids": [world["p2"]],
        },
        now_ms=later,
    )
    assert result["updated"] == 1 and result["deleted"] == 1 and result["version"] == 2
    facts = facts_of(db, petr)
    assert [f["text"] for f in facts] == ["Руководит разработкой в Globex"]
    assert facts[0]["projects"] == sorted([world["p1"], world["p2"]]) and facts[0]["ts"] == later
    # Пустое применение двигает курсор и версию.
    assert apply(db, petr, {"version": 2}, now_ms=later)["version"] == 3


def test_d5_apply_rejects_foreign_and_manual_atomically(db, world):
    petr, kira = world["petr"], world["kira"]
    own = fact(db, petr, "свой авто")
    manual = fact(db, petr, "ручной", source="manual")
    foreign = fact(db, kira, "чужой")
    rejected = fact(db, petr, "отклонённый", rejected=NOW_MS - 1)
    work_fact = fact(db, petr, "рабочий", schema="work")
    put_state(db, petr, version=4)
    bad_bodies = [
        {"update": [{"id": foreign, "text": "x"}]},
        {"delete": [manual]},
        {"update": [{"id": manual, "text": "x"}]},
        {"delete": [rejected]},
        {"delete": [work_fact]},  # факт другого файла
        {"delete": [own], "update": [{"id": own, "text": "x"}]},
        {"add": ["ok"] * 11},
        {"add": [""]},
        {"add": ["я" * 301]},
        {"add": ["ok"], "project_ids": [987654]},
        {"add": ["ok"], "project_ids": None},
    ]
    for body in bad_bodies:
        with pytest.raises(person_facts.Invalid):
            apply(db, petr, {"version": 4, **body})
    assert [f["text"] for f in facts_of(db, petr)] == ["свой авто", "ручной"]
    assert state_of(db, petr)["version"] == 4 and state_of(db, petr)["last_try_ts"] is None


def test_d5_conflict_failed_and_cursor(db, world):
    petr = world["petr"]
    put_state(db, petr, version=3, cursor_ts=NOW_MS - DAY_MS, last_try_ts=NOW_MS - 2 * HOUR_MS)
    with pytest.raises(person_facts.Conflict):
        apply(db, petr, {"version": 2, "add": ["x"]})
    assert state_of(db, petr)["last_try_ts"] == NOW_MS - 2 * HOUR_MS  # 409 попытку не трогает
    # failed — только попытка.
    result = apply(db, petr, {"version": 2, "failed": True, "cursor_ts": NOW_MS})
    assert result["failed"] is True and result["skipped"] is False
    assert state_of(db, petr) == {
        "cursor_ts": NOW_MS - DAY_MS,
        "last_try_ts": NOW_MS,
        "version": 3,
        "fails": 1,
        "updated_ts": None,
    }
    assert facts_of(db, petr) == []
    # П5: пятый сбой подряд — курсор на cursor_ts тела, fails обнулён.
    for _ in range(3):
        apply(db, petr, {"version": 3, "failed": True, "cursor_ts": NOW_MS - HOUR_MS})
    assert state_of(db, petr)["fails"] == 4 and state_of(db, petr)["cursor_ts"] == NOW_MS - DAY_MS
    result = apply(db, petr, {"version": 3, "failed": True, "cursor_ts": NOW_MS - HOUR_MS})
    assert result["skipped"] is True
    assert state_of(db, petr)["fails"] == 0 and state_of(db, petr)["cursor_ts"] == NOW_MS - HOUR_MS
    # Курсор не уходит назад и не дальше now.
    apply(db, petr, {"version": 3, "cursor_ts": NOW_MS - 5 * DAY_MS})
    assert state_of(db, petr)["cursor_ts"] == NOW_MS - HOUR_MS
    apply(db, petr, {"version": 4, "cursor_ts": NOW_MS + DAY_MS})
    assert state_of(db, petr)["cursor_ts"] == NOW_MS


def test_d5_duplicates_and_rejected_not_added(db, world):
    """П8: сверка в Python — кириллица без регистра, `ё` = `е`, пробелы схлопнуты."""
    petr = world["petr"]
    fact(db, petr, "Предпочитает Ты", rejected=NOW_MS - 1)
    live = fact(db, petr, "Живёт в Казани")
    other = fact(db, petr, "Любит чай")
    result = apply(
        db,
        petr,
        {
            "version": 0,
            "add": ["предпочитает  ты", "ЖИВЕТ В КАЗАНИ", "Новый факт", "новый   факт"],
            "update": [{"id": other, "text": "ПРЕДПОЧИТАЕТ ТЫ"}],
        },
    )
    assert result["added"] == 1 and result["skipped"] == 4 and result["updated"] == 0
    texts = [f["text"] for f in facts_of(db, petr)]
    assert texts == ["Живёт в Казани", "Любит чай", "Новый факт"]
    assert live


async def test_d5_route_apply_audit_without_texts(bearer_client, db, world):
    headers = _facts_token(db)
    petr = world["petr"]
    url = f"{API}/people/{petr}/facts/apply"
    body = {"version": 0, "cursor_ts": NOW_MS, "add": [SECRET], "project_ids": [world["p1"]]}
    r = await bearer_client.post(url, params={"db": "main"}, json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
    r = await bearer_client.post(url, params={"db": "main"}, json=body, headers=headers)
    assert r.status_code == 409 and r.json() == {"detail": "conflict"}
    bad = {"version": 1, "cursor_ts": NOW_MS, "delete": [123456], "project_ids": []}
    r = await bearer_client.post(url, params={"db": "main"}, json=bad, headers=headers)
    assert r.status_code == 422 and SECRET not in r.text
    r = await bearer_client.post(url, params={"db": "main"}, json={**body, "extra": 1}, headers=headers)
    assert r.status_code == 422
    r = await bearer_client.post(
        f"{API}/people/999999/facts/apply", params={"db": "main"}, json=body, headers=headers
    )
    assert r.status_code == 404
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'person_facts_apply'"
        ).fetchall()
    assert len(rows) == 1
    detail = json.loads(rows[0][0])
    assert detail["person_id"] == petr and detail["added"] == 1 and SECRET not in rows[0][0]


# --- D.6: предел 50 -----------------------------------------------------------------------------------------


def test_d6_limit_50(db, world):
    petr = world["petr"]
    autos = [fact(db, petr, f"авто {i}", ts=NOW_MS - DAY_MS + i) for i in range(47)]
    manual_old = fact(db, petr, "ручной старый", source="manual", ts=NOW_MS - 2 * DAY_MS)
    fact(db, petr, "ручной 2", source="manual", ts=NOW_MS - 2 * DAY_MS)
    fact(db, petr, "ручной 3", source="manual", ts=NOW_MS - 2 * DAY_MS)
    fact(db, petr, "отклонённый", rejected=NOW_MS - 1, ts=NOW_MS - 3 * DAY_MS)
    result = apply(db, petr, {"version": 0, "add": ["новый 1", "новый 2", "новый 3"]})
    assert result["added"] == 3 and result["evicted"] == 3
    live = facts_of(db, petr)
    ids = {f["id"] for f in live}
    assert len(live) == 50 and not ids & set(autos[:3]) and set(autos[3:]) <= ids and manual_old in ids
    assert len(facts_of(db, petr, live=False)) == 51  # отклонённый остаётся, но не считается
    # Ручных 50 — новые авто не остаются.
    kira = world["kira"]
    for i in range(50):
        fact(db, kira, f"ручной {i}", source="manual")
    result = apply(db, kira, {"version": 0, "add": ["авто"]})
    assert result["added"] == 1 and result["evicted"] == 1
    assert {f["source"] for f in facts_of(db, kira)} == {"manual"} and len(facts_of(db, kira)) == 50


# --- D.7: правка владельца ---------------------------------------------------------------------------------------


async def test_d7_manual_ui_only(client, bearer_client, ui, db, world):
    petr = world["petr"]
    auto = fact(db, petr, "авто факт", projects=[world["p1"]])
    r = await client.post(f"{API}/people/{petr}/facts", json={"text": "  владелец\nдобавил  "}, headers=ui)
    assert r.status_code == 200, r.text
    card = r.json()
    manual = next(f for f in card["facts"]["items"] if f["source"] == "manual")
    assert manual["text"] == "владелец добавил"
    assert db_of(manual["id"]) == "main"  # первый контакт — главный файл
    assert state_of(db, petr)["version"] == 1
    # Правка авто делает его ручным и защищает от службы.
    r = await client.patch(f"{API}/people/{petr}/facts/{auto}", json={"text": "поправил"}, headers=ui)
    assert r.status_code == 200
    assert facts_of(db, petr)[0] == {
        "id": auto,
        "text": "поправил",
        "source": "manual",
        "projects": [world["p1"]],
        "ts": NOW_MS,
        "rejected": None,
    }
    with pytest.raises(person_facts.Invalid):
        apply(db, petr, {"version": 2, "update": [{"id": auto, "text": "служба"}]})
    # Убрать — rejected_ts, пропал из карточки, попал в «отклонено» контекста.
    r = await client.delete(f"{API}/people/{petr}/facts/{auto}", headers=ui)
    assert r.status_code == 200
    assert auto not in {f["id"] for f in r.json()["facts"]["items"]}
    assert ctx(db, petr)["rejected"] == ["поправил"]
    assert state_of(db, petr)["version"] == 3
    # Факт другого человека — 404; пустой текст — 422.
    kira_fact = fact(db, world["kira"], "чужой")
    r = await client.patch(f"{API}/people/{petr}/facts/{kira_fact}", json={"text": "x"}, headers=ui)
    assert r.status_code == 404
    r = await client.delete(f"{API}/people/{petr}/facts/{auto}", headers=ui)
    assert r.status_code == 404  # уже убран
    r = await client.post(f"{API}/people/{petr}/facts", json={"text": "  \n "}, headers=ui)
    assert r.status_code == 422
    r = await client.post(f"{API}/people/999999/facts", json={"text": "x"}, headers=ui)
    assert r.status_code == 404
    # Bearer (даже `facts`) — 403 и аудит ui_only_denied.
    for headers in (_read_token(db), _facts_token(db)):
        for method, url, body in (
            ("POST", f"{API}/people/{petr}/facts", {"text": "x"}),
            ("PATCH", f"{API}/people/{petr}/facts/{manual['id']}", {"text": "x"}),
            ("DELETE", f"{API}/people/{petr}/facts/{manual['id']}", None),
        ):
            r = await bearer_client.request(method, url, json=body, headers=headers)
            assert r.status_code == 403, (method, url)
    with db.reader() as conn:
        denied = conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'ui_only_denied'").fetchone()[0]
        audit = conn.execute(
            "SELECT action, detail_json FROM main.audit WHERE action LIKE 'person_fact_%' ORDER BY id"
        ).fetchall()
    assert denied == 6
    assert [a for a, _ in audit] == ["person_fact_add", "person_fact_edit", "person_fact_reject"]
    assert all("поправил" not in d and "владелец добавил" not in d for _, d in audit)


def test_d7_manual_file_is_first_contact(db):
    sl = account(db, "slack", schema="work")
    only_work = contact(db, sl, "U0W", "Только рабочий", schema="work")
    msg(db, channel(db, sl, "D9", schema="work"), ts=NOW_MS, sender=only_work)
    link(db)
    person_id = person_of(db, only_work)
    fact_id = db.write_sync(lambda conn: person_facts.add_manual(conn, person_id, "рабочий факт", NOW_MS))
    assert db_of(fact_id) == "work" and state_of(db, person_id, schema="work")["version"] == 1


# --- D.8: скоуп facts ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "scopes", "project_ids"),
    [
        ("interactive", ("read", "facts"), None),
        ("background", ("read", "facts"), [1]),
        ("background", ("facts",), None),
        ("background", ("read", "facts", "draft"), None),
        ("background", ("read", "facts", "transcribe"), None),
    ],
)
def test_d8_facts_scope_only_background_all_projects_read(db, kind, scopes, project_ids):
    with pytest.raises(ValueError, match="scope"):
        db.write_sync(
            lambda conn: security.create_api_token(
                conn, name="x", kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
            )
        )


async def test_d8_service_routes_need_facts(client, bearer_client, ui, db, world):
    make_bearer_token(db, kind="background", scopes=("read", "facts"))  # разрешённое сочетание
    petr = world["petr"]
    routes = (
        ("GET", f"{API}/people/facts/due", None),
        ("GET", f"{API}/people/{petr}/facts/context?db=main", None),
        ("POST", f"{API}/people/{petr}/facts/apply?db=main", {"version": 0, "cursor_ts": NOW_MS}),
    )
    for method, url, body in routes:
        r = await bearer_client.request(method, url, json=body, headers=_read_token(db))
        assert r.status_code == 403, url
        r = await client.request(method, url, json=body, headers=ui)  # UI-сессии скоуп не выдаётся
        assert r.status_code == 403, url
    r = await bearer_client.get(
        f"{API}/people/{petr}/facts/context", params={"db": "main"}, headers=_facts_token(db)
    )
    assert r.status_code == 200
    r = await bearer_client.get(
        f"{API}/people/{petr}/facts/context", params={"db": "x"}, headers=_facts_token(db)
    )
    assert r.status_code == 422
    r = await bearer_client.get(
        f"{API}/people/999999/facts/context", params={"db": "main"}, headers=_facts_token(db)
    )
    assert r.status_code == 404


# --- D.9: хуки людей ----------------------------------------------------------------------------------------


def test_d9_merge_moves_facts_and_limit(db, world):
    petr, kira = world["petr"], world["kira"]
    for i in range(30):
        fact(db, petr, f"Пётр {i}", ts=NOW_MS - DAY_MS + i)
        fact(db, kira, f"Кира {i}", ts=NOW_MS - 2 * DAY_MS + i)
    fact(db, kira, "Кира в рабочем", schema="work")
    put_state(db, petr, cursor_ts=NOW_MS - DAY_MS, version=3, fails=2)
    put_state(db, kira, cursor_ts=NOW_MS - HOUR_MS, version=7, last_try_ts=NOW_MS - 5)
    db.write_sync(lambda conn: people.merge(conn, petr, [kira]))
    live = facts_of(db, petr)
    assert len(live) == 50
    assert {f["text"] for f in live} >= {f"Пётр {i}" for i in range(30)}  # вытеснены старейшие — Кирины
    assert facts_of(db, kira) == [] and state_of(db, kira) is None
    assert state_of(db, petr) == {
        "cursor_ts": NOW_MS - HOUR_MS,
        "last_try_ts": NOW_MS - 5,
        "version": 8,
        "fails": 0,
        "updated_ts": None,
    }
    assert [f["text"] for f in facts_of(db, petr, schema="work")] == ["Кира в рабочем"]
    assert state_of(db, petr, schema="work")["version"] == 1


def test_d9_split_keeps_and_cleanup_removes(db, world):
    petr = world["petr"]
    fact(db, petr, "главный факт")
    fact(db, petr, "рабочий факт", schema="work")
    new_id = db.write_sync(lambda conn: people.split(conn, [world["petr_sl"]], NOW_MS))
    assert [f["text"] for f in facts_of(db, petr)] == ["главный факт"]
    assert [f["text"] for f in facts_of(db, petr, schema="work")] == ["рабочий факт"]  # остаются у прежнего
    assert facts_of(db, new_id, schema="work") == []
    # Уборка B4: человека нет — его факты и состояния уходят в обоих файлах.
    ghost = 424242
    fact(db, ghost, "призрак main")
    fact(db, ghost, "призрак work", schema="work")
    put_state(db, ghost, version=1)
    put_state(db, ghost, schema="work", version=1)
    link(db)
    assert facts_of(db, ghost, live=False) == [] and facts_of(db, ghost, schema="work", live=False) == []
    assert state_of(db, ghost) is None and state_of(db, ghost, schema="work") is None
    assert [f["text"] for f in facts_of(db, petr, schema="work")] == ["рабочий факт"]


def test_d9_auto_merge_by_email_moves_facts(db):
    """П1: автосклейка по почте — поглощённый человек опустел, его факты переехали к цели."""
    box = account(db, "mail", config={"address": "me@corp.io"})
    sl = account(db, "slack", schema="work")
    by_mail = contact(db, box, "anna@client.ru", "Анна")
    by_slack = contact(db, sl, "U0ANNA", "Anna", schema="work")
    msg(db, channel(db, box, "t1", kind="mail_thread"), ts=NOW_MS, sender=by_mail)
    msg(db, channel(db, sl, "D7", schema="work"), ts=NOW_MS, sender=by_slack)
    link(db)
    target, absorbed = person_of(db, by_mail), person_of(db, by_slack)
    assert target < absorbed
    fact(db, absorbed, "из слака", schema="work")
    fact(db, absorbed, "из главного")
    put_state(db, absorbed, schema="work", cursor_ts=NOW_MS - DAY_MS, version=2)
    slack_people(db, sl, [{"id": "U0ANNA", "email": "Anna@Client.ru"}])
    link(db)
    assert person_of(db, by_slack) == target
    assert [f["text"] for f in facts_of(db, target, schema="work")] == ["из слака"]
    assert [f["text"] for f in facts_of(db, target)] == ["из главного"]
    assert state_of(db, target, schema="work")["cursor_ts"] == NOW_MS - DAY_MS
    assert (
        facts_of(db, absorbed, schema="work", live=False) == []
        and state_of(db, absorbed, schema="work") is None
    )


# --- D.10: карточка -----------------------------------------------------------------------------------------


async def test_d10_card_facts_both_files_and_project_scope(client, bearer_client, ui, db, world):
    petr = world["petr"]
    r = await client.get(f"{API}/people/{petr}", headers=ui)
    assert r.json()["facts"] == {"items": [], "count": 0, "updated_ts": None}
    old = fact(db, petr, "старый главный", ts=NOW_MS - DAY_MS, projects=[world["p1"]])
    work = fact(db, petr, "новый рабочий", schema="work", ts=NOW_MS - HOUR_MS, projects=[world["p2"]])
    manual = fact(db, petr, "ручной", source="manual", ts=NOW_MS - 2 * DAY_MS)
    mixed = fact(db, petr, "из двух проектов", ts=NOW_MS - 3 * DAY_MS, projects=[world["p1"], world["p2"]])
    no_project = fact(db, petr, "из беседы без проекта", ts=NOW_MS - 4 * DAY_MS, projects=[0])
    fact(db, petr, "убранный", ts=NOW_MS, rejected=NOW_MS)
    put_state(db, petr, schema="work", updated_ts=NOW_MS - 10)
    r = await client.get(f"{API}/people/{petr}", headers=ui)
    facts = r.json()["facts"]
    assert [f["id"] for f in facts["items"]] == [work, old, manual, mixed, no_project]
    assert facts["count"] == 5 and facts["updated_ts"] == NOW_MS - 10
    assert facts["items"][2] == {
        "id": manual,
        "text": "ручной",
        "source": "manual",
        "updated_ts": NOW_MS - 2 * DAY_MS,
    }
    # П2: токен проекта 1 — только факты, все проекты которых в его списке; ручные — всем.
    r = await bearer_client.get(f"{API}/people/{petr}", headers=_read_token(db, project_ids=[world["p1"]]))
    assert [f["id"] for f in r.json()["facts"]["items"]] == [old, manual]
    r = await bearer_client.get(
        f"{API}/people/{petr}", headers=_read_token(db, project_ids=[world["p1"], world["p2"]])
    )
    assert [f["id"] for f in r.json()["facts"]["items"]] == [work, old, manual, mixed]
    r = await bearer_client.get(f"{API}/people/{petr}", headers=_read_token(db))
    assert r.json()["facts"]["count"] == 5
    # Список людей — без фактов.
    r = await client.get(f"{API}/people", params={"scope": "all"}, headers=ui)
    assert all("facts" not in p for p in r.json()["items"])
