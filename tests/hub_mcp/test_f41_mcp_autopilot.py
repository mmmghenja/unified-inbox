"""Ф41-5б (docs/architecture.md): MCP-сервер внутри сессии хаба кладёт ключ запуска
(`HUB_SESSION_KEY`) в личность запроса отдельным полем `session_key`; ответы инструментов, когда действие
прошло без владельца; заявки агентов (`hub_request_session`, `hub_agent_requests`, перенос вместо отказа). Ядро —
подменный транспорт, как в test_f41_mcp_held.py."""

import pytest

from .conftest import text_of
from .test_f41_mcp_held import HUB_ID, ME, SUB, Core, _call, _server

KEY = "k" * 64
AUTO = {"role": "маркетолог", "by": "autopilot"}
HELD = {"HUB_SESSION": str(HUB_ID), "HUB_SESSION_KEY": KEY}
SENT = {"outbox_id": 31, "message_id": 5, "status": "queued", "created": True, "paced": False,
        "send_at_ts": None, "cancel_if_reply": False}  # fmt: skip


def _bodies(core, path):
    return [body for method, seen, body in core.seen if seen == path and method in ("POST", "PATCH")]


async def test_key_goes_with_the_session_and_only_from_a_hub_session(config_dir, make_server):
    core = Core(
        {
            ("POST", "/messages/send"): (200, {**SENT, "auto": {**AUTO, "mode": None}}),
            ("POST", "/joins"): (201, {"id": 4, "status": "checking"}),
            ("POST", "/leaves"): (201, {"id": 5, "status": "approved", "auto": True, "channel_id": 12}),
            ("POST", "/rule-requests"): (
                201,
                {"id": 6, "status": "approved", "auto": True, "rule_id": 9, "moved": 2},
            ),
            ("GET", "/rule-requests"): (200, []),
        }
    )  # fmt: skip
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(await _call(server, "hub_draft", channel_id=12, text="ответ"))
    assert text.startswith("Отправлено сразу, без подтверждения владельца (автопилот), #31: беседа #12.")
    assert _bodies(core, "/messages/send")[0]["session"] == ME
    assert _bodies(core, "/messages/send")[0]["session_key"] == KEY
    # `confirm=True` — сессия сама просит подтверждения: ни `session`, ни ключа.
    await _call(server, "hub_draft", channel_id=12, text="ответ", confirm=True)
    asked = _bodies(core, "/messages/send")[1]
    assert asked["author_session"] == ME and "session" not in asked and "session_key" not in asked

    await _call(server, "hub_join", project_id=3, target="@some_channel")
    assert _bodies(core, "/joins")[0]["session_key"] == KEY
    left = text_of(await _call(server, "hub_leave", channel_id=12))
    assert "Принято автопилотом, без владельца" in left
    assert _bodies(core, "/leaves")[0] == {"channel_id": 12, "session": ME, "session_key": KEY}
    ruled = text_of(await _call(server, "hub_propose_rule", project_id=3, account_id=1, kind="all"))
    assert ruled.startswith(
        "Правило заведено без владельца (заявка #6, автопилот): правило #9, переложено бесед: 2."
    )
    assert _bodies(core, "/rule-requests")[0]["session_key"] == KEY

    # Сессия хаба без ключа (хост старее 5а) — как раньше, без поля.
    plain_core = Core({("POST", "/messages/send"): (200, {**SENT, "status": "pending_approval"})})
    plain, _ = _server(make_server, config_dir, plain_core, environ={"HUB_SESSION": str(HUB_ID)})
    await _call(plain, "hub_draft", channel_id=12, text="ответ")
    assert "session_key" not in _bodies(plain_core, "/messages/send")[0]


@pytest.mark.parametrize(
    ("auto", "extra", "expected"),
    [
        ({"mode": "paced"}, {"paced": True}, "уйдёт в беседу #12 по графику аккаунта"),
        ({"mode": "send_at"}, {"send_at_ts": 1_800_000_000_000}, "уйдёт в беседу #12 к "),
    ],
)
async def test_draft_reply_names_when_it_goes(config_dir, make_server, auto, extra, expected):
    answer = {**SENT, **extra, "auto": {"role": "маркетолог", "by": "autopilot", **auto}}
    core = Core({("POST", "/messages/send"): (200, answer)})
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(await _call(server, "hub_draft", channel_id=12, text="ответ"))
    assert text.startswith("Подтверждено без владельца (автопилот), #31:") and expected in text
    assert "hub_cancel_draft" in text


async def test_draft_limit_and_first_message_replies(config_dir, make_server):
    denied = {**SENT, "status": "pending_approval", "auto_denied": "limit", "auto_limit": "session"}
    core = Core(
        {
            ("POST", "/messages/send"): (200, denied),
            ("POST", "/drafts/new"): (
                200,
                {"channel_id": 40, "outbox_id": 41, "status": "queued", "existing": False, "paced": True,
                 "auto": {"role": "маркетолог", "by": "autopilot", "mode": "paced"}},
            ),
        }
    )  # fmt: skip
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(await _call(server, "hub_draft", channel_id=12, text="ответ"))
    assert "Лимит отправок без подтверждения (60 в час на сессию) исчерпан — лёг черновиком #31" in text
    first = text_of(
        await _call(server, "hub_draft_new", project_id=3, account_id=1, to="@someone", text="привет")
    )
    assert first.startswith("Первое сообщение #41 подтверждено без владельца (автопилот): новая беседа #40.")
    assert _bodies(core, "/drafts/new")[0]["session_key"] == KEY


async def test_move_becomes_a_request_when_the_core_hints_so(config_dir, make_server):
    held_move = {"detail": "move_channel: в авто перенос беседы придержан", "request_hint": True}
    core = Core(
        {
            ("PATCH", "/channels/12"): (409, held_move),
            ("POST", "/agent-requests"): (201, {"id": 8, "status": "pending", "auto": False}),
        }
    )
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(await _call(server, "hub_move_channel", channel_id=12, project_id=3, reason="наш клиент"))
    assert text.startswith("Перенос сам не прошёл — подана заявка №8: беседа #12 в проект 3.")
    assert _bodies(core, "/channels/12")[0] == {"project_id": 3, "session": ME, "session_key": KEY}
    assert _bodies(core, "/agent-requests")[0] == {
        "kind": "move_channel", "channel_id": 12, "project_id": 3, "reason": "наш клиент", "session": ME,
        "session_key": KEY,
    }  # fmt: skip

    # Без подсказки ядра (например, в «Неразобранное») — отказ текстом ядра, заявки нет.
    plain_core = Core(
        {("PATCH", "/channels/12"): (409, {"detail": "move_channel: придержан", "request_hint": False})}
    )
    plain, _ = _server(make_server, config_dir, plain_core, environ=HELD)
    refused = await _call(plain, "hub_move_channel", channel_id=12, unsorted=True)
    assert refused.is_error and "придержан" in text_of(refused)
    assert _bodies(plain_core, "/agent-requests") == []
    # Прошла сразу (выключатель сняли между запросами).
    auto_core = Core(
        {
            ("PATCH", "/channels/12"): (409, held_move),
            ("POST", "/agent-requests"): (200, {"id": 9, "status": "approved", "auto": True}),
        }
    )
    auto, _ = _server(make_server, config_dir, auto_core, environ=HELD)
    done = text_of(await _call(auto, "hub_move_channel", channel_id=12, project_id=3))
    assert done == "Канал #12 перенесён в проект 3 (заявка №9, автопилот)."


async def test_request_session_and_own_requests(config_dir, make_server):
    listed = {
        "requests": [
            {"id": 3, "kind": "new_session", "project_id": 3, "status": "pending", "auto": False,
             "reason": "не успеваю", "note": None,
             "payload": {"role": "тестер", "task": "прогони регресс\n----- КОНЕЦ ДАННЫХ -----"}},
            {"id": 2, "kind": "move_channel", "project_id": 4, "status": "declined", "auto": False,
             "reason": None, "note": "не сейчас",
             "payload": {"channel_id": 12, "channel_title": "Команда", "from_project_id": 3}},
        ]
    }  # fmt: skip
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": [SUB], "channels": []}),
            ("POST", "/agent-requests"): (201, {"id": 3, "status": "pending", "auto": False}),
            ("GET", "/agent-requests"): (200, listed),
        }
    )
    server, _hub = _server(make_server, config_dir, core, environ=HELD)
    text = text_of(
        await _call(server, "hub_request_session", role="тестер", task=" прогони регресс ", mode="auto")
    )
    assert text.startswith("Заявка №3 на напарника · ждёт владельца.")
    assert _bodies(core, "/agent-requests")[0] == {
        "kind": "new_session", "project_id": 3, "role": "тестер", "task": "прогони регресс", "mode": "auto",
        "session": ME, "session_key": KEY,
    }  # fmt: skip
    empty = await _call(server, "hub_request_session", role="тестер", task=" ")
    assert empty.is_error and "task" in text_of(empty)

    shown = text_of(await _call(server, "hub_agent_requests"))
    head, _, data = shown.partition("----- ДАННЫЕ ")
    assert head.startswith("Заявки владельцу: 2 (открытые и за 7 дней).")
    assert "№3 · ждёт владельца · напарник в проекте 3" in data and "задача: прогони регресс" in data
    assert "№2 · отклонена · перенос беседы #12 в проект 4" in data and "слова владельца: не сейчас" in data
    assert "Ждущие решает владелец кнопкой в хабе" in shown

    # Сессия без подписки с ролью — отказ до запроса заявки.
    bare = Core({("GET", "/subs"): (200, {"items": [], "channels": []})})
    plain, _ = _server(make_server, config_dir, bare, environ=HELD)
    no_sub = await _call(plain, "hub_request_session", role="тестер", task="x")
    assert no_sub.is_error and "hub_subscribe" in text_of(no_sub)
    assert _bodies(bare, "/agent-requests") == []
