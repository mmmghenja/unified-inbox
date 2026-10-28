"""Воспроизведения находок opus-критика КОДА куска 2b (коммит ef94395), скопировано из
`.runs/critic-2b/test_critic_2b.py` (вне git) и доведено до зелёного по спеке
`docs/architecture.md`. Изменения относительно
оригинала — см. «Отчёт по кускам» в финальном отчёте исполнителя; каждое такое изменение отдельно
объясняется в докстринге теста ниже.

Запуск: `uv run pytest -q tests/core/test_critic_2b.py`.
"""

import asyncio
import threading

import pytest
from api.conftest import FakeClock, csrf_headers, login_via_code

from hub.core import outbox as outbox_mod
from hub.core import retention
from hub.core import runner as runner_mod
from hub.core.app_public import create_public_app
from hub.core.db import Db, DbClosed
from hub.core.ingest import apply_batch, unread_breakdown

DAY = 24 * 60 * 60 * 1000
PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def _retention_enabled(monkeypatch):
    """Ф8-4 П9: ретенция снята константой `RETENTION_ENABLED`; находки критика по ней проверяются с ней
    включённой."""
    monkeypatch.setattr(retention, "RETENTION_ENABLED", True)


# --- общие помощники -----------------------------------------------------------------------------------
# `db` — фикстура из tests/conftest.py (tmp_path, Db.start()/close()); своей здесь не заводим, чтобы не
# держать два разных временных каталога с одним и тем же именем БД в гарде.


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def client(db, clock):
    from httpx import ASGITransport, AsyncClient

    app = create_public_app(db, clock=clock, port=PORT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


def _account(db, *, source="slack", db_name="work"):
    return db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)
        ).lastrowid
    )


def _token(db):
    return db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')"
        ).lastrowid
    )


def _up(ext_id, ts, *, ch="c1", text="hi", is_me=False, thread_ext_id=None, broadcast=False, thread=None,
        mentions_me=False, **extra):
    ev = {
        "type": "upsert",
        "channel": {"ext_id": ch, "kind": "channel"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": mentions_me,
        "thread_ext_id": thread_ext_id,
        "thread_broadcast": broadcast,
    }
    if thread is not None:
        ev["thread"] = thread
    ev.update(extra)
    return ev


def _apply(db, acc, events, now_ms):
    return db.write_sync(lambda c: apply_batch(c, account_id=acc, events=events, cursor=None, now_ms=now_ms))


def _one(db, sql, params=()):
    with db.reader() as c:
        return c.execute(sql, params).fetchone()


def _channel_id(db, ext="c1", schema="work"):
    return _one(db, f"SELECT id FROM {schema}.channel WHERE ext_id = ?", (ext,))[0]


def _seq_of(db, ext_id, schema="work"):
    return _one(db, f"SELECT seq FROM {schema}.message WHERE ext_id = ?", (ext_id,))[0]


def _work_bytes(db):
    b = (db.data_dir / "hub-work.db").read_bytes()
    wal = db.data_dir / "hub-work.db-wal"
    return b + (wal.read_bytes() if wal.exists() else b"")


# ======================================================================================================
# РЕТЕНЦИЯ
# ======================================================================================================

# Токен FTS хранится в НИЖНЕМ регистре (unicode61) и с префиксным сжатием — ищем хвост токена.
NEEDLE = "Qwzxvkplmtrb"
TAIL = b"xvkplmtrb"


def test_ret1_ingest_path_purge_leaves_fts_token_in_file(db):
    """Строка очищена ПРИЁМОМ (правка/сверка 91-дневного сообщения раньше периодического прогона) ->
    `run_once` считает, что делать нечего (counts == 0) -> ни FTS optimize, ни vacuum, ни checkpoint ->
    токен текста остаётся в байтах hub-work.db навсегда (пока какой-нибудь будущий прогон не очистит
    что-то сам). Чинится флагом «грязно» (main.meta.retention_dirty, п.1/R1): очистка при приёме ставит
    его, run_once делает физический шаг, если счётчики нулевые, но флаг стоит."""
    acc = _account(db)
    now0 = 400 * DAY
    _apply(db, acc, [_up("old", now0 - 80 * DAY, text=f"текст {NEEDLE} тут")], now0 - 80 * DAY)
    _apply(db, acc, [_up("anchor", now0, ch="c2")], now0)
    now1 = now0 + 11 * DAY  # сообщению 91 день
    _apply(db, acc, [_up("anchor2", now1, ch="c2")], now1)
    # сверка/правка перечитала старое сообщение — очистка при приёме
    _apply(db, acc, [_up("old", now0 - 80 * DAY, text=f"текст {NEEDLE} тут", edited_ts=now1)], now1)
    assert _one(db, "SELECT text, purged FROM work.message WHERE ext_id = 'old'") == (None, 1)

    retention.run_once(db, now1)
    assert TAIL not in _work_bytes(db), "токен очищенного текста остался в файле work (FTS-сегмент)"


def test_ret2_physical_step_failure_is_never_retried(db, monkeypatch):
    """Прогон очистил строки, но физический шаг упал (или процесс убит между порциями и maintenance) ->
    следующий прогон видит «очищать нечего» и физический шаг больше не делает.

    Адаптация: после Поправок п.7 (R8) физический шаг — ТРИ отдельных элемента очереди писателя
    (`_fts_optimize`/`_incremental_vacuum_step`/`_wal_checkpoint_truncate`), единой `_apply_physical_deletion`
    больше нет (её и звал `db.maintenance_sync` одним куском — ровно то, что критик просил разбить). Монки-
    патчим ПЕРВЫЙ из трёх шагов — сценарий тот же (сбой между порциями и физической уборкой), опора теста
    (флаг «грязно» переживает сбой) не меняется."""
    acc = _account(db)
    now0 = 400 * DAY
    _apply(db, acc, [_up("old", now0 - 91 * DAY, text=f"текст {NEEDLE} тут")], now0 - 91 * DAY)
    _apply(db, acc, [_up("anchor", now0, ch="c2")], now0)

    real = retention._fts_optimize
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("SIGTERM/диск/что угодно между порциями и физическим шагом")
        return real(conn)

    monkeypatch.setattr(retention, "_fts_optimize", flaky)
    with pytest.raises(RuntimeError):
        retention.run_once(db, now0)
    retention.run_once(db, now0 + 60_000)  # планировщик повторил через тик
    assert TAIL not in _work_bytes(db), "после сбоя физического шага токен FTS так и остался в файле"


def test_ret3_committed_byte_test_is_blind_to_fts(db, monkeypatch):
    """Тест 9 (`test_word_physically_absent_from_work_db_and_wal_bytes`) ищет слово в ВЕРХНЕМ регистре, а FTS
    хранит токены в нижнем (unicode61) — без `optimize` тест всё равно зелёный.

    Уточнение при воспроизведении (эксперимент `probe_ret3` — прогон с отключённым ТОЛЬКО `_fts_optimize`):
    `PRAGMA work.secure_delete=ON` зануляет освободившиеся байты самой строки `message` ПРЯМО на UPDATE
    (`text=NULL`), независимо от `optimize`/`vacuum`/`checkpoint` — точная строка в исходном (верхнем)
    регистре из файла пропадает сама по себе. Именно поэтому байтовый тест «ищет слово как есть» слеп к
    проблеме НЕ через ложный `PASS` на ЕЩЁ присутствующем слове, а через то, что он в принципе не смотрит на
    ту форму, в которой FTS реально хранит токен (нижний регистр, `unicode61`) — и без `optimize` эта
    отдельная (не задетая UPDATE'ом) копия в служебных таблицах FTS остаётся."""
    needle = "СОВЕРШЕННОУНИКАЛЬНОЕСЛОВОДЛЯБАЙТОВОГОТЕСТА"

    monkeypatch.setattr(retention, "_fts_optimize", lambda conn: None)
    acc = _account(db)
    now0 = 100 * DAY
    _apply(db, acc, [_up("secret", now0 - 91 * DAY, text=f"текст с {needle} внутри")], now0 - 91 * DAY)
    _apply(db, acc, [_up("anchor", now0, ch="anchor-channel")], now0)
    assert retention.run_once(db, now0)["messages"] == 1
    lower_tail = needle.lower()[-12:].encode()
    assert lower_tail in _work_bytes(db)  # токен реально лежит в файле (FTS без optimize)
    # проверка «как тест 9» (по точному верхнему регистру) ничего не найдёт — и НЕ ПОТОМУ, что проблемы нет:
    # `secure_delete` уже стёр строку `message` саму по себе, а поймать FTS-хвост в нижнем регистре тест 9
    # просто не умеет (искал бы не ту форму слова).
    assert needle.encode() not in _work_bytes(db), "форма пропала сама, тест 9 не поймал бы FTS-хвост"


def test_ret4_clock_jump_plus_resolve_unknown_sent_wipes_recent_text(db):
    """Защита от скачка часов держится на `MAX(channel.last_msg_ts)`, но `resolve_unknown(decision='sent')`
    пишет в `last_msg_ts` ЧАСЫ ХАБА. Часы +1 год -> владелец разбирает «неизвестное» как отправленное ->
    опорное время = now(+1 год) -> прогон стирает всё, в т.ч. вчерашнее. Починка (п.3/R3): опорное время —
    отметка времени ИСТОЧНИКОВ (`main.meta.source_time_hwm`), которую двигает только `ingest.py` по `ts`
    события; `resolve_unknown`/`channel.last_msg_ts` на неё больше не влияют."""
    acc = _account(db)
    tok = _token(db)
    now0 = 400 * DAY
    _apply(db, acc, [_up("recent", now0 - DAY, text="вчерашний текст")], now0)
    ch = _channel_id(db)
    enq = db.write_sync(
        lambda c: outbox_mod.enqueue(
            c, channel_id=ch, text="x", origin="ui", api_token_id=tok, idempotency_key="k", now_ms=now0
        )
    )
    db.write_sync(
        lambda c: c.execute("UPDATE work.outbox SET status = 'unknown' WHERE id = ?", (enq.outbox_id,))
    )
    jumped = now0 + 365 * DAY
    db.write_sync(lambda c: outbox_mod.resolve_unknown(c, enq.outbox_id, decision="sent", now_ms=jumped))
    retention.run_once(db, jumped)
    assert _one(db, "SELECT text FROM work.message WHERE ext_id = 'recent'")[0] == "вчерашний текст"


def test_ret5_silent_source_pauses_retention_and_hub_watch_alerts(db):
    """Оригинал критика утверждал (докстринг): «опорное время = min(now, MAX(last_msg_ts)+1 день) ->
    источник замолчал -> опорное время стоит -> текст старше 90 дней по НАСТЕННЫМ часам не стирается
    никогда» — и требовал, чтобы `retention.run_once` в итоге ВСЁ РАВНО стирал текст по настенным часам.

    Переделано под принятое архитектурное решение (раздел «Поправки», п.3-4/R3-R4, план 11.7): опорное
    время НАРОЧНО замирает, пока источник молчит — это и есть защита от скачка/вранья часов (тесты
    `test_ret4`/`test_ret6`), поэтому «текст всё равно стирается поwall-clock» было бы РЕГРЕССИЕЙ, а не
    починкой. Вместо автопродолжения по часам — раздельная проблема `hub-watch`: `retention_stalled`,
    когда источники молчат дольше недели (владелец видит и решает — переподключить коннектор, снять тревогу
    руками, и т.п.), сама ретенция остаётся стоять и НЕ трогает текст."""
    from hub.watch import RETENTION_STALLED_MS, evaluate

    acc = _account(db)
    t_last = 400 * DAY
    _apply(db, acc, [_up("m", t_last - 10 * DAY, text="старый текст")], t_last)
    _apply(db, acc, [_up("last", t_last, text="последнее сообщение")], t_last)
    now = t_last + 200 * DAY  # полгода тишины, настенные часы честные, прогоны идут каждые 6 ч
    for k in range(0, 200 * 4, 40):
        retention.run_once(db, t_last + k * 6 * 3600 * 1000)
    retention.run_once(db, now)
    # ретенция ЗАКОННО стоит: опорное время защищено отметкой источников, текст не тронут.
    assert _one(db, "SELECT text FROM work.message WHERE ext_id = 'm'")[0] == "старый текст"

    with db.reader() as c:
        hwm = retention.get_source_time_hwm(c)
    assert now - hwm > RETENTION_STALLED_MS
    snapshot = {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0, "source_time_hwm": hwm}
    problems = evaluate(snapshot, now)
    assert [p.key for p in problems] == ["retention_stalled"], problems


# ======================================================================================================
# ПЛАНИРОВЩИК (runner.py)
# ======================================================================================================


async def test_run1_cancel_does_not_stop_running_retention_thread(tmp_path):
    """`retention_task.cancel()` отменяет только await `asyncio.to_thread` — сам поток `run_once` работает
    дальше и после `db.close()` лезет в закрытый Db (RuntimeError / вечное ожидание future, если успел
    положить задачу в очередь после None — тогда ещё и `asyncio.run` виснет на shutdown_default_executor).

    Починка (п.8/P1 + приёмка архитектора): `_serve_forever` дожидается потока прогона (`inflight`) до
    `db.close()` — см. `test_run1b`. Этот тест — страховка НИЖНЕГО уровня: если поток всё-таки пережил
    закрытие, запись на закрытом `Db` не виснет и не маскируется, а падает явным `DbClosed` (`Db._submit`)."""
    db = Db(tmp_path)
    db.start()
    started = threading.Event()
    release = threading.Event()
    outcome = {}

    def slow_run_once(db_arg, now_ms, stop_event=None):
        del stop_event
        started.set()
        release.wait(5)
        try:
            db_arg.write_sync(lambda c: c.execute("SELECT 1").fetchone())
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["exc"] = exc

    clock_ms = {"v": 0}

    async def fast_sleep(_s):
        clock_ms["v"] += 10 * 60 * 1000
        await asyncio.sleep(0)

    task = asyncio.ensure_future(
        runner_mod._retention_loop(db, lambda: clock_ms["v"] / 1000, sleep=fast_sleep, run_once=slow_run_once)
    )
    await asyncio.to_thread(started.wait, 5)
    # ровно то, что раньше делал finally в runner._serve_forever (до Поправок п.8) — страховка нижнего уровня:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db.close()
    release.set()
    for _ in range(100):
        if outcome:
            break
        await asyncio.sleep(0.02)
    assert outcome, "запись на закрытом Db повисла"
    assert isinstance(outcome.get("exc"), DbClosed), outcome


async def test_run1b_inflight_run_is_awaited_before_close(tmp_path):
    """Приёмка 2b (архитектор): отмена планировщика не бросает начатый прогон — его future лежит в
    `inflight`, и остановка ядра дожидается его (как `_serve_forever`) ДО `db.close()`; запись прогона
    успевает."""
    db = Db(tmp_path)
    db.start()
    started = threading.Event()
    outcome = {}

    def slow_run_once(db_arg, now_ms, stop_event=None):
        started.set()
        stop_event.wait(5)  # выходит по флагу остановки, как настоящий run_once между порциями
        outcome["value"] = db_arg.write_sync(lambda c: c.execute("SELECT 1").fetchone()[0])

    clock_ms = {"v": 0}

    async def fast_sleep(_s):
        clock_ms["v"] += 10 * 60 * 1000
        await asyncio.sleep(0)

    stop = threading.Event()
    inflight: dict = {}
    task = asyncio.ensure_future(
        runner_mod._retention_loop(
            db, lambda: clock_ms["v"] / 1000, sleep=fast_sleep, run_once=slow_run_once,
            stop_event=stop, inflight=inflight,
        )
    )
    await asyncio.to_thread(started.wait, 5)
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(asyncio.shield(inflight["future"]), timeout=5)
    db.close()
    assert outcome == {"value": 1}


async def test_run2_early_startup_failure_masks_error_and_skips_db_close(tmp_path, monkeypatch):
    """`retention_task` создаётся ВНУТРИ try, а `finally` обращается к нему безусловно: любое исключение
    между `db.start()` и `ensure_future(_retention_loop…)` (create_public_app, bind_socket, …) превращается в
    UnboundLocalError, а `db.close()` не вызывается (раньше вызывался)."""
    data = tmp_path / "data"
    cfg = tmp_path / "cfg"
    data.mkdir()
    cfg.mkdir()
    closed = {"v": False}
    real_close = Db.close

    def spy_close(self):
        closed["v"] = True
        return real_close(self)

    monkeypatch.setattr(Db, "close", spy_close)

    def boom(*a, **k):
        raise RuntimeError("исходная причина")

    monkeypatch.setattr(runner_mod, "create_public_app", boom)
    with pytest.raises(RuntimeError, match="исходная причина"):
        await runner_mod._serve_forever(data, cfg, host="127.0.0.1", port=0, clock=lambda: 1.0, ui_dir=None)
    assert closed["v"], "db.close() не вызван"


# ======================================================================================================
# ТРЕДЫ И НЕПРОЧИТАННОЕ
# ======================================================================================================


def test_thr1_thread_summary_unread_ignores_window_phantom_unread(db):
    """Сводка треда (`_thread_summary_map`) и `GET /threads?unread=1` НЕ применяют окно
    `ts >= first_seen_ts − 24 ч` — старый большой тред при первой загрузке: канал говорит 0 тредового
    непрочитанного, а сводка родителя и список «тредов с непрочитанным» — сотни."""
    from hub.core import queries
    from hub.core.security import Principal

    acc = _account(db)
    now0 = 400 * DAY
    old = now0 - 30 * DAY
    events = [_up("P", old, is_me=True)] + [_up(f"R{i}", old + i, thread_ext_id="P") for i in range(1, 6)]
    _apply(db, acc, events, now0)  # стартовая загрузка: тред «впервые увиден» сейчас, ответы — месяц назад
    ch = _channel_id(db)
    with db.reader() as c:
        top, thr = unread_breakdown(c, ch)
        principal = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)
        threads = queries.list_channel_threads(c, principal, ch, unread_only=True, limit=50)
    assert thr == 0  # окно работает для канала
    assert threads == [] or threads[0]["thread"]["unread"] == 0, (
        f"сводка треда: unread={threads[0]['thread']['unread']}, у канала unread_threads=0"
    )


def test_thr2_out_of_order_reply_does_not_bump_parent(db):
    """Ответ, пришедший НЕ последним по времени (догон после сна/сверка `conversations.replies`), меняет
    `reply_count` сводки, но `touch()` видит «last_reply_ts не изменился» -> родитель без нового seq ->
    в `/sync` сводка застыла (раздел D.3: «ответ пришёл/удалён» -> бамп родителя)."""
    acc = _account(db)
    _apply(db, acc, [_up("P", 1000), _up("R3", 3000, thread_ext_id="P")], 5000)
    parent_seq = _seq_of(db, "P")
    _apply(db, acc, [_up("R1", 2000, thread_ext_id="P")], 6000)  # более ранний ответ догнался позже
    assert _seq_of(db, "P") > parent_seq, "reply_count родителя вырос, а seq родителя — нет"


def test_thr3_delete_of_non_latest_reply_does_not_bump_parent(db):
    acc = _account(db)
    events = [_up("P", 1000), _up("R1", 2000, thread_ext_id="P"), _up("R2", 3000, thread_ext_id="P")]
    _apply(db, acc, events, 5000)
    parent_seq = _seq_of(db, "P")
    ch = _channel_id(db)
    ch_seq = _one(db, "SELECT seq FROM work.channel WHERE id = ?", (ch,))[0]
    delete_event = {"type": "delete", "channel": {"ext_id": "c1", "kind": "channel"}, "ext_ids": ["R1"]}
    _apply(db, acc, [delete_event], 6000)
    ch_bumped = _one(db, "SELECT seq FROM work.channel WHERE id = ?", (ch,))[0] > ch_seq
    parent_bumped = _seq_of(db, "P") > parent_seq
    assert (parent_bumped, ch_bumped) == (True, True), "reply_count изменился, seq родителя/канала — нет"


def test_thr4_my_parent_arriving_after_replies_does_not_subscribe(db):
    """Родитель догружается ПОСЛЕ ответов (инвариант 2c: `parent_known=false` -> догрузить родителя). Если
    родитель мой — тред по «Модели» подписан, но `touch()` на приёме родителя не зовётся."""
    acc = _account(db)
    _apply(db, acc, [_up("R1", 2000, thread_ext_id="P")], 5000)
    _apply(db, acc, [_up("P", 1000, is_me=True)], 6000)
    assert _one(db, "SELECT subscribed FROM work.thread WHERE ext_id = 'P'")[0] == 1


def test_thr5_first_reply_to_my_parent_does_not_raise_last_msg_ts(db):
    """Первый чужой ответ на МОЁ сообщение: тред подписан (родитель мой), но `last_msg_ts_eligible_…`
    смотрит подписку ДО `touch()` (строки треда ещё нет) -> канал не поднимается в списке, хотя
    unread_threads уже +1."""
    acc = _account(db)
    _apply(db, acc, [_up("P", 1000, is_me=True)], 1000)
    _apply(db, acc, [_up("R1", 50_000, thread_ext_id="P")], 50_000)
    ch = _channel_id(db)
    with db.reader() as c:
        assert unread_breakdown(c, ch)[1] == 1  # тред подписан, ответ непрочитан
    assert _one(db, "SELECT last_msg_ts FROM work.channel WHERE id = ?", (ch,))[0] == 50_000


def test_thr6_readevent_for_unknown_thread_creates_thread_row(db):
    """Раздел D.2: `ReadEvent` с `thread_ext_id` на неизвестный тред — no-op. Сейчас создаётся строка треда
    (у сообщения появляется фиктивная сводка `thread: {reply_count: 0}`) и бампаются родитель и канал."""
    acc = _account(db)
    _apply(db, acc, [_up("1700000000.000100", 1_700_000_000_000)], 1_700_000_000_000)
    res = _apply(
        db,
        acc,
        [{"type": "read", "channel": {"ext_id": "c1", "kind": "channel"},
          "up_to_ext_id": "1700000000.000100", "thread_ext_id": "1700000000.000100"}],
        1_700_000_001_000,
    )
    assert _one(db, "SELECT COUNT(*) FROM work.thread")[0] == 0
    assert res.applied == 0


async def test_thr7_ui_thread_read_does_not_bump_channel_or_parent(client, db, clock):
    """Раздел D.3: смена маркера треда -> новый seq родителю И каналу. Тредовая `read` из UI (маршрут
    `POST …/threads/{ext}/read`) не бампает НИЧЕГО -> `/sync` другой вкладки/клиента не узнает, что
    `unread_threads` канала упал."""
    acc = _account(db)
    _apply(db, acc, [_up("P", 1000, is_me=True), _up("R1", 2000, thread_ext_id="P")], 3000)
    ch = _channel_id(db)
    ch_seq = _one(db, "SELECT seq FROM work.channel WHERE id = ?", (ch,))[0]
    p_seq = _seq_of(db, "P")
    r1_id = _one(db, "SELECT id FROM work.message WHERE ext_id = 'R1'")[0]
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/channels/{ch}/threads/P/read",
        json={"up_to_message_id": r1_id},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200 and r.json()["changed"] is True
    with db.reader() as c:
        assert unread_breakdown(c, ch)[1] == 0
    assert _one(db, "SELECT seq FROM work.channel WHERE id = ?", (ch,))[0] > ch_seq, "канал без нового seq"
    assert _seq_of(db, "P") > p_seq, "родитель без нового seq"


def test_thr8_threadinfo_without_subscribed_erases_source_opinion_and_bumps(db):
    """`ThreadInfo.subscribed=None` трактуется как «источник сказал NULL», а не «не знаю»: снимок из
    `conversations.history` (без subscribed) после снимка из `conversations.replies` (subscribed=false)
    стирает мнение источника и даёт лишний бамп seq; каждый цикл сверки — ещё два бампа (шум)."""
    acc = _account(db)
    replies_snapshot = {"reply_count": 1, "latest_reply_ext_id": "R1", "subscribed": False}
    history_snapshot = {"reply_count": 1, "latest_reply_ext_id": "R1"}
    _apply(db, acc, [_up("P", 1000, thread=replies_snapshot), _up("R1", 2000, thread_ext_id="P")], 3000)
    p_seq = _seq_of(db, "P")
    for k in range(3):  # три цикла сверки: history, затем replies
        _apply(db, acc, [_up("P", 1000, thread=history_snapshot)], 4000 + k)
        _apply(db, acc, [_up("P", 1000, thread=replies_snapshot)], 5000 + k)
    assert _seq_of(db, "P") == p_seq, "одинаковые по сути снимки треда дают новые seq на каждом цикле"


# ======================================================================================================
# ext_sort / ts Slack
# ======================================================================================================


def test_ext1_is_slack_ts_too_permissive():
    """`^…$` без `re.ASCII`/`fullmatch`: `$` пропускает хвостовой `\\n`, `\\d` — любые юникод-цифры."""
    from hub.core import extsort

    assert not extsort.is_slack_ts("1700000000.123456\n")
    assert extsort.ext_sort("١٧٠٠٠٠٠٠٠٠.١٢٣٤٥٦") == 0


# ======================================================================================================
# ДОПОЛНИТЕЛЬНО: ретенция — часы, outbox; треды — масштаб
# ======================================================================================================


def test_ret6_transient_forward_clock_jump_stops_retention_for_the_jump_length(db):
    """`last_run_ts` пишется НАСТЕННЫМИ часами и потом охраняет «now раньше прошлого прогона — пропуск».
    Разовый скачок +1 год (NTP/RTC/ручная дата) -> один прогон на +1 год (ничего не стёр — защита опорным
    временем сработала) -> часы вернулись -> ретенция стоит ГОД: и `run_once`, и планировщик пропускают.

    Починка (п.5/R5): `run_once` больше не пропускает прогон целиком из-за `now < last_run_ts` — опорное
    время (п.3/R3) само по себе достаточная защита; прогон честно проводится, просто со строкой в журнал."""
    acc = _account(db)
    now0 = 400 * DAY
    _apply(db, acc, [_up("m", now0 - 60 * DAY, text="текст")], now0)
    _apply(db, acc, [_up("a0", now0, ch="c2")], now0)
    retention.run_once(db, now0 + 365 * DAY)  # скачок: прогон прошёл, last_run_ts = +1 год
    now1 = now0 + 40 * DAY  # часы исправлены, жизнь идёт, сообщению «m» уже 100 дней
    _apply(db, acc, [_up("a1", now1, ch="c2")], now1)
    retention.run_once(db, now1)
    assert _one(db, "SELECT text FROM work.message WHERE ext_id = 'm'")[0] is None


def _pending_outbox(db, acc, now_ms, text="черновик агента"):
    tok = _token(db)
    _apply(db, acc, [_up("x", now_ms)], now_ms)
    ch = _channel_id(db)
    enq = db.write_sync(
        lambda c: outbox_mod.enqueue(
            c, channel_id=ch, text=text, origin="ui", api_token_id=tok, idempotency_key="k", now_ms=now_ms
        )
    )
    db.write_sync(
        lambda c: c.execute(
            "UPDATE work.outbox SET status = 'pending_approval' WHERE id = ?", (enq.outbox_id,)
        )
    )
    return enq


def test_ret7_approve_of_purged_pending_queues_empty_text(db):
    """Раздел F.3: `pending_approval` старше 90 дней -> `text=''`, статус не меняется. `approve` без правки
    текста не проверяет пустой текст -> строка уходит в `queued` с `text=''` -> коннектор отправит пустое.

    Починка (п.6/R7): ретенция сама переводит просроченную `pending_approval` в `cancelled` (error=
    'retention') — approve() отказывает уже по статусу (`InvalidTransition`, 409), решение человека по
    строке без текста больше не имеет смысла."""
    acc = _account(db)
    now0 = 400 * DAY
    enq = _pending_outbox(db, acc, now0 - 91 * DAY)
    _apply(db, acc, [_up("anchor", now0, ch="c2")], now0)
    assert retention.run_once(db, now0)["outbox"] == 1
    try:
        db.write_sync(lambda c: outbox_mod.approve(c, enq.outbox_id, now_ms=now0))
    except outbox_mod.OutboxError:
        return  # правильное поведение — отказ
    status, text = _one(db, "SELECT status, text FROM work.outbox WHERE id = ?", (enq.outbox_id,))
    assert not (status == "queued" and text == ""), "в очередь на отправку ушёл пустой текст"


def test_ret8_new_text_on_purged_row_is_never_purged_again(db):
    """`approve(text=…)` пишет новый текст в заготовку с `purged=1` и старым `ts` -> периодический прогон
    пропускает `purged=1` навсегда -> этот текст (если отправка не удалась) живёт в work бессрочно.

    Уточнение при воспроизведении: после починки п.6 (R7) ретенция сама переводит просроченную
    `pending_approval` в `cancelled` в ТОМ ЖЕ прогоне, что чистит и её заготовку-сообщение (обе строки стареют
    синхронно, `enqueue()` ставит им одинаковый `ts`/`created_ts`) — оригинальная подготовка теста (прогнать
    `retention.run_once`, потом звать `approve(text=…)` на всё ещё `pending_approval`) больше недостижима
    обычным путём: `approve()` откажет по статусу раньше, чем дойдёт до записи текста (см. `test_ret7`).
    Заготовка здесь приводится в состояние «purged=1» НАПРЯМУЮ (та же физическая ситуация — старое
    сообщение уже физически очищено), не через прогон ретенции по outbox — так проверяется именно починка
    `approve()` (сброс `purged=0` при записи нового текста), а не порядок обработки таблиц ретенцией."""
    acc = _account(db)
    now0 = 400 * DAY
    enq = _pending_outbox(db, acc, now0)
    db.write_sync(
        lambda c: c.execute("UPDATE work.message SET purged = 1, text = NULL WHERE id = ?", (enq.message_id,))
    )
    db.write_sync(lambda c: outbox_mod.approve(c, enq.outbox_id, text="новый текст", now_ms=now0))
    db.write_sync(
        lambda c: c.execute("UPDATE work.outbox SET status = 'failed' WHERE id = ?", (enq.outbox_id,))
    )
    later = now0 + 200 * DAY
    _apply(db, acc, [_up("anchor2", later, ch="c2")], later)
    retention.run_once(db, later)
    assert _one(db, "SELECT text FROM work.message WHERE id = ?", (enq.message_id,))[0] is None


async def test_thr9_channel_threads_list_breaks_on_many_threads(client, db, clock):
    """`list_channel_threads` берёт ВСЕ треды канала (строки `thread` ретенцией не удаляются) и строит
    `IN (…)` на каждый -> на канале с ≥ 32766 тредами SQLite: too many SQL variables -> 500; до этого —
    полный проход всех тредов и всех ответов канала на каждый GET."""
    acc = _account(db)
    _apply(db, acc, [_up("P0", 1000)], 1000)
    ch = _channel_id(db)
    db.write_sync(
        lambda c: c.executemany(
            "INSERT INTO work.thread (channel_id, ext_id, last_reply_ts, first_seen_ts) VALUES (?, ?, ?, 0)",
            [(ch, f"T{i}", i) for i in range(33_000)],
        )
    )
    await login_via_code(client, db, clock)
    try:
        r = await client.get(f"/api/v1/channels/{ch}/threads?unread=1&limit=5")
        status = r.status_code
    except Exception as exc:  # noqa: BLE001 — ASGITransport пробрасывает исключение приложения
        status = f"exception {type(exc).__name__}: {exc}"
    assert status == 200, status


def test_ret9_checkpoint_blocked_by_reader_retries_and_completes_on_next_run(db):
    """`wal_checkpoint(TRUNCATE)` не может перенести страницы, пока открыт читатель со старым снимком (бэкап,
    долгий запрос). Результат (busy=1) не проверялся, повтора не было -> старая страница с текстом оставалась
    в основном файле work после «успешного» по счётчикам прогона НАВСЕГДА (до случайного автосчекпойнта).

    Уточнение при воспроизведении: пока читатель (даже после `COMMIT` своей транзакции — снимок держит само
    ОТКРЫТОЕ соединение, не только незакрытая транзакция) жив, `wal_checkpoint(TRUNCATE)` не переносит ничего
    в основной файл вообще (проверено экспериментом `probe_ret9`) — слово остаётся сразу после выхода из
    `with db.reader()`, ДО следующего прогона. Починка (п.1/R1) — не в том, что тот же прогон как-то обходит
    открытого читателя (это невозможно), а в том, что busy=1 оставляет флаг «грязно»: СЛЕДУЮЩИЙ прогон (когда
    читателя уже нет) доводит физическую уборку до конца сам, без нового логического «есть что почистить»."""
    acc = _account(db)
    now0 = 400 * DAY
    word = "ЧИТАТЕЛЬДЕРЖИТСНИМОКСЛОВО"
    _apply(db, acc, [_up("old", now0 - 91 * DAY, text=f"текст {word}")], now0 - 91 * DAY)
    _apply(db, acc, [_up("anchor", now0, ch="c2")], now0)
    db.maintenance_sync(lambda c: c.execute("PRAGMA work.wal_checkpoint(TRUNCATE)").fetchall())
    assert word.encode() in db.work_path.read_bytes()
    with db.reader() as r:
        r.execute("BEGIN")
        r.execute("SELECT COUNT(*) FROM work.message").fetchone()  # снимок как у бэкапа
        assert retention.run_once(db, now0)["messages"] == 1
        r.execute("COMMIT")
    # читатель закрыт — физический шаг мог не довершиться (checkpoint был busy). Логически чистить уже
    # нечего (строка уже purged=1) — `any_done` будет False, но флаг «грязно» должен заставить прогон всё
    # равно попытаться убрать байты физически.
    assert retention.run_once(db, now0 + 1) is None
    assert word.encode() not in db.work_path.read_bytes(), "повторный прогон не довёл уборку до конца"


async def test_thr6b_ui_thread_read_on_plain_message_creates_fake_thread(client, db, clock):
    """Тредовая `read` до сообщения без ответов (родитель допустим) создаёт строку треда -> у обычного
    сообщения в ленте появляется `thread: {reply_count: 0}` (раздел B: строку для 0 ответов не создавать)."""
    acc = _account(db)
    _apply(db, acc, [_up("P", 1000)], 1000)
    ch = _channel_id(db)
    pid = _one(db, "SELECT id FROM work.message WHERE ext_id = 'P'")[0]
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/channels/{ch}/threads/P/read", json={"up_to_message_id": pid}, headers=csrf_headers(cookie)
    )
    assert r.status_code in (200, 404, 422)
    msgs = (await client.get(f"/api/v1/channels/{ch}/messages")).json()["messages"]
    assert msgs[0]["thread"] is None, f"фиктивная сводка: {msgs[0]['thread']}"


def test_ext2_thread_window_contains_parent_depending_on_arrival_order(db):
    """`/channels/window?thread_ext_id=` — «окно ОТВЕТОВ треда», но фильтр только `thread_ext_id = ?`:
    родитель, пришедший с `thread_ext_id == ext_id` (так его отдаёт `replies`), В окне; пришедший раньше
    ответов (NULL) — нет. Состав окна зависит от порядка прихода; сверка 2c, считающая «ответы» без
    родителя, пометит родителя удалённым."""
    from hub.core import queries

    acc = _account(db)
    _apply(db, acc, [_up("P1", 1000, thread_ext_id="P1"), _up("R1", 2000, thread_ext_id="P1")], 3000)
    with db.reader() as c:
        window = queries.connector_channel_window(c, acc, "c1", 50, thread_ext_id="P1")
    assert window == ["R1"], window


def test_thr8b_history_snapshot_undoes_explicit_unsubscribe(db):
    """Спека 2c (стр. 65-68): из истории/событий `thread` идёт БЕЗ `subscribed` — ровно чтобы подписка не
    мигала. Ядро трактует отсутствие как «источник сказал NULL»: `source_subscribed` стирается, и
    следующий чужой ответ в треде с МОИМ родителем снова подписывает тред, хотя в Slack я от него
    отписался."""
    acc = _account(db)
    _apply(db, acc, [_up("P", 1000, is_me=True, thread={"reply_count": 1, "subscribed": False}),
                     _up("R1", 2000, thread_ext_id="P")], 3000)
    assert _one(db, "SELECT subscribed FROM work.thread WHERE ext_id = 'P'")[0] == 0
    _apply(db, acc, [_up("P", 1000, is_me=True, thread={"reply_count": 2})], 4000)  # conversations.history
    _apply(db, acc, [_up("R2", 5000, thread_ext_id="P")], 5000)  # новый чужой ответ, живое событие
    assert _one(db, "SELECT subscribed FROM work.thread WHERE ext_id = 'P'")[0] == 0, "отписка отменена"


def test_thr1b_unsubscribed_threads_flood_unread_list(db):
    """2e показывает кнопку «Треды (N)» по `unread_threads` канала и список из `GET /threads?unread=1`. Список
    отдаёт и НЕПОДПИСАННЫЕ треды (их маркера нет -> все чужие ответы «непрочитаны»): бейдж 0, список — все
    треды канала."""
    from hub.core import queries
    from hub.core.security import Principal

    acc = _account(db)
    now0 = 400 * DAY
    _apply(db, acc, [_up("P", now0 - 1000), _up("R1", now0 - 500, thread_ext_id="P")], now0)
    ch = _channel_id(db)
    principal = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    with db.reader() as c:
        assert unread_breakdown(c, ch)[1] == 0  # тред чужой, не подписан
        threads = queries.list_channel_threads(c, principal, ch, unread_only=True, limit=50)
    ext_ids = [t["thread_ext_id"] for t in threads]
    assert threads == [], f"список «тредов с непрочитанным» при unread_threads=0: {ext_ids}"
