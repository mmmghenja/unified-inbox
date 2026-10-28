"""21.10 (живой случай: личка коллеги ждала дежурную Globex 5 минут посреди хода): ЛИЧНОЕ для сессии хаба —
срочное в разделе `sessions.signals` ожидания хоста (`urgent`). Личное — карточка разборщика по личке, прямому
упоминанию владельца или ответу в его треде (`subs.personal_card`), сигналы сообщений категорий `dm`/`mention`/
`thread` (`chan_subs.PERSONAL_CATS`) — у подписки «лично мне» с ключами и у подписки на беседу. Метка карточки
`urgent|reply` не меняется.

Хост поддельный; помощники — из test_f41_incoming.py и test_hub_sessions.py."""

from hub.core import subs

from . import test_hub_sessions as base
from .conftest import insert_channel, insert_message
from .test_f41_incoming import API, _card, _idle, _triage, _wait_host
from .test_f41_incoming import _signals as _take
from .test_hub_sessions import MIN, ROLE, _ev, _h, _hs, _hsub, _ingest, _mk, _now, _sub, _wait

procs = base.procs
ui = base.ui
world = base.world


def _signals(data) -> list[dict]:
    return [{k: s[k] for k in ("ready", "urgent")} for s in data["sessions"]["signals"]]


def _set(db, message_id, column, value) -> None:
    db.write_sync(
        lambda conn: conn.execute(f"UPDATE main.message SET {column} = ? WHERE id = ?", (value, message_id))
    )


async def test_personal_card_is_urgent_for_host_label_unchanged(client, ui, db, clock, world):
    w = world
    triage = _triage(db)
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="cards")
    now = _now(clock)

    # Обычная беседа «реагирую» без упоминания — не срочно (как раньше).
    group = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="grp", kind="group")
    await _card(client, triage, group, [insert_message(db, group, ext_id="1", ts=now - MIN)])
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 1, "urgent": 0}]

    # Личка человека — срочно; метка карточки остаётся `reply`.
    dm = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="dm", kind="dm", title="Дмитрий")
    card = await _card(client, triage, dm, [insert_message(db, dm, ext_id="1", ts=now - MIN)])
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 2, "urgent": 1}]
    with db.reader() as conn:
        item = conn.execute("SELECT label FROM main.agent_item WHERE id = ?", (card,)).fetchone()
        signal = conn.execute("SELECT label FROM main.session_signal WHERE item_id = ?", (card,)).fetchone()
    assert item[0] == signal[0] == "reply"

    # Прямое упоминание владельца в группе — срочно; `@channel` — нет.
    ment = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="ment", kind="group")
    await _card(client, triage, ment, [insert_message(db, ment, ext_id="1", ts=now - MIN, mentions_me=1)])
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 3, "urgent": 2}]
    bcast = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="bcast", kind="group")
    mid = insert_message(db, bcast, ext_id="1", ts=now - MIN, mentions_me=1)
    _set(db, mid, "mentions_broadcast", 1)
    await _card(client, triage, bcast, [mid])
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 4, "urgent": 2}]

    # Ответ на сообщение владельца — срочно.
    reply = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="reply", kind="group")
    insert_message(db, reply, ext_id="1", ts=now - 2 * MIN, is_me=1)
    rid = insert_message(db, reply, ext_id="2", ts=now - MIN)
    _set(db, rid, "reply_to_ext_id", "1")
    await _card(client, triage, reply, [rid])
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 5, "urgent": 3}]

    # Личка с ботом — не срочно.
    bot_dm = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="botdm", kind="dm")
    bid = insert_message(db, bot_dm, ext_id="1", ts=now - MIN)
    _set(db, bid, "is_bot", 1)
    await _card(client, triage, bot_dm, [bid])
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 6, "urgent": 3}]

    # Слияние карточки: упоминание в покрытых сообщениях остаётся личным, хотя последнее — обычное.
    later = insert_message(db, ment, ext_id="2", ts=now - MIN // 2)
    with db.reader() as conn:
        ids = conn.execute("SELECT id FROM main.message WHERE channel_id = ? ORDER BY id", (ment,)).fetchall()
    await _card(client, triage, ment, [i for (i,) in ids])
    assert later in [i for (i,) in ids]
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 6, "urgent": 3}]


async def test_personal_message_signals_are_urgent_for_host(client, ui, db, clock, world):
    """Подписка «лично мне» с ключами: «по ключу» — не срочно, личка — срочно; подписка на беседу — так же."""
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="messages", msg_filter="personal", keywords=["договор"], batch_min=0)
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock), text="пришлите договор")], clock)
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 1, "urgent": 0}]
    insert_channel(db, w["acc"], project_id=w["p3"], ext_id="dm7", kind="dm", title="Дмитрий")
    _ingest(db, w, [_ev("dm7", "1", _now(clock), kind="dm", text="владелец, глянь")], clock)
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 2, "urgent": 1}]

    # Подписка на беседу второй сессии: обычное — не срочно, упоминание — срочно.
    other = (await _mk(ui, w, role="секретарь"))["id"]
    r = await client.post(
        f"{API}/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(other, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("other", "301", _now(clock), text="привет всем")], clock)
    data = await _wait_host(client, w, known={})
    assert {s["id"]: (s["ready"], s["urgent"]) for s in data["sessions"]["signals"]}[other] == (1, 0)
    mention = {**_ev("other", "302", _now(clock), text="@владелец посмотри"), "mentions_me": True}
    _ingest(db, w, [mention], clock)
    data = await _wait_host(client, w, known={})
    assert {s["id"]: (s["ready"], s["urgent"]) for s in data["sessions"]["signals"]}[other] == (1, 1)


async def test_personal_card_does_not_wait_card_batch_and_takes_accumulated(client, ui, db, clock, world):
    """Ф67-1 + 21.10: у подписки «карточки» с пачкой обычная карточка ждёт срока, личная — сразу и забирает
    накопленное: и у сессии хаба (хост), и у внешней сессии (`/subs/{id}/wait`)."""
    w = world
    triage = _triage(db)
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="cards", batch_min=5)
    ext = (await _sub(client, w, base.B, mode="cards", batch_min=5))["sub"]["id"]
    now = _now(clock)
    group = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="grp", kind="group")
    await _card(client, triage, group, [insert_message(db, group, ext_id="1", ts=now - MIN)])
    (item,) = (await _wait_host(client, w, known={}))["sessions"]["signals"]
    assert (item["ready"], item["urgent"]) == (0, 0) and item["next_ms"] > 0
    with db.reader() as conn:
        assert subs.wait_state(conn, ext, now_ms=now)[0] == 0
    dm = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="dm", kind="dm", title="Дмитрий")
    await _card(client, triage, dm, [insert_message(db, dm, ext_id="1", ts=now - MIN)])
    # Готова сразу только личная; обычная ждёт срока, но выдача заберёт её той же подачей.
    assert _signals(await _wait_host(client, w, known={})) == [{"ready": 1, "urgent": 1}]
    with db.reader() as conn:
        assert subs.wait_state(conn, ext, now_ms=now)[0] == 1
    await _idle(client, w, hub_id)
    assert sorted(s["channel_id"] for s in (await _take(client, w, hub_id))["signals"]) == [group, dm]
    got = await _wait(client, w, ext, base._sess(base.B))
    assert sorted(s["channel_id"] for s in got) == [group, dm]
