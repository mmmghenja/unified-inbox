"""Ф42-1 (`docs/architecture.md`): догон Telegram без потерь — список дыр, страницы
по 100 от новых к старым, дыры в курсоре аккаунта, снимок позиций до живого потока. Настоящее ядро в процессе
(`ASGITransport`), настоящий буфер событий и толкатель каркаса; шлюз — `FakeGateway`, режущий ответ по 100,
как сервер. Чат становится известным ядру обычной стартовой загрузкой, пропуск за «простой» — сообщения,
добавленные в историю шлюза после неё.
"""

import asyncio
import contextlib
import json
import logging

import httpx
import pytest
from telethon import types
from telethon.errors import ChannelPrivateError, FloodWaitError

from hub.connectors import base as base_mod
from hub.connectors.telegram import connector as connector_mod
from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import NewMessageGE

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway

_NOW_S = 1_700_000_000.0  # совпадает с датами `helpers.make_dialog`/`make_message` по умолчанию


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


class _Stand:
    """Один «процесс» коннектора поверх общего ядра и общего шлюза; `restart()` — новый процесс."""

    def __init__(self, internal_transport, secret: str, account_id: int, gw: FakeGateway) -> None:
        self._transport = internal_transport
        self._secret = secret
        self.account_id = account_id
        self.gw = gw
        self.connector: TelegramConnector
        self.client = None
        self.emitted: list[dict] = []
        self._pusher: asyncio.Task | None = None
        self.restart()

    def restart(self) -> TelegramConnector:
        if self._pusher is not None:
            self._pusher.cancel()
        self.client = make_core_client(self._transport, self._secret)
        conn = TelegramConnector(
            self.client, self.gw, account_id=self.account_id, wall=lambda: _NOW_S, sleep=_no_sleep
        )
        conn._me_id = self.gw.me.id  # noqa: SLF001 — обычно ставит `start_source()`
        self.emitted = []
        real_emit = conn.emit

        def emit(event: dict) -> None:
            self.emitted.append(event)
            real_emit(event)

        conn.emit = emit
        self.connector = conn
        self._pusher = asyncio.ensure_future(conn._pusher_loop())  # noqa: SLF001 — толкатель буфера каркаса
        return conn

    async def sync(self, reason: str = "scheduled") -> None:
        await self.connector.sync(reason)
        assert await self.connector.flush_events(5)

    async def stop(self) -> None:
        if self._pusher is not None:
            self._pusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pusher
        with contextlib.suppress(Exception):
            await self.connector.stop_source()

    def history_offsets(self, marked: int) -> list[int]:
        return [offset for m, offset, _limit in self.gw.history_calls if m == marked]

    def upserts(self, chat_ext: str) -> list[int]:
        return [
            int(e["ext_id"])
            for e in self.emitted
            if e["type"] == "upsert" and e["channel"]["ext_id"].split(":")[0] == chat_ext
        ]

    async def cursor(self) -> dict:
        raw = await self.client.get_cursor()
        return json.loads(raw["value"]) if raw else {"gaps": []}

    def pending_sync_reasons(self) -> list[str]:
        queue = self.connector._sync_queue  # noqa: SLF001
        return list(queue._queue)  # noqa: SLF001 — содержимое очереди причин сверки


@pytest.fixture
async def stand(admin_client, internal_transport):
    account_id, secret = await create_tg_account(admin_client)
    s = _Stand(internal_transport, secret, account_id, FakeGateway())
    yield s
    await s.stop()


def _core_rows(db, account_id: int) -> list[tuple[str, int]]:
    """Все сообщения аккаунта в ядре: (ext_id беседы, id сообщения)."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT c.ext_id, m.ext_id FROM main.message m JOIN main.channel c ON c.id = m.channel_id "
            "WHERE m.account_id = ?",
            (account_id,),
        ).fetchall()
    return [(chat, int(ext)) for chat, ext in rows]


def _core_ids(db, account_id: int, chat_ext: str) -> list[int]:
    return sorted(ext for chat, ext in _core_rows(db, account_id) if chat == chat_ext)


async def _known_chat(stand: _Stand, user_id: int, *, first_id: int = 1) -> types.User:
    """Личный чат, известный ядру (одно сообщение `first_id`), — обычной стартовой загрузкой."""
    user = h.make_user(user_id, first_name=f"U{user_id}")
    stand.gw.add_dialog(h.make_dialog(user, top_message=first_id))
    stand.gw.add_message(user_id, h.make_message(first_id, h.peer_of(user), message="до", from_id=None))
    await stand.sync("start")
    stand.gw.history_calls.clear()
    stand.emitted.clear()
    return user


def _miss(stand: _Stand, user: types.User, ids, *, read_inbox_max_id: int = 0) -> None:
    """Сообщения `ids` появились в чате за простой; диалог показывает новую вершину."""
    ids = list(ids)
    for i in ids:
        stand.gw.add_message(user.id, h.make_message(i, h.peer_of(user), message=f"m{i}", from_id=None))
    for n, d in enumerate(stand.gw.dialogs):
        if d.entity is user:
            stand.gw.dialogs[n] = h.make_dialog(
                user, top_message=max(ids), read_inbox_max_id=read_inbox_max_id
            )


# --- 1. 250 сообщений за простой — все, по одному разу, страницы от новых ----------------------------------


async def test_1_250_missed_all_arrive_once_pages_newest_first(stand, db, caplog):
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252), read_inbox_max_id=251)

    with caplog.at_level(logging.INFO, logger="hub"):
        await stand.sync()

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 252))
    assert stand.history_offsets(7) == [252, 152, 52]  # три страницы, от новых к старым, без четвёртой
    got = stand.upserts("7")[:250]  # догон; дальше — обычное перечитывание окна (последние 30 повторно)
    assert sorted(got) == list(range(2, 252))  # каждое — один раз
    assert got[:100] == list(range(152, 252))  # новая страница первой, внутри — по возрастанию id
    assert any("догон chat=7 страниц=3 сообщений=250" in r.getMessage() for r in caplog.records)
    assert not any("m251" in r.getMessage() for r in caplog.records)  # текстов в журнале нет
    assert (await stand.cursor())["gaps"] == []
    # Раздел G: маркер прочтения — после сообщений чата (ядро двигает его только по известному сообщению).
    kinds = [e["type"] for e in stand.emitted if e["channel"]["ext_id"] == "7"]
    assert kinds.index("read") == 250  # сразу за 250 сообщениями догона


async def test_1_known_chat_without_messages_takes_one_page_only(stand, db):
    """Ядро знает чат, но ни одного его сообщения (`last` = 0): как до Ф42-1 — последняя страница, а не вся
    история чата с первого сообщения."""
    user = h.make_user(8, first_name="U8")

    async def heads():
        return [
            {
                "ext_id": "8",
                "kind": "dm",
                "parent_ext_id": None,
                "own_id_space": False,
                "max_ext_sort": None,
                "last_msg_ts": None,
                "archived": False,
            }
        ]

    stand.client.channels_heads = heads
    stand.gw.add_dialog(h.make_dialog(user, top_message=300))
    for i in range(1, 301):
        stand.gw.add_message(8, h.make_message(i, h.peer_of(user), message="x", from_id=None))

    await stand.sync("start")

    assert stand.history_offsets(8) == [301]
    assert _core_ids(db, stand.account_id, "8") == list(range(201, 301))
    assert (await stand.cursor())["gaps"] == []


# --- 2. Форум: 250 сообщений в трёх темах — все на своих темах ---------------------------------------------


async def test_2_forum_250_in_three_topics_each_on_its_topic(stand, db):
    forum = h.make_channel(900, title="Форум", megagroup=True, forum=True)
    peer = h.peer_of(forum)
    marked = h.utils.get_peer_id(peer)
    topics = (10, 11, 12)
    stand.gw.add_dialog(h.make_dialog(forum, top_message=12))
    for t in topics:
        action = types.MessageActionTopicCreate(title=f"Тема {t}", icon_color=0)
        stand.gw.add_message(marked, h.make_message(t, peer, from_id=None, action=action))
        stand.gw.forum_topics[(marked, t)] = h.make_forum_topic(900, t, title=f"Тема {t}")
    await stand.sync("start")
    stand.gw.history_calls.clear()

    by_topic: dict[int, list[int]] = {t: [t] for t in topics}
    for i in range(13, 263):
        t = topics[i % 3]
        by_topic[t].append(i)
        reply = h.make_reply(top_id=t, forum_topic=True)
        stand.gw.add_message(marked, h.make_message(i, peer, message=f"m{i}", from_id=None, reply_to=reply))
    stand.gw.dialogs[0] = h.make_dialog(forum, top_message=262)

    await stand.sync()

    assert stand.history_offsets(marked) == [263, 163, 63]
    rows = _core_rows(db, stand.account_id)
    assert len(rows) == 3 + 250 and len(set(rows)) == len(rows)
    for t in topics:
        assert sorted(ext for chat, ext in rows if chat == f"{marked}:{t}") == by_topic[t]


# --- 3. Гонка: живое сообщение после подключения, но до первой сверки ---------------------------------------


async def _live(stand: _Stand, user: types.User, msg_id: int, db) -> None:
    msg = h.make_message(msg_id, h.peer_of(user), message=f"m{msg_id}", from_id=None)
    stand.gw.add_message(user.id, msg)
    stand.gw.push(NewMessageGE(message=msg, entities={user.id: user}))
    for _ in range(200):
        if msg_id in _core_ids(db, stand.account_id, str(user.id)):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("живое сообщение не дошло до ядра")


async def test_3_live_message_before_first_sync_does_not_hide_the_gap(stand, db):
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 152))  # пропуск за простой: 2..151
    conn = stand.restart()

    await conn.start_source()  # снимок позиций — до живого потока
    await _live(stand, user, 152, db)  # ядро уже знает 152: свежий максимум равен вершине диалога
    stand.gw.dialogs[0] = h.make_dialog(user, top_message=152)
    await stand.sync("start")

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 153))
    assert "snap" not in await stand.cursor()  # снимок израсходован первой сверкой


async def test_3_snapshot_survives_crash_before_first_sync(stand, db):
    """Процесс подключился, принял живое сообщение и упал, не дойдя до сверки: позиции прошлого подключения
    лежат в курсоре, новый процесс считает пропуск по ним, а не по поднятому живым сообщением максимуму."""
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 152))
    conn = stand.restart()
    await conn.start_source()
    await _live(stand, user, 152, db)
    assert (await stand.cursor())["snap"] == {"7": 1}
    await conn.stop_source()

    conn = stand.restart()  # падение и новый процесс
    await conn.start_source()
    stand.gw.dialogs[0] = h.make_dialog(user, top_message=152)
    await stand.sync("start")

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 153))


async def test_3_reconnect_takes_positions_before_connect(stand, db):
    """Путь переподключения (`health`): снимок — до `gateway.connect()`."""
    user = await _known_chat(stand, 7)
    conn = stand.connector
    await conn.start_source()
    await stand.sync("start")
    await stand.gw.disconnect()  # обрыв
    _miss(stand, user, range(2, 152))

    await conn.health()  # переподключение
    await _live(stand, user, 152, db)
    stand.gw.dialogs[0] = h.make_dialog(user, top_message=152)
    assert "reconnect" in stand.pending_sync_reasons()
    await stand.sync("reconnect")

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 153))


async def test_3_reconnect_during_running_sync_keeps_snapshot_for_next_sync(stand):
    """Переподключение случилось, пока шла сверка: часть чатов она сравнила до обрыва — снимок этого
    подключения она не расходует, его возьмёт следующая сверка."""
    await _known_chat(stand, 7)
    conn = stand.connector
    real_iter = stand.gw.iter_dialogs

    async def iter_with_reconnect():
        async for d in real_iter():
            yield d
        await conn._catchup_snapshot()  # noqa: SLF001 — пульс переподключился посреди прохода

    stand.gw.iter_dialogs = iter_with_reconnect
    await stand.sync()
    assert conn._catchup_snap == {"7": 1}  # noqa: SLF001

    stand.gw.iter_dialogs = real_iter
    await stand.sync("reconnect")
    assert conn._catchup_snap == {}  # noqa: SLF001
    assert "snap" not in await stand.cursor()


def _skip_dialog(stand: _Stand, entity) -> None:
    """Проход диалогов не выдаёт чат: Telethon листает по дате (`client/dialogs.py::_DialogsIter`), и чат,
    получивший сообщение между запросами страниц, уезжает наверх и в этом проходе не встречается."""
    real_iter = stand.gw.iter_dialogs

    async def iter_skipping():
        async for d in real_iter():
            if d.entity is not entity:
                yield d

    stand.gw.iter_dialogs = iter_skipping
    stand.real_iter = real_iter


async def test_3_snapshot_entry_waits_for_pass_that_sees_the_chat(stand, db):
    """Ревью, блокер 2: снимок расходуется по чатам. Первая сверка чат не встретила — его запись ждёт сверки,
    которая встретит; пропуск за простой считается по ней, а не по поднятому живым сообщением максимуму."""
    await _known_chat(stand, 8)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 152))
    conn = stand.restart()
    await conn.start_source()  # снимок: {"7": 1, "8": 1}

    _skip_dialog(stand, user)
    await _live(stand, user, 152, db)
    await stand.sync("start")
    assert conn._catchup_snap == {"7": 1}  # noqa: SLF001 — чат 8 встречен и израсходован, чат 7 ждёт
    assert (await stand.cursor())["snap"] == {"7": 1}

    stand.gw.iter_dialogs = stand.real_iter
    stand.gw.dialogs[:] = [
        h.make_dialog(user, top_message=152) if d.entity is user else d for d in stand.gw.dialogs
    ]
    await stand.sync()

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 153))
    assert "snap" not in await stand.cursor()


async def test_3_sleep_live_before_on_resume_does_not_hide_the_gap(stand, db):
    """Ревью, п.4: после сна Telethon переподключился сам и отдал живое сообщение раньше, чем детектор сна
    (раз в 5 с) позвал `on_resume`. Снимок в `on_resume` берёт позицию не выше той, что видела последняя
    сверка, — живое сообщение её не поднимает."""
    user = await _known_chat(stand, 7)
    conn = stand.connector
    await conn.start_source()
    await stand.sync("start")
    _miss(stand, user, range(2, 152))  # пришло за время сна
    await _live(stand, user, 152, db)  # Telethon проснулся раньше детектора сна
    stand.gw.dialogs[0] = h.make_dialog(user, top_message=152)

    await conn.on_resume()
    await stand.sync("resume")

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 153))


async def test_3_sleep_noticed_on_live_event_before_it_reaches_core(stand, db):
    """Ревью, п.4: «был сон» проверяется синхронно перед отдачей ядру живого события (настенные часы ушли
    вперёд монотонных) — снимок позиций снят до него, даже если `on_resume` не вызывался вовсе."""
    conn = stand.connector
    now = {"wall": _NOW_S, "mono": 100.0}
    conn._wall = lambda: now["wall"]  # noqa: SLF001
    conn._mono = lambda: now["mono"]  # noqa: SLF001
    user = await _known_chat(stand, 7)
    await conn.start_source()
    await stand.sync("start")
    assert conn._catchup_snap == {}  # noqa: SLF001

    _miss(stand, user, range(2, 152))
    now["wall"] += 3600  # сон: настенные часы ушли на час, монотонные стояли
    now["mono"] += 1
    await _live(stand, user, 152, db)
    assert conn._catchup_snap == {"7": 1}  # noqa: SLF001 — снимок снят до живого сообщения
    stand.gw.dialogs[0] = h.make_dialog(user, top_message=152)
    await stand.sync()

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 153))


async def test_3_sleep_noticed_by_sync_before_fresh_positions(stand):
    """Та же проверка — в начале сверки, до чтения свежих позиций из ядра."""
    conn = stand.connector
    now = {"wall": _NOW_S, "mono": 100.0}
    conn._wall = lambda: now["wall"]  # noqa: SLF001
    conn._mono = lambda: now["mono"]  # noqa: SLF001
    user = await _known_chat(stand, 7)
    await _known_chat(stand, 8)
    _skip_dialog(stand, user)  # чат 7 в проходе не встретится — его запись снимка останется видна
    now["wall"] += 3600

    await stand.sync()

    assert conn._catchup_snap == {"7": 1}  # noqa: SLF001


# --- 4. Рестарт посреди заполнения --------------------------------------------------------------------------


async def test_4_restart_mid_fill_continues_from_cursor(stand, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 2)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 452))

    await stand.sync()  # две страницы из пяти

    assert stand.history_offsets(7) == [452, 352]
    gaps = (await stand.cursor())["gaps"]
    assert [(g["c"], g["lo"], g["hi"], g["p"], g["m"]) for g in gaps] == [("7", 1, 252, 2, 200)]

    stand.restart()  # новый процесс: в памяти пусто, вершина диалога уже известна ядру
    stand.gw.history_calls.clear()
    await stand.sync("start")
    await stand.sync("catchup")

    assert stand.history_offsets(7) == [252, 152, 52]  # с места остановки, закрытое не перезапрошено
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 452))
    assert (await stand.cursor())["gaps"] == []


# --- 5. FloodWait посреди заполнения ------------------------------------------------------------------------


async def test_5_floodwait_mid_fill_resumes_from_same_place(stand, db):
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    real = stand.gw.get_history
    calls = 0

    async def flaky(peer, *, offset_id, limit):
        nonlocal calls
        calls += 1
        if calls == 2:
            stand.gw.history_calls.append((h.utils.get_peer_id(peer), offset_id, limit))
            raise FloodWaitError(request=None, capture=30)
        return await real(peer, offset_id=offset_id, limit=limit)

    stand.gw.get_history = flaky

    await stand.sync()  # FloodWait — пауза цикла сверки, не падение

    assert stand.history_offsets(7) == [252, 152]
    assert _core_ids(db, stand.account_id, "7") == [1, *range(152, 252)]
    assert [(g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [(1, 152)]

    await stand.sync()

    assert stand.history_offsets(7) == [252, 152, 152, 52]  # следующий заход — с того же места
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 252))


# --- 6. Бюджет страниц --------------------------------------------------------------------------------------


async def test_6_budget_small_gaps_first_and_catchup_requested(stand, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 3)
    big = await _known_chat(stand, 7)  # большой — первым в списке диалогов
    small_a = await _known_chat(stand, 8)
    small_b = await _known_chat(stand, 9)
    _miss(stand, big, range(2, 352))
    _miss(stand, small_a, range(2, 7))
    _miss(stand, small_b, range(2, 12))

    await stand.sync()

    assert [m for m, _o, _l in stand.gw.history_calls] == [8, 9, 7]  # маленькие дыры — раньше большой
    assert _core_ids(db, stand.account_id, "8") == list(range(1, 7))
    assert _core_ids(db, stand.account_id, "9") == list(range(1, 12))
    assert [g["c"] for g in (await stand.cursor())["gaps"]] == ["7"]
    assert stand.pending_sync_reasons() == ["catchup"]

    for _ in range(2):
        await stand.sync("catchup")

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 352))
    assert stand.history_offsets(7) == [352, 252, 152, 52]
    assert (await stand.cursor())["gaps"] == []


async def test_6_no_catchup_request_when_every_page_fails(stand, monkeypatch):
    """Бюджет съеден, а ни одна страница не удалась (чат отвечает ошибкой): немедленный заход не просим —
    иначе горячий цикл запросов; дыра остаётся до сверки по графику."""
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    stand.gw.history_faults.append(RuntimeError("чат недоступен"))

    await stand.sync()

    assert "catchup" not in stand.pending_sync_reasons()
    assert [(g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [(1, 252)]


async def test_6_no_catchup_request_when_everything_fits(stand):
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 152))
    await stand.sync()
    assert "catchup" not in stand.pending_sync_reasons()


# --- 7. Граница страницы: ровно 100 и ровно 200 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "offsets"), [(100, [102]), (200, [202, 102]), (99, [101]), (101, [103, 3])]
)
async def test_7_page_boundary_nothing_lost_nothing_extra(stand, db, count, offsets):
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 2 + count))

    await stand.sync()

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 2 + count))
    # Догон эмитит ровно пропуск: уже известное (id 1) — нет (дальше — перечитывание окна, оно не в счёт).
    assert sorted(stand.upserts("7")[:count]) == list(range(2, 2 + count))
    assert stand.history_offsets(7) == offsets
    assert (await stand.cursor())["gaps"] == []


async def test_7_short_page_from_server_is_not_the_end(stand, db):
    """Сервер бывает отдаёт меньше запрошенного при непустом продолжении — «страница короче 100» дыру не
    закрывает (иначе хвост терялся бы молча)."""
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 152))
    real = stand.gw.get_history

    async def short(peer, *, offset_id, limit):
        messages, entities = await real(peer, offset_id=offset_id, limit=limit)
        return messages[:40], entities

    stand.gw.get_history = short

    await stand.sync()

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 152))


# --- 8. Сбой между «страница в ядре» и «курсор записан» -----------------------------------------------------


async def test_8_crash_between_page_and_cursor_refetches_page_without_dupes(stand, db):
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    real = stand.client.push_events
    cursor_writes = 0

    async def push_events(events, cursor=None):
        nonlocal cursor_writes
        if cursor is not None:
            cursor_writes += 1
            if cursor_writes == 2:  # первая запись — заведённая дыра, вторая — после первой страницы
                raise RuntimeError("сбой перед записью курсора")
        return await real(events, cursor=cursor)

    stand.client.push_events = push_events
    with pytest.raises(RuntimeError):
        await stand.connector.sync()
    assert await stand.connector.flush_events(5)

    assert _core_ids(db, stand.account_id, "7") == [1, *range(152, 252)]  # страница в ядре
    assert [(g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [(1, 252)]  # курсор — прежний

    stand.restart()
    stand.gw.history_calls.clear()
    await stand.sync("start")

    assert stand.history_offsets(7) == [252, 152, 52]  # первая страница запрошена повторно
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 252))  # дублей нет, потерь нет


async def test_8_page_rejected_by_core_does_not_move_cursor(stand, db, caplog):
    """Страница не дошла до ядра (ядро отвергло пачку — каркас её выбросил): курсор не двигается, заход
    прерван, следующий заход запрашивает ту же страницу."""
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    real = stand.client.push_events
    reject = True

    async def push_events(events, cursor=None):
        if events and reject:
            request = httpx.Request("POST", "http://hub-internal/internal/v1/events")
            raise httpx.HTTPStatusError("422", request=request, response=httpx.Response(422, request=request))
        return await real(events, cursor=cursor)

    stand.client.push_events = push_events
    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.connector.sync()

    assert stand.history_offsets(7) == [252]  # дальше первой страницы заход не пошёл
    assert [(g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [(1, 252)]
    assert "catchup" not in stand.pending_sync_reasons()  # без горячего цикла, пока ядро не принимает
    assert any("ядро не приняло ни одного сообщения страницы" in r.getMessage() for r in caplog.records)

    reject = False
    await stand.sync()

    assert stand.history_offsets(7) == [252, 252, 152, 52]
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 252))


async def test_8_core_unreachable_does_not_move_cursor(stand, db, monkeypatch):
    """Ядро недоступно — страница ждёт в буфере дольше таймаута: курсор не двигается; когда ядро вернулось,
    страница доходит сама, а следующий заход продолжает без потерь."""
    monkeypatch.setattr(connector_mod, "_CATCHUP_FLUSH_TIMEOUT_S", 0.2)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    real = stand.client.push_events
    down = True

    async def push_events(events, cursor=None):
        if events and down:
            raise httpx.ConnectError("ядро недоступно")
        return await real(events, cursor=cursor)

    stand.client.push_events = push_events
    await stand.connector.sync()

    assert stand.history_offsets(7) == [252]
    assert [(g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [(1, 252)]

    down = False
    await stand.sync()

    assert _core_ids(db, stand.account_id, "7") == list(range(1, 252))
    assert (await stand.cursor())["gaps"] == []


# --- 9. Вышли из чата с открытой дырой ----------------------------------------------------------------------


async def test_9_left_chat_drops_open_gap(stand, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    group = h.make_chat(55, title="Группа")
    peer = h.peer_of(group)
    marked = h.utils.get_peer_id(peer)
    stand.gw.add_dialog(h.make_dialog(group, top_message=1))
    stand.gw.add_message(marked, h.make_message(1, peer, message="до", from_id=None))
    await stand.sync("start")
    for i in range(2, 252):
        stand.gw.add_message(marked, h.make_message(i, peer, message="x", from_id=None))
    stand.gw.dialogs[0] = h.make_dialog(group, top_message=251)
    await stand.sync()
    assert [g["c"] for g in (await stand.cursor())["gaps"]] == [str(marked)]
    stand.gw.history_calls.clear()

    left = h.make_chat(55, title="Группа")
    left.left = True
    stand.gw.dialogs[0] = h.make_dialog(left, top_message=251)
    stand.gw.entities[marked] = left
    await stand.sync()

    assert stand.gw.history_calls == []  # историю покинутого чата не запрашиваем
    assert (await stand.cursor())["gaps"] == []
    assert stand.connector._gaps == {}  # noqa: SLF001


async def test_9_gap_of_chat_missing_from_pass_is_filled_from_entity_cache(stand, db, monkeypatch, caplog):
    """Ревью, блокер 1: чата с дырой в проходе диалогов нет (уехал наверх между страницами `GetDialogs`) —
    дыра не снимается и заполняется без диалога: peer из кэша сущностей сессии."""
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    await _known_chat(stand, 8)
    user = await _known_chat(stand, 7)
    conn = stand.connector
    await conn.start_source()
    _miss(stand, user, range(2, 252))
    await stand.sync("start")  # бюджет 1: страница 152..251, дыра (1, 152) осталась
    assert [(g["c"], g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [("7", 1, 152)]

    _skip_dialog(stand, user)
    await _live(stand, user, 252, db)
    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.sync("catchup")
        await stand.sync("catchup")

    assert not any("снята" in r.getMessage() for r in caplog.records)
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 253))
    assert (await stand.cursor())["gaps"] == []


async def test_9_entity_cache_miss_keeps_the_gap_waiting(stand, db, monkeypatch):
    """Чата нет ни в проходе, ни в кэше сущностей сессии: запроса истории нет, дыра ждёт следующего захода."""
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    await _known_chat(stand, 8)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    await stand.sync()
    stand.gw.history_calls.clear()

    real_iter = stand.gw.iter_dialogs

    async def iter_without_chat():
        async for d in real_iter():
            if d.entity is not user:
                yield d
        stand.gw.forget_peer(7)  # в ответах `GetDialogs` этого прохода чата нет — кэш сессии его не знает

    stand.gw.iter_dialogs = iter_without_chat
    stand.real_iter = real_iter
    while stand.pending_sync_reasons():  # «catchup» первого захода — он и идёт следующим
        stand.connector._sync_queue.get_nowait()  # noqa: SLF001
    await stand.sync("catchup")

    assert stand.gw.history_calls == []
    assert [(g["c"], g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [("7", 1, 152)]
    assert "catchup" not in stand.pending_sync_reasons()  # без горячего цикла

    stand.gw.iter_dialogs = stand.real_iter  # проход диалогов снова видит чат — кэш наполнен
    await stand.sync()
    await stand.sync("catchup")
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 252))


async def test_9_chat_gone_forever_drops_the_gap_with_warning(stand, monkeypatch, caplog):
    """Источник на запрос истории ответил «чат недоступен навсегда» (закрытый канал) — дыра снята."""
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 252))
    await stand.sync()
    assert (await stand.cursor())["gaps"] != []

    stand.gw.history_faults.append(ChannelPrivateError(request=None))
    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.sync("catchup")

    assert (await stand.cursor())["gaps"] == []
    assert any(
        "дыра chat=7 снята — чат недоступен (ChannelPrivateError)" in r.getMessage() for r in caplog.records
    )


async def test_9_gap_of_chat_unknown_to_core_is_dropped(stand, caplog):
    """Ядро чата больше не знает (аккаунт вычищен) — догонять некуда, дыра снята."""
    await _known_chat(stand, 7)
    stand.connector._gaps["99"] = [{"lo": 1, "hi": 50, "t": 0, "p": 0, "m": 0}]  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.sync()

    assert stand.connector._gaps == {}  # noqa: SLF001
    assert any("дыра chat=99 снята — ядро чата не знает" in r.getMessage() for r in caplog.records)


async def test_9_empty_dialog_list_keeps_gaps(stand, monkeypatch):
    """Пустой или неполный список диалогов — не признак: дыра остаётся и заполняется дальше."""
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 352))
    await stand.sync()
    stand.gw.dialogs.clear()

    await stand.sync("catchup")

    assert [(g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [(1, 152)]


# --- ревью, п.3: негодная страница не запирает остальные чаты -----------------------------------------------


async def test_10_unserializable_message_is_skipped_and_other_chats_fill(stand, db, monkeypatch, caplog):
    """Сообщение маленькой дыры даёт несериализуемое событие: оно пропущено с записью в журнал, дыра
    двигается дальше, чаты, стоящие после по размеру, заполняются в том же заходе."""
    small = await _known_chat(stand, 7)
    big = await _known_chat(stand, 8)
    _miss(stand, small, range(2, 12))
    _miss(stand, big, range(2, 252))
    real = convert.convert_message

    def poisoned(msg, ctx):
        payload = real(msg, ctx)
        if msg.id == 5 and ctx.channel["ext_id"] == "7":
            payload["text"] = b"bytes"  # ошибка конвертера: несериализуемое поле
        return payload

    monkeypatch.setattr(convert, "convert_message", poisoned)
    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.sync()

    assert _core_ids(db, stand.account_id, "8") == list(range(1, 252))
    assert 5 not in stand.upserts("7")[:9] and sorted(stand.upserts("7")[:9]) == [2, 3, 4, *range(6, 12)]
    assert (await stand.cursor())["gaps"] == []
    assert any("догон chat=7 сообщение id=5 пропущено" in r.getMessage() for r in caplog.records)


async def test_10_message_rejected_by_core_is_skipped_rest_of_page_arrives(stand, db, caplog):
    """Ядро отвергло пачку из-за одного сообщения: события страницы уходят по одному, не принятое пропущено
    с записью в журнал (без текста), остальное в ядре, дыра закрыта, другой чат заполнен."""
    small = await _known_chat(stand, 7)
    big = await _known_chat(stand, 8)
    _miss(stand, small, range(2, 12))
    _miss(stand, big, range(2, 152))
    real = stand.client.push_events

    async def push_events(events, cursor=None):
        if any(e.get("ext_id") == "5" and e["channel"]["ext_id"] == "7" for e in events):
            request = httpx.Request("POST", "http://hub-internal/internal/v1/events")
            raise httpx.HTTPStatusError("422", request=request, response=httpx.Response(422, request=request))
        return await real(events, cursor=cursor)

    stand.client.push_events = push_events
    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.connector.sync()
        await stand.connector.flush_events(5)

    assert _core_ids(db, stand.account_id, "7") == [1, 2, 3, 4, *range(6, 12)]
    assert _core_ids(db, stand.account_id, "8") == list(range(1, 152))
    assert (await stand.cursor())["gaps"] == []
    skipped = [r.getMessage() for r in caplog.records if "пропущено" in r.getMessage()]
    assert skipped == ["connector: догон chat=7 сообщение id=5 пропущено — не принято ядром"]


async def test_10_chat_rejected_entirely_by_core_does_not_stop_the_queue(stand, db, caplog):
    """Приёмка: ядро отвергает по содержимому ВСЕ события одного чата (422) — это беда чата, а не ядра. Его
    дыра не двигается и лежит в курсоре, чат пропущен до следующего захода, второй чат заполнен в том же."""
    small = await _known_chat(stand, 7)
    big = await _known_chat(stand, 8)
    _miss(stand, small, range(2, 12))
    _miss(stand, big, range(2, 152))
    real = stand.client.push_events

    async def push_events(events, cursor=None):
        if any(e["channel"]["ext_id"] == "7" for e in events):
            request = httpx.Request("POST", "http://hub-internal/internal/v1/events")
            raise httpx.HTTPStatusError("422", request=request, response=httpx.Response(422, request=request))
        return await real(events, cursor=cursor)

    stand.client.push_events = push_events
    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.connector.sync()
        await stand.connector.flush_events(5)

    assert _core_ids(db, stand.account_id, "8") == list(range(1, 152))
    assert _core_ids(db, stand.account_id, "7") == [1]
    assert [(g["c"], g["lo"], g["hi"]) for g in (await stand.cursor())["gaps"]] == [("7", 1, 12)]
    assert any(
        "догон chat=7 — ядро не приняло ни одного сообщения страницы, чат пропущен до следующего захода"
        in r.getMessage()
        for r in caplog.records
    )
    assert "catchup" not in stand.pending_sync_reasons()


async def test_10_failing_chat_does_not_stop_the_queue(stand, db):
    """Чат, у которого страница не получается по иной причине (сеть на запросе истории), пропускается до
    следующего захода — чаты после него заполняются."""
    small = await _known_chat(stand, 7)
    big = await _known_chat(stand, 8)
    _miss(stand, small, range(2, 12))
    _miss(stand, big, range(2, 152))
    stand.gw.history_faults.append(ConnectionError("сеть"))  # первый запрос — чат 7 (меньшая дыра)

    await stand.sync()

    assert _core_ids(db, stand.account_id, "8") == list(range(1, 152))
    assert [g["c"] for g in (await stand.cursor())["gaps"]] == ["7"]
    await stand.sync()
    assert _core_ids(db, stand.account_id, "7") == list(range(1, 12))


# --- журнал: дыра живёт дольше часа -------------------------------------------------------------------------


async def test_stale_gap_warning_once_per_hour(stand, monkeypatch, caplog):
    monkeypatch.setattr(connector_mod, "_CATCHUP_PAGES_PER_SYNC", 1)
    user = await _known_chat(stand, 7)
    _miss(stand, user, range(2, 452))
    conn = stand.connector
    now = {"wall": _NOW_S, "mono": 0.0}
    conn._wall = lambda: now["wall"]  # noqa: SLF001
    conn._mono = lambda: now["mono"]  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="hub"):
        await stand.sync()
        assert not any("не закрыта" in r.getMessage() for r in caplog.records)  # дыра только что заведена
        now["wall"] += 61 * 60
        now["mono"] += 61 * 60
        await stand.sync("catchup")
        await stand.sync("catchup")

    warnings = [r.getMessage() for r in caplog.records if "не закрыта" in r.getMessage()]
    assert warnings == ["connector: дыра chat=7 не закрыта 61 мин"]  # раз в час на чат, без текстов


# --- каркас: счётчик выброшенных событий и причина «catchup» ------------------------------------------------


def test_base_lost_count_grows_on_overflow_and_unserializable(monkeypatch):
    """`flush_events` считает выброшенное «ушедшим»; «дошло» от «выброшено» отличает `_lost_count`."""
    monkeypatch.setattr(base_mod, "_EVENT_BUFFER_CAP", 2)
    conn = TelegramConnector(None, FakeGateway(), account_id=1)
    conn.emit({"type": "x"})
    conn.emit({"type": "x"})
    assert conn._lost_count == 0  # noqa: SLF001
    conn.emit({"type": "x"})  # переполнение: буфер очищен
    assert conn._lost_count == 1  # noqa: SLF001
    conn.emit({"type": "x", "bad": object()})  # несериализуемое
    assert conn._lost_count == 2  # noqa: SLF001


def test_base_catchup_reason_beats_narrow_reasons():
    """Слияние причин: «catchup» — полная сверка, узкие «folders»/«join» её не съедают."""
    assert base_mod._pick_main_sync_reason({"catchup", "folders", "join"}) == "catchup"  # noqa: SLF001
    assert base_mod._pick_main_sync_reason({"catchup", "reconnect"}) == "reconnect"  # noqa: SLF001
