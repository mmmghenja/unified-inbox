"""`hub.engine.oc_turn.run_turn` на поддельном OpenCode (Ф16, 3.3, 3.4, П5, П6, F5–F11, F21)."""

import asyncio

import pytest

from hub.engine import oc_turn
from hub.engine.oc_turn import Allow, Ask, Deny, classify_error, run_turn

FAST = {"poll_s": 0.2, "timeout_s": 15.0}


async def _session(fake_oc, **scenario):
    fake_oc.scenario(**scenario)
    server = await fake_oc.start()
    client = server.client()
    sid = (await client.create_session("hub-test", agent="hub-test"))["id"]
    return server, client, sid


def _allow(req):
    return Allow()


def _replies(fake_oc, kind="permission"):
    return [r for r in fake_oc.jsonl("replies.jsonl") if r["kind"] == kind]


def _reply_calls(fake_oc):
    return [c for c in fake_oc.jsonl("calls.jsonl") if c["path"].startswith("/permission/")]


async def test_text_turn_ok(fake_oc):
    _, client, sid = await _session(fake_oc, turns=[[{"text": "черновик"}, {"text": "готово"}]])
    events = []
    async with client:
        result = await run_turn(
            client,
            sid,
            text="разбери",
            system="правила",
            agent="hub-test",
            model=("deepseek", "deepseek-flash"),
            variant="max",
            decide=_allow,
            on_event=events.append,
            **FAST,
        )
    assert result.ok and result.error is None
    assert result.text == "готово" and result.finish == "stop"
    assert result.cost == 0.001 and result.tokens["input"] == 10
    assert result.session_id == sid
    assert [e["properties"]["part"]["text"] for e in events if e["type"] == "message.part.updated"] == [
        "черновик",
        "готово",
    ]
    # Поток событий открыт ДО prompt.
    paths = [c["path"] for c in fake_oc.jsonl("calls.jsonl")]
    assert paths.index("/event") < paths.index(f"/session/{sid}/prompt_async")


async def test_allow_once_deny_reject_with_reason(fake_oc):
    turns = [
        [
            {
                "ask": {
                    "tool": "write",
                    "permission": "edit",
                    "args": {"filePath": "/w/a.txt", "content": "x"},
                }
            },
            {"ask": {"tool": "bash", "args": {"command": "git push"}}},
            {"text": "итог"},
        ]
    ]
    _, client, sid = await _session(fake_oc, turns=turns)
    seen = []

    def decide(req):
        seen.append((req.tool, req.permission, req.args, req.child))
        return Allow() if req.tool == "write" else Deny("гард хаба: push нельзя")

    async with client:
        result = await run_turn(client, sid, text="go", decide=decide, **FAST)
    assert result.ok and result.text == "итог"
    # Имя — по part.tool, аргументы — целиком из части сообщения (F7, П7).
    assert seen == [
        ("write", "edit", {"filePath": "/w/a.txt", "content": "x"}, False),
        ("bash", "bash", {"command": "git push"}, False),
    ]
    assert [(r["tool"], r["reply"], r["message"]) for r in _replies(fake_oc)] == [
        ("write", "once", None),
        ("bash", "reject", "гард хаба: push нельзя"),
    ]
    assert all(c["body"]["reply"] in ("once", "reject") for c in _reply_calls(fake_oc))


async def test_ask_waits_for_callback_and_without_callback_rejects(fake_oc):
    turns = [
        [{"ask": {"tool": "bash", "args": {"command": "git push"}}}, {"text": "a"}],
        [{"ask": {"tool": "bash", "args": {"command": "git push"}}}, {"text": "b"}],
    ]
    _, client, sid = await _session(fake_oc, turns=turns)
    asked = []

    async def on_ask(req, payload):
        asked.append(payload)
        await asyncio.sleep(0.3)  # владелец думает — ход стоит
        return Deny("владелец отказал")

    async with client:
        r1 = await run_turn(
            client, sid, text="1", decide=lambda req: Ask({"card": "push"}), on_ask=on_ask, **FAST
        )
        r2 = await run_turn(client, sid, text="2", decide=lambda req: Ask({"card": "push"}), **FAST)
    assert r1.ok and r2.ok
    assert asked == [{"card": "push"}]
    assert [(r["reply"], r["message"]) for r in _replies(fake_oc)] == [
        ("reject", "владелец отказал"),
        ("reject", oc_turn.NO_ASK_REASON),
    ]


async def test_ask_callback_allow_answers_once(fake_oc):
    _, client, sid = await _session(
        fake_oc, turns=[[{"ask": {"tool": "bash", "args": {"command": "ls"}}}, {"text": "x"}]]
    )
    async with client:
        await run_turn(client, sid, text="1", decide=lambda r: Ask({}), on_ask=lambda r, p: Allow(), **FAST)
    assert [r["reply"] for r in _replies(fake_oc)] == ["once"]


async def test_decide_crash_rejects(fake_oc):
    _, client, sid = await _session(
        fake_oc, turns=[[{"ask": {"tool": "read", "args": {"filePath": "/x"}}}, {"text": "x"}]]
    )

    def boom(req):
        raise RuntimeError("сломалось")

    async with client:
        result = await run_turn(client, sid, text="1", decide=boom, **FAST)
    assert result.ok
    [reply] = _replies(fake_oc)
    assert reply["reply"] == "reject" and "сбой проверки" in reply["message"]


@pytest.mark.parametrize("announce", [True, False], ids=["session.created", "parent-lookup"])
async def test_child_session_permission_goes_through_guard(fake_oc, announce):
    turns = [
        [
            {"ask": {"tool": "bash", "args": {"command": "rm -rf /"}}, "child": True, "announce": announce},
            {"text": "x"},
        ]
    ]
    _, client, sid = await _session(fake_oc, turns=turns)
    seen = []

    def decide(req):
        seen.append((req.session_id, req.child, req.args))
        return Deny("гард хаба: нет")

    async with client:
        result = await run_turn(client, sid, text="1", decide=decide, **FAST)
    assert result.ok
    [(child_sid, child, args)] = seen
    assert child and child_sid != sid and args == {"command": "rm -rf /"}
    [reply] = _replies(fake_oc)
    assert reply["child"] and reply["reply"] == "reject" and reply["message"] == "гард хаба: нет"
    if not announce:
        assert any(c["path"] == f"/session/{child_sid}" for c in fake_oc.jsonl("calls.jsonl"))


async def test_foreign_session_permission_not_answered(fake_oc):
    turns = [
        [
            {
                "ask": {"tool": "bash", "args": {"command": "ls"}},
                "foreign": True,
                "announce": False,
                "wait_s": 1.0,
            },
            {"text": "x"},
        ]
    ]
    _, client, sid = await _session(fake_oc, turns=turns)
    seen = []
    async with client:
        result = await run_turn(client, sid, text="1", decide=lambda r: seen.append(r) or Allow(), **FAST)
    assert result.ok
    assert seen == []
    [reply] = _replies(fake_oc)
    assert reply["foreign"] and reply["reply"] is None
    assert _reply_calls(fake_oc) == []


async def test_question_rejected_without_handler_and_answered_with_it(fake_oc):
    q = {
        "questions": [
            {
                "question": "Какой вариант?",
                "header": "Вариант",
                "options": [{"label": "A", "description": "a"}],
            }
        ]
    }
    turns = [[{"question": q}, {"text": "1"}], [{"question": q, "child": True}, {"text": "2"}]]
    _, client, sid = await _session(fake_oc, turns=turns)
    got = []

    def on_question(req):
        got.append((req.child, req.questions[0]["header"]))
        return [["A"]]

    async with client:
        assert (await run_turn(client, sid, text="1", decide=_allow, **FAST)).ok
        assert (await run_turn(client, sid, text="2", decide=_allow, on_question=on_question, **FAST)).ok
    assert got == [(True, "Вариант")]
    assert [(r["reply"], r.get("answers")) for r in _replies(fake_oc, "question")] == [
        ("reject", None),
        ("reply", [["A"]]),
    ]


async def test_sse_drop_reconnects_and_turn_ends_by_status_poll(fake_oc):
    turns = [
        [
            {"text": "начало"},
            {"permission_list_400": True},
            {"drop_sse": True},
            {"silent": True},  # события хода потеряны: session.idle не придёт
            {"sleep": 0.5},
            {"text": "конец"},
        ]
    ]
    _, client, sid = await _session(fake_oc, turns=turns)
    async with client:
        result = await run_turn(client, sid, text="1", decide=_allow, **FAST)
    assert result.ok and result.text == "конец"
    calls = fake_oc.jsonl("calls.jsonl")
    assert sum(c["path"] == "/event" for c in calls) >= 2
    # После переподключения — запасной GET /permission (400, F9) — ход не упал.
    assert any(c["path"] == "/permission" for c in calls)


async def test_permission_lost_in_stream_answered_via_pending_list(fake_oc):
    turns = [
        [
            {"ask": {"tool": "read", "args": {"filePath": "/w/a"}}, "hidden": True, "drop_after": True},
            {"text": "x"},
        ]
    ]
    _, client, sid = await _session(fake_oc, turns=turns)
    async with client:
        result = await run_turn(client, sid, text="1", decide=_allow, **FAST)
    assert result.ok
    assert [r["reply"] for r in _replies(fake_oc)] == ["once"]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ({"name": "ProviderAuthError", "message": "Invalid key"}, "auth"),
        ({"name": "APIError", "message": "Too Many Requests", "status": 429}, "limit"),
        ({"name": "APIError", "message": "Insufficient Balance"}, "limit"),
        ({"name": "UnknownError", "message": "Model not found: deepseek/nope"}, "model"),
        ({"name": "UnknownError", "message": "что-то сломалось"}, "fail"),
    ],
)
async def test_session_error_classified(fake_oc, error, kind):
    _, client, sid = await _session(fake_oc, turns=[[{"error": error}]])
    async with client:
        result = await run_turn(client, sid, text="1", decide=_allow, **FAST)
    assert not result.ok
    assert result.error["name"] == error["name"] and result.error["message"] == error["message"]
    assert classify_error(result.error, result.retry_s) == kind


async def test_timeout_aborts(fake_oc):
    _, client, sid = await _session(fake_oc, turns=[[{"text": "думаю"}, {"hang": True}]])
    async with client:
        result = await run_turn(client, sid, text="1", decide=_allow, poll_s=0.2, timeout_s=1.0)
    assert not result.ok and result.error["name"] == "timeout"
    assert any(c["path"] == f"/session/{sid}/abort" for c in fake_oc.jsonl("calls.jsonl"))


async def test_long_retry_is_limit(fake_oc):
    _, client, sid = await _session(fake_oc, turns=[[{"retry": "Rate limited, retrying"}, {"hang": True}]])
    async with client:
        result = await run_turn(
            client, sid, text="1", decide=_allow, poll_s=0.1, timeout_s=10.0, retry_limit_s=0.5
        )
    assert not result.ok and result.error == {"name": "retry", "message": "Rate limited, retrying"}
    assert result.retry_s >= 0.5
    # Ф83-1: долгий retry провайдера — отдельный вид `retry` (пауза — как у лимита, повод запасного — нет).
    assert classify_error(result.error, 700.0) == "retry"
    assert any(c["path"] == f"/session/{sid}/abort" for c in fake_oc.jsonl("calls.jsonl"))


async def test_prompt_to_missing_session_raises(fake_oc):
    from hub.engine.oc_server import SessionNotFound

    _, client, _ = await _session(fake_oc)
    async with client:
        with pytest.raises(SessionNotFound):
            await run_turn(client, "ses_missing", text="1", decide=_allow, **FAST)


@pytest.mark.parametrize(
    ("error", "retry", "kind"),
    [
        (None, None, "fail"),
        (None, 600.0, "retry"),
        ({"name": "UnknownError", "message": "x"}, 599.0, "fail"),
        ({"name": "APIError", "message": "Unauthorized", "status": 401}, None, "auth"),
        ({"name": "APIError", "message": "missing API key"}, None, "auth"),
        ({"name": "APIError", "message": "quota exceeded"}, None, "limit"),
        ({"name": "APIError", "message": "rate limit"}, None, "limit"),
        ({"name": "UnknownError", "message": "Model not found: x/y"}, None, "model"),
        ({"name": "MessageAbortedError", "message": "aborted"}, None, "fail"),
    ],
)
def test_classify_error_table(error, retry, kind):
    assert classify_error(error, retry) == kind


async def test_files_reach_prompt_async(fake_oc):
    _, client, sid = await _session(fake_oc, turns=[[{"text": "вижу"}]])
    part = {"type": "file", "mime": "image/png", "filename": "shot.png", "url": "data:image/png;base64,AAAA"}
    async with client:
        result = await run_turn(client, sid, text="что на картинке", files=[part], decide=_allow, **FAST)
    assert result.ok
    body = next(
        c["body"] for c in fake_oc.jsonl("calls.jsonl") if c["path"] == f"/session/{sid}/prompt_async"
    )
    assert body["parts"] == [{"type": "text", "text": "что на картинке"}, part]
