"""Ф28-1 (`docs/architecture.md`), сценарий бага Globex projects-18: снимок Slack «тред
не подписан» -> тег меня в ответе -> следующий чужой ответ без тега даёт подписке «лично мне» сигнал `thread`.

Окружение — как `test_f20_bots_personal.py` (его фикстуры и помощники)."""

from hub.core import chan_subs

from .test_f20_bots_personal import (  # noqa: F401 — фикстуры `world`, `procs`
    _ev,
    _ingest,
    _msg_id,
    _now,
    _signals,
    _subscribe,
    _wait,
    procs,
    world,
)


async def _scenario(client, db, world, clock, *, evidence):  # noqa: F811
    body = await _subscribe(client, world, mode="messages", msg_filter="personal")
    sub_id = body["sub"]["id"]
    now = _now(clock)
    sl, c3 = world["acc"]["slack"], world["ch"]["C3"]
    _ingest(
        db,
        sl,
        [
            _ev("C3", "1790917549.148089", now),
            _ev("C3", "1790917600.000100", now + 1, thread="1790917549.148089"),
        ],
        now,
    )
    snapshot = _ev("C3", "1790917549.148089", now)
    snapshot["thread"] = {"reply_count": 1, "latest_reply_ext_id": "1790917600.000100", "subscribed": False}
    _ingest(db, sl, [snapshot], now + 2)
    _ingest(db, sl, [evidence(now + 3)], now + 3)
    r = await _wait(client, world, sub_id)  # сигнал по тегу (или пусто после моего ответа) — забрали
    assert r.status_code == 200, r.text
    clock.advance_ms(60_000)
    later = _now(clock)
    _ingest(
        db,
        sl,
        [_ev("C3", "1790921220.000400", later, thread="1790917549.148089", text="Макс, ну блин")],
        later,
    )
    with db.reader() as conn:
        cat = chan_subs.message_cat(conn, _msg_id(db, c3, "1790921220.000400"))
    assert cat.cat == "thread"
    assert _signals(db)[c3][-1][0] == "pending"
    assert _signals(db)[c3][-1][3:5] == ("thread", "1790917549.148089")
    r = await _wait(client, world, sub_id)
    assert r.status_code == 200, r.text
    got = {s["channel_id"]: (s["cat"], s["thread_ext"]) for s in r.json()["signals"]}
    assert got[c3] == ("thread", "1790917549.148089")


async def test_tag_then_untagged_reply_signals_personal(client, db, world, procs, clock):  # noqa: F811
    await _scenario(
        client,
        db,
        world,
        clock,
        evidence=lambda ts: _ev("C3", "1790920680.000200", ts, thread="1790917549.148089", mention=True),
    )


async def test_my_reply_then_untagged_reply_signals_personal(client, db, world, procs, clock):  # noqa: F811
    await _scenario(
        client,
        db,
        world,
        clock,
        evidence=lambda ts: _ev("C3", "1790921040.000300", ts, thread="1790917549.148089", is_me=True),
    )
