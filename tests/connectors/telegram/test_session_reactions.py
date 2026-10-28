"""Ф15-1 П1 (docs/architecture.md): Telegram-коннектор для строки сессии
(`origin=session`) — по свежим реакциям источника: среди моих есть эмодзи не из `session_emojis` или моя
кастомная/платная -> операции не применяются, `done` с причиной `owner_reacted`."""

from __future__ import annotations

import pytest
from telethon.tl import types

from hub.connectors.reactions import ReactionResult, Reactor

from . import helpers as h
from .test_reactions import USER, job, marked, reactions_update, read_result, setup, updates


def session_job(ops, session_emojis, **kw) -> dict:
    item = job(ops, **kw)
    item["origin"] = "session"
    item["session_emojis"] = session_emojis
    return item


async def test_session_on_when_owner_has_other_emoji_is_not_applied():
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("❤", 1, True)]))
    res = await conn.push_reaction(session_job([["👀", True]], ["👀"]))
    assert res == ("done", None, "owner_reacted")
    assert [c[0] for c in gw.reaction_calls] == ["get"]  # SendReaction не звали
    assert events[-1]["reactions"] == [{"emoji": "❤", "count": 1, "me": True}]


async def test_session_on_replaces_own_marked_emoji():
    """Моя 👍 — пометка сессии: `on 👀` вытесняет её (Telegram держит одну)."""
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("👍", 1, True)]))
    gw.send_reaction_result = updates(reactions_update(USER, 15, [("👀", 1, True)]))
    res = await conn.push_reaction(session_job([["👀", True]], ["👀", "👍"]))
    assert res == ("done", None, None)
    assert [c for c in gw.reaction_calls if c[0] == "send"] == [("send", marked(USER), 15, ["👀"])]


@pytest.mark.parametrize("reaction", [types.ReactionCustomEmoji(document_id=777), types.ReactionPaid()])
async def test_session_custom_or_paid_mine_is_owner(reaction):
    gw, conn, _events = setup()
    gw.get_reactions_results.append(
        updates(
            types.UpdateMessageReactions(
                peer=h.peer_of(USER),
                msg_id=15,
                reactions=types.MessageReactions(
                    results=[types.ReactionCount(reaction=reaction, count=1, chosen_order=0)]
                ),
            )
        )
    )
    res = await conn.push_reaction(session_job([["👀", True]], ["👀"]))
    assert res == ("done", None, "owner_reacted")
    assert [c[0] for c in gw.reaction_calls] == ["get"]


async def test_session_min_read_falls_back_to_current_mine():
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("❤", 1, True)], is_min=True))
    res = await conn.push_reaction(session_job([["👀", True]], ["👀"], current_mine=["❤️"]))
    assert res == ("done", None, "owner_reacted")


async def test_ui_row_ignores_session_rule():
    """Клик владельца (origin ui) — как раньше: моя ❤ заменяется."""
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("❤", 1, True)]))
    gw.send_reaction_result = updates(reactions_update(USER, 15, [("👀", 1, True)]))
    item = job([["👀", True]])
    item["origin"] = "ui"
    res = await conn.push_reaction(item)
    assert res == ("done", None, None)
    assert [c for c in gw.reaction_calls if c[0] == "send"] == [("send", marked(USER), 15, ["👀"])]


class _Client:
    def __init__(self) -> None:
        self.reports: list[dict] = []

    async def report_reaction(self, **kw) -> dict:
        self.reports.append(kw)
        return {"ok": True}


async def _noop(*_a) -> None:
    return None


@pytest.mark.parametrize(
    "result, reason",
    [
        (ReactionResult("done", reason="owner_reacted"), "owner_reacted"),
        (ReactionResult("done", reason="nothing"), None),
        (ReactionResult("drop", reason="owner_reacted"), None),
    ],
)
async def test_reactor_passes_only_owner_reason_to_core(result, reason):
    """Ядру уходит `reason` только у `done` + `owner_reacted` (ядро по нему не ставит пометок сессии)."""
    client = _Client()

    async def push(_item):
        return result

    reactor = Reactor(
        client,
        push,
        mono=lambda: 0.0,
        sleep=_noop,
        is_blocked=lambda: False,
        on_auth_required=_noop,
        on_unauthorized=lambda: None,
    )
    await reactor.process({"message_id": 5, "lease_gen": 2})
    assert len(client.reports) == 1
    assert client.reports[0].get("reason") == reason
    assert ("reason" in client.reports[0]) is (reason is not None)
