"""Ф44-1a (docs/architecture.md): кнопки под
сообщениями Telegram-ботов в ядре — миграция 0064 (`message.buttons_json`, `buttons_ts`), правило
`buttons_ts` в приёме, чистка при скрытии и ретенции, выдача `buttons` без сырого callback (лента,
`GET /messages/{id}`, `/sync`), `buttons.find` / `callback_data` для куска нажатия."""

import base64
import json

import pytest

from hub.core import buttons, hiding, retention, security, subs
from hub.core.ingest import apply_batch

from .conftest import insert_account, insert_channel, insert_message, insert_project, login_via_code

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
CHAN = {"ext_id": "777", "kind": "dm", "title": "Бот"}
RAW = b"get_key:\x00\xff"
D = base64.b64encode(RAW).decode()
SECRET_D = base64.b64encode(b"SECRETCALLBACK").decode()

B1 = {
    "kind": "inline",
    "rows": [
        [{"t": "Получить ключ", "k": "callback", "d": D}, {"t": "Сайт", "k": "url", "u": "https://e.com/x"}],
        [
            {"t": "Код", "k": "copy", "c": "PROMO"},
            {"t": "Оплатить", "k": "other"},
            {"t": "Удалить", "k": "callback", "d": SECRET_D, "pw": True},
        ],
    ],
}
B2 = {"kind": "inline", "rows": [[{"t": "Назад", "k": "callback", "d": SECRET_D}]]}
PUBLIC_B1 = {
    "kind": "inline",
    "rows": [
        [{"t": "Получить ключ", "k": "callback"}, {"t": "Сайт", "k": "url", "u": "https://e.com/x"}],
        [
            {"t": "Код", "k": "copy", "c": "PROMO"},
            {"t": "Оплатить", "k": "other"},
            {"t": "Удалить", "k": "callback", "pw": True},
        ],
    ],
}
_UNSET = object()


def _account(db, db_name="main") -> int:
    return insert_account(db, source="telegram", db_name=db_name)


def _msg(ext_id, ts, *, etype="upsert", text="привет", edited_ts=None, btn=_UNSET, bts=None, **extra):
    event = {
        "type": etype,
        "channel": CHAN,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
        "sender": {"ext_id": "777", "name": "Бот", "is_bot": True},
        **extra,
    }
    if edited_ts is not None:
        event["edited_ts"] = edited_ts
    if btn is not _UNSET:
        event["buttons"] = btn
    if bts is not None:
        event["buttons_ts"] = bts
    return event


def _ingest(db, acc, events, now):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=now)
    )


def _row(db, ext_id, schema="main"):
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT m.id, m.seq, m.first_seq, m.buttons_json, m.buttons_ts, m.text, m.edited_ts, m.hidden, "
            f"m.purged, m.channel_id FROM {schema}.message m WHERE m.ext_id = ?",
            (ext_id,),
        ).fetchone()
    keys = ("id", "seq", "first_seq", "buttons_json", "buttons_ts", "text", "edited_ts", "hidden", "purged")
    out = dict(zip(keys, row[:-1], strict=True))
    out["cid"] = row[-1]
    out["buttons"] = json.loads(out["buttons_json"]) if out["buttons_json"] else None
    return out


def _edits(db, mid) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.message_edit WHERE message_id = ?", (mid,)).fetchone()[
            0
        ]


def _dead(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.dead_letter").fetchone()[0]


# --- приём -------------------------------------------------------------------------------------------------


def test_new_row_gets_buttons_at_once_other_sources_untouched(db, clock):
    acc = _account(db)
    now = int(clock() * 1000)
    res = _ingest(
        db,
        acc,
        [
            _msg("10", now - 3000, btn=B1, bts=now - 3000),
            _msg("11", now - 2000, btn=None, bts=now - 2000),  # «кнопок нет» — тоже состояние: отметка есть
            _msg("12", now - 1000),  # другой источник / старый коннектор: полей нет вовсе
        ],
        now,
    )
    assert (res.applied, res.dead) == (3, 0)
    row = _row(db, "10")
    assert row["buttons"] == B1 and row["buttons_ts"] == now - 3000
    assert row["seq"] == row["first_seq"]  # кнопки новой строки — без второго seq
    assert (_row(db, "11")["buttons_json"], _row(db, "11")["buttons_ts"]) == (None, now - 2000)
    assert (_row(db, "12")["buttons_json"], _row(db, "12")["buttons_ts"]) == (None, None)


def test_keyboard_only_edit_applies_after_real_text_edit(db, clock):
    """Правку одной клавиатуры Telegram помечает `edit_hide` — у события нет `edited_ts`; после настоящей
    правки текста остальное событие «устарело» (F7), а кнопки применяются своим шагом."""
    acc = _account(db)
    t = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t)], t)
    _ingest(db, acc, [_msg("10", t, etype="edit", text="правка", edited_ts=t + 100, btn=B1, bts=t + 100)], t)
    before = _row(db, "10")
    assert before["text"] == "правка" and before["buttons_ts"] == t + 100
    edits_before = _edits(db, before["id"])

    res = _ingest(db, acc, [_msg("10", t, etype="edit", text="правка", btn=B2, bts=t + 200)], t + 300)
    after = _row(db, "10")
    assert (res.applied, res.noop, res.dead) == (1, 0, 0)
    assert after["buttons"] == B2 and after["buttons_ts"] == t + 200
    assert after["seq"] > before["seq"] and after["first_seq"] == before["first_seq"]
    # не правка: ни истории, ни «(ред.)» сверх прежнего, текст и `edited_ts` на месте
    assert _edits(db, before["id"]) == edits_before
    assert (after["text"], after["edited_ts"]) == ("правка", t + 100)


def test_stale_snapshot_does_not_overwrite_and_same_json_moves_only_ts(db, clock):
    acc = _account(db)
    t = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t)], t)
    _ingest(db, acc, [_msg("10", t, etype="edit", btn=B2, bts=t + 200)], t + 200)
    fresh = _row(db, "10")
    assert fresh["buttons"] == B2

    # старый снимок сверки (кнопки до правки, отметка меньше) — не затирает; содержимое то же -> noop
    res = _ingest(db, acc, [_msg("10", t, btn=B1, bts=t)], t + 300)
    assert (res.applied, res.noop) == (0, 1)
    assert _row(db, "10") == fresh
    # снимок «кнопок нет» со старой отметкой — тоже мимо
    _ingest(db, acc, [_msg("10", t, btn=None, bts=t + 100)], t + 300)
    assert _row(db, "10") == fresh

    # тот же JSON с отметкой новее — отметка вперёд, seq прежний (П5)
    res = _ingest(db, acc, [_msg("10", t, btn=B2, bts=t + 500)], t + 600)
    moved = _row(db, "10")
    assert (res.applied, res.noop) == (0, 1)
    assert (moved["buttons_ts"], moved["seq"], moved["buttons"]) == (t + 500, fresh["seq"], B2)

    # событие без `buttons_ts` (другой путь приёма) кнопки не трогает, даже меняя текст
    _ingest(db, acc, [_msg("10", t, etype="edit", text="новый", edited_ts=t + 700)], t + 700)
    kept = _row(db, "10")
    assert (kept["text"], kept["buttons"], kept["buttons_ts"]) == ("новый", B2, t + 500)

    # бот убрал клавиатуру: `buttons: null` с отметкой новее — кнопки сняты, новый seq
    res = _ingest(db, acc, [_msg("10", t, etype="edit", text="новый", btn=None, bts=t + 900)], t + 900)
    gone = _row(db, "10")
    assert res.applied == 1
    assert (gone["buttons_json"], gone["buttons_ts"]) == (None, t + 900) and gone["seq"] > kept["seq"]


def test_tombstone_does_not_touch_buttons(db, clock):
    acc = _account(db)
    t = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t)], t)
    before = _row(db, "10")
    res = _ingest(db, acc, [_msg("10", t, text=None, btn=None, bts=t + 100, tombstone=True)], t + 100)
    assert (res.applied, res.noop) == (0, 1)
    assert _row(db, "10") == before


def test_limits_and_bad_form_do_not_reject_event(db, clock):
    """П10: сверх пределов или не та форма — сообщение принимается, кнопки обнуляются с `cut`."""
    acc = _account(db)
    t = int(clock() * 1000)
    one = {"t": "x", "k": "other"}
    bad = {
        "20": {"kind": "inline", "rows": [[one]] * 13},  # рядов больше 12
        "21": {"kind": "inline", "rows": [[one] * 101]},  # кнопок больше 100
        "22": {"kind": "inline", "rows": [[{"t": "я" * 129, "k": "other"}]]},  # текст длиннее 128
        "23": {"kind": "inline", "rows": [[{"t": "a", "k": "url", "u": "h" * 1025}]]},
        "24": {"kind": "inline", "rows": [[{"t": "a", "k": "copy", "c": "c" * 257}]]},
        # весь JSON больше 8 КиБ
        "25": {"kind": "inline", "rows": [[{"t": "ю" * 128, "k": "url", "u": "u" * 1000}] * 8] * 2},
        "26": {"kind": "inline", "rows": "мусор"},
        "27": {"kind": "reply", "rows": [[one]]},  # П9: только инлайн
        "28": {"kind": "inline", "rows": [[{"t": "a", "k": "callback", "d": "не base64!"}]]},
        "29": {"kind": "inline", "rows": [[{"t": "a", "k": "other", "x": 1}]]},
        "30": {"kind": "inline", "rows": [[{"t": "a", "k": "other", "d": D}]]},  # `d` не у callback
    }
    events = [_msg(ext, t, btn=value, bts=t) for ext, value in bad.items()]
    events.append(_msg("40", t, btn={"kind": "inline", "rows": [[one] * 8] * 12}, bts=t))  # ровно на пределе
    res = _ingest(db, acc, events, t)
    assert (res.applied, res.dead) == (len(events), 0) and _dead(db) == 0
    for ext in bad:
        assert _row(db, ext)["buttons"] == {"kind": "inline", "rows": [], "cut": True}, ext
        assert _row(db, ext)["text"] == "привет"
    assert len(_row(db, "40")["buttons"]["rows"]) == 12


def test_bad_buttons_ts_is_poison_only_by_type(db, clock):
    acc = _account(db)
    t = int(clock() * 1000)
    res = _ingest(db, acc, [_msg("10", t, btn=B1, bts=0)], t)
    assert res.dead == 1


# --- скрытие и ретенция ------------------------------------------------------------------------------------


def test_hide_scrubs_buttons_and_reconcile_does_not_return_them(db, clock):
    acc = _account(db)
    t = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t)], t)
    mid = _row(db, "10")["id"]
    db.write_sync(lambda conn: hiding.hide_message(conn, mid, now_ms=t + 10))
    hidden = _row(db, "10")
    assert (hidden["hidden"], hidden["buttons_json"], hidden["buttons_ts"]) == (1, None, None)

    # сверка окна перечитала сообщение (в источнике оно живо) — кнопки не возвращаются, seq не растёт
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t + 500)], t + 600)
    _ingest(db, acc, [_msg("10", t, etype="edit", btn=B2, bts=t + 900)], t + 900)
    again = _row(db, "10")
    assert (again["buttons_json"], again["buttons_ts"], again["seq"]) == (None, None, hidden["seq"])
    with db.reader() as conn:
        assert buttons.find(conn, mid, 1, 1) is None


def test_hidden_channel_new_row_born_without_buttons(db, clock):
    acc = _account(db)
    t = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t)], t)
    cid = _row(db, "10")["cid"]
    db.write_sync(lambda conn: hiding.hide_channel(conn, cid, now_ms=t + 10))
    assert _row(db, "10")["buttons_json"] is None
    # догрузка истории под границей удалённой беседы — строка сразу скрыта и без кнопок
    _ingest(db, acc, [_msg("9", t - 1000, btn=B1, bts=t - 1000)], t + 20)
    born = _row(db, "9")
    assert (born["hidden"], born["buttons_json"], born["buttons_ts"]) == (1, None, None)


def test_purged_row_gets_no_buttons_and_physical_purge_nulls_them(db):
    acc = _account(db, db_name="work")
    _ingest(db, acc, [_msg("10", 100, btn=B1, bts=100), _msg("11", 200, btn=B1, bts=200)], 1000)
    assert _row(db, "10", "work")["buttons"] == B1
    cleaned, _last, _more = db.write_sync(
        lambda conn: retention._purge_message_portion(conn, after_id=0, cutoff=150)
    )
    assert cleaned == 1
    purged = _row(db, "10", "work")
    assert (purged["purged"], purged["buttons_json"], purged["buttons_ts"]) == (1, None, None)
    assert _row(db, "11", "work")["buttons"] == B1
    # повтор сверкой по очищенной строке кнопки не возвращает
    _ingest(db, acc, [_msg("10", 100, btn=B2, bts=900)], 2000)
    again = _row(db, "10", "work")
    assert (again["buttons_json"], again["buttons_ts"], again["seq"]) == (None, None, purged["seq"])


def test_ingest_retention_cutoff_new_and_existing_rows(db, monkeypatch):
    """Ретенция при приёме (включена константой): старше порога — новая строка без кнопок, существующая
    очищается вместе с кнопками."""
    acc = _account(db, db_name="work")
    day = 24 * 60 * 60 * 1000
    now = 400 * day
    _ingest(db, acc, [_msg("10", now - 200 * day, btn=B1, bts=now - 200 * day)], now)
    assert _row(db, "10", "work")["buttons"] == B1  # ретенция снята — принято как есть
    monkeypatch.setattr(retention, "RETENTION_ENABLED", True)
    # свежее — опорное время ретенции (порог считается один раз на пачку — поэтому отдельной пачкой)
    _ingest(db, acc, [_msg("12", now, btn=B2, bts=now)], now)
    _ingest(
        db,
        acc,
        [
            _msg("11", now - 200 * day, btn=B1, bts=now - 200 * day),
            _msg("10", now - 200 * day, btn=B2, bts=now),
        ],
        now,
    )
    assert _row(db, "12", "work")["buttons"] == B2
    born = _row(db, "11", "work")
    assert (born["purged"], born["buttons_json"], born["buttons_ts"]) == (1, None, None)
    old = _row(db, "10", "work")
    assert (old["purged"], old["buttons_json"], old["buttons_ts"]) == (1, None, None)


# --- сигналы подписчикам -----------------------------------------------------------------------------------


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


async def test_keyboard_change_gives_no_signal_no_unread(client, db, clock, procs, tmp_path):
    acc = _account(db)
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="P", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    now = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", now - 5000, btn=B1, bts=now - 5000)], now - 3000)
    cid = _row(db, "10")["cid"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?", (pid, cid)
        )
    )
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    session = {
        "pid": PID,
        "proc_start": START,
        "session_id": SID,
        "cwd": str(workdir),
        "kind": "interactive",
        "name": None,
        "name_source": None,
    }
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": cid, "session": session},
        headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "2"},
    )
    assert r.status_code == 200, r.text

    def state():
        from hub.core.ingest import unread_breakdown

        with db.reader() as conn:
            signals = conn.execute("SELECT COUNT(*) FROM main.session_chan_signal").fetchone()[0]
            chan = conn.execute("SELECT seq, last_msg_ts FROM main.channel WHERE id = ?", (cid,)).fetchone()
            return signals, tuple(chan), unread_breakdown(conn, cid)

    before, row_before = state(), _row(db, "10")
    res = _ingest(db, acc, [_msg("10", now - 5000, etype="edit", btn=B2, bts=now)], now + 1000)
    assert res.applied == 1 and _row(db, "10")["seq"] > row_before["seq"]
    assert state() == before  # ни сигнала, ни активности беседы, ни непрочитанного

    # зубы: новое сообщение бота — сигнал есть
    _ingest(db, acc, [_msg("11", now + 2000, btn=B1, bts=now + 2000)], now + 3000)
    assert state()[0] == before[0] + 1


# --- выдача ------------------------------------------------------------------------------------------------


def _assert_no_raw(response) -> None:
    assert D not in response.text and SECRET_D not in response.text
    assert '"d":' not in response.text.replace(" ", "")
    assert "buttons_json" not in response.text and "buttons_ts" not in response.text


async def test_public_api_has_buttons_without_raw_callback(client, db, clock):
    acc = _account(db)
    pid = insert_project(db, name="P")
    now = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", now - 2000, btn=B1, bts=now - 2000), _msg("11", now - 1000)], now)
    row = _row(db, "10")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?", (pid, row["cid"])
        )
    )
    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/channels/{row['cid']}/messages")
    by_ext = {m["ext_id"]: m for m in r.json()["messages"]}
    assert by_ext["10"]["buttons"] == PUBLIC_B1
    assert by_ext["11"]["buttons"] is None
    _assert_no_raw(r)

    r = await client.get(f"/api/v1/messages/{row['id']}")
    assert r.json()["buttons"] == PUBLIC_B1
    _assert_no_raw(r)

    r = await client.get("/api/v1/sync", params={"since": 0})
    assert {m["ext_id"]: m["buttons"] for m in r.json()["messages"]}["10"] == PUBLIC_B1
    _assert_no_raw(r)

    # правка клавиатуры едет в `/sync` новым seq
    since = r.json()["max_seq"]
    _ingest(db, acc, [_msg("10", now - 2000, etype="edit", btn=B2, bts=now)], now + 10)
    r = await client.get("/api/v1/sync", params={"since": since})
    changed = {m["ext_id"]: m["buttons"] for m in r.json()["messages"]}
    assert changed == {"10": {"kind": "inline", "rows": [[{"t": "Назад", "k": "callback"}]]}}
    _assert_no_raw(r)

    # скрытое: заглушка `/sync` без кнопок
    since = r.json()["max_seq"]
    db.write_sync(lambda conn: hiding.hide_message(conn, row["id"], now_ms=now + 20))
    r = await client.get("/api/v1/sync", params={"since": since})
    stub = next(m for m in r.json()["messages"] if m["id"] == row["id"])
    assert stub["hidden"] is True and "buttons" not in stub
    _assert_no_raw(r)


def test_public_form_is_allow_list():
    assert buttons.public(None) is None and buttons.public("") is None and buttons.public("{мусор") is None
    assert buttons.public(json.dumps({"rows": "x"})) is None
    raw = {"kind": "inline", "rows": [[{"t": "a", "k": "callback", "d": D, "zzz": 1, "u": "http://x"}]]}
    assert buttons.public(json.dumps(raw)) == {"kind": "inline", "rows": [[{"t": "a", "k": "callback"}]]}
    cut = buttons.public(buttons.normalize({"kind": "inline", "rows": "мусор"}))
    assert cut == {"kind": "inline", "rows": [], "cut": True}


# --- для куска нажатия (44-1c) -----------------------------------------------------------------------------


def test_find_and_callback_data(db, clock):
    acc = _account(db)
    t = int(clock() * 1000)
    _ingest(db, acc, [_msg("10", t, btn=B1, bts=t), _msg("11", t + 1)], t + 2)
    mid, plain = _row(db, "10")["id"], _row(db, "11")["id"]
    with db.reader() as conn:
        first = buttons.find(conn, mid, 1, 1)
        assert first == {"t": "Получить ключ", "k": "callback", "d": D}
        assert buttons.callback_data(first) == RAW
        assert buttons.callback_data(buttons.find(conn, mid, 1, 2)) is None  # ссылка
        assert buttons.callback_data(buttons.find(conn, mid, 2, 3)) is None  # с паролем
        for row, col in ((0, 1), (1, 0), (1, 3), (3, 1), (2, 4)):
            assert buttons.find(conn, mid, row, col) is None
        assert buttons.find(conn, plain, 1, 1) is None
        assert buttons.find(conn, 999_999, 1, 1) is None


# --- миграция ----------------------------------------------------------------------------------------------


def test_migration_0064_on_previous_version(tmp_path, monkeypatch):
    from hub.core import db as db_module
    from hub.core.db import Db

    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 63}
    )
    old = Db(tmp_path)
    old.start()
    try:
        acc = insert_account(old, source="telegram")
        cid = insert_channel(old, acc, ext_id=CHAN["ext_id"], kind="dm", title=CHAN["title"])
        insert_message(old, cid, ext_id="10", ts=1000, text="привет")
        with old.reader() as conn:
            assert "buttons_json" not in {r[1] for r in conn.execute("PRAGMA main.table_info(message)")}
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 64
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(message)")}
                assert {"buttons_json", "buttons_ts"} <= cols
        row = _row(new, "10")
        assert (row["buttons_json"], row["buttons_ts"]) == (None, None)
        # П11: первая сверка после миграции пишет кнопки принятому сообщению — один новый seq
        _ingest(new, acc, [_msg("10", 1000, btn=B1, bts=1000)], 2000)
        assert _row(new, "10")["buttons"] == B1 and _row(new, "10")["seq"] > row["seq"]
    finally:
        new.close()
