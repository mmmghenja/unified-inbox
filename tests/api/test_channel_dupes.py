# ruff: noqa: F811
"""Ф17-3 (docs/architecture.md): автопересылка поста канала в группу обсуждения — служебная
строка, если канал есть в хабе и не в архиве; предпросмотр вступления со связанным чатом (`_clean_peek`)."""

from hub.core import join

from .conftest import insert_channel, login_via_code
from .test_chan_subs import (  # noqa: F401 — фикстуры и помощники образца
    _event,
    _ingest,
    _now,
    _signals,
    _sub,
    procs,
    world,
)

CHAN_EXT = "-1001234567890"


def _auto(channel_ext, ext_id, ts, *, from_ext=CHAN_EXT, auto=True, **kw):
    ev = _event(channel_ext, ext_id, ts, **kw)
    ev["forward"] = {"from_name": "DEMO", "from_ext_id": from_ext, "ts": ts, "channel_post": 7}
    if auto:
        ev["forward"]["auto"] = True
    return ev


def _kind(db, channel_id, ext_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT kind FROM main.message WHERE channel_id = ? AND ext_id = ?", (channel_id, ext_id)
        ).fetchone()[0]


async def test_autoforward_with_channel_in_hub_is_event_not_unread_no_signal(client, db, world, procs, clock):
    insert_channel(db, world["acc"], project_id=world["pid"], ext_id=CHAN_EXT, title="DEMO")
    await _sub(client, world, world["cid"])
    now = _now(clock)
    await login_via_code(client, db, clock)
    _ingest(db, world, [_auto("c1", "1", now)], clock)
    assert _kind(db, world["cid"], "1") == "event"
    assert _signals(db) == []
    rows = (await client.get(f"/api/v1/projects/{world['pid']}/channels")).json()
    assert next(c for c in rows if c["id"] == world["cid"])["unread"] == 0
    # повтор того же события — тот же kind
    _ingest(db, world, [_auto("c1", "1", now)], clock)
    assert _kind(db, world["cid"], "1") == "event"
    # обычное сообщение рядом — непрочитанное и сигнал
    _ingest(db, world, [_event("c1", "2", now + 1)], clock)
    assert len(_signals(db)) == 1


async def test_autoforward_without_channel_or_archived_is_plain_message(db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_auto("c1", "1", now)], clock)  # канала в хабе нет
    assert _kind(db, world["cid"], "1") == "text"
    arch = insert_channel(db, world["acc"], project_id=world["pid"], ext_id=CHAN_EXT, archived=1)
    assert arch
    _ingest(db, world, [_auto("c1", "2", now + 1)], clock)  # канал в архиве
    assert _kind(db, world["cid"], "2") == "text"


async def test_manual_forward_stays_plain_even_with_channel(db, world, procs, clock):
    insert_channel(db, world["acc"], project_id=world["pid"], ext_id=CHAN_EXT, title="DEMO")
    _ingest(db, world, [_auto("c1", "1", _now(clock), auto=False)], clock)
    assert _kind(db, world["cid"], "1") == "text"


def test_clean_peek_linked_clip_and_bad_username():
    base = {"title": "K", "kind": "broadcast"}
    got = join._clean_peek(
        {**base, "linked": {"title": "я" * 400, "username": "bad name!", "kind": "discussion"}}
    )
    assert len(got["linked"]["title"]) == join.TITLE_MAX
    assert got["linked"]["username"] is None and got["linked"]["kind"] == "discussion"
    ok = join._clean_peek({**base, "linked": {"title": "Чат", "username": "demo_chat", "kind": "channel"}})
    assert ok["linked"] == {"title": "Чат", "username": "demo_chat", "kind": "channel"}
    for junk in (
        None,
        "x",
        {"title": "", "kind": "discussion"},
        {"title": "a", "kind": "zzz"},
        {"kind": "channel"},
    ):
        assert "linked" not in join._clean_peek({**base, "linked": junk})
    assert "linked" not in join._clean_peek(base)
