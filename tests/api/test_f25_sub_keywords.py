"""Ф25-1 (docs/architecture.md и поправки П1–П5): фильтр подписки «лично мне» по
ключевым словам, ядро.

- C1: миграция 0039.
- C2 + П3: `keywords.normalize`/`matches`/`check`, скорость проверки.
- C3 + П2, П5: `POST /subs` — ключи, замена на месте, снятие, наследование.
- C4 + П1, П4: сигнал — категория `keyword`, `no_keyword`, заглушённые беседы, история вступления, тред.
- C5: накопление ключей.
- C6: выдача и `msg_signal` дежурной.
- C7 + П3, П4: пометка `keys` в чтении — только bearer.

Стиль — как test_f20_bots_personal.py: публичное приложение через ASGITransport, события через
`ingest.apply_batch`, `/proc` подменён словарём."""

import json
import time

import pytest

from hub.core import chan_subs, ingest, keywords, security, subs
from hub.core import db as db_module
from hub.core.db import Db

from .conftest import insert_account, insert_channel, insert_project, login_via_code

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
SID2 = "sess-bbbbbbbb-2222"


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    other = insert_project(db, name="Другой")
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    sl = insert_account(db, source="slack")
    tg = insert_account(db, source="tg")
    ch = {}
    for ext, kind in (("D1", "dm"), ("C1", "group"), ("C2", "group"), ("C3", "group")):
        ch[ext] = insert_channel(db, sl, project_id=pid, ext_id=ext, kind=kind)
    ch["MUTED"] = insert_channel(db, tg, project_id=pid, ext_id="MUTED", kind="group", muted=1)
    ch["ARCH"] = insert_channel(db, tg, project_id=pid, ext_id="ARCH", kind="group", archived=1)
    ch["OTHER"] = insert_channel(db, tg, project_id=other, ext_id="OTHER", kind="group")
    return {
        "pid": pid,
        "other": other,
        "workdir": str(workdir),
        "acc": {"slack": sl, "tg": tg},
        "ch": ch,
        "tok": _token(db),
    }


def _session(w, *, sid=SID, full=True):
    body = {"pid": PID, "proc_start": START, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


async def _subscribe(client, w, *, sid=SID, expect=200, **extra):
    body: dict = {"session": _session(w, sid=sid), **extra}
    if extra.get("mode") == "personal":
        body["mode"], body["msg_filter"] = "messages", "personal"
    r = await client.post("/api/v1/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _personal(client, w, *, sid=SID, **extra):
    return await _subscribe(client, w, sid=sid, mode="personal", **extra)


async def _wait(client, w, sub_id, *, sid=SID):
    return await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, sid=sid, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )


def _ev(
    chat,
    ext,
    ts,
    *,
    kind="group",
    is_me=False,
    bot=False,
    mention=False,
    thread=None,
    reply_to=None,
    text="x",
):
    snd: dict = {"ext_id": "U1", "is_me": True} if is_me else {"ext_id": "U2", "name": "Пётр"}
    if bot:
        snd["is_bot"] = True
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind},
        "ext_id": ext,
        "ts": ts,
        "sender": snd,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": mention,
    }
    if thread is not None:
        event["thread_ext_id"] = thread
    if reply_to is not None:
        event["reply_to_ext_id"] = reply_to
    return event


def _ingest(db, account_id, events, now_ms):
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0, result


def _now(clock) -> int:
    return int(clock() * 1000)


def _signals(db) -> dict[int, list[tuple]]:
    """Беседа -> строки `(status, reason, msg_count, cat, thread_ext, keys)`."""
    out: dict[int, list[tuple]] = {}
    with db.reader() as conn:
        for row in conn.execute(
            "SELECT channel_id, status, reason, msg_count, cat, thread_ext, keys_json "
            "FROM main.session_chan_signal ORDER BY id"
        ):
            keys = json.loads(row[6]) if row[6] else None
            out.setdefault(row[0], []).append((*row[1:6], keys))
    return out


def _sub_row(db, sub_id):
    with db.reader() as conn:
        return subs.get_sub(conn, sub_id)


def _audit(db, action):
    with db.reader() as conn:
        return [
            json.loads(d)
            for (d,) in conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
            )
        ]


KEYS = {"keywords": ["Штраф", "задерж выплат", "удержа"], "exclude": ["вакансия", "курс"]}


# --- C1: миграция 0039 --------------------------------------------------------------------------------------


def test_c1_migration_0039(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 38}
    )
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts, mode, msg_filter) "
                "VALUES (1, 1, 1, 1, 's', '/', 0, 'messages', 'personal')"
            )
        )
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 38
            work_version = conn.execute("PRAGMA work.user_version").fetchone()[0]
    finally:
        old.close()
    # Ф28-1: до 0039 включительно — более поздние `_shared` (0041) законно двигают версию work.
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 39}
    )
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 39
                assert (
                    conn.execute("PRAGMA work.user_version").fetchone()[0] == work_version
                )  # work не тронут
                sub_cols = {r[1] for r in conn.execute("PRAGMA main.table_info(session_sub)")}
                sig_cols = {r[1] for r in conn.execute("PRAGMA main.table_info(session_chan_signal)")}
                assert "keywords_json" in sub_cols and "keys_json" in sig_cols
                assert conn.execute("SELECT keywords_json FROM main.session_sub").fetchall() == [(None,)]
                work_tables = {r[0] for r in conn.execute("SELECT name FROM work.sqlite_master")}
                assert "session_sub" not in work_tables
        finally:
            new.close()


# --- C2 + П3: нормализация и совпадение --------------------------------------------------------------------


def test_c2_normalize():
    assert keywords.normalize(["  ШТРАФ!! ", "Задёрж,   выплат", "штраф"], 100) == ["штраф", "задерж выплат"]
    assert keywords.normalize(["Бан  карточк", "WB 2024"], 100) == ["бан карточк", "wb 2024"]
    for values, needle in (
        (["раз два три четыре"], "ключ 1: больше 3 слов"),
        (["штраф", "я"], "ключ 2: слово короче 2"),
        (["а" * 31], "ключ 1: слово длиннее 30"),
        (["ok", 5], "ключ 2: не строка"),
        (["ok", "!!!"], "ключ 2: нет ни одного слова"),
    ):
        with pytest.raises(keywords.Invalid, match=needle):
            keywords.normalize(values, 100)
    with pytest.raises(keywords.Invalid, match="ключ 101: больше 100"):
        keywords.normalize([f"слово{i}" for i in range(101)], 100)
    # дубли не считаются в предел
    assert len(keywords.normalize([f"слово{i % 100}" for i in range(150)], 100)) == 100


def test_c2_matches_and_check():
    def m(keys, text):
        return keywords.matches(keys, keywords.words(text))

    assert m(["задерж выплат"], "Задержали выплату!") == ["задерж выплат"]
    assert m(["штраф"], "опять штрафы") == ["штраф"]
    assert m(["штраф"], "штрафовать") == ["штраф"]
    assert m(["штраф"], "нас оштрафовали") == []
    assert m(["оштраф"], "нас оштрафовали") == ["оштраф"]
    # фраза — подряд идущие слова, не через слово
    assert m(["задерж выплат"], "задержали нам выплату") == []
    # кириллица, латиница, цифры, ё
    assert m(["wildber", "ozon", "54 фз"], "Wildberries и OZON по 54-ФЗ") == ["wildber", "ozon", "54 фз"]
    assert m(["еще"], "Ещё раз") == ["еще"]
    # порядок — как в списке ключей
    spec = {"keys": ["удержа", "штраф"], "exclude": ["вакансия"]}
    compiled = keywords.compile(spec)
    assert keywords.check(compiled, "Штраф и удержание") == ["удержа", "штраф"]
    assert keywords.check(compiled, "Вакансия: штрафы не платим") is None
    assert keywords.check(compiled, "ничего нет") is None
    assert keywords.check(compiled, None) is None
    assert keywords.check(keywords.compile(None), "штраф") is None
    assert keywords.check(keywords.compile({"keys": [], "exclude": []}), "штраф") is None
    # раскладка по двум буквам ловит то же, что простая проверка
    keys = ["шт", "штраф", "штраф пени", "пени", "ozon"]
    text = "Штрафы пени и OZON штраф"
    assert keywords.check(keywords.compile({"keys": keys}), text) == keywords.matches(
        keys, keywords.words(text)
    )


def test_c2_key_re_and_safe_keys():
    assert keywords.KEY_RE.fullmatch("задерж выплат")
    assert not keywords.KEY_RE.fullmatch("штраф\n")
    assert not keywords.KEY_RE.fullmatch('штраф"')
    assert not keywords.KEY_RE.fullmatch("раз два три четыре")
    assert keywords.safe_keys(["штраф", "удержа"]) == ["штраф", "удержа"]
    assert keywords.safe_keys(["штраф", "x\ny"]) is None
    assert keywords.safe_keys(["a" * 31]) is None
    assert keywords.safe_keys([f"к{i}к" for i in range(6)]) is None
    assert keywords.safe_keys("штраф") is None


@pytest.mark.parametrize(
    ("keys", "text"),
    [
        # худший случай раскладки: у всех ключей одни первые буквы, первые слова совпадают везде, вторые — нет
        ([f"ключ{i:03d} слово" for i in range(100)], " ".join(f"ключ{i % 100:03d}" for i in range(1000))),
        # сто ключей на «шт» разной длины, письмо из одних «штрафы»
        ([f"шт{'а' * (i % 20)}{i}" for i in range(100)], " ".join(["штрафы"] * 1000)),
        # обычное письмо и обычный список: разные первые буквы
        ([f"{chr(0x430 + i % 32)}{chr(0x430 + i // 32)}слово{i}" for i in range(100)], "Добрый день, " * 500),
    ],
    ids=["same-prefix", "sh-lengths", "plain"],
)
def test_p3_check_speed_1000_words_100_keys(keys, text):
    keys = keywords.normalize(keys, 100)
    assert len(keys) == 100 and len(keywords.words(text)) == 1000
    compiled = keywords.compile({"keys": keys, "exclude": ["вакансия", "курс"]})
    runs = 20
    started = time.perf_counter()
    for _ in range(runs):
        keywords.check(compiled, text)
    per_message = (time.perf_counter() - started) / runs
    assert per_message < 0.020, per_message


# --- C3 + П2, П5: POST /subs -----------------------------------------------------------------------------


async def test_c3_keywords_validation(client, db, world, procs):
    r = await _subscribe(client, world, mode="messages", keywords=["штраф"], expect=422)
    assert "personal" in json.dumps(r, ensure_ascii=False)
    await _subscribe(client, world, keywords=["штраф"], expect=422)
    await _subscribe(client, world, mode="cards", keywords=["штраф"], expect=422)
    body = {"session": _session(world), "mode": "messages", "msg_filter": "personal", "exclude": ["курс"]}
    r = await client.post("/api/v1/subs", json=body, headers=_h(world["tok"]))
    assert r.status_code == 422 and "exclude" in r.text
    body = {**body, "keywords": [], "exclude": ["курс"]}
    r = await client.post("/api/v1/subs", json=body, headers=_h(world["tok"]))
    assert r.status_code == 422
    body = {**body, "keywords": ["раз два три четыре"], "exclude": []}
    r = await client.post("/api/v1/subs", json=body, headers=_h(world["tok"]))
    assert r.status_code == 422 and "ключ 1: больше 3 слов" in r.json()["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.session_sub").fetchone()[0] == 0


async def test_c3_replace_in_place_clear_and_switch(client, db, world, procs):
    body = await _personal(client, world, **KEYS)
    sub = body["sub"]
    assert body["created"] is True
    assert sub["keywords"] == {
        "keys": ["штраф", "задерж выплат", "удержа"],
        "exclude": ["вакансия", "курс"],
        "batch_min": 15,  # Ф36-1 A4: действующий интервал пачки
    }
    assert _audit(db, "sub_create")[-1]["keys"] == 3 and _audit(db, "sub_create")[-1]["exclude"] == 2
    # П5: смена одних ключей своей живой — на месте
    body = await _personal(client, world, keywords=["бан карточк"])
    assert body["created"] is False and body["sub"]["id"] == sub["id"]
    assert body["sub"]["keywords"] == {"keys": ["бан карточк"], "exclude": [], "batch_min": 15}
    assert _audit(db, "sub_mode")[-1] == {
        "sub_id": sub["id"],
        "mode": "messages",
        "msg_filter": "personal",
        "keys": 1,
        "exclude": 0,
    }
    # без ключей в запросе — ключи остаются
    body = await _personal(client, world)
    assert body["sub"]["keywords"] == {"keys": ["бан карточк"], "exclude": [], "batch_min": 15}
    assert len(_audit(db, "sub_mode")) == 1
    # keywords=[] — сняты
    body = await _personal(client, world, keywords=[])
    assert body["sub"]["keywords"] is None and _sub_row(db, sub["id"]).keywords_json is None
    # смена на `all` снимает
    await _personal(client, world, **KEYS)
    body = await _subscribe(client, world, mode="messages", msg_filter="all")
    assert body["sub"]["msg_filter"] == "all" and body["sub"]["keywords"] is None
    assert body["sub"]["id"] == sub["id"]


async def test_p2_inherit_after_conversation_changed(client, db, world, procs):
    first = (await _personal(client, world, **KEYS))["sub"]
    # /clear: новый разговор того же процесса зовёт hub_subscribe() без аргументов
    body = await _subscribe(client, world, sid=SID2)
    sub = body["sub"]
    assert body["created"] is True and body["inherited_from"] == first["id"]
    assert (sub["mode"], sub["msg_filter"]) == ("messages", "personal")
    assert sub["keywords"] == first["keywords"]
    assert _sub_row(db, first["id"]).end_reason == "conversation_changed"
    assert _audit(db, "sub_create")[-1]["inherited_from"] == first["id"]


async def test_p2_personal_keys_back_after_cards(client, db, world, procs):
    first = (await _personal(client, world, **KEYS))["sub"]
    await _subscribe(client, world, sid=SID2, mode="cards")
    body = await _personal(client, world, sid="sess-3")
    assert body["inherited_from"] == first["id"] and body["sub"]["keywords"] == first["keywords"]


async def test_p2_cleared_keys_not_inherited(client, db, world, procs):
    await _personal(client, world, **KEYS)
    await _personal(client, world, keywords=[])
    body = await _subscribe(client, world, sid=SID2)
    assert (body["sub"]["mode"], body["sub"]["msg_filter"]) == ("messages", "personal")
    assert body["sub"]["keywords"] is None
    body = await _personal(client, world, sid="sess-3")
    assert body["sub"]["keywords"] is None and "inherited_from" not in body


@pytest.mark.parametrize("prev", ["all", "cards"])
async def test_p2_no_inherit_keys_from_all_or_cards(client, db, world, procs, prev):
    await _personal(client, world, **KEYS)
    if prev == "all":
        await _subscribe(client, world, mode="messages", msg_filter="all")
    else:
        await _subscribe(client, world, mode="cards")
    body = await _subscribe(client, world, sid=SID2)
    sub = body["sub"]
    assert sub["keywords"] is None
    assert sub["mode"] == ("messages" if prev == "all" else "cards")
    assert sub["msg_filter"] == "all"


async def test_p2_first_sub_without_mode_is_cards(client, db, world, procs):
    body = await _subscribe(client, world)
    assert body["sub"]["mode"] == "cards" and "inherited_from" not in body


# --- C4 + П1, П4: сигнал --------------------------------------------------------------------------------


async def test_c4_keyword_signals(client, db, world, procs, clock):
    await _personal(client, world, **KEYS)
    now = _now(clock)
    sl = world["acc"]["slack"]
    _ingest(
        db,
        sl,
        [
            _ev("C1", "1.0", now, text="Опять ШТРАФЫ на складе"),
            _ev("C2", "1.0", now, text="просто болтовня"),
            _ev("C2", "2.0", now + 1, text="вакансия: штраф не платим"),
            _ev("D1", "1.0", now, kind="dm", text="Какой курс?"),  # личное — минус-слово не действует
            _ev("C3", "1.0", now, bot=True, text="штраф начислен"),
        ],
        now,
    )
    ch = world["ch"]
    got = _signals(db)
    assert got[ch["C1"]] == [("pending", None, 1, "keyword", None, ["штраф"])]
    assert got[ch["C2"]] == [("skipped", "no_keyword", 2, None, None, None)]
    assert got[ch["D1"]] == [("pending", None, 1, "dm", None, None)]
    assert got[ch["C3"]] == [("skipped", "bot", 1, None, None, None)]


async def test_c4_muted_channel_listened_only_with_keys(client, db, world, procs, clock):
    now = _now(clock)
    tg = world["acc"]["tg"]
    muted = world["ch"]["MUTED"]
    await _personal(client, world)  # без ключей — заглушённая молчит
    _ingest(db, tg, [_ev("MUTED", "1", now, text="штраф")], now)
    assert muted not in _signals(db)
    await _personal(client, world, **KEYS)
    _ingest(
        db,
        tg,
        [
            _ev("MUTED", "2", now + 1, text="штраф"),
            _ev("MUTED", "3", now + 2, is_me=True, text="пост маркетолога"),
            _ev("MUTED", "4", now + 3, reply_to="3", text="спасибо"),  # ответ на сообщение владельца
            _ev("MUTED", "5", now + 4, text="ни о чём"),
        ],
        now,
    )
    assert _signals(db)[muted] == [
        ("pending", None, 2, "thread", None, ["штраф"]),
        ("skipped", "no_keyword", 1, None, None, None),
    ]
    # архив и чужой проект — ничего
    _ingest(db, tg, [_ev("ARCH", "1", now, text="штраф"), _ev("OTHER", "1", now, text="штраф")], now)
    got = _signals(db)
    assert world["ch"]["ARCH"] not in got and world["ch"]["OTHER"] not in got


async def test_c4_muted_project_silent(client, db, world, procs, clock):
    await _personal(client, world, **KEYS)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.project SET muted = 1 WHERE id = ?", (world["pid"],))
    )
    now = _now(clock)
    _ingest(
        db,
        world["acc"]["slack"],
        [_ev("C1", "1.0", now, text="штраф"), _ev("D1", "1.0", now, kind="dm")],
        now,
    )
    assert _signals(db) == {}


async def test_p1_join_history_does_not_signal(client, db, world, procs, clock):
    """П1: вступление заглушает беседу и подгружает историю — история (старше `first_seen_ts`) не сигналит
    даже в окне подписки; новое сообщение после — сигнал."""
    await _personal(client, world, **KEYS)
    now = _now(clock)
    tg = world["acc"]["tg"]
    joined = insert_channel(
        db, tg, project_id=world["pid"], ext_id="JOIN", kind="group", muted=1, first_seen_ts=now
    )
    history = [_ev("JOIN", str(i), now - 5 * 60 * 1000 + i, text=f"штраф {i}") for i in range(5)]
    history.append(_ev("JOIN", "50", now - 60_000, mention=True, text="штраф лично"))
    _ingest(db, tg, history, now)
    assert joined not in _signals(db)
    _ingest(db, tg, [_ev("JOIN", "100", now + 1000, text="задержали выплату")], now + 1000)
    assert _signals(db)[joined] == [("pending", None, 1, "keyword", None, ["задерж выплат"])]


async def test_c4_chan_sub_wins(client, db, world, procs, clock):
    await _personal(client, world, **KEYS)
    # Ф26-1: подписка на беседу глушит подписку на проект только той же сессии
    r = await client.post(
        "/api/v1/chan-subs",
        json={
            "channel_id": world["ch"]["C1"],
            "session": _session(world),
        },
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    now = _now(clock)
    _ingest(db, world["acc"]["slack"], [_ev("C1", "1.0", now, text="ни о чём")], now)
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT chan_sub_id, project_sub_id, status FROM main.session_chan_signal"
        ).fetchall()
    assert len(rows) == 1 and rows[0][0] is not None and rows[0][1] is None and rows[0][2] == "pending"


async def test_p4_keyword_in_slack_thread_carries_thread_ext(client, db, world, procs, clock):
    await _personal(client, world, **KEYS)
    now = _now(clock)
    _ingest(
        db,
        world["acc"]["slack"],
        [_ev("C1", "10.0", now, text="тема"), _ev("C1", "10.1", now + 1, thread="10.0", text="удержали 5%")],
        now,
    )
    assert _signals(db)[world["ch"]["C1"]] == [
        ("skipped", "no_keyword", 1, None, None, None),  # родитель «тема» — без ключа
        ("pending", None, 1, "keyword", "10.0", ["удержа"]),
    ]


# --- C5: накопление --------------------------------------------------------------------------------------


async def test_c5_keys_union_and_category_not_lowered(client, db, world, procs, clock):
    keys = ["штраф", "удержа", "бан", "выплат", "склад", "ozon", "возврат"]
    await _personal(client, world, keywords=keys)
    now = _now(clock)
    sl = world["acc"]["slack"]
    _ingest(
        db,
        sl,
        [
            _ev("C1", "1.0", now, text="штраф и удержание"),
            _ev("C1", "2.0", now + 1, text="бан и снова штраф"),
            _ev("C1", "3.0", now + 2, text="выплаты, склад, ozon, возврат"),
        ],
        now,
    )
    assert _signals(db)[world["ch"]["C1"]] == [
        ("pending", None, 3, "keyword", None, ["штраф", "удержа", "бан", "выплат", "склад"])
    ]
    # `keyword` после `thread` категорию не понижает, ключи дописываются
    _ingest(
        db, sl, [_ev("C2", "5.0", now, is_me=True), _ev("C2", "5.1", now + 1, thread="5.0", text="ок")], now
    )
    _ingest(db, sl, [_ev("C2", "6.0", now + 2, text="штраф")], now)
    assert _signals(db)[world["ch"]["C2"]] == [("pending", None, 2, "thread", "5.0", ["штраф"])]


# --- C6: выдача -----------------------------------------------------------------------------------------


async def test_c6_delivery_keys_and_wake_sees_no_keyword(client, db, world, procs, clock):
    sub = (await _personal(client, world, batch_min=0, **KEYS))["sub"]  # Ф36-1: без пачки — выдача сразу
    now = _now(clock)
    sl = world["acc"]["slack"]
    _ingest(db, sl, [_ev("C1", "1.0", now, text="штраф"), _ev("C2", "1.0", now, text="ни о чём")], now)
    r = await _wait(client, world, sub["id"])
    assert r.status_code == 200, r.text
    signals = r.json()["signals"]
    assert len(signals) == 1
    assert signals[0]["channel_id"] == world["ch"]["C1"]
    assert (signals[0]["cat"], signals[0]["keys"]) == ("keyword", ["штраф"])
    details = _audit(db, "sub_signal")
    assert details[-1]["keys"] == 1 and details[-1]["cat"] == "keyword"
    # дежурная: отсеянное фильтром — «сессия получила» (`msg_signal` строки `no_keyword`)
    c2 = world["ch"]["C2"]
    items = [{"id": 1, "channel_id": c2, "project_id": world["pid"]}]
    with db.reader() as conn:
        chan_subs.attach_msg_signals(conn, items)
    assert items[0]["msg_signal"]["status"] == "skipped"
    assert items[0]["msg_signal"]["reason"] == "no_keyword"
    assert "no_keyword" in chan_subs.FILTER_REASONS


# --- C7 + П3, П4: пометка `keys` в чтении ----------------------------------------------------------------


async def test_c7_read_keys_bearer_only(client, db, world, procs, clock):
    await _personal(client, world, **KEYS)
    now = _now(clock)
    sl = world["acc"]["slack"]
    tg = world["acc"]["tg"]
    _ingest(
        db,
        sl,
        [
            _ev("C1", "1.0", now, text="штраф пришёл"),
            _ev("C1", "2.0", now + 1, is_me=True, text="мой штраф"),
            _ev("C1", "3.0", now + 2, text="вакансия: штраф"),
            _ev("C1", "4.0", now + 3, text="ни о чём"),
            _ev("C1", "4.1", now + 4, thread="4.0", text="удержали"),
        ],
        now,
    )
    _ingest(db, tg, [_ev("OTHER", "1", now, text="штраф")], now)
    headers = _h(world["tok"])
    c1 = world["ch"]["C1"]

    def keys_of(messages):
        return {m["ext_id"]: m.get("keys") for m in messages}

    r = await client.get(f"/api/v1/channels/{c1}/messages", headers=headers)
    assert r.status_code == 200, r.text
    got = keys_of(r.json()["messages"])
    assert got["1.0"] == ["штраф"]
    assert got["2.0"] is None and got["3.0"] is None and got["4.0"] is None
    r = await client.get(f"/api/v1/channels/{c1}/wait", params={"after": 0, "timeout_s": 1}, headers=headers)
    assert r.status_code == 200, r.text
    got = keys_of(r.json()["messages"])
    assert got["1.0"] == ["штраф"] and got["2.0"] is None
    r = await client.get(f"/api/v1/channels/{c1}/threads/4.0/messages", headers=headers)
    assert r.status_code == 200, r.text
    assert keys_of(r.json()["messages"])["4.1"] == ["удержа"]
    # беседа проекта без подписки с ключами — поля нет
    r = await client.get(f"/api/v1/channels/{world['ch']['OTHER']}/messages", headers=headers)
    assert all("keys" not in m for m in r.json()["messages"])
    # UI (кука) — поля нет
    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/channels/{c1}/messages", headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    assert all("keys" not in m for m in r.json()["messages"])


async def test_c7_no_keys_field_without_keyword_sub(client, db, world, procs, clock):
    await _personal(client, world)
    now = _now(clock)
    _ingest(db, world["acc"]["slack"], [_ev("C1", "1.0", now, text="штраф")], now)
    r = await client.get(f"/api/v1/channels/{world['ch']['C1']}/messages", headers=_h(world["tok"]))
    assert all("keys" not in m for m in r.json()["messages"])
