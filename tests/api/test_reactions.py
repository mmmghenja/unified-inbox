"""Ф10-3 (docs/architecture.md): очередь «реакция из хаба -> реакция в источнике» — раздел E,
пункты 1–5, с поправками П1 (операции `{emoji, on}`, `ops_json`), П2 (Telegram без U+FE0F), П6 (форма
задания), П8 (служебное/удалённое — 409), П9 (выключатель и статус аккаунта при выдаче), П11 (аудит).

UI-операция идёт через публичное приложение (`POST /messages/{id}/reactions` под cookie-сессией), выдача и
отчёт — через внутреннее (секрет коннектора) на той же БД и тех же часах."""

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth
from hub.core import db as db_module
from hub.core import reactions as reactions_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db, db_of
from hub.core.feed import Notifier

from .conftest import csrf_headers, insert_account, insert_message, login_via_code, make_bearer_token

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _account(db, *, source="telegram", db_name="main", config=None, status="ok") -> tuple[int, dict]:
    def _fn(conn):
        return conn.execute(
            "INSERT INTO main.account (source, db, config_json, status) VALUES (?, ?, ?, ?)",
            (source, db_name, json.dumps(config) if config is not None else None, status),
        ).lastrowid

    account_id = db.write_sync(_fn)
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


def _set_account(db, account_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE main.account SET {sets} WHERE id = ?",  # noqa: S608 — имена колонок из теста
            (*cols.values(), account_id),
        )
    )


def _up(ch, ext_id, ts, *, kind="dm", parent=None, event_kind="text"):
    channel = {"ext_id": ch, "kind": kind}
    if parent is not None:
        channel["parent_ext_id"] = parent
        channel["parent"] = {"ext_id": parent, "kind": "group"}
    return {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": event_kind,
        "text": "t",
        "mentions_me": False,
    }


async def _events(ic, h, events):
    r = await ic.post(f"{INTERNAL}/events", headers=h, json={"events": events})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


def _channel_id(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (ext_id,),
        ).fetchone()[0]


def _mid(db, channel_id, ext_id):
    schema = db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext_id),
        ).fetchone()[0]


def _row(db, message_id):
    schema = db_of(message_id)
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.reaction_ack WHERE message_id = ?", (message_id,))  # noqa: S608
        row = cur.fetchone()
        if row is None:
            return None
        out = dict(zip([c[0] for c in cur.description], row, strict=True))
        out["ops"] = json.loads(out["ops_json"])
        return out


def _now_ms(clock) -> int:
    return int(clock() * 1000)


async def _ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


async def _react(client, ui, message_id, emoji, on=True, *, status=200):
    r = await client.post(
        f"{API}/messages/{message_id}/reactions", json={"emoji": emoji, "on": on}, headers=ui
    )
    assert r.status_code == status, r.text
    return r.json()


async def _lease(ic, h, limit=None):
    body = {} if limit is None else {"limit": limit}
    r = await ic.post(f"{INTERNAL}/reactions/lease", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, message_id, lease_gen, outcome, retry_after_ms=None, *, status=200):
    body = {"lease_gen": lease_gen, "outcome": outcome}
    if retry_after_ms is not None:
        body["retry_after_ms"] = retry_after_ms
    r = await ic.post(f"{INTERNAL}/reactions/{message_id}/report", headers=h, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, channel_id, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


async def _simple(db, ic, *, source="telegram", config=None, n=2, ch="c1", kind="dm", db_name="main"):
    """Аккаунт и канал с входящими `1..n` (ts = 1000 * i)."""
    acc, h = _account(db, source=source, config=config, db_name=db_name)
    await _events(ic, h, [_up(ch, str(i), 1000 * i, kind=kind) for i in range(1, n + 1)])
    cid = _channel_id(db, ch, db_name)
    return acc, h, cid


# --- E1 + П1: одна строка на сообщение, операции сливаются ---


async def test_e1_op_queues_row_and_second_op_merges(client, ic, db, clock):
    acc, h, ch = await _simple(db, ic, source="slack")
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    t0 = _now_ms(clock)

    assert await _react(client, ui, m, "👍") == {"status": "queued"}
    row = _row(db, m)
    assert row["ops"] == [["👍", True]] and row["gen"] == 1 and row["account_id"] == acc
    assert row["channel_id"] == ch and row["not_before_ms"] == t0 + 1500 and row["attempts"] == 0

    # неудачная попытка — attempts=1; новая операция обнуляет попытки (новая цель — новые попытки)
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    await _report(ic, h, m, item["lease_gen"], "retry")
    assert _row(db, m)["attempts"] == 1
    pause_until = _row(db, m)["pause_until_ms"]

    clock.advance_ms(700)
    await _react(client, ui, m, "🔥")
    row = _row(db, m)
    assert row["ops"] == [["👍", True], ["🔥", True]]
    assert row["gen"] == 2 and row["attempts"] == 0
    assert row["not_before_ms"] == pause_until  # пауза источника новой операцией не обходится

    # та же эмодзи — прежняя операция заменяется и встаёт в конец
    await _react(client, ui, m, "👍", on=False)
    assert _row(db, m)["ops"] == [["🔥", True], ["👍", False]]
    assert _row(db, m)["gen"] == 3

    audit = _audit(db, "reaction_set")
    # Ф15-1 A3: в аудите ещё `origin` и `not_before_ts`; Ф19-1 D: и `emoji` (было — без символа).
    assert [(r[0], r[1], {k: r[2][k] for k in ("message_id", "on", "origin", "emoji")}) for r in audit] == [
        ("queued", ch, {"message_id": m, "on": True, "origin": "ui", "emoji": "👍"}),
        ("queued", ch, {"message_id": m, "on": True, "origin": "ui", "emoji": "🔥"}),
        ("queued", ch, {"message_id": m, "on": False, "origin": "ui", "emoji": "👍"}),
    ]


async def test_e1_telegram_ops_accumulate_in_order(client, ic, db, clock):
    """Приёмка: у Telegram одна реакция, итог зависит от порядка — операции не сливаются по эмодзи, а копятся
    (коннектор проигрывает их от текущего состояния); хвост — не длиннее `TELEGRAM_OPS_MAX`."""
    _acc, _h, ch = await _simple(db, ic, source="telegram")
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    await _react(client, ui, m, "🔥")
    await _react(client, ui, m, "🔥", on=False)
    assert _row(db, m)["ops"] == [["👍", True], ["🔥", True], ["🔥", False]]
    for _ in range(reactions_mod.TELEGRAM_OPS_MAX):
        await _react(client, ui, m, "👀")
    ops = _row(db, m)["ops"]
    assert ops == [["👀", True]] * reactions_mod.TELEGRAM_OPS_MAX


async def test_e1_delay_shifts_from_last_click(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1000)
    await _react(client, ui, m, "🔥")
    clock.advance_ms(1000)  # 2 с от первого клика, 1 с от последнего
    assert await _lease(ic, h) == []
    clock.advance_ms(500)
    assert len(await _lease(ic, h)) == 1


# --- E2: нормализация, ошибки, выключатель ---


async def test_e2_telegram_strips_vs16_slack_keeps(client, ic, db, clock):
    _acc, _h, tg = await _simple(db, ic, source="telegram", ch="t1")
    _acc2, _h2, sl = await _simple(db, ic, source="slack", ch="s1", kind="group", db_name="work")
    ui = await _ui(client, db, clock)
    mt, ms = _mid(db, tg, "1"), _mid(db, sl, "1")
    await _react(client, ui, mt, "❤️")
    assert _row(db, mt)["ops"] == [["❤", True]]
    await _react(client, ui, ms, "❤️")
    await _react(client, ui, ms, ":partyparrot:")
    await _react(client, ui, ms, "❤", on=False)  # тот же эмодзи без U+FE0F — замена, не вторая операция
    assert _row(db, ms)["ops"] == [[":partyparrot:", True], ["❤", False]]


async def test_e2_unsupported_source_409(client, ic, db, clock):
    # Ф15-1: почта теперь с реакциями (Gmail) — источник без реакций здесь другой.
    _acc, _h, ch = await _simple(db, ic, source="pyrus", kind="group")
    ui = await _ui(client, db, clock)
    m = _mid(db, ch, "1")
    r = await _react(client, ui, m, "👍", status=409)
    assert r["detail"] == "unsupported"
    assert _row(db, m) is None


async def test_e2_no_ext_id_event_deleted_409(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    draft = insert_message(db, ch, ext_id=None, ts=5000, is_me=1)
    event = insert_message(db, ch, ext_id="ev1", ts=6000, kind="event")
    deleted = insert_message(db, ch, ext_id="d1", ts=7000, deleted=1)
    ui = await _ui(client, db, clock)
    for mid in (draft, event, deleted):
        await _react(client, ui, mid, "👍", status=409)
        assert _row(db, mid) is None


async def test_e2_hidden_404_and_unknown_404(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    r = await client.delete(f"{API}/messages/{m}", headers=ui)
    assert r.status_code == 204
    await _react(client, ui, m, "👍", status=404)
    await _react(client, ui, 999_999, "👍", status=404)
    assert _row(db, m) is None


async def test_e2_bearer_without_session_403(client, ic, db, clock):
    """Ф15-1 П7: bearer без сессии, подписанной на проект, — 403 с аудитом (`ui_only` снят)."""
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    token = make_bearer_token(db, scopes=("read", "send", "draft", "organize"))
    r = await client.post(
        f"{API}/messages/{m}/reactions",
        json={"emoji": "👍", "on": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert _row(db, m) is None
    denied = _audit(db, "reaction_denied")
    assert len(denied) == 1 and denied[0][2]["message_id"] == m


@pytest.mark.parametrize(
    "source, value",
    [
        ("slack", "<script>"),
        ("slack", ":A B:"),
        ("slack", "👍" * 65),
        ("slack", "x" * 65),
        ("slack", ""),
        ("slack", "a"),
        ("slack", ":" + "a" * 61 + ":"),
        ("telegram", ":fire:"),  # `:name:` — только Slack
        ("telegram", "👍👍"),
    ],
)
async def test_e2_bad_element_422(client, ic, db, clock, source, value):
    _acc, _h, ch = await _simple(db, ic, source=source)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, value, status=422)
    assert _row(db, m) is None


async def test_f18_telegram_only_its_set_on_off_any(client, ic, db, clock):
    """Ф18-1: в Telegram ставится только его набор — ✅ и 🔍 сразу 422 `not in telegram set`; 🤝 и ❤️‍🔥 (с
    U+FE0F) — да; снять можно любую; Slack ✅ принимает."""
    _acc, _h, tg = await _simple(db, ic, source="telegram", ch="t1", n=3)
    _acc2, _h2, sl = await _simple(db, ic, source="slack", ch="s1", kind="group", db_name="work")
    ui = await _ui(client, db, clock)
    m1, m2, m3 = (_mid(db, tg, str(i)) for i in (1, 2, 3))
    for value in ("✅", "🔍"):
        r = await _react(client, ui, m1, value, status=422)
        assert r["detail"] == "not in telegram set"
    assert _row(db, m1) is None
    await _react(client, ui, m1, "🤝")
    await _react(client, ui, m2, "❤️‍🔥")
    assert _row(db, m2)["ops"] == [["❤\u200d🔥", True]]
    await _react(client, ui, m3, "✅", on=False)
    assert _row(db, m3)["ops"] == [["✅", False]]
    await _react(client, ui, _mid(db, sl, "1"), "✅")


async def test_e2_body_types_strict(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    for body in ({"emoji": "👍", "on": "yes"}, {"emoji": 1, "on": True}, {"emoji": "👍"}, {"emojis": ["👍"]}):
        r = await client.post(f"{API}/messages/{m}/reactions", json=body, headers=ui)
        assert r.status_code == 422, body
    assert _row(db, m) is None


async def test_e2_reactions_false_off_no_row(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic, config={"reactions": False, "read_ack": True})
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    assert await _react(client, ui, m, "👍") == {"status": "off"}
    assert _row(db, m) is None
    # Ф19-1 D: эмодзи — и в аудите выключателя.
    assert _audit(db, "reaction_off") == [
        ("off", ch, {"message_id": m, "on": True, "origin": "ui", "emoji": "👍"})
    ]
    assert _audit(db, "reaction_set") == []
    clock.advance_ms(60_000)
    assert await _lease(ic, h) == []


# --- E3 + П6/П9: выдача ---


async def test_e3_lease_shape_telegram_with_current_mine(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "2")
    await _events(
        ic,
        h,
        [
            {
                "type": "reaction",
                "channel": {"ext_id": "c1", "kind": "dm"},
                "ext_id": "2",
                "reactions": [
                    {"emoji": "❤️", "count": 2, "me": True},
                    {"emoji": "👍", "count": 1, "me": False},
                    {"custom": "555", "count": 1, "me": True},
                ],
            }
        ],
    )
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "🔥")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    assert item == {
        "message_id": m,
        "channel_id": ch,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": "c1", "kind": "dm", "parent_ext_id": None},
        "message_ext_id": "2",
        "ops": [["🔥", True]],
        "origin": "ui",  # Ф15-1 П1
        "current_mine": ["❤"],
    }
    # реакции в `reactions_json` ядро само не трогало (решение 3)
    with db.reader() as conn:
        raw = conn.execute("SELECT reactions_json FROM main.message WHERE id = ?", (m,)).fetchone()[0]
    assert "🔥" not in raw


async def test_e3_lease_shape_slack_topic_parent_and_no_current(client, ic, db, clock):
    _acc, h = _account(db, source="slack", db_name="work")
    await _events(ic, h, [_up("C1", "1790000000.000100", 1000, kind="group")])
    ch = _channel_id(db, "C1", "work")
    m = _mid(db, ch, "1790000000.000100")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, ":custom_one:")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    assert item["message_ext_id"] == "1790000000.000100"
    assert item["channel"] == {"ext_id": "C1", "kind": "group", "parent_ext_id": None}
    assert item["ops"] == [[":custom_one:", True]]
    assert "current_mine" not in item
    assert db_of(m) == "work"


async def test_e3_topic_channel_carries_parent(client, ic, db, clock):
    _acc, h = _account(db)
    await _events(ic, h, [_up("-100500:7", "11", 1000, kind="topic", parent="-100500")])
    ch = _channel_id(db, "-100500:7")
    m = _mid(db, ch, "11")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    assert item["channel"] == {"ext_id": "-100500:7", "kind": "topic", "parent_ext_id": "-100500"}


async def test_e3_not_before_pause_and_live_lease(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    assert await _lease(ic, h) == []  # 1.5 с ещё не прошли
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    assert await _lease(ic, h) == []  # живой лиз
    clock.advance_ms(59_999)
    assert await _lease(ic, h) == []
    clock.advance_ms(1)
    (again,) = await _lease(ic, h)  # лиз истёк — выдаётся снова, новый lease_gen
    assert again["lease_gen"] == item["lease_gen"] + 1

    await _report(ic, h, m, again["lease_gen"], "retry", retry_after_ms=90_000)
    clock.advance_ms(89_999)
    assert await _lease(ic, h) == []  # пауза
    clock.advance_ms(1)
    assert len(await _lease(ic, h)) == 1


async def test_e3_hidden_after_queue_row_deleted_not_leased(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    r = await client.delete(f"{API}/messages/{m}", headers=ui)
    assert r.status_code == 204
    clock.advance_ms(1500)
    assert await _lease(ic, h) == []
    assert _row(db, m) is None


async def test_e3_deleted_by_sender_after_queue_row_deleted(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    await _events(ic, h, [{"type": "delete", "ext_ids": ["1"], "channel": {"ext_id": "c1", "kind": "dm"}}])
    clock.advance_ms(1500)
    assert await _lease(ic, h) == []
    assert _row(db, m) is None


async def test_p9_switch_off_later_deletes_rows_and_blocked_status_waits(client, ic, db, clock):
    acc, h, ch = await _simple(db, ic)
    m1, m2 = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m1, "👍")
    clock.advance_ms(1500)

    _set_account(db, acc, status="auth_required")
    assert await _lease(ic, h) == []
    assert _row(db, m1) is not None  # строка ждёт входа
    _set_account(db, acc, status="ok")

    _set_account(db, acc, config_json=json.dumps({"reactions": False}))
    assert await _lease(ic, h) == []
    assert _row(db, m1) is None
    _set_account(db, acc, config_json=None)
    await _react(client, ui, m2, "👍")
    clock.advance_ms(1500)
    assert [i["message_id"] for i in await _lease(ic, h)] == [m2]


async def test_e3_foreign_account_never_leases(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic)
    _acc2, h2 = _account(db)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    assert await _lease(ic, h2) == []
    assert _row(db, m) is not None


# --- E4: отчёт ---


async def test_e4_done_same_gen_deletes(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    assert (await _report(ic, h, m, item["lease_gen"], "done"))["result"] == "deleted"
    assert _row(db, m) is None


async def test_e4_done_after_new_op_releases_with_new_ops(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    await _react(client, ui, m, "🔥")  # пока задание в работе
    assert (await _report(ic, h, m, item["lease_gen"], "done"))["result"] == "released"
    row = _row(db, m)
    assert row["lease_until_ms"] is None and row["ops"] == [["👍", True], ["🔥", True]] and row["gen"] == 2
    clock.advance_ms(1500)
    (again,) = await _lease(ic, h)
    assert again["ops"] == [["👍", True], ["🔥", True]] and again["gen"] == 2


async def test_e4_retry_backoff_grows_retry_after_respected_gave_up_after_5(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    pauses = []
    for attempt in range(1, 5):
        (item,) = await _lease(ic, h)
        now = _now_ms(clock)
        retry_after = 200_000 if attempt == 2 else None
        assert (await _report(ic, h, m, item["lease_gen"], "retry", retry_after))["result"] == "paused"
        row = _row(db, m)
        assert row["attempts"] == attempt and row["lease_until_ms"] is None
        pauses.append(row["pause_until_ms"] - now)
        clock.advance_ms(row["pause_until_ms"] - now)
    assert pauses == [30_000, 200_000, 120_000, 240_000]
    (item,) = await _lease(ic, h)
    assert (await _report(ic, h, m, item["lease_gen"], "retry"))["result"] == "gave_up"
    assert _row(db, m) is None


async def test_e4_drop_deletes(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    (item,) = await _lease(ic, h)
    assert (await _report(ic, h, m, item["lease_gen"], "drop"))["result"] == "deleted"
    assert _row(db, m) is None


async def test_e4_stale_lease_409_foreign_account_404(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    _acc2, h2 = _account(db)
    m = _mid(db, ch, "1")
    ui = await _ui(client, db, clock)
    await _react(client, ui, m, "👍")
    clock.advance_ms(1500)
    (first,) = await _lease(ic, h)
    clock.advance_ms(60_000)
    (second,) = await _lease(ic, h)
    await _report(ic, h, m, first["lease_gen"], "done", status=409)
    await _report(ic, h2, m, second["lease_gen"], "done", status=404)
    await _report(ic, h, 999_999, 1, "done", status=404)
    assert _row(db, m) is not None
    assert (await _report(ic, h, m, second["lease_gen"], "done"))["result"] == "deleted"


# --- long-poll ---


async def test_wait_ready_after_delay_and_empty_times_out(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    started = time.monotonic()
    r = await ic.get(f"{INTERNAL}/reactions/wait", params={"timeout_s": 0.3}, headers=h)
    assert r.json() == {"ready": False}
    assert time.monotonic() - started >= 0.25

    ui = await _ui(client, db, clock)
    await _react(client, ui, _mid(db, ch, "1"), "👍")
    clock.advance_ms(1500)
    r = await ic.get(f"{INTERNAL}/reactions/wait", params={"timeout_s": 5}, headers=h)
    assert r.json() == {"ready": True}


def test_wait_state_switch_off_holds_timeout(db):
    acc = insert_account(db, source="telegram")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = ? WHERE id = ?", (json.dumps({"reactions": False}), acc)
        )
    )
    with db.reader() as conn:
        assert reactions_mod.wait_state(conn, account_id=acc, now_ms=0) == (False, None)


# --- E5: миграция 0017 на копии БД предыдущей версии, обе схемы ---


def test_e5_migration_0017_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_16(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 16}

    monkeypatch.setattr(db_module, "_migrations", _up_to_16)
    old = Db(tmp_path)
    old.start()
    try:
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                ).lastrowid
                conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "  # noqa: S608
                    "ts, is_me, kind, text, reactions_json) "
                    "VALUES (?, ?, ?, ?, '1', 1000, 0, 'text', 'привет', '[]')",
                    (1 if schema == "main" else 2, 1 if schema == "main" else 2, acc, cid),
                )
                conn.execute(
                    f"INSERT INTO {schema}.read_ack (channel_id, account_id, message_id, gen, "  # noqa: S608
                    "not_before_ms) VALUES (?, ?, 1, 1, 0)",
                    (cid, acc),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 16
                names = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master")}  # noqa: S608
                assert "reaction_ack" not in names
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 17
                cols = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(reaction_ack)")}
                assert set(cols) >= {  # Ф15-1 (0029) добавила `origin`, `token_id`
                    "message_id",
                    "channel_id",
                    "account_id",
                    "ops_json",
                    "gen",
                    "not_before_ms",
                    "pause_until_ms",
                    "attempts",
                    "lease_until_ms",
                    "lease_gen",
                    "leased_gen",
                }
                assert cols["message_id"][5] == 1  # первичный ключ
                assert cols["ops_json"][3] == 1  # NOT NULL
                assert cols["pause_until_ms"][4] == "0"
                assert cols["attempts"][4] == "0"
                idx = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(reaction_ack)")}
                assert "reaction_ack_account" in idx
                rows = conn.execute(f"SELECT text, reactions_json FROM {schema}.message").fetchall()  # noqa: S608
                assert rows == [("привет", "[]")]
                assert conn.execute(f"SELECT COUNT(*) FROM {schema}.read_ack").fetchone()[0] == 1  # noqa: S608
    finally:
        new.close()
