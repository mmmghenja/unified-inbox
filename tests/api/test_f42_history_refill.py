"""Ф42-3 (`docs/architecture.md`, ядро): событие с признаком `history` (добор истории)
принимается тихо — без сигналов подпискам, вложения по клику, в журнал только числа; `GET /channels/heads` с
`since_ts` отдаёт границы добора. Стиль и помощники — как `test_f31_tails.py`."""

import logging

from hub.core import ingest, queries

from . import test_f31_tails as f31
from .conftest import insert_account, insert_channel, insert_project
from .test_f31_tails import KEYS, A, B, _chan_sub, _ev, _ingest, _msg_id, _now, _signals, _sub

# Фикстуры соседнего файла: подмена `/proc` и мир из проекта с подписками, аккаунта Telegram и трёх бесед.
procs = f31.procs
world = f31.world


def _hist(event: dict) -> dict:
    return {**event, "history": True}


def _media_status(db, message_id) -> list[str]:
    with db.reader() as conn:
        rows = conn.execute("SELECT status FROM main.media WHERE message_id = ?", (message_id,)).fetchall()
    return [r[0] for r in rows]


# --- 1. Новое сообщение добором — сигнала нет ни подписке на проект, ни подписке на беседу ----------------


async def test_1_history_message_does_not_signal_project_or_channel_sub(client, db, world, procs, clock):
    await _sub(client, world, A, mode="messages")
    await _chan_sub(client, world, B, world["ch"]["G2"])
    now = _now(clock)

    _ingest(db, world, [_hist(_ev("G1", "1", now + 1)), _hist(_ev("G2", "1", now + 2))], now + 5)

    assert _signals(db) == []
    assert _msg_id(db, world["ch"]["G1"], "1") and _msg_id(db, world["ch"]["G2"], "1")  # строки есть

    # Контроль: те же сообщения без признака — сигнал обеим подпискам.
    _ingest(db, world, [_ev("G1", "2", now + 3), _ev("G2", "2", now + 4)], now + 5)
    pending = [s for s in _signals(db) if s[3] == "pending"]
    assert any(s[0] is not None and s[2] == world["ch"]["G2"] for s in pending)  # подписка на беседу
    assert any(s[1] is not None and s[2] == world["ch"]["G1"] for s in pending)  # подписка на проект


# --- 2. Правка известного сообщения добором — ключи подписок не перепроверяются --------------------------


async def test_2_history_edit_does_not_recheck_keywords(client, db, world, procs, clock):
    sub = await _sub(client, world, A, mode="personal", **KEYS)
    now = _now(clock)
    g1 = world["ch"]["G1"]
    _ingest(db, world, [_ev("G1", "1", now, text="ни о чём"), _ev("G1", "2", now + 1, text="пусто")], now)
    before = _signals(db)

    edit = _ev("G1", "1", now, text="у нас штраф", edited_ts=now + 10)
    _ingest(db, world, [_hist(edit)], now + 10)

    assert _signals(db) == before
    with db.reader() as conn:
        assert conn.execute("SELECT text FROM main.message WHERE ext_id = '1'").fetchone()[0] == "у нас штраф"

    # Контроль: такая же правка без признака — сигнал «по ключу».
    _ingest(db, world, [_ev("G1", "2", now + 1, text="тут штраф", edited_ts=now + 20)], now + 20)
    assert _signals(db, g1)[-1] == (None, sub, g1, "pending", None, 1, "keyword", ["штраф"])


# --- 3. Вложение свежее точки отсчёта медиа: добором — по клику, обычным событием — сразу ------------------


async def test_3_history_attachment_is_deferred(db, world, clock):
    now = _now(clock)
    g1 = world["ch"]["G1"]

    _ingest(db, world, [_hist(_ev("G1", "1", now, voice=True)), _ev("G1", "2", now + 1, voice=True)], now + 5)

    assert _media_status(db, _msg_id(db, g1, "1")) == ["deferred"]
    assert _media_status(db, _msg_id(db, g1, "2")) == ["pending"]


# --- 4. Позиция прочтения дальше сообщения — непрочитанное не выросло -------------------------------------


async def test_4_history_message_behind_read_marker_is_not_unread(db, world, clock):
    now = _now(clock)
    g1 = world["ch"]["G1"]
    _ingest(db, world, [_ev("G1", "10", now), _ev("G1", "20", now + 100)], now + 200)
    _ingest(
        db,
        world,
        [{"type": "read", "channel": {"ext_id": "G1", "kind": "group"}, "up_to_ext_id": "20"}],
        now + 200,
    )
    with db.reader() as conn:
        assert ingest.unread_breakdown(conn, g1) == (0, 0)

    _ingest(db, world, [_hist(_ev("G1", "15", now + 50))], now + 300)

    with db.reader() as conn:
        assert ingest.unread_breakdown(conn, g1) == (0, 0)
        count = conn.execute("SELECT COUNT(*) FROM main.message WHERE channel_id = ?", (g1,)).fetchone()[0]
    assert count == 3


# --- 5. Журнал: только числа; упоминание — прямое, новое, чужое -------------------------------------------


async def test_5_log_counts_inserted_and_names_direct_mentions(db, world, clock, caplog):
    now = _now(clock)
    g1 = world["ch"]["G1"]
    _ingest(db, world, [_ev("G1", "1", now, text="известное", mention=True)], now)
    broadcast = _hist(_ev("G1", "3", now + 2, text="секретный текст всем", mention=True))
    broadcast["mentions_broadcast_only"] = True
    group = _hist(_ev("G1", "6", now + 5, text="секретный текст группе", mention=True))
    group["mentions_group_only"] = True
    events = [
        _hist(_ev("G1", "1", now, text="известное", mention=True)),  # повтор известного
        _hist(_ev("G1", "2", now + 1, text="секретный текст мне", mention=True)),  # прямое упоминание
        broadcast,
        _hist(_ev("G1", "4", now + 3, text="секретный текст мой", is_me=True, mention=True)),  # своё
        _hist(_ev("G1", "5", now + 4, text="секретный текст просто")),
        group,
    ]

    # Уровень WARNING: у ядра INFO в журнал не идёт (журнал процесса не настроен) — строки потерялись бы.
    with caplog.at_level(logging.WARNING, logger="hub.core.ingest"):
        _ingest(db, world, events, now + 10)

    assert all(r.levelno == logging.WARNING for r in caplog.records if "добор" in r.getMessage())
    lines = [r.getMessage() for r in caplog.records if "добор" in r.getMessage()]
    assert lines == [
        f"ingest: добор account={world['acc']} новых=5",
        f"ingest: добор упоминание channel={g1} message={_msg_id(db, g1, '2')}",
    ]
    assert "секретный" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="hub.core.ingest"):
        _ingest(db, world, events, now + 20)  # всё уже известно — строк нет
        _ingest(db, world, [_ev("G1", "7", now + 6, mention=True)], now + 20)  # не добор — строк нет
    assert [r.getMessage() for r in caplog.records if "добор" in r.getMessage()] == []


# --- 6. `heads` с `since_ts` -------------------------------------------------------------------------------


def _heads(db, account_id, since_ts=None) -> dict[str, dict]:
    with db.reader() as conn:
        return {h["ext_id"]: h for h in queries.connector_channel_heads(conn, account_id, since_ts)}


async def test_6_heads_since_ts_gives_refill_bounds_and_project(db):
    acc = insert_account(db, source="telegram")
    pid = insert_project(db, name="П")
    insert_channel(db, acc, project_id=pid, ext_id="A", kind="group")
    insert_channel(db, acc, ext_id="B", kind="group")  # «Неразобранное»
    insert_channel(db, acc, project_id=pid, ext_id="C", kind="group")  # без сообщений

    def push(chat, ext, ts):
        return {
            "type": "upsert",
            "channel": {"ext_id": chat, "kind": "group"},
            "ext_id": ext,
            "ts": ts,
            "is_me": False,
            "kind": "text",
            "text": "x",
            "mentions_me": False,
        }

    events = [push("A", "10", 100), push("A", "20", 200), push("A", "30", 1000), push("A", "40", 2000)]
    events += [push("B", "50", 1500), push("B", "60", 2500)]
    db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=3000)
    )

    plain = _heads(db, acc)
    assert set(plain["A"]) == {
        "ext_id",
        "kind",
        "parent_ext_id",
        "own_id_space",
        "max_ext_sort",
        "max_ext_id",
        "last_msg_ts",
        "archived",
    }  # без параметра — прежний ответ

    heads = _heads(db, acc, since_ts=500)
    assert {k: v for k, v in heads["A"].items() if k in plain["A"]} == plain["A"]
    # есть сообщения раньше границы — самое новое из них
    assert (heads["A"]["refill_lo_ext_sort"], heads["A"]["max_ext_sort"]) == (20, 40)
    assert heads["A"]["project_id"] == pid
    # раньше границы ничего нет — самое старое известное
    assert (heads["B"]["refill_lo_ext_sort"], heads["B"]["project_id"]) == (50, None)
    # пустая беседа
    assert (heads["C"]["refill_lo_ext_sort"], heads["C"]["max_ext_sort"]) == (None, None)
