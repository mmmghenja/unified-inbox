"""Ф28-2 (`docs/architecture.md`), сценарий дейлика Globex projects-18: тег моей группы
Slack в ответе чужого треда — сигнал подписке «лично мне» (`mention`); следующий ответ дейлика без тега —
`skipped` `not_personal` (тред тегом группы не подписан).

Окружение — как `test_f20_bots_personal.py` (его фикстуры и помощники)."""

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

P = "1790917549.148089"
R_TAG = "1790920680.000200"
R_NEXT = "1790921220.000400"


async def test_group_tag_signals_mention_next_reply_not_personal(client, db, world, procs, clock):  # noqa: F811
    body = await _subscribe(client, world, mode="messages", msg_filter="personal")
    sub_id = body["sub"]["id"]
    now = _now(clock)
    sl, c3 = world["acc"]["slack"], world["ch"]["C3"]
    _ingest(db, sl, [_ev("C3", P, now)], now)
    tag = _ev("C3", R_TAG, now + 1, thread=P, mention=True, text="@developers дейлик")
    tag["mentions_group_only"] = True
    _ingest(db, sl, [tag], now + 1)
    assert _signals(db)[c3][-1][:1] + _signals(db)[c3][-1][3:5] == ("pending", "mention", P)
    r = await _wait(client, world, sub_id)
    assert r.status_code == 200, r.text
    assert {s["channel_id"]: s["cat"] for s in r.json()["signals"]}[c3] == "mention"
    clock.advance_ms(60_000)
    later = _now(clock)
    _ingest(db, sl, [_ev("C3", R_NEXT, later, thread=P, text="мой дейлик")], later)
    rows = _signals(db)[c3]
    # Строка `skipped` одна на причину (П6 Ф20-1): родитель без тега, затем ответ дейлика дописан в неё.
    assert [r for r in rows if r[0] == "skipped"] == [
        ("skipped", "not_personal", 2, None, None, _msg_id(db, c3, R_NEXT))
    ]
    assert [r[0] for r in rows if r[0] != "skipped"] == ["delivered"]  # нового сигнала нет
