"""Ф42-3 (`docs/architecture.md`, коннектор): добор потерянного с 23.09 поверх списка
дыр Ф42-1 — засев один раз по границам от ядра, дыры добора отдельно от обычных, события их страниц с
`history=True`, выключатель `history_refill`. Стенд — тот же, что у `test_catchup.py` (настоящее ядро в
процессе, шлюз режет по 100)."""

import json
import logging

import pytest

from hub.connectors.telegram import connector as connector_mod

from . import helpers as h
from . import test_catchup as tc
from .test_catchup import _core_ids, _known_chat, _miss

stand = tc.stand

_SINCE = 1_000  # граница добора в тестах: все сообщения фикстур новее неё


def _known(stand, chat_ext: str, ids, *, kind: str = "dm", channel: dict | None = None) -> list[dict]:
    """События «ядру уже известно»: сообщения `ids` беседы — напрямую в ядро."""
    ref = channel or {"ext_id": chat_ext, "kind": kind, "title": f"Чат {chat_ext}"}
    return [
        {
            "type": "upsert",
            "channel": ref,
            "ext_id": str(i),
            "ts": 1_700_000_000_000 + i,
            "is_me": False,
            "kind": "text",
            "text": "известное",
            "mentions_me": False,
        }
        for i in ids
    ]


def _sql(db, query: str, *params) -> None:
    db.write_sync(lambda conn: conn.execute(query, params))


def _to_project(db, *chat_exts: str) -> None:
    for ext in chat_exts:
        # Ручная раскладка: авто-раскладку пересчитывает приём (правил в тесте нет — проект бы слетел).
        _sql(db, "UPDATE main.channel SET project_id = 1, map_source = 'manual' WHERE ext_id = ?", ext)


def _config(db, account_id: int, **config) -> None:
    _sql(db, "UPDATE main.account SET config_json = ? WHERE id = ?", json.dumps(config), account_id)


def _hist_gaps(cursor: dict) -> list[tuple]:
    return [(g["c"], g["lo"], g["hi"]) for g in cursor["gaps"] if g.get("h")]


def _server(stand, entity, ids) -> None:
    marked = h.utils.get_peer_id(h.peer_of(entity))
    for i in ids:
        stand.gw.add_message(marked, h.make_message(i, h.peer_of(entity), message=f"m{i}", from_id=None))


# --- 7. Засев: какие чаты получают дыру ---------------------------------------------------------------------


async def test_7_seed_only_project_chats_not_archived_not_left_forum_by_topics(stand, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 0)  # только засев, без заполнения
    users = {i: h.make_user(i, first_name=f"U{i}") for i in (7, 8, 9)}
    left = h.make_chat(55, title="Покинутая")
    left.left = True
    left_ext = str(h.utils.get_peer_id(h.peer_of(left)))
    forum = h.make_channel(900, title="Форум", megagroup=True, forum=True)
    forum_ext = str(h.utils.get_peer_id(h.peer_of(forum)))
    parent = {"ext_id": forum_ext, "kind": "group", "title": "Форум", "own_id_space": True}

    def topic(topic_id: int) -> dict:
        return {
            "ext_id": f"{forum_ext}:{topic_id}",
            "kind": "topic",
            "title": f"Тема {topic_id}",
            "parent_ext_id": forum_ext,
            "own_id_space": True,
            "parent": parent,
        }

    events = [*_known(stand, "7", (1, 50)), *_known(stand, "8", (1, 50)), *_known(stand, "9", (1, 50))]
    events += _known(stand, left_ext, (1, 50), kind="group")
    events += _known(stand, "", (10, 200), channel=topic(10))
    events += _known(stand, "", (11, 300), channel=topic(11))
    r = await stand.client.push_events(events)
    assert r["dead"] == 0, r
    _to_project(db, "7", "9", left_ext, f"{forum_ext}:10")  # 8 — «Неразобранное»; у форума — одна тема
    _sql(db, "UPDATE main.channel SET archived = 1 WHERE ext_id = '9'")
    for user in users.values():
        stand.gw.add_dialog(h.make_dialog(user, top_message=50))
    stand.gw.add_dialog(h.make_dialog(left, top_message=50))
    stand.gw.add_dialog(h.make_dialog(forum, top_message=300))

    await stand.sync("start")

    cursor = await stand.cursor()
    assert sorted(_hist_gaps(cursor)) == sorted([("7", 1, 50), (forum_ext, 10, 300)])
    assert (cursor["hist"], cursor["hn"]) == (_SINCE, 2)
    assert [g for g in cursor["gaps"] if not g.get("h")] == []


# --- 8. Потерянный блок посреди известных -------------------------------------------------------------------


async def test_8_lost_block_is_refilled_quietly_fresh_catchup_is_not_history(stand, db, monkeypatch, caplog):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    user = h.make_user(7, first_name="U7")
    stand.gw.add_dialog(h.make_dialog(user, top_message=340))
    _server(stand, user, range(1, 341))
    # Ядро знает 1 и 252..330: блок 2..251 (250 сообщений) потерян раньше; 331..340 — свежий пропуск.
    r = await stand.client.push_events(_known(stand, "7", [1, *range(252, 331)]))
    assert r["dead"] == 0, r
    _to_project(db, "7")

    with caplog.at_level(logging.INFO, logger="hub"):
        await stand.sync("start")

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 341))
    upserts = [e for e in stand.emitted if e["type"] == "upsert" and e["channel"]["ext_id"] == "7"]
    history = sorted(int(e["ext_id"]) for e in upserts if e.get("history"))
    assert history == list(range(2, 330))  # всё строго между границами добора — тихо
    fresh = [e for e in upserts if int(e["ext_id"]) > 330]
    assert fresh and not any(e.get("history") for e in fresh)  # свежий догон — обычными событиями
    # Обычная дыра (330, 341) — раньше добора (1, 330); добор — страницами от новых к старым.
    assert stand.history_offsets(7) == [341, 330, 230, 130, 30]
    lines = [r.getMessage() for r in caplog.records]
    assert "connector: добор засеян, чатов=1" in lines
    assert "connector: добор chat=7 страниц=4 сообщений=328" in lines
    assert "connector: добор закончен, чатов=1" in lines
    assert not any("m100" in line for line in lines)  # текстов в журнале нет
    assert (await stand.cursor())["gaps"] == []
    assert "catchup" not in stand.pending_sync_reasons()


# --- 9. Засев один раз -------------------------------------------------------------------------------------


async def test_9_seed_once_restart_continues_other_constant_reseeds(stand, db, monkeypatch, caplog):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    user = h.make_user(7, first_name="U7")
    stand.gw.add_dialog(h.make_dialog(user, top_message=330))
    _server(stand, user, range(1, 331))
    await stand.client.push_events(_known(stand, "7", [1, *range(300, 331)]))
    _to_project(db, "7")

    with caplog.at_level(logging.INFO, logger="hub"):
        await stand.sync("start")  # засев и одна страница добора
        assert _hist_gaps(await stand.cursor()) == [("7", 1, 230)]
        assert "catchup" not in stand.pending_sync_reasons()  # остаток добора — сверками по графику

        stand.restart()  # новый процесс: отметка в курсоре — засева нет, добор продолжается
        await stand.sync("start")
        assert _hist_gaps(await stand.cursor()) == [("7", 1, 130)]
        assert stand.history_offsets(7) == [330, 230]
        await stand.sync()
        await stand.sync()
        assert _hist_gaps(await stand.cursor()) == []
        assert _core_ids(db, stand.account_id, "7") == list(range(1, 331))
    seeded = [r.getMessage() for r in caplog.records if "добор засеян" in r.getMessage()]
    assert seeded == ["connector: добор засеян, чатов=1"]

    # Другая граница — засев заново (ядро теперь знает всё: дыра та же по границам, заполняется вхолостую).
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE + 1)
    await stand.sync()
    cursor = await stand.cursor()
    assert cursor["hist"] == _SINCE + 1 and _hist_gaps(cursor) == [("7", 1, 230)]


# --- 10. Очередь и слияние ----------------------------------------------------------------------------------


async def test_10_regular_gap_closes_before_refill(stand, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 2)
    big = h.make_user(7, first_name="U7")  # добор — маленькая дыра
    other = h.make_user(8, first_name="U8")  # обычная — большая дыра
    stand.gw.add_dialog(h.make_dialog(big, top_message=60))
    stand.gw.add_dialog(h.make_dialog(other, top_message=200))
    _server(stand, big, range(1, 61))
    _server(stand, other, range(1, 201))
    await stand.client.push_events([*_known(stand, "7", (1, 60)), *_known(stand, "8", (1,))])
    _to_project(db, "7")

    await stand.sync("start")  # бюджет 2: обе страницы — обычной дыре чата 8, добору не досталось

    assert [m for m, _o, _l in stand.gw.history_calls] == [8, 8]
    cursor = await stand.cursor()
    assert _hist_gaps(cursor) == [("7", 1, 60)] and [g for g in cursor["gaps"] if not g.get("h")] == []

    await stand.sync()
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 61))
    assert (await stand.cursor())["gaps"] == []


def test_10_refill_gap_never_merges_with_regular(stand):
    """Дыры разного вида не сливаются (у слитой пропал бы один из признаков); одного вида — как обычно."""
    conn = stand.connector
    conn._gap_note("7", 10, 50)  # noqa: SLF001
    conn._gap_note("7", 20, 60, history=True)  # noqa: SLF001 — пересекается с обычной
    conn._gap_note("7", 55, 80, history=True)  # noqa: SLF001 — пересекается с добором
    conn._gap_note("7", 40, 90)  # noqa: SLF001 — пересекается с обычной
    gaps = sorted((g["lo"], g["hi"], bool(g.get("h"))) for g in conn._gaps["7"])  # noqa: SLF001
    assert gaps == [(10, 90, False), (20, 80, True)]
    # Правило «ядро не знает чат — одна страница» к добору не применяется и добор не трогает.
    conn._gap_note("8", 0, 40, history=True)  # noqa: SLF001
    conn._gap_note("8", 0, 70)  # noqa: SLF001
    gaps = sorted((g["lo"], g["hi"], bool(g.get("h")), bool(g.get("one"))) for g in conn._gaps["8"])  # noqa: SLF001
    assert gaps == [(0, 40, True, False), (0, 70, False, True)]


# --- 11. Выключатель ----------------------------------------------------------------------------------------


async def test_11_history_refill_off_no_seed_and_seeded_gaps_wait(stand, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    user = h.make_user(7, first_name="U7")
    stand.gw.add_dialog(h.make_dialog(user, top_message=330))
    _server(stand, user, range(1, 331))
    await stand.client.push_events(_known(stand, "7", [1, *range(300, 331)]))
    _to_project(db, "7")
    _config(db, stand.account_id, history_refill=False)

    await stand.sync("start")
    assert "hist" not in await stand.cursor()  # засева нет
    assert stand.gw.history_calls == []

    _config(db, stand.account_id)  # включили: засев и первая страница — без перезапуска
    await stand.sync()
    assert _hist_gaps(await stand.cursor()) == [("7", 1, 230)]

    _config(db, stand.account_id, history_refill=False)  # выключили: дыра добора стоит, обычная заполняется
    stand.gw.history_calls.clear()
    other = await _known_chat(stand, 8)
    _miss(stand, other, range(2, 12))
    await stand.sync()
    assert [m for m, _o, _l in stand.gw.history_calls] == [8]
    assert _core_ids(db, stand.account_id, "8") == list(range(1, 12))
    assert _hist_gaps(await stand.cursor()) == [("7", 1, 230)]


# --- 12. Засев — только после успешной сверки (после включения ноутбука прокси поднимается позже) -----------


async def test_12_broken_sync_does_not_seed_first_good_sync_seeds_once(stand, db, monkeypatch, caplog):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 0)  # только засев
    user = h.make_user(7, first_name="U7")
    stand.gw.add_dialog(h.make_dialog(user, top_message=330))
    _server(stand, user, range(1, 331))
    await stand.client.push_events(_known(stand, "7", [1, *range(300, 331)]))
    _to_project(db, "7")

    real_iter = stand.gw.iter_dialogs
    broken = {"left": 2}

    async def flaky_iter():
        if broken["left"] > 0:
            broken["left"] -= 1
            raise ConnectionError("прокси ещё не поднялся")
        async for d in real_iter():
            yield d

    stand.gw.iter_dialogs = flaky_iter
    with caplog.at_level(logging.INFO, logger="hub"):
        for _ in range(2):  # соединение рвётся: сверка оборвана на проходе диалогов
            with pytest.raises(ConnectionError):
                await stand.connector.sync("start")
            cursor = await stand.cursor()
            assert "hist" not in cursor and _hist_gaps(cursor) == []
            assert stand.connector._catchup_hist is None  # noqa: SLF001
        assert not any("добор засеян" in r.getMessage() for r in caplog.records)

        await stand.sync("start")  # первая успешная сверка — засев
        cursor = await stand.cursor()
        assert cursor["hist"] == _SINCE and _hist_gaps(cursor) == [("7", 1, 330)]
        await stand.sync()
    seeded = [r.getMessage() for r in caplog.records if "добор засеян" in r.getMessage()]
    assert seeded == ["connector: добор засеян, чатов=1"]


async def test_12_seed_cursor_write_failure_leaves_no_mark_and_no_gaps(stand, db, monkeypatch, caplog):
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", _SINCE)
    user = h.make_user(7, first_name="U7")
    stand.gw.add_dialog(h.make_dialog(user, top_message=330))
    _server(stand, user, range(1, 331))
    await stand.client.push_events(_known(stand, "7", [1, *range(300, 331)]))
    _to_project(db, "7")

    conn = stand.connector
    real_save = conn._catchup_save  # noqa: SLF001
    real_seed = conn._history_seed  # noqa: SLF001

    async def failing_save(gaps=None):
        raise ConnectionError("ядро не ответило")

    async def seed_with_broken_cursor():
        conn._catchup_save = failing_save  # noqa: SLF001 — запись курсора рвётся ровно на засеве
        try:
            await real_seed()
        finally:
            conn._catchup_save = real_save  # noqa: SLF001

    conn._history_seed = seed_with_broken_cursor  # noqa: SLF001
    with caplog.at_level(logging.INFO, logger="hub"):
        await stand.sync("start")
    assert "connector: добор не засеян (ConnectionError)" in [r.getMessage() for r in caplog.records]
    cursor = await stand.cursor()
    assert "hist" not in cursor and _hist_gaps(cursor) == []  # ни отметки, ни дыр — одна запись или ничего
    assert stand.gw.history_calls == []  # незасеянный добор не заполнялся
    assert conn._catchup_hist is None  # noqa: SLF001

    conn._history_seed = real_seed  # noqa: SLF001
    await stand.sync()
    cursor = await stand.cursor()
    assert cursor["hist"] == _SINCE and _hist_gaps(cursor) == []  # засеяно и добрано за тот же заход
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 331))


# --- 13. Добор не уходит глубже своей даты ------------------------------------------------------------------


async def test_13_refill_stops_at_its_date_and_does_not_load_older_history(stand, db, monkeypatch, caplog):
    """Нижняя граница дыры добора — известное ядру сообщение, и оно бывает много старше даты добора
    (родитель ответа, закреп). Добор берёт потерянное новее даты и останавливается на первой странице,
    дошедшей до неё: старая история чата в хаб не грузится (прод 07.10.2026: добор ушёл за 23.09)."""
    base = 1_700_000_000_000
    since = base + 399_500  # сообщения 400 и новее — после даты, 399 и старше — до неё
    monkeypatch.setattr(connector_mod, "_HISTORY_REFILL_SINCE_MS", since)
    user = h.make_user(7, first_name="U7")
    stand.gw.add_dialog(h.make_dialog(user, top_message=600))
    for i in range(1, 601):
        stand.gw.add_message(
            7, h.make_message(i, h.peer_of(user), message=f"m{i}", from_id=None, date_ms=base + i * 1000)
        )
    # Ядро знает старое одиночное сообщение 1 и свежие 480..600; потеряно после даты — 400..479.
    known = [
        {
            "type": "upsert",
            "channel": {"ext_id": "7", "kind": "dm", "title": "Чат 7"},
            "ext_id": str(i),
            "ts": base + i * 1000,
            "is_me": False,
            "kind": "text",
            "text": "известное",
            "mentions_me": False,
        }
        for i in [1, *range(480, 601)]
    ]
    r = await stand.client.push_events(known)
    assert r["dead"] == 0, r
    _to_project(db, "7")

    with caplog.at_level(logging.INFO, logger="hub"):
        await stand.sync("start")

    assert _core_ids(db, stand.account_id, "7") == [1, *range(400, 601)]  # 2..399 в хаб не попали
    # Третья страница (ниже 400) целиком старше даты — на ней добор и остановился, до сообщения 1 не дошёл.
    assert stand.history_offsets(7) == [600, 500, 400]
    assert (await stand.cursor())["gaps"] == []
    lines = [r.getMessage() for r in caplog.records]
    assert "connector: добор chat=7 страниц=3 сообщений=200" in lines
    assert "connector: добор закончен, чатов=1" in lines
